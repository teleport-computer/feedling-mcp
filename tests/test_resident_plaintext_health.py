"""Read-source readiness: authenticated backend probes never vouch for enclave."""
import base64
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
for key, value in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http", "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}.items():
    os.environ.setdefault(key, value)

from tools import chat_resident_consumer as c  # noqa: E402
from chat import consumer as backend, resident_maintenance as maintenance  # noqa: E402


@pytest.fixture
def wire(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "FEEDLING_API_URL", "https://api.test")
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(c, "_whoami_cache", {"content_encryption_effective": "off"})
    monkeypatch.setattr(c, "_decrypt_health", {"status": "unknown", "checked_at": 0.0})
    monkeypatch.setattr(c, "_decrypt_health_route", {"plaintext": None}, raising=False)
    monkeypatch.setattr(c, "_decrypt_health_last_refresh", {"at": 0.0})
    monkeypatch.setattr(c, "_decrypt_read_failures", {"count": 0})
    monkeypatch.setattr(c, "DECRYPT_HEALTH_FILE", tmp_path / "shared.json")
    monkeypatch.setattr(c, "DECRYPT_HEALTH_SHARED", True)
    monkeypatch.setattr(c, "ENCLAVE_FETCH_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(c, "DECRYPT_SELFCHECK", True)
    clock = [1000.0]
    monkeypatch.setattr(c.time, "time", lambda: clock[0])
    requests = []
    clients = []

    def install(handler):
        def record(request):
            requests.append(request)
            return handler(request)
        client = httpx.Client(transport=httpx.MockTransport(record))
        clients.append(client)
        monkeypatch.setattr(c, "_HTTP", client)
        monkeypatch.setattr(c, "_ENCLAVE_CLIENT", client)
        return requests

    yield install, clock
    for client in clients:
        client.close()


@pytest.mark.parametrize("auth", [{"X-Api-Key": "test-key"}, {"X-Feedling-Runtime-Token": "test-token"}])
@pytest.mark.parametrize("url", ["", "https://enclave.test"])
@pytest.mark.parametrize("page", [{"messages": []}, {"history": []}, {"messages": [{"id": "old", "ts": 1, "body_ct": "sealed"}]}])
def test_off_probe_is_authenticated_backend_read_not_decryption(wire, monkeypatch, auth, url, page):
    install, clock = wire
    monkeypatch.setattr(c, "_HEADERS", auth)
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", url)
    requests = install(lambda request: httpx.Response(200, json=page))
    assert c._verify_decrypt_sources() is True
    assert c._decrypt_health == {"status": "backend_ready", "checked_at": clock[0]}
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "api.test"
    assert request.url.path == "/v1/chat/history"
    assert dict(request.url.params) == {"limit": "1", "include_image_body": "false"}
    assert all(request.headers[k] == v for k, v in auth.items())
    assert not c.DECRYPT_HEALTH_FILE.exists()


@pytest.mark.parametrize("outcome", [401, 403, 404, 500, "timeout", "json", None, [], {},
                                     {"messages": None}, {"messages": [None]},
                                     {"messages": [{"id": "x"}]},
                                     {"messages": [{"ts": 1}]},
                                     {"messages": [{"id": "x", "ts": "nan"}]}])
def test_backend_failure_never_becomes_ready_from_shared_ok(wire, outcome):
    install, clock = wire
    c.DECRYPT_HEALTH_FILE.write_text(json.dumps({"status": "ok", "checked_at": clock[0]}))
    before = c.DECRYPT_HEALTH_FILE.read_bytes()
    def handle(request):
        assert request.url.host == "api.test"
        if outcome == "timeout":
            raise httpx.ReadTimeout("offline", request=request)
        if outcome == "json":
            return httpx.Response(200, content=b"bad json")
        if isinstance(outcome, int):
            return httpx.Response(outcome)
        return httpx.Response(200, content=json.dumps(outcome))
    requests = install(handle)
    c._maybe_refresh_decrypt_health()
    assert c._decrypt_health["status"] == "backend_unreachable"
    assert len(requests) == 1
    clock[0] += 1
    c._maybe_refresh_decrypt_health()
    assert len(requests) == 1
    assert c.DECRYPT_HEALTH_FILE.read_bytes() == before


@pytest.mark.parametrize("backend_ok", [True, False])
@pytest.mark.parametrize("enclave_ok", [True, False])
def test_two_consumers_share_only_enclave_evidence(wire, monkeypatch, backend_ok, enclave_ok):
    install, clock = wire
    def handle(request):
        if request.url.host == "api.test":
            return httpx.Response(200 if backend_ok else 503, json={"messages": []})
        return httpx.Response(200, json={"decrypt": "ok" if enclave_ok else "fail", "loopback": "ok"})
    requests = install(handle)
    def select_consumer(mode):
        # Independent process-local dictionaries; both consumers use the same
        # real file and HTTP transport, as on a host-all runner.
        monkeypatch.setattr(c, "_whoami_cache", {"content_encryption_effective": mode})
        monkeypatch.setattr(c, "_decrypt_health", {"status": "unknown", "checked_at": 0.0})
        monkeypatch.setattr(c, "_decrypt_health_route", {"plaintext": None}, raising=False)
        monkeypatch.setattr(c, "_decrypt_health_last_refresh", {"at": 0.0})
    select_consumer("off")
    c._maybe_refresh_decrypt_health()
    assert c._decrypt_health["status"] == ("backend_ready" if backend_ok else "backend_unreachable")
    assert not c.DECRYPT_HEALTH_FILE.exists()
    select_consumer("on")
    c._maybe_refresh_decrypt_health()
    assert c._decrypt_health["status"] == ("ok" if enclave_ok else "unreachable")
    assert c.DECRYPT_HEALTH_FILE.exists() is enclave_ok
    select_consumer("off")
    c._maybe_refresh_decrypt_health()
    assert c._decrypt_health["status"] == ("backend_ready" if backend_ok else "backend_unreachable")
    assert [r.url.host for r in requests] == ["api.test", "enclave.test", "api.test"]
    if enclave_ok:
        select_consumer("on")
        clock[0] += 1
        c._maybe_refresh_decrypt_health()
        assert len(requests) == 3
        assert c._decrypt_health == {"status": "ok", "checked_at": 1000.0}


@pytest.mark.parametrize("start,end", [("off", "on"), ("on", "off"), ("off", "unknown")])
def test_mode_change_discards_old_health_and_probe_throttle(wire, monkeypatch, start, end):
    install, _clock = wire
    monkeypatch.setattr(c, "DECRYPT_HEALTH_SHARED", False)
    requests = install(lambda request: httpx.Response(200, json={"messages": [], "decrypt": "ok", "loopback": "ok"}))
    c._whoami_cache["content_encryption_effective"] = start
    c._verify_decrypt_sources()
    c._whoami_cache["content_encryption_effective"] = end
    assert c._decrypt_health_headers() == {}
    assert c._decrypt_health_last_refresh["at"] == 0
    c._maybe_refresh_decrypt_health()
    assert len(requests) == 2
    assert c._decrypt_health["status"] == ("backend_ready" if end == "off" else "ok")
    assert requests[-1].url.host == ("api.test" if end == "off" else "enclave.test")


def test_backend_read_degrade_survives_probes_until_real_read_success(wire):
    install, clock = wire
    install(lambda request: httpx.Response(200, json={"messages": []}))
    for _ in range(c.DECRYPT_DEGRADE_AFTER):
        c._note_decrypt_read_failure()
    for status in ("backend_ready", "backend_unreachable", "unconfigured"):
        clock[0] += 1
        c._apply_infra_health(status)
        assert c._decrypt_health == {"status": "backend_degraded", "checked_at": clock[0]}
    c._note_decrypt_read_success()
    assert c._decrypt_health["status"] == "backend_ready"
    assert c._decrypt_read_failures["count"] == 0


@pytest.mark.parametrize("mode", ["on", "unknown"])
@pytest.mark.parametrize("auth", [{"X-API-Key": "test-key"}, {"X-Feedling-Runtime-Token": "test-token"}])
@pytest.mark.parametrize("status,page,expected,fallback", [
    (401, {}, "ok", True), (404, {}, "ok", True),
    (403, {}, "unreachable", False), (500, {}, "unreachable", False),
    (200, [], "unreachable", False), (200, None, "unreachable", False),
    (200, "broken", "unreachable", False),
    (200, {"decrypt": "fail", "loopback": "ok"}, "unreachable", False),
    (200, {"decrypt": "ok", "loopback": "fail"}, "unreachable", False),
    (200, {"decrypt": "ok", "loopback": "ok"}, "ok", False),
])
def test_encrypted_selfcheck_contract(wire, monkeypatch, mode, auth, status, page, expected, fallback):
    install, _clock = wire
    c._whoami_cache["content_encryption_effective"] = mode
    monkeypatch.setattr(c, "_HEADERS", auth)
    def handle(request):
        assert all(request.headers[k] == v for k, v in auth.items())
        assert request.url.host == "enclave.test"
        if request.url.path.endswith("selfcheck"):
            return httpx.Response(status, content=b"bad" if page == "broken" else json.dumps(page).encode())
        assert dict(request.url.params) == {"limit": "1", "probe": "1"}
        return httpx.Response(200)
    requests = install(handle)
    assert c._measure_infra_health() == expected
    assert len(requests) == 1 + fallback


@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("auth", [{"X-Api-Key": "test-key"}, {"X-Feedling-Runtime-Token": "test-token"}])
def test_real_run_whoami_then_backend_probe_then_first_poll_without_enclave(wire, monkeypatch, healthy, auth):
    install, _clock = wire
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "")
    monkeypatch.setattr(c, "_whoami_cache", {})
    monkeypatch.setattr(c, "_HEADERS", auth)
    monkeypatch.setattr(c, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(c, "_warn_if_agent_entry_may_drift", lambda: None)
    monkeypatch.setattr(c, "_resident_ipc_listener_enabled", lambda: False)
    monkeypatch.setattr(c, "_load_checkpoint", lambda: 123.0)
    monkeypatch.setattr(c, "_save_checkpoint", lambda ts: None)
    monkeypatch.setattr(c, "_load_proactive_checkpoint", lambda: 123.0)
    monkeypatch.setattr(c, "PROACTIVE_POLL_ENABLED", False)
    monkeypatch.setattr(c, "CAPTURE_TICK_ENABLED", False)
    monkeypatch.setattr(c, "_clear_startup_exit", lambda: None)
    monkeypatch.setattr(c, "_refresh_auth_header", lambda: None)
    monkeypatch.setattr(c, "_running", True)
    class FirstPoll(BaseException):
        pass
    def handle(request):
        assert request.url.host == "api.test"
        assert all(request.headers[k] == v for k, v in auth.items())
        if request.url.path.endswith("whoami"):
            return httpx.Response(200, json={"user_id": "usr_test", "public_key": base64.b64encode(b"x" * 32).decode(), "content_encryption_effective": "off"})
        if request.url.path.endswith("history"):
            return httpx.Response(200 if healthy else 503, json={"messages": []})
        if request.url.path == "/v1/genesis/resident/pending":
            return httpx.Response(404)
        if request.url.path.endswith("poll"):
            raise FirstPoll
        raise AssertionError(request.url)
    requests = install(handle)
    with pytest.raises(FirstPoll):
        c.run()
    assert [r.url.path for r in requests] == ["/v1/users/whoami", "/v1/chat/history", "/v1/genesis/resident/pending", "/v1/chat/poll"]
    assert requests[-1].headers["X-Feedling-Decrypt-Status"] == ("backend_ready" if healthy else "backend_unreachable")
    assert float(requests[-1].headers["X-Feedling-Decrypt-Checked-At"]) == 1000


@pytest.mark.parametrize("mode", ["off", "on", "unknown"])
@pytest.mark.parametrize("status", ["backend_ready", "backend_unreachable", "backend_degraded"])
def test_backend_uses_authoritative_account_mode_for_readiness(wire, monkeypatch, mode, status):
    monkeypatch.setattr(backend.registry, "effective_content_encryption", lambda uid: mode)
    store = SimpleNamespace(user_id="usr_test", consumer_state_lock=threading.Lock(), first_chat_ok_at=lambda: "")
    state = {"official": True, "last_poll_epoch": 1000, "decrypt_status": status, "decrypt_checked_at_epoch": 1000}
    monkeypatch.setattr(backend, "_load_consumer_state", lambda _: state)
    health = backend._consumer_validation_state(store, now_epoch=1000)["decrypt_health"]
    assert health["passing"] is (mode == "off" and status == "backend_ready")
    assert health["status"] == (status if mode == "off" else "unknown")
    assert backend._decrypt_health_from_state(state, now_epoch=1000)["passing"] is False
    policy = backend._decrypt_health_enforcement_state(store, {"decrypt_health": health}, now_epoch=1000)
    assert policy["blocks_verify"] is (not health["passing"])


@pytest.mark.parametrize("timestamp", [0, "bad", float("nan"), 699, 1061])
def test_backend_ready_needs_valid_fresh_timestamp(wire, monkeypatch, timestamp):
    monkeypatch.setattr(backend.registry, "effective_content_encryption", lambda uid: "off")
    health = backend._decrypt_health_from_state({"decrypt_status": "backend_ready", "decrypt_checked_at_epoch": timestamp}, store=SimpleNamespace(user_id="usr_test"), now_epoch=1000)
    assert not health["passing"]
    assert health["status"] == "unknown"


@pytest.mark.parametrize("status", ["backend_unreachable", "backend_degraded"])
def test_backend_failure_guidance_does_not_send_operator_to_enclave(wire, status):
    reason = {"reason": "decrypt_source_unavailable", "decrypt_status": status}
    for text in (maintenance._prompt_for(reason, {}), maintenance._notice_text(reason)):
        assert "backend history" in text
        assert "FEEDLING_ENCLAVE_URL" not in text
        assert "enclave 密钥" not in text


@pytest.mark.parametrize("runtime", [True, False])
def test_actual_auth_selection_is_used_for_backend_startup_probe(wire, monkeypatch, tmp_path, runtime):
    install, _clock = wire
    token_path = tmp_path / "runtime-token"
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 2000}).encode()).decode().rstrip("=")
    token = payload + ".test-signature"
    if runtime:
        token_path.write_text(token)
    monkeypatch.setattr(c, "FEEDLING_RUNTIME_TOKEN_FILE", str(token_path))
    monkeypatch.setattr(c, "FEEDLING_API_KEY", "test-key")
    monkeypatch.setattr(c, "_HEADERS", {})
    c._refresh_auth_header()
    requests = install(lambda request: httpx.Response(200, json={"messages": []}))
    assert c._verify_decrypt_sources()
    header = "X-Feedling-Runtime-Token" if runtime else "X-API-Key"
    other = "X-API-Key" if runtime else "X-Feedling-Runtime-Token"
    assert requests[0].headers[header] == (token if runtime else "test-key")
    assert other not in requests[0].headers


def test_whoami_mode_round_trip_invalidates_old_proof_even_before_next_poll(wire):
    install, _clock = wire
    mode = ["off"]
    install(lambda request: httpx.Response(200, json={"user_id": "usr_test", "public_key": base64.b64encode(b"x" * 32).decode(), "content_encryption_effective": mode[0]}))
    assert c._load_whoami()
    c._note_decrypt_read_success()
    mode[0] = "on"
    assert c._load_whoami()
    mode[0] = "off"
    assert c._load_whoami()
    assert c._decrypt_health_headers() == {}
    assert c._decrypt_health_last_refresh["at"] == 0


def test_backend_poll_record_revalidates_account_mode_and_clears_omitted_health(wire, monkeypatch):
    mode = ["off"]
    monkeypatch.setattr(backend.registry, "effective_content_encryption", lambda uid: mode[0])
    state = {}
    store = SimpleNamespace(user_id="usr_test", consumer_state_lock=threading.Lock())
    monkeypatch.setattr(backend, "_mutate_consumer_state", lambda _store, mutate: mutate(state))
    monkeypatch.setattr(backend, "_load_consumer_state", lambda _store: dict(state))
    headers = {"X-Feedling-Consumer": "feedling-chat-resident", "X-Feedling-Decrypt-Status": "backend_ready", "X-Feedling-Decrypt-Checked-At": "1000"}
    info = backend._consumer_headers_from_map(headers)
    backend._record_consumer_event(store, "poll", info=info)
    assert backend._consumer_validation_state(store, now_epoch=1000)["decrypt_health"]["passing"]
    mode[0] = "on"
    assert not backend._consumer_validation_state(store, now_epoch=1000)["decrypt_health"]["passing"]
    backend._record_consumer_event(store, "poll", info=info)
    assert state["decrypt_health_unknown_since_epoch"] == 1000
    mode[0] = "off"
    backend._record_consumer_event(store, "poll", info=backend._consumer_headers_from_map({"X-Feedling-Consumer": "feedling-chat-resident"}))
    assert state["decrypt_status"] == ""
    assert not backend._consumer_validation_state(store, now_epoch=1000)["decrypt_health"]["passing"]


@pytest.mark.parametrize("mode", ["off", "on", "unknown"])
@pytest.mark.parametrize("url", ["", "https://enclave.test"])
@pytest.mark.parametrize("threshold", [1, 2, 3])
def test_empty_backend_history_claims_use_account_mode_through_processing(
    wire, monkeypatch, caplog, mode, url, threshold,
):
    install, clock = wire
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", url)
    monkeypatch.setattr(c, "DECRYPT_DEGRADE_AFTER", threshold)
    monkeypatch.setitem(c._whoami_cache, "content_encryption_effective", mode)
    monkeypatch.setattr(c, "_seen_ids", set())
    monkeypatch.setattr(c, "_seen_ids_order", [])
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    calls = []
    monkeypatch.setattr(c, "call_agent", lambda *a, **kw: calls.append("agent"))
    monkeypatch.setattr(c, "post_reply", lambda *a, **kw: calls.append("reply"))
    rows = [{"id": f"empty-{i}", "ts": 1001 + i, "role": "user",
             "source": "chat", "content_type": "text", "body": ""}
            for i in range(threshold)]
    requests = install(lambda request: httpx.Response(200, json={"messages": rows}))
    c._set_decrypt_health("ok")
    # Exercise the real history reader and message processing selection point,
    # rather than calling the read-failure helper directly.
    messages = c.get_decrypted_history(1000, include_image_body=False)
    assert len(messages) == threshold
    assert len(requests) == 1 and requests[0].url.host == "api.test"
    configured = mode == "off" or bool(url)
    for index, message in enumerate(messages, 1):
        assert c._process_messages([message]) == message["ts"]
        assert c._decrypt_read_failures["count"] == (index if configured else 0)
        expected = (
            ("backend_degraded" if mode == "off" else "degraded")
            if index >= threshold else ("backend_ready" if mode == "off" else "ok")
        ) if configured else "unconfigured"
        assert c._decrypt_health["status"] == expected
    assert calls == []  # Empty text still skips replies and advances the cursor.
    if mode == "off":
        assert "backend history" in caplog.text
        assert "set FEEDLING_ENCLAVE_URL" not in caplog.text
        clock[0] += 1
        c._probe_decrypt_reachability()  # Actual successful backend history probe.
        assert c._decrypt_health == {"status": "backend_degraded", "checked_at": clock[0]}
        for status in ("backend_unreachable", "unconfigured", "backend_ready"):
            clock[0] += 1
            c._apply_infra_health(status)
            assert c._decrypt_health["status"] == "backend_degraded"
    elif not url:
        assert "set FEEDLING_ENCLAVE_URL" in caplog.text
