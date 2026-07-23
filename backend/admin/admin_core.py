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

import json
import logging
from datetime import datetime, timezone

import db
from accounts import registry
from admin import data_track
from content import content_core
from core import store as core_store
from core import wake_bus
from core.reqctx import bind, request

log = logging.getLogger("feedling.admin")


def summary_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_payload(include_users=False)


def users_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_payload(include_users=True)


def dau_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_dau_payload()


def growth_payload(query_string: str) -> dict:
    with bind(query_string):
        return data_track._data_track_growth_payload()


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
        if view == "growth":
            return data_track._render_data_track_growth_page(data_track._data_track_growth_payload())
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


def login_page(*, error: bool = False, next_url: str = "/admin/data-track") -> str:
    return data_track._render_admin_login_page(error=error, next_url=next_url)


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


def delete_user(user_id: str) -> tuple[dict, int]:
    """Delete one account by authoritative DB id and evict its cached state."""
    if not db.user_exists(user_id):
        return {"error": "user_not_found"}, 404

    archive_err = content_core._purge_onboarding_archives_with_retry(user_id)
    if archive_err is not None:
        log.error(
            "[admin:user/delete] onboarding archive cleanup failed user_id=%r: %s",
            user_id,
            archive_err,
        )
        return {"error": "archive_cleanup_failed"}, 503

    with registry._users_lock:
        if not db.delete_user(user_id):
            return {"error": "user_not_found"}, 404
        registry._users[:] = [
            entry for entry in registry._users if entry.get("user_id") != user_id
        ]
        stale_hashes = [
            key_hash
            for key_hash, cached_user_id in registry._key_to_user.items()
            if cached_user_id == user_id
        ]
        for key_hash in stale_hashes:
            registry._key_to_user.pop(key_hash, None)

    audit = {
        "event": "admin_user_delete",
        "who": "admin",
        "user_id": user_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    log.info("[admin:user/delete] %s", json.dumps(audit, separators=(",", ":")))

    registry.notify_users_changed()
    wake_bus.notify("blob", user_id)

    for label, cleanup in (
        ("frames-r2", lambda: db.delete_user_frames(user_id)),
        ("chat-files-r2", lambda: db.delete_user_chat_files(user_id)),
        ("db-belt", lambda: db.delete_user_data(user_id)),
    ):
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[admin:user/delete] cleanup failed label=%s user_id=%r: %s",
                label,
                user_id,
                exc,
            )

    with core_store._stores_lock:
        cached_store = core_store._stores.pop(user_id, None)
    if cached_store is not None:
        core_store._wake_store_waiters(cached_store)

    return {"deleted": True, "user_id": user_id}, 200
