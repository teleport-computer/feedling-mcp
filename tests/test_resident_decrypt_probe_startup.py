"""Startup enclave outages must not cause a resident restart/circuit-open loop."""

import logging
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
os.environ.setdefault("AGENT_MODE", "http")
os.environ.setdefault("AGENT_HTTP_URL", "http://localhost:8080/chat")

from tools import chat_resident_consumer as consumer  # noqa: E402


@pytest.fixture
def probe(monkeypatch):
    """Inject only the transport: client routing, requests and status errors are real."""
    monkeypatch.setattr(consumer, "FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(consumer, "ENCLAVE_FETCH_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(consumer, "ENCLAVE_FETCH_BACKOFF_SEC", 0)
    monkeypatch.setattr(consumer, "ENCLAVE_CONNECT_TIMEOUT_SEC", 1.25)
    monkeypatch.setattr(consumer, "ENCLAVE_READ_TIMEOUT_SEC", 7.5)
    monkeypatch.setitem(consumer._decrypt_health, "status", "unknown")
    monkeypatch.setitem(consumer._decrypt_health, "checked_at", 0.0)
    monkeypatch.setitem(consumer._decrypt_health_last_refresh, "at", 0.0)
    requests = []
    clients = []

    def install(outcome):
        def handle(request):
            requests.append(request)
            return outcome(request, len(requests))

        client = httpx.Client(
            transport=httpx.MockTransport(handle),
            timeout=consumer._enclave_http_timeout(),
        )
        clients.append(client)
        monkeypatch.setattr(consumer, "_ENCLAVE_CLIENT", client)
        return requests

    yield install
    for client in clients:
        client.close()


@pytest.mark.parametrize("error_type", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_startup_probe_retries_transport_until_last_attempt(probe, error_type):
    attempts = consumer.ENCLAVE_FETCH_MAX_ATTEMPTS

    def outcome(request, count):
        if count < attempts:
            raise error_type("transient enclave timeout", request=request)
        return httpx.Response(200, json={"messages": []})

    requests = probe(outcome)
    assert consumer._verify_decrypt_sources() is True
    assert len(requests) == attempts
    assert consumer._decrypt_health["status"] == "ok"
    for request in requests:
        assert request.url.path == "/v1/chat/history"
        assert dict(request.url.params) == {"limit": "1"}
        assert request.extensions["timeout"]["connect"] == 1.25
        assert request.extensions["timeout"]["read"] == 7.5


def test_startup_probe_exhaustion_returns_false_and_reports_unreachable(probe):
    def outcome(request, _count):
        raise httpx.ReadTimeout("enclave unavailable", request=request)

    requests = probe(outcome)
    assert consumer._verify_decrypt_sources() is False
    assert len(requests) == consumer.ENCLAVE_FETCH_MAX_ATTEMPTS
    assert consumer._decrypt_health["status"] == "unreachable"
    assert consumer._decrypt_health["checked_at"] > 0
    assert consumer._decrypt_health_last_refresh["at"] > 0


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_startup_probe_deterministic_4xx_is_not_retried(probe, status):
    requests = probe(lambda request, count: httpx.Response(status))
    assert consumer._verify_decrypt_sources() is False
    assert len(requests) == 1
    assert consumer._decrypt_health["status"] == "unreachable"


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_startup_probe_retries_transient_status_with_bounded_backoff(
    probe, monkeypatch, status
):
    sleeps = []
    monkeypatch.setattr(consumer, "ENCLAVE_FETCH_BACKOFF_SEC", 0.5)
    monkeypatch.setattr(consumer.time, "sleep", sleeps.append)
    requests = probe(lambda request, count: httpx.Response(status))
    assert consumer._verify_decrypt_sources() is False
    assert len(requests) == consumer.ENCLAVE_FETCH_MAX_ATTEMPTS
    assert sleeps == [0.5, 1.0]
    assert consumer._decrypt_health["status"] == "unreachable"


@pytest.mark.parametrize("status", [None, 403])
def test_run_enters_poll_loop_after_failed_startup_probe(
    probe, monkeypatch, caplog, status
):
    def outcome(request, _count):
        if status is None:
            raise httpx.ConnectTimeout("enclave unavailable", request=request)
        return httpx.Response(status)

    requests = probe(outcome)
    monkeypatch.setattr(consumer, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(consumer, "_load_whoami_with_retries", lambda: True)
    monkeypatch.setattr(consumer, "_warn_if_agent_entry_may_drift", lambda: None)
    monkeypatch.setattr(consumer, "_resident_ipc_listener_enabled", lambda: False)
    monkeypatch.setattr(consumer, "_load_checkpoint", lambda: 123.0)
    monkeypatch.setattr(consumer, "_save_checkpoint", lambda ts: None)
    monkeypatch.setattr(consumer, "_load_proactive_checkpoint", lambda: 123.0)
    monkeypatch.setattr(consumer, "PROACTIVE_POLL_ENABLED", False)
    monkeypatch.setattr(consumer, "CAPTURE_TICK_ENABLED", False)
    monkeypatch.setattr(consumer, "_running", True)

    class PollLoopReached(BaseException):
        """Stop at the first instruction inside the real poll loop."""

    poll_health = []

    def reached_poll_loop():
        poll_health.append(dict(consumer._decrypt_health))
        raise PollLoopReached

    monkeypatch.setattr(consumer, "_refresh_auth_header", reached_poll_loop)
    with caplog.at_level(logging.ERROR), pytest.raises(PollLoopReached):
        consumer.run()
    assert poll_health[0]["status"] == "unreachable"
    assert len(requests) == (consumer.ENCLAVE_FETCH_MAX_ATTEMPTS if status is None else 1)
    assert any(
        record.levelno == logging.ERROR
        and "decrypt source unreachable at startup" in record.getMessage()
        and "continuing" in record.getMessage()
        for record in caplog.records
    )
