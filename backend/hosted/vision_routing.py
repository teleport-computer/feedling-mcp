"""Shared visual-route capability and send-time pinning for every runtime."""

from __future__ import annotations

import db
from accounts import onboarding as accounts_onboarding
from chat import consumer as chat_consumer
from hosted import config_store as hosted_config_store


def runtime_capability(store) -> dict:
    onboarding_route = accounts_onboarding._load_onboarding_route(store)
    if onboarding_route == "model_api":
        try:
            if hosted_config_store.hosted_runtime_v2_enabled_strict(store):
                return {
                    "available": True,
                    "runtime": "v2",
                    "onboarding_route": onboarding_route,
                    "unavailable_reason": "",
                }
        except Exception:
            pass

    try:
        resident_ready = chat_consumer.consumer_supports_capability(
            store,
            chat_consumer.VISION_OBSERVER_CAPABILITY,
        )
    except Exception:
        # A missing/corrupt resident heartbeat must never enable dedicated
        # routing. Legacy follow-main image handling remains available.
        resident_ready = False
    return {
        "available": resident_ready,
        "runtime": "hosted_v1" if onboarding_route == "model_api" else "vps",
        "onboarding_route": onboarding_route,
        "unavailable_reason": "" if resident_ready else "resident_update_required",
    }


def dedicated_route_for_send(store) -> tuple[dict | None, tuple[dict, int] | None]:
    """Return the pinned observer route, or a fail-closed pre-append error."""
    route = db.model_api_vision_route(store.user_id)
    if route is None:
        return None, None

    capability = runtime_capability(store)
    if not capability["available"]:
        return None, ({
            "error": "vision_resident_update_required",
            "runtime": capability["runtime"],
        }, 409)
    if str(route.get("vision_test_status") or "untested") != "ok":
        return None, ({"error": "vision_model_required"}, 409)
    return route, None
