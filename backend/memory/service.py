"""Memory Garden storage: memories, change log, days-scaled guidance floor (advisory only)."""

import threading
import time
import uuid
from datetime import datetime


import db
from core.store import UserStore

from core import util as core_util

MEMORY_TYPES = ("moment", "quote", "fact", "event", "insight", "reflection")

# Which iOS Garden tab a type renders into.
TAB_FOR_TYPE = {
    "moment":     "story",
    "quote":      "story",
    "fact":       "about_me",
    "event":      "about_me",
    "insight":    "ta_thinking",
    "reflection": "ta_thinking",
}


def _load_moments(store: UserStore) -> list:
    try:
        return [to_v1_card(moment) for moment in db.memory_load(store.user_id)]
    except Exception as e:
        print(f"[{store.user_id}/memory] load failed: {e}")
    return []


def _salience_to_importance(value, default: float = 0.5) -> float:
    salience = str(value or "").strip().lower()
    if salience == "critical":
        return 0.9
    if salience == "high":
        return 0.75
    if salience == "medium":
        return 0.5
    if salience == "low":
        return 0.25
    return default


def _float_01(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def to_v1_card(doc: dict) -> dict:
    """Normalize plaintext envelope fields without looking inside body_ct.

    The content fields are encrypted, so old inner shapes are adapted in the
    enclave after decryption. This function only supplies v1 envelope defaults.
    """
    if not isinstance(doc, dict):
        return doc
    card = dict(doc)
    card["status"] = str(card.get("status") or "active").strip().lower() or "active"
    if "importance" not in card:
        card["importance"] = _salience_to_importance(card.get("salience"), 0.5)
    else:
        card["importance"] = _float_01(card.get("importance"), _salience_to_importance(card.get("salience"), 0.5))
    if "pulse" not in card:
        card["pulse"] = 0.3
    else:
        card["pulse"] = _float_01(card.get("pulse"), 0.3)
    if not str(card.get("last_referenced_at") or "").strip():
        card["last_referenced_at"] = str(
            card.get("occurred_at")
            or card.get("updated_at")
            or card.get("created_at")
            or core_util._now_iso()
        )
    return card


def _memory_is_archived(moment: dict) -> bool:
    return bool(
        isinstance(moment, dict)
        and (
            moment.get("is_archived") is True
            or str(moment.get("archived_at") or "").strip()
            or str(moment.get("archive_reason") or "").strip()
        )
    )


def _active_memory_moments(moments: list) -> list[dict]:
    return [m for m in moments if isinstance(m, dict) and not _memory_is_archived(m)]


_FALLBACK_MUTATION_LOCK = threading.RLock()


def mutation_lock(store):
    """The store's reentrant memory_lock, held across a load→mutate→save so
    concurrent same-user writes serialize and can't lost-update (a stale-snapshot
    ``memory_replace_all`` would otherwise delete a concurrently-added card).

    A real ``UserStore`` always has one; the process-wide fallback only covers
    partial test doubles that mock ``_load_moments``/``_save_moments`` and never
    reach the real DB reconcile."""
    return getattr(store, "memory_lock", None) or _FALLBACK_MUTATION_LOCK


def _save_moments(store: UserStore, moments: list):
    with mutation_lock(store):
        db.memory_replace_all(store.user_id, moments)


def _append_memory_change(store: UserStore, entry: dict) -> dict:
    record = {
        "id": uuid.uuid4().hex[:16],
        "ts": core_util._now_iso(),
        "action": str(entry.get("action") or "unknown")[:80],
        "memory_id": str(entry.get("memory_id") or "")[:160],
    }
    for key in (
        "type", "old_type", "new_type", "fields", "reason",
        "capture_mode", "source_chat_message_ids", "anchor_memory_ids", "supersedes",
    ):
        if key in entry:
            record[key] = entry[key]
    # ts is an ISO string here, so leave the indexed ts column NULL.
    db.log_append(store.user_id, "memory_changes", record)
    return record


def _append_memory_capture_job(store: UserStore, entry: dict) -> dict:
    job = {
        "job_id": entry.get("job_id") or f"mc_{uuid.uuid4().hex[:16]}",
        "ts": time.time(),
        "created_at": core_util._now_iso(),
        "status": str(entry.get("status") or "queued")[:80],
        "mode": str(entry.get("mode") or "running")[:80],
    }
    for key in (
        "source_chat_message_ids", "message_chars", "reply_chars",
        "actions_planned", "actions_written", "effects", "error", "warnings",
        "reason", "turn_count", "progress", "messages_reviewed",
        "candidate_windows_total", "candidate_windows_done",
        "candidates_extracted", "candidate_cluster_count",
        "memories_planned", "memories_created", "first_message_ts",
        "latest_message_ts", "recap_job_id", "old_cards_detected",
        "old_cards_archived", "new_cards_planned", "new_cards_created",
        "repair_noisy_ids", "archive_old",
    ):
        if key in entry:
            job[key] = entry[key]
    db.log_append(store.user_id, "memory_capture_jobs", job,
                  ts=job["ts"], item_key=job["job_id"])
    return job


def _count_by_tab(moments: list) -> dict:
    """Return legacy-shaped counts backed by v1 active-card count.

    v1 no longer stores type/tab. Keep the old return shape so bootstrap and
    verify routes do not break while their wording is retired in P6.
    """
    counts = {"story": 0, "about_me": 0, "ta_thinking": 0, "total": 0}
    if not isinstance(moments, list):
        return counts
    for m in moments:
        if not isinstance(m, dict):
            continue
        if _memory_is_archived(m):
            continue
        counts["total"] += 1
    counts["story"] = counts["total"]
    counts["about_me"] = counts["total"]
    counts["ta_thinking"] = counts["total"]
    return counts


def _validate_anchor_ids(moments: list, anchor_ids, owner_user_id: str) -> tuple:
    """Validate that every anchor_memory_id refers to an existing memory
    owned by this user. Returns (ok: bool, error_dict | None). Caller is
    expected to have already type-checked anchor_ids as a list of strings.
    """
    if not isinstance(anchor_ids, list):
        return False, {"error": "anchor_memory_ids must be a list of memory ids"}
    if any(not isinstance(x, str) or not x for x in anchor_ids):
        return False, {"error": "anchor_memory_ids must be non-empty strings"}
    existing_ids = {m.get("id") for m in moments if isinstance(m, dict)}
    missing = [aid for aid in anchor_ids if aid not in existing_ids]
    if missing:
        return False, {
            "error": "anchor_memory_ids_not_found",
            "missing": missing,
            "required": (
                "Each anchor must reference a memory id that already exists "
                "in this user's garden. Write the substrate memories first."
            ),
        }
    return True, None


def _reflection_time_cap_ok(moments: list, days: int) -> tuple:
    """Enforce reflection cadence by relationship age tier.

    <30 days: hard max of 2 reflections lifetime.
    30-180 days: ≥7 rolling days since last reflection.
    ≥180 days: ≥3 rolling days since last reflection.

    Returns (ok: bool, error_dict | None).
    """
    reflections = [
        m for m in moments
        if isinstance(m, dict) and m.get("type") == "reflection"
    ]
    if days < 30:
        if len(reflections) >= 2:
            return False, {
                "error": "reflection_lifetime_cap",
                "current_count": len(reflections),
                "cap": 2,
                "required": (
                    f"At {days} days of relationship, you can hold at most 2 "
                    "reflections total. Substrate is still thin — write more "
                    "facts/events/quotes/moments before standalone reflections."
                ),
            }
        return True, None

    # For older tiers, find the latest reflection's created_at.
    latest = None
    for r in reflections:
        ca = r.get("created_at", "")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue
    if latest is None:
        return True, None  # No prior reflection → free to write.
    cap_days = 7 if days < 180 else 3
    elapsed = (datetime.now() - latest).total_seconds() / 86400.0
    if elapsed < cap_days:
        return False, {
            "error": "reflection_too_soon",
            "elapsed_days": round(elapsed, 2),
            "min_days": cap_days,
            "required": (
                f"Reflections need {cap_days}+ days between them at this "
                f"relationship age; last reflection was {elapsed:.1f} days ago. "
                "Let substrate accumulate before reflecting again."
            ),
        }
    return True, None


def _per_tab_floors_for_days(days: int) -> dict:
    """Per-tab memory floors by relationship age. Returns
    {story, about_me, ta_thinking, total}. The total isn't a sum of the
    three; it is the v2 consistency signal surfaced in bootstrap/status.

    Tiers (post-2026-07-14 Seven calibration):
      ≥ 6 months:  5 / 20 /  5   (total 30)  — established, deep substrate
      ≥ 1 month:   2 /  8 /  2   (total 12)  — real history
      ≥ 2 days:    1 /  3 /  1   (total  5)  — recent but real
      < 2 days:    1 /  1 /  0   (total  2)  — we-just-met

    Per-tab floors are display/diagnostic only. Memory quantity must never
    gate onboarding or chat; it only flags possible under-distillation.
    """
    if days >= 180:
        return {"story":  5, "about_me": 20, "ta_thinking":  5, "total": 30}
    if days >= 30:
        return {"story":  2, "about_me":  8, "ta_thinking":  2, "total": 12}
    if days >= 2:
        return {"story":  1, "about_me":  3, "ta_thinking":  1, "total":  5}
    return     {"story":  1, "about_me":  1, "ta_thinking":  0, "total":  2}


def _memory_floor_for_days(days: int) -> int:
    """Days-scaled memory-count floor (TOTAL only; v2 has no tabs). NOT a
    gate — used as a consistency signal: a long relationship should have a
    proportional number of memory cards. See _per_tab_floors_for_days for
    history (was a hard onboarding gate, now decoupled from gating).
    """
    return int(_per_tab_floors_for_days(days).get("total", 0))


def _memory_aspiration_for_days(days: int) -> int:
    """Wide upper reference for resident distillation density.

    Like the floor, this is a consistency signal only. It gives the VPS resident
    a target range while preserving the non-fiction rule: sparse material should
    still produce fewer cards, never invented filler.
    """
    try:
        d = int(days or 0)
    except Exception:
        d = 0
    if d >= 180:
        return 70
    if d >= 30:
        return 35
    if d >= 2:
        return 15
    return 4
