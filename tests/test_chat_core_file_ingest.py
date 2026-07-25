"""VPS user ingest (chat_core.write_message) accepts content_type=file.

spec: .superpowers/sdd/task-4-brief.md
Run:  python -m pytest tests/test_chat_core_file_ingest.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from chat import chat_core  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _env(user_id: str, marker: str) -> dict:
    return {
        "v": 1, "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared", "owner_user_id": user_id,
    }


@pytest.fixture()
def store(backend_env):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


def test_write_message_accepts_file(store):
    payload = {
        "envelope": _env(store.user_id, "envf1"),
        "content_type": "file",
        "file_name": "plan.md",
        "file_mime": "text/markdown",
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envf1")
    assert m["content_type"] == "file"
    assert m["file_name"] == "plan.md"
    assert m["file_mime"] == "text/markdown"


def test_write_message_rejects_unknown_content_type(store):
    payload = {"envelope": _env(store.user_id, "envv1"), "content_type": "video"}
    body, status = chat_core.write_message(store, payload)
    assert status == 400
    assert body["error"].startswith("content_type")


def test_write_message_text_still_works(store):
    payload = {"envelope": _env(store.user_id, "envt1"), "content_type": "text"}
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envt1")
    assert m["content_type"] == "text"


def test_write_message_image_persists_caption(store):
    # image + text: the user's caption rides a separate client-built envelope and
    # MUST be persisted (caption_body_ct) so the enclave decrypts it into content
    # and the agent sees the text. Regression for "image+text → text swallowed".
    cap = _env(store.user_id, "capimg1")
    payload = {
        "envelope": _env(store.user_id, "envimg1"),
        "content_type": "image",
        "caption_envelope": cap,
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envimg1")
    assert m["content_type"] == "image"
    assert m.get("caption_body_ct") == cap["body_ct"]   # caption persisted
    assert m.get("caption_K_enclave") == cap["K_enclave"]  # enclave can decrypt it


def test_write_message_image_without_caption_ok(store):
    # image with no accompanying text → no caption fields, no crash.
    payload = {"envelope": _env(store.user_id, "envimg2"), "content_type": "image"}
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envimg2")
    assert m["content_type"] == "image"
    assert not m.get("caption_body_ct")


def test_write_message_file_persists_caption_alongside_name(store):
    cap = _env(store.user_id, "capfile1")
    payload = {
        "envelope": _env(store.user_id, "envfile2"),
        "content_type": "file",
        "file_name": "plan.md",
        "caption_envelope": cap,
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envfile2")
    assert m["file_name"] == "plan.md"
    assert m.get("caption_body_ct") == cap["body_ct"]
