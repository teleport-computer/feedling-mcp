"""Onboarding route selection (resident / model_api / official_import).

The selected route is the per-user line switch the rest of the backend gates
on (hosted wake consumer, model_api chat, ...).
"""

import db
from accounts import registry
from core import util
from core.store import UserStore

MODEL_API_ROUTES = set(registry.ACCESS_MODES)


def _normalize_onboarding_route(route: str) -> str:
    return registry._normalize_access_mode(route)


def _load_onboarding_route(store: UserStore) -> str:
    data = db.get_blob(store.user_id, "onboarding_route") or {}
    route = _normalize_onboarding_route(str(data.get("route") or "resident"))
    return route if route in MODEL_API_ROUTES else "resident"


def _save_onboarding_route(store: UserStore, route: str) -> dict:
    normalized = _normalize_onboarding_route(route)
    if normalized not in MODEL_API_ROUTES:
        raise ValueError("route must be resident, official_import, or model_api")
    data = {"route": normalized, "selected_at": util._now_iso()}
    db.set_blob(store.user_id, "onboarding_route", data)
    return data
