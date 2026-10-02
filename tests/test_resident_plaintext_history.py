"""Resident history reads: explicit plaintext mode never decrypts old history."""

import base64
import hashlib
import os
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
for key, value in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}.items():
    os.environ.setdefault(key, value)

from tools import chat_resident_consumer as crc  # noqa: E402
from core import chat_images  # noqa: E402
from chat import service as chat_service  # noqa: E402


def row(**fields):
    return {"id": "m1", "role": "user", "ts": 2.0, "seq": 7,
            "content_type": "text", "owner_user_id": "usr_test", **fields}


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(crc, "FEEDLING_API_URL", "https://api.test")
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(crc, "_whoami_cache", {
        "user_id": "usr_test", "content_encryption_effective": "off",
    })
    monkeypatch.setattr(crc, "ENCLAVE_FETCH_MAX_ATTEMPTS", 1)
    requests = []
    clients = []

    def install(page, bodies=None, status=200):
        def handle(request):
            requests.append(request)
            if request.url.host == "enclave.test":
                if request.url.path.endswith("/body"):
                    return httpx.Response(200, json={"message": row(content="legacy decrypted")})
                return httpx.Response(200, json={"messages": [row(content="legacy decrypted")]})
            assert request.url.host == "api.test"
            if request.url.path.endswith("/history"):
                return httpx.Response(status, json=page)
            body = (bodies or {}).get(request.url.path)
            if body is None:
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, json={"message": body})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        monkeypatch.setattr(crc, "_HTTP", client)
        monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", client)
        return requests

    yield install
    for client in clients:
        client.close()


def enclave_requests(requests):
    return [r for r in requests if r.url.host == "enclave.test"]


@pytest.mark.parametrize("mode", ["off", "on", "unknown"])
@pytest.mark.parametrize("include_body", [True, False])
@pytest.mark.parametrize("storage", ["disabled", "missing"])
@pytest.mark.parametrize("kind", ["image", "file"])
def test_real_missing_sealed_pointer_projection_cannot_be_context_or_recall(
    transport, monkeypatch, mode, include_body, storage, kind,
):
    saved = row(content_type=kind, body_key="usr_test/old-object", body_ct_len=100,
                nonce="sealed-nonce", K_user="wrapped", K_enclave="wrapped",
                vision_route_id="must-not-run", caption_body="must-not-enter-context")
    reads = []
    monkeypatch.setattr(chat_service.db.object_storage, "chat_files_enabled", lambda: storage != "disabled")
    monkeypatch.setattr(chat_service.db.object_storage, "get_chat_body",
                        lambda *args, **kwargs: reads.append(args))
    projected = chat_service._chat_history_item(saved, include_image_body=include_body)
    assert not ({"body_key", "body_ct", "body_b64", "body"} & projected.keys())
    omitted = kind == "image" and not include_body
    assert bool(projected.get("body_omitted")) is omitted
    assert len(reads) == int(storage == "missing" and not omitted)
    requests = transport({"messages": [projected]})
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", mode)
    recall_calls = []
    monkeypatch.setattr(crc, "_turn_selection_from_backend", lambda *args: recall_calls.append(args))
    result = crc._hydrate_omitted_bodies(crc.get_decrypted_history(1.0, include_image_body=include_body))
    if mode == "off":
        assert result[0]["body_unavailable"] is True
        assert {k: result[0][k] for k in ("id", "seq", "ts", "role")} == {
            k: saved[k] for k in ("id", "seq", "ts", "role")}
        assert result[0].get("content") == ""
        assert "vision_route_id" not in result[0]
        assert "caption_body" not in result[0]
        assert crc._clean_messages_for_proactive_context(result) == []
        assert crc._capture_live_history(result) == []
        assert crc._auto_memory_fetch_for_turn(result[0]) is None
        assert recall_calls == []
        assert not enclave_requests(requests)
    else:
        assert result[0]["content"] == "legacy decrypted"
        assert len(enclave_requests(requests)) == 1
        assert enclave_requests(requests)[0].url.path == "/v1/chat/history"


@pytest.mark.parametrize("mode", ["off", "on", "unknown"])
@pytest.mark.parametrize("include_body", [True, False])
@pytest.mark.parametrize("storage", ["inline", "pointer"])
def test_real_plaintext_projection_still_delivers_attachment(
    transport, monkeypatch, mode, include_body, storage,
):
    raw = b"pixels"
    encoded = base64.b64encode(raw).decode()
    saved = row(content_type="image", caption_body="caption", caption_owner_user_id="usr_test")
    if storage == "inline":
        # Optional size intentionally absent; the omission uses body_ct_len.
        saved["body_b64"] = encoded
    else:
        saved.update(body_key="usr_test/plain-object", body_object_format="plaintext_v1",
                     body_size_bytes=len(raw), body_sha256=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(chat_service.db.object_storage, "chat_files_enabled", lambda: True)
    monkeypatch.setattr(chat_service.db.object_storage, "get_chat_body_bytes", lambda *args, **kwargs: raw)
    projected = chat_service._chat_history_item(saved, include_image_body=include_body)
    full = chat_service._chat_history_item(saved, include_image_body=True)
    # A readable sibling keeps the legacy on/unknown mixed-page path. Its
    # ambiguous omitted-inline handling remains unchanged (per-row enclave).
    requests = transport({"messages": [row(id="text", body="readable"), projected]},
                         {"/v1/chat/messages/m1/body": full})
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", mode)
    result = crc._hydrate_omitted_bodies(crc.get_decrypted_history(1.0, include_image_body=include_body))
    assert result[0]["content"] == "readable"
    attachment = result[1]
    if storage == "inline" and not include_body and mode != "off":
        assert attachment["content"] == "legacy decrypted"
        assert len(enclave_requests(requests)) == 1
    else:
        assert attachment["image_b64"] == encoded
        assert attachment["content"] == "caption"
        assert not attachment.get("body_unavailable")
        assert not enclave_requests(requests)


def test_backend_omitted_hydration_is_not_fetched_again(transport):
    page = row(content_type="image", body_omitted=True, image_omitted=True,
               body_omitted_reason="include_image_body", body_size_bytes=3)
    body = row(content_type="image", body_b64=base64.b64encode(b"png").decode(), seq=None)
    requests = transport({"messages": [page]}, {"/v1/chat/messages/m1/body": body})
    history = crc.get_decrypted_history(1.0, include_image_body=False)
    result = crc._hydrate_omitted_bodies(history)
    assert not enclave_requests(requests)
    assert len(requests) == 2
    assert result[0]["image_b64"] == body["body_b64"]
    assert result[0]["seq"] == 7
    assert not result[0].get("body_omitted")


@pytest.mark.parametrize("page,status", [
    ({"messages": []}, 503), (None, 200), ({}, 200),
    ({"messages": None}, 200), ({"messages": {}}, 200),
    ({"messages": [None]}, 200), ({"messages": [row(ts="invalid")]}, 200),
    ({"messages": [row(ts="nan")]}, 200),
    ({"messages": [row(ts=None)]}, 200),
    ({"messages": [row(id=None)]}, 200),
])
def test_off_backend_failure_is_not_empty_success_or_enclave_retry(transport, page, status):
    requests = transport(page, status=status)
    assert crc.get_decrypted_history(1.0) is None
    assert not enclave_requests(requests)


def test_empty_plaintext_history_is_a_success(transport):
    requests = transport({"messages": []})
    assert crc.get_decrypted_history(1.0) == []
    assert not enclave_requests(requests)


@pytest.mark.parametrize("sealed", [
    row(body_ct="cipher", content="must not be read"),
    row(body_omitted=True, body_ct_len=100, image_omitted=True, content_type="image"),
])
def test_all_sealed_off_rows_stay_unreadable_with_cursor_metadata(transport, sealed):
    requests = transport({"messages": [sealed]})
    result = crc._hydrate_omitted_bodies(crc.get_decrypted_history(1.0))
    assert not enclave_requests(requests)
    assert len(result) == 1
    assert {key: result[0][key] for key in ("id", "seq", "role", "ts")} == {
        key: sealed[key] for key in ("id", "seq", "role", "ts")}
    assert result[0]["body_unavailable"] is True
    assert result[0].get("content") == ""
    assert crc._capture_live_history(result) == []
    assert crc._clean_messages_for_proactive_context(result) == []
    assert crc._auto_memory_fetch_for_turn(result[0]) is None
    assert not enclave_requests(requests)


@pytest.mark.parametrize("mode", ["on", "unknown", None])
def test_encrypted_or_unknown_backend_failure_keeps_legacy_fallback(transport, monkeypatch, mode):
    requests = transport({}, status=503)
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", mode)
    assert crc.get_decrypted_history(1.0)[0]["content"] == "legacy decrypted"
    assert len(enclave_requests(requests)) == 1


@pytest.mark.parametrize("mode", ["off", "on", "unknown"])
@pytest.mark.parametrize("mixed", [False, True])
def test_sealed_and_mixed_pages_follow_account_mode(transport, monkeypatch, mode, mixed):
    sealed = row(body_ct="cipher")
    plain = row(id="plain", seq=8, ts=3.0, body="still readable")
    requests = transport({"messages": [sealed, plain] if mixed else [sealed]})
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", mode)
    result = crc.get_decrypted_history(1.0, limit=9, include_image_body=False, after_seq=6)
    assert dict(requests[0].url.params) == {
        "limit": "9", "since": "1.0", "include_image_body": "false", "after_seq": "6"}
    assert requests[0].headers["x-api-key"] == crc._HEADERS["X-API-Key"]
    if mode == "off":
        assert not enclave_requests(requests)
        assert result[0]["body_unavailable"] is True
    else:
        assert result[0]["content"] == "legacy decrypted"
        assert len(enclave_requests(requests)) == 1
        expected_path = "/v1/chat/messages/m1/body" if mixed else "/v1/chat/history"
        assert enclave_requests(requests)[0].url.path == expected_path
    if mixed:
        assert result[1]["content"] == "still readable"


def test_mode_refresh_applies_to_next_read_but_cannot_revive_unreadable_rows(transport, monkeypatch):
    requests = transport({"messages": [row(body_omitted=True, body_ct_len=100)]})
    sealed = crc.get_decrypted_history(1.0)
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", "on")
    assert crc._hydrate_omitted_bodies(sealed) == sealed
    assert not enclave_requests(requests)
    assert crc.get_decrypted_history(1.0)[0]["content"] == "legacy decrypted"
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", "off")
    assert crc.get_decrypted_history(1.0)[0]["body_unavailable"] is True
    assert len(enclave_requests(requests)) == 1


@pytest.mark.parametrize("kind", ["text", "file", "image"])
def test_omitted_body_round_trip_preserves_bytes_caption_and_page_metadata(transport, kind):
    caption = "  precise caption 中文  "
    fields = dict(content_type=kind, body_omitted=True, body_omitted_reason="large_body",
                  image_omitted=True, file_omitted=True, body_size_bytes=200000)
    page = row(**fields)
    if kind == "text":
        # Some legacy omission metadata does not include a size.
        page.pop("body_size_bytes")
        body_fields = {"body": "oversized text " * 10000}
    elif kind == "file":
        body_fields = {"body_b64": base64.b64encode(b"file bytes\x00\xff").decode()}
    else:
        bundle = chat_images.encode_image_bundle([(b"first", "image/jpeg"), (b"second", "image/png")])
        body_fields = {"body_b64": base64.b64encode(bundle).decode(), "image_bundle_version": 1}
    full = row(content_type=kind, seq=None, role="assistant", ts=99.0,
               caption_body=caption, caption_owner_user_id="usr_test", **body_fields)
    requests = transport({"messages": [page]}, {"/v1/chat/messages/m1/body": full})
    result = crc._hydrate_omitted_bodies(crc.get_decrypted_history(1.0))[0]
    assert not enclave_requests(requests)
    assert len(requests) == 2
    assert {k: result[k] for k in ("id", "role", "seq", "ts")} == {
        k: page[k] for k in ("id", "role", "seq", "ts")}
    assert not any(result.get(k) for k in ("body_omitted", "image_omitted", "file_omitted"))
    if kind == "text":
        assert result["content"] == body_fields["body"]
    elif kind == "file":
        assert result["file_b64"] == body_fields["body_b64"]
        assert result["content"] == caption
    else:
        assert result["images"] == [
            {"image_b64": base64.b64encode(b"first").decode(), "image_mime": "image/jpeg"},
            {"image_b64": base64.b64encode(b"second").decode(), "image_mime": "image/png"}]
        assert result["content"] == caption


@pytest.mark.parametrize("body", [None, {}, {"body_b64": 23}, row(body="wrong", id="other"),
                                  row(body="wrong", owner_user_id="other"), row(body_ct="sealed")])
def test_partial_body_failure_stays_unreadable_without_retry(transport, monkeypatch, body):
    page = row(body_omitted=True, body_size_bytes=10, content_type="file")
    requests = transport({"messages": [page, row(id="ok", seq=8, body="good")]},
                         {"/v1/chat/messages/m1/body": body})
    history = crc.get_decrypted_history(1.0)
    monkeypatch.setitem(crc._whoami_cache, "content_encryption_effective", "on")
    result = crc._hydrate_omitted_bodies(history)
    assert len(requests) == 2
    assert not enclave_requests(requests)
    assert result[0]["body_unavailable"] is True
    assert result[0]["seq"] == 7
    assert result[1]["content"] == "good"


def test_off_hydration_uses_backend_and_preserves_legacy_seq_none(transport):
    requests = transport({}, {"/v1/chat/messages/m1/body": row(body="text", seq=99)})
    result = crc._hydrate_omitted_bodies([row(body_omitted=True, seq=None)])
    assert result[0]["seq"] is None
    assert result[0]["content"] == "text"
    assert len(requests) == 1 and not enclave_requests(requests)


@pytest.mark.parametrize("sealed", [False, True])
def test_omitted_inline_binary_without_optional_size_resolves_on_backend(transport, sealed):
    # _chat_history_item strips inline body_b64 and reports body_ct_len even
    # for plaintext uploads; body_size_bytes is optional at upload validation.
    page = row(content_type="image", body_omitted=True, body_ct_len=8)
    full = row(content_type="image", **(
        {"body_ct": "old sealed"} if sealed else {"body_b64": "cGl4ZWxz"}))
    requests = transport({"messages": [page]}, {"/v1/chat/messages/m1/body": full})
    result = crc._hydrate_omitted_bodies(crc.get_decrypted_history(1.0))
    assert len(requests) == 2 and not enclave_requests(requests)
    if sealed:
        assert result[0]["body_unavailable"] is True
        assert crc._capture_live_history(result) == []
    else:
        assert result[0]["image_b64"] == "cGl4ZWxz"


def test_backend_transport_failure_never_falls_back(transport, monkeypatch):
    requests = transport({"messages": []})
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(args)
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(crc._HTTP, "get", fail)
    assert crc.get_decrypted_history(1.0) is None
    assert requests == []
    assert len(attempts) == 1


@pytest.mark.parametrize("page,expected_checkpoints,expected_content", [
    ({"messages": [row(body="backend text")]}, [1.0, 2.0], "backend text"),
    ({"messages": []}, [1.0, 2.0], None),
    ({}, [1.0], None),
])
def test_run_without_enclave_reads_backend_and_distinguishes_empty_from_failure(
    transport, monkeypatch, page, expected_checkpoints, expected_content,
):
    requests = transport(page)
    saved, processed = [], []
    for key, value in {
        "_running": True, "_ENCRYPTION_AVAILABLE": True, "FEEDLING_ENCLAVE_URL": "",
        "PROACTIVE_POLL_ENABLED": False, "CAPTURE_TICK_ENABLED": False,
    }.items():
        monkeypatch.setattr(crc, key, value)
    for name in ("_warn_if_agent_entry_may_drift", "_clear_startup_exit", "_refresh_auth_header",
                 "_maybe_apply_user_mcp", "_process_agent_body_job", "_process_vision_probe",
                 "_process_resident_distill_once", "_apply_infra_health"):
        monkeypatch.setattr(crc, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(crc, "_load_whoami_with_retries", lambda: True)
    monkeypatch.setattr(crc, "_resident_ipc_listener_enabled", lambda: False)
    monkeypatch.setattr(crc, "_load_checkpoint", lambda: 1.0)
    monkeypatch.setattr(crc, "_save_checkpoint", saved.append)
    monkeypatch.setattr(crc, "_load_proactive_checkpoint", lambda: 0.0)
    def poll(_since):
        crc._running = False
        return {"timed_out": False, "messages": [row(content="poll is only a trigger")]}
    monkeypatch.setattr(crc, "poll_chat", poll)
    monkeypatch.setattr(crc, "_process_messages", lambda messages: processed.extend(messages) or 2.0)
    crc.run()
    assert saved == expected_checkpoints
    assert [m["content"] for m in processed] == ([] if expected_content is None else [expected_content])
    assert len(requests) == 1 and requests[0].url.path == "/v1/chat/history"
