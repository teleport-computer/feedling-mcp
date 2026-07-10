"""Framework-neutral entry points for the native ASGI admin data-track routes.

The ``admin.data_track`` helpers read their query parameters from a request
proxy (``request.args``) deep inside ``_data_track_payload`` /
``_data_track_request_filters`` / ``_data_track_qs`` (the HTML pages embed
``admin_key``/``since``/``sort``/… in their hrefs). To run that logic without
forking it, each entry point binds a neutral, flask-free request context
(``core.reqctx.bind``) built from the ASGI request's raw query string, so the
identical ``data_track`` code path executes off the event loop.

Every entry point is blocking (sync ``db.py`` under the hood) and must be invoked
via ``asgi.threadpool.run_db`` from the async routes.
"""

from __future__ import annotations

import time
import db
from accounts import registry
from admin import data_track
from core import store as core_store
from core.reqctx import bind, request
from hosted import config_store
from model_api_runtime.v2 import jobs_store


def summary_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_payload(include_users=False)


def users_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_payload(include_users=True)


def dau_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_dau_payload()


def debug_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_debug_payload()


def user_payload(query_string: str, user_id: str) -> tuple[dict, int]:
    # Mirror admin_data_track_user: 404 -> {"error": "user_not_found"}.
    with registry._users_lock:
        entry = next((dict(u) for u in registry._users if u.get("user_id") == user_id), None)
    if not entry:
        return {"error": "user_not_found"}, 404
    with bind(query_string):
        return {"user": data_track._build_data_track_user(entry, include_detail=True)}, 200


def page_html(query_string: str) -> str:
    # Mirror admin_data_track_page's view dispatch.
    with bind(query_string):
        view = (request.args.get("view") or "").strip().lower()
        if view == "dau":
            return data_track._render_data_track_dau_page(data_track._data_track_dau_payload())
        if view == "proactive":
            return data_track._render_proactive_daily_page(data_track._data_track_proactive_daily_payload())
        if view == "debug":
            return data_track._render_data_track_debug_page(data_track._data_track_debug_payload())
        if view == "events":
            event = (request.args.get("event") or "").strip()
            if event == "onboarding":
                return data_track._render_onboarding_funnel_page(data_track._data_track_onboarding_funnel_payload())
            if event:
                return data_track._render_event_users_page(data_track._data_track_event_users_payload(event))
            return data_track._render_events_page(data_track._data_track_events_payload())
        return data_track._render_data_track_page(data_track._data_track_payload(include_users=True))


def user_page(query_string: str, user_id: str) -> tuple[str, str, int]:
    # Mirror admin_data_track_user_page. Returns (kind, body, status):
    # ("text", "user not found", 404) or ("html", <page>, 200).
    with registry._users_lock:
        entry = next((dict(u) for u in registry._users if u.get("user_id") == user_id), None)
    if not entry:
        return "text", "user not found", 404
    with bind(query_string):
        body = data_track._render_user_detail_page(
            data_track._build_data_track_user(entry, include_detail=True)
        )
    return "html", body, 200


def store_evict(user_id: str) -> dict:
    # Mirror admin_store_evict's side effect + payload (validation stays in the route).
    evicted = core_store._evict_store(user_id)
    print(f"[admin:store/evict] user_id={user_id} evicted={evicted}")
    return {"evicted": evicted, "user_id": user_id}


# --------------------------------------------------------------------------- #
# hosted_runtime_mode control plane (Hosted Runtime V2 D0 rollout — gated flip
# between resident_cli and db_action_v2 without a direct DB write).
# --------------------------------------------------------------------------- #

def set_runtime_mode(user_id: str, mode: str) -> tuple[dict, int]:
    store = core_store.get_store(user_id)
    if mode == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2:
        try:
            # Seed first. If profile persistence subsequently fails this row is
            # dormant because every producer is mode-filtered; the reverse order
            # creates a real window where the resident is reaped but no V2 wake
            # schedule exists.
            jobs_store.upsert_wake_schedule(user_id, next_heartbeat_at=time.time())
        except Exception as e:  # noqa: BLE001 — do not report a half-ready flip
            return {"error": "v2_schedule_seed_failed", "detail": str(e)[:160]}, 503
    try:
        persisted_mode = config_store.set_hosted_runtime_mode(store, mode)
    except ValueError as e:
        return {"error": str(e)}, 400
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    return {"user_id": user_id, "hosted_runtime_mode": persisted_mode}, 200


def get_runtime_mode(user_id: str) -> tuple[dict, int]:
    store = core_store.get_store(user_id)
    try:
        mode = config_store.get_hosted_runtime_mode_strict(store)
    except Exception:
        return {"error": "runtime_control_unavailable"}, 503
    return {"user_id": user_id, "hosted_runtime_mode": mode}, 200


def list_runtime_modes() -> dict:
    # Group user_ids by their persisted hosted_runtime_mode; absent/unknown
    # values fall back to resident_cli (mirrors config_store.get_hosted_runtime_mode's
    # default-on-missing-or-invalid behavior).
    result: dict = {
        config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2: [],
        config_store.HOSTED_RUNTIME_MODE_RESIDENT: [],
    }
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id, doc->>'hosted_runtime_mode' AS mode
            FROM user_blobs
            WHERE kind = 'model_api_runtime'
            """
        ).fetchall()
    for row in rows:
        user_id = row[0] if not isinstance(row, dict) else row["user_id"]
        mode = row[1] if not isinstance(row, dict) else row["mode"]
        if mode == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2:
            result[config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2].append(user_id)
        else:
            result[config_store.HOSTED_RUNTIME_MODE_RESIDENT].append(user_id)
    return result


# --------------------------------------------------------------------------- #
# v2 turn metrics (Task 4 — D4 load-testing consumes these via
# GET /v1/admin/v2-metrics; queue depth/worker liveness/service time/token
# throughput, all sourced from jobs_store's existing DB-backed counters).
# --------------------------------------------------------------------------- #

def v2_metrics() -> dict:
    return {
        "inflight": jobs_store.inflight_job_count(),
        "pending": jobs_store.pending_job_count(),
        "live_workers": jobs_store.live_worker_count(),
        "live_worker_capacity": jobs_store.live_worker_capacity(),
        "mean_service_sec": jobs_store.recent_mean_service_sec(lane="chat"),
        "recent_mean_tokens_per_turn": jobs_store.recent_mean_tokens_per_turn(lane="chat"),
        "wake": jobs_store.wake_success_stats(),
        # The genesis import worker rides in the serve_worker process on its own
        # thread, and `run_loop` imports `genesis.worker` lazily — so that thread can
        # die while the turn loops keep beating. Without this field, a dead genesis
        # thread is invisible until a user reports their onboarding distillation
        # stuck. `live_workers` counts kind='turn' only and would not notice.
        "genesis_alive": jobs_store.genesis_worker_alive(),
    }
