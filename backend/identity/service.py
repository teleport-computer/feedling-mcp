"""Identity card storage, change log, relationship-day anchors."""

import re
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta


import db
from core.store import UserStore

from memory import service as memory_service

# Re-exported: consumers access this as identity_service._IDENTITY_RUNTIME_LABELS
# (identity/actions.py, genesis/service.py, hosted/history_import.py).
from identity.card_policy import RUNTIME_LABELS as _IDENTITY_RUNTIME_LABELS  # noqa: F401
from identity import card_policy


# Per-user identity-mutation mutex. Broader than UserStore.identity_lock below
# (which only wraps the final db.set_blob write in _save_identity): that lock
# leaves the read-existing-card -> merge -> re-encrypt span unguarded, so two
# concurrent profile_patch calls for the same user can both read the same
# pre-mutation card, independently merge (e.g. two different add_signature
# ops), and the second write clobbers the first's addition (lost update).
# identity_mutation_lock(user_id) closes that whole span instead.
#
# Keyed by user_id (not by UserStore instance) so it holds even if callers
# construct/obtain separate UserStore objects for the same user. The guard
# lock only protects inserting a new per-user Lock into the dict — it is not
# held while the per-user lock itself is held.
#
# IMPORTANT — this is a PROCESS-local threading.Lock, not a cluster-wide
# one. deploy/docker-compose.yaml runs gunicorn with multiple worker
# PROCESSES; each has its OWN Python interpreter and therefore its OWN
# independent `_IDENTITY_MUTATION_LOCKS` dict — two workers handling the same
# user's writes concurrently each acquire their own uncontended lock and can
# still race at the DB. This lock only closes the race WITHIN one worker
# process (e.g. two threads in the same worker, or the thread-pooled ASGI
# server). The cross-worker case is closed at the DB layer instead — see
# `_save_identity_cas` / `IdentityWriteConflict` below and
# identity/actions.py::_with_identity_mutation_lock_and_retry, which wraps
# this lock AND the CAS retry together. Keep both: the lock is cheap
# same-process serialization (avoids paying for a CAS retry on the common
# case), the CAS is what's actually correct across workers.
_IDENTITY_MUTATION_LOCKS: dict[str, threading.Lock] = {}
_IDENTITY_MUTATION_LOCKS_GUARD = threading.Lock()


@contextmanager
def identity_mutation_lock(user_id: str):
    """Serialize identity profile mutations for a single user WITHIN this
    process. Must wrap the full read-existing-card -> merge -> re-encrypt ->
    save span (see identity/actions.py::_identity_profile_patch), not just
    the final save — see module comment above for why, and for why this
    alone is NOT sufficient across gunicorn worker processes."""
    with _IDENTITY_MUTATION_LOCKS_GUARD:
        lock = _IDENTITY_MUTATION_LOCKS.setdefault(user_id, threading.Lock())
    with lock:
        yield


class IdentityWriteConflict(Exception):
    """Raised by `_save_identity_cas` (via its caller) when the DB-level
    compare-and-swap loses a race to a concurrent writer — same user,
    potentially a DIFFERENT gunicorn worker process (identity_mutation_lock
    cannot prevent that, see above). Callers retry the whole
    read->merge->encrypt->write span from a fresh read; see
    identity/actions.py::_with_identity_mutation_lock_and_retry."""


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


def _save_identity_cas(store: UserStore, expected: dict, data: dict) -> bool:
    """Compare-and-swap write for the identity row: writes `data` ONLY if the
    row's current doc still equals `expected` — the exact blob the caller's
    merge (in identity/actions.py::_save_identity_action_payload) was
    computed from. This is the DB-level half of concurrency safety for
    identity.profile_patch / identity.dimension_nudge: it is what actually
    closes the cross-gunicorn-worker lost-update window that
    `identity_mutation_lock` (a process-local threading.Lock) cannot — see
    that function's docstring above.

    Built on `db.set_blob_if_unchanged`, which does the comparison as a
    single atomic `UPDATE ... WHERE doc = expected RETURNING` under the row
    lock (JSONB `=` is a semantic/normalized comparison, so key order and
    whitespace in `expected` don't matter) — no read-modify-write window at
    the DB layer either.

    Returns True iff the write landed. On success, ALSO marks the identity
    row for TEE-plaintext requeue, same as `_save_identity` above — this
    duplicates that one line rather than calling `_save_identity` (which
    would re-do the plain `db.set_blob`, defeating the CAS). This is required
    because `db.set_blob_if_unchanged` deliberately excludes "identity" from
    its own normal shadow-write mirroring (see db.py's `kind not in
    ("identity", "consumer_state")` guard) for the exact same reason
    `_save_identity` documents: identity's TEE mirror goes through the
    requeue lane, not a direct shadow write, because an in-place UPDATE keeps
    the same PK and the cursor-based replicator never revisits it."""
    if not db.set_blob_if_unchanged(store.user_id, "identity", expected, data):
        return False
    from tee_shadow import mirror
    mirror.mark_pending(store.user_id, "identity", "identity", "requeue")
    return True


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


# Canonical field lists now live in card_policy so the enclave's decrypt-and-serve
# route derives from the SAME list these write paths use. They were duplicated here
# by hand, and the copies drifted: the enclave forwarded only 9 of the 13, so every
# profile_patch silently erased the other 4 (custom_persona_prompt included).
# See card_policy.PROFILE_STRING_FIELDS for the full rationale.
_IDENTITY_PROFILE_STRING_FIELDS = card_policy.PROFILE_STRING_FIELDS
_IDENTITY_PROFILE_LIST_FIELDS = card_policy.PROFILE_LIST_FIELDS
_IDENTITY_PROFILE_FIELDS = set(card_policy.PROFILE_FIELDS)


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
