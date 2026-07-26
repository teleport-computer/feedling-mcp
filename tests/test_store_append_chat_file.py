"""append_chat must persist content_type='file' + file_name/file_mime extras.

Not pure-unit: needs the `backend_env` DB fixture (registers a real user via
the HTTP client, then fetches the store through core_store.get_store). Mirrors
the fixture/helper pattern in tests/test_chat_system_notice_role.py.

Run: python -m pytest tests/test_store_append_chat_file.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402


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


def test_append_chat_preserves_file_content_type_and_metadata(store):
    msg = store.append_chat(
        "user", "chat", _env(store.user_id, "envfile1"),
        content_type="file",
        extra={
            "file_name": "离职协议.docx",
            "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    )
    assert msg["content_type"] == "file"
    m = next(x for x in store.chat_messages if x["id"] == "envfile1")
    assert m["content_type"] == "file"
    assert m["file_name"] == "离职协议.docx"
    assert m["file_mime"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_append_chat_text_turn_still_works(store):
    msg = store.append_chat("user", "chat", _env(store.user_id, "envtext1"))
    assert msg["content_type"] == "text"
    m = next(x for x in store.chat_messages if x["id"] == "envtext1")
    assert m["content_type"] == "text"
    assert "file_name" not in m
    assert "file_mime" not in m
