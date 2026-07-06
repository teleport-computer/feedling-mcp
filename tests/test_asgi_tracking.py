"""Native /v1/track/event parity + auth (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI tracking route returns the same status + body shape as the
Flask oracle (both delegate to ``tracking.tracking_core``), that it requires
auth (fixed-body 401, same gate as the Flask ``auth.require_user()``), that a
malformed/empty body is tolerated exactly like Flask's
``get_json(silent=True) or {}``, and that the content-refusing sanitizer keeps
sensitive keys out of the persisted event.

The tracking router is not in ``asgi_app._ASGI_PACKAGES`` (assembly is owned by
that file, which this slice must not edit), so the test registers it onto the
shared app itself via the package's ``register_asgi`` — the same entrypoint the
assembly uses.
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
from tracking import routes_asgi as tracking_routes_asgi  # noqa: E402

# Register the tracking router onto the shared ASGI app once (idempotent guard).
# fastapi 0.139 / starlette 1.3 include_router is lazy: app.routes holds
# ``_IncludedRouter`` proxies with no ``.path``, so the guard must walk through
# ``original_router`` — a flat ``r.path`` scan never sees the already-assembled
# route and silently double-registers.


def _app_has_route(app, path: str) -> bool:
    def walk(routes) -> bool:
        for r in routes:
            original = getattr(r, "original_router", None)
            if original is not None:
                if walk(original.routes):
                    return True
            elif getattr(r, "path", None) == path:
                return True
        return False

    return walk(app.routes)


if not _app_has_route(asgi_app.app, "/v1/track/event"):
    tracking_routes_asgi.register_asgi(asgi_app.app)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
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
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _asgi_post(path: str, json_body=None, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(path, json=json_body, headers=headers or {})
            return resp.status_code, resp.json()

    return asyncio.run(go())


def _flask_post(path: str, json_body=None, headers: dict | None = None):
    res = make_client().post(path, json=json_body, headers=headers or {})
    return res.status_code, res.get_json()


def _normalize(body: dict) -> dict:
    """event_id is a fresh random public id per call, so blank it before compare;
    everything else (status) must match between the two backends."""
    out = dict(body)
    if "event_id" in out:
        out["event_id"] = "<trk>"
    return out


# --------------------------------------------------------------------------- #
# parity
# --------------------------------------------------------------------------- #

def test_track_event_parity_api_key(user):
    _uid, api_key = user
    payload = {"event_type": "App.Opened", "source": "ios", "app_version": "1.2.3"}
    f_status, f_body = _flask_post("/v1/track/event", payload, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/track/event", payload, {"X-API-Key": api_key})
    assert f_status == a_status == 200
    assert _normalize(a_body) == _normalize(f_body) == {"status": "ok", "event_id": "<trk>"}
    assert a_body["event_id"].startswith("trk_")


def test_track_event_empty_body_tolerated(user):
    """Flask uses get_json(silent=True) or {} — a missing/empty body is still a
    200 'unknown' event, never a 400. The ASGI guard must match."""
    _uid, api_key = user
    f_status, f_body = _flask_post("/v1/track/event", None, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/track/event", None, {"X-API-Key": api_key})
    assert f_status == a_status == 200
    assert _normalize(a_body) == _normalize(f_body) == {"status": "ok", "event_id": "<trk>"}


# --------------------------------------------------------------------------- #
# auth (required — same gate as Flask auth.require_user())
# --------------------------------------------------------------------------- #

def test_track_event_requires_auth(user):
    f_status, _f_body = _flask_post("/v1/track/event", {"event_type": "x"})
    a_status, a_body = _asgi_post("/v1/track/event", {"event_type": "x"})
    assert f_status == a_status == 401
    assert a_body == {"error": "unauthorized"}


def test_track_event_bad_api_key_is_fixed_401(user):
    status, body = _asgi_post("/v1/track/event", {"event_type": "x"}, {"X-API-Key": "nope"})
    assert status == 401
    assert body == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# content-refusing sanitizer (the point of this route)
# --------------------------------------------------------------------------- #

def test_track_event_sanitizes_sensitive_keys(user, tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    uid, api_key = user
    payload = {
        "event_type": "note!!",  # non-slug chars -> normalized
        "payload": {"api_key": "SECRET", "count": 3, "clipboard": "leak"},
    }
    status, body = _asgi_post("/v1/track/event", payload, {"X-API-Key": api_key})
    assert status == 200 and body["status"] == "ok"

    store = core_store.get_store(uid)
    events = store.list_tracking_events(limit=10)
    assert events, "event was not persisted"
    ev = events[-1]
    assert ev["type"] == "note"  # sanitized event type
    assert ev["payload"] == {"count": 3}  # sensitive keys dropped, scalar kept
