"""Native agent perception routes parity (ASGI-migration plan §5.3 / §8.2).

Asserts the four FastAPI ``/v1/agent/perception*`` routes return the same
status + body as the Flask oracle for a registered user, covering the happy
path, each route's 400 branch, and the fixed-body 401 for missing auth.

The Flask route and the ASGI route both delegate to the shared
``agent.perception_core`` builders, which call the ``perception`` store/service
modules. We monkeypatch those module functions to deterministic returns so the
two sequential calls (Flask oracle, then ASGI) compare byte-for-byte with no
volatile perception timestamps in play.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from agent import routes_asgi as agent_asgi  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from perception import service as perception_service  # noqa: E402
from perception import store as perception_store  # noqa: E402

_FAKE_ENCLAVE = {"content_pk_hex": ("33" * 32), "compose_hash": "test-compose"}

# Deterministic perception fixtures shared by the Flask oracle and the ASGI route
# (both read through the same perception.* modules via agent.perception_core).
_SNAPSHOT = {
    "local_time": "2026-06-23T12:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "locale": "zh-Hans-CN",
    "battery_level": 0.72,
    "charging": False,
    "place_label": "home",
    "motion_state": "still",
    "now_playing": {"title": "Song"},
    "broadcast_state": "off",
    "broadcast_active": False,
    "user_state": "default",
}
_PULL = {
    "place_label": "home",
    "wifi_label": "home_wifi",
    "country": "CN",
    "locality": "深圳市",
    "wifi_anchor_id": "wifi-home",
    "condition": "rain",
    "temperature": 23.4,
    "is_daylight": True,
    "step_count": 6500,
    "resting_heart_rate": 60,
}
_DAILY_ROWS = {
    "health_vitals": [
        {"date": "2026-06-23", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 58, "max": 62}}},
        {"date": "2026-06-24", "doc": {"resting_heart_rate": {"sum": 120, "count": 2, "min": 59, "max": 61}}},
        {"date": "2026-06-25", "doc": {"resting_heart_rate": {"sum": 132, "count": 2, "min": 65, "max": 67}}},
    ],
    "health_activity": [
        {"date": "2026-06-23", "doc": {"active_energy_kcal": {"total": 200}}},
        {"date": "2026-06-24", "doc": {"active_energy_kcal": {"total": 200}}},
        {"date": "2026-06-25", "doc": {"active_energy_kcal": {"total": 500}}},
    ],
}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def asgi_app_obj():
    """A minimal FastAPI app mirroring asgi_app.py's assembly (exception handlers
    + our router). We build it here because the orchestrator registers
    agent.routes_asgi in asgi_app.py separately (out of this task's scope)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    agent_asgi.register_asgi(app)
    return app


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setattr(core_enclave, "_get_enclave_info", lambda: dict(_FAKE_ENCLAVE))
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

    # Deterministic perception layer for both backends (shared perception.* modules).
    monkeypatch.setattr(perception_store, "get_state", lambda uid: {})
    monkeypatch.setattr(perception_service, "snapshot", lambda uid, now=None: dict(_SNAPSHOT))
    monkeypatch.setattr(perception_service, "pull_snapshot", lambda uid, now=None: dict(_PULL))
    monkeypatch.setattr(perception_service, "photos_recent", lambda uid, limit=20: ({"photos": []}, 200))
    monkeypatch.setattr(
        perception_store, "list_perception_daily",
        lambda uid, signal, days: list(_DAILY_ROWS.get(signal, [])),
    )
    return body["user_id"], body["api_key"]


def _asgi_get(app, path: str, headers: dict | None = None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(path, headers=headers or {})
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text

    return asyncio.run(go())


def _flask_get(path: str, headers: dict | None = None):
    res = make_client().get(path, headers=headers or {})
    return res.status_code, res.get_json()


def _both(app, user, path):
    _uid, api_key = user
    f = _flask_get(path, {"X-API-Key": api_key})
    a = _asgi_get(app, path, {"X-API-Key": api_key})
    return f, a


# --------------------------------------------------------------------------- #
# /v1/agent/perception
# --------------------------------------------------------------------------- #

def test_perception_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception?signals=now,weather,location")
    assert fs == as_ == 200
    assert ab == fb
    assert fb["ok"] is True
    assert fb["signals"]["weather"]["condition"] == "rain"


def test_perception_default_signals_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception")
    assert fs == as_ == 200
    assert ab == fb


def test_perception_unknown_signal_400_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception?signals=now,nope")
    assert fs == as_ == 400
    assert ab == fb
    assert fb["error"] == "unknown_signals"
    assert fb["unknown"] == ["nope"]


def test_perception_no_auth_is_401(asgi_app_obj, user):
    status, body = _asgi_get(asgi_app_obj, "/v1/agent/perception")
    assert status == 401
    assert body == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# /v1/agent/perception/trend
# --------------------------------------------------------------------------- #

def test_trend_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/trend?signal=vitals&field=resting_heart_rate&days=7")
    assert fs == as_ == 200
    assert ab == fb
    assert fb["ok"] is True


def test_trend_unknown_signal_400_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/trend?signal=nope")
    assert fs == as_ == 400
    assert ab == fb
    assert fb["error"] == "unknown_or_unhistorized_signal"


def test_trend_invalid_days_400_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/trend?signal=vitals&days=nope")
    assert fs == as_ == 400
    assert ab == fb == {"ok": False, "error": "invalid_days"}


def test_trend_no_auth_is_401(asgi_app_obj, user):
    status, body = _asgi_get(asgi_app_obj, "/v1/agent/perception/trend?signal=vitals")
    assert status == 401
    assert body == {"error": "unauthorized"}


# --------------------------------------------------------------------------- #
# /v1/agent/perception/history
# --------------------------------------------------------------------------- #

def test_history_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/history?signal=vitals&days=14")
    assert fs == as_ == 200
    assert ab == fb
    assert fb["signal"] == "health_vitals"
    assert fb["days"] == 14


def test_history_unknown_signal_400_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/history?signal=nope")
    assert fs == as_ == 400
    assert ab == fb
    assert fb["error"] == "unknown_or_unhistorized_signal"


def test_history_invalid_days_400_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/history?signal=vitals&days=nope")
    assert fs == as_ == 400
    assert ab == fb == {"ok": False, "error": "invalid_days"}


# --------------------------------------------------------------------------- #
# /v1/agent/perception/digest
# --------------------------------------------------------------------------- #

def test_digest_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/digest?days=7")
    assert fs == as_ == 200
    assert ab == fb
    assert fb["ok"] is True
    assert fb["days"] == 7
    assert "domains" in fb and "changes" in fb


def test_digest_invalid_days_400_parity(asgi_app_obj, user):
    (fs, fb), (as_, ab) = _both(asgi_app_obj, user, "/v1/agent/perception/digest?days=nope")
    assert fs == as_ == 400
    assert ab == fb == {"ok": False, "error": "invalid_days"}


def test_digest_no_auth_is_401(asgi_app_obj, user):
    status, body = _asgi_get(asgi_app_obj, "/v1/agent/perception/digest")
    assert status == 401
    assert body == {"error": "unauthorized"}
