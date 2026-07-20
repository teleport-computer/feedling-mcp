"""PostgreSQL persistence layer for the Feedling backend.

This module replaces the previous local-file persistence (JSON / JSONL files
under FEEDLING_DATA_DIR). The in-memory model (core.store's per-user
``UserStore`` cache plus accounts.registry) is unchanged: per-user
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
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def health_probe(timeout: float = 2.0) -> dict:
    """Fast liveness probe for /healthz.

    Acquire a pooled connection within ``timeout`` seconds and run ``SELECT 1``.
    Returns ``{"ok", "latency_ms", "error"}`` and NEVER raises. The short
    timeout is deliberate: the pool's default acquire wait is 10s, so a
    saturated/hung pool would otherwise block the health endpoint for that whole
    window. With a 2s cap, a pool that can't hand out a connection surfaces as
    ``ok=False`` fast, letting the caller report unhealthy instead of hanging.
    """
    t0 = time.perf_counter()
    try:
        with get_pool().connection(timeout=timeout) as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "error": None}
    except Exception as e:  # noqa: BLE001 — health must never raise
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": str(e)[:200],
        }


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

    The TEE mirror must carry the value RDS actually ended up with, not the
    candidate this call happened to propose: when the key already exists in
    RDS (the primary INSERT is a no-op) but the TEE row is absent/stale
    (dual-write was off during the original bootstrap, or a prior racer's
    candidate landed there), mirroring ``value`` would fork the shadow
    secret away from the primary. So we mirror the RDS-adopted value, using
    an upsert on the TEE side to correct any stale/divergent row.
    """
    sql = ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
           "ON CONFLICT (key) DO NOTHING RETURNING value")
    with get_pool().connection() as conn:
        with conn.transaction():
            inserted = conn.execute(sql, (key, value)).fetchone()
            row = inserted or conn.execute(
                "SELECT value FROM server_config WHERE key = %s", (key,)
            ).fetchone()
    adopted_value = bytes(row[0])
    from tee_shadow import mirror
    if inserted is not None:
        # Our candidate was adopted by RDS — mirror it as-is (current SQL/shape).
        mirror.execute(
            "INSERT INTO server_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (key, value),
        )
    else:
        # Key already existed in RDS — mirror the value RDS actually has, and
        # upsert so any stale/divergent TEE value gets corrected.
        mirror.execute(
            "INSERT INTO server_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, adopted_value),
        )
    return adopted_value


def set_config(key: str, value: bytes) -> None:
    """Unconditional upsert. Used by the migration script."""
    sql = ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    with get_pool().connection() as conn:
        conn.execute(sql, (key, value))
    from tee_shadow import mirror
    mirror.execute(sql, (key, value))


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
    sql = (
        "INSERT INTO agent_runtime_supervisor_heartbeats "
        "(owner, host, shard_index, shard_count, max_children, active_children, "
        " host_all, gateway, version, payload, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (owner) DO UPDATE SET "
        "  host = EXCLUDED.host, shard_index = EXCLUDED.shard_index, "
        "  shard_count = EXCLUDED.shard_count, max_children = EXCLUDED.max_children, "
        "  active_children = EXCLUDED.active_children, host_all = EXCLUDED.host_all, "
        "  gateway = EXCLUDED.gateway, version = EXCLUDED.version, "
        "  payload = EXCLUDED.payload, updated_at = now()"
    )
    params = (
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
    )
    with get_pool().connection() as conn:
        conn.execute(sql, params)
    from tee_shadow import mirror
    mirror.execute(sql, params)


def list_supervisor_instance_heartbeats() -> list[dict]:
    """All runner heartbeat rows. Each dict carries the typed flags plus ``ts``
    (the row's ``updated_at`` as an epoch float) so the caller can age-filter in
    pure code. Freshness/aggregation is the guard's job, not this query's. Raises
    on a DB error so the caller can fall back to the legacy key."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT owner, host, shard_index, shard_count, max_children, "
            "       active_children, host_all, gateway, version, "
            "       extract(epoch FROM updated_at) AS ts, payload "
            "FROM agent_runtime_supervisor_heartbeats"
        ).fetchall()
    out = []
    for r in rows:
        # ``pi`` has no promoted column (unlike host_all/gateway) — the supervisor
        # only writes it into ``payload``. It MUST be read back out, or the wedge
        # guard's ``hb.get("pi")`` is None → falsy → supervisor_pi_disabled → every
        # pi-driver send 503s. See test_supervisor_instance_heartbeat_roundtrips_
        # the_pi_capability_bit.
        payload = r[10] if isinstance(r[10], dict) else {}
        out.append({
            "owner": r[0], "host": r[1], "shard_index": r[2], "shard_count": r[3],
            "max_children": r[4], "active_children": r[5],
            "host_all": bool(r[6]), "gateway": bool(r[7]), "version": r[8],
            "ts": float(r[9]),
            "pi": bool(payload.get("pi")),
        })
    return out


def prune_supervisor_instance_heartbeats(max_age_sec: float) -> None:
    """Delete heartbeat rows older than ``max_age_sec`` (dead runners that never
    released). Best-effort housekeeping so the table doesn't accrete forever."""
    sql = ("DELETE FROM agent_runtime_supervisor_heartbeats "
           "WHERE updated_at < now() - make_interval(secs => %s)")
    params = (float(max_age_sec),)
    with get_pool().connection() as conn:
        conn.execute(sql, params)
    from tee_shadow import mirror
    mirror.execute(sql, params)


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
    sql = ("INSERT INTO global_blobs (key, doc) VALUES (%s, %s) "
           "ON CONFLICT (key) DO UPDATE SET doc = EXCLUDED.doc")
    try:
        with get_pool().connection() as conn:
            conn.execute(sql, (key, Jsonb(doc)))
    except Exception as e:
        log.error("[db] set_global_blob(%s) failed: %s", key, e)
        return
    from tee_shadow import mirror
    mirror.execute(sql, (key, Jsonb(doc)))


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
    sql = ("INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s) "
           "ON CONFLICT (user_id) DO NOTHING")
    params = (entry["user_id"], entry.get("created_at"), Jsonb(entry))
    with get_pool().connection() as conn:
        conn.execute(sql, params)
    from tee_shadow import mirror
    mirror.execute(sql, (entry["user_id"], entry.get("created_at"), Jsonb(entry)))


def upsert_user(entry: dict) -> None:
    """Insert-or-update one user document from the in-memory user dict (the
    source of truth after the caller mutates it under _users_lock)."""
    sql = ("INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s) "
           "ON CONFLICT (user_id) DO UPDATE SET created_at = EXCLUDED.created_at, doc = EXCLUDED.doc")
    with get_pool().connection() as conn:
        conn.execute(sql, (entry["user_id"], entry.get("created_at"), Jsonb(entry)))
    from tee_shadow import mirror
    mirror.execute(sql, (entry["user_id"], entry.get("created_at"), Jsonb(entry)))


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
    upsert_sql = ("INSERT INTO users (user_id, created_at, doc) VALUES (%s, %s, %s) "
                  "ON CONFLICT (user_id) DO UPDATE SET "
                  "created_at = EXCLUDED.created_at, doc = EXCLUDED.doc")
    mirror_group: list[tuple[str, tuple]] = []
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                keep_ids = [str(e.get("user_id")) for e in users if e.get("user_id")]
                # Remove only genuinely-absent users (empty snapshot ⇒ remove all).
                if keep_ids:
                    delete_sql, delete_params = (
                        "DELETE FROM users WHERE NOT (user_id = ANY(%s))", (keep_ids,))
                else:
                    delete_sql, delete_params = ("DELETE FROM users", ())
                conn.execute(delete_sql, delete_params)
                mirror_group.append((delete_sql, delete_params))
                for entry in users:
                    uid = entry.get("user_id")
                    if not uid:
                        continue
                    # Upsert (not plain INSERT): kept rows still exist, so a plain
                    # INSERT would hit the users PK. Upsert leaves child rows intact.
                    params = (uid, entry.get("created_at"), Jsonb(entry))
                    conn.execute(upsert_sql, params)
                    mirror_group.append((upsert_sql, params))
    except Exception as e:
        log.error("[db] save_all_users failed: %s", e)
        return
    from tee_shadow import mirror
    mirror.execute_many(mirror_group)


def delete_user(user_id: str) -> None:
    sql = "DELETE FROM users WHERE user_id = %s"
    with get_pool().connection() as conn:
        conn.execute(sql, (user_id,))
    from tee_shadow import mirror
    mirror.execute(sql, (user_id,))


# ---------------------------------------------------------------------------
# TEE shadow sync-run history (observability — migration 0015).
#
# The in-process auto-sync scheduler appends one row per tick so convergence /
# replication lag / dual-write failures / TEE liveness can be watched over the
# soak window (the cut-read go/no-go signal). Kept in RDS, not the TEE db, so a
# row is recordable even when the shadow is unreachable. See
# backend/admin/tee_sync_scheduler.py for the producer.
# ---------------------------------------------------------------------------

# Flattened metric columns written verbatim from the scheduler's summary dict;
# ``mirror_failures_delta`` and ``report`` are handled separately in the INSERT.
_TEE_SYNC_RUN_COLS = (
    "did_reconcile", "reconcile_ok", "verify_ran", "verify_ok",
    "unconverged_tables", "unconverged_users", "requeue_backlog",
    "replicate_copied", "replicate_pending", "replicate_errors", "replicate_skipped",
    "replicate_table_failures",
    "reconcile_copied", "reconcile_pruned", "reconcile_skipped",
    "mirror_failures", "tee_healthy", "tee_probe_ms", "duration_ms",
)


def record_tee_sync_run(summary: dict) -> None:
    """Append one row to ``tee_sync_runs``. Best-effort: recording a metric must
    never break the sync loop, so any failure is swallowed+logged (matching the
    shadow-era discipline everywhere else in the pipeline).

    ``mirror_failures`` is a cumulative in-process counter that zeroes on
    restart; ``mirror_failures_delta`` is computed here against the previous
    persisted row and clamped to >=0 (a negative delta means a restart happened,
    so the current value IS the count since restart)."""
    try:
        report_json = json.dumps(summary.get("report") or {}, default=str, ensure_ascii=False)
        vals = [summary.get(c) for c in _TEE_SYNC_RUN_COLS]
        cols = ", ".join(_TEE_SYNC_RUN_COLS)
        ph = ", ".join(["%s"] * len(_TEE_SYNC_RUN_COLS))
        with get_pool().connection() as conn:
            last = conn.execute(
                "SELECT mirror_failures FROM tee_sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cur = int(summary.get("mirror_failures") or 0)
            delta = cur - int(last[0]) if last else cur
            if delta < 0:
                delta = cur
            conn.execute(
                f"INSERT INTO tee_sync_runs ({cols}, mirror_failures_delta, report) "
                f"VALUES ({ph}, %s, %s::jsonb)",
                (*vals, delta, report_json),
            )
    except Exception as e:  # noqa: BLE001 — metrics must not break the loop
        log.error("[db] record_tee_sync_run failed: %s", e)


def mark_reconcile_success() -> None:
    """Stamp ``tee_reconcile_state`` the moment a reconcile pass completes (see
    alembic 0019). Called from the scheduler right after reconcile, BEFORE the
    slow replicate/verify — so a worker recycled mid-tick still leaves reconcile
    marked done and the next leader skips reconcile-first instead of re-running it
    and starving replicate. Best-effort: a failed stamp only costs one extra
    reconcile, never breaks the loop."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO tee_reconcile_state (id, last_success_at) VALUES (TRUE, now()) "
                "ON CONFLICT (id) DO UPDATE SET last_success_at = now()")
    except Exception as e:  # noqa: BLE001
        log.warning("[db] mark_reconcile_success failed: %s", e)


def last_tee_reconcile_age_sec() -> float | None:
    """Seconds since the last completed reconcile pass (``mark_reconcile_success``),
    or None if there has never been one. Read by the tee-sync scheduler at loop
    start so a new leader (gunicorn worker recycle) does NOT redo reconcile-first
    when one completed recently — a full reconcile outlasts a max_requests worker
    lifetime, so without this it never finished (2026-07-14 test: 2h of leader
    churn, zero completed ticks). Sourced from tee_reconcile_state (stamped at
    reconcile completion) rather than the end-of-tick tee_sync_runs row, because
    the tick often dies in the slow replicate phase before that row is written
    (2026-07-15 prod). Age is computed server-side so client clock skew is moot."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - last_success_at)) "
            "FROM tee_reconcile_state WHERE id"
        ).fetchone()
    if not row or row[0] is None:
        return None
    return max(0.0, float(row[0]))


def recent_tee_sync_runs(limit: int = 50) -> list[dict]:
    """Most-recent TEE sync summaries, newest first (observability endpoint).
    ``ran_at`` is ISO-8601; ``report`` is the parsed JSONB detail dict."""
    cols = ("id", "ran_at") + _TEE_SYNC_RUN_COLS + ("mirror_failures_delta", "report")
    sel = ", ".join(cols)
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {sel} FROM tee_sync_runs ORDER BY id DESC LIMIT %s", (limit,)
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(zip(cols, r))
        ran = d.get("ran_at")
        if ran is not None and hasattr(ran, "isoformat"):
            d["ran_at"] = ran.isoformat()
        out.append(d)
    return out


# --- TEE reconcile resume cursors (see alembic 0018) ------------------------ #
# A per-table keyset checkpoint so a backfill interrupted by a worker recycle /
# deploy / crash resumes where it left off instead of restarting from row 1.
# All three are best-effort: a cursor read/write failure only forfeits
# resumability for that pass, it must never break the reconcile itself.

def save_reconcile_cursor(table: str, cursor_pk: list) -> None:
    """Persist ``cursor_pk`` (a JSON-serializable list = the last-copied pk) as
    the resume point for ``table``'s reconcile backfill."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO tee_reconcile_cursors (table_name, cursor_pk, updated_at) "
                "VALUES (%s, %s::jsonb, now()) ON CONFLICT (table_name) DO UPDATE SET "
                "cursor_pk = EXCLUDED.cursor_pk, updated_at = now()",
                (table, json.dumps(cursor_pk)),
            )
    except Exception as e:  # noqa: BLE001 — resume is an optimization, never a gate
        log.warning("[db] save_reconcile_cursor(%s) failed: %s", table, e)


def load_reconcile_cursor(table: str) -> list | None:
    """The persisted last-copied pk for ``table`` (a list), or None to start from
    the top. Read failure degrades to None = current restart-from-scratch."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT cursor_pk FROM tee_reconcile_cursors WHERE table_name = %s",
                (table,)).fetchone()
        return list(row[0]) if row and row[0] is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("[db] load_reconcile_cursor(%s) failed: %s", table, e)
        return None


def clear_reconcile_cursor(table: str) -> None:
    """Drop ``table``'s resume cursor once its backfill pass completes, so the
    next periodic reconcile starts fresh (and re-catches any missed updates)."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM tee_reconcile_cursors WHERE table_name = %s", (table,))
    except Exception as e:  # noqa: BLE001
        log.warning("[db] clear_reconcile_cursor(%s) failed: %s", table, e)


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
    them. The returned shape is consumed by the data-track surface in
    admin/data_track.py (routes wired in admin/routes_asgi.py).
    """
    ids = [str(uid) for uid in user_ids if uid]
    if not ids:
        return {}

    def ensure(out: dict[str, dict], uid: str) -> dict:
        return out.setdefault(uid, {})

    out: dict[str, dict] = {
        uid: {"app_usage": {"foreground_sec": 0, "sessions": 0, "last_at": None}}
        for uid in ids
    }
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id,
                       COUNT(*)::int AS total,
                       COUNT(*) FILTER (
                         WHERE doc->>'role' = 'user'
                           AND COALESCE(doc->>'source', '') NOT IN ('verify_ping', 'resident_maintenance')
                       )::int AS user_messages,
                       COUNT(*) FILTER (WHERE doc->>'role' IN ('agent', 'openclaw'))::int AS agent_messages,
                       COUNT(*) FILTER (WHERE doc->>'content_type' = 'image')::int AS image_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'agent_initiated_proactive')::int AS proactive_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'model_api' AND doc->>'role' = 'user')::int AS model_api_user_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'model_api' AND doc->>'role' IN ('agent', 'openclaw'))::int AS model_api_agent_messages,
                       COUNT(*) FILTER (WHERE doc->>'source' = 'model_api' AND doc->>'model_api_kind' = 'onboarding_greeting')::int AS model_api_greetings,
                       MIN(ts) AS first_ts,
                       MAX(ts) AS last_ts,
                       MAX(ts) FILTER (WHERE doc->>'source' = 'agent_initiated_proactive') AS proactive_last_ts,
                       MAX(ts) FILTER (
                         WHERE doc->>'role' = 'user'
                           AND COALESCE(doc->>'source', '') NOT IN ('verify_ping', 'resident_maintenance')
                       ) AS last_user_ts,
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
                       COALESCE(SUM(
                         CASE
                           WHEN doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'
                           THEN (doc->'payload'->>'duration_sec')::bigint
                           ELSE 0
                         END
                       ), 0)::bigint AS foreground_sec,
                       COUNT(*)::int AS sessions,
                       MAX(ts) AS last_at
                FROM user_logs
                WHERE user_id = ANY(%s)
                  AND stream = 'tracking_events'
                  AND doc->>'type' = 'app_session_end'
                GROUP BY user_id
                """,
                (ids,),
            ).fetchall()
            for uid, foreground_sec, sessions, last_at in rows:
                ensure(out, uid)["app_usage"] = {
                    "foreground_sec": int(foreground_sec or 0),
                    "sessions": int(sessions or 0),
                    "last_at": last_at,
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
                       COALESCE(NULLIF(BTRIM(doc->>'status_reason'), ''), 'unknown') AS reason,
                       COUNT(*)::int
                FROM user_logs
                WHERE user_id = ANY(%s)
                  AND stream = 'proactive_jobs'
                  AND doc->>'status' IN ('failed', 'skipped')
                GROUP BY user_id, reason
                """,
                (ids,),
            ).fetchall()
            for uid, reason, count in rows:
                ensure(out, uid).setdefault("proactive_extra", {}).setdefault(
                    "jobs_failed_by_reason", {}
                )[reason] = count

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


def _dau_row(row) -> dict:
    return {
        "day": row[0],
        "dau": int(row[1] or 0),
        "chat_dau": int(row[2] or 0),
        "tracking_dau": int(row[3] or 0),
        "active_events": int(row[4] or 0),
        "user_messages": int(row[5] or 0),
        "tracking_events": int(row[6] or 0),
        "first_ts": row[7],
        "last_ts": row[8],
        "avg_session_sec": float(row[9] or 0),
        "foreground_sec": int(row[10] or 0),
        "session_count": int(row[11] or 0),
        "session_dau": int(row[12] or 0),
        "median_user_sec": float(row[13] or 0),
        "frozen": bool(row[14]),
    }


def admin_data_track_dau(*, since_epoch: float = 0.0, days: int = 30, tz: str = "Asia/Shanghai") -> list[dict]:
    """Return daily active-user aggregates, preferring immutable snapshots.

    A completed day uses ``dau_daily_snapshot`` once frozen. Today and days
    before the snapshot boundary remain live. If ``since_epoch`` cuts through
    a frozen day, that day also falls back to live data so the existing exact
    timestamp-filter contract is preserved. Every row exposes ``frozen``.

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
                      AND COALESCE(doc->>'source', '') NOT IN ('verify_ping', 'resident_maintenance')
                      AND (%s = 0 OR ts >= %s)

                    UNION ALL

                    SELECT user_id, ts, 'tracking' AS source
                    FROM user_logs
                    WHERE stream = 'tracking_events'
                      AND ts IS NOT NULL
                      AND (%s = 0 OR ts >= %s)
                ),
                usage_events AS (
                    SELECT
                        user_id,
                        ts,
                        CASE
                          WHEN doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'
                          THEN (doc->'payload'->>'duration_sec')::bigint
                          ELSE 0
                        END AS duration_sec
                    FROM user_logs
                    WHERE stream = 'tracking_events'
                      AND doc->>'type' = 'app_session_end'
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
                ),
                usage_daily AS (
                    SELECT
                        to_char(timezone(%s, to_timestamp(ts)), 'YYYY-MM-DD') AS day,
                        COALESCE(AVG(duration_sec), 0)::double precision AS avg_session_sec,
                        COALESCE(SUM(duration_sec), 0)::bigint AS foreground_sec,
                        COUNT(*)::int AS session_count,
                        COUNT(DISTINCT user_id)::int AS session_dau
                    FROM usage_events
                    GROUP BY day
                ),
                -- Per-user daily foreground total, then the median across users:
                -- the "typical user" counterpart to foreground_sec/session_dau
                -- (a mean that a few heavy users skew high). Frozen snapshots
                -- store only aggregates, so this only populates for live days
                -- and days frozen after this column shipped.
                usage_per_user AS (
                    SELECT
                        to_char(timezone(%s, to_timestamp(ts)), 'YYYY-MM-DD') AS day,
                        user_id,
                        SUM(duration_sec) AS user_sec
                    FROM usage_events
                    GROUP BY day, user_id
                ),
                usage_median AS (
                    SELECT day,
                           COALESCE(
                             percentile_cont(0.5) WITHIN GROUP (ORDER BY user_sec), 0
                           )::double precision AS median_user_sec
                    FROM usage_per_user
                    GROUP BY day
                ),
                live AS (
                    SELECT d.day, d.dau, d.chat_dau, d.tracking_dau, d.active_events,
                           d.user_messages, d.tracking_events, d.first_ts, d.last_ts,
                           COALESCE(u.avg_session_sec, 0)::double precision AS avg_session_sec,
                           COALESCE(u.foreground_sec, 0)::bigint AS foreground_sec,
                           COALESCE(u.session_count, 0)::int AS session_count,
                           COALESCE(u.session_dau, 0)::int AS session_dau,
                           COALESCE(m.median_user_sec, 0)::double precision AS median_user_sec
                    FROM daily d
                    LEFT JOIN usage_daily u ON u.day = d.day
                    LEFT JOIN usage_median m ON m.day = d.day
                ),
                frozen_rows AS (
                    SELECT day, dau, chat_dau, tracking_dau, active_events,
                           user_messages, tracking_events, first_ts, last_ts,
                           avg_session_sec, foreground_sec, session_count, session_dau,
                           median_user_sec
                    FROM dau_daily_snapshot
                    WHERE active_events > 0
                      AND (%s = 0 OR first_ts >= %s)
                ),
                merged AS (
                    SELECT f.*, TRUE AS frozen FROM frozen_rows f
                    UNION ALL
                    SELECT l.*, FALSE AS frozen
                    FROM live l
                    WHERE NOT EXISTS (SELECT 1 FROM frozen_rows f WHERE f.day = l.day)
                )
                SELECT day, dau, chat_dau, tracking_dau, active_events,
                       user_messages, tracking_events, first_ts, last_ts,
                       avg_session_sec, foreground_sec, session_count, session_dau,
                       median_user_sec, frozen
                FROM merged
                ORDER BY day DESC
                LIMIT %s
                """,
                (since, since, since, since, since, since, tz, tz, tz, since, since, day_limit),
            ).fetchall()
        return [_dau_row(row) for row in rows]
    except Exception as e:
        log.error("[db] admin_data_track_dau failed: %s", e)
        return []


def _completed_dau_row(conn, *, day: date, tz: str) -> dict:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone).timestamp()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone).timestamp()
    row = conn.execute(
        """
        WITH active AS (
            SELECT user_id, ts, 'chat' AS source
            FROM chat_messages
            WHERE doc->>'role' = 'user'
              AND COALESCE(doc->>'source', '') NOT IN ('verify_ping', 'resident_maintenance')
              AND ts >= %s AND ts < %s

            UNION ALL

            SELECT user_id, ts, 'tracking' AS source
            FROM user_logs
            WHERE stream = 'tracking_events'
              AND ts IS NOT NULL
              AND ts >= %s AND ts < %s
        ),
        usage_events AS (
            SELECT
                user_id,
                CASE
                  WHEN doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'
                  THEN (doc->'payload'->>'duration_sec')::bigint
                  ELSE 0
                END AS duration_sec
            FROM user_logs
            WHERE stream = 'tracking_events'
              AND doc->>'type' = 'app_session_end'
              AND ts IS NOT NULL
              AND ts >= %s AND ts < %s
        )
        SELECT
            COUNT(DISTINCT user_id)::int AS dau,
            (COUNT(DISTINCT user_id) FILTER (WHERE source = 'chat'))::int AS chat_dau,
            (COUNT(DISTINCT user_id) FILTER (WHERE source = 'tracking'))::int AS tracking_dau,
            COUNT(*)::int AS active_events,
            (COUNT(*) FILTER (WHERE source = 'chat'))::int AS user_messages,
            (COUNT(*) FILTER (WHERE source = 'tracking'))::int AS tracking_events,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts,
            COALESCE((SELECT AVG(duration_sec) FROM usage_events), 0)::double precision,
            COALESCE((SELECT SUM(duration_sec) FROM usage_events), 0)::bigint,
            (SELECT COUNT(*)::int FROM usage_events),
            (SELECT COUNT(DISTINCT user_id)::int FROM usage_events),
            COALESCE((
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY user_sec)
                FROM (
                    SELECT SUM(duration_sec) AS user_sec
                    FROM usage_events
                    GROUP BY user_id
                ) per_user
            ), 0)::double precision
        FROM active
        """,
        (start, end, start, end, start, end),
    ).fetchone()
    return {
        "day": day.isoformat(),
        "dau": int(row[0] or 0),
        "chat_dau": int(row[1] or 0),
        "tracking_dau": int(row[2] or 0),
        "active_events": int(row[3] or 0),
        "user_messages": int(row[4] or 0),
        "tracking_events": int(row[5] or 0),
        "first_ts": row[6],
        "last_ts": row[7],
        "avg_session_sec": float(row[8] or 0),
        "foreground_sec": int(row[9] or 0),
        "session_count": int(row[10] or 0),
        "session_dau": int(row[11] or 0),
        "median_user_sec": float(row[12] or 0),
    }


def freeze_completed_dau_days(*, now_epoch: float | None = None,
                              tz: str = "Asia/Shanghai") -> list[str]:
    """Insert immutable snapshots for completed Beijing days.

    The first successful run freezes yesterday only, establishing the rollout
    boundary without pretending older, already-understated history is exact.
    Later runs fill every missing day from that boundary through yesterday.
    Zero-activity rows are stored to preserve the boundary but omitted from the
    DAU read API. ``ON CONFLICT DO NOTHING`` makes concurrent ticks write-once.
    """
    try:
        zone = ZoneInfo(tz)
        now = datetime.fromtimestamp(float(now_epoch), zone) if now_epoch is not None else datetime.now(zone)
        last_completed = now.date() - timedelta(days=1)
        with get_pool().connection() as conn:
            row = conn.execute("SELECT MIN(day) FROM dau_daily_snapshot").fetchone()
            first_day = date.fromisoformat(row[0]) if row and row[0] else last_completed
            if first_day > last_completed:
                return []
            existing = {
                date.fromisoformat(r[0])
                for r in conn.execute(
                    "SELECT day FROM dau_daily_snapshot WHERE day >= %s AND day <= %s",
                    (first_day.isoformat(), last_completed.isoformat()),
                ).fetchall()
            }
            inserted: list[str] = []
            cursor = first_day
            while cursor <= last_completed:
                if cursor not in existing:
                    snap = _completed_dau_row(conn, day=cursor, tz=tz)
                    saved = conn.execute(
                        """
                        INSERT INTO dau_daily_snapshot (
                            day, dau, chat_dau, tracking_dau, active_events,
                            user_messages, tracking_events, session_dau,
                            avg_session_sec, foreground_sec, session_count,
                            first_ts, last_ts, median_user_sec
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (day) DO NOTHING
                        RETURNING day
                        """,
                        (
                            snap["day"], snap["dau"], snap["chat_dau"],
                            snap["tracking_dau"], snap["active_events"],
                            snap["user_messages"], snap["tracking_events"],
                            snap["session_dau"], snap["avg_session_sec"],
                            snap["foreground_sec"], snap["session_count"],
                            snap["first_ts"], snap["last_ts"], snap["median_user_sec"],
                        ),
                    ).fetchone()
                    if saved:
                        inserted.append(saved[0])
                cursor += timedelta(days=1)
        return inserted
    except Exception as e:
        log.error("[db] freeze_completed_dau_days failed: %s", e)
        return []


def admin_dau_snapshot_bounds() -> dict:
    """Return the immutable snapshot range for admin rendering."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT MIN(day), MAX(day), COUNT(*) FROM dau_daily_snapshot"
            ).fetchone()
        return {
            "first_day": row[0] or "",
            "last_day": row[1] or "",
            "days": int(row[2] or 0),
        }
    except Exception as e:
        log.error("[db] admin_dau_snapshot_bounds failed: %s", e)
        return {"first_day": "", "last_day": "", "days": 0}


# --- User growth (new + cumulative signups) ------------------------------- #
# ``users.created_at`` is a naive server-local ISO string; cast ::timestamptz
# (session tz) then bucket into Beijing days, matching the onboarding funnel's
# EXTRACT(EPOCH FROM created_at::timestamptz) contract. Frozen days come from
# user_growth_daily_snapshot (deletion-proof); pre-boundary days are computed
# live and understate because deleted accounts drop out of ``users``.
_CREATED_AT_ISO = "created_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'"


def admin_data_track_growth(*, days: int = 60, tz: str = "Asia/Shanghai") -> list[dict]:
    """Per-Beijing-day new signups + running cumulative, newest last-limited.

    Cumulative is a running sum over the FULL series so the returned tail carries
    a correct total; only the last ``days`` rows are returned for display.
    """
    day_limit = max(1, min(int(days or 60), 366))
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                f"""
                WITH reg AS (
                    SELECT to_char(timezone(%s, created_at::timestamptz), 'YYYY-MM-DD') AS day
                    FROM users
                    WHERE {_CREATED_AT_ISO}
                ),
                live AS (
                    SELECT day, COUNT(*)::int AS new_users FROM reg GROUP BY day
                ),
                frozen_rows AS (
                    SELECT day, new_users FROM user_growth_daily_snapshot
                ),
                merged AS (
                    SELECT day, new_users, TRUE AS frozen FROM frozen_rows
                    UNION ALL
                    SELECT l.day, l.new_users, FALSE AS frozen
                    FROM live l
                    WHERE NOT EXISTS (SELECT 1 FROM frozen_rows f WHERE f.day = l.day)
                )
                SELECT day, new_users, frozen FROM merged ORDER BY day ASC
                """,
                (tz,),
            ).fetchall()
        cumulative = 0
        out: list[dict] = []
        for day, new_users, frozen in rows:
            cumulative += int(new_users or 0)
            out.append({
                "day": day,
                "new_users": int(new_users or 0),
                "cumulative": cumulative,
                "frozen": bool(frozen),
            })
        return out[-day_limit:]
    except Exception as e:
        log.error("[db] admin_data_track_growth failed: %s", e)
        return []


def _completed_growth_row(conn, *, day: date, tz: str) -> dict:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone).timestamp()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone).timestamp()
    row = conn.execute(
        f"""
        SELECT COUNT(*)::int
        FROM users
        WHERE {_CREATED_AT_ISO}
          AND EXTRACT(EPOCH FROM created_at::timestamptz) >= %s
          AND EXTRACT(EPOCH FROM created_at::timestamptz) < %s
        """,
        (start, end),
    ).fetchone()
    return {"day": day.isoformat(), "new_users": int(row[0] or 0)}


def freeze_completed_growth_days(*, now_epoch: float | None = None,
                                 tz: str = "Asia/Shanghai") -> list[str]:
    """Freeze immutable new-signup counts for completed Beijing days.

    Mirrors ``freeze_completed_dau_days``: the first run establishes the rollout
    boundary at yesterday; later runs fill the gap; ``ON CONFLICT DO NOTHING``
    keeps it write-once so account deletion can never change a frozen day.
    """
    try:
        zone = ZoneInfo(tz)
        now = datetime.fromtimestamp(float(now_epoch), zone) if now_epoch is not None else datetime.now(zone)
        last_completed = now.date() - timedelta(days=1)
        with get_pool().connection() as conn:
            row = conn.execute("SELECT MIN(day) FROM user_growth_daily_snapshot").fetchone()
            first_day = date.fromisoformat(row[0]) if row and row[0] else last_completed
            if first_day > last_completed:
                return []
            existing = {
                date.fromisoformat(r[0])
                for r in conn.execute(
                    "SELECT day FROM user_growth_daily_snapshot WHERE day >= %s AND day <= %s",
                    (first_day.isoformat(), last_completed.isoformat()),
                ).fetchall()
            }
            inserted: list[str] = []
            cursor = first_day
            while cursor <= last_completed:
                if cursor not in existing:
                    snap = _completed_growth_row(conn, day=cursor, tz=tz)
                    saved = conn.execute(
                        """
                        INSERT INTO user_growth_daily_snapshot (day, new_users)
                        VALUES (%s, %s)
                        ON CONFLICT (day) DO NOTHING
                        RETURNING day
                        """,
                        (snap["day"], snap["new_users"]),
                    ).fetchone()
                    if saved:
                        inserted.append(saved[0])
                cursor += timedelta(days=1)
        return inserted
    except Exception as e:
        log.error("[db] freeze_completed_growth_days failed: %s", e)
        return []


def admin_growth_snapshot_bounds() -> dict:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT MIN(day), MAX(day), COUNT(*) FROM user_growth_daily_snapshot"
            ).fetchone()
        return {"first_day": row[0] or "", "last_day": row[1] or "", "days": int(row[2] or 0)}
    except Exception as e:
        log.error("[db] admin_growth_snapshot_bounds failed: %s", e)
        return {"first_day": "", "last_day": "", "days": 0}


# --- Weekly cohort retention ---------------------------------------------- #
# Cohort = the Beijing week a user registered in (Monday date). period_index k =
# weeks since registration (0 = the signup week itself). A cell (W,k) freezes
# once week k has fully ended; active = the same user-initiated activity DAU
# uses (user chat message OR tracking event). cohort_size is pinned to the value
# at the cohort's first frozen cell so deletion can't shrink the denominator and
# fake-inflate retention.

def _retention_cells(conn, tz: str) -> tuple[dict, dict]:
    """(cohort_size_by_week, active_count_by_(week,period)) computed live now."""
    size_rows = conn.execute(
        f"""
        SELECT to_char((date_trunc('week', timezone(%s, created_at::timestamptz)))::date,
                       'YYYY-MM-DD') AS cohort_week,
               COUNT(*)::int AS cohort_size
        FROM users
        WHERE {_CREATED_AT_ISO}
        GROUP BY cohort_week
        """,
        (tz,),
    ).fetchall()
    sizes = {r[0]: int(r[1] or 0) for r in size_rows}

    cell_rows = conn.execute(
        f"""
        WITH reg AS (
            SELECT user_id,
                   (date_trunc('week', timezone(%s, created_at::timestamptz)))::date AS cohort_monday
            FROM users
            WHERE {_CREATED_AT_ISO}
        ),
        act AS (
            SELECT DISTINCT r.user_id, r.cohort_monday,
                   (date_trunc('week', timezone(%s, to_timestamp(a.ts))))::date AS act_monday
            FROM reg r
            JOIN (
                SELECT user_id, ts FROM chat_messages
                  WHERE doc->>'role' = 'user'
                    AND COALESCE(doc->>'source', '') NOT IN ('verify_ping', 'resident_maintenance')
                UNION ALL
                SELECT user_id, ts FROM user_logs
                  WHERE stream = 'tracking_events' AND ts IS NOT NULL
            ) a ON a.user_id = r.user_id
        )
        SELECT to_char(cohort_monday, 'YYYY-MM-DD') AS cohort_week,
               ((act_monday - cohort_monday) / 7)::int AS period_index,
               COUNT(DISTINCT user_id)::int AS active_count
        FROM act
        WHERE act_monday >= cohort_monday
        GROUP BY cohort_monday, period_index
        """,
        (tz, tz),
    ).fetchall()
    cells = {(r[0], int(r[1])): int(r[2] or 0) for r in cell_rows}
    return sizes, cells


def admin_data_track_retention(*, tz: str = "Asia/Shanghai") -> dict:
    """Weekly cohort retention matrix. Frozen cells win (deletion-proof); the
    current in-progress period falls back to live. Returns
    {cohorts: [{cohort_week, cohort_size, cells: {k: {active, pct, frozen}}}],
     max_period}."""
    try:
        with get_pool().connection() as conn:
            frozen = conn.execute(
                "SELECT cohort_week, period_index, cohort_size, active_count "
                "FROM retention_cohort_snapshot"
            ).fetchall()
            sizes, live_cells = _retention_cells(conn, tz)

        frozen_cells = {(r[0], int(r[1])): (int(r[2] or 0), int(r[3] or 0)) for r in frozen}
        # Denominator per cohort: prefer a frozen anchor (pinned size), else live.
        anchor: dict[str, int] = {}
        for (week, _period), (size, _active) in frozen_cells.items():
            anchor.setdefault(week, size)
        weeks = sorted(set(sizes) | {w for (w, _p) in frozen_cells})

        cohorts = []
        max_period = 0
        for week in weeks:
            size = anchor.get(week, sizes.get(week, 0))
            cells: dict[int, dict] = {}
            periods = {p for (w, p) in frozen_cells if w == week} | {
                p for (w, p) in live_cells if w == week
            }
            for p in periods:
                if (week, p) in frozen_cells:
                    active = frozen_cells[(week, p)][1]
                    is_frozen = True
                else:
                    active = live_cells.get((week, p), 0)
                    is_frozen = False
                pct = round(100.0 * active / size, 1) if size else 0.0
                cells[p] = {"active": active, "pct": pct, "frozen": is_frozen}
                max_period = max(max_period, p)
            cohorts.append({"cohort_week": week, "cohort_size": size, "cells": cells})
        return {"cohorts": cohorts, "max_period": max_period}
    except Exception as e:
        log.error("[db] admin_data_track_retention failed: %s", e)
        return {"cohorts": [], "max_period": 0}


def freeze_completed_retention_cohorts(*, now_epoch: float | None = None,
                                       tz: str = "Asia/Shanghai") -> list[str]:
    """Freeze (cohort_week, period) cells whose week has fully ended.

    cohort_size is pinned to the cohort's earliest already-frozen cell so every
    period shares one deletion-proof denominator. Returns the "week#period" keys
    inserted. Best-effort per the scheduler contract.
    """
    try:
        zone = ZoneInfo(tz)
        now = datetime.fromtimestamp(float(now_epoch), zone) if now_epoch is not None else datetime.now(zone)
        now_date = now.date()
        inserted: list[str] = []
        with get_pool().connection() as conn:
            sizes, live_cells = _retention_cells(conn, tz)
            existing = {
                (r[0], int(r[1])): int(r[2] or 0)
                for r in conn.execute(
                    "SELECT cohort_week, period_index, cohort_size FROM retention_cohort_snapshot"
                ).fetchall()
            }
            # Pin denominator per cohort to the earliest frozen cell if any.
            anchor: dict[str, int] = {}
            for (week, period) in sorted(existing, key=lambda k: k[1]):
                anchor.setdefault(week, existing[(week, period)])

            # Freeze cohorts that still have live users OR an existing anchor. The
            # anchor branch is essential: once a cohort's W0 is frozen, its whole
            # membership can delete and it drops out of `sizes` — without this it
            # would never get later-period 0 cells, HIDING total churn (the exact
            # opposite of the deletion-proofing goal). A cohort deleted before its
            # first freeze is genuinely unrecoverable and stays absent.
            for week in set(sizes) | set(anchor):
                cohort_monday = date.fromisoformat(week)
                # periods 0..K-1 are complete: week k ends at monday + (k+1)*7 days.
                completed = (now_date - cohort_monday).days // 7
                if completed <= 0:
                    continue
                size = anchor.get(week, sizes.get(week, 0))
                for k in range(completed):
                    if (week, k) in existing:
                        continue
                    active = live_cells.get((week, k), 0)
                    saved = conn.execute(
                        """
                        INSERT INTO retention_cohort_snapshot
                            (cohort_week, period_index, cohort_size, active_count)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (cohort_week, period_index) DO NOTHING
                        RETURNING cohort_week
                        """,
                        (week, k, size, active),
                    ).fetchone()
                    if saved:
                        inserted.append(f"{week}#{k}")
                        anchor.setdefault(week, size)
            return inserted
    except Exception as e:
        log.error("[db] freeze_completed_retention_cohorts failed: %s", e)
        return []


def admin_data_track_proactive_daily(*, since_epoch: float = 0.0, days: int = 30,
                                     tz: str = "Asia/Shanghai") -> list[dict]:
    """Per-Beijing-day proactive-job aggregates for the ops trend view.

    Answers "is the proactive success rate improving day over day". 只有面向
    用户的 wake lane 进成功率口径：``delivered``/``failed``/``skipped``/
    ``pending`` 均不含 memory-maintenance（capture/dream/migrate）jobs——那些
    永远不产生 delivered，坏一个用户的 key 就能无限灌 failed（2026-07-05
    prod：40 用户的重试风暴把整体成功率打到 3%）。maintenance 单独成列。
    ``failed`` 只含 status='failed'；gate 拒绝的 ``skipped``（用户关 ambient）
    是产品行为不是失败，单独计数。``completed``（醒了、正常决策、只是没发
    消息——sleep/纯动作）算成功：口径衡量「系统是否健康」，不是「醒了的里面
    有多少真正送达」。成功率由调用方算
    （(delivered+completed) / (delivered+completed+failed)）。"""
    day_limit = max(1, min(int(days or 30), 366))
    since = float(since_epoch or 0.0)
    screen_kinds = "('screen_watch','scene_change','screen_tick','broadcast_opened','heartbeat_broadcast_on')"
    maintenance_kinds = "('memory_capture','memory_dream','memory_migrate')"
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
                       (COUNT(*) FILTER (WHERE kind NOT IN {maintenance_kinds}
                                          AND status IN ('posted','delivered')))::int AS delivered,
                       (COUNT(*) FILTER (WHERE kind NOT IN {maintenance_kinds}
                                          AND status = 'completed'))::int AS completed,
                       (COUNT(*) FILTER (WHERE kind NOT IN {maintenance_kinds}
                                          AND status = 'failed'))::int AS failed,
                       (COUNT(*) FILTER (WHERE kind NOT IN {maintenance_kinds}
                                          AND status = 'skipped'))::int AS skipped,
                       (COUNT(*) FILTER (WHERE kind NOT IN {maintenance_kinds}
                                          AND status = 'pending'))::int AS pending,
                       (COUNT(*) FILTER (WHERE kind IN {maintenance_kinds}))::int AS maintenance,
                       (COUNT(*) FILTER (WHERE kind IN {maintenance_kinds}
                                          AND status IN ('failed','skipped')))::int AS maintenance_failed,
                       (COUNT(*) FILTER (WHERE kind IN {screen_kinds}))::int AS screen,
                       -- 自发 tick：现网 kind 是 'presence'，heartbeat* 为历史 kind
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
                "day": r[0], "jobs": r[1], "delivered": r[2], "completed": r[3],
                "failed": r[4], "skipped": r[5], "pending": r[6], "maintenance": r[7],
                "maintenance_failed": r[8], "screen": r[9], "heartbeat": r[10],
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

    # 4) 回复消息: 真回复率 + 兜底率 + 回复延迟(中位)。real_replies 排除
    #    agent_initiated_proactive(主动消息不是"对用户的回复")。latency = 每条真回复
    #    与其前一条用户消息的时间差(窗口配对)。
    rows = _run("reply", f"""
        {_EVENTS_ROUTES_CTE}, paired AS (
          SELECT c.user_id, c.ts, c.doc->>'role' AS role, COALESCE(c.doc->>'source','') AS src,
            MAX(CASE WHEN c.doc->>'role'='user' AND COALESCE(c.doc->>'source','') NOT IN ('verify_ping','resident_maintenance') THEN c.ts END)
              OVER (PARTITION BY c.user_id ORDER BY c.ts ROWS UNBOUNDED PRECEDING) AS last_user_ts
          FROM chat_messages c
        )
        SELECT COALESCE(r.route,'resident') AS route,
               (COUNT(*) FILTER (WHERE p.role='user' AND p.src NOT IN ('verify_ping','resident_maintenance')))::int AS user_msgs,
               (COUNT(DISTINCT p.last_user_ts) FILTER (WHERE p.role IN ('agent','openclaw')
                    AND p.src NOT IN ('foreground_fallback','proactive_fallback','agent_initiated_proactive')
                    AND p.last_user_ts IS NOT NULL))::int AS real_replies,
               (COUNT(*) FILTER (WHERE p.src='foreground_fallback'))::int AS fallback_replies,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY
                 CASE WHEN p.role IN ('agent','openclaw')
                      AND p.src NOT IN ('foreground_fallback','proactive_fallback','agent_initiated_proactive')
                      AND p.last_user_ts IS NOT NULL AND p.ts >= p.last_user_ts
                      THEN p.ts - p.last_user_ts END) AS median_latency
        FROM paired p LEFT JOIN routes r ON r.user_id = p.user_id
        GROUP BY route
    """)
    out["reply"] = [
        {"route": r[0], "user_msgs": r[1], "real_replies": r[2], "fallback_replies": r[3],
         "median_latency": float(r[4]) if r[4] is not None else None}
        for r in rows
    ]
    return out


def admin_events_by_user(category: str, *, limit: int = 400) -> list[dict]:
    """Per-user breakdown for ONE event category (drill-down). Each row:
    {user_id, route, total, success, failed, fallback?, median_dur, last_ts}.
    Route-joined; the caller sorts worst-first + maps route→VPS/API."""
    cat = str(category or "").strip()
    # No SQL LIMIT: grouped rows = #users (bounded); the caller sorts worst-first
    # then slices, so an early DB truncation can't hide the actual worst users.
    _ = limit
    dur_l = _JOB_DUR_SEC.replace("doc", "l.doc")

    def _run(sql, params=()):
        try:
            with get_pool().connection() as conn:
                return conn.execute(sql, params).fetchall()
        except Exception as e:  # noqa: BLE001
            log.error("[db] admin_events_by_user(%s) failed: %s", cat, e)
            return []

    def _job_rows(rows):
        return [{"user_id": r[0], "route": r[1], "total": r[2], "success": r[3],
                 "failed": r[4], "median_dur": float(r[5]) if r[5] is not None else None,
                 "last_ts": float(r[6]) if r[6] is not None else None} for r in rows]

    if cat in ("heartbeat", "trigger", "screen", "other"):
        rows = _run(f"""
            {_EVENTS_ROUTES_CTE}
            SELECT j.user_id, COALESCE(r.route,'resident') AS route,
                   COUNT(*)::int AS total,
                   (COUNT(*) FILTER (WHERE j.status IN ('posted','delivered','completed')))::int AS success,
                   (COUNT(*) FILTER (WHERE j.status IN ('failed','skipped')))::int AS failed,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY j.dur) AS median_dur,
                   MAX(j.ts) AS last_ts
            FROM (
              SELECT l.user_id, l.ts, COALESCE(l.doc->>'status','') AS status, {dur_l} AS dur,
                CASE
                  WHEN k.kind IN ('screen_watch','screen_tick','broadcast_opened','heartbeat_broadcast_on') THEN 'screen'
                  WHEN k.kind IN ('perception_event','scene_change','photo_added','arrived_at_anchor','location','unlock_after_absence','scheduled_wake') THEN 'trigger'
                  WHEN k.kind = 'presence' OR left(k.kind,9) = 'heartbeat' THEN 'heartbeat'
                  ELSE 'other'
                END AS lane
              FROM user_logs l,
                LATERAL (SELECT COALESCE(NULLIF(l.doc->>'job_kind',''),NULLIF(l.doc->>'wake_kind',''),NULLIF(l.doc->>'trigger',''),'unknown') AS kind) k
              WHERE l.stream='proactive_jobs'
                AND COALESCE(l.doc->>'job_kind','') NOT IN ('memory_capture','memory_dream','memory_migrate')
            ) j LEFT JOIN routes r ON r.user_id = j.user_id
            WHERE j.lane = %s
            GROUP BY j.user_id, route
        """, (cat,))
        return _job_rows(rows)

    if cat == "memory_org":
        rows = _run(f"""
            {_EVENTS_ROUTES_CTE}
            SELECT m.uid AS user_id, COALESCE(r.route,'resident') AS route,
                   COUNT(*)::int AS total,
                   (COUNT(*) FILTER (WHERE m.status = 'completed'))::int AS success,
                   (COUNT(*) FILTER (WHERE m.status IN ('failed','error','skipped')))::int AS failed,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY m.dur) AS median_dur,
                   MAX(m.ts) AS last_ts
            FROM (
              SELECT user_id AS uid, ts, doc->>'status' AS status, {_JOB_DUR_SEC} AS dur
              FROM user_logs WHERE stream='memory_capture_jobs'
              UNION ALL
              SELECT user_id AS uid, ts, doc->>'status' AS status, {_JOB_DUR_SEC} AS dur
              FROM user_logs WHERE stream='proactive_jobs'
                AND doc->>'job_kind' IN ('memory_capture','memory_dream','memory_migrate')
            ) m LEFT JOIN routes r ON r.user_id = m.uid
            GROUP BY m.uid, route
        """)
        return _job_rows(rows)

    if cat in ("distill_first", "distill_second"):
        cond = "= 'onboarding'" if cat == "distill_first" else "<> 'onboarding'"
        rows = _run(f"""
            {_EVENTS_ROUTES_CTE}
            SELECT g.user_id, COALESCE(r.route,'resident') AS route,
                   COUNT(*)::int AS total,
                   (COUNT(*) FILTER (WHERE g.status IN ('done','completed')))::int AS success,
                   (COUNT(*) FILTER (WHERE g.status IN ('error','failed')))::int AS failed,
                   NULL::float AS median_dur,
                   EXTRACT(EPOCH FROM MAX(g.updated_at)) AS last_ts
            FROM genesis_import_jobs g LEFT JOIN routes r ON r.user_id = g.user_id
            WHERE COALESCE(NULLIF(g.metadata->>'mode',''),'onboarding') {cond}
            GROUP BY g.user_id, route
        """)
        return _job_rows(rows)

    if cat == "reply":
        rows = _run(f"""
            {_EVENTS_ROUTES_CTE}, paired AS (
              SELECT c.user_id, c.ts, c.doc->>'role' AS role, COALESCE(c.doc->>'source','') AS src,
                MAX(CASE WHEN c.doc->>'role'='user' AND COALESCE(c.doc->>'source','') NOT IN ('verify_ping','resident_maintenance') THEN c.ts END)
                  OVER (PARTITION BY c.user_id ORDER BY c.ts ROWS UNBOUNDED PRECEDING) AS last_user_ts
              FROM chat_messages c
            )
            SELECT p.user_id, COALESCE(r.route,'resident') AS route,
                   (COUNT(*) FILTER (WHERE p.role='user' AND p.src NOT IN ('verify_ping','resident_maintenance')))::int AS user_msgs,
                   (COUNT(DISTINCT p.last_user_ts) FILTER (WHERE p.role IN ('agent','openclaw') AND p.src NOT IN ('foreground_fallback','proactive_fallback','agent_initiated_proactive') AND p.last_user_ts IS NOT NULL))::int AS real_replies,
                   (COUNT(*) FILTER (WHERE p.src='foreground_fallback'))::int AS fallback_replies,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                     CASE WHEN p.role IN ('agent','openclaw')
                          AND p.src NOT IN ('foreground_fallback','proactive_fallback','agent_initiated_proactive')
                          AND p.last_user_ts IS NOT NULL AND p.ts >= p.last_user_ts
                          THEN p.ts - p.last_user_ts END) AS median_latency,
                   MAX(p.ts) AS last_ts
            FROM paired p LEFT JOIN routes r ON r.user_id = p.user_id
            GROUP BY p.user_id, route
        """)
        out = []
        for r in rows:
            um, real, fb = int(r[2] or 0), int(r[3] or 0), int(r[4] or 0)
            out.append({"user_id": r[0], "route": r[1], "total": um, "success": real,
                        "failed": max(0, um - real), "fallback": fb, "fallback_base": real + fb,
                        "median_dur": float(r[5]) if r[5] is not None else None,
                        "last_ts": float(r[6]) if r[6] is not None else None})
        return out

    return []


def admin_onboarding_funnel() -> list[dict]:
    """Per-user onboarding milestone epochs for the funnel view. Each row:
    {user_id, route, t0, t1, t2, t3} (epoch seconds; None = not reached).

    Milestones (route-aware):
      t0 registered = users.created_at
      t1 配置/上线   = API: has an onboarding genesis job (⊇ t2 → monotonic;
                      the client model_api_setup_succeeded event was too spotty —
                      key-verification is tracked separately via admin_api_key_stats);
                      VPS: first chat/proactive activity (consumer online, 'B')
      t2 内容就绪    = API: onboarding-genesis job done; VPS: first memory card
      t3 首次真回复  = first non-fallback agent message ('A', both routes)
    The caller aggregates conversion + median segment durations, split VPS/API."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(f"""
                {_EVENTS_ROUTES_CTE},
                u AS (SELECT user_id,
                        CASE WHEN created_at ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                             THEN EXTRACT(EPOCH FROM created_at::timestamptz) ELSE NULL END AS t0
                      FROM users),
                gen_started AS (SELECT user_id, MIN(EXTRACT(EPOCH FROM updated_at)) AS t
                          FROM genesis_import_jobs
                          WHERE COALESCE(NULLIF(metadata->>'mode',''),'onboarding')='onboarding'
                          GROUP BY user_id),
                firstact AS (SELECT user_id, MIN(ts) AS t FROM (
                             SELECT user_id, ts FROM chat_messages
                             UNION ALL SELECT user_id, ts FROM user_logs WHERE stream='proactive_jobs'
                           ) a GROUP BY user_id),
                gen AS (SELECT user_id, MIN(EXTRACT(EPOCH FROM updated_at)) AS t
                        FROM genesis_import_jobs
                        WHERE status IN ('done','completed')
                          AND COALESCE(NULLIF(metadata->>'mode',''),'onboarding')='onboarding'
                        GROUP BY user_id),
                mem AS (SELECT user_id,
                        MIN(EXTRACT(EPOCH FROM (COALESCE(NULLIF(doc->>'created_at',''), occurred_at))::timestamptz)) AS t
                        FROM memory_moments
                        WHERE COALESCE(NULLIF(doc->>'created_at',''), occurred_at) ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}'
                        GROUP BY user_id),
                reply AS (SELECT user_id, MIN(ts) AS t FROM chat_messages
                          WHERE doc->>'role' IN ('agent','openclaw')
                            AND COALESCE(doc->>'source','') NOT IN ('foreground_fallback','proactive_fallback')
                          GROUP BY user_id)
                SELECT u.user_id, COALESCE(r.route,'resident') AS route, u.t0,
                       CASE WHEN COALESCE(r.route,'resident')='model_api' THEN gen_started.t ELSE firstact.t END AS t1,
                       CASE WHEN COALESCE(r.route,'resident')='model_api' THEN gen.t ELSE mem.t END AS t2,
                       reply.t AS t3
                FROM u
                LEFT JOIN routes r ON r.user_id = u.user_id
                LEFT JOIN gen_started ON gen_started.user_id = u.user_id
                LEFT JOIN firstact ON firstact.user_id = u.user_id
                LEFT JOIN gen ON gen.user_id = u.user_id
                LEFT JOIN mem ON mem.user_id = u.user_id
                LEFT JOIN reply ON reply.user_id = u.user_id
            """).fetchall()
        def f(v):
            return float(v) if v is not None else None
        return [{"user_id": r[0], "route": r[1], "t0": f(r[2]), "t1": f(r[3]),
                 "t2": f(r[4]), "t3": f(r[5])} for r in rows]
    except Exception as e:  # noqa: BLE001
        log.error("[db] admin_onboarding_funnel failed: %s", e)
        return []


def admin_api_key_stats() -> dict:
    """model_api users by API-key verification status, from the SERVER-SIDE
    model_api config test_status (reliable) rather than the spotty client
    model_api_setup_succeeded tracking event. passed = test_status 'ok';
    stuck = has a model_api config but not yet 'ok'."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute("""
                SELECT lower(COALESCE(NULLIF(doc->>'test_status',''),'(none)')) AS st, COUNT(*)::int
                FROM user_blobs WHERE kind='model_api'
                GROUP BY st
            """).fetchall()
        by = {r[0]: r[1] for r in rows}
        total = sum(by.values())
        passed = int(by.get("ok", 0))
        return {"passed": passed, "stuck": total - passed, "total": total, "by_status": by}
    except Exception as e:  # noqa: BLE001
        log.error("[db] admin_api_key_stats failed: %s", e)
        return {"passed": 0, "stuck": 0, "total": 0, "by_status": {}}


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


def get_blobs_for_users(
    user_ids: list[str],
    kinds: list[str],
) -> dict[tuple[str, str], object]:
    """Return singleton blobs for a set of users and kinds in one DB round trip.

    Missing rows are omitted.  This is primarily for admin fan-out views: using
    ``get_blob`` twice per user made the global debug page perform 2N pool
    acquisitions and queries (more than 1,100 queries in production).
    """
    ids = list(dict.fromkeys(str(uid or "").strip() for uid in user_ids))
    wanted_kinds = list(dict.fromkeys(str(kind or "").strip() for kind in kinds))
    ids = [uid for uid in ids if uid]
    wanted_kinds = [kind for kind in wanted_kinds if kind]
    if not ids or not wanted_kinds:
        return {}
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT user_id, kind, doc FROM user_blobs "
                "WHERE user_id = ANY(%s) AND kind = ANY(%s)",
                (ids, wanted_kinds),
            ).fetchall()
        return {(str(user_id), str(kind)): doc for user_id, kind, doc in rows}
    except Exception as e:
        log.error("[db] get_blobs_for_users(%d users,%d kinds) failed: %s",
                  len(ids), len(wanted_kinds), e)
        return {}


def set_blob_if_unchanged(user_id: str, kind: str, expected_doc, new_doc) -> bool:
    """Compare-and-swap the (user_id, kind) blob: write ``new_doc`` only if the
    row's current doc STILL equals ``expected_doc``. Returns True iff the swap
    happened.

    JSONB ``=`` is a semantic comparison (the column is stored normalized, so
    key order and whitespace don't matter), and a single ``UPDATE ... WHERE doc
    = expected RETURNING`` is atomic under the row lock — no read-modify-write
    window, so a concurrent writer that moved the blob between the caller's read
    and this call cannot be silently clobbered. Used by mcp_core.test_server:
    it loads the whole server list, runs a probe that can take tens of seconds,
    then wants to persist a detected transport WITHOUT rolling back any upsert /
    delete / toggle that landed in the meantime. On CAS failure the caller drops
    the (best-effort) persistence and keeps the probe result.

    A missing row (``expected_doc`` from a blob that was since deleted) also
    returns False: the WHERE matches nothing, so we never resurrect it.
    """
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE user_blobs SET doc = %s "
                "WHERE user_id = %s AND kind = %s AND doc = %s "
                "RETURNING user_id",
                (Jsonb(new_doc), user_id, kind, Jsonb(expected_doc)),
            ).fetchone()
    except Exception as e:
        log.error("[db] set_blob_if_unchanged(%s,%s) failed: %s", user_id, kind, e)
        return False
    if row is None:
        return False
    # Mirror the winning write to the TEE shadow on the same terms as set_blob
    # (user_mcp is not an excluded kind). Best-effort; the CAS already committed.
    if kind not in ("identity", "consumer_state"):
        from tee_shadow import mirror
        mirror.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc",
            (user_id, kind, Jsonb(new_doc)))
    return True


def set_blob(user_id: str, kind: str, doc) -> None:
    sql = ("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
           "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc")
    try:
        with get_pool().connection() as conn:
            conn.execute(sql, (user_id, kind, Jsonb(doc)))
    except Exception as e:
        log.error("[db] set_blob(%s,%s) failed: %s", user_id, kind, e)
        return
    from tee_shadow import mirror
    # identity 归 tee_replicator 明文化管辖：RDS 里是 E2E 密文信封、TEE 里是
    # replicator 落的明文版本。这里若把密文信封原样镜像进 TEE user_blobs，就会
    # 盖掉那份明文（密文绝不能盖明文）。identity 的原地 UPDATE 传播改走 requeue
    # lane（见 identity/service._save_identity）。
    #
    # consumer_state 是全系统最热的写：每一次 /v1/chat/poll 都记一条 consumer 事件
    # （chat/consumer._record_consumer_event → _save_consumer_state → 本函数），N 个
    # 常驻 consumer 长轮询就是每轮 N 次写。把它镜像出去会打满 max_size=4 的 TEE 池
    # （direct-TLS 过网关），于是每个主写都要先在池上等满 pool_timeout 才 fail-open
    # ——2026-07-13 test 实测：13 分钟 18 次 pool timeout，poll/写端点被拖到秒级
    # （perception/report 23.7s、track/event 13.4s）。而它只是 runner 侧运维状态
    # （上次 poll 时间 / consumer id），不是用户数据，TEE 影子没有任何理由持有它。
    #
    # 两处辖区必须同步：reconciler._SCOPE_WHERE["user_blobs"] 同样排除这两个 kind，
    # 否则 reconciler 会把镜像端故意不写的行又 copy 回 TEE、并在两侧计数里要求它存在。
    # 其余 kind（如 model_api provider-key 信封）有意原样镜像（凭据保持加密）。
    if kind not in ("identity", "consumer_state"):
        mirror.execute(sql, (user_id, kind, Jsonb(doc)))


def list_agent_runtime_enabled_users() -> list[dict]:
    """有 active route 且该 route test_status='ok'、其 credential 的 provider 能
    fit 的用户都纳入托管（与 hosted/agent_runtime_cutover.resolve_driver 一致——
    不再有 per-user ``agent_runtime_driver`` 开关；kill switch 改用删/换 active
    route 或改 test_status）。发现无条件进行——没有 gateway proxy 要避让，所有
    fit provider 都直连（pi 走 openai-completions wire，不经 LiteLLM 网关）。
    AGENT 由 provider 派生（保持 CASE 与 cutover.driver_for_provider 同步）：
    anthropic/deepseek → claude（deepseek 走其 /anthropic 兼容层，Anthropic wire）；
    openai → codex (native)；其余 fit provider
    （gemini/openrouter/openai_compatible）→ pi。
    Returns [{"user_id","driver","provider","model","base_url","supports_responses",
    "reasoning_effort"}]
    sorted by user_id (``supports_responses`` is the openai_compatible relay's
    /v1/responses capability, set at setup; selects native passthrough vs the
    LiteLLM chat-completions bridge)。"""
    providers = ["anthropic", "claude", "deepseek", "openai",
                 "gemini", "openrouter", "openai_compatible"]
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT r.user_id,
                  CASE LOWER(c.provider)
                    WHEN 'anthropic' THEN 'claude'
                    WHEN 'claude'    THEN 'claude'
                    WHEN 'deepseek'  THEN 'claude'
                    WHEN 'openai'    THEN 'codex'
                    ELSE 'pi'
                  END AS driver,
                  LOWER(c.provider) AS provider,
                  r.model AS model,
                  c.base_url AS base_url,
                  c.supports_responses AS supports_responses,
                  COALESCE(r.reasoning_effort, '') AS reasoning_effort
                FROM model_api_routes r
                JOIN model_api_credentials c ON c.id = r.credential_id
                WHERE r.is_active
                  AND r.test_status = 'ok'
                  AND LOWER(c.provider) = ANY(%s)
                ORDER BY r.user_id
                """,
                (providers,),
            ).fetchall()
        return [{"user_id": uid, "driver": driver, "provider": provider,
                 "model": model, "base_url": base_url,
                 "supports_responses": bool(supports_responses),
                 "reasoning_effort": reasoning_effort}
                for uid, driver, provider, model, base_url, supports_responses, reasoning_effort in rows]
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
    sql = ("INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, 'hosted_tick', %s) "
           "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc "
           "WHERE COALESCE((user_blobs.doc->>'ts')::float8, 0) <= %s "
           "RETURNING doc")
    try:
        threshold = now - interval_sec
        with get_pool().connection() as conn:
            row = conn.execute(sql, (user_id, Jsonb(doc), threshold)).fetchone()
    except Exception as e:
        log.error("[db] try_stamp_hosted_tick(%s) failed: %s", user_id, e)
        return False
    won = row is not None
    if won:
        from tee_shadow import mirror
        mirror.execute(sql, (user_id, Jsonb(doc), threshold))
    return won


def claim_and_enqueue_introduction(
    user_id: str,
    settings_doc: dict,
    job: dict,
    *,
    at_iso: str,
    ts: float | None = None,
    item_key: str | None = None,
) -> dict | None:
    """Cross-process exactly-once: claim the one-shot ``introduced_at`` marker
    (user_blobs/proactive_settings) AND append the introduction job
    (user_logs/proactive_jobs) in a SINGLE PostgreSQL transaction.

    Why a transaction, not a per-process lock (Codex P1): backend workers and
    the standalone runner do NOT share a Python lock, so two processes could
    each read an empty marker and both enqueue. Here the guarded UPSERT is the
    atomic gate — only the process whose ``jsonb_set`` actually flips the empty
    marker gets a ``RETURNING`` row; everyone else loses. And because the job
    INSERT is in the same transaction, a job-write failure rolls the marker back
    automatically — the marker can never persist without a stored job, and there
    is no ``unclaim`` step that could erase another caller's success.

    ``settings_doc`` seeds the row ONLY when it does not yet exist (fresh user);
    when the row exists the marker is merged in via ``jsonb_set`` so peer fields
    (e.g. ``first_chat_ok_at``) are preserved. Returns ``{"job", "seq"}`` iff
    THIS caller won and the job persisted, else ``None`` (already introduced,
    lost the race, or a DB failure that rolled back)."""
    # ON CONFLICT merges the marker into the EXISTING doc (jsonb_set) and also
    # refreshes updated_at/version like every other proactive_settings mutation,
    # WITHOUT clobbering peer fields (first_chat_ok_at, switches, timezone…).
    claim_sql = (
        "INSERT INTO user_blobs (user_id, kind, doc) "
        "VALUES (%s, 'proactive_settings', %s) "
        "ON CONFLICT (user_id, kind) DO UPDATE "
        "  SET doc = jsonb_set(user_blobs.doc, '{introduced_at}', to_jsonb(%s::text), true) "
        "            || jsonb_build_object('updated_at', %s::text, 'version', 2) "
        "  WHERE COALESCE(user_blobs.doc->>'introduced_at', '') = '' "
        "RETURNING doc"
    )
    job_sql = (
        "INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
        "VALUES (%s, 'proactive_jobs', %s, %s, %s) RETURNING seq"
    )
    claimed_doc = None
    seq = None
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(claim_sql, (user_id, Jsonb(settings_doc), at_iso, at_iso)).fetchone()
                if row is not None:
                    claimed_doc = row[0]
                    seq = conn.execute(job_sql, (user_id, ts, item_key, Jsonb(job))).fetchone()[0]
    except Exception as e:
        log.error("[db] claim_and_enqueue_introduction(%s) failed: %s", user_id, e)
        return None
    if claimed_doc is None or seq is None:
        return None
    # Committed on the primary — mirror BOTH rows into the TEE shadow as ONE
    # transaction (they are a single logical write). The blob merges the marker
    # (jsonb_set + updated_at/version) rather than overwriting the whole doc, so
    # a late-arriving introduction mirror can't briefly clobber peer fields the
    # reconciler already advanced on the shadow; the job seq is pinned so the
    # shadow keeps the row identity every seq-ordered read relies on.
    from tee_shadow import mirror
    mirror.execute_many([
        (
            "INSERT INTO user_blobs (user_id, kind, doc) "
            "VALUES (%s, 'proactive_settings', %s) "
            "ON CONFLICT (user_id, kind) DO UPDATE "
            "  SET doc = jsonb_set(user_blobs.doc, '{introduced_at}', to_jsonb(%s::text), true) "
            "            || jsonb_build_object('updated_at', %s::text, 'version', 2)",
            (user_id, Jsonb(claimed_doc), at_iso, at_iso),
        ),
        (
            "INSERT INTO user_logs (user_id, stream, seq, ts, item_key, doc) "
            "OVERRIDING SYSTEM VALUE VALUES (%s, 'proactive_jobs', %s, %s, %s, %s) "
            "ON CONFLICT (user_id, stream, seq) DO NOTHING",
            (user_id, seq, ts, item_key, Jsonb(job)),
        ),
    ])
    return {"job": job, "seq": seq}


def delete_blob(user_id: str, kind: str) -> bool:
    sql = "DELETE FROM user_blobs WHERE user_id = %s AND kind = %s"
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (user_id, kind))
    except Exception as e:
        log.error("[db] delete_blob(%s,%s) failed: %s", user_id, kind, e)
        return False
    from tee_shadow import mirror
    # Deletes are plaintext-safe even for kind='identity': removing the RDS
    # ciphertext blob means the user's identity is gone, so dropping the TEE
    # plaintext row is exactly right (unlike set_blob, which would clobber the
    # replicator's plaintext with ciphertext — see set_blob's identity guard).
    mirror.execute(sql, (user_id, kind))
    if kind == "identity":
        # Drop any requeue/terminal pending marker for this user's identity
        # row too — worker._TABLES["identity"] is keyed by user_id alone (one
        # row/user, no per-item id column), so a stale pending row here would
        # otherwise outlive the RDS blob it was tracking and permanently
        # unbalance verify's rds == tee + pending equation.
        mirror.execute(
            "DELETE FROM tee_pending_device_migration "
            "WHERE user_id = %s AND table_name = 'identity'", (user_id,))
    return cur.rowcount > 0


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
    sql = (
        """
        INSERT INTO genesis_import_jobs
            (user_id, job_id, status, source_kind, file_manifest_hash,
             total_chunks, total_bytes, privacy_mode, metadata, output,
             updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, now())
        ON CONFLICT (user_id, job_id) DO NOTHING
        RETURNING *
        """
    )
    params = (
        user_id,
        job["job_id"],
        job.get("status", "created"),
        job.get("source_kind", "unknown"),
        job.get("file_manifest_hash", ""),
        int(job.get("total_chunks") or 0),
        int(job.get("total_bytes") or 0),
        job.get("privacy_mode", ""),
        Jsonb(job.get("metadata") or {}),
    )
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        result = _genesis_row(cur, cur.fetchone())
    from tee_shadow import mirror
    mirror.execute(sql, params)
    return result


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
    if out:
        # NOTE: re-running the SKIP LOCKED "picked" query against TEE could pick
        # a different set of 'uploaded' rows than the primary just claimed (TEE's
        # status snapshot can lag). Pin the mirror to the EXACT (user_id, job_id)
        # pairs the primary actually claimed instead — deterministic, no drift.
        placeholders = ", ".join(["(%s, %s)"] * len(out))
        mirror_sql = (
            "UPDATE genesis_import_jobs SET status = 'processing', error = '', "
            "output = jsonb_build_object('stage', 'worker_claimed'), updated_at = now() "
            f"WHERE (user_id, job_id) IN ({placeholders})"
        )
        mirror_params = tuple(v for item in out for v in (item["user_id"], item["job_id"]))
        from tee_shadow import mirror
        mirror.execute(mirror_sql, mirror_params)
    return out


def genesis_claim_resident_jobs(user_id: str, *, consumer_id: str, limit: int = 1) -> list[dict]:
    """Atomically claim ``awaiting_resident`` genesis jobs for a resident consumer.

    Scoped to a single ``user_id`` — the resident consumer authenticates as its own
    user (same per-user credential it uses for chat poll), so it only ever claims that
    user's jobs, never another user's. Mirrors ``genesis_claim_uploaded_jobs`` (FOR
    UPDATE SKIP LOCKED so a user's multiple consumer processes can't double-process),
    moving awaiting_resident -> processing and stamping the claiming consumer + a fresh
    heartbeat + attempt count (so a dead consumer's job can be reaped / re-queued).
    """
    cid = str(consumer_id or "").strip()
    if not cid:
        # An empty consumer_id would move the job to processing with a blank owner —
        # invisible to genesis_reap_stale_resident_jobs (resident_consumer_id <> '') and
        # thus unrecoverable. Refuse rather than wedge it.
        raise ValueError("consumer_id_required")
    safe_limit = max(1, min(int(limit or 1), 16))
    with get_pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                """
                WITH picked AS (
                    SELECT user_id, job_id
                    FROM genesis_import_jobs
                    WHERE user_id = %s AND status = 'awaiting_resident'
                    ORDER BY finalized_at ASC NULLS LAST, updated_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE genesis_import_jobs AS j SET
                    status = 'processing',
                    error = '',
                    resident_consumer_id = %s,
                    resident_claimed_at = now(),
                    resident_heartbeat_at = now(),
                    resident_attempts = j.resident_attempts + 1,
                    output = jsonb_build_object('stage', 'resident_claimed'),
                    updated_at = now()
                FROM picked
                WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
                RETURNING j.*
                """,
                (user_id, safe_limit, cid),
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
                  AND COALESCE(resident_consumer_id, '') = ''
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
    if out:
        # Same rationale as genesis_claim_uploaded_jobs: pin the mirror to the
        # exact rows the primary reaped rather than re-picking independently.
        placeholders = ", ".join(["(%s, %s)"] * len(out))
        mirror_sql = (
            "UPDATE genesis_import_jobs SET status = 'failed', error = %s, updated_at = now() "
            f"WHERE (user_id, job_id) IN ({placeholders})"
        )
        mirror_params = (error[:1000],) + tuple(
            v for item in out for v in (item["user_id"], item["job_id"]))
        from tee_shadow import mirror
        mirror.execute(mirror_sql, mirror_params)
    return out


def genesis_resident_heartbeat(user_id: str, job_id: str, *, consumer_id: str) -> bool:
    """Renew a resident job's lease. Only the owning consumer (claimed it, still
    processing) may heartbeat — this is what keeps genesis_reap_stale_resident_jobs
    from re-queueing a job whose consumer is alive and grinding. Returns True if renewed."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            UPDATE genesis_import_jobs SET
                resident_heartbeat_at = now(),
                updated_at = now()
            WHERE user_id = %s AND job_id = %s
              AND status = 'processing' AND resident_consumer_id = %s
            """,
            (user_id, job_id, consumer_id),
        )
        return cur.rowcount > 0


def genesis_reap_stale_resident_jobs(
    older_than_sec: int, *, max_attempts: int, error: str, limit: int = 50
) -> list[dict]:
    """Recover resident jobs whose consumer died mid-distill (processing, resident-owned,
    heartbeat older than the lease). Under the attempt cap → re-queue to awaiting_resident
    (another consumer re-claims, resident_attempts keeps accumulating across re-queues);
    at/over the cap → fail. Atomic (FOR UPDATE SKIP LOCKED) so a live consumer that just
    heartbeated is not touched. Returns the rows changed so the caller can sync state."""
    safe_sec = max(60, int(older_than_sec or 0))
    safe_limit = max(1, min(int(limit or 1), 200))
    safe_max = max(1, int(max_attempts or 1))
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            WITH picked AS (
                SELECT user_id, job_id
                FROM genesis_import_jobs
                WHERE status = 'processing'
                  AND resident_consumer_id <> ''
                  AND resident_heartbeat_at < now() - make_interval(secs => %s)
                ORDER BY resident_heartbeat_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE genesis_import_jobs AS j SET
                status = CASE WHEN j.resident_attempts < %s THEN 'awaiting_resident' ELSE 'failed' END,
                error = CASE WHEN j.resident_attempts < %s THEN '' ELSE %s END,
                resident_consumer_id = '',
                resident_claimed_at = NULL,
                resident_heartbeat_at = NULL,
                updated_at = now()
            FROM picked
            WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
            RETURNING j.*
            """,
            (safe_sec, safe_limit, safe_max, safe_max, error[:1000]),
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


def genesis_reap_stale_unclaimed_jobs(
    older_than_sec: int, *, error: str, limit: int = 50
) -> list[dict]:
    """Fail sealed resident-distill jobs wedged in 'awaiting_resident' past a generous
    cutoff — no consumer ever claimed them (consumer running stale code that never opened
    the distill lane, offline, or never started). Nothing else times these out: the cloud
    reaper only touches un-owned 'processing' rows and genesis_reap_stale_resident_jobs
    only touches claimed 'processing' rows, so an unclaimed row would otherwise sit forever
    and the app spins 'processing' with no error.

    Atomic (FOR UPDATE SKIP LOCKED, status re-checked INSIDE the UPDATE) so a consumer that
    claims the row between SELECT and UPDATE is never clobbered — a just-claimed job is
    'processing', no longer 'awaiting_resident', and is skipped. ``updated_at`` is the row's
    last state change (job creation, or a reaper re-queue), so a freshly re-queued job
    restarts the cutoff clock rather than being reaped immediately. Returns the rows flipped
    so the caller can sync each one's genesis_state blob."""
    safe_sec = max(60, int(older_than_sec or 0))
    safe_limit = max(1, min(int(limit or 1), 200))
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            WITH picked AS (
                SELECT user_id, job_id
                FROM genesis_import_jobs
                WHERE status = 'awaiting_resident'
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
              AND j.status = 'awaiting_resident'
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


def genesis_oldest_awaiting_resident_job(user_id: str, *, older_than_sec: int) -> dict | None:
    """Oldest still-unclaimed resident distill job for one user, or None.

    Read-only early warning helper. The terminal state transition stays owned by
    genesis_reap_stale_unclaimed_jobs, which uses a much longer cutoff.
    """
    safe_sec = max(60, int(older_than_sec or 0))
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            SELECT *,
                   EXTRACT(EPOCH FROM (now() - updated_at)) AS age_sec
            FROM genesis_import_jobs
            WHERE user_id = %s
              AND status = 'awaiting_resident'
              AND updated_at < now() - make_interval(secs => %s)
            ORDER BY updated_at ASC
            LIMIT 1
            """,
            (user_id, safe_sec),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    item = dict(zip(cols, row))
    for key, value in list(item.items()):
        if hasattr(value, "isoformat"):
            item[key] = value.isoformat()
    try:
        item["age_sec"] = float(item.get("age_sec") or 0.0)
    except (TypeError, ValueError):
        item["age_sec"] = 0.0
    return item


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


def genesis_delete_chunks(user_id: str, job_id: str) -> int:
    """Delete a job's stored (encrypted) chunks. Used after a resident distill completes:
    the sealed material is ephemeral — consumed once the local agent has distilled it,
    so the server keeps no leftover ciphertext. Returns the number of chunks deleted."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM genesis_import_chunks WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        return cur.rowcount


def genesis_mark_finalized(user_id: str, job_id: str) -> dict | None:
    sql = (
        """
        UPDATE genesis_import_jobs SET
            status = 'uploaded',
            finalized_at = COALESCE(finalized_at, now()),
            updated_at = now()
        WHERE user_id = %s AND job_id = %s
          AND status IN ('created', 'uploading', 'uploaded', 'failed')
        RETURNING *
        """
    )
    params = (user_id, job_id)
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        result = _genesis_row(cur, cur.fetchone())
    from tee_shadow import mirror
    mirror.execute(sql, params)
    return result


def genesis_set_job_status(
    user_id: str,
    job_id: str,
    *,
    status: str,
    error: str = "",
    output: dict | None = None,
    processed_chunks: int | None = None,
) -> dict | None:
    sql = (
        """
        UPDATE genesis_import_jobs SET
            status = %s,
            error = %s,
            output = COALESCE(%s::jsonb, output),
            processed_chunks = COALESCE(%s, processed_chunks),
            updated_at = now()
        WHERE user_id = %s AND job_id = %s
        RETURNING *
        """
    )
    params = (
        status,
        error[:1000],
        Jsonb(output) if output is not None else None,
        processed_chunks,
        user_id,
        job_id,
    )
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        result = _genesis_row(cur, cur.fetchone())
    from tee_shadow import mirror
    mirror.execute(sql, (
        status,
        error[:1000],
        Jsonb(output) if output is not None else None,
        processed_chunks,
        user_id,
        job_id,
    ))
    return result


def genesis_touch_job(user_id: str, job_id: str) -> None:
    """Heartbeat: bump updated_at for a processing genesis job so the stale
    reaper can tell a live long import from a worker that died mid-run. No-op
    unless the job is currently 'processing'."""
    sql = (
        """
        UPDATE genesis_import_jobs SET updated_at = now()
        WHERE user_id = %s AND job_id = %s AND status = 'processing'
        """
    )
    params = (user_id, job_id)
    with get_pool().connection() as conn:
        conn.execute(sql, params)
    from tee_shadow import mirror
    mirror.execute(sql, params)


def genesis_upsert_output(
    user_id: str,
    job_id: str,
    output_type: str,
    *,
    doc: dict,
    status: str,
    ref: str = "",
) -> dict | None:
    sql = (
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
        """
    )
    with get_pool().connection() as conn:
        cur = conn.execute(sql, (user_id, job_id, output_type, ref, status, Jsonb(doc)))
        result = _genesis_row(cur, cur.fetchone())
    from tee_shadow import mirror
    mirror.execute(sql, (user_id, job_id, output_type, ref, status, Jsonb(doc)))
    return result


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
    sql = (
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
        """
    )
    with get_pool().connection() as conn:
        cur = conn.execute(sql, (
            Jsonb(output), int(memory_action_count), identity_status[:120],
            persona_ref[:240], persona_sha256[:80], user_id, job_id,
        ))
        result = _genesis_row(cur, cur.fetchone())
    from tee_shadow import mirror
    mirror.execute(sql, (
        Jsonb(output), int(memory_action_count), identity_status[:120],
        persona_ref[:240], persona_sha256[:80], user_id, job_id,
    ))
    return result


# ---------------------------------------------------------------------------
# Chat messages (row-per-item ring buffer)
# ---------------------------------------------------------------------------


def chat_newest_ts(user_id: str) -> float | None:
    """``ts`` of the user's newest-APPENDED chat row (by ``seq``), or None when
    the user has no rows. Single-row probe on the (user_id, seq) index — cheap
    enough for the /chat/history read-time staleness self-heal, which calls it
    on every empty since-poll (prod ~9/s). Newest-by-seq deliberately: the
    staleness this probe detects is a missed cross-worker APPEND broadcast, and
    seq is the append order. Raises on DB failure — the caller fails open."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT ts FROM chat_messages WHERE user_id = %s "
            "ORDER BY seq DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return float(row[0]) if row else None


def chat_load(user_id: str) -> list[dict]:
    """Load the user's chat ring. R2-offloaded file rows are returned as SLIM
    POINTERS (``body_key`` + ``body_ct_len``, no ``body_ct``) — the heavy
    ciphertext is fetched lazily only at the read exits that actually deliver a
    body (``hydrate_chat_file_body``), so a bulk/metadata-only load never
    downloads every historical file. Mirrors how large image bodies are omitted
    from the visible feed and lazily re-fetched per message."""
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


def chat_onboarding_greeting_row(user_id: str) -> dict | None:
    """Single-row lookup of the user's onboarding greeting
    (``model_api_kind='onboarding_greeting'``), oldest first.

    Deliberately RAISES on database failure instead of the swallow-and-default
    used elsewhere in this module: the caller uses this as the exactly-once
    guard for the greeting append, and collapsing "could not look" into
    "absent" would let a transient read failure bypass an existing greeting
    and insert a duplicate."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id = %s "
            "AND doc->>'model_api_kind' = 'onboarding_greeting' "
            "ORDER BY seq ASC LIMIT 1",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def chat_insert_onboarding_greeting_once(
    user_id: str, msg_id: str, ts: float, doc: dict
) -> tuple[dict, bool]:
    """First-writer-wins insert for the onboarding greeting row.

    Greeting-specific by design — generic ``chat_append`` upserts
    (ON CONFLICT DO UPDATE), which under the stable greeting msg_id would let
    a concurrent second writer REWRITE the winner's ciphertext/ts: the two
    processes' rings would hold different envelopes, and a same-PK rewrite
    that lowers ``ts`` can slip behind the TEE replicator's (ts, msg_id)
    forward cursor, leaving RDS and TEE on different documents forever.
    DO NOTHING freezes the first legal greeting; a loser gets the
    authoritative winner row back and must treat it as the truth.

    RAISES on database failure (this is the exactly-once guard — never
    collapse "could not write/look" into an answer). Returns
    ``(winner_doc, inserted_by_this_call)``."""
    with get_pool().connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, msg_id) DO NOTHING RETURNING doc",
                (user_id, msg_id, ts, Jsonb(doc)),
            ).fetchone()
            inserted = row is not None
            if not inserted:
                # ON CONFLICT waits out an in-flight conflicting insert, so by
                # here the winner is committed and visible to this statement.
                row = conn.execute(
                    "SELECT doc FROM chat_messages WHERE user_id = %s AND msg_id = %s",
                    (user_id, msg_id),
                ).fetchone()
    if row is None:
        # Conflict fired yet the row is gone (deleted between statements, e.g.
        # a concurrent chat clear) — surface it rather than invent an answer.
        raise RuntimeError("onboarding_greeting_row_vanished_after_conflict")
    # Deliberately NO inline TEE mirror: TEE chat rows are produced by the
    # replicator, which decrypts via the enclave (plaintext body, no
    # body_ct/K_*) and assigns seq from its own copy pass. An inline mirror of
    # this ENCRYPTED doc would write the wrong data shape into TEE and allocate
    # an independent seq that later conflict-updates never repair. The RDS row
    # is the immutable first-writer winner, so the normal replicator sweep
    # copies it exactly once.
    return row[0], inserted


# Content types whose body_ct is heavy enough to live in R2 rather than inline in
# the chat_messages row. Images join files here: a single photo's ciphertext runs
# 1-2MB, which TOASTs the row and is then carried through every WAL record, WAL-G
# backup, and TEE mirror/re-encrypt pass. The read side is content-type agnostic
# (a pointer is anything with body_key), so adding a type here is write-side only.
_R2_OFFLOAD_CONTENT_TYPES = ("file", "image")


def _is_chat_file_pointer(doc) -> bool:
    return isinstance(doc, dict) and bool(doc.get("body_key")) and doc.get("body_ct") is None


def hydrate_chat_file_body(user_id: str, doc: dict) -> dict:
    """Return a doc guaranteed to carry ``body_ct``. If ``doc`` is an R2 pointer
    (``body_key`` set, ``body_ct`` absent) the ciphertext is fetched from R2 and
    inlined into a COPY (the stored/cached row stays slim). Non-pointers and, when
    R2 is unconfigured, everything are returned unchanged. A missing/failed fetch
    returns the doc as-is (``body_ct`` still absent) so the enclave surfaces a
    per-item decrypt error for that one message rather than crashing the read.

    Call this ONLY at exits that actually deliver a body (poll delivery, a
    history page that includes the body, single message-body fetch) — never in
    bulk load — so a leaked/large file is fetched once, on demand.

    The fetch uses the row's OWN ``body_key`` rather than recomputing one, so a
    row written under an older key layout still resolves. The key is data, though,
    so object_storage refuses one that isn't under this user's own prefix."""
    if not _is_chat_file_pointer(doc) or not object_storage.chat_files_enabled():
        return doc
    body = object_storage.get_chat_body(str(doc.get("body_key") or ""), user_id)
    if body is None:
        return doc
    out = {k: v for k, v in doc.items() if k != "body_key"}
    out["body_ct"] = body
    return out


def chat_append(user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int) -> None:
    """Insert one chat message then trim to the newest ``max_messages`` rows,
    mirroring the in-memory ring buffer. Idempotent on msg_id.

    A heavy body_ct (``_R2_OFFLOAD_CONTENT_TYPES``: file, image) is offloaded to
    R2 when configured (``object_storage.chat_files_enabled()``); the row then
    keeps only the envelope metadata plus a ``body_key`` pointer, and
    ``chat_load`` reconstitutes ``body_ct`` from R2 transparently. Falls back to
    inline storage when R2 is unconfigured OR the upload fails. Crash-safe, same
    ordering as frame_upsert: the row is written inline (readable, no pointer)
    BEFORE the object exists and flipped to the pointer shape only AFTER the
    upload succeeds — a crash never leaves a pointer to a missing object."""
    offload = (
        object_storage.chat_files_enabled()
        and isinstance(doc, dict)
        and doc.get("content_type") in _R2_OFFLOAD_CONTENT_TYPES
        and doc.get("body_ct") is not None
    )
    trimmed_docs: list = []
    trimmed_ids: list[str] = []
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                # 1) inline first — message readable, references no R2 object yet.
                conn.execute(
                    "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id, msg_id) DO UPDATE SET ts = EXCLUDED.ts, doc = EXCLUDED.doc",
                    (user_id, msg_id, ts, Jsonb(doc)),
                )
                if max_messages and max_messages > 0:
                    rows = conn.execute(
                        "DELETE FROM chat_messages WHERE user_id = %s AND seq < ("
                        "  SELECT MIN(seq) FROM ("
                        "    SELECT seq FROM chat_messages WHERE user_id = %s "
                        "    ORDER BY seq DESC LIMIT %s"
                        "  ) t"
                        ") RETURNING msg_id, doc",
                        (user_id, user_id, max_messages),
                    ).fetchall()
                    trimmed_ids = [r[0] for r in rows]
                    trimmed_docs = [r[1] for r in rows]
        if offload:
            # 2) upload OUTSIDE the txn; on failure the inline row stays readable.
            try:
                body_ct_len = len(doc["body_ct"])
                # The key comes back from the upload rather than being recomputed —
                # the row can then never point somewhere the object isn't.
                key = object_storage.put_chat_body(
                    user_id, msg_id, doc["body_ct"], str(doc.get("content_type") or "file")
                )
                # 3) object exists → flip the row to the pointer shape as the last
                #    durable step. ATOMIC on the CURRENT row (not a stale snapshot):
                #    drop only body_ct and add the pointer keys, so any reply/claim
                #    metadata another worker merged into `doc` during the upload is
                #    preserved. The `? 'body_ct'` guard makes it a no-op if the row
                #    was already flipped (idempotent, avoids a double-flip race).
                pointer = {"body_key": key, "body_ct_len": body_ct_len}
                with get_pool().connection() as conn:
                    conn.execute(
                        "UPDATE chat_messages SET doc = (doc - 'body_ct') || %s "
                        "WHERE user_id = %s AND msg_id = %s AND doc ? 'body_ct'",
                        (Jsonb(pointer), user_id, msg_id),
                    )
            except Exception as e:  # noqa: BLE001
                log.error("[db] chat_append(%s,%s) R2 offload failed, left inline: %s",
                          user_id, msg_id, e)
        # Best-effort: drop R2 objects for any offloaded rows just trimmed. Driven by
        # the row's own body_key (not content_type, not a recomputed key) — a pointer
        # row always has exactly one object to reclaim, whatever type/layout wrote it.
        if trimmed_docs and object_storage.chat_files_enabled():
            for d in trimmed_docs:
                if isinstance(d, dict) and d.get("body_key"):
                    object_storage.delete_chat_body(str(d["body_key"]), user_id)
    except Exception as e:
        log.error("[db] chat_append(%s,%s) failed: %s", user_id, msg_id, e)
        return
    if trimmed_ids:
        # Primary trim committed → pin the mirror DELETE to the EXACT rows
        # evicted from RDS (same "pin to actual eviction" pattern as
        # frame_prune_to, rather than re-deriving "newest max_messages"
        # independently against TEE's own row set/order). Also drop any
        # tee_pending_device_migration markers those rows may carry (e.g. a
        # requeue/visibility_local_only marker from a swap shortly before
        # eviction) — otherwise a trimmed-away row's pending marker survives
        # forever with no RDS row left to justify it, permanently unbalancing
        # verify's rds == tee + pending equation (a false "missing row").
        from tee_shadow import mirror
        mirror.execute_many([
            ("DELETE FROM chat_messages WHERE user_id = %s AND msg_id = ANY(%s)",
             (user_id, trimmed_ids)),
            ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
             "AND table_name = 'chat_messages' AND item_id = ANY(%s)",
             (user_id, trimmed_ids)),
        ])


def chat_append_idempotent(
    user_id: str,
    msg_id: str,
    ts: float,
    doc: dict,
    max_messages: int,
    *,
    client_msg_id: str,
    window_sec: int,
) -> tuple[dict, bool]:
    """Atomically insert or recover one client-identified chat send.

    The transaction-scoped advisory lock is shared by every backend process
    using this PostgreSQL database. It serializes only the same user/key pair;
    a hash collision merely serializes unrelated sends because the winner query
    still compares the complete values. Unlike ``chat_append``, failures raise:
    an idempotency guard must fail closed rather than turn a failed lookup into
    an accidental second insert.
    """
    if not client_msg_id:
        raise ValueError("client_msg_id is required")
    if window_sec <= 0:
        raise ValueError("window_sec must be positive")

    offload = (
        object_storage.chat_files_enabled()
        and isinstance(doc, dict)
        and doc.get("content_type") in _R2_OFFLOAD_CONTENT_TYPES
        and doc.get("body_ct") is not None
    )
    trimmed_docs: list = []
    trimmed_ids: list[str] = []
    with get_pool().connection() as conn:
        with conn.transaction():
            # Length-prefix the user id so concatenation is unambiguous without
            # a NUL byte (PostgreSQL text values reject U+0000).
            lock_key = f"{len(user_id)}:{user_id}{client_msg_id}"
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            row = conn.execute(
                "SELECT doc FROM chat_messages "
                "WHERE user_id = %s AND doc->>'client_msg_id' = %s "
                "AND ts >= EXTRACT(EPOCH FROM clock_timestamp()) - %s "
                "ORDER BY seq DESC LIMIT 1",
                (user_id, client_msg_id, window_sec),
            ).fetchone()
            if row is not None:
                return row[0], False

            # Keep normal msg-id semantics: an envelope-id collision updates the
            # same primary-key row, matching chat_append's existing behavior.
            row = conn.execute(
                "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, msg_id) DO UPDATE "
                "SET ts = EXCLUDED.ts, doc = EXCLUDED.doc RETURNING doc",
                (user_id, msg_id, ts, Jsonb(doc)),
            ).fetchone()
            if max_messages and max_messages > 0:
                rows = conn.execute(
                    "DELETE FROM chat_messages WHERE user_id = %s AND seq < ("
                    "  SELECT MIN(seq) FROM ("
                    "    SELECT seq FROM chat_messages WHERE user_id = %s "
                    "    ORDER BY seq DESC LIMIT %s"
                    "  ) t"
                    ") RETURNING msg_id, doc",
                    (user_id, user_id, max_messages),
                ).fetchall()
                trimmed_ids = [r[0] for r in rows]
                trimmed_docs = [r[1] for r in rows]

    # Mirror chat_append's crash-safe R2 ordering. Only the transaction winner
    # reaches this block, so retries neither upload twice nor flip the row twice.
    if offload:
        try:
            body_ct_len = len(doc["body_ct"])
            key = object_storage.put_chat_body(
                user_id, msg_id, doc["body_ct"], str(doc.get("content_type") or "file")
            )
            pointer = {"body_key": key, "body_ct_len": body_ct_len}
            with get_pool().connection() as conn:
                conn.execute(
                    "UPDATE chat_messages SET doc = (doc - 'body_ct') || %s "
                    "WHERE user_id = %s AND msg_id = %s AND doc ? 'body_ct'",
                    (Jsonb(pointer), user_id, msg_id),
                )
        except Exception as e:  # noqa: BLE001
            log.error(
                "[db] chat_append_idempotent(%s,%s) R2 offload failed, left inline: %s",
                user_id,
                msg_id,
                e,
            )
    if trimmed_docs and object_storage.chat_files_enabled():
        for trimmed in trimmed_docs:
            if isinstance(trimmed, dict) and trimmed.get("body_key"):
                object_storage.delete_chat_body(str(trimmed["body_key"]), user_id)
    if trimmed_ids:
        from tee_shadow import mirror

        mirror.execute_many([
            ("DELETE FROM chat_messages WHERE user_id = %s AND msg_id = ANY(%s)",
             (user_id, trimmed_ids)),
            ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
             "AND table_name = 'chat_messages' AND item_id = ANY(%s)",
             (user_id, trimmed_ids)),
        ])
    if row is None:
        raise RuntimeError("chat_idempotent_insert_returned_no_row")
    return row[0], True


def chat_update_metadata(user_id: str, msg_id: str, fields: dict) -> dict | None:
    """Shallow-merge ``fields`` into the stored message doc. Returns the merged
    doc, or None if the message was not found."""
    sql = ("UPDATE chat_messages SET doc = doc || %s WHERE user_id = %s AND msg_id = %s "
           "RETURNING doc")
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql, (Jsonb(fields), user_id, msg_id)).fetchone()
    except Exception as e:
        log.error("[db] chat_update_metadata(%s,%s) failed: %s", user_id, msg_id, e)
        return None
    from tee_shadow import mirror
    mirror.execute(sql, (Jsonb(fields), user_id, msg_id))
    return row[0] if row is not None else None


_CHAT_FINALIZE_REPLY_ONCE_SQL = (
    "WITH won AS ("
    "  UPDATE chat_messages SET doc = doc || %s "
    "  WHERE user_id = %s AND msg_id = %s "
    "    AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
    "    AND COALESCE(doc->>'reply_message_id','') = '' "
    "  RETURNING doc AS parent_doc"
    "), inserted AS ("
    "  INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
    "  SELECT %s, %s, %s, %s FROM won "
    "  RETURNING doc AS reply_doc"
    ") "
    "SELECT won.parent_doc, inserted.reply_doc FROM won CROSS JOIN inserted"
)

_CHAT_FINALIZE_REPLY_PARENT_MIRROR_SQL = (
    "UPDATE chat_messages SET doc = doc || %s "
    "WHERE user_id = %s AND msg_id = %s"
)


def chat_finalize_reply_once(
    user_id: str,
    parent_msg_id: str,
    reply_msg_id: str,
    reply_ts: float,
    reply_doc: dict,
    replied_fields: dict,
) -> tuple[dict, dict] | None:
    """Atomically mark one parent answered and insert its encrypted reply.

    The parent primary-key UPDATE is the compare-and-swap.  The data-modifying
    CTE makes the reply INSERT conditional on winning that UPDATE and keeps the
    two writes in one PostgreSQL statement: a duplicate reply id or any other
    INSERT failure rolls the parent mutation back with the statement.  Losing
    the CAS is the only normal ``None`` result; database failures deliberately
    propagate so callers fail closed instead of accidentally retrying a reply.

    This low-level helper performs no cache, wake, capture, R2, or inline reply
    mirror side effects.  After the RDS statement commits, it best-effort
    mirrors only the parent's plaintext metadata; the encrypted reply remains
    exclusively on the normal decrypting TEE-replicator path.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            _CHAT_FINALIZE_REPLY_ONCE_SQL,
            (
                Jsonb(replied_fields),
                user_id,
                parent_msg_id,
                user_id,
                reply_msg_id,
                reply_ts,
                Jsonb(reply_doc),
            ),
        ).fetchone()
    if row is None:
        return None
    # The encrypted reply row is intentionally NOT mirrored here.  The normal
    # TEE replicator decrypts and copies that row in its canonical plaintext
    # shape.  Only the already-existing parent's plaintext metadata is safe to
    # merge inline; mirror.execute is best-effort and swallows TEE failures.
    from tee_shadow import mirror

    mirror.execute(
        _CHAT_FINALIZE_REPLY_PARENT_MIRROR_SQL,
        (Jsonb(replied_fields), user_id, parent_msg_id),
    )
    return row[0], row[1]


def chat_finalize_reply_post_commit(
    user_id: str, reply_doc: dict, max_messages: int
) -> None:
    """Run normal append maintenance after an atomic reply winner commits.

    Finalization must commit the parent CAS and inline encrypted reply together,
    so trimming and optional R2 offload happen afterwards.  This preserves
    ``chat_append`` semantics: trim to the newest bounded history, write heavy
    ciphertext inline first, upload outside a transaction, and only then flip
    the current row to an R2 pointer.  Failures are logged and leave the already
    committed inline reply readable.
    """
    reply_msg_id = str(reply_doc.get("id") or "")
    offload = (
        object_storage.chat_files_enabled()
        and reply_doc.get("content_type") in _R2_OFFLOAD_CONTENT_TYPES
        and reply_doc.get("body_ct") is not None
    )
    trimmed_docs: list = []
    trimmed_ids: list[str] = []
    try:
        if max_messages and max_messages > 0:
            with get_pool().connection() as conn:
                rows = conn.execute(
                    "DELETE FROM chat_messages WHERE user_id = %s AND seq < ("
                    "  SELECT MIN(seq) FROM ("
                    "    SELECT seq FROM chat_messages WHERE user_id = %s "
                    "    ORDER BY seq DESC LIMIT %s"
                    "  ) t"
                    ") RETURNING msg_id, doc",
                    (user_id, user_id, max_messages),
                ).fetchall()
                trimmed_ids = [row[0] for row in rows]
                trimmed_docs = [row[1] for row in rows]

        if offload:
            try:
                body_ct_len = len(reply_doc["body_ct"])
                key = object_storage.put_chat_body(
                    user_id,
                    reply_msg_id,
                    reply_doc["body_ct"],
                    str(reply_doc.get("content_type") or "file"),
                )
                pointer = {"body_key": key, "body_ct_len": body_ct_len}
                with get_pool().connection() as conn:
                    conn.execute(
                        "UPDATE chat_messages SET doc = (doc - 'body_ct') || %s "
                        "WHERE user_id = %s AND msg_id = %s AND doc ? 'body_ct'",
                        (Jsonb(pointer), user_id, reply_msg_id),
                    )
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[db] chat_finalize_reply_post_commit(%s,%s) R2 offload "
                    "failed, left inline: %s",
                    user_id,
                    reply_msg_id,
                    e,
                )

        if trimmed_docs and object_storage.chat_files_enabled():
            for trimmed in trimmed_docs:
                if isinstance(trimmed, dict) and trimmed.get("body_key"):
                    object_storage.delete_chat_body(
                        str(trimmed["body_key"]), user_id
                    )
    except Exception as e:  # noqa: BLE001
        log.error(
            "[db] chat_finalize_reply_post_commit(%s,%s) failed: %s",
            user_id,
            reply_msg_id,
            e,
        )
        return

    if trimmed_ids:
        from tee_shadow import mirror

        mirror.execute_many([
            (
                "DELETE FROM chat_messages WHERE user_id = %s AND msg_id = ANY(%s)",
                (user_id, trimmed_ids),
            ),
            (
                "DELETE FROM tee_pending_device_migration WHERE user_id = %s "
                "AND table_name = 'chat_messages' AND item_id = ANY(%s)",
                (user_id, trimmed_ids),
            ),
        ])


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
      verify_ping probes and resident maintenance prompts are not conversation
      and never supersede."""
    same_consumer_sql = "" if redelivery else "OR doc->>'reply_claimed_by' = %s "
    unanswered_tail_sql = (
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM chat_messages n "
        "    WHERE n.user_id = chat_messages.user_id "
        "      AND n.ts > chat_messages.ts "
        "      AND n.doc->>'role' = 'user' "
        "      AND COALESCE(n.doc->>'source','') NOT IN ('verify_ping','resident_maintenance') "
        "      AND ((n.doc->>'reply_status') = 'replied' "
        "           OR COALESCE(n.doc->>'reply_message_id','') <> '')"
        "  ) "
    ) if redelivery else ""
    params: list = [Jsonb(fields), user_id, msg_id]
    if not redelivery:
        params.append(consumer_id)
    params.append(now)
    sql = (
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
        ") RETURNING doc"
    )
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
    except Exception as e:
        log.error("[db] chat_try_claim_reply(%s,%s) failed: %s", user_id, msg_id, e)
        return None
    if row is None:
        return None
    # Only mirror the claim itself (a real state transition) — not a
    # rejected/no-op attempt, per the brief's "claim 状态字段更新" scope.
    from tee_shadow import mirror
    mirror.execute(sql, tuple(params))
    # This is a delivery exit — the resident consumer decrypts the returned
    # doc, so an R2-offloaded file must arrive with body_ct inlined.
    return hydrate_chat_file_body(user_id, row[0])


def chat_expire_reply_claims(user_id: str) -> int:
    """释放该用户所有「已 claim 但尚未回复」的 chat 行的 claim。

    切 active route 会 respawn consumer（supervisor._spawn_identity 变了）。被 kill 的
    旧 consumer 持有的 claim 否则要等 CHAT_POLL_CLAIM_TTL_SEC（默认 600s）才过期，
    lost-turn redelivery backstop 才会重投那条消息。主动清空让新 consumer 立刻接手。

    WHERE 条件与 chat_try_claim_reply 的 CAS 对齐：只碰未回复（reply_status 不是
    replied 且 reply_message_id 为空）且当前确实被 claim 住的行。"""
    sql = (
        "UPDATE chat_messages "
        "SET doc = doc || '{\"reply_claimed_by\":\"\",\"reply_claim_expires_at\":\"\"}'::jsonb "
        "WHERE user_id = %s "
        "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
        "  AND COALESCE(doc->>'reply_message_id','') = '' "
        "  AND COALESCE(doc->>'reply_claimed_by','') <> ''"
    )
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (user_id,))
    except Exception as e:
        log.error("[db] chat_expire_reply_claims(%s) failed: %s", user_id, e)
        return 0
    if cur.rowcount:
        # 与 chat_try_claim_reply 的 claim mirror 对称：claim 的释放同样是
        # claim 状态字段更新，不镜像会让 TEE 侧残留已失效的 claim 字段。
        from tee_shadow import mirror
        mirror.execute(sql, (user_id,))
    return cur.rowcount


def chat_delete(user_id: str, msg_id: str) -> bool:
    sql = "DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s"
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql + " RETURNING doc", (user_id, msg_id)).fetchone()
    except Exception as e:
        log.error("[db] chat_delete(%s,%s) failed: %s", user_id, msg_id, e)
        return False
    # Mirror unconditionally (idempotent DELETE): even a not-found primary delete
    # may self-heal a TEE row left behind by an earlier missed mirror write.
    from tee_shadow import mirror
    mirror.execute_many([
        (sql, (user_id, msg_id)),
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
         "AND table_name = 'chat_messages' AND item_id = %s", (user_id, msg_id)),
    ])
    if row is None:
        return False
    # Drop the offloaded R2 body if this row was a pointer. Driven by the row's own
    # body_key — see the trim path in chat_append.
    doc = row[0]
    if (
        object_storage.chat_files_enabled()
        and isinstance(doc, dict)
        and doc.get("body_key")
    ):
        object_storage.delete_chat_body(str(doc["body_key"]), user_id)
    return True


def chat_clear(user_id: str) -> int | None:
    """Delete every chat row for one user. Returns deleted row count, or None
    if the database operation failed."""
    sql = "DELETE FROM chat_messages WHERE user_id = %s"
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (user_id,))
        # Prefix-delete every offloaded chat-file body for this user (cheap no-op
        # when R2 is unconfigured or the user never sent a file).
        if object_storage.chat_files_enabled():
            object_storage.delete_user_chat_files(user_id)
    except Exception as e:
        log.error("[db] chat_clear(%s) failed: %s", user_id, e)
        return None
    from tee_shadow import mirror
    mirror.execute_many([
        (sql, (user_id,)),
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
         "AND table_name = 'chat_messages'", (user_id,)),
    ])
    return cur.rowcount


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
    except Exception as e:
        log.error("[db] memory_upsert(%s,%s) failed: %s", user_id, moment_id, e)
        return False
    # Primary committed. This is a same-PK in-place rewrite (insert-or-edit),
    # which the append-only replicator cursor never revisits once the PK has
    # been seen (or, for a back-dated occurred_at, may never even reach in
    # forward scan order) — same requeue-lane pattern as memory_replace_all's
    # survivors. Best-effort: mirror swallows failures.
    from tee_shadow import mirror
    mirror.mark_pending(user_id, "memory_moments", moment_id, "requeue")
    return True


def memory_delete(user_id: str, moment_id: str) -> bool:
    sql = "DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s"
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (user_id, moment_id))
    except Exception as e:
        log.error("[db] memory_delete(%s,%s) failed: %s", user_id, moment_id, e)
        return False
    from tee_shadow import mirror
    mirror.execute_many([
        (sql, (user_id, moment_id)),
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
         "AND table_name = 'memory_moments' AND item_id = %s", (user_id, moment_id)),
    ])
    return cur.rowcount > 0


def memory_replace_all(user_id: str, moments: list[dict]) -> None:
    """Atomically reconcile the stored moment set to `moments`. The final row
    set equals the input list (full-replace semantics preserved), but only rows
    that were removed are deleted and only rows whose doc changed are upserted,
    so a single-card edit no longer rewrites the user's entire garden. Used
    where the old code did load-list / mutate / save-whole-list."""
    removed_ids: list[str] = []
    survivor_ids: list[str] = []
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

                removed_ids = list(existing.keys() - new.keys())
                survivor_ids = list(new.keys())
                for mid in removed_ids:
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
        return
    # Primary committed → propagate to the TEE shadow (best-effort). memory rows
    # are ciphertext→plaintext REPLICATED (not dual-written), and an in-place
    # edit keeps the same (occurred_at, moment_id) PK while a back-dated insert
    # lands BEHIND the append-only cursor — the replicator never revisits either.
    # So: mirror the pinned DELETEs for removed ids (same pattern as
    # frame_prune_to) + enqueue every survivor on the requeue lane. memory sets
    # are small (tens), so requeue-all-survivors is acceptable churn (brief §C3).
    from tee_shadow import mirror
    if removed_ids:
        # Same "pin to actual eviction + clear its pending marker" pattern used
        # throughout: a removed moment may itself carry a stale pending row
        # (e.g. it was mid-requeue), which would otherwise outlive the now-gone
        # RDS row and permanently unbalance verify's rds == tee + pending count.
        mirror.execute_many([
            ("DELETE FROM memory_moments WHERE user_id = %s AND moment_id = ANY(%s)",
             (user_id, removed_ids)),
            ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
             "AND table_name = 'memory_moments' AND item_id = ANY(%s)",
             (user_id, removed_ids)),
        ])
    for mid in survivor_ids:
        mirror.mark_pending(user_id, "memory_moments", mid, "requeue")


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
    except Exception as e:
        log.error("[db] world_book_upsert(%s,%s) failed: %s", user_id, entry_id, e)
        return False
    # Same same-PK in-place-rewrite reasoning as memory_upsert above — requeue
    # so the next replicator pass re-derives the TEE plaintext. Best-effort:
    # mirror swallows failures.
    from tee_shadow import mirror
    mirror.mark_pending(user_id, "world_book_entries", entry_id, "requeue")
    return True


def world_book_delete(user_id: str, entry_id: str) -> bool:
    sql = "DELETE FROM world_book_entries WHERE user_id = %s AND entry_id = %s"
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(sql, (user_id, entry_id))
    except Exception as e:
        log.error("[db] world_book_delete(%s,%s) failed: %s", user_id, entry_id, e)
        return False
    from tee_shadow import mirror
    mirror.execute_many([
        (sql, (user_id, entry_id)),
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
         "AND table_name = 'world_book_entries' AND item_id = %s", (user_id, entry_id)),
    ])
    return cur.rowcount > 0


def world_book_replace_all(user_id: str, entries: list[dict]) -> None:
    # NOTE: currently has no callers (grep-confirmed); the TEE-propagation fix
    # below is applied for symmetry with memory_replace_all (cheap + correct) so
    # it can't silently reintroduce the C3 defect if a caller is added later.
    removed_ids: list[str] = []
    survivor_ids: list[str] = []
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT entry_id, updated_at, doc FROM world_book_entries WHERE user_id = %s",
                    (user_id,),
                ).fetchall()
                existing = {r[0]: (r[1], r[2]) for r in rows}
                new = {str(e["id"]): e for e in entries if e.get("id")}
                removed_ids = list(existing.keys() - new.keys())
                survivor_ids = list(new.keys())
                for entry_id in removed_ids:
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
        return
    # Same disease/cure as memory_replace_all (world_book is also ciphertext→
    # plaintext replicated with an in-place-editable (updated_at, entry_id) PK).
    from tee_shadow import mirror
    if removed_ids:
        mirror.execute_many([
            ("DELETE FROM world_book_entries WHERE user_id = %s AND entry_id = ANY(%s)",
             (user_id, removed_ids)),
            ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
             "AND table_name = 'world_book_entries' AND item_id = ANY(%s)",
             (user_id, removed_ids)),
        ])
    for entry_id in survivor_ids:
        mirror.mark_pending(user_id, "world_book_entries", entry_id, "requeue")


# ─────────────────────────── model_api credentials / routes ───────────────────
#
# 取代单条 user_blobs(kind='model_api')。credentials 一把 key 一行（envelope 密文），
# routes 是 (credential, model) 组合。`model_api_routes_one_active` 这个 partial
# unique index 让「每用户恰一条 active」由 DB 强制，而不是靠调用方自觉。
#
# ⚠️ ``model_api_routes_list`` 刻意 **不返回** api_key_envelope——它直接喂给
# GET /v1/model_api/routes 的响应体。只有 ``model_api_active_route`` 带 envelope，
# 供 config_store 走 enclave 解密。别在 list 里加回来。

_ROUTE_COLUMNS = """
    r.id::text, r.credential_id::text, c.provider, r.model, c.label,
    c.api_key_hint, c.base_url, c.supports_responses,
    COALESCE(r.reasoning_effort, ''), r.is_active, r.test_status,
    COALESCE(to_char(r.last_test_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), ''),
    r.last_test_error, r.last_runtime_error, r.last_runtime_error_class,
    COALESCE(to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), ''),
    COALESCE(to_char(r.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), '')
"""


def _route_row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0], "credential_id": row[1], "provider": row[2], "model": row[3],
        "credential_label": row[4], "api_key_hint": row[5], "base_url": row[6],
        "supports_responses": bool(row[7]), "reasoning_effort": row[8],
        "is_active": bool(row[9]), "test_status": row[10], "last_test_at": row[11],
        "last_test_error": row[12], "last_runtime_error": row[13],
        "last_runtime_error_class": row[14],
        "created_at": row[15], "updated_at": row[16],
    }


def model_api_credentials_list(user_id: str) -> list[dict]:
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id::text, provider, label, base_url, api_key_hint, "
                "       supports_responses "
                "FROM model_api_credentials WHERE user_id = %s ORDER BY created_at, id",
                (user_id,),
            ).fetchall()
        return [{"id": r[0], "provider": r[1], "label": r[2], "base_url": r[3],
                 "api_key_hint": r[4], "supports_responses": bool(r[5])} for r in rows]
    except Exception as e:
        log.error("[db] model_api_credentials_list(%s) failed: %s", user_id, e)
        return []


def model_api_credential_get(user_id: str, credential_id: str) -> dict | None:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT id::text, provider, label, base_url, api_key_hint, "
                "       supports_responses, api_key_envelope "
                "FROM model_api_credentials WHERE user_id = %s AND id = %s",
                (user_id, credential_id),
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "provider": row[1], "label": row[2], "base_url": row[3],
                "api_key_hint": row[4], "supports_responses": bool(row[5]),
                "api_key_envelope": row[6]}
    except Exception as e:
        log.error("[db] model_api_credential_get(%s,%s) failed: %s", user_id, credential_id, e)
        return None


def model_api_credential_create(user_id: str, *, provider: str, base_url: str,
                                label: str, api_key_envelope: dict,
                                api_key_hint: str, supports_responses: bool) -> str | None:
    """总是新建一条 credential，返回其 id。

    同一 (user_id, provider, base_url) 下允许多条 —— 用户可以为同一个 provider
    存多把 key（个人的 / 团队的）。setup 的幂等不靠唯一索引，而是在 setup_core 里
    锚定 active route 的 credential 决定「更新」还是「新建」。
    """
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO model_api_credentials "
                "  (id, user_id, provider, label, base_url, api_key_envelope, "
                "   api_key_hint, supports_responses) "
                "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id::text",
                (user_id, provider, label, base_url, Jsonb(api_key_envelope),
                 api_key_hint, supports_responses),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("[db] model_api_credential_create(%s,%s) failed: %s", user_id, provider, e)
        return None


def model_api_credential_update(user_id: str, credential_id: str, *,
                               label: str | None = None,
                               api_key_envelope: dict | None = None,
                               api_key_hint: str | None = None,
                               supports_responses: bool | None = None) -> bool:
    sets, params = [], []
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if api_key_envelope is not None:
        sets.append("api_key_envelope = %s")
        params.append(Jsonb(api_key_envelope))
    if api_key_hint is not None:
        sets.append("api_key_hint = %s")
        params.append(api_key_hint)
    if supports_responses is not None:
        sets.append("supports_responses = %s")
        params.append(supports_responses)
    if not sets:
        return False
    sets.append("updated_at = now()")
    params += [user_id, credential_id]
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                f"UPDATE model_api_credentials SET {', '.join(sets)} "
                "WHERE user_id = %s AND id = %s",
                tuple(params),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_credential_update(%s,%s) failed: %s", user_id, credential_id, e)
        return False


def model_api_credential_delete(user_id: str, credential_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM model_api_credentials WHERE user_id = %s AND id = %s",
                (user_id, credential_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_credential_delete(%s,%s) failed: %s", user_id, credential_id, e)
        return False


def model_api_routes_list(user_id: str) -> list[dict]:
    """不含 api_key_envelope——直接喂给 GET /v1/model_api/routes 的响应。"""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                f"SELECT {_ROUTE_COLUMNS} "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s ORDER BY r.created_at, r.id",
                (user_id,),
            ).fetchall()
        return [_route_row_to_dict(r) for r in rows]
    except Exception as e:
        log.error("[db] model_api_routes_list(%s) failed: %s", user_id, e)
        return []


def model_api_route_get(user_id: str, route_id: str) -> dict | None:
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_ROUTE_COLUMNS} "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s AND r.id = %s",
                (user_id, route_id),
            ).fetchone()
        return _route_row_to_dict(row) if row else None
    except Exception as e:
        log.error("[db] model_api_route_get(%s,%s) failed: %s", user_id, route_id, e)
        return None


def model_api_active_route(user_id: str) -> dict | None:
    """带 api_key_envelope —— 供 config_store 走 enclave 解密。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_ROUTE_COLUMNS}, c.api_key_envelope "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s AND r.is_active",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        out = _route_row_to_dict(row)
        # envelope is appended AFTER _ROUTE_COLUMNS, so read it by tail index — this
        # stays correct if _ROUTE_COLUMNS ever grows again (it just did: created_at/
        # updated_at), unlike a hardcoded positional index.
        out["api_key_envelope"] = row[-1]
        return out
    except Exception as e:
        log.error("[db] model_api_active_route(%s) failed: %s", user_id, e)
        return None


def model_api_route_upsert(user_id: str, credential_id: str, model: str,
                           reasoning_effort: str | None) -> str | None:
    """按 (credential_id, model) upsert。跨用户引用会被复合外键拒绝 → 返回 None。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO model_api_routes "
                "  (id, user_id, credential_id, model, reasoning_effort) "
                "VALUES (gen_random_uuid(), %s, %s, %s, %s) "
                "ON CONFLICT (credential_id, model) DO UPDATE SET "
                "  reasoning_effort = EXCLUDED.reasoning_effort, updated_at = now() "
                "RETURNING id::text",
                (user_id, credential_id, model, reasoning_effort),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("[db] model_api_route_upsert(%s,%s,%s) failed: %s",
                  user_id, credential_id, model, e)
        return None


def model_api_route_delete(user_id: str, route_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "DELETE FROM model_api_routes WHERE user_id = %s AND id = %s",
                (user_id, route_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_delete(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_activate(user_id: str, route_id: str) -> bool:
    """切换 active route。两条 UPDATE 语句包在一个事务里，等价于原子切换。

    NOTE: 最初按 plan 写成单条 ``SET is_active = (id = %s) WHERE ... (is_active
    OR id = %s)``，理论上「唯一索引在语句末检查」——但这对 Postgres 的非
    DEFERRABLE partial unique index 不成立：同一条 UPDATE 语句内，多行的索引维护
    是逐行进行的，不保证按插入顺序处理；只要目标行先于旧 active 行被处理，就会在
    行内瞬间出现两条 is_active=true，直接撞 ``model_api_routes_one_active`` 报
    duplicate key。已用最小复现验证（先 activate(r1) 触发一次行迁移改变物理顺序，
    再 activate(r2) 必现），因此改为显式事务内两条语句：先把「非目标的当前 active
    行」置 false，再把目标行置 true。这在同一事务内对旧 active 行加了行锁，并发
    activate 会在该行上排队序列化，正确性等价于「单语句」的设计意图，且已被
    test_activate_leaves_exactly_one_active 覆盖住。

    先在事务内 ``SELECT ... FOR UPDATE`` 确认目标 route 存在且属于该用户：不存在
    就直接返回 False、**绝不做任何写入**。否则第一条「清 active」的 UPDATE 会在
    route_id 不存在/属于别人时把用户当前的 active route 误清掉——那会让他从
    ``list_agent_runtime_enabled_users`` 的 roster 消失、supervisor 杀掉 consumer
    且不自愈，客户端发一个陈旧 route_id 就能把自己的托管 agent 打停。``FOR UPDATE``
    顺便锁住目标行，对并发也有好处。route_id 非法 UUID 字面量时 psycopg cast 抛异常，
    被外层 except 接住返回 False——也是我们要的。"""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                target = conn.execute(
                    "SELECT 1 FROM model_api_routes WHERE user_id = %s AND id = %s FOR UPDATE",
                    (user_id, route_id),
                ).fetchone()
                if target is None:
                    return False          # 目标不存在/不属于该用户 —— 绝不能有副作用
                conn.execute(
                    "UPDATE model_api_routes SET is_active = FALSE, updated_at = now() "
                    "WHERE user_id = %s AND is_active AND id != %s",
                    (user_id, route_id),
                )
                cur = conn.execute(
                    "UPDATE model_api_routes SET is_active = TRUE, updated_at = now() "
                    "WHERE user_id = %s AND id = %s",
                    (user_id, route_id),
                )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_activate(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_mark_test(user_id: str, route_id: str, *, status: str, error: str = "") -> bool:
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE model_api_routes SET test_status = %s, last_test_error = %s, "
                "       last_test_at = now(), updated_at = now() "
                "WHERE user_id = %s AND id = %s",
                (status, str(error or "")[:300], user_id, route_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_mark_test(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_mark_runtime_error(user_id: str, *, error: str, error_class: str | None) -> bool:
    """写 active route 行。传空串即清空（agent-runner 回合成功时调用）。

    ``error_class=None`` 时只更新 ``last_runtime_error`` 列，不动
    ``last_runtime_error_class`` —— 供 legacy inline action-trace 路径调用：那条
    路径从不知道 error 的 class（只有 agent-runner 的 record_runtime_error 会算
    出 class），传 None 保留 record_runtime_error 已经写入的 class 不被清空，
    等价于旧 user_blobs 时代 _patch_model_api_runtime_profile 按 key 合并、从不
    覆盖它没提到的字段的行为。"""
    try:
        with get_pool().connection() as conn:
            if error_class is None:
                cur = conn.execute(
                    "UPDATE model_api_routes SET last_runtime_error = %s, updated_at = now() "
                    "WHERE user_id = %s AND is_active",
                    (str(error or "")[:300], user_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE model_api_routes SET last_runtime_error = %s, "
                    "       last_runtime_error_class = %s, updated_at = now() "
                    "WHERE user_id = %s AND is_active",
                    (str(error or "")[:300], str(error_class or "")[:64], user_id),
                )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_mark_runtime_error(%s) failed: %s", user_id, e)
        return False


def model_api_route_deactivate(user_id: str, route_id: str) -> bool:
    """清掉某条 route 的 is_active（不接管）。供 test 失败后先腾位再 autoselect。

    ``model_api_autoselect_active`` 只考虑 ``NOT is_active`` 的候选——对着一条仍
    ``is_active=TRUE`` 的行调它会撞 ``model_api_routes_one_active`` partial unique
    index（试图让第二行也变 active）。调用方必须先用这个函数腾位，再 autoselect。"""
    try:
        with get_pool().connection() as conn:
            cur = conn.execute(
                "UPDATE model_api_routes SET is_active = FALSE, updated_at = now() "
                "WHERE user_id = %s AND id = %s AND is_active",
                (user_id, route_id),
            )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_deactivate(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_autoselect_active(user_id: str) -> str | None:
    """删掉 active route 之后重新选主：挑 updated_at 最新的 ok route。
    没有候选则返回 None（该用户从 roster 消失，consumer 会停）。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "UPDATE model_api_routes SET is_active = TRUE, updated_at = now() "
                "WHERE id = ("
                "  SELECT id FROM model_api_routes "
                "  WHERE user_id = %s AND test_status = 'ok' AND NOT is_active "
                "  ORDER BY updated_at DESC, id LIMIT 1"
                ") RETURNING id::text",
                (user_id,),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        log.error("[db] model_api_autoselect_active(%s) failed: %s", user_id, e)
        return None


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
    # TEE's shadow table is `frames` (different columns; same user_id/frame_id
    # PK) — see backend/alembic_tee/versions/0001_tee_baseline.py. pending_table
    # for frames is "frame_envelopes" (worker._TABLES / verify._CIPHERTEXT_TABLES
    # key), not "frames".
    from tee_shadow import mirror
    mirror.execute_many([
        ("DELETE FROM frames WHERE user_id = %s AND frame_id = %s", (user_id, frame_id)),
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
         "AND table_name = 'frame_envelopes' AND item_id = %s", (user_id, frame_id)),
    ])
    if object_storage.enabled():
        object_storage.delete_frame_body(user_id, frame_id)
        # Also reap the TEE storage-layer re-encrypted body (frames-tee/) so a
        # single-frame delete doesn't orphan it (best-effort, same style).
        object_storage.delete_frame_tee_body(user_id, frame_id)


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
        if evicted:
            # Pin the mirror delete to the EXACT ids evicted from RDS (rather
            # than re-deriving "newest max_frames" independently against TEE's
            # `frames` table, which could have a different row set/order).
            # Also clear any pending marker those ids carry (pending_table is
            # "frame_envelopes", not "frames" — see frame_delete).
            from tee_shadow import mirror
            mirror.execute_many([
                ("DELETE FROM frames WHERE user_id = %s AND frame_id = ANY(%s)",
                 (user_id, evicted)),
                ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
                 "AND table_name = 'frame_envelopes' AND item_id = ANY(%s)",
                 (user_id, evicted)),
            ])
        if evicted and object_storage.enabled():
            for fid in evicted:
                object_storage.delete_frame_body(user_id, fid)
                # Reap the TEE storage-layer re-encrypted body too (frames-tee/).
                object_storage.delete_frame_tee_body(user_id, fid)
        return evicted
    except Exception as e:
        log.error("[db] frame_prune_to(%s) failed: %s", user_id, e)
        return []


# ---------------------------------------------------------------------------
# Per-user append logs (the 6 JSONL streams)
# ---------------------------------------------------------------------------


def log_append(user_id: str, stream: str, doc: dict,
               ts: float | None = None, item_key: str | None = None) -> None:
    sql = ("INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
           "VALUES (%s, %s, %s, %s, %s) RETURNING seq")
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql, (user_id, stream, ts, item_key, Jsonb(doc))).fetchone()
    except Exception as e:
        log.error("[db] log_append(%s,%s) failed: %s", user_id, stream, e)
        return
    if row is None:
        return
    # Mirror with the PRIMARY-assigned seq pinned explicitly (OVERRIDING SYSTEM
    # VALUE): user_logs.seq is GENERATED ALWAYS AS IDENTITY, so a plain INSERT
    # on the TEE side would mint its own, independent seq and break the
    # cross-db row-identity invariant every seq-ordered read path relies on.
    # ON CONFLICT DO NOTHING makes a reconciler replay of this same row
    # (same PK (user_id, stream, seq)) idempotent rather than erroring.
    from tee_shadow import mirror
    mirror_sql = (
        "INSERT INTO user_logs (user_id, stream, seq, ts, item_key, doc) "
        "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, stream, seq) DO NOTHING"
    )
    mirror.execute(mirror_sql, (user_id, stream, row[0], ts, item_key, Jsonb(doc)))


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
    try:
        with get_pool().connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
    except Exception as e:
        log.error("[db] log_patch_item(%s,%s,%s) failed: %s", user_id, stream, item_key, e)
        return None
    if row is not None:
        # Only mirror when the primary's guard (only_if_status) actually
        # matched a row — a rejected/no-op guarded update must not be
        # replayed against TEE (same convention as chat_try_claim_reply /
        # scheduled_wake claim_due). A missed real update is caught by the
        # reconciler; mirroring a rejection would forge a transition that
        # never happened on the primary.
        from tee_shadow import mirror
        mirror.execute(sql, tuple(params))
    return row[0] if row is not None else None


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
    try:
        with get_pool().connection() as conn:
            conn.execute(sql, params)
    except Exception as e:
        log.error("[db] log_trim(%s,%s) failed: %s", user_id, stream, e)
        return
    from tee_shadow import mirror
    mirror.execute(sql, params)


def log_prune_older_than(user_id: str, stream: str, cutoff_epoch: float) -> None:
    """Delete rows whose ts is older than the cutoff. Rows with NULL ts are
    kept (those streams don't carry an epoch ts)."""
    sql = ("DELETE FROM user_logs WHERE user_id = %s AND stream = %s "
           "AND ts IS NOT NULL AND ts < %s")
    params = (user_id, stream, cutoff_epoch)
    try:
        with get_pool().connection() as conn:
            conn.execute(sql, params)
    except Exception as e:
        log.error("[db] log_prune_older_than(%s,%s) failed: %s", user_id, stream, e)
        return
    from tee_shadow import mirror
    mirror.execute(sql, params)


# ---------------------------------------------------------------------------
# Account reset
# ---------------------------------------------------------------------------


def delete_user_data(user_id: str) -> None:
    """Redundant DB belt: per-user 行现由 delete_user 的 CASCADE 原子清净
    (0011)。仍被 content/content_core.py 的销号(account/reset)兜底路径调用；
    删账号主路径不再依赖它做 R2。"""
    tables = (
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
        "model_api_routes",
        "model_api_credentials",
    )
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                for table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
    except Exception as e:
        log.error("[db] delete_user_data(%s) failed: %s", user_id, e)
        return
    # TEE: frame_envelopes -> frames (different shape, same PK). Skipped from the
    # mirror group: genesis_import_chunks (staging data, never replicated — see
    # 0001_tee_baseline.py) and the model_api_* tables (0014 multi-profile,
    # added upstream after the TEE 19-table baseline; not replicated).
    tee_table_for = {"frame_envelopes": "frames"}
    _no_tee_tables = {"genesis_import_chunks", "model_api_routes", "model_api_credentials"}
    mirror_group = [
        (f"DELETE FROM {tee_table_for.get(table, table)} WHERE user_id = %s", (user_id,))
        for table in tables
        if table not in _no_tee_tables
    ]
    # Full-user wipe → no RDS row of ANY table survives for this user, so no
    # tee_pending_device_migration row should either (regardless of table_name
    # or reason) — clear the whole per-user pending set in one shot rather than
    # re-deriving it table-by-table.
    mirror_group.append(
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s", (user_id,)))
    from tee_shadow import mirror
    mirror.execute_many(mirror_group)


def delete_user_frames(user_id: str) -> None:
    """Best-effort R2 frame-body 清理(无 DB 行)。从 delete_user_data 拆出，
    使 DB 删除保持原子、R2 失败非致命。"""
    if object_storage.enabled():
        object_storage.delete_user_frames(user_id)


def delete_user_chat_files(user_id: str) -> None:
    """Best-effort R2 chat-file body cleanup (no DB rows — the chat_messages
    CASCADE already dropped the pointer rows). Mirrors delete_user_frames."""
    if object_storage.chat_files_enabled():
        object_storage.delete_user_chat_files(user_id)
