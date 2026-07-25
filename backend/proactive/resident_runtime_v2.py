"""Resident Runtime V2 rollout helpers.

This is an operations cutover flag, not a user-facing switch. Resident users do
not have the hosted model_api runtime profile, so the flag lives in its own
per-user blob.

⚠️ It does NOT default off in a deployed environment: absence falls through to
core/util.runtime_v2_default_on(), and every main compose injects
FEEDLING_RUNTIME_V2_DEFAULT_ON=true (prod included). Off is only the default of
an env-less process.
"""
from __future__ import annotations

from typing import Any, Mapping

import db
from core import util as core_util
from core.store import UserStore

RESIDENT_RUNTIME_PROFILE_KIND_V2 = "resident_runtime_v2"
RESIDENT_WAKE_RUNTIME_V2_FLAG = "resident_wake_runtime_v2_enabled"
RESIDENT_CHAT_RUNTIME_V2_FLAG = "resident_chat_runtime_v2_enabled"


def load_resident_runtime_profile_v2(store: UserStore) -> dict[str, Any]:
    doc = db.get_blob(store.user_id, RESIDENT_RUNTIME_PROFILE_KIND_V2)
    return dict(doc) if isinstance(doc, Mapping) else {}


def _resident_flag_enabled(store: UserStore, flag: str) -> bool:
    # No auto-seeding: the resident_runtime_v2 blob only carries a key when an
    # operator set it. So an explicit value (True or False) wins; absence falls
    # back to the env-gated baseline — which every deployed environment (prod
    # included) turns ON, see core/util.runtime_v2_default_on.
    try:
        val = load_resident_runtime_profile_v2(store).get(flag)
        return core_util.runtime_v2_default_on() if val is None else bool(val)
    except Exception:
        return False


def resident_wake_runtime_v2_enabled(store: UserStore) -> bool:
    return _resident_flag_enabled(store, RESIDENT_WAKE_RUNTIME_V2_FLAG)


def resident_chat_runtime_v2_enabled(store: UserStore) -> bool:
    return _resident_flag_enabled(store, RESIDENT_CHAT_RUNTIME_V2_FLAG)


def resident_runtime_v2_public_profile(store: UserStore) -> dict[str, Any]:
    return {
        RESIDENT_WAKE_RUNTIME_V2_FLAG: resident_wake_runtime_v2_enabled(store),
        RESIDENT_CHAT_RUNTIME_V2_FLAG: resident_chat_runtime_v2_enabled(store),
    }
