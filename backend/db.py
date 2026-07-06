"""PostgreSQL persistence layer for the Feedling backend.

This module replaces the previous local-file persistence (JSON / JSONL files
under FEEDLING_DATA_DIR). The in-memory model in app.py is unchanged: per-user
``UserStore`` instances still hold their state in memory behind their own
``threading.Lock``s — this module only swaps where that state is read from /
written to.

Crypto note: the server never decrypts. Every encrypted payload (chat / memory
/ identity / frame envelopes) is an opaque ``body_ct`` / ``nonce`` / ``K_user``
/ ``K_enclave`` set of base64 strings plus plaintext metadata. Those fields are
stored verbatim as JSONB and returned byte-for-byte, so the enclave's decrypt
path is unaffected.

Concurrency: ``-w N`` workers, ``--threads 32`` each in production compose. Each
worker has its own ``psycopg_pool.ConnectionPool`` (max_size=16) shared across
its threads, plus one pool-external connection for the LISTEN wake bus (see
``listen_connection`` / ``pg_notify`` and ``core/wake_bus.py``). The long-poll
endpoints block on in-memory ``threading.Event``s, NOT on a held DB connection,
so they don't starve the pool; cross-worker wakes ride the NOTIFY channel.

Durability parity: like the old file savers, write helpers swallow-and-log on
failure (logged at error level) rather than raising, to keep request-path
behavior identical to the file era. Read helpers return empty/None on failure.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

import object_storage  # lowest-layer peer: R2 offload for frame body_ct

log = logging.getLogger("feedling.db")

# ---------------------------------------------------------------------------
# Connection pool (lazy: opened on first use so importing this module without a
# DATABASE_URL — e.g. tooling — doesn't crash at import time).
# ---------------------------------------------------------------------------

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. The backend now persists to PostgreSQL; "
            "set DATABASE_URL (must include sslmode=require for external PG)."
        )
    return url


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                _database_url(),
                min_size=2,
                max_size=16,
                timeout=10,
                max_idle=300,
                kwargs={"autocommit": True},
                open=True,
            )
    return _pool


# ---------------------------------------------------------------------------
# Schema — managed by Alembic (single source of truth).
# Migrations live in backend/alembic/versions/. To change the schema, add a new
# revision (`alembic revision -m "..."`) rather than editing DDL here.
# ---------------------------------------------------------------------------

_schema_lock = threading.Lock()


def init_schema() -> None:
    """Bring the database schema up to the latest Alembic revision.

    Runs ``alembic upgrade head`` programmatically, reading DATABASE_URL via
    backend/alembic/env.py. The baseline revision's DDL is idempotent, so this
    is safe on the already-provisioned production database (it just records the
    version). Called at app startup, by the migrate container, and by tests.
    """
    from alembic import command
    from alembic.config import Config

    here = Path(__file__).resolve().parent
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here / "alembic"))
    with _schema_lock:
        command.upgrade(cfg, "head")
    log.info("[db] schema at head (alembic upgrade)")


def healthcheck() -> bool:
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception as e:
        log.error("[db] healthcheck failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# LISTEN/NOTIFY (cross-worker wake bus — see core/wake_bus.py)
#
# These are the only DB-layer primitives for the wake bus; the protocol /
# payload / dispatch lives in core/wake_bus.py (db.py stays free of business
# deps). pg_notify() borrows a pooled connection for a fire-and-forget signal;
# listen_connection() hands out a dedicated, pool-external autocommit
# connection that one daemon thread per worker holds open and blocks on.
# ---------------------------------------------------------------------------


def pg_notify(channel: str, payload: str) -> None:
    """Fire a Postgres NOTIFY on ``channel``. Swallow-and-log on failure to keep
    request-path behavior identical to the file era (a missed wake degrades to
    the long-poll timeout / cache TTL, never a 500)."""
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))
    except Exception as e:
        log.error("[db] pg_notify(%s) failed: %s", channel, e)


def listen_connection() -> "psycopg.Connection":
    """A dedicated, pool-external autocommit connection for LISTEN. The wake bus
    holds exactly one of these per worker, outside the request pool, and blocks
    on ``conn.notifies()`` — so it never consumes a pool slot. Raises on connect
    failure; the caller's reconnect loop handles it."""
    return psycopg.connect(_database_url(), autocommit=True)


# ---------------------------------------------------------------------------
# server_config (pepper, etc.)
# ---------------------------------------------------------------------------


def get_config(key: str) -> bytes | None:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT value FROM server_config WHERE key = %s", (key,)
            ).fetchone()
        if row is None:
            return None
        val = row[0]
        # psycopg returns BYTEA as a memoryview; normalize to bytes.
        return bytes(val)
    except Exception as e:
        log.error("[db] get_config(%s) failed: %s", key, e)
        return None


def set_config_if_absent(key: str, value: bytes) -> bytes:
    """Insert (key, value) only if the key is absent, then return the stored
    value. This makes pepper bootstrap race-safe across concurrent workers:
    the first writer wins and everyone reads back the same pepper.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO server_config (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, value),
            )
            row = conn.execute(
                "SELECT value FROM server_config WHERE key = %s", (key,)
            ).fetchone()
    return bytes(row[0])


def set_config(key: str, value: bytes) -> None:
    """Unconditional upsert. Used by the migration script."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO server_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


# The agent-runner supervisor heartbeats here each tick; the backend's
# /v1/model_api/chat/send wedge guard reads it to confirm a supervisor is
# actually hosting before routing a turn into the agent-runner (else the turn
# would park in "processing" with no consumer to answer it).
AGENT_RUNTIME_SUPERVISOR_HEARTBEAT_KEY = "agent_runtime_supervisor_heartbeat"


def set_supervisor_heartbeat(payload: dict) -> None:
    """Upsert the supervisor's global heartbeat (JSON in server_config)."""
    set_config(AGENT_RUNTIME_SUPERVISOR_HEARTBEAT_KEY,
               json.dumps(payload).encode("utf-8"))


def read_supervisor_heartbeat() -> dict | None:
    """Return the parsed supervisor heartbeat, or None when the row is absent or
    malformed. Raises on a DB/connection error so the caller can fail-open rather
    than mistake an outage for "no supervisor" (which would 503 every send)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT value FROM server_config WHERE key = %s",
            (AGENT_RUNTIME_SUPERVISOR_HEARTBEAT_KEY,),
        ).fetchone()
    if row is None:
        return None
    try:
        obj = json.loads(bytes(row[0]))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


# Per-owner supervisor heartbeats (migration 0009). Unlike the single global key
# above, each runner writes its OWN row keyed by ``owner`` ("<host>:<pid>"), so
# multiple runners don't clobber one another. The backend's wedge guard lists
# these and treats the cluster as live iff any fresh row is actually hosting.
# Liveness alone is in the lease table; this row additionally carries the
# cluster-capability flags (host_all/gateway) + shard/capacity config.

def set_supervisor_instance_heartbeat(owner: str, payload: dict) -> None:
    """Upsert this runner's heartbeat row. ``payload`` is the rich heartbeat dict;
    the typed columns are projected out of it for cheap aggregation, and the full
    dict is also stored as JSONB for diagnostics. ``updated_at`` is stamped now()."""
    def _i(key, default=0):
        try:
            return int(payload.get(key, default))
        except (TypeError, ValueError):
            return default
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_runtime_supervisor_heartbeats "
            "(owner, host, shard_index, shard_count, max_children, active_children, "
            " host_all, gateway, version, payload, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (owner) DO UPDATE SET "
            "  host = EXCLUDED.host, shard_index = EXCLUDED.shard_index, "
            "  shard_count = EXCLUDED.shard_count, max_children = EXCLUDED.max_children, "
            "  active_children = EXCLUDED.active_children, host_all = EXCLUDED.host_all, "
            "  gateway = EXCLUDED.gateway, version = EXCLUDED.version, "
            "  payload = EXCLUDED.payload, updated_at = now()",
            (
                str(owner),
                payload.get("host"),
                _i("shard_index", 0),
                _i("shard_count", 1),
                _i("max_children", 0),
                _i("active_children", 0),
                bool(payload.get("host_all")),
                bool(payload.get("gateway")),
                payload.get("version"),
                json.dumps(payload),
            ),
        )


def list_supervisor_instance_heartbeats() -> list[dict]:
    """All runner heartbeat rows. Each dict carries the typed flags plus ``ts``
    (the row's ``updated_at`` as an epoch float) so the caller can age-filter in
    pure code. Freshness/aggregation is the guard's job, not this query's. Raises
    on a DB error so the caller can fall back to the legacy key."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT owner, host, shard_index, shard_count, max_children, "
            "       active_children, host_all, gateway, version, "
            "       extract(epoch FROM updated_at) AS ts "
            "FROM agent_runtime_supervisor_heartbeats"
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "owner": r[0], "host": r[1], "shard_index": r[2], "shard_count": r[3],
            "max_children": r[4], "active_children": r[5],
            "host_all": bool(r[6]), "gateway": bool(r[7]), "version": r[8],
            "ts": float(r[9]),
        })
    return out


def prune_supervisor_instance_heartbeats(max_age_sec: float) -> None:
    """Delete heartbeat rows older than ``max_age_sec`` (dead runners that never
    released). Best-effort housekeeping so the table doesn't accrete forever."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM agent_runtime_supervisor_heartbeats "
            "WHERE updated_at < now() - make_interval(secs => %s)",
            (float(max_age_sec),),
        )


# ---------------------------------------------------------------------------
# Global (non-per-user) JSON documents
# ---------------------------------------------------------------------------


def get_global_blob(key: str):
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc FROM global_blobs WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row is not None else None
    except Exception as e:
        log.error("[db] get_global_blob(%s) failed: %s", key, e)
        return None


def set_global_blob(key: str, doc) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO global_blobs (key, doc) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET doc = EXCLUDED.doc",
                (key, Jsonb(doc)),
            )
    except Exception as e:
        log.error("[db] set_global_blob(%s) failed: %s", key, e)


# ---------------------------------------------------------------------------
# users registry
# ---------------------------------------------------------------------------

def load_all_users() -> list[dict]:
    """Return the full user registry as a list of dicts (each the verbatim
    stored user document), ordered by created_at."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM users ORDER BY created_at NULLS FIRST, user_id"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] load_all_users failed: %s", e)
        return []


def insert_user(entry: dict) -> None:
    """Insert one user document. ON CONFLICT DO NOTHING so the migration is
    idempotent and a re-registration race can't duplicate a user_id."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            (entry["user_id"], entry.get("created_at"), Jsonb(entry)),
        )


def upsert_user(entry: dict) -> None:
    """Insert-or-update one user document from the in-memory user dict (the
    source of truth after the caller mutates it under _users_lock)."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET created_at = EXCLUDED.created_at, doc = EXCLUDED.doc",
            (entry["user_id"], entry.get("created_at"), Jsonb(entry)),
        )


def save_all_users(users: list[dict]) -> None:
    """Persist the whole in-memory user list. The app calls this (via
    _save_users) for full-list rewrites — startup normalization and test resets.

    Upsert each snapshot user + delete ONLY users absent from the snapshot (genuine
    removals). It deliberately does NOT ``DELETE FROM users`` wholesale: under the
    per-user ``ON DELETE CASCADE`` FKs (0011) a blanket delete would cascade-wipe
    every KEPT user's chat/memory/frames/logs/blobs/imports before the reinsert —
    the reinsert restores the ``users`` row but not the cascaded child rows. So
    kept users are upserted in place (their child rows untouched); a user in the DB
    but not in this snapshot is truly removed and its data cascade-deleted.

    NOTE: still destructive from THIS worker's snapshot — under ``-w N`` it must not
    be used for ordinary per-user edits (a stale snapshot missing a user another
    worker just created would delete that user + cascade its data). Genuine
    single-user edits go through ``registry.persist_user`` → ``db.upsert_user``
    (per-row, non-destructive); the remaining callers here read-then-rewrite their
    own full snapshot or run pre-fork at startup."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                keep_ids = [str(e.get("user_id")) for e in users if e.get("user_id")]
                # Remove only genuinely-absent users (empty snapshot ⇒ remove all).
                if keep_ids:
                    conn.execute(
                        "DELETE FROM users WHERE NOT (user_id = ANY(%s))", (keep_ids,)
                    )
                else:
                    conn.execute("DELETE FROM users")
                for entry in users:
                    uid = entry.get("user_id")
                    if not uid:
                        continue
                    # Upsert (not plain INSERT): kept rows still exist, so a plain
                    # INSERT would hit the users PK. Upsert leaves child rows intact.
                    conn.execute(
                        "INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s) "
                        "ON CONFLICT (user_id) DO UPDATE SET "
                        "created_at = EXCLUDED.created_at, doc = EXCLUDED.doc",
                        (uid, entry.get("created_at"), Jsonb(entry)),
                    )
    except Exception as e:
        log.error("[db] save_all_users failed: %s", e)


def delete_user(user_id: str) -> None:
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))


def user_exists(user_id: str) -> bool:
    """Authoritative membership check against the users table. The push path uses
    it to close the sub-second window where another worker committed a delete but
    THIS worker's in-memory registry hasn't processed the ``users`` wake-bus
    reload yet — the stale snapshot would otherwise pass the guard and send a push
    to a just-deleted account. One indexed PK lookup; negligible next to the store
    load / chat work a push already does."""
    if not user_id:
        return False
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = %s LIMIT 1", (user_id,)
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Admin/data-track aggregate reads
# ---------------------------------------------------------------------------


def admin_data_track_snapshot(user_ids: list[str]) -> dict[str, dict]:
    """Return metadata-only aggregate stats for a set of users.

    This is deliberately SQL-aggregate based: admin dashboards must not pull
    full encrypted chat envelopes or memory bodies into Python just to count
    them. The returned shape is consumed by app.py's data-track surface.
    """
    ids = [str(uid) for uid in user_ids if uid]
    if not ids:
        return {}

    def ensure(out: dict[str, dict], uid: str) -> dict:
        return out.setdefault(uid, {})

    out: dict[str, dict] = {uid: {} for uid in ids}
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id,
                       COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE doc->>'role' = 'user')::int AS user_messages,
                       COUNT(*) FILTER (WHERE doc->>'role' IN ('agent', 'openclaw'))::int AS agent_messages,
                       COUNT(*) FILTER (WHERE doc->>'content_type' = 'image')::int AS image_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'agent_initiated_proactive')::int AS proactive_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'model_api' AND doc->>'role' = 'user')::int AS model_api_user_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'model_api' AND doc->>'role' IN ('agent', 'openclaw'))::int AS model_api_agent_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'model_api' AND doc->>'model_api_kind' = 'onboarding_greeting')::int AS model_api_greetings,
                       MIN(ts) AS first_ts,
                       MAX(ts) AS last_ts,
                       MAX(ts) FILTER (WHERE doc->>'source' = 'agent_initiated_proactive') AS proactive_last_ts,
                       MAX(ts) FILTER (WHERE doc->>'role' = 'user') AS last_user_ts,
                       MAX(ts) FILTER (WHERE doc->>'role' IN ('agent', 'openclaw')) AS last_agent_ts
                FROM chat_messages
                WHERE user_id = ANY(%s)
                GROUP BY user_id
                """,
                (ids,),
            ).fetchall()
            for row in rows:
                uid = row[0]
                ensure(out, uid)["chat"] = {
                    "total": row[1],
                    "user_messages": row[2],
                    "agent_messages": row[3],
                    "image_messages": row[4],
                    "proactive_messages": row[5],
                    "model_api_user_messages": row[6],
                    "model_api_agent_messages": row[7],
                    "model_api_greetings": row[8],
                    "first_ts": row[9],
                    "last_ts": row[10],
                    "proactive_last_ts": row[11],
                    "last_user_ts": row[12],
                    "last_agent_ts": row[13],
                    "by_role": {},
                    "by_source": {},
                    "by_content_type": {},
                }

            for field, target in (
                ("role", "by_role"),
                ("source", "by_source"),
                ("content_type", "by_content_type"),
            ):
                rows = conn.execute(
                    f"""
                    SELECT user_id, COALESCE(NULLIF(doc->>%s, ''), 'unknown') AS value,
                           COUNT(*)::int
                    FROM chat_messages
                    WHERE user_id = ANY(%s)
                    GROUP BY user_id, value
                    """,
                    (field, ids),
                ).fetchall()
                for uid, value, count in rows:
                    chat = ensure(out, uid).setdefault("chat", {})
                    chat.setdefault(target, {})[value] = count

            rows = conn.execute(
                """
                SELECT user_id,
                       COUNT(*)::int AS total,
                       MIN(NULLIF(doc->>'created_at', '')) AS first_created_at,
                       MAX(NULLIF(doc->>'created_at', '')) AS last_created_at,
                       MIN(NULLIF(doc->>'occurred_at', '')) AS earliest_occurred_at,
                       MAX(NULLIF(doc->>'occurred_at', '')) AS latest_occurred_at
                FROM memory_moments
                WHERE user_id = ANY(%s)
                GROUP BY user_id
                """,
                (ids,),
            ).fetchall()
            for row in rows:
                ensure(out, row[0])["memory"] = {
                    "total": row[1],
                    "by_type": {},
                    "by_source": {},
                    "first_created_at": row[2] or "",
                    "last_created_at": row[3] or "",
                    "earliest_occurred_at": row[4] or "",
                    "latest_occurred_at": row[5] or "",
                }

            for field, target in (("type", "by_type"), ("source", "by_source")):
                rows = conn.execute(
                    f"""
                    SELECT user_id, COALESCE(NULLIF(doc->>%s, ''), 'unknown') AS value,
                           COUNT(*)::int
                    FROM memory_moments
                    WHERE user_id = ANY(%s)
                    GROUP BY user_id, value
                    """,
                    (field, ids),
                ).fetchall()
                for uid, value, count in rows:
                    memory = ensure(out, uid).setdefault("memory", {})
                    memory.setdefault(target, {})[value] = count

            rows = conn.execute(
                """
                SELECT user_id, stream, COUNT(*)::int, MAX(ts)
                FROM user_logs
                WHERE user_id = ANY(%s)
                  AND stream IN (
                    'memory_changes', 'memory_capture_jobs', 'gate_decisions',
                    'proactive_jobs', 'device_events', 'tracking_events',
                    'bootstrap_events'
                  )
                GROUP BY user_id, stream
                """,
                (ids,),
            ).fetchall()
            for uid, stream, count, max_ts in rows:
                ensure(out, uid).setdefault("logs", {})[stream] = {
                    "count": count,
                    "last_ts": max_ts,
                }

            rows = conn.execute(
                """
                SELECT user_id,
                       COUNT(*)::int AS decisions,
                       COUNT(*) FILTER (
                         WHERE LOWER(COALESCE(doc->>'should_reach_out', '')) IN ('true', '1', 'yes')
                       )::int AS decision_true
                FROM user_logs
                WHERE user_id = ANY(%s) AND stream = 'gate_decisions'
                GROUP BY user_id
                """,
                (ids,),
            ).fetchall()
            for uid, decisions, decision_true in rows:
                ensure(out, uid).setdefault("proactive_extra", {}).update({
                    "decisions": decisions,
                    "decision_true": decision_true,
                })

            rows = conn.execute(
                """
                SELECT user_id, COALESCE(NULLIF(doc->>'status', ''), 'unknown') AS status,
                       COUNT(*)::int
                FROM user_logs
                WHERE user_id = ANY(%s) AND stream = 'proactive_jobs'
                GROUP BY user_id, status
                """,
                (ids,),
            ).fetchall()
            for uid, status, count in rows:
                ensure(out, uid).setdefault("proactive_extra", {}).setdefault("jobs_by_status", {})[status] = count

            # Split proactive jobs by lane (heartbeat vs screen-share vs other).
            # The persisted job doc carries job_kind / wake_kind / trigger; group
            # by the first non-empty of those and let the caller bucket the raw
            # kind strings (data_track._classify_proactive_kind).
            rows = conn.execute(
                """
                SELECT user_id,
                       COALESCE(
                         NULLIF(doc->>'job_kind', ''),
                         NULLIF(doc->>'wake_kind', ''),
                         NULLIF(doc->>'trigger', ''),
                         'unknown'
                       ) AS kind,
                       COUNT(*)::int AS total,
                       (COUNT(*) FILTER (
                          WHERE doc->>'status' IN ('failed', 'skipped')))::int AS failed
                FROM user_logs
                WHERE user_id = ANY(%s) AND stream = 'proactive_jobs'
                GROUP BY user_id, kind
                """,
                (ids,),
            ).fetchall()
            for uid, kind, total, failed in rows:
                pex = ensure(out, uid).setdefault("proactive_extra", {})
                pex.setdefault("jobs_by_kind", {})[kind] = total
                pex.setdefault("jobs_failed_by_kind", {})[kind] = failed

            rows = conn.execute(
                """
                SELECT user_id,
                       COALESCE(NULLIF(doc->>'live_activity_status', ''), 'unknown') AS live_status,
                       COUNT(*)::int
                FROM chat_messages
                WHERE user_id = ANY(%s) AND doc->>'source' = 'agent_initiated_proactive'
                GROUP BY user_id, live_status
                """,
                (ids,),
            ).fetchall()
            for uid, status, count in rows:
                ensure(out, uid).setdefault("proactive_extra", {}).setdefault("live_activity_status", {})[status] = count

            rows = conn.execute(
                """
                SELECT user_id,
                       COALESCE(NULLIF(doc->>'alert_status', ''), 'unknown') AS alert_status,
                       COUNT(*)::int
                FROM chat_messages
                WHERE user_id = ANY(%s) AND doc->>'source' = 'agent_initiated_proactive'
                GROUP BY user_id, alert_status
                """,
                (ids,),
            ).fetchall()
            for uid, status, count in rows:
                ensure(out, uid).setdefault("proactive_extra", {}).setdefault("alert_status", {})[status] = count

            rows = conn.execute(
                """
                SELECT user_id,
                       COUNT(*)::int AS capture_jobs,
                       COALESCE(SUM(NULLIF(doc->>'actions_written', '')::int), 0)::int AS actions_written,
                       MAX(ts) AS last_capture_ts
                FROM user_logs
                WHERE user_id = ANY(%s) AND stream = 'memory_capture_jobs'
                GROUP BY user_id
                """,
                (ids,),
            ).fetchall()
            for uid, capture_jobs, actions_written, last_ts in rows:
                ensure(out, uid).setdefault("memory_extra", {}).update({
                    "capture_jobs": capture_jobs,
                    "capture_actions_written": actions_written,
                    "last_capture_ts": last_ts,
                })

            for stream, field, out_key in (
                ("memory_changes", "action", "changes_by_action"),
                ("memory_changes", "capture_mode", "changes_by_capture_mode"),
                ("memory_capture_jobs", "status", "capture_jobs_by_status"),
                ("memory_capture_jobs", "mode", "capture_jobs_by_mode"),
                ("tracking_events", "type", "tracking_by_type"),
                ("bootstrap_events", "event_type", "bootstrap_by_type"),
            ):
                rows = conn.execute(
                    """
                    SELECT user_id, COALESCE(NULLIF(doc->>%s, ''), 'unknown') AS value,
                           COUNT(*)::int
                    FROM user_logs
                    WHERE user_id = ANY(%s) AND stream = %s
                    GROUP BY user_id, value
                    """,
                    (field, ids, stream),
                ).fetchall()
                for uid, value, count in rows:
                    ensure(out, uid).setdefault("log_counts", {}).setdefault(out_key, {})[value] = count

            rows = conn.execute(
                """
                SELECT user_id, kind, doc
                FROM user_blobs
                WHERE user_id = ANY(%s)
                  AND kind IN ('onboarding_route', 'identity', 'model_api', 'model_api_runtime', 'consumer_state')
                """,
                (ids,),
            ).fetchall()
            for uid, kind, doc in rows:
                ensure(out, uid).setdefault("blobs", {})[kind] = doc

            rows = conn.execute(
                """
                SELECT DISTINCT ON (user_id) user_id, doc
                FROM user_blobs
                WHERE user_id = ANY(%s) AND kind LIKE 'history_import_job:%%'
                ORDER BY user_id, COALESCE(doc->>'updated_at', doc->>'created_at', '') DESC
                """,
                (ids,),
            ).fetchall()
            for uid, doc in rows:
                ensure(out, uid)["history_import"] = doc
    except Exception as e:
        log.error("[db] admin_data_track_snapshot failed: %s", e)
    return out


def admin_data_track_dau(*, since_epoch: float = 0.0, days: int = 30, tz: str = "Asia/Shanghai") -> list[dict]:
    """Return metadata-only daily active user aggregates.

    DAU is intentionally user-initiated activity only: user chat messages plus
    client tracking events. Agent replies, proactive writes, and synthetic
    verify pings are excluded so automated reply loops cannot inflate activity.
    """
    day_limit = max(1, min(int(days or 30), 366))
    since = float(since_epoch or 0.0)
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                WITH active AS (
                    SELECT user_id, ts, 'chat' AS source
                    FROM chat_messages
                    WHERE doc->>'role' = 'user'
                      AND COALESCE(doc->>'source', '') <> 'verify_ping'
                      AND (%s = 0 OR ts >= %s)

                    UNION ALL

                    SELECT user_id, ts, 'tracking' AS source
                    FROM user_logs
                    WHERE stream = 'tracking_events'
                      AND ts IS NOT NULL
                      AND (%s = 0 OR ts >= %s)
                ),
                daily AS (
                    SELECT
                        to_char(timezone(%s, to_timestamp(ts)), 'YYYY-MM-DD') AS day,
                        COUNT(DISTINCT user_id)::int AS dau,
                        (COUNT(DISTINCT user_id) FILTER (WHERE source = 'chat'))::int AS chat_dau,
                        (COUNT(DISTINCT user_id) FILTER (WHERE source = 'tracking'))::int AS tracking_dau,
                        COUNT(*)::int AS active_events,
                        (COUNT(*) FILTER (WHERE source = 'chat'))::int AS user_messages,
                        (COUNT(*) FILTER (WHERE source = 'tracking'))::int AS tracking_events,
                        MIN(ts) AS first_ts,
                        MAX(ts) AS last_ts
                    FROM active
                    GROUP BY day
                )
                SELECT day, dau, chat_dau, tracking_dau, active_events,
                       user_messages, tracking_events, first_ts, last_ts
                FROM daily
                ORDER BY day DESC
                LIMIT %s
                """,
                (since, since, since, since, tz, day_limit),
            ).fetchall()
        return [
            {
                "day": row[0],
                "dau": row[1],
                "chat_dau": row[2],
                "tracking_dau": row[3],
                "active_events": row[4],
                "user_messages": row[5],
                "tracking_events": row[6],
                "first_ts": row[7],
                "last_ts": row[8],
            }
            for row in rows
        ]
    except Exception as e:
        log.error("[db] admin_data_track_dau failed: %s", e)
        return []


def admin_data_track_proactive_daily(*, since_epoch: float = 0.0, days: int = 30,
                                     tz: str = "Asia/Shanghai") -> list[dict]:
    """Per-Beijing-day proactive-job aggregates for the ops trend view.

    Answers "is the proactive success rate improving day over day", split into
    the two runtime lanes (heartbeat vs screen-share). ``delivered`` = jobs that
    reached posted/delivered; ``failed`` = failed/skipped. Success rate is
    computed by the caller (delivered / non-pending)."""
    day_limit = max(1, min(int(days or 30), 366))
    since = float(since_epoch or 0.0)
    screen_kinds = "('screen_watch','scene_change','screen_tick','broadcast_opened','heartbeat_broadcast_on')"
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                f"""
                WITH jobs AS (
                    SELECT
                        to_char(timezone(%s, to_timestamp(ts)), 'YYYY-MM-DD') AS day,
                        COALESCE(NULLIF(doc->>'job_kind',''), NULLIF(doc->>'wake_kind',''),
                                 NULLIF(doc->>'trigger',''), 'unknown') AS kind,
                        COALESCE(doc->>'status','') AS status
                    FROM user_logs
                    WHERE stream = 'proactive_jobs'
                      AND ts IS NOT NULL
                      AND (%s = 0 OR ts >= %s)
                )
                SELECT day,
                       COUNT(*)::int AS jobs,
                       (COUNT(*) FILTER (WHERE status IN ('posted','delivered','completed')))::int AS delivered,
                       (COUNT(*) FILTER (WHERE status IN ('failed','skipped')))::int AS failed,
                       (COUNT(*) FILTER (WHERE status = 'pending'))::int AS pending,
                       (COUNT(*) FILTER (WHERE kind IN {screen_kinds}))::int AS screen,
                       (COUNT(*) FILTER (WHERE (kind = 'presence' OR kind LIKE 'heartbeat%%')
                                          AND kind NOT IN {screen_kinds}))::int AS heartbeat
                FROM jobs
                GROUP BY day
                ORDER BY day DESC
                LIMIT %s
                """,
                (tz, since, since, day_limit),
            ).fetchall()
        return [
            {
                "day": r[0], "jobs": r[1], "delivered": r[2], "failed": r[3],
                "pending": r[4], "screen": r[5], "heartbeat": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        log.error("[db] admin_data_track_proactive_daily failed: %s", e)
        return []


# Route split for the event-health view: model_api → "API", everything else
# (resident / official_import / unknown) folds to "VPS" on the caller side.
_EVENTS_ROUTES_CTE = (
    "WITH routes AS (SELECT user_id, "
    "lower(COALESCE(NULLIF(doc->>'route',''),'resident')) AS route "
    "FROM user_blobs WHERE kind = 'onboarding_route')"
)
# EXTRACT epoch from terminal_at - created_at, guarded to ISO-ish strings so
# malformed values degrade to NULL instead of aborting the whole aggregate.
_JOB_DUR_SEC = (
    "CASE WHEN COALESCE(doc->>'completed_at',doc->>'posted_at',doc->>'failed_at') "
    "~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
    "AND doc->>'created_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
    "THEN EXTRACT(EPOCH FROM ((COALESCE(doc->>'completed_at',doc->>'posted_at',doc->>'failed_at'))::timestamptz "
    "- (doc->>'created_at')::timestamptz)) ELSE NULL END"
)


def admin_events_overview() -> dict:
    """Fleet-wide event-health aggregates for the `view=events` board, split by
    route (VPS/resident vs API/model_api). Each sub-query is independently
    guarded so one failure degrades to an empty slice, not the whole board.

    Returns {proactive:[...], capture:[...], genesis:[...], reply:[...]} where each
    row carries route + the event dimension + counts + median duration (seconds)."""
    out = {"proactive": [], "capture": [], "genesis": [], "reply": []}

    def _run(key, sql):
        try:
            with get_pool().connection() as conn:
                rows = conn.execute(sql).fetchall()
            return rows
        except Exception as e:  # noqa: BLE001
            log.error("[db] admin_events_overview.%s failed: %s", key, e)
            return []

    # 1) Proactive lanes: 心跳 / 主动触发(感知+定时) / 屏幕 / 其他
    rows = _run("proactive", f"""
        {_EVENTS_ROUTES_CTE}
        SELECT COALESCE(r.route,'resident') AS route, j.lane,
               COUNT(*)::int AS total,
               (COUNT(*) FILTER (WHERE j.status IN ('posted','delivered','completed')))::int AS success,
               (COUNT(*) FILTER (WHERE j.status IN ('failed','skipped')))::int AS failed,
               (COUNT(*) FILTER (WHERE j.status = 'pending'))::int AS pending,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY j.dur) AS median_dur
        FROM (
          SELECT l.user_id, COALESCE(l.doc->>'status','') AS status, {_JOB_DUR_SEC.replace('doc','l.doc')} AS dur,
            CASE
              WHEN k.kind IN ('screen_watch','screen_tick','broadcast_opened','heartbeat_broadcast_on') THEN 'screen'
              WHEN k.kind IN ('perception_event','scene_change','photo_added','arrived_at_anchor','location','unlock_after_absence','scheduled_wake') THEN 'trigger'
              WHEN k.kind = 'presence' OR left(k.kind, 9) = 'heartbeat' THEN 'heartbeat'
              ELSE 'other'
            END AS lane
          FROM user_logs l,
            LATERAL (SELECT COALESCE(NULLIF(l.doc->>'job_kind',''),NULLIF(l.doc->>'wake_kind',''),NULLIF(l.doc->>'trigger',''),'unknown') AS kind) k
          WHERE l.stream = 'proactive_jobs'
            AND COALESCE(l.doc->>'job_kind','') NOT IN ('memory_capture','memory_dream','memory_migrate')
        ) j LEFT JOIN routes r ON r.user_id = j.user_id
        GROUP BY route, j.lane
    """)
    out["proactive"] = [
        {"route": r[0], "lane": r[1], "total": r[2], "success": r[3], "failed": r[4],
         "pending": r[5], "median_dur": float(r[6]) if r[6] is not None else None}
        for r in rows
    ]

    # 2) 主动记忆整理(category-level so the median is valid across dream+capture):
    #    memory_dream(做梦) + memory_capture(自写) + memory_migrate 合一。
    rows = _run("capture", f"""
        {_EVENTS_ROUTES_CTE}
        SELECT COALESCE(r.route,'resident') AS route,
               COUNT(*)::int AS total,
               (COUNT(*) FILTER (WHERE m.status = 'completed'))::int AS success,
               (COUNT(*) FILTER (WHERE m.status IN ('failed','error','skipped')))::int AS failed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY m.dur) AS median_dur
        FROM (
          SELECT l.user_id, COALESCE(l.doc->>'status','') AS status, {_JOB_DUR_SEC.replace('doc','l.doc')} AS dur
          FROM user_logs l
          WHERE l.stream = 'memory_capture_jobs'
          UNION ALL
          SELECT l.user_id, COALESCE(l.doc->>'status','') AS status, {_JOB_DUR_SEC.replace('doc','l.doc')} AS dur
          FROM user_logs l
          WHERE l.stream = 'proactive_jobs'
            AND COALESCE(l.doc->>'job_kind','') IN ('memory_capture','memory_dream','memory_migrate')
        ) m LEFT JOIN routes r ON r.user_id = m.user_id
        GROUP BY route
    """)
    out["capture"] = [
        {"route": r[0], "total": r[1], "success": r[2], "failed": r[3],
         "median_dur": float(r[4]) if r[4] is not None else None}
        for r in rows
    ]

    # 3) 蒸馏: genesis job — mode=onboarding → 一次(first); add_memory/update_identity → 二次(second)
    rows = _run("genesis", f"""
        {_EVENTS_ROUTES_CTE}
        SELECT COALESCE(r.route,'resident') AS route,
               CASE WHEN COALESCE(NULLIF(g.metadata->>'mode',''),'onboarding') = 'onboarding'
                    THEN 'first' ELSE 'second' END AS distill,
               COUNT(*)::int AS total,
               (COUNT(*) FILTER (WHERE g.status IN ('done','completed')))::int AS success,
               (COUNT(*) FILTER (WHERE g.status IN ('error','failed')))::int AS failed
        FROM genesis_import_jobs g LEFT JOIN routes r ON r.user_id = g.user_id
        GROUP BY route, distill
    """)
    out["genesis"] = [
        {"route": r[0], "distill": r[1], "total": r[2], "success": r[3], "failed": r[4]}
        for r in rows
    ]

    # 4) 回复消息: 真回复率 + 兜底率(foreground_fallback)
    rows = _run("reply", f"""
        {_EVENTS_ROUTES_CTE}
        SELECT COALESCE(r.route,'resident') AS route,
               (COUNT(*) FILTER (WHERE c.doc->>'role' = 'user'
                    AND COALESCE(c.doc->>'source','') <> 'verify_ping'))::int AS user_msgs,
               (COUNT(*) FILTER (WHERE c.doc->>'role' IN ('agent','openclaw')
                    AND COALESCE(c.doc->>'source','') NOT IN ('foreground_fallback','proactive_fallback')))::int AS real_replies,
               (COUNT(*) FILTER (WHERE c.doc->>'source' = 'foreground_fallback'))::int AS fallback_replies
        FROM chat_messages c LEFT JOIN routes r ON r.user_id = c.user_id
        GROUP BY route
    """)
    out["reply"] = [
        {"route": r[0], "user_msgs": r[1], "real_replies": r[2], "fallback_replies": r[3]}
        for r in rows
    ]
    return out


# ---------------------------------------------------------------------------
# Per-user singleton blobs
# ---------------------------------------------------------------------------


def get_blob(user_id: str, kind: str):
    """Return the stored JSON doc (dict or list) for (user_id, kind), or None."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc FROM user_blobs WHERE user_id = %s AND kind = %s",
                (user_id, kind),
            ).fetchone()
        return row[0] if row is not None else None
    except Exception as e:
        log.error("[db] get_blob(%s,%s) failed: %s", user_id, kind, e)
        return None


def set_blob(user_id: str, kind: str, doc) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc",
                (user_id, kind, Jsonb(doc)),
            )
    except Exception as e:
        log.error("[db] set_blob(%s,%s) failed: %s", user_id, kind, e)


def list_agent_runtime_enabled_users(include_gateway: bool = False) -> list[dict]:
    """配了能 fit 的 provider 且 test_status='ok' 的用户都纳入托管（与
    hosted/agent_runtime_cutover.resolve_driver 一致——不再有 per-user
    ``agent_runtime_driver`` 开关；kill switch 改用删 config 或改 test_status）。
    AGENT 由 provider 派生（保持 CASE 与 cutover.driver_for_provider 同步）：
    anthropic/deepseek → claude；openai → codex (native)。gateway-only provider
    (gemini/openrouter/openai_compatible → codex via LiteLLM gateway) 仅当
    ``include_gateway`` 时返回（gateway 关时不发现，避免 spawn 到不存在的 proxy）。
    Returns [{"user_id","driver","provider","model","base_url","supports_responses"}]
    sorted by user_id (``supports_responses`` is the openai_compatible relay's
    /v1/responses capability, set at setup; selects native passthrough vs the
    LiteLLM chat-completions bridge)。"""
    providers = ["anthropic", "claude", "deepseek", "openai"]
    if include_gateway:
        providers += ["gemini", "openrouter", "openai_compatible"]
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id,
                  CASE LOWER(COALESCE(doc->>'provider', ''))
                    WHEN 'anthropic' THEN 'claude'
                    WHEN 'claude'    THEN 'claude'
                    WHEN 'deepseek'  THEN 'claude'
                    ELSE 'codex'
                  END AS driver,
                  LOWER(COALESCE(doc->>'provider', '')) AS provider,
                  COALESCE(doc->>'model', '') AS model,
                  COALESCE(doc->>'base_url', '') AS base_url,
                  COALESCE(doc->>'supports_responses', '') AS supports_responses
                FROM user_blobs
                WHERE kind = 'model_api'
                  AND COALESCE(doc->>'test_status', '') = 'ok'
                  AND LOWER(COALESCE(doc->>'provider', '')) = ANY(%s)
                ORDER BY user_id
                """,
                (providers,),
            ).fetchall()
        return [{"user_id": uid, "driver": driver, "provider": provider,
                 "model": model, "base_url": base_url,
                 "supports_responses": supports_responses == "true"}
                for uid, driver, provider, model, base_url, supports_responses in rows]
    except Exception as e:
        log.error("[db] list_agent_runtime_enabled_users failed: %s", e)
        return []


def try_stamp_hosted_tick(user_id: str, doc: dict, now: float, interval_sec: float) -> bool:
    """Atomically claim this user's next hosted-heartbeat slot. Stamps the
    ``hosted_tick`` blob with ``doc`` iff there is no prior stamp or the prior
    one is at least ``interval_sec`` old, and returns whether THIS call won.

    Replaces the read-then-write ts check so that two workers which both hold
    the user's plaintext key can't each create a heartbeat in the same interval
    (the per-job consume path is separately deduped by the job-status CAS in
    log_patch_item). ``doc`` must carry a numeric ``ts`` field."""
    try:
        threshold = now - interval_sec
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, 'hosted_tick', %s) "
                "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc "
                "WHERE COALESCE((user_blobs.doc->>'ts')::float8, 0) <= %s "
                "RETURNING doc",
                (user_id, Jsonb(doc), threshold),
            ).fetchone()
        return row is not None
    except Exception as e:
        log.error("[db] try_stamp_hosted_tick(%s) failed: %s", user_id, e)
        return False


def delete_blob(user_id: str, kind: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM user_blobs WHERE user_id = %s AND kind = %s",
                (user_id, kind),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] delete_blob(%s,%s) failed: %s", user_id, kind, e)
        return False


def list_blobs(user_id: str, kind_prefix: str) -> list[dict]:
    """Return all blob docs for a user whose ``kind`` starts with ``kind_prefix``.
    Used for collection-style blobs keyed as ``<prefix><id>`` (e.g. one blob per
    history-import job)."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM user_blobs WHERE user_id = %s AND kind LIKE %s",
                (user_id, kind_prefix.replace("%", r"\%").replace("_", r"\_") + "%"),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] list_blobs(%s,%s) failed: %s", user_id, kind_prefix, e)
        return []


# ---------------------------------------------------------------------------
# Genesis import ledger (chunked import, reducer outputs, runtime-ready state)
# ---------------------------------------------------------------------------


def _genesis_row(cur, row) -> dict | None:
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    out = dict(zip(cols, row))
    for key, value in list(out.items()):
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


def genesis_create_job(user_id: str, job: dict) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO genesis_import_jobs
                (user_id, job_id, status, source_kind, file_manifest_hash,
                 total_chunks, total_bytes, privacy_mode, metadata, output,
                 updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, now())
            ON CONFLICT (user_id, job_id) DO NOTHING
            RETURNING *
            """,
            (
                user_id,
                job["job_id"],
                job.get("status", "created"),
                job.get("source_kind", "unknown"),
                job.get("file_manifest_hash", ""),
                int(job.get("total_chunks") or 0),
                int(job.get("total_bytes") or 0),
                job.get("privacy_mode", ""),
                Jsonb(job.get("metadata") or {}),
            ),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_get_job(user_id: str, job_id: str) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT * FROM genesis_import_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_list_jobs(user_id: str, *, limit: int = 20) -> list[dict]:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT * FROM genesis_import_jobs WHERE user_id = %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (user_id, max(1, min(int(limit or 20), 100))),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    out: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        for key, value in list(item.items()):
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        out.append(item)
    return out


def genesis_latest_done_job(user_id: str) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT * FROM genesis_import_jobs WHERE user_id = %s AND status = 'done' "
            "ORDER BY completed_at DESC NULLS LAST, updated_at DESC LIMIT 1",
            (user_id,),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_claim_uploaded_jobs(*, limit: int = 1) -> list[dict]:
    """Atomically claim uploaded genesis jobs for the CVM worker.

    Uses SKIP LOCKED so multiple worker loops can poll without double-processing
    the same import. Claimed jobs move uploaded -> processing in the same
    transaction; genesis_state is updated by the worker service layer.
    """
    safe_limit = max(1, min(int(limit or 1), 16))
    with get_pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                """
                WITH picked AS (
                    SELECT user_id, job_id
                    FROM genesis_import_jobs
                    WHERE status = 'uploaded'
                    ORDER BY finalized_at ASC NULLS LAST, updated_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE genesis_import_jobs AS j SET
                    status = 'processing',
                    error = '',
                    output = jsonb_build_object('stage', 'worker_claimed'),
                    updated_at = now()
                FROM picked
                WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
                RETURNING j.*
                """,
                (safe_limit,),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    out: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        for key, value in list(item.items()):
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        out.append(item)
    return out


def genesis_reap_stale_processing_jobs(older_than_sec: int, *, error: str, limit: int = 50) -> list[dict]:
    """Atomically fail genesis jobs wedged in 'processing' past a staleness cutoff.

    A normal failure flips a job to 'failed' via the service layer. But if the
    worker/plaintext daemon crashes or is killed mid-LLM-call, the job stays
    'processing' forever — the worker only re-claims 'uploaded' jobs, so nothing
    ever fails it, and that blocks the user's agent spawn.

    The status='processing' AND cutoff checks live INSIDE the UPDATE (with
    FOR UPDATE SKIP LOCKED), so a row another worker has since heartbeated
    (updated_at bumped past the cutoff) or completed is not selected and not
    touched — no list→fail TOCTOU race with live/finished imports under multiple
    workers. A live reducer heartbeats updated_at per chunk via genesis_touch_job,
    so a genuinely-progressing job is never older than the cutoff. Returns the
    rows actually flipped so the caller can sync their genesis_state blobs.
    """
    safe_sec = max(60, int(older_than_sec or 0))
    safe_limit = max(1, min(int(limit or 1), 200))
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            WITH picked AS (
                SELECT user_id, job_id
                FROM genesis_import_jobs
                WHERE status = 'processing'
                  AND updated_at < now() - make_interval(secs => %s)
                ORDER BY updated_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE genesis_import_jobs AS j SET
                status = 'failed',
                error = %s,
                updated_at = now()
            FROM picked
            WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
            RETURNING j.*
            """,
            (safe_sec, safe_limit, error[:1000]),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    out: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        for key, value in list(item.items()):
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        out.append(item)
    return out


def genesis_put_chunk(
    user_id: str,
    job_id: str,
    *,
    seq: int,
    byte_start: int,
    byte_end: int,
    ciphertext_sha256: str,
    content_sha256: str,
    aad: dict,
    encrypted_body: bytes,
) -> dict:
    size_bytes = len(encrypted_body)
    with get_pool().connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                "SELECT ciphertext_sha256 FROM genesis_import_chunks "
                "WHERE user_id = %s AND job_id = %s AND seq = %s",
                (user_id, job_id, seq),
            ).fetchone()
            if existing is not None and existing[0] != ciphertext_sha256:
                raise ValueError("chunk_hash_conflict")
            cur = conn.execute(
                """
                INSERT INTO genesis_import_chunks
                    (user_id, job_id, seq, byte_start, byte_end,
                     ciphertext_sha256, content_sha256, aad, encrypted_body,
                     size_bytes, status, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'uploaded', now())
                ON CONFLICT (user_id, job_id, seq) DO UPDATE SET
                    byte_start = EXCLUDED.byte_start,
                    byte_end = EXCLUDED.byte_end,
                    content_sha256 = EXCLUDED.content_sha256,
                    aad = EXCLUDED.aad,
                    status = 'uploaded',
                    updated_at = now()
                RETURNING user_id, job_id, seq, byte_start, byte_end,
                          ciphertext_sha256, content_sha256, aad, size_bytes,
                          status, attempts, map_output_ref, error, created_at,
                          updated_at
                """,
                (
                    user_id,
                    job_id,
                    seq,
                    byte_start,
                    byte_end,
                    ciphertext_sha256,
                    content_sha256,
                    Jsonb(aad),
                    encrypted_body,
                    size_bytes,
                ),
            )
            chunk = _genesis_row(cur, cur.fetchone()) or {}
            conn.execute(
                """
                UPDATE genesis_import_jobs SET
                    status = CASE
                        WHEN status = 'created' THEN 'uploading'
                        ELSE status
                    END,
                    received_chunks = (
                        SELECT COUNT(*) FROM genesis_import_chunks
                        WHERE user_id = %s AND job_id = %s
                    ),
                    received_bytes = COALESCE((
                        SELECT SUM(size_bytes) FROM genesis_import_chunks
                        WHERE user_id = %s AND job_id = %s
                    ), 0),
                    updated_at = now()
                WHERE user_id = %s AND job_id = %s
                """,
                (user_id, job_id, user_id, job_id, user_id, job_id),
            )
    return chunk


def genesis_missing_chunk_seqs(user_id: str, job_id: str, total_chunks: int) -> list[int]:
    if total_chunks <= 0:
        return []
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT seq FROM genesis_import_chunks WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        ).fetchall()
    have = {int(row[0]) for row in rows}
    return [seq for seq in range(total_chunks) if seq not in have]


def genesis_list_chunks(user_id: str, job_id: str) -> list[dict]:
    """Return all chunk rows, including encrypted body bytes, ordered by seq."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            SELECT user_id, job_id, seq, byte_start, byte_end,
                   ciphertext_sha256, content_sha256, aad, encrypted_body,
                   size_bytes, status, attempts, map_output_ref, error,
                   created_at, updated_at
            FROM genesis_import_chunks
            WHERE user_id = %s AND job_id = %s
            ORDER BY seq ASC
            """,
            (user_id, job_id),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    out: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        body = item.get("encrypted_body")
        if isinstance(body, memoryview):
            item["encrypted_body"] = body.tobytes()
        for key, value in list(item.items()):
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        out.append(item)
    return out


def genesis_mark_finalized(user_id: str, job_id: str) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            UPDATE genesis_import_jobs SET
                status = 'uploaded',
                finalized_at = COALESCE(finalized_at, now()),
                updated_at = now()
            WHERE user_id = %s AND job_id = %s
              AND status IN ('created', 'uploading', 'uploaded', 'failed')
            RETURNING *
            """,
            (user_id, job_id),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_set_job_status(
    user_id: str,
    job_id: str,
    *,
    status: str,
    error: str = "",
    output: dict | None = None,
    processed_chunks: int | None = None,
) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            UPDATE genesis_import_jobs SET
                status = %s,
                error = %s,
                output = COALESCE(%s::jsonb, output),
                processed_chunks = COALESCE(%s, processed_chunks),
                updated_at = now()
            WHERE user_id = %s AND job_id = %s
            RETURNING *
            """,
            (
                status,
                error[:1000],
                Jsonb(output) if output is not None else None,
                processed_chunks,
                user_id,
                job_id,
            ),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_touch_job(user_id: str, job_id: str) -> None:
    """Heartbeat: bump updated_at for a processing genesis job so the stale
    reaper can tell a live long import from a worker that died mid-run. No-op
    unless the job is currently 'processing'."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            UPDATE genesis_import_jobs SET updated_at = now()
            WHERE user_id = %s AND job_id = %s AND status = 'processing'
            """,
            (user_id, job_id),
        )


def genesis_upsert_output(
    user_id: str,
    job_id: str,
    output_type: str,
    *,
    doc: dict,
    status: str,
    ref: str = "",
) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO genesis_import_outputs
                (user_id, job_id, output_type, ref, status, doc, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, job_id, output_type) DO UPDATE SET
                ref = EXCLUDED.ref,
                status = EXCLUDED.status,
                doc = EXCLUDED.doc,
                updated_at = now()
            RETURNING *
            """,
            (user_id, job_id, output_type, ref, status, Jsonb(doc)),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_get_output(user_id: str, job_id: str, output_type: str) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT * FROM genesis_import_outputs "
            "WHERE user_id = %s AND job_id = %s AND output_type = %s",
            (user_id, job_id, output_type),
        )
        return _genesis_row(cur, cur.fetchone())


def genesis_complete_job(
    user_id: str,
    job_id: str,
    *,
    output: dict,
    memory_action_count: int,
    identity_status: str,
    persona_ref: str,
    persona_sha256: str,
) -> dict | None:
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            UPDATE genesis_import_jobs SET
                status = 'done',
                output = %s,
                memory_action_count = %s,
                identity_status = %s,
                persona_ref = %s,
                persona_sha256 = %s,
                completed_at = COALESCE(completed_at, now()),
                updated_at = now(),
                error = ''
            WHERE user_id = %s AND job_id = %s
            RETURNING *
            """,
            (
                Jsonb(output),
                int(memory_action_count),
                identity_status[:120],
                persona_ref[:240],
                persona_sha256[:80],
                user_id,
                job_id,
            ),
        )
        return _genesis_row(cur, cur.fetchone())


# ---------------------------------------------------------------------------
# Chat messages (row-per-item ring buffer)
# ---------------------------------------------------------------------------


def chat_load(user_id: str) -> list[dict]:
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM chat_messages WHERE user_id = %s ORDER BY seq ASC",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] chat_load(%s) failed: %s", user_id, e)
        return []


def chat_append(user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int) -> None:
    """Insert one chat message then trim to the newest ``max_messages`` rows,
    mirroring the in-memory ring buffer. Idempotent on msg_id."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id, msg_id) DO UPDATE SET ts = EXCLUDED.ts, doc = EXCLUDED.doc",
                    (user_id, msg_id, ts, Jsonb(doc)),
                )
                if max_messages and max_messages > 0:
                    conn.execute(
                        "DELETE FROM chat_messages WHERE user_id = %s AND seq < ("
                        "  SELECT MIN(seq) FROM ("
                        "    SELECT seq FROM chat_messages WHERE user_id = %s "
                        "    ORDER BY seq DESC LIMIT %s"
                        "  ) t"
                        ")",
                        (user_id, user_id, max_messages),
                    )
    except Exception as e:
        log.error("[db] chat_append(%s,%s) failed: %s", user_id, msg_id, e)


def chat_update_metadata(user_id: str, msg_id: str, fields: dict) -> dict | None:
    """Shallow-merge ``fields`` into the stored message doc. Returns the merged
    doc, or None if the message was not found."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE chat_messages SET doc = doc || %s WHERE user_id = %s AND msg_id = %s "
                "RETURNING doc",
                (Jsonb(fields), user_id, msg_id),
            ).fetchone()
        return row[0] if row is not None else None
    except Exception as e:
        log.error("[db] chat_update_metadata(%s,%s) failed: %s", user_id, msg_id, e)
        return None


def chat_try_claim_reply(
    user_id: str, msg_id: str, consumer_id: str, now: float, fields: dict,
    *, redelivery: bool = False,
) -> dict | None:
    """Atomically claim a chat reply for ``consumer_id`` — the cross-worker-safe
    replacement for read-cache-then-write. The claim succeeds iff the row is
    currently unclaimed, already ours, or the prior claim has expired (the SQL
    WHERE mirrors chat.service._chat_message_claimable). Returns the merged doc
    on success, or None if the row is missing or another consumer/worker holds
    an unexpired claim — so two workers polling the same reply can't both win.

    ``redelivery=True`` (the lost-turn backstop, chat.service) hardens the CAS
    against the caller's stale per-worker cache with two extra conditions the
    fresh-delivery path must NOT have:
    - rejects OUR OWN unexpired claim (no idempotent self-refresh): re-handing
      an in-flight redelivered turn to its claimer would run a duplicate
      provider turn. A fresh delivery keeps the self-refresh so a poll retry of
      a just-claimed message doesn't error.
    - rejects the claim when ANY newer visible user message is already replied
      (the superseded-tail rule, decided HERE at claim time): the cache-side
      _redelivery_floor pre-filter can miss it because parent reply_status
      metadata updates are not broadcast across workers, and a late reply to a
      conversation that already moved on would land out of order. Synthetic
      verify_ping probes are not conversation and never supersede."""
    same_consumer_sql = "" if redelivery else "OR doc->>'reply_claimed_by' = %s "
    unanswered_tail_sql = (
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM chat_messages n "
        "    WHERE n.user_id = chat_messages.user_id "
        "      AND n.ts > chat_messages.ts "
        "      AND n.doc->>'role' = 'user' "
        "      AND COALESCE(n.doc->>'source','') <> 'verify_ping' "
        "      AND ((n.doc->>'reply_status') = 'replied' "
        "           OR COALESCE(n.doc->>'reply_message_id','') <> '')"
        "  ) "
    ) if redelivery else ""
    params: list = [Jsonb(fields), user_id, msg_id]
    if not redelivery:
        params.append(consumer_id)
    params.append(now)
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE chat_messages SET doc = doc || %s "
                "WHERE user_id = %s AND msg_id = %s "
                # Reject already-replied rows in the DB itself, not just via the
                # caller's (possibly stale) cache pre-gate: another worker may
                # have posted the reply (reply_status/reply_message_id) after
                # this worker last refreshed. Mirrors _chat_message_claimable.
                "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                "  AND COALESCE(doc->>'reply_message_id','') = '' "
                f"{unanswered_tail_sql}"
                "  AND ("
                "    COALESCE(doc->>'reply_claimed_by','') = '' "
                f"    {same_consumer_sql}"
                "    OR COALESCE(NULLIF(doc->>'reply_claim_expires_at','')::float8, 0) <= %s"
                ") RETURNING doc",
                tuple(params),
            ).fetchone()
        return row[0] if row is not None else None
    except Exception as e:
        log.error("[db] chat_try_claim_reply(%s,%s) failed: %s", user_id, msg_id, e)
        return None


def chat_delete(user_id: str, msg_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s",
                (user_id, msg_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] chat_delete(%s,%s) failed: %s", user_id, msg_id, e)
        return False


def chat_clear(user_id: str) -> int | None:
    """Delete every chat row for one user. Returns deleted row count, or None
    if the database operation failed."""
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM chat_messages WHERE user_id = %s",
                (user_id,),
            )
        return cur.rowcount
    except Exception as e:
        log.error("[db] chat_clear(%s) failed: %s", user_id, e)
        return None


# ---------------------------------------------------------------------------
# Memory moments (row-per-item)
# ---------------------------------------------------------------------------


def memory_load(user_id: str) -> list[dict]:
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM memory_moments WHERE user_id = %s "
                "ORDER BY occurred_at, moment_id",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] memory_load(%s) failed: %s", user_id, e)
        return []


def memory_upsert(user_id: str, moment_id: str, occurred_at: str, doc: dict) -> bool:
    """Single-row upsert. Returns True iff the write committed — callers that
    advance state on success (e.g. memory.upgrade / migration) MUST check it."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, moment_id) DO UPDATE SET "
                "occurred_at = EXCLUDED.occurred_at, doc = EXCLUDED.doc",
                (user_id, moment_id, occurred_at or "", Jsonb(doc)),
            )
        return True
    except Exception as e:
        log.error("[db] memory_upsert(%s,%s) failed: %s", user_id, moment_id, e)
        return False


def memory_delete(user_id: str, moment_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s",
                (user_id, moment_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] memory_delete(%s,%s) failed: %s", user_id, moment_id, e)
        return False


def memory_replace_all(user_id: str, moments: list[dict]) -> None:
    """Atomically reconcile the stored moment set to `moments`. The final row
    set equals the input list (full-replace semantics preserved), but only rows
    that were removed are deleted and only rows whose doc changed are upserted,
    so a single-card edit no longer rewrites the user's entire garden. Used
    where the old code did load-list / mutate / save-whole-list."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT moment_id, occurred_at, doc FROM memory_moments WHERE user_id = %s",
                    (user_id,),
                ).fetchall()
                existing = {r[0]: (r[1], r[2]) for r in rows}

                # last-writer-wins on duplicate ids, mirroring the old
                # DELETE-then-INSERT/ON CONFLICT behavior; drop id-less dicts.
                new = {str(m["id"]): m for m in moments if m.get("id")}

                for mid in existing.keys() - new.keys():
                    conn.execute(
                        "DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s",
                        (user_id, mid),
                    )
                for mid, m in new.items():
                    occurred_at = str(m.get("occurred_at") or "")
                    prev = existing.get(mid)
                    # Skip only when BOTH the doc and the derived occurred_at
                    # column match — the old full-replace path always rewrote
                    # occurred_at from the input, so an unchanged doc paired with
                    # a stale ordering column must still be rewritten or
                    # memory_load() (ORDER BY occurred_at) returns wrong order.
                    if prev is not None and prev[0] == occurred_at and prev[1] == m:
                        continue
                    conn.execute(
                        "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (user_id, moment_id) DO UPDATE SET "
                        "occurred_at = EXCLUDED.occurred_at, doc = EXCLUDED.doc",
                        (user_id, mid, occurred_at, Jsonb(m)),
                    )
    except Exception as e:
        log.error("[db] memory_replace_all(%s) failed: %s", user_id, e)


# ---------------------------------------------------------------------------
# World book entries (row-per-item)
# ---------------------------------------------------------------------------


def world_book_load(user_id: str) -> list[dict]:
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM world_book_entries WHERE user_id = %s "
                "ORDER BY updated_at, entry_id",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] world_book_load(%s) failed: %s", user_id, e)
        return []


def world_book_upsert(user_id: str, entry_id: str, updated_at: str, doc: dict) -> bool:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, entry_id) DO UPDATE SET "
                "updated_at = EXCLUDED.updated_at, doc = EXCLUDED.doc",
                (user_id, entry_id, updated_at or "", Jsonb(doc)),
            )
        return True
    except Exception as e:
        log.error("[db] world_book_upsert(%s,%s) failed: %s", user_id, entry_id, e)
        return False


def world_book_delete(user_id: str, entry_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM world_book_entries WHERE user_id = %s AND entry_id = %s",
                (user_id, entry_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] world_book_delete(%s,%s) failed: %s", user_id, entry_id, e)
        return False


def world_book_replace_all(user_id: str, entries: list[dict]) -> None:
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT entry_id, updated_at, doc FROM world_book_entries WHERE user_id = %s",
                    (user_id,),
                ).fetchall()
                existing = {r[0]: (r[1], r[2]) for r in rows}
                new = {str(e["id"]): e for e in entries if e.get("id")}
                for entry_id in existing.keys() - new.keys():
                    conn.execute(
                        "DELETE FROM world_book_entries WHERE user_id = %s AND entry_id = %s",
                        (user_id, entry_id),
                    )
                for entry_id, entry in new.items():
                    updated_at = str(entry.get("updated_at") or "")
                    prev = existing.get(entry_id)
                    if prev is not None and prev[0] == updated_at and prev[1] == entry:
                        continue
                    conn.execute(
                        "INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (user_id, entry_id) DO UPDATE SET "
                        "updated_at = EXCLUDED.updated_at, doc = EXCLUDED.doc",
                        (user_id, entry_id, updated_at, Jsonb(entry)),
                    )
    except Exception as e:
        log.error("[db] world_book_replace_all(%s) failed: %s", user_id, e)


# ---------------------------------------------------------------------------
# Frame envelopes (heavy body_ct lives here; frames_meta index stays a blob)
# ---------------------------------------------------------------------------


def _frame_write_row(user_id: str, frame_id: str, ts: float,
                     doc: dict | None, env_meta: dict | None, body_key: str | None) -> bool:
    """Upsert one frame_envelopes row. Returns True on success; swallows-and-logs
    on failure (request-path parity) and returns False so the caller can decide
    whether it is safe to touch R2."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO frame_envelopes (user_id, frame_id, ts, doc, env_meta, body_key) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, frame_id) DO UPDATE SET ts = EXCLUDED.ts, "
                "doc = EXCLUDED.doc, env_meta = EXCLUDED.env_meta, body_key = EXCLUDED.body_key",
                (user_id, frame_id, float(ts),
                 Jsonb(doc) if doc is not None else None,
                 Jsonb(env_meta) if env_meta is not None else None,
                 body_key),
            )
        return True
    except Exception as e:
        log.error("[db] frame_upsert(%s,%s) row write failed: %s", user_id, frame_id, e)
        return False


def frame_upsert(user_id: str, frame_id: str, ts: float, doc: dict) -> None:
    """Persist a v1 frame envelope.

    With R2 configured, the heavy ``body_ct`` is offloaded to object storage and
    the row keeps only the small envelope metadata (``env_meta``) plus the R2
    pointer (``body_key``); ``doc`` is NULL. Without R2 the full envelope is
    stored inline in ``doc`` (legacy shape). The caller's ``doc`` is not mutated.

    Ordering matters — the row is written so it is self-consistent at every
    durable point, never pointing at an object that does not exist yet:
      1. write the row INLINE (full envelope, no pointer) — readable immediately;
      2. upload the body to R2;
      3. only once the object exists, flip the row to the pointer shape (doc
         NULL, env_meta + body_key) as the LAST durable step.
    A crash/abort at any point leaves either an inline (readable) row or a
    pointer whose object is already present; a failed upload just keeps the
    inline row. ``doc`` is offloaded out of the row only after the body is in
    R2, so the at-rest table stays small without a missing-object window."""
    if object_storage.enabled() and isinstance(doc, dict) and doc.get("body_ct") is not None:
        # 1) inline first — frame readable, references no R2 object yet.
        if not _frame_write_row(user_id, frame_id, ts, doc, None, None):
            return  # DB write failed → nothing committed, R2 untouched.
        # 2) upload; on failure keep the inline row (frame stays readable).
        try:
            object_storage.put_frame_body(user_id, frame_id, doc["body_ct"])
        except Exception as e:  # noqa: BLE001
            log.error("[db] frame_upsert(%s,%s) R2 upload failed, leaving inline: %s",
                      user_id, frame_id, e)
            return
        # 3) object now exists → flip to pointer as the last durable step. If
        #    this write fails the row stays inline (readable); the uploaded
        #    object is a harmless orphan.
        env_meta = {k: v for k, v in doc.items() if k != "body_ct"}
        body_key = object_storage.frame_key(user_id, frame_id)
        _frame_write_row(user_id, frame_id, ts, None, env_meta, body_key)
        return
    _frame_write_row(user_id, frame_id, ts, doc, None, None)


def frame_exists(user_id: str, frame_id: str) -> bool:
    """Cheap existence check (avoids pulling the heavy body_ct) for the proxy
    guards in frame_decrypt / frame_image."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM frame_envelopes WHERE user_id = %s AND frame_id = %s",
                (user_id, frame_id),
            ).fetchone()
        return row is not None
    except Exception as e:
        log.error("[db] frame_exists(%s,%s) failed: %s", user_id, frame_id, e)
        return False


def frame_get(user_id: str, frame_id: str) -> dict | None:
    """Return the full v1 envelope, reconstructing ``body_ct`` from R2 for
    offloaded rows (``body_key`` set) and returning the inline ``doc`` for
    legacy rows."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc, env_meta, body_key FROM frame_envelopes "
                "WHERE user_id = %s AND frame_id = %s",
                (user_id, frame_id),
            ).fetchone()
    except Exception as e:
        log.error("[db] frame_get(%s,%s) failed: %s", user_id, frame_id, e)
        return None
    if row is None:
        return None
    doc, env_meta, body_key = row
    if body_key:
        body_ct = object_storage.get_frame_body(user_id, frame_id)
        if body_ct is None:
            # The pointer row exists but its R2 body is missing/unreadable.
            # Report not-found rather than a metadata-only dict — callers treat
            # any dict as a valid envelope and would serve an undecryptable frame.
            log.error("[db] frame_get(%s,%s) R2 body missing for key %s",
                      user_id, frame_id, body_key)
            return None
        return {**(env_meta or {}), "body_ct": body_ct}
    return doc


def frame_delete(user_id: str, frame_id: str) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM frame_envelopes WHERE user_id = %s AND frame_id = %s",
                (user_id, frame_id),
            )
    except Exception as e:
        # Row delete failed → the pointer row survives, so leave the R2 body in
        # place; deleting it now would corrupt later reads of the still-present row.
        log.error("[db] frame_delete(%s,%s) failed: %s", user_id, frame_id, e)
        return
    if object_storage.enabled():
        object_storage.delete_frame_body(user_id, frame_id)


def frame_list_meta(user_id: str) -> list[dict]:
    """Reconstruct a lightweight frames_meta index from the stored envelopes.
    Used as the rebuild fallback when the frames_meta blob is missing."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT frame_id, ts, COALESCE(env_meta, doc) FROM frame_envelopes "
                "WHERE user_id = %s ORDER BY ts",
                (user_id,),
            ).fetchall()
    except Exception as e:
        log.error("[db] frame_list_meta(%s) failed: %s", user_id, e)
        return []
    meta: list[dict] = []
    for frame_id, ts, doc in rows:
        meta.append({
            "filename": f"{frame_id}.env.json",
            "ts": ts,
            "app": None,
            "ocr_text": "",
            "w": 0,
            "h": 0,
            "encrypted": True,
            "id": frame_id,
            "v": (doc or {}).get("v", 1),
            "owner_user_id": (doc or {}).get("owner_user_id"),
        })
    return meta


def frame_prune_to(user_id: str, max_frames: int) -> list[str]:
    """Keep only the newest ``max_frames`` envelopes (by ts); delete the rest.
    Returns the evicted frame_ids."""
    if not max_frames or max_frames <= 0:
        return []
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT frame_id FROM frame_envelopes WHERE user_id = %s AND frame_id NOT IN ("
                    "  SELECT frame_id FROM frame_envelopes WHERE user_id = %s "
                    "  ORDER BY ts DESC LIMIT %s"
                    ")",
                    (user_id, user_id, max_frames),
                ).fetchall()
                evicted = [r[0] for r in rows]
                if evicted:
                    conn.execute(
                        "DELETE FROM frame_envelopes WHERE user_id = %s AND frame_id = ANY(%s)",
                        (user_id, evicted),
                    )
        if evicted and object_storage.enabled():
            for fid in evicted:
                object_storage.delete_frame_body(user_id, fid)
        return evicted
    except Exception as e:
        log.error("[db] frame_prune_to(%s) failed: %s", user_id, e)
        return []


# ---------------------------------------------------------------------------
# Per-user append logs (the 6 JSONL streams)
# ---------------------------------------------------------------------------


def log_append(user_id: str, stream: str, doc: dict,
               ts: float | None = None, item_key: str | None = None) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, stream, ts, item_key, Jsonb(doc)),
            )
    except Exception as e:
        log.error("[db] log_append(%s,%s) failed: %s", user_id, stream, e)


def log_read(user_id: str, stream: str, limit: int = 100, since_epoch: float = 0.0) -> list[dict]:
    """Return log docs in chronological (seq) order. When ``limit`` > 0 returns
    the newest ``limit`` rows (still chronological). ``since_epoch`` filters on
    the ts column (rows with NULL ts are excluded when since_epoch is set)."""
    try:
        params: list = [user_id, stream]
        where = "user_id = %s AND stream = %s"
        if since_epoch:
            where += " AND ts > %s"
            params.append(since_epoch)
        if limit and limit > 0:
            sql = (
                f"SELECT doc FROM (SELECT doc, seq FROM user_logs WHERE {where} "
                f"ORDER BY seq DESC LIMIT %s) t ORDER BY seq ASC"
            )
            params.append(limit)
        else:
            sql = f"SELECT doc FROM user_logs WHERE {where} ORDER BY seq ASC"
        with get_pool().connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] log_read(%s,%s) failed: %s", user_id, stream, e)
        return []


def log_read_all(user_id: str, stream: str) -> list[dict]:
    return log_read(user_id, stream, limit=0, since_epoch=0.0)


def log_patch_item(user_id: str, stream: str, item_key: str, patch: dict,
                   only_if_status: str | None = None) -> dict | None:
    """Shallow-merge ``patch`` into the newest log row matching ``item_key``.
    When ``only_if_status`` is set, the update only applies if the row's current
    ``doc->>'status'`` equals it (returns None otherwise). Returns merged doc."""
    try:
        params: list = [Jsonb(patch), user_id, stream, user_id, stream, item_key]
        guard = ""
        if only_if_status is not None:
            guard = " AND doc->>'status' = %s"
            params.append(only_if_status)
        sql = (
            "UPDATE user_logs SET doc = doc || %s "
            "WHERE user_id = %s AND stream = %s AND seq = ("
            "  SELECT seq FROM user_logs WHERE user_id = %s AND stream = %s AND item_key = %s "
            "  ORDER BY seq DESC LIMIT 1"
            ")" + guard + " RETURNING doc"
        )
        with get_pool().connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row is not None else None
    except Exception as e:
        log.error("[db] log_patch_item(%s,%s,%s) failed: %s", user_id, stream, item_key, e)
        return None


def log_trim(user_id: str, stream: str, max_rows: int,
             only_statuses: "list[str] | None" = None) -> None:
    """Keep only the newest ``max_rows`` rows of a stream.

    When ``only_statuses`` is given, a row is eligible for deletion only if its
    ``doc->>'status'`` is in that set — rows in any other status (e.g. an
    in-flight ``queued``/``processing`` trace still awaiting its completion
    patch) are kept regardless of age, so trim never drops a row a later
    ``log_patch_item`` still expects to update. The newest-``max_rows`` cutoff is
    computed over all rows; only the *deletion* is status-restricted."""
    if not max_rows or max_rows <= 0:
        return
    try:
        sql = (
            "DELETE FROM user_logs WHERE user_id = %s AND stream = %s AND seq < ("
            "  SELECT MIN(seq) FROM ("
            "    SELECT seq FROM user_logs WHERE user_id = %s AND stream = %s "
            "    ORDER BY seq DESC LIMIT %s"
            "  ) t"
            ")"
        )
        params: list = [user_id, stream, user_id, stream, max_rows]
        if only_statuses:
            sql += " AND doc->>'status' = ANY(%s)"
            params.append(list(only_statuses))
        with get_pool().connection() as conn:
            conn.execute(sql, params)
    except Exception as e:
        log.error("[db] log_trim(%s,%s) failed: %s", user_id, stream, e)


def log_prune_older_than(user_id: str, stream: str, cutoff_epoch: float) -> None:
    """Delete rows whose ts is older than the cutoff. Rows with NULL ts are
    kept (those streams don't carry an epoch ts)."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM user_logs WHERE user_id = %s AND stream = %s "
                "AND ts IS NOT NULL AND ts < %s",
                (user_id, stream, cutoff_epoch),
            )
    except Exception as e:
        log.error("[db] log_prune_older_than(%s,%s) failed: %s", user_id, stream, e)


# ---------------------------------------------------------------------------
# Account reset
# ---------------------------------------------------------------------------


def delete_user_data(user_id: str) -> None:
    """Redundant DB belt: per-user 行现由 delete_user 的 CASCADE 原子清净
    (0011)。保留供 migrate_to_pg 等旧调用方；删账号主路径不再依赖它做 R2。"""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                for table in (
                    "chat_messages",
                    "memory_moments",
                    "world_book_entries",
                    "frame_envelopes",
                    "user_logs",
                    "user_blobs",
                    "perception_items",
                    "perception_daily",
                    "agent_runtime_instances",
                    "genesis_import_chunks",
                    "genesis_import_outputs",
                    "genesis_import_jobs",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
    except Exception as e:
        log.error("[db] delete_user_data(%s) failed: %s", user_id, e)


def delete_user_frames(user_id: str) -> None:
    """Best-effort R2 frame-body 清理(无 DB 行)。从 delete_user_data 拆出，
    使 DB 删除保持原子、R2 失败非致命。"""
    if object_storage.enabled():
        object_storage.delete_user_frames(user_id)
