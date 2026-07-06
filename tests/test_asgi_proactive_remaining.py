"""Native ASGI parity for the remaining proactive routes (plan §7.4 / §9).

Drives the FastAPI ``proactive.routes_asgi`` handlers over ``httpx.ASGITransport``
and asserts they return the same status/body as the Flask oracle
(``proactive.routes``) for every migrated route except the already-covered
long-poll (``test_asgi_poll_native``). Both frameworks call the same
framework-neutral ``proactive.proactive_core``, so parity is the proof the split
is behavior-preserving.

Read routes compare on ONE shared user (both backends resolve the same store).
Mutating routes apply Flask to user A and ASGI to user B and compare bodies with
volatile ids/timestamps blanked. The two ``/debug`` dashboards assert status +
Content-Type + a stable substring. Auth-failure (401) and validation (400/404/409)
paths are checked directly.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402  (Flask oracle; import triggers db.init_schema)
from asgi import middleware  # noqa: E402
from core import config as core_config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from proactive import routes_asgi as proactive_asgi  # noqa: E402


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    proactive_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()
_pk_counter = itertools.count(1)

# Named volatile keys; any key ending in ``_at`` (created_at / expires_at /
# updated_at / claimed_at / …) is also blanked by ``_norm``.
_VOLATILE_KEYS = frozenset({
    "decision_id", "job_id", "gate_decision_id", "review_id", "event_id",
    "ts", "generated_at", "current_time", "now", "wake_id", "user_id",
})


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    yield


def _register() -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = appmod.app.test_client().post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _key(api_key: str) -> dict:
    return {"X-API-Key": api_key}


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _flask(method, path, *, headers=None, json_body=None, data=None):
    c = appmod.app.test_client()
    res = c.open(path, method=method, headers=headers or {}, json=json_body, data=data)
    return res.status_code, res.get_json(silent=True)


def _asgi(method, path, *, headers=None, json_body=None, data=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if json_body is not None:
                kw["json"] = json_body
            if data is not None:
                kw["data"] = data
            resp = await client.request(method, path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body
    return asyncio.run(go())


def _asgi_raw(method, path, *, headers=None, data=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if data is not None:
                kw["data"] = data
            resp = await client.request(method, path, headers=headers or {}, **kw)
            return resp.status_code, resp.text, resp.headers.get("content-type")
    return asyncio.run(go())


def _flask_raw(method, path, *, headers=None, data=None):
    res = appmod.app.test_client().open(path, method=method, headers=headers or {}, data=data)
    return res.status_code, res.get_data(as_text=True), res.headers.get("Content-Type")


def _norm(obj):
    """Recursively blank volatile ids/timestamps + user_id so two throwaway users
    (or two sequential calls) compare structurally."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _VOLATILE_KEYS or (isinstance(k, str) and k.endswith("_at")):
                out[k] = "<v>"
            else:
                out[k] = _norm(v)
        return out
    if isinstance(obj, list):
        return [_norm(v) for v in obj]
    return obj


# =========================================================================== #
# auth (401) parity — representative sample across GET/POST + path-param routes
# =========================================================================== #

@pytest.mark.parametrize("method,path,body", [
    ("GET", "/v1/proactive/settings", None),
    ("POST", "/v1/proactive/settings", {}),
    ("GET", "/v1/proactive/state", None),
    ("GET", "/v1/device/events", None),
    ("POST", "/v1/capture/tick", {}),
    ("POST", "/v1/proactive/tick", {}),
    ("POST", "/v1/proactive/jobs/pj_x/claim", {}),
    ("POST", "/v1/proactive/jobs/pj_x/status", {}),
    ("GET", "/v1/proactive/decisions", None),
    ("GET", "/v1/proactive/reviews", None),
    ("POST", "/v1/proactive/decisions/gd_x/review", {}),
    ("GET", "/v1/proactive/debug", None),
    ("GET", "/debug/proactive", None),
])
def test_no_auth_is_401_parity(env, method, path, body):
    f = _flask(method, path, json_body=body)
    a = _asgi(method, path, json_body=body)
    assert f == a == (401, {"error": "unauthorized"})


# =========================================================================== #
# settings + state
# =========================================================================== #

def test_settings_get_parity(env):
    uid, key = _register()
    appmod.get_store(uid).save_proactive_settings({"timezone": "Asia/Tokyo"})
    f = _flask("GET", "/v1/proactive/settings", headers=_key(key))
    a = _asgi("GET", "/v1/proactive/settings", headers=_key(key))
    assert f[0] == a[0] == 200
    assert f[1]["timezone"] == a[1]["timezone"] == "Asia/Tokyo"
    assert _norm(f[1]) == _norm(a[1])


def test_settings_post_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    body = {"timezone": "Europe/Paris"}
    f = _flask("POST", "/v1/proactive/settings", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/settings", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["timezone"] == a[1]["timezone"] == "Europe/Paris"
    assert _norm(f[1]) == _norm(a[1])


def test_state_get_parity(env):
    uid, key = _register()
    f = _flask("GET", "/v1/proactive/state", headers=_key(key))
    a = _asgi("GET", "/v1/proactive/state", headers=_key(key))
    assert f[0] == a[0] == 200
    assert _norm(f[1]) == _norm(a[1])
    assert set(f[1]) == {"version", "enabled", "dnd", "ambient", "scheduled",
                         "reminders_delivery", "user_state", "manual_user_state",
                         "ai_state", "broadcast_state", "wake_interval_sec",
                         "dream_enabled", "capture_enabled", "screen_watch_enabled",
                         "photo_wake_enabled", "arrival_wake_enabled",
                         "unlock_wake_enabled", "updated_at"}


def test_state_post_three_switch_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    body = {"ambient": False, "scheduled": False, "reminders_delivery": False}
    f = _flask("POST", "/v1/proactive/state", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/state", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["enabled"] is a[1]["enabled"] is False
    assert f[1]["dnd"] is a[1]["dnd"] is True
    assert _norm(f[1]) == _norm(a[1])


# =========================================================================== #
# device events
# =========================================================================== #

def test_device_events_get_parity_and_invalid(env):
    uid, key = _register()
    # Seed one event through the write endpoint so it persists exactly as a real
    # device event would (the GET below is served identically by both backends).
    _flask("POST", "/v1/device/events", headers=_key(key),
           json_body={"type": "permission_changed", "payload": {"permission": "authorized"}})
    f = _flask("GET", "/v1/device/events?since=0&limit=50", headers=_key(key))
    a = _asgi("GET", "/v1/device/events?since=0&limit=50", headers=_key(key))
    assert f[0] == a[0] == 200
    assert len(f[1]["events"]) == len(a[1]["events"]) == 1
    # invalid since -> 400 on both
    fb = _flask("GET", "/v1/device/events?since=abc", headers=_key(key))
    ab = _asgi("GET", "/v1/device/events?since=abc", headers=_key(key))
    assert fb == ab == (400, {"error": "invalid since"})


def test_device_events_post_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    body = {"type": "permission_changed", "payload": {"permission": "authorized"}}
    f = _flask("POST", "/v1/device/events", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/device/events", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["type"] == a[1]["type"] == "permission_changed"
    assert _norm(f[1]) == _norm(a[1])


# =========================================================================== #
# capture / dream ticks + force
# =========================================================================== #

def test_capture_tick_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    f = _flask("POST", "/v1/capture/tick", headers=_key(fk), json_body={"now": 1000.0})
    a = _asgi("POST", "/v1/capture/tick", headers=_key(ak), json_body={"now": 1000.0})
    assert f[0] == a[0] == 200
    assert _norm(f[1]) == _norm(a[1])
    assert "dream" in f[1] and "migrate" in f[1]


def test_capture_tick_invalid_now_parity(env):
    _uid, key = _register()
    f = _flask("POST", "/v1/capture/tick", headers=_key(key), json_body={"now": "x"})
    a = _asgi("POST", "/v1/capture/tick", headers=_key(key), json_body={"now": "x"})
    assert f == a == (400, {"error": "invalid now"})


def test_capture_force_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    f = _flask("POST", "/v1/capture/force", headers=_key(fk))
    a = _asgi("POST", "/v1/capture/force", headers=_key(ak))
    assert f[0] == a[0] == 200
    assert _norm(f[1]) == _norm(a[1])


def test_dream_tick_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    f = _flask("POST", "/v1/dream/tick", headers=_key(fk), json_body={"now": 1000.0})
    a = _asgi("POST", "/v1/dream/tick", headers=_key(ak), json_body={"now": 1000.0})
    assert f[0] == a[0] == 200
    assert _norm(f[1]) == _norm(a[1])


def test_dream_tick_invalid_now_parity(env):
    _uid, key = _register()
    f = _flask("POST", "/v1/dream/tick", headers=_key(key), json_body={"now": "x"})
    a = _asgi("POST", "/v1/dream/tick", headers=_key(key), json_body={"now": "x"})
    assert f == a == (400, {"error": "invalid now"})


# =========================================================================== #
# proactive tick
# =========================================================================== #

def test_proactive_tick_parity_forced_wake(env):
    _fu, fk = _register()
    _au, ak = _register()
    body = {"force": True, "context_hint": "a research screen", "intent_label": "research_pause"}
    f = _flask("POST", "/v1/proactive/tick", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/tick", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["enqueued"] is a[1]["enqueued"] is True
    assert f[1]["job"]["source"] == a[1]["job"]["source"] == appmod.PROACTIVE_JOB_SOURCE
    assert _norm(f[1]) == _norm(a[1])


# =========================================================================== #
# job claim / status
# =========================================================================== #

def _seed_pending_job(uid: str, job_id: str) -> dict:
    return appmod.get_store(uid).append_proactive_job({
        "job_id": job_id,
        "source": appmod.PROACTIVE_JOB_SOURCE,
        "ts": 1000.0,
        "status": "pending",
        "trigger": "heartbeat_broadcast_on",
    })


def test_job_claim_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_pending_job(fu, "pj_claim")
    _seed_pending_job(au, "pj_claim")
    body = {"consumer_id": "consumer-a"}
    f = _flask("POST", "/v1/proactive/jobs/pj_claim/claim", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/jobs/pj_claim/claim", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["claimed"] is a[1]["claimed"] is True
    assert _norm(f[1]) == _norm(a[1])


def test_job_claim_missing_job_parity(env):
    fu, fk = _register()
    au, ak = _register()
    f = _flask("POST", "/v1/proactive/jobs/nope/claim", headers=_key(fk), json_body={})
    a = _asgi("POST", "/v1/proactive/jobs/nope/claim", headers=_key(ak), json_body={})
    assert f[0] == a[0] == 200
    assert f[1] == a[1] == {"claimed": False, "job": None, "reason": "not_pending_or_missing"}


def test_job_status_empty_patch_400_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_pending_job(fu, "pj_s")
    _seed_pending_job(au, "pj_s")
    f = _flask("POST", "/v1/proactive/jobs/pj_s/status", headers=_key(fk), json_body={})
    a = _asgi("POST", "/v1/proactive/jobs/pj_s/status", headers=_key(ak), json_body={})
    assert f == a == (400, {"error": "empty_status_patch"})


def test_job_status_not_found_404_parity(env):
    fu, fk = _register()
    au, ak = _register()
    body = {"status": "completed"}
    f = _flask("POST", "/v1/proactive/jobs/ghost/status", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/jobs/ghost/status", headers=_key(ak), json_body=body)
    assert f == a == (404, {"error": "job_not_found"})


def test_job_status_consumer_mismatch_409_parity(env):
    fu, fk = _register()
    au, ak = _register()
    for uid in (fu, au):
        job = _seed_pending_job(uid, "pj_m")
        appmod.get_store(uid).update_proactive_job(job["job_id"], {"consumer_id": "owner-x"})
    body = {"status": "posted", "consumer_id": "intruder"}
    f = _flask("POST", "/v1/proactive/jobs/pj_m/status", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/jobs/pj_m/status", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 409
    assert f[1]["error"] == a[1]["error"] == "consumer_mismatch"
    assert f[1]["expected_consumer_id"] == a[1]["expected_consumer_id"] == "owner-x"


def test_job_status_success_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_pending_job(fu, "pj_ok")
    _seed_pending_job(au, "pj_ok")
    body = {"status": "failed", "reason": "agent_call_failed", "consumer_id": "c1"}
    f = _flask("POST", "/v1/proactive/jobs/pj_ok/status", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/jobs/pj_ok/status", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["job"]["status"] == a[1]["job"]["status"] == "failed"
    assert _norm(f[1]) == _norm(a[1])


# =========================================================================== #
# scheduled actions / fire
# =========================================================================== #

def _patch_scheduled_deps(monkeypatch):
    from proactive import scheduled_wake_v2, store_v2
    from proactive.controls_v2 import resolve_settings_v2

    scheduled_store = scheduled_wake_v2.InMemoryScheduledWakeStoreV2()
    settings_by_user: dict[str, dict] = {}

    class _SettingsStore:
        def load(self, user_id: str):
            return resolve_settings_v2(settings_by_user.get(user_id))

    monkeypatch.setattr(scheduled_wake_v2, "DBScheduledWakeStoreV2", lambda: scheduled_store)
    monkeypatch.setattr(store_v2, "DBProactiveSettingsStoreV2", _SettingsStore)


def test_scheduled_actions_not_list_400_parity(env):
    _uid, key = _register()
    body = {"actions": "nope"}
    f = _flask("POST", "/v1/proactive/scheduled/actions", headers=_key(key), json_body=body)
    a = _asgi("POST", "/v1/proactive/scheduled/actions", headers=_key(key), json_body=body)
    assert f == a == (400, {"error": "actions_required"})


def test_scheduled_actions_empty_parity(env, monkeypatch):
    _patch_scheduled_deps(monkeypatch)
    _fu, fk = _register()
    _au, ak = _register()
    body = {"actions": []}
    f = _flask("POST", "/v1/proactive/scheduled/actions", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/scheduled/actions", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1] == a[1] == {"results": []}


def test_scheduled_fire_no_timers_parity(env, monkeypatch):
    _patch_scheduled_deps(monkeypatch)
    _fu, fk = _register()
    _au, ak = _register()
    f = _flask("POST", "/v1/proactive/scheduled/fire", headers=_key(fk))
    a = _asgi("POST", "/v1/proactive/scheduled/fire", headers=_key(ak))
    assert f[0] == a[0] == 200
    assert f[1] == a[1] == {"results": [], "jobs": [], "queued": 0}


# =========================================================================== #
# decisions / reviews (read)
# =========================================================================== #

def _seed_decision(uid: str, decision_id: str) -> None:
    appmod.get_store(uid).append_gate_decision({
        "decision_id": decision_id,
        "ts": 1000.0,
        "should_reach_out": True,
        "reason": "wake_created",
        "intent_label": "manual",
        "connection": {},
        "frame_ids": [],
    })


def test_decisions_get_parity_and_invalid(env):
    uid, key = _register()
    _seed_decision(uid, "gd_1")
    f = _flask("GET", "/v1/proactive/decisions?since=0", headers=_key(key))
    a = _asgi("GET", "/v1/proactive/decisions?since=0", headers=_key(key))
    assert f[0] == a[0] == 200
    assert len(f[1]["decisions"]) == len(a[1]["decisions"]) == 1
    assert _norm(f[1]) == _norm(a[1])
    fb = _flask("GET", "/v1/proactive/decisions?limit=oops", headers=_key(key))
    ab = _asgi("GET", "/v1/proactive/decisions?limit=oops", headers=_key(key))
    assert fb == ab == (400, {"error": "invalid limit"})


def test_reviews_get_parity(env):
    uid, key = _register()
    appmod.get_store(uid).append_gate_review({
        "review_id": "gr_1", "decision_id": "gd_1", "ts": 1000.0, "label": "good_presence"})
    f = _flask("GET", "/v1/proactive/reviews?since=0", headers=_key(key))
    a = _asgi("GET", "/v1/proactive/reviews?since=0", headers=_key(key))
    assert f[0] == a[0] == 200
    assert len(f[1]["reviews"]) == len(a[1]["reviews"]) == 1
    assert _norm(f[1]) == _norm(a[1])


# =========================================================================== #
# decision review (json + form/html)
# =========================================================================== #

def test_decision_review_json_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_decision(fu, "gd_r")
    _seed_decision(au, "gd_r")
    body = {"label": "good_presence", "notes": "felt natural"}
    f = _flask("POST", "/v1/proactive/decisions/gd_r/review", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/decisions/gd_r/review", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["review"]["label"] == a[1]["review"]["label"] == "good_presence"
    assert f[1]["review"]["label_family"] == a[1]["review"]["label_family"] == "round3"
    assert _norm(f[1]) == _norm(a[1])


def test_decision_review_invalid_label_400_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_decision(fu, "gd_bad")
    _seed_decision(au, "gd_bad")
    body = {"label": "not-a-label"}
    f = _flask("POST", "/v1/proactive/decisions/gd_bad/review", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/decisions/gd_bad/review", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 400
    assert f[1]["error"] == a[1]["error"] == "invalid_label"
    assert f[1]["allowed"] == a[1]["allowed"]


def test_decision_review_not_found_404_parity(env):
    fu, fk = _register()
    au, ak = _register()
    body = {"label": "good_presence"}
    f = _flask("POST", "/v1/proactive/decisions/ghost/review", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/proactive/decisions/ghost/review", headers=_key(ak), json_body=body)
    assert f == a == (404, {"error": "decision_not_found"})


def test_decision_review_form_html_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_decision(fu, "gd_html")
    _seed_decision(au, "gd_html")
    headers_f = {**_key(fk), "Accept": "text/html"}
    headers_a = {**_key(ak), "Accept": "text/html"}
    data = {"label": "good_presence"}
    f = _flask_raw("POST", "/v1/proactive/decisions/gd_html/review", headers=headers_f, data=data)
    a = _asgi_raw("POST", "/v1/proactive/decisions/gd_html/review", headers=headers_a, data=data)
    assert f[0] == a[0] == 200
    assert "text/html" in (f[2] or "") and "text/html" in (a[2] or "")
    assert "Review saved." in f[1] and "Review saved." in a[1]


# =========================================================================== #
# debug (json + html page)
# =========================================================================== #

def test_debug_json_parity(env):
    uid, key = _register()
    _seed_decision(uid, "gd_dbg")
    _seed_pending_job(uid, "pj_dbg")
    f = _flask("GET", "/v1/proactive/debug", headers=_key(key))
    a = _asgi("GET", "/v1/proactive/debug", headers=_key(key))
    assert f[0] == a[0] == 200
    assert f[1]["counts"] == a[1]["counts"]
    assert _norm(f[1]) == _norm(a[1])


def test_debug_page_html_parity(env):
    uid, key = _register()
    _seed_pending_job(uid, "pj_page")
    f = _flask_raw("GET", "/debug/proactive?lang=zh", headers=_key(key))
    a = _asgi_raw("GET", "/debug/proactive?lang=zh", headers=_key(key))
    assert f[0] == a[0] == 200
    assert "text/html" in (f[2] or "") and "text/html" in (a[2] or "")
    assert "IO Proactive Harness" in f[1] and "IO Proactive Harness" in a[1]
    assert "pj_page" in f[1] and "pj_page" in a[1]


def test_debug_page_lang_en_parity(env):
    uid, key = _register()
    f = _flask_raw("GET", "/debug/proactive?lang=en", headers=_key(key))
    a = _asgi_raw("GET", "/debug/proactive?lang=en", headers=_key(key))
    assert f[0] == a[0] == 200
    assert "Hidden Jobs" in f[1] and "Hidden Jobs" in a[1]
