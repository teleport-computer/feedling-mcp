"""Self-contained DB access for Extended Perception.

Keeps all perception SQL inside the feature module (db.py is untouched). Reuses
the shared connection pool from db.get_pool(). Singletons live in user_blobs
(atomic shallow-merge upsert); collections live in the perception_items table
added by migration 0002; the wake/change audit trail uses user_logs.
"""
from __future__ import annotations

import logging
import os

from psycopg.types.json import Jsonb

from db import (
    get_pool, log_append, log_read, log_trim,
    frame_upsert, frame_get, frame_delete,
)

log = logging.getLogger("perception.store")

# user_blobs kinds (singletons, one row per user)
STATE = "perception_state"            # {field: {"v": .., "ts": ..}}
CONFIG = "perception_config"          # geofences / ssid_labels / ...
USER_STATE = "perception_user_state"  # {"manual": "default"}

EVENT_STREAM = "perception_events"
# Wake/change audit trail: one append per perception evaluation (wake /
# suppressed / debounced) — high frequency. Cap the stream so it can't grow
# without bound; kept above the dashboard's event read cap.
EVENT_MAX = int(os.environ.get("FEEDLING_PERCEPTION_EVENT_MAX", 2000))


# ---------------------------------------------------------------------------
# Singleton blobs (user_blobs)
# ---------------------------------------------------------------------------

def _get_blob(user_id: str, kind: str) -> dict:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc FROM user_blobs WHERE user_id = %s AND kind = %s",
                (user_id, kind),
            ).fetchone()
        return row[0] if row and isinstance(row[0], dict) else {}
    except Exception as e:
        log.error("get_blob(%s,%s) failed: %s", user_id, kind, e)
        return {}


def _merge_blob(user_id: str, kind: str, patch: dict) -> dict:
    """Atomic shallow-merge: existing doc || patch (patch wins per top-level key).
    Inserts the row if absent. Returns the merged doc."""
    if not patch:
        return _get_blob(user_id, kind)
    sql = ("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
           "ON CONFLICT (user_id, kind) DO UPDATE "
           "SET doc = user_blobs.doc || EXCLUDED.doc RETURNING doc")
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql, (user_id, kind, Jsonb(patch))).fetchone()
    except Exception as e:
        log.error("merge_blob(%s,%s) failed: %s", user_id, kind, e)
        return _get_blob(user_id, kind)
    from tee_shadow import mirror
    mirror.execute(sql, (user_id, kind, Jsonb(patch)))
    return row[0] if row else patch


def _set_blob(user_id: str, kind: str, doc: dict) -> None:
    sql = ("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
           "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc")
    try:
        with get_pool().connection() as conn:
            conn.execute(sql, (user_id, kind, Jsonb(doc)))
    except Exception as e:
        log.error("set_blob(%s,%s) failed: %s", user_id, kind, e)
        return
    from tee_shadow import mirror
    mirror.execute(sql, (user_id, kind, Jsonb(doc)))


def _delete_state_keys(user_id: str, keys: list[str]) -> None:
    """Remove specific top-level keys from perception_state (used when a
    capability is turned off — its fields must vanish from the snapshot)."""
    if not keys:
        return
    sql = ("UPDATE user_blobs SET doc = doc - %s::text[] "
           "WHERE user_id = %s AND kind = %s")
    try:
        with get_pool().connection() as conn:
            # doc - ARRAY[...]  removes each key from the JSONB object
            conn.execute(sql, (list(keys), user_id, STATE))
    except Exception as e:
        log.error("delete_state_keys(%s) failed: %s", user_id, e)
        return
    from tee_shadow import mirror
    mirror.execute(sql, (list(keys), user_id, STATE))


# typed accessors
def get_state(user_id: str) -> dict:
    return _get_blob(user_id, STATE)


def merge_state(user_id: str, patch: dict) -> dict:
    return _merge_blob(user_id, STATE, patch)


def merge_state_guarded(user_id: str, patch: dict) -> set:
    """Atomically merge per-field cells into perception_state under a ROW LOCK,
    applying a per-field timestamp guard: a field is written only if its new ts
    >= the currently-stored ts. Returns the set of field names actually written.

    The compare-and-write happens inside one locked transaction, so concurrent
    reports for the same user can't both read the old value and have the older
    one win — a late older record can never clobber a newer value."""
    if not patch:
        return set()
    written: set = set()
    insert_sql = ("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, '{}'::jsonb) "
                  "ON CONFLICT (user_id, kind) DO NOTHING")
    update_sql = "UPDATE user_blobs SET doc = %s WHERE user_id = %s AND kind = %s"
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                # Ensure the row exists, then lock it (FOR UPDATE serializes
                # concurrent writers on this user's state row).
                conn.execute(insert_sql, (user_id, STATE))
                row = conn.execute(
                    "SELECT doc FROM user_blobs WHERE user_id = %s AND kind = %s FOR UPDATE",
                    (user_id, STATE),
                ).fetchone()
                merged = dict(row[0]) if row and isinstance(row[0], dict) else {}
                for field, cell in patch.items():
                    existing = merged.get(field)
                    old_ts = existing.get("ts") if isinstance(existing, dict) else None
                    new_ts = cell.get("ts")
                    if old_ts is None or new_ts is None or float(new_ts) >= float(old_ts):
                        merged[field] = cell
                        written.add(field)
                conn.execute(update_sql, (Jsonb(merged), user_id, STATE))
    except Exception as e:
        log.error("merge_state_guarded(%s) failed: %s", user_id, e)
        return set()
    from tee_shadow import mirror
    mirror.execute_many([
        (insert_sql, (user_id, STATE)),
        (update_sql, (Jsonb(merged), user_id, STATE)),
    ])
    return written


def clear_state_fields(user_id: str, fields: list[str]) -> None:
    _delete_state_keys(user_id, fields)


def get_config(user_id: str) -> dict:
    return _get_blob(user_id, CONFIG)


def merge_config(user_id: str, patch: dict) -> dict:
    return _merge_blob(user_id, CONFIG, patch)


def get_user_state_doc(user_id: str) -> dict:
    return _get_blob(user_id, USER_STATE)


def set_user_state_doc(user_id: str, doc: dict) -> None:
    _set_blob(user_id, USER_STATE, doc)


def set_manual_user_state_guarded(user_id: str, value: str, ts: float) -> dict:
    """Atomically set the manual user_state under a ROW LOCK with a ts guard: the
    write applies only if `ts` >= the stored manual_ts. Returns the resulting
    user_state doc (whether or not this write won). Mirrors merge_state_guarded's
    locked compare-and-write so a late/concurrent older report can't clobber a
    newer manual value — the read-check-write is one serialized transaction."""
    insert_sql = ("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, '{}'::jsonb) "
                  "ON CONFLICT (user_id, kind) DO NOTHING")
    update_sql = "UPDATE user_blobs SET doc = %s WHERE user_id = %s AND kind = %s"
    won = False
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                conn.execute(insert_sql, (user_id, USER_STATE))
                row = conn.execute(
                    "SELECT doc FROM user_blobs WHERE user_id = %s AND kind = %s FOR UPDATE",
                    (user_id, USER_STATE),
                ).fetchone()
                doc = dict(row[0]) if row and isinstance(row[0], dict) else {}
                prev_ts = doc.get("manual_ts")
                if prev_ts is None or float(ts) >= float(prev_ts):
                    doc["manual"] = str(value or "default")
                    doc["manual_ts"] = float(ts)
                    conn.execute(update_sql, (Jsonb(doc), user_id, USER_STATE))
                    won = True
    except Exception as e:
        log.error("set_manual_user_state_guarded(%s) failed: %s", user_id, e)
        return _get_blob(user_id, USER_STATE)
    if won:
        # Mirror the final computed doc directly (not the guarded read-check-
        # write), avoiding any TOCTOU divergence from re-deriving the guard
        # against TEE's own (possibly lagging) copy of the row.
        from tee_shadow import mirror
        mirror.execute_many([
            (insert_sql, (user_id, USER_STATE)),
            (update_sql, (Jsonb(doc), user_id, USER_STATE)),
        ])
    return doc


# ---------------------------------------------------------------------------
# Collections (perception_items)
# ---------------------------------------------------------------------------

def item_upsert(user_id: str, kind: str, item_id: str, ts: float,
                doc: dict, expires_at: float | None = None) -> None:
    sql = ("INSERT INTO perception_items (user_id, kind, item_id, ts, expires_at, doc) "
           "VALUES (%s, %s, %s, %s, %s, %s) "
           "ON CONFLICT (user_id, kind, item_id) DO UPDATE "
           "SET ts = EXCLUDED.ts, expires_at = EXCLUDED.expires_at, doc = EXCLUDED.doc")
    try:
        with get_pool().connection() as conn:
            conn.execute(sql, (user_id, kind, item_id, ts, expires_at, Jsonb(doc)))
    except Exception as e:
        log.error("item_upsert(%s,%s,%s) failed: %s", user_id, kind, item_id, e)
        return
    from tee_shadow import mirror
    mirror.execute(sql, (user_id, kind, item_id, ts, expires_at, Jsonb(doc)))


def item_get(user_id: str, kind: str, item_id: str, now: float | None = None) -> dict | None:
    """Fetch one item. If `now` is given, expired items (expires_at <= now) are
    treated as absent."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc, expires_at FROM perception_items "
                "WHERE user_id = %s AND kind = %s AND item_id = %s",
                (user_id, kind, item_id),
            ).fetchone()
        if not row:
            return None
        doc, expires_at = row[0], row[1]
        if now is not None and expires_at is not None and expires_at <= now:
            return None
        return doc
    except Exception as e:
        log.error("item_get(%s,%s,%s) failed: %s", user_id, kind, item_id, e)
        return None


def item_list(user_id: str, kind: str, limit: int = 20,
              now: float | None = None) -> list[dict]:
    """Newest-first list of a kind, skipping expired rows."""
    try:
        sql = ("SELECT doc FROM perception_items "
               "WHERE user_id = %s AND kind = %s")
        params: list = [user_id, kind]
        if now is not None:
            sql += " AND (expires_at IS NULL OR expires_at > %s)"
            params.append(now)
        sql += " ORDER BY ts DESC LIMIT %s"
        params.append(max(1, min(limit, 200)))
        with get_pool().connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("item_list(%s,%s) failed: %s", user_id, kind, e)
        return []


def item_patch(user_id: str, kind: str, item_id: str, patch: dict,
               expires_at: float | None = "__keep__") -> dict | None:
    """Shallow-merge `patch` into an item's doc. Optionally also set expires_at
    (pass None to clear the TTL / promote a staged item; omit to keep current)."""
    if expires_at == "__keep__":
        sql = ("UPDATE perception_items SET doc = doc || %s "
               "WHERE user_id = %s AND kind = %s AND item_id = %s RETURNING doc")
        params = (Jsonb(patch), user_id, kind, item_id)
    else:
        sql = ("UPDATE perception_items SET doc = doc || %s, expires_at = %s "
               "WHERE user_id = %s AND kind = %s AND item_id = %s RETURNING doc")
        params = (Jsonb(patch), expires_at, user_id, kind, item_id)
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql, params).fetchone()
    except Exception as e:
        log.error("item_patch(%s,%s,%s) failed: %s", user_id, kind, item_id, e)
        return None
    from tee_shadow import mirror
    mirror.execute(sql, params)
    return row[0] if row else None


def item_delete(user_id: str, kind: str, item_id: str) -> bool:
    sql = "DELETE FROM perception_items WHERE user_id = %s AND kind = %s AND item_id = %s"
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (user_id, kind, item_id))
    except Exception as e:
        log.error("item_delete(%s,%s,%s) failed: %s", user_id, kind, item_id, e)
        return False
    from tee_shadow import mirror
    mirror.execute(sql, (user_id, kind, item_id))
    return cur.rowcount > 0


def prune_expired(now: float) -> int:
    """Delete all expired items across users (lazy GC; safe to call anytime)."""
    sql = "DELETE FROM perception_items WHERE expires_at IS NOT NULL AND expires_at <= %s"
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (now,))
    except Exception as e:
        log.error("prune_expired failed: %s", e)
        return 0
    from tee_shadow import mirror
    mirror.execute(sql, (now,))
    return cur.rowcount or 0


def latest_ts(user_id: str, kind: str) -> float | None:
    """Most-recent ts for a kind (used for photo burst clustering)."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT MAX(ts) FROM perception_items WHERE user_id = %s AND kind = %s",
                (user_id, kind),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("latest_ts(%s,%s) failed: %s", user_id, kind, e)
        return None


# ---------------------------------------------------------------------------
# Event / wake audit trail (user_logs)
# ---------------------------------------------------------------------------

def append_event(user_id: str, event: dict, ts: float) -> None:
    log_append(user_id, EVENT_STREAM, event, ts=ts)
    log_trim(user_id, EVENT_STREAM, EVENT_MAX)


def read_events(user_id: str, limit: int = 50) -> list[dict]:
    return log_read(user_id, EVENT_STREAM, limit=limit)


# App-usage time series (one append per app-open from the iOS Shortcut endpoint).
APP_USAGE_STREAM = "app_usage"
APP_USAGE_MAX = int(os.environ.get("FEEDLING_APP_USAGE_MAX", 2000))


def append_app_open(user_id: str, doc: dict, ts: float) -> None:
    log_append(user_id, APP_USAGE_STREAM, doc, ts=ts)
    log_trim(user_id, APP_USAGE_STREAM, APP_USAGE_MAX)


def read_app_opens(user_id: str, limit: int = 100, since_epoch: float = 0.0) -> list[dict]:
    return log_read(user_id, APP_USAGE_STREAM, limit=limit, since_epoch=since_epoch)


# ---------------------------------------------------------------------------
# Photo ciphertext — reuses the screen-frame envelope channel.
# ---------------------------------------------------------------------------
# Confirmed photo envelopes live in the existing frame_envelopes table (keyed by
# the envelope id == photo_id). This keeps the big base64 ciphertext out of the
# perception_items JSONB AND makes photos decryptable through the enclave's
# existing /v1/screen/frames/<id>/decrypt path with zero new crypto code. The
# screen-frame *list* (frames_meta, a separate index blob) is left untouched, so
# photos never appear among screen frames.

def put_photo_envelope(user_id: str, frame_id: str, ts: float, env: dict) -> None:
    frame_upsert(user_id, frame_id, ts, env)


def get_photo_envelope(user_id: str, frame_id: str) -> dict | None:
    return frame_get(user_id, frame_id)


def delete_photo_envelope(user_id: str, frame_id: str) -> None:
    frame_delete(user_id, frame_id)


# ---------------------------------------------------------------------------
# Quantitative history (perception_daily) — Tier 2 daily rollups.
# ---------------------------------------------------------------------------

def merge_perception_daily(user_id: str, date: str, signal: str, merge_fn, ts: float) -> dict:
    """Read-modify-write one (user, local-date, signal) rollup under a row lock:
    load the running day-doc, hand it to ``merge_fn(prev_doc) -> new_doc`` (the
    field-agnostic incremental aggregator), and persist. The lock serializes the
    30s/5min report churn so concurrent reports can't lose increments.
    Returns the new doc (or {} on failure)."""
    insert_sql = ("INSERT INTO perception_daily (user_id, date, signal, doc, updated_at) "
                  "VALUES (%s, %s, %s, '{}'::jsonb, %s) "
                  "ON CONFLICT (user_id, date, signal) DO NOTHING")
    update_sql = ("UPDATE perception_daily SET doc = %s, updated_at = %s "
                  "WHERE user_id = %s AND date = %s AND signal = %s")
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                conn.execute(insert_sql, (user_id, date, signal, ts))
                row = conn.execute(
                    "SELECT doc FROM perception_daily "
                    "WHERE user_id = %s AND date = %s AND signal = %s FOR UPDATE",
                    (user_id, date, signal),
                ).fetchone()
                prev = row[0] if row and isinstance(row[0], dict) else {}
                new_doc = merge_fn(prev) or {}
                conn.execute(update_sql, (Jsonb(new_doc), ts, user_id, date, signal))
    except Exception as e:
        log.error("merge_perception_daily(%s,%s,%s) failed: %s", user_id, date, signal, e)
        return {}
    # Mirror the final computed doc directly (same rationale as
    # merge_state_guarded) rather than re-deriving merge_fn's incremental
    # aggregation against TEE's own (possibly lagging) prior doc.
    from tee_shadow import mirror
    mirror.execute_many([
        (insert_sql, (user_id, date, signal, ts)),
        (update_sql, (Jsonb(new_doc), ts, user_id, date, signal)),
    ])
    return new_doc


def list_perception_daily(user_id: str, signal: str, days: int = 30) -> list[dict]:
    """Most-recent ``days`` rollups for a signal, ascending by date:
    [{"date": "YYYY-MM-DD", "doc": {...}}]."""
    limit = max(1, min(int(days), 400))
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT date, doc FROM perception_daily "
                "WHERE user_id = %s AND signal = %s ORDER BY date DESC LIMIT %s",
                (user_id, signal, limit),
            ).fetchall()
        out = [{"date": r[0], "doc": r[1] if isinstance(r[1], dict) else {}} for r in rows]
        out.reverse()  # ascending for trend math
        return out
    except Exception as e:
        log.error("list_perception_daily(%s,%s) failed: %s", user_id, signal, e)
        return []
