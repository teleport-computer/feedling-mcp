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
    """Return the optional dedicated route and reject only a known text-only route.

    Untested/failed/testing verdicts still flow to the real call. A cached
    ``unsupported`` verdict is definitive for its exact provider/model binding,
    so reject it before persisting pixels while keeping image selection enabled.
    """
    selected = db.model_api_vision_route(store.user_id)
    if selected is not None:
        route = selected
    elif accounts_onboarding._load_onboarding_route(store) == "model_api":
        route = db.model_api_active_route(store.user_id)
    else:
        resident = chat_consumer.resident_vision_validation(store)
        if str(resident.get("status") or "") != "unsupported":
            return None, None
        return None, ({
            "error": "vision_model_incompatible",
            "retryable": False,
            "provider": str(resident.get("provider") or ""),
            "model": str(resident.get("model") or ""),
        }, 400)

    if not isinstance(route, dict) or str(
        route.get("vision_test_status") or "untested"
    ) != "unsupported":
        return selected, None
    return None, ({
        "error": "vision_model_incompatible",
        "retryable": False,
        "provider": str(route.get("provider") or ""),
        "model": str(route.get("model") or ""),
    }, 400)
