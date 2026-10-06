"""Backend unit tests for screen/caption.py.

Pure-unit: no database, no network. All external dependencies are monkeypatched.
Add this file to conftest._PURE_UNIT so it runs on machines without Postgres.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
from screen import caption as cap


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeStore:
    """In-memory stand-in for perception.store keyed by (user_id, kind, item_id).

    Lets the cache read (_cached_caption -> item_get) and write-through
    (item_upsert) exercise their real logic without a Postgres connection, so
    this file is genuinely pure-unit (its docstring's promise) on a machine
    with no test DB.
    """

    def __init__(self):
        self._rows = {}

    def item_get(self, user_id, kind, item_id, now=None):
        return self._rows.get((user_id, kind, item_id))

    def item_upsert(self, user_id, kind, item_id, ts, doc, expires_at=None):
        self._rows[(user_id, kind, item_id)] = dict(doc)


def test_caption_frame_calls_enclave_then_caches(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.local")
    monkeypatch.setattr(cap, "perception_store", _FakeStore())
    monkeypatch.setattr(cap.db, "frame_get", lambda u, f: {"body_ct": "sealed"})
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None):
        calls["n"] += 1
        return _Resp(200, {"frame_id": "f1", "caption": "Mail inbox", "model": "qwen"})

    monkeypatch.setattr(cap, "_enclave_get", fake_get)

    out1 = cap.caption_frame("u1", "key", "f1abf1abf1abf1ab", mode="caption")
    assert out1["caption"] == "Mail inbox"
    assert out1["cached"] is False

    out2 = cap.caption_frame("u1", "key", "f1abf1abf1abf1ab", mode="caption")
    assert out2["caption"] == "Mail inbox"
    assert out2["cached"] is True
    assert calls["n"] == 1  # second read served from cache, no second enclave call


def test_plaintext_caption_never_calls_enclave(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.local")
    monkeypatch.setattr(cap, "perception_store", _FakeStore())
    monkeypatch.setattr(
        cap.db,
        "frame_get",
        lambda user_id, frame_id: {
            "id": frame_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_b64": "/9j/",
            "body_size_bytes": 3,
        },
    )
    monkeypatch.setattr(
        cap,
        "_enclave_get",
        lambda *args, **kwargs: pytest.fail("plaintext frame must not call enclave"),
    )

    out = cap.caption_frame("u1", "key", "f1abf1abf1abf1ab", mode="caption")

    assert out == {
        "frame_id": "f1abf1abf1abf1ab",
        "error": "frame_plaintext_caption_unsupported",
    }


def test_malformed_plaintext_caption_is_invalid_without_enclave(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.local")
    monkeypatch.setattr(cap, "perception_store", _FakeStore())
    monkeypatch.setattr(
        cap.db,
        "frame_get",
        lambda user_id, frame_id: {
            "id": frame_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_b64": "not-valid-base64",
        },
    )
    monkeypatch.setattr(
        cap,
        "_enclave_get",
        lambda *args, **kwargs: pytest.fail("malformed plaintext must not call enclave"),
    )

    out = cap.caption_frame("u1", "key", "f1abf1abf1abf1ab", mode="caption")

    assert out == {
        "frame_id": "f1abf1abf1abf1ab",
        "error": "frame_plaintext_invalid",
    }


def test_caption_frame_no_frame_is_unavailable(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.local")
    monkeypatch.setattr(cap.db, "frame_get", lambda u, f: None)
    out = cap.caption_frame("u1", "key", "deadbeefdeadbeef")
    assert out["error"] == "no_recent_frame"


def test_recent_frames_never_calls_vlm(monkeypatch):
    # Mock uses REAL shape from db.frame_list_meta (filename, not id)
    monkeypatch.setattr(cap.db, "frame_list_meta",
                        lambda u, *, source: [{"filename": "f1abf1abf1abf1ab.env.json", "ts": 2.0, "app": None}])
    monkeypatch.setattr(cap, "_cached_caption", lambda u, f: "Mail inbox")
    # Actively prove that the VLM (enclave) is never contacted by recent_frames
    monkeypatch.setattr(cap, "_enclave_get",
                        lambda *a, **kw: pytest.fail("recent_frames must not call the VLM"))
    out = cap.recent_frames("u1", limit=5)
    assert out["frames"][0]["frame_id"] == "f1abf1abf1abf1ab"
    assert out["frames"][0]["caption"] == "Mail inbox"


def test_caption_frame_json_decode_error_returns_fail_closed(monkeypatch):
    """Fix #1: non-JSON enclave response must return error, not propagate exception."""
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave.local")

    class _BadResp:
        status_code = 200
        text = "<html>error page</html>"

        def json(self):
            raise ValueError("No JSON object could be decoded")

    monkeypatch.setattr(cap, "_enclave_get", lambda *a, **kw: _BadResp())
    monkeypatch.setattr(cap.db, "frame_get", lambda u, f: {"body_ct": "sealed"})
    monkeypatch.setattr(cap, "_cached_caption", lambda u, f: None)

    out = cap.caption_frame("u1", "key", "f1abf1abf1abf1ab")
    assert out["error"] == "caption_error:JSONDecodeError"
    assert out["frame_id"] == "f1abf1abf1abf1ab"
