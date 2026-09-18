"""T547: threshold, measurement boundaries, real curl controls and CI wiring."""
from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import conftest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.strict_yaml import load_yaml_strict

SCRIPT = ROOT / ".github/workflows/tcp_connect_probe.py"
spec = importlib.util.spec_from_file_location("tcp_connect_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def result(*, code=200, connect=0.02, exit=0, **kwargs):
    raw = dict(http_code=code, time_connect=connect, time_namelookup=0.01,
               time_appconnect=0.04, time_total=0.08, remote_ip="192.0.2.1", remote_port=443)
    raw.update(kwargs)
    return subprocess.CompletedProcess([], exit, json.dumps(raw), "curl diagnostic" if exit else "")


@pytest.mark.parametrize("exit,connect,code,failed,stage", [
    (0, 0.02, 200, False, "none"),
    (0, 0.02, 503, False, "none"),  # HTTP failure still proves TCP/HTTP reachability.
    (0, 0.02, 403, False, "none"),
    (7, 0, 0, True, "before_tcp_complete"),
    (28, 0, 0, True, "before_tcp_complete"),
    (6, 0, 0, True, "dns"),
    (35, 0.02, 0, True, "after_tcp_complete"),  # TLS is not a TCP failure.
    (60, 0.02, 0, True, "after_tcp_complete"),
    (28, 0.02, 0, True, "after_tcp_complete"),
    (0, 0, 0, True, "before_tcp_complete"),
])
def test_raw_failure_classification(monkeypatch, exit, connect, code, failed, stage):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: result(exit=exit, connect=connect, code=code))
    row = probe.sample(probe.TARGETS[0], 1)
    assert row["curl_exit"] == exit
    assert row["http_code"] == f"{code:03d}"
    assert row["time_connect"] == connect
    assert row["probe_failed"] is failed
    assert row["tcp_not_established"] is (connect == 0)
    assert row["failure_stage"] == stage


@pytest.mark.parametrize("bad", ["", "{}", "not json", "[]", '{"time_connect": NaN}'])
def test_bad_metrics_are_unknown_not_success(monkeypatch, bad):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, bad, ""))
    row = probe.sample(probe.TARGETS[0], 1)
    assert row["measurement_error"]
    assert row["probe_failed"] is None
    assert row["time_connect"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True, "0.02"])
def test_invalid_timing_is_not_a_measured_sample(monkeypatch, bad):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: result(connect=bad))
    assert probe.sample(probe.TARGETS[0], 1)["measurement_error"]


@pytest.mark.parametrize("error", [FileNotFoundError("curl"), subprocess.TimeoutExpired("curl", 10)])
def test_missing_or_stuck_curl_is_unknown(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(probe.subprocess, "run", fail)
    row = probe.sample(probe.TARGETS[0], 1)
    assert row["measurement_error"] == type(error).__name__
    assert row["curl_exit"] is None and row["probe_failed"] is None


@pytest.mark.parametrize("failures,expected,status", [(0, 0, "BELOW_THRESHOLD"), (1, 0, "BELOW_THRESHOLD"),
                                                     (2, 1, "RED"), (10, 1, "RED")])
def test_full_run_threshold_and_raw_artifacts(monkeypatch, tmp_path, failures, expected, status):
    calls = []
    def run(command, **kwargs):
        assert command[:2] == ["curl", "-q"]
        assert command[command.index("--noproxy") + 1] == "*"
        assert command[command.index("--proxy") + 1] == ""
        assert command[command.index("--retry") + 1] == "0"
        assert "--http1.1" in command and "--location" not in command
        assert "--insecure" not in command and "--fail" not in command
        assert kwargs["timeout"] == 10 and kwargs["check"] is False
        calls.append(command[-1])
        bad = command[-1] == probe.TARGETS[0] and calls.count(command[-1]) <= failures
        return result(exit=7 if bad else 0, connect=0 if bad else 0.02, code=0 if bad else 200)
    monkeypatch.setattr(probe.subprocess, "run", run)
    conftest.capture_sleeps(monkeypatch, probe)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "job-summary.md"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    assert probe.main(["--output", str(tmp_path)]) == expected
    assert calls == list(probe.TARGETS) * 10  # New process per attempt, interleaved hosts.
    rows = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert len(rows) == 40 and all(r["run_id"] == "123" for r in rows)
    assert all(r["observer"] == "github-actions" and r["started_at_utc"].endswith("+00:00") for r in rows)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["hosts"][0]["status"] == status
    assert summary["hosts"][0]["probe_failures"] == failures
    assert summary["hosts"][0]["failure_rate"] == failures / 10
    assert all(r["status"] == "BELOW_THRESHOLD" for r in summary["hosts"][1:])
    markdown = (tmp_path / "job-summary.md").read_text()
    assert markdown == (tmp_path / "summary.md").read_text()
    assert ("🔴 RED" in markdown) is (failures >= 2)
    assert "time_connect (s)" in markdown and "curl exit" in markdown


def test_all_measurements_missing_never_green(monkeypatch, tmp_path):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 2, "", "bad curl"))
    conftest.capture_sleeps(monkeypatch, probe)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert probe.main(["--output", str(tmp_path)]) == 2
    payload = json.loads((tmp_path / "summary.json").read_text())
    assert all(h["measured"] == 0 and h["failure_rate"] is None for h in payload["hosts"])
    assert "🟢" not in (tmp_path / "summary.md").read_text()
    assert all(h["status"] == "UNMEASURED" for h in probe.aggregate([]))


@pytest.mark.parametrize("reachable", [True, False])
def test_real_curl_ten_sample_green_and_red_controls(monkeypatch, tmp_path, reachable):
    """Known answers from OS sockets, not fake curl metrics or public-IP assumptions."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) if reachable else None
    listener = socket.socket()
    if server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        port = server.server_port
    else:
        # Release a fresh ephemeral port. A bound non-listening socket times out
        # on macOS; assert actual exit 7 below rather than assume it is refusal.
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
    monkeypatch.setattr(probe, "TARGETS", (f"http://127.0.0.1:{port}/",))
    conftest.capture_sleeps(monkeypatch, probe)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # A poisoned environment proxy must not affect direct loopback measurements.
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    try:
        assert probe.main(["--output", str(tmp_path)]) == (0 if reachable else 1)
    finally:
        listener.close()
        if server:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
    host = json.loads((tmp_path / "summary.json").read_text())["hosts"][0]
    assert host["measured"] == 10
    assert host["probe_failures"] == (0 if reachable else 10)
    assert host["tcp_not_established"] == (0 if reachable else 10)
    assert host["status"] == ("BELOW_THRESHOLD" if reachable else "RED")
    assert host["http_codes"] == ({"200": 10} if reachable else {"000": 10})
    assert host["connect_errors"] == (0 if reachable else 10)
    assert host["timeouts"] == 0
    rows = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert all(r["curl_exit"] == (0 if reachable else 7) for r in rows)


def test_workflow_schedule_canary_and_artifact_retention():
    workflow = load_yaml_strict((ROOT / ".github/workflows/tcp-connect-monitor.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"] == [{"cron": "7,22,37,52 * * * *"}]
    assert "workflow_dispatch" in triggers
    for event in ("pull_request", "push"):
        assert set(triggers[event]["paths"]) == {".github/workflows/tcp-connect-monitor.yml",
                                                  ".github/workflows/tcp_connect_probe.py"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["sample"]
    assert job["timeout-minutes"] >= 8
    assert any(s.get("run") == "python3 .github/workflows/tcp_connect_probe.py --output tcp-connect-results"
               for s in job["steps"])
    upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"
    assert upload["with"]["retention-days"] >= 14
    assert upload["with"]["if-no-files-found"] == "error"
    assert "github.run_id" in upload["with"]["name"] and "github.run_attempt" in upload["with"]["name"]
    assert "secrets." not in json.dumps(workflow)
    assert tuple(probe.TARGETS) == ("https://api.feedling.app/healthz", "https://test-api.feedling.app/healthz",
                                    "https://test-enclave.feedling.app/healthz", "https://cloudflare.com/")
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "tests/test_tcp_connect_monitor.py" in ci


def test_real_connect_timeout_ten_sample_red_control(monkeypatch, tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = listener.getsockname()[1]
    connections = []
    try:
        # macOS can admit 128 connections despite backlog=0; Linux differs.
        # Fill the actual queue before calling curl; never assume listen(0) is full.
        for _ in range(256):
            conn = socket.socket()
            conn.settimeout(0.03)
            try:
                conn.connect(("127.0.0.1", port))
            except TimeoutError:
                conn.close()
                break
            connections.append(conn)
        else:
            pytest.fail("could not establish the known connect-timeout control")
        monkeypatch.setattr(probe, "TARGETS", (f"https://127.0.0.1:{port}/",))
        monkeypatch.setattr(probe, "CONNECT_TIMEOUT", 0.3)
        monkeypatch.setattr(probe, "MAX_TIME", 0.5)
        conftest.capture_sleeps(monkeypatch, probe)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        assert probe.main(["--output", str(tmp_path)]) == 1
    finally:
        for conn in connections:
            conn.close()
        listener.close()
    rows = [json.loads(line) for line in (tmp_path / "samples.jsonl").read_text().splitlines()]
    assert len(rows) == 10
    assert all(r["curl_exit"] == 28 and r["time_connect"] == 0 for r in rows)
    assert all(r["http_code"] == "000" and r["error_kind"] == "timeout" for r in rows)
    host = json.loads((tmp_path / "summary.json").read_text())["hosts"][0]
    assert host["status"] == "RED" and host["timeouts"] == 10 and host["connect_errors"] == 0
