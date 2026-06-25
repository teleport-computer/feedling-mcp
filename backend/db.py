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

import logging
import os
import threading
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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

    DELETE-all + reinsert, so it reflects removals too. NOTE: this is destructive
    from THIS worker's snapshot — under ``-w N`` it must not be used for ordinary
    per-user edits (a stale snapshot would wipe a user another worker just
    created). Genuine single-user edits go through ``registry.persist_user`` →
    ``db.upsert_user`` (per-row, non-destructive) instead; the remaining callers
    here read-then-rewrite their own full snapshot or run pre-fork at startup."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM users")
                for entry in users:
                    uid = entry.get("user_id")
                    if not uid:
                        continue
                    conn.execute(
                        "INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s)",
                        (uid, entry.get("created_at"), Jsonb(entry)),
                    )
    except Exception as e:
        log.error("[db] save_all_users failed: %s", e)


def delete_user(user_id: str) -> None:
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))


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


def list_agent_runtime_enabled_users() -> list[dict]:
    """Users opted into the hosted agent runtime (Stage C auto-discovery).

    A ``model_api`` config that is tested-ok and has hosting enabled
    (``agent_runtime_driver`` set to anything other than legacy/off, via POST
    /v1/model_api/driver). The agent is DERIVED from the provider, never chosen:
    anthropic/deepseek → claude, openai → codex (keep this CASE in sync with
    hosted/agent_runtime_cutover.driver_for_provider). Providers with no hosted
    fit (gemini/openrouter/…) are excluded. Returns ``[{"user_id", "driver"}]``
    sorted by user_id. The supervisor reads this directly (it already holds a DB
    pool for leases) to discover who to run instead of a static roster."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id,
                  CASE LOWER(COALESCE(doc->>'provider', ''))
                    WHEN 'anthropic' THEN 'claude'
                    WHEN 'claude'    THEN 'claude'
                    WHEN 'deepseek'  THEN 'claude'
                    WHEN 'openai'    THEN 'codex'
                  END AS driver
                FROM user_blobs
                WHERE kind = 'model_api'
                  AND COALESCE(doc->>'test_status', '') = 'ok'
                  AND LOWER(COALESCE(doc->>'agent_runtime_driver', 'legacy'))
                      NOT IN ('', 'legacy', 'off', 'false', '0', 'no', 'disabled')
                  AND LOWER(COALESCE(doc->>'provider', '')) IN ('anthropic', 'claude', 'deepseek', 'openai')
                ORDER BY user_id
                """,
            ).fetchall()
        return [{"user_id": uid, "driver": driver} for uid, driver in rows]
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
    user_id: str, msg_id: str, consumer_id: str, now: float, fields: dict
) -> dict | None:
    """Atomically claim a chat reply for ``consumer_id`` — the cross-worker-safe
    replacement for read-cache-then-write. The claim succeeds iff the row is
    currently unclaimed, already ours, or the prior claim has expired (the SQL
    WHERE mirrors chat.service._chat_message_claimable). Returns the merged doc
    on success, or None if the row is missing or another consumer/worker holds
    an unexpired claim — so two workers polling the same reply can't both win."""
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
                "  AND ("
                "    COALESCE(doc->>'reply_claimed_by','') = '' "
                "    OR doc->>'reply_claimed_by' = %s "
                "    OR COALESCE(NULLIF(doc->>'reply_claim_expires_at','')::float8, 0) <= %s"
                ") RETURNING doc",
                (Jsonb(fields), user_id, msg_id, consumer_id, now),
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


def memory_upsert(user_id: str, moment_id: str, occurred_at: str, doc: dict) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, moment_id) DO UPDATE SET "
                "occurred_at = EXCLUDED.occurred_at, doc = EXCLUDED.doc",
                (user_id, moment_id, occurred_at or "", Jsonb(doc)),
            )
    except Exception as e:
        log.error("[db] memory_upsert(%s,%s) failed: %s", user_id, moment_id, e)


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
# Frame envelopes (heavy body_ct lives here; frames_meta index stays a blob)
# ---------------------------------------------------------------------------


def frame_upsert(user_id: str, frame_id: str, ts: float, doc: dict) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO frame_envelopes (user_id, frame_id, ts, doc) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, frame_id) DO UPDATE SET ts = EXCLUDED.ts, doc = EXCLUDED.doc",
                (user_id, frame_id, float(ts), Jsonb(doc)),
            )
    except Exception as e:
        log.error("[db] frame_upsert(%s,%s) failed: %s", user_id, frame_id, e)


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
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT doc FROM frame_envelopes WHERE user_id = %s AND frame_id = %s",
                (user_id, frame_id),
            ).fetchone()
        return row[0] if row is not None else None
    except Exception as e:
        log.error("[db] frame_get(%s,%s) failed: %s", user_id, frame_id, e)
        return None


def frame_delete(user_id: str, frame_id: str) -> None:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM frame_envelopes WHERE user_id = %s AND frame_id = %s",
                (user_id, frame_id),
            )
    except Exception as e:
        log.error("[db] frame_delete(%s,%s) failed: %s", user_id, frame_id, e)


def frame_list_meta(user_id: str) -> list[dict]:
    """Reconstruct a lightweight frames_meta index from the stored envelopes.
    Used as the rebuild fallback when the frames_meta blob is missing."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT frame_id, ts, doc FROM frame_envelopes WHERE user_id = %s "
                "ORDER BY ts",
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
    """Hard-delete all per-user rows (everything except the global users row,
    which the caller removes via delete_user). Single transaction."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                for table in (
                    "chat_messages",
                    "memory_moments",
                    "frame_envelopes",
                    "user_logs",
                    "user_blobs",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
    except Exception as e:
        log.error("[db] delete_user_data(%s) failed: %s", user_id, e)
