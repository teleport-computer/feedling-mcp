"""Identity card storage, change log, relationship-day anchors."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta


import db
from core.store import UserStore

from memory import service as memory_service

def _load_identity(store: UserStore) -> dict | None:
    try:
        data = db.get_blob(store.user_id, "identity")
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[{store.user_id}/identity] load failed: {e}")
    return None


def _save_identity(store: UserStore, data: dict):
    with store.identity_lock:
        db.set_blob(store.user_id, "identity", data)
    # identity 密文信封由 tee_replicator 明文化管辖（db.set_blob 不镜像 identity）。
    # 一次原地 identity UPDATE 保持同一 user_blobs PK，游标式 replicator 永不回头，
    # 故把它放上 requeue lane：下一趟 worker identity pass 会重新解密落 TEE 明文。
    # 影子期尽力而为（写失败吞掉）。item_id 用常量 "identity"，与
    # tee_replicator.worker 的 identity _Table（unpack 写死 item_id="identity"）对齐。
    from tee_shadow import mirror
    mirror.mark_pending(store.user_id, "identity", "identity", "requeue")


# Identity change audit log
# ---------------------------------------------------------------------------
# Appended to on every identity_init / replace / nudge. Surfaced to iOS as
# the "最近的变化" feed and the local push trigger. Server doesn't decrypt
# the envelope, so the diff (dimension / old / new / reason) is supplied
# by the caller — the MCP tools do this; HTTP-mode callers can pass an
# optional `audit` field on identity_init / identity_replace requests.

def _append_identity_change(store: UserStore, entry: dict) -> dict:
    """Append a single audit entry. Always returns the stored entry
    (with `id` and `ts` injected) so the caller can echo it back. Never
    raises — audit failures must not break the underlying write."""
    record = {
        "id": uuid.uuid4().hex[:16],
        "ts": datetime.now().isoformat(),
        "action": entry.get("action", "unknown"),
    }
    # Whitelist + coerce the fields the iOS card needs. Anything else
    # the caller submits is dropped silently so we don't leak whatever
    # debugging junk the agent stuffed in.
    for k in ("dimension", "old_value", "new_value", "delta", "reason"):
        if k in entry:
            record[k] = entry[k]
    # ts here is an ISO string, not an epoch — leave the indexed ts column NULL
    # and keep the since/sort filtering in Python (string comparison) below.
    db.log_append(store.user_id, "identity_changes", record)
    return record


def _load_identity_changes(store: UserStore, since: str = "", limit: int = 50) -> list:
    """Read the audit log. `since` is an ISO timestamp string; results
    are filtered to entries with ts > since, newest-first, capped at limit."""
    entries = db.log_read_all(store.user_id, "identity_changes")
    if since:
        entries = [e for e in entries if e.get("ts", "") > since]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


def _parse_iso_calendar_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except Exception:
            pass
    m = re.match(r"^\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})(?:\D|$)", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    try:
        norm = raw.replace("年", "-").replace("月", "-").replace("日", "")
        norm = norm.replace("/", "-").replace(".", "-").replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm + "T00:00:00"
        return datetime.fromisoformat(norm).date()
    except Exception:
        return None


def _earliest_memory_date(store: UserStore) -> date | None:
    dates: list[date] = []
    for moment in memory_service._load_moments(store):
        if not isinstance(moment, dict):
            continue
        d = _parse_iso_calendar_date(moment.get("occurred_at", ""))
        if d:
            dates.append(d)
    return min(dates) if dates else None


def _anchor_from_days(days: int, store: UserStore | None = None, prefer_memory: bool = False) -> str:
    """Convert "we've known each other N days" into a fixed ISO timestamp.

    The anchor is the source of truth for days_with_user — every read computes
    a calendar-day delta from this date, so the displayed count increments at
    midnight instead of at the exact bootstrap hour.
    """
    if prefer_memory and store is not None:
        earliest = _earliest_memory_date(store)
        if earliest:
            return earliest.isoformat()
    safe_days = max(0, int(days))
    started_at = datetime.now().date() - timedelta(days=safe_days)
    return started_at.isoformat()


def _live_days_with_user(identity: dict, store: UserStore | None = None) -> int:
    """Compute the live days_with_user from the relationship anchor."""
    anchor_date = _parse_iso_calendar_date(identity.get("relationship_started_at", ""))

    # Migration repair for anchors created from server UTC time after the
    # user's local midnight boundary: if old identities have no explicit
    # anchor source and the memory garden proves an earlier first date, use it.
    if store is not None and not identity.get("relationship_anchor_source"):
        earliest = _earliest_memory_date(store)
        if earliest and (anchor_date is None or earliest < anchor_date):
            anchor_date = earliest

    if not anchor_date:
        return 0
    return max(0, (datetime.now().date() - anchor_date).days)


_IDENTITY_RUNTIME_LABELS = {
    "io", "feedling", "p0", "p-zero",
    "hermes", "claude", "claude code", "claude desktop", "claude-code", "claude-desktop",
    "claude.ai", "anthropic", "openclaw", "open-claw", "open claw", "cursor",
    "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o", "gpt-5", "openai", "openrouter",
    "gemini", "google ai", "google", "bard", "deepseek", "minimax", "copilot", "github copilot",
    "agent", "assistant", "ai", "bot",
}

_IDENTITY_PROFILE_STRING_FIELDS = (
    "agent_name",
    "self_introduction",
    "category",
    "user_preferred_name",
    "agent_role",
    "tone_style",
    # User-authored persona override (D1 user layer / feedback 4b): a free-text
    # directive the user writes to pin the agent's role and voice. Highest-
    # priority persona signal, distinct from the system-distilled tone_style.
    # Editable via identity.profile_patch and (later) iOS. See prompts.py for
    # the precedence instruction injected into the foreground chat prompt.
    "custom_persona_prompt",
    "language_preference",
    "relationship_anchor",
)
_IDENTITY_PROFILE_LIST_FIELDS = (
    "signature",
    "boundaries",
    "do_not_say",
    "stable_definitions",
)
_IDENTITY_PROFILE_FIELDS = set(_IDENTITY_PROFILE_STRING_FIELDS) | set(_IDENTITY_PROFILE_LIST_FIELDS)


def _relationship_age_days(store) -> int:
    """Best-effort relationship age in days. Reads from identity anchor
    if present; otherwise falls back to earliest memory's occurred_at;
    finally to 0 (treat as fresh)."""
    identity = _load_identity(store)
    if identity and identity.get("relationship_started_at"):
        return _live_days_with_user(identity, store=store)
    moments = memory_service._load_moments(store)
    if moments:
        try:
            earliest = _earliest_memory_date(store)
            if earliest:
                return max(0, (datetime.now().date() - earliest).days)
        except Exception:
            pass
    return 0
