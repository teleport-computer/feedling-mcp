"""Real hydration/GET plumbing with fake S3, no network or database."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
import debug_trace
import object_storage
import storage_read_trace
from chat import chat_core, service
from core.store import UserStore


_RAW = b"private body must never enter trace"
_KEY = "chatfiles/alice/private-document-path"


def _pointer(*, plaintext=False, uid="alice", key=_KEY):
    doc = {"id": "private-message-id", "owner_user_id": uid, "role": "user",
           "content_type": "file", "body_key": key, "body_ct_len": len(_RAW)}
    if plaintext:
        doc.update(body_object_format="plaintext_v1", body_size_bytes=len(_RAW),
                   body_sha256=hashlib.sha256(_RAW).hexdigest())
    return doc


@pytest.fixture
def rig(monkeypatch):
    events = []
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(storage_read_trace, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(debug_trace, "_enabled_fast", lambda store: True)
    monkeypatch.setattr(debug_trace, "_enqueue", lambda uid, event: events.append((uid, event)))
    monkeypatch.setattr(object_storage, "chat_files_enabled", lambda: True)
    monkeypatch.setenv("R2_CHAT_FILES_BUCKET", "private-bucket")

    def read():
        clock.now = 0.250
        return _RAW

    def get_object(**kwargs):
        assert kwargs["Bucket"] == "private-bucket"
        clock.now = 0.125
        return {"Body": SimpleNamespace(read=read)}

    client = SimpleNamespace(get_object=get_object)
    monkeypatch.setattr(object_storage, "_client", lambda: client)
    return UserStore("alice"), events, client, clock


def _assert_pair(events, expected, *, size=None, uid="alice"):
    assert [event["type"] for _, event in events] == [
        "object_storage.get", "chat.file_body.hydrate",
    ]
    for owner, event in events:
        assert owner == uid
        assert event["status"] == ("ok" if expected == "ok" else "error")
        detail = event["detail"]
        assert set(detail) == {"dur_ms", "bytes", "status", "error_class"}
        assert detail["status"] == expected
        assert detail["bytes"] == size
        assert detail["dur_ms"] >= 0
        assert event["dur_ms"] == detail["dur_ms"]
        assert "content_excerpt" not in event
        serialized = json.dumps(event)
        for secret in (_KEY, "private-document-path", "private-message-id",
                       "private-bucket", _RAW.decode(), "secret-endpoint"):
            assert secret not in serialized


@pytest.mark.parametrize("plaintext", [False, True])
def test_message_body_emits_two_content_free_timings(rig, monkeypatch, plaintext):
    store, events, _, _ = rig
    pointer = _pointer(plaintext=plaintext)
    monkeypatch.setattr(db, "chat_get_strict", lambda uid, mid: pointer)
    result, status = chat_core.message_body(store, pointer["id"])
    assert status == 200
    field = "body_b64" if plaintext else "body_ct"
    assert base64.b64decode(result["message"][field]) == _RAW
    assert "body_key" not in result["message"]
    _assert_pair(events, "ok", size=len(_RAW))
    # GET headers arrive at 125ms, body finishes at 250ms: count the latter.
    assert [e["detail"]["dur_ms"] for _, e in events] == [250.0, 250.0]


@pytest.mark.parametrize(("exc", "expected"), [
    (ClientError({"Error": {"Code": "NoSuchKey", "Message": _KEY}}, "GetObject"), "not_found"),
    (ClientError({"Error": {"Code": "NoSuchBucket", "Message": _KEY}}, "GetObject"), "not_found"),
    (ClientError({"Error": {"Code": "unknown"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject"), "not_found"),
    (ClientError({"Error": {"Code": "InternalError"}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "GetObject"), "http_5xx"),
    (ClientError({"Error": {"Code": "503"}}, "GetObject"), "http_5xx"),
    (ReadTimeoutError(endpoint_url="https://secret-endpoint/private"), "timeout"),
    (ConnectTimeoutError(endpoint_url="https://secret-endpoint/private"), "timeout"),
    (TimeoutError(_KEY), "timeout"),
    (RuntimeError(_KEY), "other"),
])
def test_swallowed_get_failure_is_visible_at_both_layers(rig, exc, expected):
    store, events, client, _ = rig

    def fail(**kwargs):
        raise exc

    client.get_object = fail
    item = service._chat_history_item(_pointer(), store=store)
    assert "body_ct" not in item
    assert "body_key" not in item
    _assert_pair(events, expected)
    assert all(e["detail"]["error_class"] for _, e in events)


def test_stream_read_timeout_still_raises_and_records_both_layers(rig):
    store, events, client, _ = rig
    exc = ReadTimeoutError(endpoint_url="https://secret-endpoint/private")

    def fail_read():
        raise exc

    client.get_object = lambda **kwargs: {"Body": SimpleNamespace(read=fail_read)}
    with pytest.raises(ReadTimeoutError) as caught:
        service._chat_history_item(_pointer(), store=store)
    assert caught.value is exc
    _assert_pair(events, "timeout")


def test_integrity_failure_is_hydrate_error_after_successful_get(rig):
    store, events, _, _ = rig
    pointer = _pointer(plaintext=True)
    pointer["body_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="chat_plaintext_body_integrity_mismatch"):
        service._chat_history_item(pointer, store=store)
    assert [(e["type"], e["detail"]["status"]) for _, e in events] == [
        ("object_storage.get", "ok"), ("chat.file_body.hydrate", "other"),
    ]
    assert events[-1][1]["detail"]["error_class"] == "ValueError"


def test_ownership_rejection_does_not_fetch(rig):
    store, events, client, _ = rig
    client.get_object = lambda **kw: pytest.fail("foreign key must never reach S3")
    service._chat_history_item(_pointer(key="chatfiles/bob/private"), store=store)
    _assert_pair(events, "refused_foreign_key")


def test_no_store_or_inline_body_does_not_emit(rig):
    store, events, _, _ = rig
    service._chat_history_item(_pointer())
    service._chat_history_item({"content_type": "file", "body_ct": "aA=="}, store=store)
    assert events == []


def test_mismatched_store_cannot_record_another_users_read(rig):
    store, events, _, _ = rig
    pointer = _pointer(uid="bob", key="chatfiles/bob/private")
    # The read itself is owned by Bob, but its delivery context belongs to
    # Alice. Neither observation may be attributed to Alice's trace.
    with storage_read_trace.bind_store(store):
        result = db.hydrate_chat_file_body("bob", pointer)
    assert base64.b64decode(result["body_ct"]) == _RAW
    assert events == []


def test_prefetch_workers_bind_store_and_do_not_duplicate_hydrated_events(rig):
    store, events, _, _ = rig
    msgs = [_pointer(key=f"chatfiles/alice/{i}") for i in range(2)]
    out = service.hydrate_history_page(msgs, include_image_body=True, store=store)
    assert len(events) == 4
    assert sorted(e["type"] for _, e in events) == [
        "chat.file_body.hydrate", "chat.file_body.hydrate",
        "object_storage.get", "object_storage.get",
    ]
    for item in out:
        service._chat_history_item(item, store=store)
    assert len(events) == 4


def test_concurrent_users_do_not_share_failure_or_store(rig):
    _, events, client, _ = rig
    barrier = threading.Barrier(2)

    def get_object(**kw):
        barrier.wait(timeout=5)
        if kw["Key"].startswith("chatfiles/alice/"):
            raise TimeoutError(_KEY)
        return {"Body": SimpleNamespace(read=lambda: _RAW)}

    client.get_object = get_object
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service._chat_history_item,
                               _pointer(uid=uid, key=f"chatfiles/{uid}/private"),
                               store=UserStore(uid)) for uid in ("alice", "bob")]
        for future in futures:
            future.result(timeout=5)
    _assert_pair([e for e in events if e[0] == "alice"], "timeout")
    _assert_pair([e for e in events if e[0] == "bob"], "ok", size=len(_RAW), uid="bob")


def test_trace_failure_cannot_break_delivery(rig, monkeypatch):
    store, _, _, _ = rig

    def broken_trace(*args, **kwargs):
        raise RuntimeError("trace backend unavailable")

    monkeypatch.setattr(debug_trace, "trace_event", broken_trace)
    item = service._chat_history_item(_pointer(), store=store)
    assert base64.b64decode(item["body_ct"]) == _RAW
