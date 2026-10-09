"""Offline T798 collector safety tests. Run with --noconftest: no DB required."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import time
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t798_collector", ROOT / "tools/test_ingress_readonly.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SECRET = "phak_DO_NOT_EXPORT_THIS_TEST_SECRET"
LOG_ENDPOINT = "https://" + m.APP + "-8090.dstack-pha-prod9.phala.network/logs?token=SIGNED_PRIVATE"
ROW = {"id": "a" * 64, "names": ["/" + m.CONTAINER], "state": "running",
       "image": m.IMAGE, "log_endpoint": LOG_ENDPOINT}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("OFFLINE_SUITE_FORBIDS_NETWORK")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def composition(rows=None, **extra):
    return json.dumps({"is_online": True, "error": None,
                       "containers": [ROW.copy()] if rows is None else rows, **extra}).encode()


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def get(self, url, *, auth):
        self.calls.append((url, auth))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_partial_evidence_does_not_claim_historical_coverage():
    r = Scripted(composition(), b"2026-10-09T11:17:01Z SSL handshake failure " + SECRET.encode())
    out = m.collect(r)
    assert len(r.calls) == 2
    assert r.calls[0] == (m.COMPOSITION, True)
    assert r.calls[1][1] is False
    assert "since=2026-10-09T11%3A16%3A30Z" in r.calls[1][0]
    assert "until=2026-10-09T11%3A21%3A30Z" in r.calls[1][0]
    assert out["logs"]["literal_indicator_counts"]["tls_handshake_failure_text"] == 1
    assert out["logs"]["complete_window"] is False
    assert out["restart_counts"]["status"] == "UNMEASURED"
    assert SECRET not in json.dumps(out)
    assert "SIGNED_PRIVATE" not in json.dumps(out)


@pytest.mark.parametrize("raw,coverage", [
    (b"", "EMPTY_UNPROVEN"),
    (b"2026-10-09T11:30:00Z SSL handshake failure", "OUT_OF_WINDOW"),
    (b"no timestamp: SSL handshake failure", "TIMESTAMPS_MISSING"),
    (b"2026-10-09T11:17:00Z harmless\n" * 300, "TAIL_SATURATED"),
])
def test_tail_empty_and_outside_are_not_no_error_proof(raw, coverage):
    result = m.project_logs(raw)
    assert result["coverage"] == coverage
    assert result["complete_window"] is False and result["no_errors_proven"] is False


@pytest.mark.parametrize("rows", [[], [ROW, ROW],
    [{**ROW, "names": ["prod-ingress-1"]}],
    [{**ROW, "names": ["feedling-test-ingress-10"]}],
    [{**ROW, "image": "evil:latest"}],
    [{**ROW, "state": "exited"}],
    [{**ROW, "id": "name; shell"}],
])
def test_identity_failure_never_fetches_logs(rows):
    r = Scripted(composition(rows), b"unused")
    with pytest.raises(m.Refused):
        m.collect(r)
    assert len(r.calls) == 1


@pytest.mark.parametrize("value", [
    "http://example.com/logs", "https://evil.example/logs",
    "https://127.0.0.1/logs", "https://api.feedling.app/logs",
    LOG_ENDPOINT.replace(m.APP, "f" * 40),
    LOG_ENDPOINT.replace("https://", "https://secret@"),
    LOG_ENDPOINT + "#fragment",
    LOG_ENDPOINT + "&since=other",
    LOG_ENDPOINT.replace(".network/", ".network:444/"),
    "https://cloud-api.phala.com/api/v1/cvms/prod/logs",
])
def test_endpoint_pin_blocks_redirected_or_other_resources(value):
    r = Scripted(composition([{**ROW, "log_endpoint": value}]))
    with pytest.raises(m.Refused):
        m.collect(r)
    assert len(r.calls) == 1


def test_no_raw_secret_user_body_or_untrusted_suffix_in_output(tmp_path):
    lines = [
        f"2026-10-09T11:17:00Z Authorization: Bearer {SECRET}",
        f"2026-10-09T11:17:01Z SSL handshake failure uri=/user?key={SECRET}",
        f"2026-10-09T11:17:02Z user-message: hi secret={SECRET}",
        SECRET,
    ]
    obj = {"channel": "stderr", "message": base64.b64encode("\n".join(lines).encode()).decode()}
    result = m.project_logs(json.dumps(obj).encode())
    m.save_report(tmp_path, result)
    body = (tmp_path / "report.json").read_text()
    assert SECRET not in body and "Authorization" not in body and "user-message" not in body
    assert obj["message"] not in body  # Encoded envelopes are also sensitive.
    assert set(result) == {"coverage", "complete_window", "no_errors_proven",
                           "returned_lines", "missing_timestamps", "outside_window",
                           "unclassified_lines", "earliest", "latest",
                           "literal_indicator_counts", "events"}
    assert result["missing_timestamps"] == 1
    assert result["unclassified_lines"] == 2


@pytest.mark.parametrize("raw", [
    b"line\n" * 301, b'{"wrong":"shape"}', b'{"message":false}',
    b'{"channel":"evil","message":"hello"}', b"\xff", b'{"channel":"stderr","message":"//8="}',
])
def test_bad_or_excess_log_input_stops(raw):
    with pytest.raises(m.Refused):
        m.project_logs(raw)


def test_invalid_timestamp_stops():
    with pytest.raises(m.Refused, match="invalid_timestamp"):
        m.project_logs(b"2026-99-09T11:17:00Z SSL handshake failure")


class Response(io.BytesIO):
    status = 200
    headers = {}


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_strict_tls_and_proxy_settings(monkeypatch):
    captured = []
    real = ssl.create_default_context
    def context():
        c = real()
        captured.append(c)
        return c
    monkeypatch.setattr(m.ssl, "create_default_context", context)
    r = m.Reader(SECRET)
    assert captured[0].verify_mode == ssl.CERT_REQUIRED and captured[0].check_hostname
    assert any(isinstance(x, m.NoRedirect) for x in r.opener.handlers)
    # ProxyHandler({}) installs no proxy-open methods; env never enters opener.
    assert not any(type(x).__name__ == "ProxyHandler" and x.proxies for x in r.opener.handlers)


def test_key_only_sent_to_fixed_composition():
    r = m.Reader(SECRET)
    r.opener = Opener(Response(b"{}"))
    r.get(m.COMPOSITION, auth=True)
    assert r.opener.calls[0].get_header("X-api-key") == SECRET
    r.opener = Opener(Response(b"hello"))
    r.get(m.logs_url(LOG_ENDPOINT), auth=False)
    assert r.opener.calls[0].get_header("X-api-key") is None
    with pytest.raises(m.Refused, match="request_limit"):
        r.get(m.COMPOSITION, auth=True)
    r = m.Reader(SECRET)
    with pytest.raises(m.Refused, match="credential_target_refused"):
        r.get(LOG_ENDPOINT, auth=True)
    assert r.records == []


@pytest.mark.parametrize("kind", ["input_limit", "permission", "timeout", "redirect", "transport", "encoding"])
def test_actual_reader_refuses_and_never_retries_or_leaks(kind, monkeypatch):
    r = m.Reader(SECRET)
    if kind == "input_limit":
        r.remaining = 20
        response = Response(b"x" * 21)
    elif kind == "permission":
        response = urllib.error.HTTPError(LOG_ENDPOINT, 403, SECRET, {}, io.BytesIO(SECRET.encode()))
    elif kind == "timeout":
        class Slow(Response):
            def read(self, size):
                time.sleep(.2)
                return b""
        response = Slow()
        monkeypatch.setattr(m, "SECONDS", .03)
    elif kind == "redirect":
        response = m.Refused("redirect_refused")
    elif kind == "transport":
        response = urllib.error.URLError(SECRET)
    else:
        response = Response(b"{}")
        response.headers = {"Content-Encoding": "gzip"}
    r.opener = Opener(response)
    with pytest.raises(m.Refused):
        r.get(m.COMPOSITION, auth=True)
    assert len(r.opener.calls) == 1
    assert SECRET not in json.dumps(r.records)
    assert "SIGNED_PRIVATE" not in json.dumps(r.records)
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    if kind == "input_limit":
        assert r.records[0]["bytes"] == 20  # No over-budget sentinel byte.


def test_aggregate_input_bound():
    r = m.Reader(SECRET)
    r.remaining = 5
    r.opener = Opener(Response(b"123"))
    r.get(m.COMPOSITION, auth=True)
    r.opener = Opener(Response(b"456"))
    with pytest.raises(m.Refused, match="input_limit"):
        r.get(m.logs_url(LOG_ENDPOINT), auth=False)


def test_redirect_handler_does_not_make_second_request():
    with pytest.raises(m.Refused, match="redirect_refused"):
        m.NoRedirect().redirect_request(None, None, 302, "secret", {}, "https://evil.example")


@pytest.mark.parametrize("key,value", [
    ("GITHUB_ACTIONS", "false"), ("GITHUB_EVENT_NAME", "push"),
    ("GITHUB_REF", "refs/heads/test"), ("GITHUB_SHA", "b" * 40),
    ("GITHUB_REPOSITORY", "other/repo"), ("EXPECTED_COLLECTOR_SHA", "invalid"),
    ("PHALA_CLOUD_API_KEY", ""),
])
def test_runtime_gate_stops_before_secret_transport(key, value):
    env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "GITHUB_REF": m.BRANCH, "GITHUB_SHA": "a" * 40,
           "GITHUB_REPOSITORY": "teleport-computer/feedling-mcp",
           "EXPECTED_COLLECTOR_SHA": "a" * 40, "PHALA_CLOUD_API_KEY": SECRET}
    m.runtime_gate(env)
    env[key] = value
    with pytest.raises(m.Refused):
        m.runtime_gate(env)


def test_main_refusal_and_plan_never_create_network(monkeypatch, tmp_path, capsys):
    out = tmp_path / "plan"
    assert m.main(["--out", str(out)]) == 0
    assert not out.exists()
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert m.main(["--out", str(out), "--run"]) == 2
    result = json.loads((out / "report.json").read_text())
    assert result["reason"] == "execution_identity_refused"
    assert "requests" not in result
    assert SECRET not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        m.main(["--out", str(out), "--run"])


def test_output_bound_has_fail_closed_receipt(tmp_path):
    assert m.save_report(tmp_path, {"sensitive": SECRET * 9999}) < m.OUTPUT_LIMIT
    obj = json.loads((tmp_path / "report.json").read_text())
    assert obj["reason"] == "output_limit" and obj["result"] == "STOP"


def test_workflow_dispatch_is_the_only_entry_and_artifact_is_report_only():
    import yaml
    cfg = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(cfg["on"]) == {"workflow_dispatch"}
    assert list(cfg["jobs"]) == ["collect"]
    job = cfg["jobs"]["collect"]
    assert m.BRANCH in job["if"] and "github.sha == inputs.expected_sha" in job["if"]
    assert cfg["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] == "5"
    steps = job["steps"]
    assert steps[0]["with"]["persist-credentials"] == "false"
    assert steps[-1]["with"]["path"] == "t798-ingress-evidence/report.json"
    assert sum("PHALA_CLOUD_API_KEY" in x.get("env", {}) for x in steps) == 1
    assert all(not any(w in x.get("run", "") for w in ["deploy", "restart", "curl", "sudo", "npm"]) for x in steps)


def test_same_ref_other_workflows_cannot_fire_on_collector_branch_push():
    import fnmatch
    import yaml
    branch = m.BRANCH.removeprefix("refs/heads/")
    for file in (ROOT / ".github/workflows").glob("*.yml"):
        cfg = yaml.load(file.read_text(), Loader=yaml.BaseLoader)
        events = cfg.get("on", {})
        assert isinstance(events, dict), file.name
        assert not ({"workflow_run", "workflow_call", "repository_dispatch"} & set(events)), file.name
        assert not any("uses" in job for job in cfg.get("jobs", {}).values()), file.name
        if "push" in events:
            assert isinstance(events["push"], dict), file.name
            branches = events["push"].get("branches")
            assert isinstance(branches, list) and branches, file.name
            assert not any(fnmatch.fnmatchcase(branch, value) for value in branches), file.name


def test_source_never_exposes_raw_exception_signed_url_or_secret(monkeypatch, tmp_path, capsys):
    good = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": m.BRANCH, "GITHUB_SHA": "a" * 40,
            "GITHUB_REPOSITORY": "teleport-computer/feedling-mcp",
            "EXPECTED_COLLECTOR_SHA": "a" * 40, "PHALA_CLOUD_API_KEY": SECRET}
    for key, value in good.items():
        monkeypatch.setenv(key, value)
    def failure(reader):
        raise ValueError(SECRET + LOG_ENDPOINT)
    monkeypatch.setattr(m, "collect", failure)
    assert m.main(["--out", str(tmp_path / "out"), "--run"]) == 2
    saved = (tmp_path / "out/report.json").read_text()
    assert SECRET not in saved and "SIGNED_PRIVATE" not in saved
    assert json.loads(saved)["reason"] == "unexpected_local_failure"
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("suffix", [
    "../other-cvm/logs", "%2e%2e/other-cvm/logs", "%2E%2E/other-cvm/logs",
    "%252e%252e/other-cvm/logs", "%25252e%25252e/other-cvm/logs",
    "./logs", "logs/../other-cvm/logs", "logs/./tail",
    "..%2fother-cvm/logs", "%2e.%2Fother-cvm/logs",
    "logs%2ftail", "logs%252ftail", "logs%5ctail", "logs%255ctail",
    "logs\\..\\other-cvm", "logs//tail", "/logs",
    "logs;\u002e\u002e/other-cvm", "logs\x00", "logs\r", "logs\n", "logs\t",
    "logs%00", "logs%0d%0a", "logs%250d%250a", "logs/\uff0e\uff0e/tail",
])
@pytest.mark.parametrize("origin", [
    m.API + "/cvms/" + m.CVM + "/",
    "https://" + m.APP + "-8090.dstack-pha-prod9.phala.network/",
])
def test_ambiguous_path_is_refused_before_second_get(origin, suffix):
    reader = Scripted(composition([{**ROW, "log_endpoint": origin + suffix}]), b"unused")
    with pytest.raises(m.Refused, match="log_endpoint_refused"):
        m.collect(reader)
    assert reader.calls == [(m.COMPOSITION, True)]


@pytest.mark.parametrize("url", [
    "\n" + LOG_ENDPOINT, "\x00" + LOG_ENDPOINT, " " + LOG_ENDPOINT,
    LOG_ENDPOINT.replace("https://", "https:\\\\"),
    LOG_ENDPOINT + "&x=%0d%0a", LOG_ENDPOINT + "&x=%250d",
    LOG_ENDPOINT + "&%73ince=2026", LOG_ENDPOINT + "&key%2fpart=anything",
])
def test_query_and_preparse_ambiguity_refused_without_log_request(url):
    reader = Scripted(composition([{**ROW, "log_endpoint": url}]), b"unused")
    with pytest.raises(m.Refused):
        m.collect(reader)
    assert len(reader.calls) == 1


@pytest.mark.parametrize("path", ["logs", "logs/tail_1", "containers/" + "a" * 64 + "/logs"])
def test_safe_ascii_fixed_cvm_paths_are_preserved(path):
    import posixpath
    endpoint = m.API + "/cvms/" + m.CVM + "/" + path
    accepted = m.logs_url(endpoint)
    normalized = posixpath.normpath(m.urllib.parse.unquote(m.urllib.parse.urlsplit(accepted).path))
    assert normalized.startswith("/api/v1/cvms/" + m.CVM + "/")
    assert m.urllib.parse.urlsplit(accepted).path == m.urllib.parse.urlsplit(endpoint).path


@pytest.mark.parametrize("size", [20, 21, 999])
def test_exact_input_cap_refuses_without_reading_a_sentinel(size):
    reader = m.Reader(SECRET)
    reader.remaining = 20
    reader.opener = Opener(Response(b"x" * size))
    with pytest.raises(m.Refused, match="input_limit"):
        reader.get(m.COMPOSITION, auth=True)
    assert reader.remaining == 0
    assert reader.records[0]["bytes"] == 20
