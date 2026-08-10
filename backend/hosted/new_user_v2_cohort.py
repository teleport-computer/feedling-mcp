"""Fail-safe defaulting policy for newly registered Model API users.

The allowlist is the rollout authority.  This module only creates its own
automatic entry with an insert-only compare-and-set, so an operator's existing
pin always wins.  The runtime fence itself is changed exclusively through the
normal hosted runtime transition API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import db
from hosted import config_store


NEW_USER_V2_CUTOFF_ENV = "FEEDLING_V2_NEW_USER_CUTOFF"
AUTO_UPDATED_BY = "new-user-cohort"
ACCESS_MODE_UPDATED_BY = "access-mode"


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    normalized_cutoff: str = ""


def _parse_timestamp(raw: str, *, allow_naive: bool) -> datetime:
    """Parse an ISO timestamp as UTC, allowing historical naive user dates."""
    value = str(raw or "").strip()
    parsed = datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith("Z") else value
    )
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("timezone required")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def decision_for_user(user_id: str) -> Decision:
    """Return a cutoff decision, failing safely to resident on bad inputs."""
    raw_cutoff = str(os.environ.get(NEW_USER_V2_CUTOFF_ENV) or "").strip()
    if not raw_cutoff:
        return Decision(False, "no_cutoff")
    try:
        cutoff = _parse_timestamp(raw_cutoff, allow_naive=False)
    except (TypeError, ValueError):
        return Decision(False, "invalid_cutoff")

    raw_created = db.get_user_created_at_strict(user_id)
    if not raw_created:
        return Decision(False, "invalid_created_at")
    try:
        # Older rows can predate timezone-aware storage.  They represented UTC
        # at creation time, so normalize those values explicitly rather than
        # accepting the host process timezone.
        created = _parse_timestamp(raw_created, allow_naive=True)
    except (TypeError, ValueError):
        return Decision(False, "invalid_created_at")

    normalized_cutoff = cutoff.isoformat().replace("+00:00", "Z")
    eligible = created >= cutoff
    return Decision(
        eligible,
        "eligible" if eligible else "before_cutoff",
        normalized_cutoff,
    )


def _log(user_id: str, outcome: str) -> None:
    """Emit only the user identifier and a bounded policy outcome."""
    print(f"[new-user-v2:{user_id}] outcome={outcome}")


def restore_allowlist(user_id: str, previous: dict | None) -> None:
    """Restore the allowlist state captured before a resident transition."""
    if previous is None:
        db.delete_runtime_allowlist(user_id)
        return
    db.upsert_runtime_allowlist(
        user_id,
        previous["desired"],
        updated_by=previous["updated_by"],
        note=previous["note"],
    )


def pin_resident(store) -> None:
    """Persist explicit resident ownership, then converge the runtime fence."""
    db.upsert_runtime_allowlist(
        store.user_id,
        "resident",
        updated_by=ACCESS_MODE_UPDATED_BY,
        note="user-selected-resident",
    )
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_RESIDENT
    )


def _require_tested_active_route(store) -> None:
    """Fail closed unless the current active route is proven runnable."""
    route = config_store.load_active_route(store)
    if route is None or route.get("test_status") != "ok":
        raise RuntimeError(
            "new-user V2 cohort requires a tested active route"
        )


def apply_default(store) -> str:
    """Apply the new-user default without overriding an explicit rollout pin."""
    if config_store.hosted_runtime_policy() != config_store.HOSTED_RUNTIME_POLICY_DUAL:
        return "forced_policy"

    existing = db.get_runtime_allowlist_entry(store.user_id)
    inserted = False
    if existing is None:
        decision = decision_for_user(store.user_id)
        if not decision.eligible:
            _log(store.user_id, decision.reason)
            return decision.reason
        _require_tested_active_route(store)
        inserted = db.insert_runtime_allowlist_if_absent(
            store.user_id,
            "v2",
            updated_by=AUTO_UPDATED_BY,
            note=f"registered-at-or-after:{decision.normalized_cutoff}",
        )
        # Re-read after the CAS: an admin pin installed in the race is now the
        # authoritative decision, regardless of which writer won the insert.
        existing = db.get_runtime_allowlist_entry(store.user_id)

    if not existing:
        _log(store.user_id, "explicit_pin")
        return "explicit_pin"

    is_automatic = existing["updated_by"] == AUTO_UPDATED_BY
    if existing["desired"] != "v2":
        outcome = "automatic_resident_pin" if is_automatic else "explicit_pin"
        _log(store.user_id, outcome)
        return outcome

    # Both automatic admission and an explicit V2 pin are synchronous
    # ownership decisions.  Keep the row untouched, but do not report setup or
    # activation success until its tested route and fence have converged.
    _require_tested_active_route(store)

    if is_automatic:
        _log(
            store.user_id,
            "record_created" if inserted else "record_already_present",
        )
    else:
        _log(store.user_id, "explicit_v2_pin")
    config_store.set_hosted_runtime_mode(
        store, config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    )
    _log(store.user_id, "converged")
    return "converged"
