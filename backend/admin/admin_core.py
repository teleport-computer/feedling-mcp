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

from accounts import registry
from admin import data_track
from core import store as core_store
from core.reqctx import bind, request


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
