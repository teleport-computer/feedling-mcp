"""Offline guard verification: zero real network calls."""
import importlib.util
import io
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request

import pytest

spec = importlib.util.spec_from_file_location(
    "access_once", Path(__file__).parents[1] / "tools/t798_cvm_access_once.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def valid_env():
    return dict(EXPECTED_DIAGNOSTIC_SHA="a" * 40, GITHUB_SHA="a" * 40,
                GITHUB_EVENT_NAME="workflow_dispatch",
                GITHUB_REPOSITORY="teleport-computer/feedling-mcp",
                GITHUB_REF="refs/heads/fix/t798-cvm-access-once",
                GITHUB_RUN_ATTEMPT="1")


@pytest.mark.parametrize("field", list(valid_env()))
def test_guard_rejects_each_wrong_context(field):
    env = valid_env()
    env[field] = "wrong"
    assert not m.permitted(env, "a" * 40)


def test_guard_rejects_wrong_checkout():
    assert m.permitted(valid_env(), "a" * 40)
    assert not m.permitted(valid_env(), "b" * 40)


@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
def test_failure_status_cannot_claim_target_read(status):
    report = m.summarize(status, json.dumps({"id": m.TARGET}).encode())
    assert report["outcome"] == "diagnostic_stop"


def test_only_expected_id_and_status_can_pass():
    assert m.summarize(200, b'{"id":"another"}')["outcome"] == "diagnostic_stop"
    assert m.summarize(200, json.dumps({"id": m.TARGET}).encode())["outcome"] == "target_read_ok"


def test_unknown_error_text_and_metadata_never_escape():
    body = json.dumps({"code": "SECRET", "error": {"code": "SECRET"},
                       "message": "SECRET", "compose_file": "SECRET"}).encode()
    assert "SECRET" not in json.dumps(m.summarize(403, body))
    assert m.summarize(403, b'{"error":{"code":"forbidden"}}')["provider_code"] == "forbidden"


def test_exact_body_cap_stops_even_valid_json():
    body = b'{"id":"' + m.TARGET.encode() + b'"}'
    body += b" " * (m.CAP - len(body))
    assert m.summarize(200, body)["outcome"] == "body_cap_stop"


def test_no_redirect_fallback():
    assert m.NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.invalid") is None


def test_http403_is_single_get_bounded_and_strict(monkeypatch):
    calls = []

    class Body(io.BytesIO):
        def read(self, size=-1):
            assert size == 16384
            return super().read(size)

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 30
            assert request.full_url == m.URL
            assert request.get_method() == "GET"
            assert request.get_header("X-api-key") == "test-only"
            assert request.get_header("X-phala-version") == "2025-10-28"
            raise urllib.error.HTTPError(m.URL, 403, "Forbidden", {}, Body(b'{"code":"forbidden"}'))

    def build(*handlers):
        proxy, https, redirect = handlers
        assert proxy.proxies == {}
        assert https._context.check_hostname is True
        assert https._context.verify_mode == ssl.CERT_REQUIRED
        assert isinstance(redirect, m.NoRedirect)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    assert m.fetch_once("test-only")["provider_code"] == "forbidden"
    assert len(calls) == 1


def test_main_guard_sends_zero_requests(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXPECTED_DIAGNOSTIC_SHA", raising=False)
    monkeypatch.setattr(m.subprocess, "check_output", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(m, "fetch_once", lambda key: pytest.fail("network guard bypass"))
    assert m.main() == 2
    assert json.loads((tmp_path / "t798-cvm-access.json").read_text())["requests"] == 0
