"""The chat-poll response advertises the commit a resident consumer should run,
so a self-hosted consumer can self-update to the backend's deployed commit.

Run:  python -m pytest tests/test_chat_poll_client_release.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x22" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["api_key"]


def test_poll_response_advertises_expected_consumer_commit(api_key, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "")  # use fallback
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "deadbeefcafe")
    res = make_client().get(
        "/v1/chat/poll?since=0&timeout=0",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["client_release"]["expected_consumer_commit"] == "deadbeefcafe"
