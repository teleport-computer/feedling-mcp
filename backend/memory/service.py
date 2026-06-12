"""Memory Garden storage: typed moments, change log, per-tab floors."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

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
        return db.memory_load(store.user_id)
    except Exception as e:
        print(f"[{store.user_id}/memory] load failed: {e}")
    return []


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


def _save_moments(store: UserStore, moments: list):
    with store.memory_lock:
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
        "capture_mode", "source_chat_message_ids", "anchor_memory_ids",
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
    """Return {story: int, about_me: int, ta_thinking: int, total: int}."""
    counts = {"story": 0, "about_me": 0, "ta_thinking": 0, "total": 0}
    if not isinstance(moments, list):
        return counts
    for m in moments:
        if not isinstance(m, dict):
            continue
        if _memory_is_archived(m):
            continue
        counts["total"] += 1
        t = m.get("type", "")
        tab = TAB_FOR_TYPE.get(t)
        if tab:
            counts[tab] += 1
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
    three (some over-shooting on About me shouldn't compensate for an
    empty Story); it's the bootstrap-gate threshold that subsumes them.

    Tiers (post-2026-05-22):
      ≥ 6 months: 15 / 60 / 12   (total 87)  — established, deep substrate
      ≥ 1 month:   8 / 25 /  5   (total 38)  — real history
      ≥ 2 days:    3 /  8 /  2   (total 13)  — recent but real
      < 2 days:    1 /  1 /  0   (total  2)  — we-just-met

    Per-tab floors drive identity_init gate (Story + About me floors are
    hard prerequisites; TA 在想 is encouraged but not blocking because
    reflections require substrate from the other two tabs first).
    """
    if days >= 180:
        return {"story": 15, "about_me": 60, "ta_thinking": 12, "total": 87}
    if days >= 30:
        return {"story":  8, "about_me": 25, "ta_thinking":  5, "total": 38}
    if days >= 2:
        return {"story":  3, "about_me":  8, "ta_thinking":  2, "total": 13}
    return     {"story":  1, "about_me":  1, "ta_thinking":  0, "total":  2}


def _memory_floor_for_days(days: int) -> int:
    """Total memory floor used by the bootstrap gate. Backwards-compatible
    name; preserved for callers that don't care about per-tab breakdown.
    """
    return _per_tab_floors_for_days(days)["total"]
