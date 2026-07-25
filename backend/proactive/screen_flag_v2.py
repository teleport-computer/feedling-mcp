"""Per-user flag for screen.read VLM captioning.

Default follows the env-gated baseline (core/util.runtime_v2_default_on).
⚠️ That baseline is ON in prod, test AND pre — every main compose injects
FEEDLING_RUNTIME_V2_DEFAULT_ON=true — so the effective default for a user with
no explicit value is ENABLED, not disabled. ("OFF in prod-without-env", the
earlier wording here, is true only of a process that never got the env, e.g. a
local run.) An explicit per-user value still wins. NOTE: this gates screen
egress to a VLM, so it stays fail-closed on ERRORS — any config/db hiccup falls
back to OFF — but a clean read of an unset flag lands on the ON baseline, which
is a deliberate rollout choice, not a fail-closed one.
"""
from __future__ import annotations

from core import util as core_util
from hosted import config_store as hosted_config_store

SCREEN_CAPTION_FLAG = "screen_caption_enabled"


def screen_caption_enabled(store) -> bool:
    try:
        config = hosted_config_store._load_model_api_config(store) or {}
        profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
        if SCREEN_CAPTION_FLAG in profile:
            return bool(profile.get(SCREEN_CAPTION_FLAG))
        if SCREEN_CAPTION_FLAG in config:
            return bool(config.get(SCREEN_CAPTION_FLAG))
        return core_util.runtime_v2_default_on()
    except Exception:
        return False
