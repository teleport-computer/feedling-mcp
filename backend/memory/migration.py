"""Legacy memory card (pre-v1) → v1 migration — pure helpers (plan §3).

Detection + batch selection + per-user migration state, all side-effect free so
they unit-test without DB/enclave. The orchestration (decrypt batch → call_agent
derive v1 → memory.upgrade) lives in the resident consumer's migrate handler;
the trigger lives in capture_scheduler. Both build on these.

DECRYPT NOTE: judge shape on the RAW inner from the memory_action decrypt path
(`_memory_plain_from_envelope`). Do NOT judge on the readside output — the enclave
readside already adapts old→v1 for display (`enclave_app._memory_inner_to_v1`), so
a legacy card read there looks v1 and would never be detected.
"""
from __future__ import annotations

import os
import time
from typing import Any, Mapping

from core import envelope as core_envelope

MIGRATION_STATE_BLOB = "memory_migration_state"
DEFAULT_MIGRATE_BATCH = 8


def migration_enabled() -> bool:
    """Legacy→v1 migration runs ONLY when FEEDLING_MIGRATE_ENABLE is truthy.

    Default OFF — unset / empty / 0 / false / no / off all mean disabled. Opt-in
    kill switch: set =1 (on backend + consumer env) to run migration; unset or set 0
    + restart to stop it instantly without a deploy. Every migrate surface (enqueue /
    tick / process / memory.upgrade) checks this, so 'off' is a true full stop —
    not just 'auto-trigger off'."""
    return str(os.environ.get("FEEDLING_MIGRATE_ENABLE", "")).strip().lower() in ("1", "true", "yes", "on")


# Re-scan even a 'done' user this often, so a card reverted to old shape by some
# legacy path still self-heals (§5.5-C). Cheap: just re-enqueues one batch job.
DEFAULT_REAUDIT_SEC = 7 * 24 * 3600

# Per-card attempt cap (A11). A card that can NEVER migrate (agent keeps dropping it
# / envelope build keeps failing) would otherwise loop forever every quiet window and
# pin the user at 'pending' for good. After this many failed attempts on a card we
# mark it skipped: stop selecting it, leave it legacy (still readable — readside
# adapts old→v1), so legacy_remaining can reach 0 and status → done. Tunable via
# FEEDLING_MIGRATE_MAX_ATTEMPTS (same env-flag style as the rest of the migrate knobs).
DEFAULT_MAX_ATTEMPTS = 3


def max_attempts() -> int:
    """Per-card failed-attempt cap; >= this many fails ⇒ card is skipped (capped)."""
    try:
        n = int(os.environ.get("FEEDLING_MIGRATE_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS)) or DEFAULT_MAX_ATTEMPTS)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_ATTEMPTS
    return max(1, n)

# v1 inners always carry {bucket, threads} (memory._memory_inner_from_action);
# old inners carry these content fields and none of the v1 structure.
_OLD_CONTENT_FIELDS = ("title", "description", "her_quote", "context", "linked_dimension")


_V1_FIELDS = ("summary", "content", "bucket", "threads")


def is_legacy_card_inner(inner: Mapping[str, Any] | None) -> bool:
    """True if a DECRYPTED RAW inner is a pre-v1 (old-schema) card.

    A genuine v1 inner has the full {summary, content, bucket, threads} set — same
    criterion the enclave uses to detect v1 (`enclave_app._memory_inner_to_v1`).
    Requiring all four (not just bucket+threads) avoids skipping a card that was
    patched to carry bucket/threads but whose body is still title/description."""
    if not isinstance(inner, Mapping):
        return False
    if all(k in inner for k in _V1_FIELDS):
        return False  # full v1 structure
    return any(inner.get(k) for k in _OLD_CONTENT_FIELDS)


def body_hash(moment: Mapping[str, Any] | None) -> str:
    """CAS token over the authoritative persisted content shape."""
    return core_envelope.envelope_content_token(dict(moment or {}))


def select_legacy_batch(
    decrypted: list[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    batch_size: int = DEFAULT_MIGRATE_BATCH,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """From [(moment, raw_inner), ...] pick up to batch_size legacy cards, each as
    {id, inner, old_body_hash} for the migrate→upgrade loop. Skips id-less rows.

    `exclude_ids` = cards that hit the per-card attempt cap (skipped, A11) — they
    stay legacy/readable but are never re-selected, so legacy_remaining can reach 0."""
    skip = {str(i) for i in (exclude_ids or set())}
    out: list[dict] = []
    cap = max(1, int(batch_size))
    for moment, inner in decrypted:
        if not isinstance(moment, Mapping):
            continue
        mid = str(moment.get("id") or "")
        if not mid or mid in skip or not is_legacy_card_inner(inner):
            continue
        out.append({"id": mid, "inner": dict(inner), "old_body_hash": body_hash(moment)})
        if len(out) >= cap:
            break
    return out


def count_legacy(
    decrypted: list[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    exclude_ids: set[str] | None = None,
) -> int:
    """Count legacy cards still eligible to migrate. Capped (skipped) cards are
    excluded so this can reach 0 and the state machine can settle on 'done'."""
    skip = {str(i) for i in (exclude_ids or set())}
    return sum(
        1 for m, inner in decrypted
        if is_legacy_card_inner(inner) and str((m or {}).get("id") or "") not in skip
    )


def migrate_key_for_window(user_id: str, window_id: str) -> str:
    """Idempotent migrate-job key: one batch per user per quiet window."""
    return f"migrate:v1:{user_id}:{window_id}"[:240]


# --- per-user migration state blob (status: unknown | pending | done) ---

def initial_state() -> dict:
    return {"v": 1, "status": "unknown", "legacy_remaining": -1, "migrated_total": 0, "attempts": {}}


# --- per-card attempt cap (A11) ---
# The attempt count lives in the state blob under `attempts: {card_id: n}` — per-user,
# cheap, and the trigger/handler already read+write this blob. A card with
# n >= max_attempts() is "skipped/capped": excluded from selection so it stops
# retrying and legacy_remaining can reach 0. Old blobs that predate A11 simply lack
# `attempts`; every reader treats a missing/garbage map as {} (back-compat).

def _attempts_map(state: Mapping[str, Any] | None) -> dict[str, int]:
    """Read the per-card attempt map, tolerating old blobs / bad shapes (→ {})."""
    raw = (state or {}).get("attempts") if isinstance(state, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    return out


def capped_ids(state: Mapping[str, Any] | None, *, cap: int | None = None) -> set[str]:
    """Card ids that have hit the attempt cap (skipped) and must be excluded from
    selection/count so they never get retried again. cap defaults to max_attempts()."""
    limit = max_attempts() if cap is None else max(1, int(cap))
    return {mid for mid, n in _attempts_map(state).items() if n >= limit}


def is_capped(state: Mapping[str, Any] | None, card_id: str, *, cap: int | None = None) -> bool:
    limit = max_attempts() if cap is None else max(1, int(cap))
    return _attempts_map(state).get(str(card_id), 0) >= limit


def bump_attempts(
    state: Mapping[str, Any] | None,
    failed_ids,
    *,
    now: float | None = None,
) -> dict:
    """Return a copy of `state` with each id in `failed_ids` incremented by 1 in the
    per-card attempt map. Pure: the handler computes the failed-id list (agent dropped
    it / envelope build / upgrade failed) and the persistence layer stores the result.
    Ignores blank ids; preserves all other state fields. Does NOT touch status here —
    next_state() recomputes status/legacy_remaining from the (now-capped-excluded) scan."""
    base = dict(state) if isinstance(state, Mapping) else initial_state()
    attempts = _attempts_map(base)
    for raw_id in (failed_ids or []):
        mid = str(raw_id or "").strip()
        if not mid:
            continue
        attempts[mid] = attempts.get(mid, 0) + 1
    base["attempts"] = attempts
    base["v"] = 1
    base["updated_at"] = float(now if now is not None else time.time())
    return base


def migration_done(state: Mapping[str, Any] | None) -> bool:
    return isinstance(state, Mapping) and str(state.get("status") or "").lower() == "done"


def should_enqueue(
    state: Mapping[str, Any] | None,
    *,
    observed_legacy_count: int | None = None,
) -> bool:
    """Whether to enqueue a migration batch.

    Card SHAPE is the source of truth; the state blob is only a cache. So when the
    caller has actually scanned and knows how many legacy cards remain, trust that
    (`observed_legacy_count`) — this is what makes plan §5.5-C self-heal work: even
    after status==done, a card reverted to old shape (concurrent replace-all / old
    path) re-enqueues. Only fall back to the cached state when no observation."""
    if observed_legacy_count is not None:
        return int(observed_legacy_count) > 0
    return not migration_done(state)


def next_state(
    state: Mapping[str, Any] | None,
    *,
    migrated: int,
    legacy_remaining: int,
    now: float | None = None,
) -> dict:
    """Compute the updated state after a batch. legacy_remaining<=0 ⇒ done."""
    base = dict(state) if isinstance(state, Mapping) else initial_state()
    base["v"] = 1
    base["migrated_total"] = int(base.get("migrated_total") or 0) + max(0, int(migrated))
    base["legacy_remaining"] = int(legacy_remaining)
    base["status"] = "done" if int(legacy_remaining) <= 0 else "pending"
    base["updated_at"] = float(now if now is not None else time.time())
    return base


def reaudit_due(state: Mapping[str, Any] | None, *, now: float | None = None, reaudit_sec: float = DEFAULT_REAUDIT_SEC) -> bool:
    """For a 'done' user, whether enough time passed to re-scan once (self-heal).
    Non-done users don't need this — should_enqueue already enqueues them."""
    if not migration_done(state):
        return False
    now_ts = float(now if now is not None else time.time())
    try:
        updated_at = float((state or {}).get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    return (now_ts - updated_at) >= float(reaudit_sec)
