"""Shared visual-route capability and send-time pinning for every runtime."""

from __future__ import annotations

import db
from accounts import onboarding as accounts_onboarding
from chat import consumer as chat_consumer
from hosted import config_store as hosted_config_store


def runtime_capability(store) -> dict:
    onboarding_route = accounts_onboarding._load_onboarding_route(store)
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
    """Return the user's optional dedicated route without gating image delivery.

    Capability verdicts are settings/UI signals only.  ``unsupported``,
    ``failed``, ``testing``, and ``untested`` all preserve the configured send
    path: follow-main still sends pixels to the main model, while a selected
    dedicated route stays pinned for observation.  The provider's real response
    owns the turn outcome.
    """
    selected = db.model_api_vision_route(store.user_id)
    return selected, None
