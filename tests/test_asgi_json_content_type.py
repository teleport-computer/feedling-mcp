"""Content-type gate parity (ASGI-migration plan §19.4, codex-review finding).

Flask ``request.get_json(silent=True)`` returns None when the Content-Type is
not JSON, so a ``text/plain`` body carrying JSON is IGNORED (`… or {}` → {}).
Bare Starlette ``request.json()`` parses regardless of content-type; the migrated
write routes must not act on a body Flask would have dropped. These tests drive a
``text/plain`` JSON body against a migrated write route and assert ASGI matches
the Flask oracle (both treat it as an empty payload).
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
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
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["api_key"]


_JSON_AS_TEXT = b'{"event": "smoke_test_event", "props": {"x": 1}}'


def _flask_post_textplain(path, key):
    res = make_client().post(
        path, data=_JSON_AS_TEXT, content_type="text/plain", headers={"X-API-Key": key}
    )
    return res.status_code, res.get_json()


def _asgi_post_textplain(path, key):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(
                path,
                content=_JSON_AS_TEXT,
                headers={"Content-Type": "text/plain", "X-API-Key": key},
            )
            return r.status_code, (r.json() if r.content else None)

    return asyncio.run(go())


def _norm(body):
    """Blank volatile ids so two calls compare structurally."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    for k in ("event_id", "id", "message_id"):
        if k in out:
            out[k] = "<id>"
    return out


def test_track_event_textplain_json_ignored_like_flask(api_key):
    # text/plain JSON must be dropped to {} on BOTH backends (not parsed+acted on).
    f_status, f_body = _flask_post_textplain("/v1/track/event", api_key)
    a_status, a_body = _asgi_post_textplain("/v1/track/event", api_key)
    assert f_status == a_status
    assert _norm(a_body) == _norm(f_body)


def test_push_register_token_textplain_json_ignored_like_flask(api_key):
    f_status, f_body = _flask_post_textplain("/v1/push/register-token", api_key)
    a_status, a_body = _asgi_post_textplain("/v1/push/register-token", api_key)
    assert f_status == a_status
    assert _norm(a_body) == _norm(f_body)
