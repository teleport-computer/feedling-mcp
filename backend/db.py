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
worker has its own ``psycopg_pool.ConnectionPool`` (default max_size=16,
overridable with ``FEEDLING_DB_POOL_MAX_SIZE``) shared across its threads, plus
one pool-external connection for the LISTEN wake bus (see
``listen_connection`` / ``pg_notify`` and ``core/wake_bus.py``) and at most one
bounded pool-external config-lock connection. The long-poll
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
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
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


def _pool_max_size() -> int:
    """Return the process-local pool ceiling, failing closed on bad config.

    The ordinary backend default remains 16.  Hosted Runtime V2's standalone
    entrypoint sets this env before the pool is first opened so its nested
    outbox-transaction + sink-write shape has two connections per turn slot
    plus operational headroom.  Keeping the generic parser here means a typo
    cannot silently fall back to an undersized pool.
    """
    raw = os.environ.get("FEEDLING_DB_POOL_MAX_SIZE", "16").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("FEEDLING_DB_POOL_MAX_SIZE must be an integer >= 2") from exc
    if value < 2:
        raise RuntimeError("FEEDLING_DB_POOL_MAX_SIZE must be an integer >= 2")
    return value


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                _database_url(),
                min_size=2,
                max_size=_pool_max_size(),
                timeout=10,
                max_idle=300,
                kwargs={"autocommit": True},
                open=True,
            )
    return _pool


def close_pool() -> None:
    """Close and forget this process's pool.

    Gunicorn's master performs schema/policy startup work before forking. Any
    pool opened there must be fully stopped first: psycopg pool worker threads
    do not survive ``fork()``, and inheriting their connection objects into web
    workers can hang the first request. Each child lazily creates its own pool.
    """
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is not None:
        pool.close()


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
# above, each runner writes its OWN row keyed by ``owner`` (the stable production
# CVM ID, with ``<host>:<pid>`` as a local/dev fallback), so separate CVMs don't
# clobber one another. The backend's wedge guard lists
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

def load_all_users(*, raise_on_error: bool = False) -> list[dict]:
    """Return the full user registry as a list of dicts (each the verbatim
    stored user document), ordered by created_at.

    By default a read failure is swallowed and returns ``[]`` — the historical
    behavior most callers want. ``raise_on_error=True`` lets the error propagate
    so a caller can tell a failed read apart from a genuinely empty table; the
    registry reload uses it to avoid blanking ``_users`` (an empty registry 401s
    every request) when the database merely blipped."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT doc FROM users ORDER BY created_at NULLS FIRST, user_id"
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        log.error("[db] load_all_users failed: %s", e)
        if raise_on_error:
            raise
        return []


def load_user(user_id: str) -> dict | None:
    """Return ONE user document, or None when the row no longer exists.

    Backs the targeted ``users`` wake-bus reload: a per-row write (the resident
    poll heartbeat rewrites one binding roughly once a minute per online
    resident) must not make every subscribing process re-read the whole table.
    Like ``find_user_by_api_key_hash`` this DELIBERATELY lets a database error
    propagate — the caller has to tell a transient failure apart from a deleted
    row, because acting on a false "deleted" would evict a live user from the
    in-memory registry."""
    if not user_id:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
    return row[0] if row else None


def find_user_by_api_key_hash(h: str) -> dict | None:
    """Return the user document whose api key hashes to ``h``, or None.

    The shared source of truth for the in-memory registry's miss fallback
    (``accounts.registry._resolve_user``): under many workers a just-registered
    user may not yet be in a given worker's ``_users`` snapshot, and a pure
    in-memory miss would 401 a valid key. Matching mirrors the in-memory scan:
    the legacy top-level ``api_key_hash`` matches unconditionally, while an
    ``api_keys[]`` entry matches only when it is not revoked.

    Unlike the swallow-and-log helpers above, this DELIBERATELY lets a database
    error PROPAGATE. The caller must be able to tell a transient DB failure apart
    from a genuine "no such user": on an error it degrades to unauthenticated for
    that one attempt WITHOUT negative-caching, so a pool-timeout hiccup can't pin
    a valid key to 401 for the whole cache TTL. ``None`` here means only a
    definitive miss (the query ran and matched nothing)."""
    if not h:
        return None
    sql = (
        "SELECT doc FROM users WHERE doc->>'api_key_hash' = %s "
        "OR EXISTS (SELECT 1 FROM jsonb_array_elements("
        "COALESCE(doc->'api_keys', '[]'::jsonb)) AS k "
        "WHERE k->>'api_key_hash' = %s AND COALESCE(k->>'revoked_at', '') = '') "
        "LIMIT 1"
    )
    with get_pool().connection() as conn:
        row = conn.execute(sql, (h, h)).fetchone()
    return row[0] if row else None


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


def compare_and_set_user(
    user_id: str,
    expected: dict,
    new: dict,
) -> tuple[bool, bool, dict | None]:
    """Compare-and-set ONE user row: write ``new`` only if the stored doc still
    equals ``expected`` (JSONB equality is the CAS boundary).

    Returns ``(read_ok, applied, authoritative_doc)``:
    * ``read_ok=False`` — the database operation itself failed.
    * ``applied`` — whether our write landed (the stored doc matched expected).
    * ``authoritative_doc`` — the row now in the DB (``new`` when applied, the
      winning concurrent row when not, ``None`` when the row is now deleted).

    This is the primitive behind both startup normalization and the resident
    heartbeat: each reads a row, edits its own copy, and CAS-writes so a stale
    snapshot can neither overwrite a concurrent edit nor lose to one silently.
    """
    sql = (
        "UPDATE users SET created_at=%s, doc=%s "
        "WHERE user_id=%s AND doc=%s RETURNING doc"
    )
    params = (
        new.get("created_at"),
        Jsonb(new),
        str(user_id),
        Jsonb(expected),
    )
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(sql, params).fetchone()
                applied = row is not None
                if row is None:
                    row = conn.execute(
                        "SELECT doc FROM users WHERE user_id=%s",
                        (str(user_id),),
                    ).fetchone()
    except Exception as exc:
        log.error("[db] compare_and_set_user(%s) failed: %s", user_id, exc)
        return False, False, None
    if applied:
        # Mirror the SAME gated CAS to the shadow — deliberately NOT an
        # unconditional upsert. An unconditional upsert re-creates a shadow row
        # whose primary was concurrently deleted: db.delete_user removes the row
        # from primary then shadow, and if it lands between this CAS commit and
        # this mirror, the upsert resurrects the deleted user's encrypted row in
        # the shadow until the 24h prune — a data-retention gap. The gated
        # `WHERE user_id=%s AND doc=%s` form no-ops on a missing shadow row, so
        # it can never resurrect. A shadow row that has drifted is left for the
        # reconcile job (the shadow's own convergence backstop), not force-healed
        # here — the heartbeat drives this path ~once/min per online resident, so
        # the safe choice is the one that cannot resurrect deleted data.
        from tee_shadow import mirror
        mirror.execute(sql, params)
    return True, applied, (row[0] if row is not None else None)


def normalize_user_cas(
    user_id: str,
    expected: dict,
    normalized: dict,
) -> tuple[bool, dict | None]:
    """Persist startup normalization without overwriting a concurrent edit.

    Registry reloads run in every web/turn process. A stale process must neither
    full-rewrite the users table (deleting a just-registered account absent from
    its snapshot) nor replace a row changed after it was read. Thin wrapper over
    ``compare_and_set_user`` that drops the ``applied`` flag its callers don't
    need. Return ``(read_ok, authoritative_doc)`` so a CAS loser immediately
    replaces its randomly normalized local copy with the winning row.
    ``authoritative_doc=None`` means the row was concurrently deleted;
    ``read_ok=False`` means the database operation itself failed.
    """
    read_ok, _applied, authoritative = compare_and_set_user(
        user_id, expected, normalized
    )
    return read_ok, authoritative


def save_all_users(users: list[dict]) -> None:
    """Persist an explicit whole-registry snapshot for tests/offline tooling.

    Production startup and reconnect normalization must use
    ``normalize_user_cas`` instead; request-time edits use ``upsert_user``.

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
    (per-row, non-destructive); the remaining callers here deliberately own the
    complete snapshot they are replacing."""
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
                    removed_sql, removed_params = (
                        "SELECT user_id FROM users "
                        "WHERE NOT (user_id = ANY(%s))",
                        (keep_ids,),
                    )
                else:
                    removed_sql, removed_params = (
                        "SELECT user_id FROM users", ()
                    )
                removed_ids = sorted({
                    str(row[0])
                    for row in conn.execute(removed_sql, removed_params).fetchall()
                })
                # A bulk-registry removal is an account deletion just as much as
                # delete_user(). Advance the durable object generation before the
                # users-row CASCADE and leave inventory work for the isolated R2
                # cleanup worker; never make registry persistence wait on R2.
                with conn.cursor() as cur:
                    for removed_id in removed_ids:
                        _lock_chat_user_fence_on_cursor(
                            cur, removed_id, exclusive=True,
                        )
                        _mark_chat_r2_inventory_pending_on_cursor(
                            cur, removed_id, advance_generation=True,
                        )
                        # Preserve the global lifecycle -> users/chat lock order.
                        # A bulk users FOR UPDATE before lifecycle would deadlock
                        # against append/clear, which take the lifecycle fence
                        # first. Deleting only the IDs observed above also avoids
                        # erasing a concurrently registered user absent from this
                        # older full-registry snapshot.
                        cur.execute(
                            "DELETE FROM users WHERE user_id=%s", (removed_id,),
                        )
                        mirror_group.append(
                            ("DELETE FROM users WHERE user_id=%s", (removed_id,))
                        )
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


def delete_user(user_id: str) -> bool:
    """False means no such account — callers answer 404 instead of claiming a
    deletion that never happened."""
    sql = "DELETE FROM users WHERE user_id = %s"
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(
                    cur, user_id, exclusive=True,
                )
                # Under the exclusive fence an absent users row is final, so bail
                # out before the lifecycle marker below materializes a
                # chat_r2_lifecycle row for an account that never existed.
                cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
                if cur.fetchone() is None:
                    return False
                # Lock before the users-row DELETE/FK cascade. The chat DELETE
                # trigger writes cleanup rows with no users FK, so both lifecycle
                # generation and exact-key intents survive account removal.
                _mark_chat_r2_inventory_pending_on_cursor(
                    cur, user_id, advance_generation=True,
                )
                cur.execute(sql, (user_id,))
    from tee_shadow import mirror
    mirror.execute(sql, (user_id,))
    return True


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
    "snapshot_copied", "snapshot_failures",
    "prune_stale", "prune_deleted", "prune_refused",
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
                    """
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
                    """
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
                SELECT wanted.user_id,
                       COALESCE(vrs.hosted_runtime_state, 'resident'),
                       route.id::text,
                       route.is_active,
                       route.test_status,
                       ari.status,
                       ari.lease_owner,
                       COALESCE(
                         to_char(
                           ari.lease_expires_at AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                         ),
                         ''
                       ),
                       (
                         ari.lease_owner IS NOT NULL
                         AND ari.lease_expires_at IS NOT NULL
                         AND ari.lease_expires_at >= now()
                       ) AS runner_lease_active
                FROM unnest(%s::text[]) AS wanted(user_id)
                LEFT JOIN v2_runtime_state vrs ON vrs.user_id = wanted.user_id
                LEFT JOIN LATERAL (
                  SELECT id, is_active, test_status
                  FROM model_api_routes
                  WHERE user_id = wanted.user_id AND is_active
                  ORDER BY updated_at DESC, id
                  LIMIT 1
                ) route ON TRUE
                LEFT JOIN agent_runtime_instances ari
                  ON ari.user_id = wanted.user_id
                """,
                (ids,),
            ).fetchall()
            for (
                uid,
                runtime_state,
                route_id,
                route_active,
                route_test_status,
                runner_status,
                lease_owner,
                lease_expires_at,
                lease_active,
            ) in rows:
                ensure(out, uid)["responder_runtime"] = {
                    "hosted_runtime_state": str(runtime_state or "resident"),
                    "model_api_route": {
                        "id": str(route_id or ""),
                        "is_active": bool(route_active),
                        "test_status": str(route_test_status or ""),
                    },
                    "runner_lease": {
                        "active": bool(lease_active),
                        "status": str(runner_status or ""),
                        "lease_owner": str(lease_owner or ""),
                        "lease_expires_at": str(lease_expires_at or ""),
                    },
                }

            rows = conn.execute(
                """
                SELECT user_id, provider_state,
                       COALESCE(to_char(
                         last_provider_success_at AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                       ), ''),
                       COALESCE(to_char(
                         last_provider_failure_at AT TIME ZONE 'UTC',
                         'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                       ), ''),
                       last_provider_error_class,
                       last_provider_error_blame,
                       COALESCE(recent_latency_ms, 0)
                FROM provider_health
                WHERE user_id = ANY(%s)
                """,
                (ids,),
            ).fetchall()
            for (
                uid,
                provider_state,
                success_at,
                failure_at,
                error_class,
                error_blame,
                recent_latency_ms,
            ) in rows:
                ensure(out, uid)["provider_health"] = {
                    "provider_state": provider_state or "ok",
                    "last_provider_success_at": success_at or "",
                    "last_provider_failure_at": failure_at or "",
                    "last_provider_error_class": error_class or "",
                    "last_provider_error_blame": error_blame or "",
                    # A slow-but-working route explains "everything takes
                    # minutes", which none of the failure fields above can.
                    # Raw value only: the slow/not-slow verdict lives in
                    # provider_health, which imports db and so cannot be
                    # imported back from here.
                    "recent_latency_ms": round(float(recent_latency_ms or 0.0)),
                }

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
    day_limit = max(1, min(int(days or 30), 1000))
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


def _admin_utc_text_timestamp_sql(
    expression: str,
    *,
    supports_input_validation: bool,
) -> str:
    """Safely parse ISO text, treating legacy offset-less values as UTC.

    PostgreSQL 16 added ``pg_input_is_valid``.  Older supported/test clusters
    use the strict fallback below: its outer CASE proves the shape, the nested
    CASE proves the real number of days in the month, and only then can a cast
    run.  Keeping casts inside CASE branches matters because SQL does not
    promise WHERE/AND predicate evaluation order.
    """

    if supports_input_validation:
        return f"""
CASE
  WHEN ({expression}) ~ '(Z|[+-]\\d{{2}}:?\\d{{2}})$'
    AND pg_input_is_valid(({expression}), 'timestamptz')
    THEN ({expression})::timestamptz
  WHEN pg_input_is_valid(({expression}), 'timestamp')
    THEN ({expression})::timestamp AT TIME ZONE 'UTC'
  ELSE NULL
END
"""
    return f"""
CASE
  WHEN ({expression}) ~
    '^[0-9]{{4}}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])'
    '([T ]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'
    '(\\.[0-9]+)?(Z|[+-](0[0-9]|1[0-5]):?[0-5][0-9])?)?$'
  THEN CASE
    WHEN substring(({expression}) FROM 9 FOR 2)::int <= CASE
      WHEN substring(({expression}) FROM 6 FOR 2)::int IN (1,3,5,7,8,10,12)
        THEN 31
      WHEN substring(({expression}) FROM 6 FOR 2)::int IN (4,6,9,11)
        THEN 30
      WHEN (
        mod(substring(({expression}) FROM 1 FOR 4)::int, 400) = 0 OR (
          mod(substring(({expression}) FROM 1 FOR 4)::int, 4) = 0
          AND mod(substring(({expression}) FROM 1 FOR 4)::int, 100) <> 0
        )
      ) THEN 29
      ELSE 28
    END
    THEN CASE
      WHEN ({expression}) ~ '(Z|[+-]\\d{{2}}:?\\d{{2}})$'
        THEN ({expression})::timestamptz
      ELSE ({expression})::timestamp AT TIME ZONE 'UTC'
    END
    ELSE NULL
  END
  ELSE NULL
END
"""


def recent_admin_product_kpis(*, within_hours: int = 24) -> dict:
    """Rolling-window product/account KPIs for the Admin operations overview.

    This is intentionally *not* the daily DAU snapshot.  ``window_app_users``
    is one distinct-account count across the exact rolling 24h/7d/30d window;
    it does not sum daily DAU and therefore does not double-count a user active
    on multiple days.  The dedicated DAU page remains the Beijing-calendar-day
    source of truth.

    Onboarding reuses ``admin_onboarding_funnel``'s existing event milestones.
    Its denominator is parseable accounts registered inside this same rolling
    window, and "complete" means that cohort has reached first genuine reply
    (t3) by query time.  If the funnel cannot cover the registration cohort,
    the rate is returned as ``None`` rather than a fabricated zero.
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    with get_pool().connection() as conn:
        registered_at_sql = _admin_utc_text_timestamp_sql(
            "users.created_at",
            supports_input_validation=conn.info.server_version >= 160000,
        )
        row = conn.execute(
            f"""
            WITH bounds AS (
              SELECT extract(epoch FROM clock_timestamp())::double precision AS now_ts,
                     extract(epoch FROM (
                       clock_timestamp() - make_interval(hours => %s)
                     ))::double precision AS cutoff_ts
            ), sessions AS (
              SELECT logs.user_id
              FROM user_logs logs CROSS JOIN bounds
              WHERE logs.stream='tracking_events'
                AND logs.doc->>'type'='app_session_end'
                AND logs.ts IS NOT NULL
                AND logs.ts >= bounds.cutoff_ts AND logs.ts <= bounds.now_ts
            ), parsed_users AS (
              SELECT users.user_id,{registered_at_sql} AS registered_at
              FROM users
            ), registrations AS (
              SELECT parsed_users.user_id
              FROM parsed_users CROSS JOIN bounds
              WHERE extract(epoch FROM parsed_users.registered_at)
                    >= bounds.cutoff_ts
                AND extract(epoch FROM parsed_users.registered_at)
                    <= bounds.now_ts
            )
            SELECT
              bounds.now_ts,
              bounds.cutoff_ts,
              (SELECT count(DISTINCT user_id)::int FROM sessions)
                AS window_app_users,
              (SELECT count(*)::int FROM sessions) AS app_sessions,
              (SELECT count(*)::int FROM registrations)
                AS new_registered_accounts,
              (SELECT count(*)::int FROM parsed_users
               WHERE registered_at IS NULL)
                AS unparseable_registration_rows,
              (SELECT count(*)::int FROM parsed_users) AS account_rows
            FROM bounds
            """,
            (safe_hours,),
        ).fetchone()

    now_ts = float(row[0])
    cutoff_ts = float(row[1])
    new_registered = int(row[4] or 0)
    funnel_rows = admin_onboarding_funnel()
    cohort = [
        item
        for item in funnel_rows
        if item.get("t0") is not None
        and cutoff_ts <= float(item["t0"]) <= now_ts
    ]
    # ``admin_onboarding_funnel`` historically catches query errors and returns
    # ``[]``.  Compare its fleet row count as well as the window cohort so an
    # outage cannot look like a valid empty cohort when accounts do exist.
    funnel_coverage_complete = (
        len(funnel_rows) == int(row[6] or 0)
        and len(cohort) == new_registered
    )
    configured = sum(1 for item in cohort if item.get("t1") is not None)
    content_ready = sum(1 for item in cohort if item.get("t2") is not None)
    first_reply = sum(1 for item in cohort if item.get("t3") is not None)
    onboarding_rate = (
        float(first_reply) / float(new_registered)
        if funnel_coverage_complete and new_registered
        else None
    )
    return {
        "window_hours": safe_hours,
        "generated_at": now_ts,
        "cutoff_at": cutoff_ts,
        "window_app_users": int(row[2] or 0),
        "app_sessions": int(row[3] or 0),
        "new_registered_accounts": new_registered,
        "unparseable_registration_rows": int(row[5] or 0),
        "onboarding": {
            "definition": "registered_cohort_to_first_genuine_reply",
            "cohort_accounts": new_registered,
            "configured": configured if funnel_coverage_complete else None,
            "content_ready": content_ready if funnel_coverage_complete else None,
            "first_genuine_reply": first_reply if funnel_coverage_complete else None,
            "completion_rate": onboarding_rate,
            "coverage_complete": funnel_coverage_complete,
        },
    }


_ADMIN_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def admin_data_track_usage_histogram(
    *,
    day: str,
    tz: str = "Asia/Shanghai",
) -> dict:
    """Return one Beijing day's per-user app-usage distribution.

    The sample exactly matches ``admin_data_track_dau``'s ``usage_per_user``
    CTE: one row per user with at least one ``app_session_end`` event that day,
    summing only decimal ``payload.duration_sec`` values and treating malformed
    values as zero. Users without a session-end report are not backfilled with
    zero. All fixed buckets and summary percentiles are computed in one query.
    """
    day_text = str(day or "").strip()
    if not _ADMIN_DAY_RE.fullmatch(day_text):
        raise ValueError("invalid_day")
    try:
        selected_day = date.fromisoformat(day_text)
    except ValueError as exc:
        raise ValueError("invalid_day") from exc
    if selected_day.isoformat() != day_text:
        raise ValueError("invalid_day")

    empty = {
        "day": day_text,
        "buckets": [
            {"label": label, "lo_sec": lo, "hi_sec": hi, "users": 0}
            for label, lo, hi in (
                ("0-1min", 0, 60),
                ("1-5min", 60, 300),
                ("5-15min", 300, 900),
                ("15-30min", 900, 1800),
                ("30-60min", 1800, 3600),
                ("1-2h", 3600, 7200),
                ("2-4h", 7200, 14400),
                ("4h+", 14400, None),
            )
        ],
        "total_users": 0,
        "median_sec": 0.0,
        "mean_sec": 0.0,
        "p90_sec": 0.0,
        "max_sec": 0,
    }
    try:
        zone = ZoneInfo(tz)
        start = datetime.combine(
            selected_day, datetime.min.time(), tzinfo=zone
        ).timestamp()
        end = datetime.combine(
            selected_day + timedelta(days=1),
            datetime.min.time(),
            tzinfo=zone,
        ).timestamp()
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                WITH usage_per_user AS (
                    SELECT
                        user_id,
                        SUM(
                            CASE
                              WHEN doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'
                              THEN (doc->'payload'->>'duration_sec')::bigint
                              ELSE 0
                            END
                        )::bigint AS user_sec
                    FROM user_logs
                    WHERE stream = 'tracking_events'
                      AND doc->>'type' = 'app_session_end'
                      AND ts IS NOT NULL
                      AND ts >= %s AND ts < %s
                    GROUP BY user_id
                ),
                stats AS (
                    SELECT
                        COUNT(*)::int AS total_users,
                        COALESCE(
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY user_sec), 0
                        )::double precision AS median_sec,
                        COALESCE(AVG(user_sec), 0)::double precision AS mean_sec,
                        COALESCE(
                          percentile_cont(0.9) WITHIN GROUP (ORDER BY user_sec), 0
                        )::double precision AS p90_sec,
                        COALESCE(MAX(user_sec), 0)::bigint AS max_sec
                    FROM usage_per_user
                ),
                buckets(ord, label, lo_sec, hi_sec) AS (
                    VALUES
                      (1, '0-1min', 0::bigint, 60::bigint),
                      (2, '1-5min', 60::bigint, 300::bigint),
                      (3, '5-15min', 300::bigint, 900::bigint),
                      (4, '15-30min', 900::bigint, 1800::bigint),
                      (5, '30-60min', 1800::bigint, 3600::bigint),
                      (6, '1-2h', 3600::bigint, 7200::bigint),
                      (7, '2-4h', 7200::bigint, 14400::bigint),
                      (8, '4h+', 14400::bigint, NULL::bigint)
                )
                SELECT
                    b.label,
                    b.lo_sec,
                    b.hi_sec,
                    COUNT(u.user_id)::int AS users,
                    s.total_users,
                    s.median_sec,
                    s.mean_sec,
                    s.p90_sec,
                    s.max_sec
                FROM buckets b
                CROSS JOIN stats s
                LEFT JOIN usage_per_user u
                  ON u.user_sec >= b.lo_sec
                 AND (b.hi_sec IS NULL OR u.user_sec < b.hi_sec)
                GROUP BY
                    b.ord, b.label, b.lo_sec, b.hi_sec,
                    s.total_users, s.median_sec, s.mean_sec, s.p90_sec, s.max_sec
                ORDER BY b.ord
                """,
                (start, end),
            ).fetchall()
        if not rows:
            return empty
        return {
            "day": day_text,
            "buckets": [
                {
                    "label": str(row[0]),
                    "lo_sec": int(row[1]),
                    "hi_sec": int(row[2]) if row[2] is not None else None,
                    "users": int(row[3] or 0),
                }
                for row in rows
            ],
            "total_users": int(rows[0][4] or 0),
            "median_sec": float(rows[0][5] or 0),
            "mean_sec": float(rows[0][6] or 0),
            "p90_sec": float(rows[0][7] or 0),
            "max_sec": int(rows[0][8] or 0),
        }
    except Exception as e:
        log.error(
            "[db] admin_data_track_usage_histogram failed day=%s: %s",
            day_text,
            e,
        )
        return empty


def admin_data_track_user_daily_usage(
    *,
    user_id: str,
    days: int = 14,
    tz: str = "Asia/Shanghai",
) -> list[dict]:
    """Return one user's app usage for the latest ``days`` local dates.

    The series includes today and explicitly backfills dates without a
    session-end report. Duration parsing is intentionally identical to the
    all-time data-track snapshot: decimal ``payload.duration_sec`` contributes
    to foreground time; malformed values count as sessions but contribute zero.
    """
    try:
        day_limit = max(1, min(int(14 if days is None else days), 90))
    except (TypeError, ValueError):
        day_limit = 14

    # Keep the read-helper fail-soft contract while still returning the useful
    # zero-filled shape if PostgreSQL is temporarily unavailable.
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    empty = [
        {
            "day": (today - timedelta(days=offset)).isoformat(),
            "foreground_sec": 0,
            "sessions": 0,
            "max_session_sec": 0,
        }
        for offset in range(day_limit)
    ]
    empty.reverse()

    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                WITH local_clock AS (
                    SELECT timezone(%s, CURRENT_TIMESTAMP)::date AS today
                ),
                bounds AS (
                    SELECT
                        today,
                        EXTRACT(EPOCH FROM (
                            (today - (%s::int - 1))::timestamp AT TIME ZONE %s
                        ))::double precision AS start_epoch,
                        EXTRACT(EPOCH FROM (
                            (today + 1)::timestamp AT TIME ZONE %s
                        ))::double precision AS end_epoch
                    FROM local_clock
                ),
                calendar AS (
                    SELECT generate_series(
                        (b.today - (%s::int - 1))::timestamp,
                        b.today::timestamp,
                        interval '1 day'
                    )::date AS day
                    FROM bounds b
                ),
                usage AS (
                    SELECT
                        timezone(%s, to_timestamp(l.ts))::date AS day,
                        COALESCE(SUM(
                            CASE
                              WHEN l.doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'
                              THEN (l.doc->'payload'->>'duration_sec')::bigint
                              ELSE 0
                            END
                        ), 0)::bigint AS foreground_sec,
                        COUNT(*)::int AS sessions,
                        COALESCE(MAX(
                            CASE
                              WHEN l.doc->'payload'->>'duration_sec' ~ '^[0-9]{1,10}$'
                              THEN (l.doc->'payload'->>'duration_sec')::bigint
                              ELSE 0
                            END
                        ), 0)::bigint AS max_session_sec
                    FROM user_logs l
                    CROSS JOIN bounds b
                    WHERE l.user_id = %s
                      AND l.stream = 'tracking_events'
                      AND l.doc->>'type' = 'app_session_end'
                      AND l.ts IS NOT NULL
                      AND l.ts >= b.start_epoch
                      AND l.ts < b.end_epoch
                    GROUP BY day
                )
                SELECT
                    to_char(c.day, 'YYYY-MM-DD') AS day,
                    COALESCE(u.foreground_sec, 0)::bigint AS foreground_sec,
                    COALESCE(u.sessions, 0)::int AS sessions,
                    COALESCE(u.max_session_sec, 0)::bigint AS max_session_sec
                FROM calendar c
                LEFT JOIN usage u USING (day)
                ORDER BY c.day
                """,
                (tz, day_limit, tz, tz, day_limit, tz, str(user_id or "")),
            ).fetchall()
        return [
            {
                "day": str(row[0]),
                "foreground_sec": int(row[1] or 0),
                "sessions": int(row[2] or 0),
                "max_session_sec": int(row[3] or 0),
            }
            for row in rows
        ]
    except Exception as e:
        log.error(
            "[db] admin_data_track_user_daily_usage failed user_id=%s days=%s: %s",
            user_id,
            day_limit,
            e,
        )
        return empty


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
    day_limit = max(1, min(int(days or 60), 1000))
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


_RETENTION_DAY_OFFSETS = (1, 3, 7, 14, 30)


def admin_data_track_retention_daily(
    *, tz: str = "Asia/Shanghai", since_day: str = "", granularity: str = "day",
) -> dict:
    """Classic day-N cohort retention, cohort keyed by signup day or ISO week.

    Cohort = signup Beijing day (``granularity="day"``) or Beijing ISO week
    (``"week"``, labeled by the Monday). D_N = share of the cohort active exactly
    N days after EACH member's own signup day (activity = a user chat message or
    a tracking event — identical to the DAU definition). Only signups on/after
    ``since_day`` (the freeze boundary) count; pre-freeze days drift as accounts
    delete and are excluded entirely per the product decision.

    A cell is ``None`` ("—") until the whole cohort has had N full days to return
    (the cohort's LATEST signup + N <= today), so a still-maturing cohort is
    never shown as a deflated 0%.

    Returns {offsets, granularity, cohorts:[{cohort, size, cells:{N: pct|None}}],
    since_day}.
    """
    offsets = list(_RETENTION_DAY_OFFSETS)
    floor_day = since_day or "1970-01-01"
    gran = "week" if str(granularity).lower().startswith("w") else "day"
    signup_expr = "(timezone(%s, created_at::timestamptz))::date"
    cohort_expr = (
        "(date_trunc('week', timezone(%s, created_at::timestamptz)))::date"
        if gran == "week"
        else "(timezone(%s, created_at::timestamptz))::date"
    )
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    try:
        with get_pool().connection() as conn:
            size_rows = conn.execute(
                f"""
                SELECT to_char({cohort_expr}, 'YYYY-MM-DD') AS cohort,
                       COUNT(*)::int AS size,
                       to_char(MAX({signup_expr}), 'YYYY-MM-DD') AS mature_ref
                FROM users
                WHERE {_CREATED_AT_ISO}
                  AND {signup_expr} >= %s::date
                GROUP BY 1
                """,
                (tz, tz, tz, floor_day),
            ).fetchall()
            cell_rows = conn.execute(
                f"""
                WITH reg AS (
                    SELECT user_id,
                           {signup_expr} AS signup_day,
                           {cohort_expr} AS cohort_key
                    FROM users
                    WHERE {_CREATED_AT_ISO}
                      AND {signup_expr} >= %s::date
                ),
                act AS (
                    SELECT DISTINCT r.user_id, r.signup_day, r.cohort_key,
                           (timezone(%s, to_timestamp(a.ts)))::date AS act_day
                    FROM reg r
                    JOIN (
                        -- "使用 DAU": genuinely opened the app (an app_session_end
                        -- foreground session), NOT the broad chat∪tracking DAU that
                        -- also counts proactive/background telemetry.
                        SELECT user_id, ts FROM user_logs
                          WHERE stream = 'tracking_events'
                            AND doc->>'type' = 'app_session_end' AND ts IS NOT NULL
                    ) a ON a.user_id = r.user_id
                )
                SELECT to_char(cohort_key, 'YYYY-MM-DD') AS cohort,
                       (act_day - signup_day)::int AS day_offset,
                       COUNT(DISTINCT user_id)::int AS active
                FROM act
                WHERE (act_day - signup_day) = ANY(%s)
                GROUP BY cohort_key, day_offset
                """,
                (tz, tz, tz, floor_day, tz, offsets),
            ).fetchall()
        sizes = {r[0]: (int(r[1] or 0), r[2]) for r in size_rows}
        active = {(r[0], int(r[1])): int(r[2] or 0) for r in cell_rows}
        cohorts = []
        for cohort in sorted(sizes, reverse=True):
            size, mature_ref = sizes[cohort]
            ref_date = date.fromisoformat(mature_ref or cohort)
            cells: dict[int, float | None] = {}
            for n in offsets:
                if ref_date + timedelta(days=n) > today:
                    cells[n] = None
                elif size:
                    cells[n] = round(100.0 * active.get((cohort, n), 0) / size, 1)
                else:
                    cells[n] = 0.0
            cohorts.append({"cohort": cohort, "size": size, "cells": cells})
        return {"offsets": offsets, "granularity": gran,
                "cohorts": cohorts, "since_day": since_day}
    except Exception as e:
        log.error("[db] admin_data_track_retention_daily failed: %s", e)
        return {"offsets": offsets, "granularity": gran,
                "cohorts": [], "since_day": since_day}


def admin_data_track_growth_accounting(
    *, tz: str = "Asia/Shanghai", since_day: str = "",
) -> dict:
    """Daily growth accounting (post-freeze only). For each Beijing day it splits
    the active set into new / resurrected / retained, plus churned (users active
    the prior day but not today) and Quick Ratio = (new+resurrected)/churned.

    active = a user chat message or tracking event that day (DAU definition).
    new = signed up that day; resurrected = active today, existed before, but was
    not active yesterday; retained = active both days. Iterates every calendar
    day from ``since_day`` to today so a zero-activity gap correctly shows as
    churn, not a skipped comparison. The first day is a baseline (no deltas).
    Returns {rows:[{day, active, new, resurrected, retained, churned, quick_ratio}], since_day}.
    """
    floor_day = since_day or ""
    if not floor_day:
        return {"rows": [], "since_day": since_day}
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    try:
        with get_pool().connection() as conn:
            act_rows = conn.execute(
                f"""
                SELECT DISTINCT user_id,
                       (timezone(%s, to_timestamp(ts)))::date AS d
                FROM (
                    -- "使用 DAU": app_session_end foreground sessions only.
                    SELECT user_id, ts FROM user_logs
                      WHERE stream = 'tracking_events'
                        AND doc->>'type' = 'app_session_end' AND ts IS NOT NULL
                ) a
                WHERE (timezone(%s, to_timestamp(ts)))::date >= %s::date
                """,
                (tz, tz, floor_day),
            ).fetchall()
            signup_rows = conn.execute(
                f"""
                SELECT user_id, (timezone(%s, created_at::timestamptz))::date AS d
                FROM users WHERE {_CREATED_AT_ISO}
                """,
                (tz,),
            ).fetchall()
        active_by_day: dict[str, set] = {}
        for uid, d in act_rows:
            active_by_day.setdefault(str(d), set()).add(uid)
        signup = {uid: str(d) for uid, d in signup_rows}
        start = date.fromisoformat(floor_day)
        rows = []
        prev_set: set = set()
        first = True
        cursor = start
        while cursor <= today:
            key = cursor.isoformat()
            cur = active_by_day.get(key, set())
            new = {u for u in cur if signup.get(u) == key}
            if first:
                rows.append({
                    "day": key, "active": len(cur), "new": len(new),
                    "resurrected": None, "retained": None,
                    "churned": None, "quick_ratio": None,
                })
                first = False
            else:
                retained = cur & prev_set
                resurrected = {u for u in (cur - prev_set) if u not in new}
                churned = prev_set - cur
                qr = (round((len(new) + len(resurrected)) / len(churned), 2)
                      if churned else None)
                rows.append({
                    "day": key, "active": len(cur), "new": len(new),
                    "resurrected": len(resurrected), "retained": len(retained),
                    "churned": len(churned), "quick_ratio": qr,
                })
            prev_set = cur
            cursor += timedelta(days=1)
        return {"rows": rows, "since_day": since_day}
    except Exception as e:
        log.error("[db] admin_data_track_growth_accounting failed: %s", e)
        return {"rows": [], "since_day": since_day}


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
    day_limit = max(1, min(int(days or 30), 1000))
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
                        COALESCE(doc->>'status','') AS status,
                        COALESCE(doc->>'status_reason','') AS status_reason
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
                                          AND kind NOT IN {screen_kinds}))::int AS heartbeat,
                       -- ①服务端心跳闸拦下的 tick(gate reason=heartbeat_throttled)。
                       -- 闸上线前恒 0；上线后此列直接读出闸每天拦了多少。
                       (COUNT(*) FILTER (WHERE status = 'skipped'
                                          AND status_reason = 'heartbeat_throttled'))::int
                           AS heartbeat_throttled
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
                "heartbeat_throttled": r[11],
            }
            for r in rows
        ]
    except Exception as e:
        log.error("[db] admin_data_track_proactive_daily failed: %s", e)
        return []


def admin_data_track_proactive_kinds(*, since_epoch: float = 0.0, days: int = 30,
                                     tz: str = "Asia/Shanghai") -> dict[str, dict[str, int]]:
    """Per-Beijing-day raw kind counts: {day: {kind: count}}.

    与 admin_data_track_proactive_daily 同一个 kind 定义(job_kind → wake_kind →
    trigger → 'unknown')。全量分桶而不是猜枚举——现网出现过什么 kind 就报什么,
    新增唤醒源(解锁/到达/照片/scheduled…)自动出现,不需要每次改 SQL。
    2026-07 心跳暴增排查花了半天才发现分类盲区,这个函数就是那次的教训。"""
    day_limit = max(1, min(int(days or 30), 1000))
    since = float(since_epoch or 0.0)
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT to_char(timezone(%s, to_timestamp(ts)), 'YYYY-MM-DD') AS day,
                       COALESCE(NULLIF(doc->>'job_kind',''), NULLIF(doc->>'wake_kind',''),
                                NULLIF(doc->>'trigger',''), 'unknown') AS kind,
                       COUNT(*)::int AS n
                FROM user_logs
                WHERE stream = 'proactive_jobs'
                  AND ts IS NOT NULL
                  AND (%s = 0 OR ts >= %s)
                GROUP BY day, kind
                ORDER BY day DESC
                """,
                (tz, since, since),
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for day, kind, n in rows:
            if len(out) >= day_limit and day not in out:
                continue
            out.setdefault(day, {})[kind] = int(n)
        return out
    except Exception as e:
        log.error("[db] admin_data_track_proactive_kinds failed: %s", e)
        return {}


def admin_proactive_heartbeat_overspeed(*, since_epoch: float = 0.0, days: int = 7,
                                        tz: str = "Asia/Shanghai") -> dict[str, list[dict]]:
    """超速哨兵：每天心跳 job 数超过其 wake_interval 物理上限的用户。

    物理上限 = 86400 / clamp(wake_interval_sec, 900, 43200)(默认 7200 → 12/天),
    +1 容差(重启/日界的首 tick)。任何用户超上限 = 频率闸失效的直接信号
    (2026-07-22:中位 68/天 vs 默认上限 12,靠人肉挖了半天——这个哨兵让它
    自己跳出来)。返回 {day: [{user_id, heartbeats, interval_sec, cap}, ...]},
    仅含超速用户,按超速幅度降序。

    两个来源 UNION:legacy V1(user_logs stream='proactive_jobs',consumer tick
    经 gate)+ Runtime V2(agent_jobs lane='heartbeat',serve-worker 调度器
    enqueue)。V2 被 gate block 时不落 job 行,所以 V2 侧天然全是 admitted,
    无需 throttled 排除;dual 共存下用户切换 runtime 也不会脱离监控。"""
    day_limit = max(1, min(int(days or 7), 60))
    since = float(since_epoch or 0.0)
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                WITH hb_raw AS (
                    SELECT user_id,
                           to_char(timezone(%s, to_timestamp(ts)), 'YYYY-MM-DD') AS day
                    FROM user_logs
                    WHERE stream = 'proactive_jobs'
                      AND ts IS NOT NULL
                      AND (%s = 0 OR ts >= %s)
                      AND (
                        COALESCE(NULLIF(doc->>'job_kind',''), NULLIF(doc->>'wake_kind',''),
                                 NULLIF(doc->>'trigger',''), 'unknown') = 'presence'
                        OR COALESCE(NULLIF(doc->>'job_kind',''), NULLIF(doc->>'wake_kind',''),
                                    NULLIF(doc->>'trigger',''), 'unknown') LIKE 'heartbeat%%'
                      )
                      AND COALESCE(NULLIF(doc->>'job_kind',''), NULLIF(doc->>'wake_kind',''),
                                   NULLIF(doc->>'trigger',''), 'unknown')
                          NOT IN ('screen_watch','scene_change','screen_tick',
                                  'broadcast_opened','heartbeat_broadcast_on')
                      -- 哨兵只数「放行(admitted)」的心跳:①闸拦下的 throttled
                      -- skipped 是闸在正常工作,不能算——否则闸守得越好标得越红,
                      -- 与「出现即闸失效」的页面语义正好相反(codex review ④)。
                      AND NOT (COALESCE(doc->>'status','') = 'skipped'
                               AND COALESCE(doc->>'status_reason','') = 'heartbeat_throttled')

                    UNION ALL

                    -- Runtime V2 pooled worker heartbeats (blocked wakes never
                    -- enqueue, so every row here is admitted by definition).
                    SELECT user_id,
                           to_char(created_at AT TIME ZONE %s, 'YYYY-MM-DD') AS day
                    FROM agent_jobs
                    WHERE lane = 'heartbeat'
                      AND (%s = 0 OR created_at >= to_timestamp(%s))
                ),
                hb AS (
                    SELECT user_id, day, COUNT(*)::int AS heartbeats
                    FROM hb_raw
                    GROUP BY user_id, day
                ),
                intervals AS (
                    SELECT user_id,
                           GREATEST(900, LEAST(43200, COALESCE(
                               NULLIF(doc->>'wake_interval_sec','')::int, 7200
                           ))) AS interval_sec
                    FROM user_blobs
                    WHERE kind = 'proactive_settings'
                      AND COALESCE(doc->>'wake_interval_sec','') ~ '^[0-9]{1,6}$'
                )
                SELECT hb.day, hb.user_id, hb.heartbeats,
                       COALESCE(i.interval_sec, 7200) AS interval_sec,
                       (86400 / COALESCE(i.interval_sec, 7200))::int AS cap
                FROM hb
                LEFT JOIN intervals i ON i.user_id = hb.user_id
                WHERE hb.heartbeats > (86400 / COALESCE(i.interval_sec, 7200)) + 1
                ORDER BY hb.day DESC, (hb.heartbeats::float / GREATEST(1, 86400 / COALESCE(i.interval_sec, 7200))) DESC
                """,
                (tz, since, since, tz, since, since),
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for day, user_id, heartbeats, interval_sec, cap in rows:
            if len(out) >= day_limit and day not in out:
                continue
            out.setdefault(day, []).append({
                "user_id": user_id, "heartbeats": int(heartbeats),
                "interval_sec": int(interval_sec), "cap": int(cap),
            })
        return out
    except Exception as e:
        log.error("[db] admin_proactive_heartbeat_overspeed failed: %s", e)
        return {}


def admin_v2_heartbeat_daily(*, since_epoch: float = 0.0, days: int = 30,
                             tz: str = "Asia/Shanghai") -> dict[str, dict]:
    """Runtime V2 心跳日聚合:{day: {jobs, completed, failed, expired}}。

    V2 唤醒走 agent_jobs(lane='heartbeat'),从不写 legacy proactive_jobs 流
    ——④日报最初只读旧流,V2 心跳是观测盲区(2026-07-24 Seven 定补)。口径
    对齐 jobs_store.wake_success_stats:completed=成功(含醒了决定不发声),
    failed/expired=失败侧;这里按天分桶供日报并列展示。blocked 的 wake 不落
    行,所以 jobs=当天 admitted 总量。"""
    day_limit = max(1, min(int(days or 30), 1000))
    since = float(since_epoch or 0.0)
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                """
                SELECT to_char(created_at AT TIME ZONE %s, 'YYYY-MM-DD') AS day,
                       COUNT(*)::int AS jobs,
                       (COUNT(*) FILTER (WHERE status = 'completed'))::int AS completed,
                       (COUNT(*) FILTER (WHERE status = 'failed'))::int AS failed,
                       (COUNT(*) FILTER (WHERE status = 'expired'))::int AS expired
                FROM agent_jobs
                WHERE lane = 'heartbeat'
                  AND (%s = 0 OR created_at >= to_timestamp(%s))
                GROUP BY day
                ORDER BY day DESC
                LIMIT %s
                """,
                (tz, since, since, day_limit),
            ).fetchall()
        return {
            r[0]: {"jobs": int(r[1]), "completed": int(r[2]),
                   "failed": int(r[3]), "expired": int(r[4])}
            for r in rows
        }
    except Exception as e:
        log.error("[db] admin_v2_heartbeat_daily failed: %s", e)
        return {}


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


_VALID_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VALID_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+/-]{0,63}$")


def admin_events_overview(*, day: str = "", tz: str = "Asia/Shanghai") -> dict:
    """Fleet-wide event-health aggregates for the `view=events` board, split by
    route (VPS/resident vs API/model_api). Each sub-query is independently
    guarded so one failure degrades to an empty slice, not the whole board.

    ``day`` scopes every aggregate to ONE calendar day in ``tz`` (default Beijing);
    empty means all time. Until 2026-08-04 this function took no arguments and had
    no time filter at all, so the board silently reported since-the-beginning-of-time
    totals while the page URL carried `day=`/`hours=` params that did nothing. That
    makes an outage invisible: 200 failures today against 10,000 historical successes
    still renders 98% green, and the denominator only grows, so the metric gets
    number by the day. Day-scoped is what an operator actually reads it for —
    "was today healthy".

    Returns {proactive:[...], capture:[...], genesis:[...], reply:[...]} where each
    row carries route + the event dimension + counts + median duration (seconds)."""
    out = {"proactive": [], "capture": [], "genesis": [], "reply": []}
    zone = str(tz or "Asia/Shanghai")
    if not _VALID_TZ_NAME.match(zone):
        zone = "Asia/Shanghai"
    want_day = str(day or "").strip()
    if want_day and not _VALID_DAY.match(want_day):
        # An unparseable day would otherwise silently widen back to all-time —
        # the exact failure this function was changed to remove.
        raise ValueError(f"admin_events_overview: bad day {want_day!r}, want YYYY-MM-DD")

    def _day_filter(ts_expr: str, *, epoch: bool = True) -> str:
        """SQL predicate scoping one row's time column to ``want_day`` in ``tz``.

        ``epoch=True`` for the numeric ``ts`` columns (user_logs / chat_messages),
        False for a real timestamptz (genesis_import_jobs.created_at). Same
        bucketing idiom as admin_data_track_dau, so this board and the DAU chart
        can never disagree about which day a row belongs to.

        ``want_day``/``zone`` are inlined rather than bound because these SQL
        strings are assembled by f-string in the callers below; both are stripped
        of quotes and every caller passes a value this function itself validated
        (``_valid_day``), so no caller-controlled text reaches SQL."""
        if not want_day:
            return ""
        stamp = f"to_timestamp({ts_expr})" if epoch else f"({ts_expr})"
        return (f" AND to_char(timezone('{zone}', {stamp}), "
                f"'YYYY-MM-DD') = '{want_day}'")

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
            {_day_filter('l.ts')}
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
            {_day_filter('l.ts')}
          UNION ALL
          SELECT l.user_id, COALESCE(l.doc->>'status','') AS status, {_JOB_DUR_SEC.replace('doc','l.doc')} AS dur
          FROM user_logs l
          WHERE l.stream = 'proactive_jobs'
            AND COALESCE(l.doc->>'job_kind','') IN ('memory_capture','memory_dream','memory_migrate')
            {_day_filter('l.ts')}
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
        WHERE TRUE {_day_filter('g.created_at', epoch=False)}
        GROUP BY route, distill
    """)
    out["genesis"] = [
        {"route": r[0], "distill": r[1], "total": r[2], "success": r[3], "failed": r[4]}
        for r in rows
    ]

    # 4) 回复消息: 真回复率 + 兜底率 + 回复延迟(中位)。real_replies 排除
    #    agent_initiated_proactive(主动消息不是"对用户的回复")。latency = 每条真回复
    #    与其前一条用户消息的时间差(窗口配对)。
    #    日期过滤只能加在 paired 之外：窗口函数要回看"这条回复之前的那条用户消息"，
    #    在 CTE 里就按天切会让每天 0 点后的第一条回复找不到它的问句(last_user_ts 为
    #    NULL)，于是被算成"没有真回复"——把跨零点的正常对话统计成故障。
    rows = _run("reply", f"""
        {_EVENTS_ROUTES_CTE}, paired AS (
          SELECT c.user_id, c.ts, c.doc->>'role' AS role, COALESCE(c.doc->>'source','') AS src,
            MAX(CASE WHEN c.doc->>'role' IN ('user','human') AND COALESCE(c.doc->>'source','') NOT IN ('verify_ping','resident_maintenance') THEN c.ts END)
              OVER (PARTITION BY c.user_id ORDER BY c.ts ROWS UNBOUNDED PRECEDING) AS last_user_ts
          FROM chat_messages c
        )
        SELECT COALESCE(r.route,'resident') AS route,
               (COUNT(*) FILTER (WHERE p.role IN ('user','human') AND p.src NOT IN ('verify_ping','resident_maintenance')))::int AS user_msgs,
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
        WHERE TRUE {_day_filter('p.ts')}
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
                MAX(CASE WHEN c.doc->>'role' IN ('user','human') AND COALESCE(c.doc->>'source','') NOT IN ('verify_ping','resident_maintenance') THEN c.ts END)
                  OVER (PARTITION BY c.user_id ORDER BY c.ts ROWS UNBOUNDED PRECEDING) AS last_user_ts
              FROM chat_messages c
            )
            SELECT p.user_id, COALESCE(r.route,'resident') AS route,
                   (COUNT(*) FILTER (WHERE p.role IN ('user','human') AND p.src NOT IN ('verify_ping','resident_maintenance')))::int AS user_msgs,
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
            registered_at_sql = _admin_utc_text_timestamp_sql(
                "created_at",
                supports_input_validation=conn.info.server_version >= 160000,
            )
            rows = conn.execute(f"""
                {_EVENTS_ROUTES_CTE},
                u AS (SELECT user_id,
                        EXTRACT(EPOCH FROM ({registered_at_sql})) AS t0
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


_BLOB_REVISION_KEY = "_rds_revision"
_REVISIONED_BLOB_KINDS = frozenset({"model_api_runtime", "v1_flow_trace"})


def _blob_revision(doc) -> int:
    """Return a bounded internal mirror revision from a JSON document."""
    if not isinstance(doc, dict):
        return 0
    raw = str(doc.get(_BLOB_REVISION_KEY, ""))
    if not raw.isdigit() or len(raw) > 18:
        return 0
    return int(raw)


def _next_blob_revision(doc) -> int:
    revision = _blob_revision(doc)
    if revision >= 999_999_999_999_999_999:
        raise RuntimeError("blob mirror revision exhausted")
    return revision + 1


def get_blob_strict(user_id: str, kind: str):
    """Return a blob or ``None`` for a genuine miss; propagate DB failures."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM user_blobs WHERE user_id = %s AND kind = %s",
            (user_id, kind),
        ).fetchone()
    return row[0] if row is not None else None


def get_blob(user_id: str, kind: str):
    """Legacy best-effort wrapper around :func:`get_blob_strict`."""
    try:
        return get_blob_strict(user_id, kind)
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


def set_blob_strict(user_id: str, kind: str, doc) -> None:
    """Persist a blob or raise."""
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if kind == "proactive_settings":
                    _lock_chat_user_fence_on_cursor(cur, user_id)
                    _lock_capture_consent_on_cursor(cur, user_id)
                cur.execute(
                    "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc",
                    (user_id, kind, Jsonb(doc)),
                )


def set_blob_strict_mirrored(user_id: str, kind: str, doc) -> None:
    """Strict singleton write with the same mirror policy as ``set_blob``."""
    set_blob_strict(user_id, kind, doc)
    _mirror_persisted_blob(user_id, kind, doc)


def set_onboarding_route_strict(user_id: str, doc: dict) -> str | None:
    """Persist the route selector and reconcile the V1 model route atomically.

    ``onboarding_route`` is the ownership selector.  A non-model-api route must
    not leave an active V1 route behind; switching back to ``model_api`` restores
    the most recently updated tested-ok route when one exists.  The blob and
    route-row updates share one transaction so readers cannot observe a committed
    selector whose auxiliary kill switch still has the previous value.

    Returns the active route id after the transaction, or ``None`` when the
    selected access mode is not model_api / no tested-ok route can be restored.
    Raises on primary persistence failure.
    """
    route = str((doc or {}).get("route") or "").strip().lower()
    active_route_id: str | None = None
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('onboarding-route:' || %s, 0))",
                    (str(user_id),),
                )
                cur.execute(
                    "INSERT INTO user_blobs (user_id, kind, doc) "
                    "VALUES (%s, 'onboarding_route', %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc",
                    (user_id, Jsonb(doc)),
                )
                if route == "model_api":
                    # Deactivate first: the partial unique index is not deferrable,
                    # so a one-statement boolean flip can transiently collide.
                    cur.execute(
                        "UPDATE model_api_routes SET is_active = FALSE, updated_at = now() "
                        "WHERE user_id = %s AND is_active",
                        (user_id,),
                    )
                    row = cur.execute(
                        "UPDATE model_api_routes SET is_active = TRUE, updated_at = now() "
                        "WHERE id = ("
                        "  SELECT id FROM model_api_routes "
                        "  WHERE user_id = %s AND test_status = 'ok' "
                        "  ORDER BY updated_at DESC, id LIMIT 1"
                        ") RETURNING id::text",
                        (user_id,),
                    ).fetchone()
                    active_route_id = str(row[0]) if row else None
                else:
                    cur.execute(
                        "UPDATE model_api_routes SET is_active = FALSE, updated_at = now() "
                        "WHERE user_id = %s AND is_active",
                        (user_id,),
                    )
    _mirror_persisted_blob(user_id, "onboarding_route", doc)
    return active_route_id


def patch_proactive_settings_strict(
    user_id: str,
    patch: dict,
    *,
    seed_doc: dict | None = None,
) -> dict:
    """Atomically merge one proactive-settings patch or raise.

    The read happens *after* acquiring the Capture-consent advisory lock. This
    is deliberately not implemented as ``get_blob`` followed by ``set_blob``:
    two backend processes do not share ``UserStore.proactive_lock``, and an
    unrelated stale full-document write must never restore an earlier
    ``capture_enabled`` value. Permission-state updates are nested patches too,
    so concurrent device-permission reports preserve each other's keys.
    """
    if not isinstance(patch, dict):
        raise ValueError("proactive settings patch must be an object")
    seed = dict(seed_doc) if isinstance(seed_doc, dict) else {}
    persisted: dict
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(cur, str(user_id))
                _lock_capture_consent_on_cursor(cur, str(user_id))
                cur.execute(
                    "SELECT doc FROM user_blobs WHERE user_id=%s "
                    "AND kind='proactive_settings' FOR UPDATE",
                    (str(user_id),),
                )
                row = cur.fetchone()
                current = dict(row[0] or {}) if row is not None else seed
                update = dict(patch)
                permission_patch = update.pop("permission_states", None)
                current.update(update)
                if isinstance(permission_patch, dict):
                    permission_states = dict(
                        current.get("permission_states") or {}
                    )
                    permission_states.update(permission_patch)
                    current["permission_states"] = permission_states
                if patch.get("capture_enabled") is False:
                    # Prepared batches are encrypted but still derived from
                    # conversation content. Opt-out is their immediate erasure
                    # boundary; the shared consent lock prevents a provider
                    # completion from creating another journal behind us.
                    cur.execute(
                        "DELETE FROM v2_capture_batches WHERE user_id=%s",
                        (str(user_id),),
                    )
                cur.execute(
                    "INSERT INTO user_blobs (user_id,kind,doc) "
                    "VALUES (%s,'proactive_settings',%s) "
                    "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc "
                    "RETURNING doc",
                    (str(user_id), Jsonb(current)),
                )
                persisted = dict(cur.fetchone()[0] or {})
    _mirror_proactive_settings_current(str(user_id))
    return persisted


def set_blob_if_unchanged(
    user_id: str,
    kind: str,
    expected_doc,
    new_doc,
    *,
    insert_if_missing: bool = False,
) -> bool:
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

    A missing row normally returns False: the WHERE matches nothing, so callers
    cannot accidentally resurrect deleted data. ``insert_if_missing=True`` is
    reserved for state machines whose canonical initial value is an empty
    object. In that mode, an empty expected object may atomically create the row;
    concurrent creators serialize through the unique key and only one wins.
    """
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "UPDATE user_blobs SET doc = %s "
                    "WHERE user_id = %s AND kind = %s AND doc = %s "
                    "RETURNING user_id",
                    (Jsonb(new_doc), user_id, kind, Jsonb(expected_doc)),
                ).fetchone()
                if row is None and insert_if_missing and expected_doc == {}:
                    row = conn.execute(
                        "INSERT INTO user_blobs (user_id, kind, doc) "
                        "VALUES (%s, %s, %s) "
                        "ON CONFLICT (user_id, kind) DO NOTHING "
                        "RETURNING user_id",
                        (user_id, kind, Jsonb(new_doc)),
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
        set_blob_strict(user_id, kind, doc)
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
    # （chat/consumer._record_consumer_event → _mutate_consumer_state 的 PostgreSQL CAS），N 个
    # 常驻 consumer 长轮询就是每轮 N 次写。把它镜像出去会打满 max_size=4 的 TEE 池
    # （direct-TLS 过网关），于是每个主写都要先在池上等满 pool_timeout 才 fail-open
    # ——2026-07-13 test 实测：13 分钟 18 次 pool timeout，poll/写端点被拖到秒级
    # （perception/report 23.7s、track/event 13.4s）。而它只是 runner 侧运维状态
    # （上次 poll 时间 / consumer id），不是用户数据，TEE 影子没有任何理由持有它。
    #
    # 两处辖区必须同步：reconciler._SCOPE_WHERE["user_blobs"] 同样排除这两个 kind，
    # 否则 reconciler 会把镜像端故意不写的行又 copy 回 TEE、并在两侧计数里要求它存在。
    # 其余 kind（如 model_api provider-key 信封）有意原样镜像（凭据保持加密）。
    if kind == "proactive_settings":
        _mirror_proactive_settings_current(user_id)
    elif kind not in ("identity", "consumer_state"):
        mirror.execute(sql, (user_id, kind, Jsonb(doc)))


def _mirror_persisted_blob(user_id: str, kind: str, doc) -> None:
    """Mirror an already-committed blob document exactly.

    Strict blob helpers cannot reuse :func:`set_blob`: doing so would perform a
    second primary write and reopen the read/modify/write races those helpers
    exist to close.  Mirror only the post-commit document, while preserving the
    same ownership exclusions as ``set_blob``/the reconciler.
    """
    if kind in ("identity", "consumer_state"):
        return
    from tee_shadow import mirror

    revision = _blob_revision(doc)
    if revision:
        # Primary row locks make revisions strictly increasing, but mirror
        # writes happen after commit and can finish in either order. The TEE
        # compare-and-set keeps a delayed older full document from restoring a
        # stale cursor or sibling field after a newer document already landed.
        sql = (
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc "
            "WHERE (CASE "
            "  WHEN COALESCE(user_blobs.doc ->> %s, '') ~ '^[0-9]{1,18}$' "
            "  THEN (user_blobs.doc ->> %s)::bigint ELSE 0 END) <= %s::bigint"
        )
        mirror.execute(
            sql,
            (
                user_id,
                kind,
                Jsonb(doc),
                _BLOB_REVISION_KEY,
                _BLOB_REVISION_KEY,
                revision,
            ),
        )
        return
    sql = (
        "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc"
    )
    mirror.execute(sql, (user_id, kind, Jsonb(doc)))


def _mirror_proactive_settings_current(user_id: str) -> None:
    """Mirror the authoritative settings row under the consent fence.

    A postcommit mirror of an older full document could otherwise land after a
    newer opt-out and restore ``capture_enabled=true`` in the TEE shadow. The
    primary is re-read only after acquiring the same advisory lock used by all
    settings mutations and Capture disclosure/commit boundaries.
    """
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    _lock_chat_user_fence_on_cursor(cur, str(user_id))
                    _lock_capture_consent_on_cursor(cur, str(user_id))
                    cur.execute(
                        "SELECT doc FROM user_blobs WHERE user_id=%s "
                        "AND kind='proactive_settings'",
                        (str(user_id),),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        _mirror_persisted_blob(
                            str(user_id),
                            "proactive_settings",
                            dict(row[0] or {}),
                        )
    except Exception as exc:  # noqa: BLE001 — primary setting is authoritative
        log.warning(
            "[db] proactive_settings mirror deferred user=%s code=%s",
            user_id,
            type(exc).__name__.lower(),
        )


def append_blob_events_strict(
    user_id: str,
    kind: str,
    new_events: list[dict],
    *,
    cutoff_ts: float,
    max_events: int,
):
    """Atomically append a bounded event batch to one blob ring.

    The row lock covers the read, merge, and full-document update. That makes
    the operation safe across threads, workers, and processes, unlike the
    legacy ``get_blob`` + ``set_blob`` pair. A monotonic document revision also
    orders the post-commit TEE mirrors.
    """
    incoming = [dict(event) for event in new_events if isinstance(event, dict)]
    if not incoming:
        return get_blob_strict(user_id, kind)
    cap = max(1, int(max_events))
    cutoff = float(cutoff_ts)
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO user_blobs (user_id,kind,doc) "
                "VALUES (%s,%s,'{}'::jsonb) "
                "ON CONFLICT (user_id,kind) DO NOTHING",
                (user_id, kind),
            )
            row = conn.execute(
                "SELECT doc FROM user_blobs "
                "WHERE user_id=%s AND kind=%s FOR UPDATE",
                (user_id, kind),
            ).fetchone()
            current = (
                row["doc"] if isinstance(row, dict) else row[0]
            ) if row is not None else {}
            events = (
                list(current.get("events"))
                if isinstance(current, dict)
                and isinstance(current.get("events"), list)
                else []
            )
            events.extend(incoming)
            retained = [
                event
                for event in events
                if isinstance(event, dict)
                and float(event.get("ts") or 0) >= cutoff
            ][-cap:]
            persisted = {
                "v": 1,
                "events": retained,
                _BLOB_REVISION_KEY: _next_blob_revision(current),
            }
            conn.execute(
                "UPDATE user_blobs SET doc=%s WHERE user_id=%s AND kind=%s",
                (Jsonb(persisted), user_id, kind),
            )
    _mirror_persisted_blob(user_id, kind, persisted)
    return persisted


def patch_blob_strict(
    user_id: str,
    kind: str,
    patch: dict,
    *,
    remove_keys: tuple[str, ...] | list[str] = (),
    runtime_state_target: str | None = None,
    require_active_hosted_route: bool = False,
):
    """Atomically merge top-level blob keys and optionally remove keys.

    Unlike a Python read/modify/full-write, the merge happens in one UPSERT, so
    independent control-plane writers cannot resurrect stale sibling fields.
    Returns the persisted document and propagates DB failures.

    ``runtime_state_target`` is reserved for the hosted-runtime control-plane
    flip.  When supplied, the blob patch and the mandatory
    resident/v2 -> draining -> target generation changes commit in the SAME
    PostgreSQL transaction. Readers therefore see either the old routing flag
    and generation or the new pair, never a split-brain combination. The
    runtime-state row is locked before the blob, matching the effect applier's
    lock order so an in-flight generation-fenced dispatch drains before cutover.
    ``require_active_hosted_route`` revalidates V2 eligibility inside that same
    transaction. It prevents a setup/startup transition that read a route just
    before concurrent credential deletion from resurrecting V2 afterward.
    """
    clean_patch = dict(patch or {})
    revisioned = kind in _REVISIONED_BLOB_KINDS
    if revisioned:
        clean_patch.pop(_BLOB_REVISION_KEY, None)
    keys = [
        str(key)
        for key in remove_keys
        if str(key) and (not revisioned or str(key) != _BLOB_REVISION_KEY)
    ]
    resident_bridge_ids: list[str] = []
    resident_bridge_fields = {
        "reply_status": "replied",
        "replied_by": "hosted_runtime_v2_cutover",
    }
    if runtime_state_target not in (None, "resident", "v2"):
        raise ValueError("runtime_state_target must be resident or v2")
    if require_active_hosted_route and runtime_state_target != "v2":
        raise ValueError("active hosted route may only be required for a v2 target")
    with get_pool().connection() as conn:
        with conn.transaction():
            if runtime_state_target is not None:
                conn.execute(
                    "INSERT INTO v2_runtime_state (user_id) "
                    "SELECT %s WHERE EXISTS "
                    "(SELECT 1 FROM users WHERE user_id=%s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id),
                )
                state_row = conn.execute(
                    "SELECT hosted_runtime_state FROM v2_runtime_state "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                ).fetchone()
                if state_row is None:
                    raise ValueError("cannot cut over unknown user")
                current_state = str(state_row[0])
                if current_state not in {"resident", "draining", "v2"}:
                    raise RuntimeError(
                        f"invalid hosted runtime state: {current_state!r}")
                if require_active_hosted_route:
                    eligible = conn.execute(
                        "SELECT 1 FROM model_api_routes r "
                        "JOIN model_api_credentials c ON c.id=r.credential_id "
                        "WHERE r.user_id=%s AND r.is_active "
                        "AND r.test_status='ok' "
                        "AND LOWER(c.provider)=ANY(%s) "
                        "LIMIT 1 FOR SHARE OF r, c",
                        (user_id, list(HOSTED_RUNTIME_SUPPORTED_PROVIDERS)),
                    ).fetchone()
                    if eligible is None:
                        raise ValueError(
                            "cannot enable hosted runtime v2 without an active tested route"
                        )
                expected_mode = (
                    "db_action_v2"
                    if runtime_state_target == "v2"
                    else "resident_cli"
                )
                requested_mode = str(
                    clean_patch.get("hosted_runtime_mode") or "")
                if requested_mode != expected_mode:
                    raise ValueError(
                        "hosted_runtime_mode patch does not match runtime_state_target")
                # Materialize then lock the blob row after the runtime row. This
                # both preserves the global runtime->blob lock order and lets us
                # detect/repair pre-existing split-brain state atomically.
                conn.execute(
                    "INSERT INTO user_blobs (user_id, kind, doc) "
                    "VALUES (%s,%s,'{}'::jsonb) "
                    "ON CONFLICT (user_id,kind) DO NOTHING",
                    (user_id, kind),
                )
                blob_row = conn.execute(
                    "SELECT doc FROM user_blobs "
                    "WHERE user_id=%s AND kind=%s FOR UPDATE",
                    (user_id, kind),
                ).fetchone()
                existing_doc = blob_row[0] if blob_row else {}
                existing_mode = str(
                    (existing_doc or {}).get("hosted_runtime_mode") or "")
                if existing_mode not in {"resident_cli", "db_action_v2"}:
                    existing_mode = "resident_cli"
                routing_changed = (
                    current_state != runtime_state_target
                    or existing_mode != expected_mode
                )
                if routing_changed:
                    if current_state != "draining":
                        conn.execute(
                            "UPDATE v2_runtime_state SET "
                            "hosted_runtime_state='draining', "
                            "runtime_generation=runtime_generation+1, updated_at=now() "
                            "WHERE user_id=%s",
                            (user_id,),
                        )
                    conn.execute(
                        "UPDATE v2_runtime_state SET hosted_runtime_state=%s, "
                        "runtime_generation=runtime_generation+1, updated_at=now() "
                        "WHERE user_id=%s AND hosted_runtime_state='draining'",
                        (runtime_state_target, user_id),
                    )
            if revisioned:
                insert_doc = {
                    **clean_patch,
                    _BLOB_REVISION_KEY: 1,
                }
                row = conn.execute(
                    "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET doc = "
                    "((user_blobs.doc - %s::text[]) || EXCLUDED.doc) || "
                    "jsonb_build_object(%s::text, "
                    "  (CASE WHEN COALESCE(user_blobs.doc ->> %s, '') "
                    "              ~ '^[0-9]{1,18}$' "
                    "        THEN (user_blobs.doc ->> %s)::bigint ELSE 0 END) + 1"
                    ") RETURNING doc",
                    (
                        user_id,
                        kind,
                        Jsonb(insert_doc),
                        keys,
                        _BLOB_REVISION_KEY,
                        _BLOB_REVISION_KEY,
                        _BLOB_REVISION_KEY,
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET "
                    "doc = (user_blobs.doc - %s::text[]) || EXCLUDED.doc "
                    "RETURNING doc",
                    (user_id, kind, Jsonb(clean_patch), keys),
                ).fetchone()
            if runtime_state_target == "v2":
                # Bidirectional resident -> V2 cutover bridge. Resident replies
                # mark their parent user row instead of advancing V2's seq
                # cursor. Advancing through the newest replied user row also
                # consumes any older unanswered row that resident redelivery
                # already considers superseded. Do this in the SAME transaction
                # as the routing flag/generation so V2 can never observe the new
                # mode with a stale cursor and re-answer resident history.
                bridge = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM chat_messages "
                    "WHERE user_id=%s AND doc->>'role' IN ('user','human') "
                    "  AND COALESCE(doc->>'source','') <> 'verify_ping' "
                    "  AND (doc->>'reply_status'='replied' "
                    "       OR COALESCE(doc->>'reply_message_id','') <> '')",
                    (user_id,),
                ).fetchone()
                bridge_seq = int(bridge[0] or 0) if bridge else 0
                with conn.cursor() as cur:
                    persisted = _advance_blob_int_on_cursor(
                        cur,
                        user_id,
                        kind,
                        "v2_reply_cursor_seq",
                        bridge_seq,
                    )
                row = (persisted,)
            elif runtime_state_target == "resident":
                persisted_doc = row[0] if row else {}
                raw_cursor = str(
                    (persisted_doc or {}).get("v2_reply_cursor_seq", 0)
                )
                if not raw_cursor.isdigit():
                    raise RuntimeError("invalid persisted V2 reply cursor")
                v2_cursor_seq = int(raw_cursor)
                if v2_cursor_seq > 0:
                    updated = conn.execute(
                        "UPDATE chat_messages SET doc=doc || %s "
                        "WHERE user_id=%s AND seq<=%s "
                        "  AND doc->>'role' IN ('user','human') "
                        "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                        "  AND COALESCE(doc->>'reply_message_id','')='' "
                        "RETURNING msg_id",
                        (
                            Jsonb(resident_bridge_fields),
                            user_id,
                            v2_cursor_seq,
                        ),
                    ).fetchall()
                    resident_bridge_ids.extend(str(value[0]) for value in updated)
    if resident_bridge_ids:
        from tee_shadow import mirror
        mirror.execute(
            "UPDATE chat_messages SET doc=doc || %s "
            "WHERE user_id=%s AND msg_id=ANY(%s)",
            (Jsonb(resident_bridge_fields), user_id, resident_bridge_ids),
        )
    persisted_doc = row[0]
    _mirror_persisted_blob(user_id, kind, persisted_doc)
    return persisted_doc


def advance_blob_int_strict(user_id: str, kind: str, key: str, new_value: int):
    """Atomically advance one non-negative integer field without regression.

    Independent/replayed outbox effects may arrive out of order.  A regular
    JSON merge would let an older cursor overwrite a newer one; this UPSERT
    persists ``max(existing, new_value)`` in PostgreSQL while preserving every
    sibling key. Invalid existing values raise instead of being silently reset,
    keeping correctness cursors fail-closed.
    """
    field = str(key)
    if not field:
        raise ValueError("key must be non-empty")
    value = int(new_value)
    if value < 0:
        raise ValueError("new_value must be >= 0")
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            row = _advance_blob_int_on_cursor(
                cur, user_id, kind, field, value)
    _mirror_persisted_blob(user_id, kind, row)
    return row


def _advance_blob_int_on_cursor(cur, user_id: str, kind: str, key: str, new_value: int):
    """Cursor-level form used when a correctness cursor shares a transaction
    with another durable write (notably final V2 reply persistence)."""
    field = str(key)
    value = int(new_value)
    if kind in _REVISIONED_BLOB_KINDS:
        cur.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET doc = "
            "user_blobs.doc || jsonb_build_object("
            "  %s::text, GREATEST("
            "    COALESCE(NULLIF(user_blobs.doc ->> %s::text, ''), '0')::bigint, "
            "    %s::bigint"
            "  ), "
            "  %s::text, (CASE "
            "    WHEN COALESCE(user_blobs.doc ->> %s, '') ~ '^[0-9]{1,18}$' "
            "    THEN (user_blobs.doc ->> %s)::bigint ELSE 0 END) + 1"
            ") RETURNING doc",
            (
                user_id,
                kind,
                Jsonb({field: value, _BLOB_REVISION_KEY: 1}),
                field,
                field,
                value,
                _BLOB_REVISION_KEY,
                _BLOB_REVISION_KEY,
                _BLOB_REVISION_KEY,
            ),
        )
    else:
        cur.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET doc = "
            "user_blobs.doc || jsonb_build_object("
            "  %s::text, GREATEST("
            "    COALESCE(NULLIF(user_blobs.doc ->> %s::text, ''), '0')::bigint, "
            "    %s::bigint"
            "  )"
            ") RETURNING doc",
            (user_id, kind, Jsonb({field: value}), field, field, value),
        )
    fetched = cur.fetchone()
    if fetched is None:
        raise RuntimeError("integer blob advance returned no row")
    return fetched["doc"] if isinstance(fetched, dict) else fetched[0]


def patch_blob(
    user_id: str,
    kind: str,
    patch: dict,
    *,
    remove_keys: tuple[str, ...] | list[str] = (),
):
    """Legacy best-effort wrapper around :func:`patch_blob_strict`."""
    try:
        return patch_blob_strict(user_id, kind, patch, remove_keys=remove_keys)
    except Exception as e:
        log.error("[db] patch_blob(%s,%s) failed: %s", user_id, kind, e)
        return None


HOSTED_RUNTIME_SUPPORTED_PROVIDERS = (
    "anthropic",
    "claude",
    "deepseek",
    "openai",
    "gemini",
    "openrouter",
    "openai_compatible",
)


_hosted_runtime_config_lock_users: ContextVar[frozenset[str]] = ContextVar(
    "hosted_runtime_config_lock_users",
    default=frozenset(),
)
_hosted_runtime_config_connection_slots = threading.BoundedSemaphore(1)
_HOSTED_RUNTIME_CONFIG_LOCK_WAIT_SEC = 5.0


class HostedRuntimeConfigBusyError(RuntimeError):
    """The bounded config-mutation lock could not be acquired in time."""


@contextmanager
def hosted_runtime_config_mutation_lock(user_id: str):
    """Serialize one user's config mutation and runtime-generation rotation.

    The lock is session-level and pool-external because provider validation can
    span a network call. A one-slot process-local semaphore is acquired *before*
    opening that connection, so a burst of settings requests waits in Python
    rather than consuming one PostgreSQL session per waiter. Both the local and
    cross-process waits are deadline-bounded. ContextVar reentrancy lets route
    creation call route activation and lets setup call the runtime transition
    without trying to acquire the same advisory lock on a second connection.
    """
    normalized = str(user_id)
    held = _hosted_runtime_config_lock_users.get()
    if normalized in held:
        yield
        return

    wait_sec = float(_HOSTED_RUNTIME_CONFIG_LOCK_WAIT_SEC)
    if not _hosted_runtime_config_connection_slots.acquire(timeout=wait_sec):
        raise HostedRuntimeConfigBusyError(
            "hosted runtime config mutation is busy"
        )
    conn = None
    token = None
    advisory_locked = False
    try:
        conn = listen_connection()
        deadline = time.monotonic() + wait_sec
        while True:
            row = conn.execute(
                "SELECT pg_try_advisory_lock("
                "hashtextextended('hosted-runtime-config:' || %s, 0))",
                (normalized,),
            ).fetchone()
            if row is not None and bool(row[0]):
                advisory_locked = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostedRuntimeConfigBusyError(
                    "hosted runtime config mutation is busy"
                )
            time.sleep(min(0.05, remaining))
        token = _hosted_runtime_config_lock_users.set(
            held | frozenset({normalized})
        )
        yield
    finally:
        if token is not None:
            _hosted_runtime_config_lock_users.reset(token)
        if advisory_locked and conn is not None:
            try:
                conn.execute(
                    "SELECT pg_advisory_unlock("
                    "hashtextextended('hosted-runtime-config:' || %s, 0))",
                    (normalized,),
                )
            except Exception as exc:  # connection close releases it regardless
                log.error(
                    "[db] hosted runtime config unlock(%s) failed: %s",
                    normalized,
                    exc,
                )
        if conn is not None:
            conn.close()
        _hosted_runtime_config_connection_slots.release()


def list_hosted_runtime_eligible_controls() -> list[tuple[str, str, str, int]]:
    """Runnable hosted users and their control tuples in one DB snapshot.

    Startup policy reconciliation must see unset, V2, and inconsistent control
    rows alike; it is independent of the retired per-user process roster.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT r.user_id,
              COALESCE(mrt.doc->>'hosted_runtime_mode','resident_cli') AS mode,
              COALESCE(vrs.hosted_runtime_state,'resident') AS runtime_state,
              COALESCE(vrs.runtime_generation,1) AS runtime_generation
            FROM model_api_routes r
            JOIN model_api_credentials c ON c.id = r.credential_id
            LEFT JOIN user_blobs mrt
              ON mrt.user_id = r.user_id
             AND mrt.kind = 'model_api_runtime'
            LEFT JOIN v2_runtime_state vrs ON vrs.user_id = r.user_id
            WHERE r.is_active
              AND r.test_status = 'ok'
              AND LOWER(c.provider) = ANY(%s)
            ORDER BY r.user_id
            """,
            (list(HOSTED_RUNTIME_SUPPORTED_PROVIDERS),),
        ).fetchall()
    return [
        (str(user_id), str(mode), str(state), int(generation))
        for user_id, mode, state, generation in rows
    ]


def list_hosted_runtime_eligible_user_ids() -> list[str]:
    """Every runnable hosted user, independent of the current runtime owner."""
    return [row[0] for row in list_hosted_runtime_eligible_controls()]


def list_hosted_runtime_nonresident_controls() -> list[tuple[str, str, str, int]]:
    """Compatibility inventory of controls not at the dormant resident fence.

    This is not a rollback roster. Hosted execution is V2-only; the query is
    retained for migration/account diagnostics until the legacy control values
    themselves can be removed in a separate schema cleanup.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT u.user_id,
              COALESCE(mrt.doc->>'hosted_runtime_mode','resident_cli') AS mode,
              COALESCE(vrs.hosted_runtime_state,'resident') AS runtime_state,
              COALESCE(vrs.runtime_generation,1) AS runtime_generation
            FROM users u
            LEFT JOIN user_blobs mrt
              ON mrt.user_id = u.user_id
             AND mrt.kind = 'model_api_runtime'
            LEFT JOIN v2_runtime_state vrs ON vrs.user_id = u.user_id
            WHERE COALESCE(mrt.doc->>'hosted_runtime_mode','resident_cli')
                    = 'db_action_v2'
               OR COALESCE(vrs.hosted_runtime_state,'resident') <> 'resident'
            ORDER BY u.user_id
            """
        ).fetchall()
    return [
        (str(user_id), str(mode), str(state), int(generation))
        for user_id, mode, state, generation in rows
    ]


def audit_resident_active_model_routes(*, apply: bool = False) -> list[dict]:
    """List route-selector/V1-route conflicts; optionally deactivate them.

    This deliberately does *not* use ``consumer_state.consumer_id``: that field
    is last-writer-wins and alternating hosted/resident pollers overwrite one
    another.  The model route is the configured V1 eligibility signal and
    ``agent_runtime_instances.lease_expires_at`` is the authoritative current
    hosted-process lease.  ``apply=False`` is the safe default used by the
    operator CLI.

    Apply returns the exact pre-change rows selected in its transaction, making
    a reviewed dry-run directly comparable with the eventual mutation.
    """
    query = """
        SELECT r.user_id,
               route_blob.doc,
               LOWER(COALESCE(NULLIF(route_blob.doc->>'route', ''), 'resident'))
                   AS onboarding_route,
               r.id::text,
               r.is_active,
               r.test_status,
               LOWER(c.provider),
               r.model,
               COALESCE(vrs.hosted_runtime_state, 'resident'),
               ari.status,
               ari.lease_owner,
               ari.lease_expires_at,
               (
                 ari.lease_owner IS NOT NULL
                 AND ari.lease_expires_at IS NOT NULL
                 AND ari.lease_expires_at >= now()
               ) AS runner_lease_active
        FROM model_api_routes r
        JOIN model_api_credentials c ON c.id = r.credential_id
        LEFT JOIN user_blobs route_blob
          ON route_blob.user_id = r.user_id
         AND route_blob.kind = 'onboarding_route'
        LEFT JOIN v2_runtime_state vrs ON vrs.user_id = r.user_id
        LEFT JOIN agent_runtime_instances ari ON ari.user_id = r.user_id
        WHERE r.is_active
          AND r.test_status = 'ok'
          AND LOWER(COALESCE(NULLIF(route_blob.doc->>'route', ''), 'resident'))
                <> 'model_api'
        ORDER BY r.user_id, r.id
        FOR UPDATE OF r
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            rows = conn.execute(query).fetchall()
            if apply and rows:
                conn.execute(
                    "UPDATE model_api_routes "
                    "SET is_active = FALSE, updated_at = now() "
                    "WHERE id::text = ANY(%s) AND is_active",
                    ([str(row[3]) for row in rows],),
                )

    def _iso(value) -> str:
        return value.isoformat() if value is not None else ""

    return [
        {
            "user_id": str(row[0]),
            "route_blob": row[1] if isinstance(row[1], dict) else None,
            "onboarding_route": str(row[2]),
            "model_api_route": {
                "id": str(row[3]),
                "is_active": bool(row[4]),
                "test_status": str(row[5]),
                "provider": str(row[6]),
                "model": str(row[7]),
            },
            "hosted_runtime_state": str(row[8]),
            "runner_lease": {
                "active": bool(row[12]),
                "status": str(row[9] or ""),
                "lease_owner": str(row[10] or ""),
                "lease_expires_at": _iso(row[11]),
            },
        }
        for row in rows
    ]


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
    LiteLLM chat-completions bridge)。

    Anti-double-run gates: the committed onboarding selector must positively be
    ``model_api`` and the hosted-runtime fence must not read ``v2``/``draining``.
    Missing/legacy route blobs therefore fail closed instead of silently
    spawning V1 beside a resident consumer.  V2 ownership remains the second,
    independent fence.
    """
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
                  AND EXISTS (
                    SELECT 1 FROM user_blobs route_blob
                    WHERE route_blob.user_id = r.user_id
                      AND route_blob.kind = 'onboarding_route'
                      AND LOWER(COALESCE(route_blob.doc->>'route', '')) = 'model_api'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM v2_runtime_state vrs
                    WHERE vrs.user_id = r.user_id
                      AND vrs.hosted_runtime_state IN ('v2', 'draining')
                  )
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
                with conn.cursor() as cur:
                    # Serialize with Capture effect commit and every ordinary
                    # settings patch. The conflict UPDATE already preserves
                    # peer fields; this lock also covers the no-row insertion
                    # race where PostgreSQL has no tuple to lock yet.
                    _lock_chat_user_fence_on_cursor(cur, str(user_id))
                    _lock_capture_consent_on_cursor(cur, str(user_id))
                    cur.execute(
                        claim_sql,
                        (user_id, Jsonb(settings_doc), at_iso, at_iso),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        claimed_doc = row[0]
                        cur.execute(
                            job_sql,
                            (user_id, ts, item_key, Jsonb(job)),
                        )
                        seq = cur.fetchone()[0]
    except Exception as e:
        log.error("[db] claim_and_enqueue_introduction(%s) failed: %s", user_id, e)
        return None
    if claimed_doc is None or seq is None:
        return None
    # Committed on the primary. Re-read/mirror the settings row under the same
    # consent fence so a late introduction callback cannot overwrite a newer
    # Capture opt-out in TEE. The job seq remains pinned so the shadow keeps the
    # row identity every seq-ordered read relies on.
    from tee_shadow import mirror
    _mirror_proactive_settings_current(str(user_id))
    mirror.execute(
        "INSERT INTO user_logs (user_id, stream, seq, ts, item_key, doc) "
        "OVERRIDING SYSTEM VALUE VALUES (%s, 'proactive_jobs', %s, %s, %s, %s) "
        "ON CONFLICT (user_id, stream, seq) DO NOTHING",
        (user_id, seq, ts, item_key, Jsonb(job)),
    )
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


class GenesisRedistillJobActive(Exception):
    """Raised by ``genesis_create_job`` when the insert collides with the
    partial unique index (0023_redistill_job_exclusivity) that enforces "one
    active resident_redistill job per user" — NOT the ``(user_id, job_id)``
    primary key (that conflict is absorbed by ``ON CONFLICT ... DO NOTHING``
    below and simply returns ``None``, same as before this index existed).
    Plaintext imports use the separate ``GenesisPlaintextJobActive`` contract.

    Carries the job_id of whichever OTHER job currently holds the exclusivity
    slot, so the caller (``genesis_core._resident_sealed_import``) can surface
    it as ``409 {"error": "redistill_job_active", "active_job_id": ...}``
    without a second round trip of its own."""

    def __init__(self, active_job_id: str):
        super().__init__(f"resident_redistill job already active: {active_job_id}")
        self.active_job_id = active_job_id


class GenesisPlaintextJobActive(Exception):
    """A different processing plaintext import already owns this user's slot."""

    def __init__(self, active_job_id: str):
        super().__init__(f"plaintext import job already active: {active_job_id}")
        self.active_job_id = active_job_id


def _genesis_active_job_of_kind(conn, user_id: str, source_kind: str) -> dict | None:
    """The job currently occupying the redistill-exclusivity slot for this user
    (same statuses the partial unique index watches). Used only to resolve
    ``GenesisRedistillJobActive``'s ``active_job_id`` — never called on the hot
    path for other job kinds, since only ``resident_redistill`` can raise it."""
    cur = conn.execute(
        "SELECT * FROM genesis_import_jobs WHERE user_id = %s AND source_kind = %s "
        "AND status IN ('awaiting_resident', 'processing') "
        "ORDER BY updated_at DESC LIMIT 1",
        (user_id, source_kind),
    )
    return _genesis_row(cur, cur.fetchone())


def _genesis_active_plaintext_job(conn, user_id: str) -> dict | None:
    cur = conn.execute(
        "SELECT * FROM genesis_import_jobs WHERE user_id = %s "
        "AND status = 'processing' AND metadata->>'ingest' = 'plaintext' "
        "ORDER BY updated_at DESC LIMIT 1",
        (user_id,),
    )
    return _genesis_row(cur, cur.fetchone())


def genesis_create_job(user_id: str, job: dict) -> dict | None:
    """Insert a new genesis import job. Conflict shapes stay explicit
    so callers can't mistake one for the other:

    (a) same ``(user_id, job_id)`` already exists (idempotent retry — job_id is
        a deterministic hash of the caller's request/client_job_id) — absorbed
        by ``ON CONFLICT ... DO NOTHING``; returns ``None`` and the caller does
        its own ``genesis_get_job`` lookup to hand back the existing job, same
        as before this function had any notion of exclusivity.
    (b) a DIFFERENT job_id of source_kind='resident_redistill' is already
        active for this user (0023's partial unique index) — raises
        ``GenesisRedistillJobActive(active_job_id=...)``. This can only happen
        for that one job kind.
    (c) a DIFFERENT processing job with metadata.ingest='plaintext' is active
        for this user (0074's partial unique index) — raises
        ``GenesisPlaintextJobActive(active_job_id=...)``.
    """
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
    source_kind = job.get("source_kind", "unknown")
    params = (
        user_id,
        job["job_id"],
        job.get("status", "created"),
        source_kind,
        job.get("file_manifest_hash", ""),
        int(job.get("total_chunks") or 0),
        int(job.get("total_bytes") or 0),
        job.get("privacy_mode", ""),
        Jsonb(job.get("metadata") or {}),
    )
    with get_pool().connection() as conn:
        try:
            cur = conn.execute(sql, params)
        except psycopg.errors.UniqueViolation:
            # Not the (user_id, job_id) PK (that's ON CONFLICT DO NOTHING above) —
            # this is the redistill-exclusivity partial index. autocommit=True means
            # this failed statement was its own implicit transaction, so the
            # connection is immediately usable for the lookup below (no rollback
            # needed, unlike a multi-statement transaction).
            metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
            if str(metadata.get("ingest") or "") == "plaintext":
                active = _genesis_active_plaintext_job(conn, user_id)
                raise GenesisPlaintextJobActive(active["job_id"] if active else "") from None
            active = _genesis_active_job_of_kind(conn, user_id, source_kind)
            raise GenesisRedistillJobActive(active["job_id"] if active else "") from None
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


def recent_genesis_import_health(
    *,
    within_hours: int = 24,
    recent_limit: int = 50,
) -> dict:
    """Content-free Genesis fleet health for the Admin import view.

    ``status='done'`` is deliberately kept separate from
    ``artifact_verified``.  The former is a reducer lifecycle fact; the latter
    is conservative evidence from the durable ledger that the job produced the
    artifact its mode called for.  This avoids turning a terminal row into a
    false-green import result while we still lack a dedicated per-attempt
    artifact-verification ledger.

    The returned failure code is derived in SQL from at most the first two
    snake-case error segments.  Free-form exception text is neither returned
    nor rendered by the metadata-only Admin page.
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    safe_limit = max(1, min(int(recent_limit), 200))
    from psycopg.rows import dict_row

    # Modes have slightly different success artifacts.  add_memory requires a
    # memory write; update_identity requires an identity write.  Onboarding is
    # intentionally strict: when source material exists it requires both.  A
    # legitimate nameless/fresh-start completion may therefore remain
    # "unverified" rather than being mislabeled successful.
    classification_cte = """
      WITH recent AS (
        SELECT g.*,
          lower(coalesce(nullif(g.metadata->>'mode',''), g.source_kind, 'unknown'))
            AS import_mode,
          (
            g.total_chunks > 0 OR g.total_bytes > 0 OR
            (coalesce(g.metadata->>'content_bytes','') ~ '^[0-9]+$'
              AND (g.metadata->>'content_bytes')::bigint > 0) OR
            (coalesce(g.metadata->>'history_count','') ~ '^[0-9]+$'
              AND (g.metadata->>'history_count')::bigint > 0) OR
            (coalesce(g.metadata->>'support_count','') ~ '^[0-9]+$'
              AND (g.metadata->>'support_count')::bigint > 0)
          ) AS has_source_material,
          (
            lower(coalesce(g.identity_status,'')) IN
              ('initialized','updated','already_initialized')
            OR coalesce(g.persona_ref,'') <> ''
          ) AS has_identity_evidence
        FROM genesis_import_jobs g
        WHERE g.created_at >= now() - make_interval(hours => %s)
      ), classified AS (
        SELECT recent.*,
          CASE
            WHEN import_mode LIKE '%%add_memory%%'
              THEN memory_action_count > 0
            WHEN import_mode LIKE '%%update_identity%%'
              OR import_mode = 'resident_redistill'
              THEN has_identity_evidence
            ELSE has_identity_evidence
              AND (NOT has_source_material OR memory_action_count > 0)
          END AS artifact_evidence_complete
        FROM recent
      )
    """

    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                classification_cte
                + """
                SELECT
                  count(*)::int AS started,
                  count(DISTINCT user_id)::int AS users,
                  count(*) FILTER (WHERE status='done')::int AS completed,
                  count(*) FILTER (
                    WHERE status='done' AND artifact_evidence_complete
                  )::int AS artifact_verified,
                  count(*) FILTER (
                    WHERE status='done' AND NOT artifact_evidence_complete
                  )::int AS completed_unverified,
                  count(*) FILTER (WHERE status='failed')::int AS failed,
                  count(*) FILTER (
                    WHERE status NOT IN ('done','failed')
                  )::int AS processing,
                  count(*) FILTER (
                    WHERE status NOT IN ('done','failed')
                      AND updated_at < clock_timestamp() - interval '15 minutes'
                  )::int AS stuck_over_15m,
                  percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY extract(epoch FROM (completed_at-created_at))
                  ) FILTER (
                    WHERE status='done' AND completed_at IS NOT NULL
                      AND completed_at >= created_at
                  ) AS p50_complete_sec,
                  percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY extract(epoch FROM (completed_at-created_at))
                  ) FILTER (
                    WHERE status='done' AND completed_at IS NOT NULL
                      AND completed_at >= created_at
                  ) AS p95_complete_sec
                FROM classified
                """,
                (safe_hours,),
            )
            summary = cur.fetchone() or {}

            cur.execute(
                """
                WITH recent AS (
                  SELECT error FROM genesis_import_jobs
                  WHERE status='failed'
                    AND created_at >= now() - make_interval(hours => %s)
                ), safe AS (
                  SELECT CASE
                    WHEN lower(error) ~ '^[a-z0-9_]+:[a-z0-9_]+(:.*)?$'
                      THEN split_part(lower(error),':',1) || ':' ||
                           split_part(lower(error),':',2)
                    WHEN lower(error) ~ '^[a-z0-9_]+(:.*)?$'
                      THEN split_part(lower(error),':',1)
                    ELSE 'other'
                  END AS error_code
                  FROM recent
                )
                SELECT error_code, count(*)::int AS count
                FROM safe GROUP BY error_code ORDER BY count DESC, error_code
                LIMIT 12
                """,
                (safe_hours,),
            )
            failure_reasons = [dict(row) for row in cur.fetchall()]

            cur.execute(
                classification_cte
                + """
                SELECT
                  user_id, job_id, status, source_kind, import_mode,
                  artifact_evidence_complete,
                  has_identity_evidence,
                  has_source_material,
                  memory_action_count,
                  identity_status,
                  created_at, updated_at, completed_at,
                  CASE
                    WHEN status='failed' AND
                      lower(error) ~ '^[a-z0-9_]+:[a-z0-9_]+(:.*)?$'
                      THEN split_part(lower(error),':',1) || ':' ||
                           split_part(lower(error),':',2)
                    WHEN status='failed' AND
                      lower(error) ~ '^[a-z0-9_]+(:.*)?$'
                      THEN split_part(lower(error),':',1)
                    WHEN status='failed' THEN 'other'
                    ELSE ''
                  END AS error_code,
                  extract(epoch FROM (clock_timestamp()-updated_at))
                    AS age_since_update_sec
                FROM classified
                ORDER BY created_at DESC, user_id, job_id
                LIMIT %s
                """,
                (safe_hours, safe_limit),
            )
            recent_jobs = [dict(row) for row in cur.fetchall()]

    def optional_float(value):
        return None if value is None else float(value)

    completed = int(summary.get("completed") or 0)
    verified = int(summary.get("artifact_verified") or 0)
    return {
        "window_hours": safe_hours,
        "started": int(summary.get("started") or 0),
        "users": int(summary.get("users") or 0),
        "completed": completed,
        "artifact_verified": verified,
        "completed_unverified": int(summary.get("completed_unverified") or 0),
        "failed": int(summary.get("failed") or 0),
        "processing": int(summary.get("processing") or 0),
        "stuck_over_15m": int(summary.get("stuck_over_15m") or 0),
        "terminal_success_rate": (
            float(completed) / float(completed + int(summary.get("failed") or 0))
            if completed + int(summary.get("failed") or 0) else None
        ),
        "artifact_verified_rate": (
            float(verified) / float(completed) if completed else None
        ),
        "p50_complete_sec": optional_float(summary.get("p50_complete_sec")),
        "p95_complete_sec": optional_float(summary.get("p95_complete_sec")),
        "failure_reasons": failure_reasons,
        "recent_jobs": recent_jobs,
        "evidence_contract": "ledger_strict_v1",
    }


def genesis_patch_job_metadata(user_id: str, job_id: str, patch: dict) -> dict | None:
    if not isinstance(patch, dict) or not patch:
        return genesis_get_job(user_id, job_id)
    sql = (
        "UPDATE genesis_import_jobs SET metadata = metadata || %s::jsonb, "
        "updated_at = now() WHERE user_id = %s AND job_id = %s RETURNING *"
    )
    params = (Jsonb(patch), user_id, job_id)
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        result = _genesis_row(cur, cur.fetchone())
    from tee_shadow import mirror
    mirror.execute(sql, params)
    return result


def genesis_claim_uploaded_jobs(*, worker_id: str = "", limit: int = 1) -> list[dict]:
    """Atomically claim uploaded genesis jobs for the CVM worker.

    Uses SKIP LOCKED so multiple worker loops can poll without double-processing
    the same import. Claimed jobs move uploaded -> processing in the same
    transaction; genesis_state is updated by the worker service layer.

    ``worker_id`` attributes the claim (``worker_claimed_by``/``worker_claimed_at``)
    so the death-detected reclaim (genesis_reclaim_orphaned_processing_jobs) can
    tell whose claim went stale when a worker is killed. It MUST be the same id the
    worker heartbeats under (``<worker_id>:genesis``). Default ``""`` keeps the
    pre-attribution behavior for any caller/test that does not pass it.
    """
    safe_limit = max(1, min(int(limit or 1), 16))
    wid = str(worker_id or "")
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
                    worker_claimed_by = %s,
                    worker_claimed_at = now(),
                    updated_at = now()
                FROM picked
                WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
                RETURNING j.*
                """,
                (safe_limit, wid),
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
            "output = jsonb_build_object('stage', 'worker_claimed'), "
            "worker_claimed_by = %s, worker_claimed_at = now(), updated_at = now() "
            f"WHERE (user_id, job_id) IN ({placeholders})"
        )
        mirror_params = (wid, *(v for item in out for v in (item["user_id"], item["job_id"])))
        from tee_shadow import mirror
        mirror.execute(mirror_sql, mirror_params)
    return out


def genesis_reclaim_orphaned_processing_jobs(
    live_worker_ids: list[str], *, dead_sec: int, error: str, limit: int = 50,
) -> list[dict]:
    """Fast-recover 'processing' genesis jobs whose claiming worker is dead.

    A job whose ``worker_claimed_by`` is not among the live ``kind='genesis'``
    heartbeats (and was claimed more than ``dead_sec`` ago) was left behind by a
    killed/replaced worker — most often a container deploy. Instead of waiting out
    the 30-min time reaper (genesis_reap_stale_processing_jobs, the backstop),
    recover it now:

    - **resumable** (``received_chunks > 0`` — encrypted chunks are stored) → reset
      to ``uploaded`` and clear the attribution so a live worker re-claims + re-runs
      the distill. Auto-recovery, no user action.
    - **non-resumable** (``received_chunks = 0`` — plaintext onboarding, which is
      never persisted) → ``failed`` with ``error`` so the client retries in seconds.

    Atomic + ``FOR UPDATE SKIP LOCKED`` so it can't race a live reducer. Rows with a
    blank ``worker_claimed_by`` (pre-attribution / legacy) are intentionally NOT
    eligible — the time reaper owns those. Returns the changed rows with an added
    ``_reclaim_action`` (``"requeued"`` | ``"failed"``) so the caller can sync the
    genesis_state blob.
    """
    safe_sec = max(60, int(dead_sec or 0))
    safe_limit = max(1, min(int(limit or 1), 200))
    live = list(dict.fromkeys(str(w) for w in (live_worker_ids or []) if str(w)))
    _UPDATE_SET = (
        "status = CASE WHEN {j}.received_chunks > 0 THEN 'uploaded' ELSE 'failed' END, "
        "error = CASE WHEN {j}.received_chunks > 0 THEN '' ELSE %s END, "
        "worker_claimed_by = CASE WHEN {j}.received_chunks > 0 THEN '' ELSE {j}.worker_claimed_by END, "
        "worker_claimed_at = CASE WHEN {j}.received_chunks > 0 THEN NULL ELSE {j}.worker_claimed_at END, "
        "updated_at = now()"
    )
    with get_pool().connection() as conn:
        cur = conn.execute(
            f"""
            WITH picked AS (
                SELECT user_id, job_id FROM genesis_import_jobs
                WHERE status = 'processing'
                  AND COALESCE(worker_claimed_by, '') <> ''
                  AND NOT (worker_claimed_by = ANY(%s))
                  AND worker_claimed_at < now() - make_interval(secs => %s)
                ORDER BY worker_claimed_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE genesis_import_jobs AS j SET {_UPDATE_SET.format(j='j')}
            FROM picked
            WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
            RETURNING j.*
            """,
            (live, safe_sec, safe_limit, error[:1000]),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    out: list[dict] = []
    for row in rows:
        item = dict(zip(cols, row))
        for key, value in list(item.items()):
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
        item["_reclaim_action"] = "requeued" if str(item.get("status")) == "uploaded" else "failed"
        out.append(item)
    if out:
        # Same TEE-mirror discipline as the claim / time-reaper: pin to the exact
        # (user_id, job_id) pairs the primary changed, no re-selection drift.
        placeholders = ", ".join(["(%s, %s)"] * len(out))
        mirror_sql = (
            f"UPDATE genesis_import_jobs AS j SET {_UPDATE_SET.format(j='j')} "
            f"WHERE (j.user_id, j.job_id) IN ({placeholders})"
        )
        mirror_params = (error[:1000], *(v for item in out for v in (item["user_id"], item["job_id"])))
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


def genesis_fail_stale_plaintext_job(
    user_id: str,
    job_id: str,
    *,
    older_than_sec: int,
    error: str,
    expected_worker_instance: str = "",
    force: bool = False,
) -> dict | None:
    """Atomically fail one abandoned in-process plaintext import.

    Plaintext is held only by the API process that accepted it. Its companion
    heartbeat updates ``updated_at`` independently of provider calls. A same-host
    replacement can force recovery after proving the owner PID is gone; remote
    owners use the stale interval because they may still be active during a
    rolling deploy. The expected instance fences both paths against ownership
    changing between the read and this update.
    """
    safe_sec = max(60, int(older_than_sec or 0))
    sql = """
        UPDATE genesis_import_jobs SET
            status = 'failed',
            error = %s,
            updated_at = now()
        WHERE user_id = %s AND job_id = %s
          AND status = 'processing'
          AND COALESCE(metadata->>'ingest', '') = 'plaintext'
          AND COALESCE(metadata->>'plaintext_worker_instance', '') = %s
          AND (%s OR updated_at < now() - make_interval(secs => %s))
        RETURNING *
    """
    params = (
        error[:1000],
        user_id,
        job_id,
        str(expected_worker_instance or ""),
        bool(force),
        safe_sec,
    )
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        result = _genesis_row(cur, cur.fetchone())
    if result is not None:
        from tee_shadow import mirror
        mirror.execute(
            "UPDATE genesis_import_jobs SET status='failed', error=%s, updated_at=now() "
            "WHERE user_id=%s AND job_id=%s AND status='processing' "
            "AND COALESCE(metadata->>'ingest', '')='plaintext' "
            "AND COALESCE(metadata->>'plaintext_worker_instance', '')=%s",
            (
                error[:1000],
                user_id,
                job_id,
                str(expected_worker_instance or ""),
            ),
        )
    return result


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
        try:
            cur = conn.execute(sql, params)
        except psycopg.errors.UniqueViolation:
            if status != "processing":
                raise
            active = _genesis_active_plaintext_job(conn, user_id)
            if active:
                raise GenesisPlaintextJobActive(active["job_id"]) from None
            redistill = _genesis_active_job_of_kind(conn, user_id, "resident_redistill")
            if redistill:
                raise GenesisRedistillJobActive(redistill["job_id"]) from None
            raise
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


def genesis_touch_plaintext_job(
    user_id: str,
    job_id: str,
    *,
    worker_instance: str,
) -> bool:
    """Renew an in-process plaintext lease only while this instance owns it."""
    sql = (
        """
        UPDATE genesis_import_jobs SET updated_at = now()
        WHERE user_id = %s AND job_id = %s AND status = 'processing'
          AND COALESCE(metadata->>'ingest', '') = 'plaintext'
          AND COALESCE(metadata->>'plaintext_worker_instance', '') = %s
        """
    )
    params = (user_id, job_id, str(worker_instance or ""))
    with get_pool().connection() as conn:
        cur = conn.execute(sql, params)
        renewed = cur.rowcount > 0
    if renewed:
        from tee_shadow import mirror
        mirror.execute(sql, params)
    return renewed


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
# Chat messages (durable row-per-item ledger; process caches are bounded)
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


def chat_count_since(user_id: str, since: float, *, cap: int) -> int:
    """How many chat rows this user has with ``ts > since``, counted in the DB
    (capped at ``cap``). The staleness self-heal compares this against the same
    count over the in-memory ring: a *missing middle* row — a dropped
    cross-worker broadcast for a message that is NOT the newest — leaves the two
    "newest ts" values equal, so only a per-window COUNT can see it.

    Capped so a very old ``since`` (window wider than the in-memory ring) can't
    make the DB count structurally exceed the ring and trigger a reload every
    call: past ``cap`` rows both sides saturate and compare equal. ``cap`` is
    the ring size, where the in-memory count itself saturates. Raises on DB
    failure — the caller fails open (staleness degrades, availability doesn't)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM ("
            "  SELECT 1 FROM chat_messages WHERE user_id = %s AND ts > %s LIMIT %s"
            ") t",
            (user_id, since, max(1, int(cap))),
        ).fetchone()
    return int(row[0]) if row else 0



def chat_load_strict(user_id: str) -> list[dict]:
    """Load the user's complete durable chat history. R2-offloaded file rows are returned as SLIM
    POINTERS (``body_key`` + ``body_ct_len``, no ``body_ct``) — the heavy
    ciphertext is fetched lazily only at the read exits that actually deliver a
    body (``hydrate_chat_file_body``), so a bulk/metadata-only load never
    downloads every historical file.

    This is an explicit full-history API. Hot runtime state must use
    :func:`chat_load_recent_strict` so retaining an immutable source transcript
    does not turn every worker cache refresh into an unbounded read."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id = %s ORDER BY seq ASC",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


def chat_load_recent_strict(user_id: str, limit: int) -> list[dict]:
    """Load only the newest ``limit`` durable rows, returned oldest-to-newest.

    The durable table is intentionally unbounded by prompt compaction. This
    bounded API is the process-cache boundary; it replaces the old design where
    the same limit was enforced by physically deleting source messages.
    """
    bounded = max(1, int(limit))
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT doc FROM ("
            "  SELECT seq, doc FROM chat_messages WHERE user_id = %s "
            "  ORDER BY seq DESC LIMIT %s"
            ") recent ORDER BY seq ASC",
            (user_id, bounded),
        ).fetchall()
    return [r[0] for r in rows]


def chat_load_recent(user_id: str, limit: int) -> list[dict]:
    """Legacy best-effort wrapper around :func:`chat_load_recent_strict`."""
    try:
        return chat_load_recent_strict(user_id, limit)
    except Exception as e:
        log.error("[db] chat_load_recent(%s,%s) failed: %s", user_id, limit, e)
        return []


def chat_history_page_strict(
    user_id: str,
    *,
    limit: int,
    since: float = 0.0,
    before: float = 0.0,
    hide_verify_before: float | None = None,
) -> list[dict]:
    """Read one bounded history page directly from durable storage.

    ``before`` and ``since`` preserve the public timestamp cursor semantics;
    callers request ``limit + 1`` when they need a has-more sentinel. Rows are
    always returned in append order and file bodies remain lazy R2 pointers.
    """
    bounded = max(1, int(limit))
    visibility_sql = ""
    visibility_params: tuple = ()
    if hide_verify_before is not None:
        visibility_sql = (
            " AND NOT (doc->>'source'='verify_ping' AND ("
            "   doc->>'role' IN ('agent','openclaw') OR ts < %s"
            " ))"
        )
        visibility_params = (float(hide_verify_before),)
    with get_pool().connection() as conn:
        if before > 0:
            rows = conn.execute(
                "SELECT seq,msg_id,doc FROM ("
                "  SELECT seq,msg_id,doc FROM chat_messages "
                "  WHERE user_id = %s AND ts < %s "
                + visibility_sql +
                "  ORDER BY seq DESC LIMIT %s"
                ") page ORDER BY seq ASC",
                (user_id, float(before), *visibility_params, bounded),
            ).fetchall()
        elif since > 0:
            rows = conn.execute(
                "SELECT seq,msg_id,doc FROM chat_messages "
                "WHERE user_id = %s AND ts > %s "
                + visibility_sql +
                "ORDER BY seq ASC LIMIT %s",
                (user_id, float(since), *visibility_params, bounded),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT seq,msg_id,doc FROM ("
                "  SELECT seq,msg_id,doc FROM chat_messages WHERE user_id = %s "
                + visibility_sql +
                "  ORDER BY seq DESC LIMIT %s"
                ") page ORDER BY seq ASC",
                (user_id, *visibility_params, bounded),
            ).fetchall()
    out: list[dict] = []
    for seq, msg_id, doc in rows:
        item = dict(doc)
        item.setdefault("id", str(msg_id))
        item["seq"] = int(seq)
        out.append(item)
    return out


def chat_history_page_by_seq_strict(
    user_id: str,
    *,
    limit: int,
    after_seq: int = 0,
    before_seq: int = 0,
    latest: bool = False,
    hide_verify_before: float | None = None,
) -> list[dict]:
    """Read a bounded durable page using the exact append-order cursor.

    This is the tie-safe primitive for exports, backfills, and the public UI
    cursor. Unlike the legacy timestamp query, it can traverse thousands of
    rows sharing the same timestamp without skipping a boundary row. ``latest``
    selects the newest bounded page when neither directional cursor is present.
    """
    bounded = max(1, int(limit))
    visibility_sql = ""
    visibility_params: tuple = ()
    if hide_verify_before is not None:
        visibility_sql = (
            " AND NOT (doc->>'source'='verify_ping' AND ("
            "   doc->>'role' IN ('agent','openclaw') OR ts < %s"
            " ))"
        )
        visibility_params = (float(hide_verify_before),)
    with get_pool().connection() as conn:
        if before_seq > 0:
            rows = conn.execute(
                "SELECT seq, msg_id, doc FROM ("
                "  SELECT seq, msg_id, doc FROM chat_messages "
                "  WHERE user_id=%s AND seq<%s "
                + visibility_sql +
                "  ORDER BY seq DESC LIMIT %s"
                ") page ORDER BY seq ASC",
                (user_id, int(before_seq), *visibility_params, bounded),
            ).fetchall()
        elif latest:
            rows = conn.execute(
                "SELECT seq, msg_id, doc FROM ("
                "  SELECT seq, msg_id, doc FROM chat_messages "
                "  WHERE user_id=%s "
                + visibility_sql +
                "  ORDER BY seq DESC LIMIT %s"
                ") page ORDER BY seq ASC",
                (user_id, *visibility_params, bounded),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT seq, msg_id, doc FROM chat_messages "
                "WHERE user_id=%s AND seq>%s "
                + visibility_sql +
                "ORDER BY seq ASC LIMIT %s",
                (user_id, max(0, int(after_seq)), *visibility_params, bounded),
            ).fetchall()
    out: list[dict] = []
    for seq, msg_id, doc in rows:
        item = dict(doc)
        # ``msg_id`` is the durable identity column. Production envelopes also
        # carry it as ``doc.id``, but exports/backfills must not depend on that
        # denormalized copy being present (older rows and direct imports may
        # legitimately omit it).
        item.setdefault("id", str(msg_id))
        item["seq"] = int(seq)
        out.append(item)
    return out


def chat_count_strict(
    user_id: str,
    *,
    hide_verify_before: float | None = None,
) -> int:
    """Return a durable row count without materializing history."""
    visibility_sql = ""
    params: tuple = (user_id,)
    if hide_verify_before is not None:
        visibility_sql = (
            " AND NOT (doc->>'source'='verify_ping' AND ("
            "   doc->>'role' IN ('agent','openclaw') OR ts < %s"
            " ))"
        )
        params = (user_id, float(hide_verify_before))
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id = %s"
            + visibility_sql,
            params,
        ).fetchone()
    return int(row[0]) if row else 0


def chat_get_strict(user_id: str, msg_id: str) -> dict | None:
    """Read one durable chat row by its stable id, leaving R2 bodies lazy."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id = %s AND msg_id = %s",
            (user_id, msg_id),
        ).fetchone()
    return row[0] if row else None


def chat_load(user_id: str) -> list[dict]:
    """Legacy best-effort wrapper; V2 correctness paths use chat_load_strict."""
    try:
        return chat_load_strict(user_id)
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
            with conn.cursor() as cur:
                # Greeting writes participate in the same clear/account-delete
                # linearization protocol as every ordinary chat writer.  Pin
                # the live storage generation as well: after a previous clear,
                # the column default (generation zero) is already retired.
                storage_generation = _lock_chat_r2_lifecycle_on_cursor(
                    cur, user_id,
                )
                cur.execute(
                    "INSERT INTO chat_messages "
                    "(user_id, msg_id, ts, doc, storage_generation) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (user_id, msg_id) DO NOTHING RETURNING doc",
                    (user_id, msg_id, ts, Jsonb(doc), storage_generation),
                )
                row = cur.fetchone()
                inserted = row is not None
                if not inserted:
                    # ON CONFLICT waits out an in-flight conflicting insert, so
                    # the winner is committed and visible to this statement.
                    cur.execute(
                        "SELECT doc FROM chat_messages "
                        "WHERE user_id = %s AND msg_id = %s",
                        (user_id, msg_id),
                    )
                    row = cur.fetchone()
    if row is None:
        # Conflict fired yet the row is gone despite the shared chat/lifecycle
        # fences. Surface the invariant violation rather than invent an answer.
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

# Only fields that define/decrypt the main ciphertext belong in the offload
# compare-and-swap. Operational metadata (reply claims/status, push delivery,
# etc.) is deliberately excluded so it can be merged while an upload is in
# flight without forcing the heavy body to stay inline. Presence is compared as
# well as value: an absent field and an explicit JSON null are distinct envelope
# versions.
_CHAT_BODY_CAS_FIELDS = (
    "id",
    "v",
    "body_ct",
    "nonce",
    "K_user",
    "K_enclave",
    "enclave_pk_fpr",
    "content_pk_fpr",
    "visibility",
    "owner_user_id",
    "content_type",
)
_CHAT_BODY_CAS_PREDICATE = " AND ".join(
    f"(doc->'{field}') IS NOT DISTINCT FROM %s"
    for field in _CHAT_BODY_CAS_FIELDS
)


class ChatPointerReplayConflict(RuntimeError):
    """A pointer-only envelope tried to create or restore a non-current key."""


def chat_max_seq(user_id: str) -> int:
    """Highest ``chat_messages.seq`` for the user, or 0 if the user has no
    messages. Anchors the stable per-user reply cursor (spec A1): ``seq`` is a
    real monotonic identity-column counter, unlike wall-clock ``ts`` — two
    messages appended in the same instant (common under concurrent workers)
    can share an identical ``ts``, which would make a ts-based cursor
    non-monotonic and risk silently skipping or reprocessing a message."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT MAX(seq) FROM chat_messages WHERE user_id = %s", (user_id,),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def chat_latest_genuine_user_ts(
    user_id: str,
    *,
    through_seq: int | None = None,
) -> float | None:
    """Latest real user-message timestamp inside an optional frozen frontier."""
    params: list = [str(user_id)]
    upper_predicate = ""
    if through_seq is not None:
        upper = int(through_seq)
        if upper < 0:
            raise ValueError("through_seq must be >= 0")
        upper_predicate = "AND seq <= %s "
        params.append(upper)
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT ts FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role' IN ('user','human') "
            "AND COALESCE(doc->>'source','') "
            "NOT IN ('verify_ping','resident_maintenance') "
            + upper_predicate
            + "ORDER BY seq DESC LIMIT 1",
            tuple(params),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def chat_max_user_seq_between(
    user_id: str,
    after_seq: int,
    through_seq: int,
) -> int:
    """Highest user-input seq in one closed prompt frontier, or ``0``.

    This is the metadata-only companion to the decrypted prompt readers.  It
    lets wake/chat cursor arbitration detect unanswered input even when that
    row has already moved behind the encrypted-summary watermark and therefore
    is no longer present in the verbatim tail.
    """
    lower = int(after_seq)
    upper = int(through_seq)
    if lower < 0 or upper < 0:
        raise ValueError("chat seq bounds must be >= 0")
    if upper <= lower:
        return 0
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT MAX(seq) FROM chat_messages "
            "WHERE user_id=%s AND seq>%s AND seq<=%s "
            "AND doc->>'role' IN ('user','human')",
            (user_id, lower, upper),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


_CHAT_COVERAGE_SOURCE_PREDICATE = (
    "COALESCE(doc->>'source','') "
    "NOT IN ('verify_ping','resident_maintenance')"
)


def chat_messages_after_seq(
    user_id: str,
    after_seq: int,
    *,
    limit: int | None = None,
    oldest_first: bool = True,
    through_seq: int | None = None,
    exclude_synthetic_sources: bool = False,
) -> list[dict]:
    """Return one exact per-user ``seq`` window strictly after ``after_seq``.

    ``oldest_first=True`` returns the oldest ``limit`` rows after the cursor;
    ``False`` returns the newest ``limit`` rows after it, but still orders the
    selected window ascending for prompt/replay consumers.  ``limit=None`` is
    the unbounded catch-up form used by the reply coalescer. ``through_seq``
    freezes the upper edge of the window (inclusive), so messages arriving
    after a turn's snapshot cannot change exact prompt membership mid-read.

    The relational ``msg_id``/``ts`` columns are authoritative and overwrite
    any stale copies in ``doc``.  Every result therefore carries an exact
    ``id``/``ts``/``seq`` triple suitable for a durable cursor and for exact
    prompt-membership checks.  Ordering never depends on wall-clock ``ts``.
    """
    cursor_seq = int(after_seq)
    if cursor_seq < 0:
        raise ValueError("after_seq must be >= 0")
    upper_seq = int(through_seq) if through_seq is not None else None
    if upper_seq is not None and upper_seq < 0:
        raise ValueError("through_seq must be >= 0")
    if upper_seq is not None and upper_seq <= cursor_seq:
        return []
    if limit is not None and int(limit) <= 0:
        return []
    predicate = "WHERE user_id = %s AND seq > %s"
    params: list = [user_id, cursor_seq]
    if upper_seq is not None:
        predicate += " AND seq <= %s"
        params.append(upper_seq)
    if exclude_synthetic_sources:
        # Summary-coverage callers only. `verify_ping`/`resident_maintenance`
        # rows are GC-able (a verify_ping is deleted once verify_loop completes,
        # see core/store.py), so folding one into an immutable leaf leaves a
        # coverage claim over a seq that later vanishes — the permanent
        # `v2_summary_frontier_integrity_error` brick. The gap counter
        # (count_messages_after_seq) and both frontier witnesses
        # (jobs_store.get_summary_frontier_state / append_summary_leaf_cas)
        # exclude the SAME set, so coverage stays consistent under GC.
        predicate += f" AND {_CHAT_COVERAGE_SOURCE_PREDICATE}"
    with get_pool().connection() as conn:
        if limit is None:
            rows = conn.execute(
                "SELECT seq, msg_id, ts, doc FROM chat_messages "
                f"{predicate} ORDER BY seq ASC",
                tuple(params),
            ).fetchall()
        elif oldest_first:
            rows = conn.execute(
                "SELECT seq, msg_id, ts, doc FROM chat_messages "
                f"{predicate} ORDER BY seq ASC LIMIT %s",
                (*params, int(limit)),
            ).fetchall()
        else:
            # Pick the newest bounded window with the index-friendly DESC
            # scan, then restore chronological order for the caller.
            rows = conn.execute(
                "SELECT seq, msg_id, ts, doc FROM ("
                "  SELECT seq, msg_id, ts, doc FROM chat_messages "
                f"  {predicate} ORDER BY seq DESC LIMIT %s"
                ") newest ORDER BY seq ASC",
                (*params, int(limit)),
            ).fetchall()
    return [
        {
            **dict(r[3] or {}),
            "id": str(r[1]),
            "ts": float(r[2]),
            "seq": int(r[0]),
        }
        for r in rows
    ]


def chat_coverage_bounds_after_seq(
    user_id: str,
    after_seq: int,
    *,
    limit: int,
    through_seq: int | None = None,
) -> tuple[int, int, int]:
    """Return exact bounds/count for one oldest metadata-only coverage batch.

    This is the plaintext-free sibling of ``chat_messages_after_seq`` used by
    deterministic V2 coverage accounting.  Its eligible-row predicate is
    deliberately identical to ``_read_compaction_tail_after_seq``: GC-able
    ``verify_ping`` and ``resident_maintenance`` rows can never enter an
    immutable coverage claim.
    """

    cursor_seq = int(after_seq)
    bounded = int(limit)
    upper_seq = int(through_seq) if through_seq is not None else None
    if cursor_seq < 0:
        raise ValueError("after_seq must be >= 0")
    if bounded <= 0:
        return 0, 0, 0
    if upper_seq is not None and upper_seq < 0:
        raise ValueError("through_seq must be >= 0")
    if upper_seq is not None and upper_seq <= cursor_seq:
        return 0, 0, 0

    predicate = (
        "WHERE user_id=%s AND seq>%s "
        f"AND {_CHAT_COVERAGE_SOURCE_PREDICATE} "
    )
    params: list = [str(user_id), cursor_seq]
    if upper_seq is not None:
        predicate += "AND seq<=%s "
        params.append(upper_seq)
    with get_pool().connection() as conn:
        row = conn.execute(
            "WITH selected AS ("
            " SELECT seq FROM chat_messages "
            + predicate
            + "ORDER BY seq ASC LIMIT %s"
            ") SELECT COALESCE(MIN(seq),0),COALESCE(MAX(seq),0),COUNT(*) "
            "FROM selected",
            (*params, bounded),
        ).fetchone()
    if not row:
        return 0, 0, 0
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def chat_recent_turn_rows(
    user_id: str,
    *,
    max_turns: int,
    row_cap: int,
    through_seq: int | None = None,
) -> dict:
    """Newest bounded rows anchored at genuine user turn seeds.

    ``max_turns`` bounds the seed search while ``row_cap`` bounds encrypted row
    disclosure. The newest-row scan may begin inside the oldest selected turn;
    callers must drop that partial prefix before rendering. ``window_rows``
    reports the pre-cap row count without exposing content.
    """
    bounded_turns = max(1, min(int(max_turns), 1000))
    bounded_rows = max(1, min(int(row_cap), 10_000))
    upper = int(through_seq) if through_seq is not None else chat_max_seq(user_id)
    if upper < 0:
        raise ValueError("through_seq must be >= 0")
    with get_pool().connection() as conn:
        rows = conn.execute(
            "WITH seeds AS ("
            " SELECT seq FROM chat_messages "
            " WHERE user_id=%s AND seq<=%s "
            " AND doc->>'role' IN ('user','human') "
            " AND COALESCE(doc->>'source','') "
            " NOT IN ('verify_ping','resident_maintenance') "
            " ORDER BY seq DESC LIMIT %s"
            "), boundary AS (SELECT MIN(seq) AS first_seq FROM seeds), "
            "windowed AS ("
            " SELECT cm.seq,cm.msg_id,cm.ts,cm.doc,count(*) OVER() AS window_rows "
            " FROM chat_messages cm CROSS JOIN boundary b "
            " WHERE cm.user_id=%s AND b.first_seq IS NOT NULL "
            " AND cm.seq>=b.first_seq AND cm.seq<=%s"
            ") SELECT seq,msg_id,ts,doc,window_rows FROM windowed "
            "ORDER BY seq DESC LIMIT %s",
            (
                str(user_id),
                upper,
                bounded_turns,
                str(user_id),
                upper,
                bounded_rows,
            ),
        ).fetchall()
    chronological = list(reversed(rows))
    window_rows = int(rows[0][4]) if rows else 0
    return {
        "rows": [
            {
                **dict(row[3] or {}),
                "id": str(row[1]),
                "ts": float(row[2]),
                "seq": int(row[0]),
            }
            for row in chronological
        ],
        "requested_turns": bounded_turns,
        "window_rows": window_rows,
        "source_truncated": window_rows > len(rows),
    }


def chat_capture_messages_after_seq(
    user_id: str,
    after_seq: int,
    *,
    sources: list[str] | tuple[str, ...],
    limit: int,
) -> list[dict]:
    """Return newest bounded Capture-eligible metadata after an exact seq.

    Eligibility is filtered in SQL *before* LIMIT. Filtering a newest raw
    window in Python can permanently hide an older uncaptured live row behind
    a long run of synthetic/import records.
    """
    cursor_seq = int(after_seq)
    bounded = max(1, min(int(limit), 1000))
    allowed = [str(source) for source in sources if str(source)]
    if cursor_seq < 0:
        raise ValueError("after_seq must be >= 0")
    if not allowed:
        return []
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT seq,msg_id,ts,doc FROM ("
            " SELECT seq,msg_id,ts,doc FROM chat_messages "
            " WHERE user_id=%s AND seq>%s "
            " AND doc->>'role' IN ('user','openclaw') "
            " AND COALESCE(doc->>'source','')=ANY(%s::text[]) "
            " ORDER BY seq DESC LIMIT %s"
            ") newest_live ORDER BY seq ASC",
            (str(user_id), cursor_seq, allowed, bounded),
        ).fetchall()
    return [
        {
            **dict(row[3] or {}),
            "id": str(row[1]),
            "ts": float(row[2]),
            "seq": int(row[0]),
        }
        for row in rows
    ]


def chat_seqs_after_seq(
    user_id: str,
    after_seq: int,
    *,
    limit: int | None = None,
    oldest_first: bool = True,
    through_seq: int | None = None,
) -> list[int]:
    """Return only the exact seq identities for the same window as
    :func:`chat_messages_after_seq`.

    This metadata-only form lets the V2 prompt invariant compare expected DB
    membership with the decrypted prompt tail without decrypting rows twice.
    """
    cursor_seq = int(after_seq)
    if cursor_seq < 0:
        raise ValueError("after_seq must be >= 0")
    upper_seq = int(through_seq) if through_seq is not None else None
    if upper_seq is not None and upper_seq < 0:
        raise ValueError("through_seq must be >= 0")
    if upper_seq is not None and upper_seq <= cursor_seq:
        return []
    if limit is not None and int(limit) <= 0:
        return []
    predicate = "WHERE user_id = %s AND seq > %s"
    params: list = [user_id, cursor_seq]
    if upper_seq is not None:
        predicate += " AND seq <= %s"
        params.append(upper_seq)
    with get_pool().connection() as conn:
        if limit is None:
            rows = conn.execute(
                f"SELECT seq FROM chat_messages {predicate} ORDER BY seq ASC",
                tuple(params),
            ).fetchall()
        elif oldest_first:
            rows = conn.execute(
                f"SELECT seq FROM chat_messages {predicate} "
                "ORDER BY seq ASC LIMIT %s",
                (*params, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT seq FROM ("
                f"  SELECT seq FROM chat_messages {predicate} "
                "  ORDER BY seq DESC LIMIT %s"
                ") newest ORDER BY seq ASC",
                (*params, int(limit)),
            ).fetchall()
    return [int(r[0]) for r in rows]


def count_messages_after_seq(
    user_id: str,
    after_seq: int,
    *,
    through_seq: int | None = None,
    exclude_synthetic_sources: bool = False,
) -> int:
    """COUNT of THIS USER's own ``chat_messages`` rows with ``seq > after_seq``
    — scoped by ``user_id``, unlike a bare ``chat_max_seq(...) - after_seq``
    seq-arithmetic estimate. ``chat_messages.seq`` is a TABLE-WIDE ``BIGINT
    GENERATED ALWAYS AS IDENTITY`` counter shared by every user (see
    :func:`chat_max_seq` / migration 0001_baseline.py): once other users'
    inserts interleave with this user's, the raw seq SPAN since a watermark
    (``max_seq - after_seq``) has no fixed relationship to how many of this
    user's OWN rows actually fall in that span — it can vastly overcount
    (mostly other users' rows) while the true per-user count stays small.
    Used by V2 worker's D6 prompt-coverage gap detection
    (``worker._prompt_coverage_gap``), which must compare against
    ``tail_limit`` using a real per-user count, not a global-seq guess — a
    one-shot ``COUNT(*)`` on the existing ``(user_id, seq)`` index, exactly
    as cheap as :func:`chat_max_seq`."""
    upper = int(through_seq) if through_seq is not None else None
    if upper is not None and upper <= int(after_seq):
        return 0
    predicate = "WHERE user_id = %s AND seq > %s"
    params: list = [user_id, after_seq]
    if upper is not None:
        predicate += " AND seq <= %s"
        params.append(upper)
    if exclude_synthetic_sources:
        # See chat_messages_after_seq: coverage gap detection must not count
        # GC-able synthetic rows, or it would demand folding a row that
        # verify_loop is about to delete (permanent frontier corruption).
        predicate += f" AND {_CHAT_COVERAGE_SOURCE_PREDICATE}"
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM chat_messages {predicate}",
            tuple(params),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def chat_recent_genuine_turn_boundary_seq(
    user_id: str,
    *,
    max_turns: int,
    through_seq: int,
) -> int | None:
    """Oldest seed seq among the newest ``max_turns`` genuine user turns."""
    bounded = max(1, min(int(max_turns), 1000))
    upper = int(through_seq)
    if upper < 0:
        raise ValueError("through_seq must be >= 0")
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT MIN(seq) FROM ("
            " SELECT seq FROM chat_messages "
            " WHERE user_id=%s AND seq<=%s "
            " AND doc->>'role' IN ('user','human') "
            " AND COALESCE(doc->>'source','') "
            " NOT IN ('verify_ping','resident_maintenance') "
            " ORDER BY seq DESC LIMIT %s"
            ") recent_seeds",
            (str(user_id), upper, bounded),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def chat_genuine_turn_count_after_seq(
    user_id: str,
    *,
    after_seq: int,
    through_seq: int,
) -> int:
    """Genuine user turns strictly after ``after_seq`` up to ``through_seq``.

    Same predicate as :func:`chat_recent_genuine_turn_boundary_seq` so the
    optional-window anchor's hysteresis counts exactly the rows that define
    its window.
    """
    lower = int(after_seq)
    upper = int(through_seq)
    if lower < 0 or upper < 0:
        raise ValueError("seq bounds must be >= 0")
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chat_messages "
            "WHERE user_id=%s AND seq>%s AND seq<=%s "
            "AND doc->>'role' IN ('user','human') "
            "AND COALESCE(doc->>'source','') "
            "NOT IN ('verify_ping','resident_maintenance')",
            (str(user_id), lower, upper),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def v2_effective_batch_cap(user_id: str) -> int | None:
    """The fold batch size this conversation was last observed to digest.

    ``None`` means never measured — callers fall back to their configured
    default, so existing rows and brand-new users behave exactly as before.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT effective_batch_cap FROM v2_conversation_summary "
            "WHERE user_id = %s",
            (str(user_id),),
        ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def v2_set_effective_batch_cap(user_id: str, value: int) -> None:
    """Persist the working fold batch size (floored at 1).

    Writes ONLY this column, and ONLY into a row that already exists.

    Both restrictions are load-bearing. The watermark and its CAS ``version``
    are fold coverage and must never move as a side effect of bookkeeping.
    And inserting a row here would fabricate a ``version = 0`` summary for a
    conversation that has never been folded — the fold then reads "no summary",
    computes its write against that absence, and its CAS collides with the row
    this bookkeeping call invented, failing the whole job with
    ``summary_cas_lost``.

    A conversation with no summary row therefore silently keeps no memory. It
    gets one as soon as its first fold lands, which is also the first moment
    the memory could be worth anything.

    Zero or negative is floored rather than stored: a batch of zero would wedge
    the fold on an empty slice forever.
    """
    capped = max(1, int(value))
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_conversation_summary SET effective_batch_cap = %s "
            "WHERE user_id = %s",
            (capped, str(user_id)),
        )


def chat_seq_for_msg_id(user_id: str, msg_id: str) -> int | None:
    """Exact ``seq`` for one message (its real primary-key identity), or
    ``None`` if the user has no such row. Used by V2 compaction (worker.py
    ``_run_compaction``) to attach the precise seq of the last-folded tail
    row to the summary's new watermark — a direct-by-id lookup rather than a
    ts-range query so it is exact even under same-``ts`` ties (see
    :func:`chat_max_seq`)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id = %s AND msg_id = %s",
            (user_id, msg_id),
        ).fetchone()
    return int(row[0]) if row is not None else None


def seq_for_watermark_ts(user_id: str, watermark_ts: float) -> int:
    """Conservative one-time ts->seq translation for a LEGACY summary
    watermark that only carries ``watermark_ts`` (``v2_conversation_summary
    .watermark_seq`` still at its migration default of 0 — see migration
    0031). Strictly-less (``ts < watermark_ts``), never ``<=``: a summary's
    ``watermark_ts`` marks how far compaction has folded, but with only a
    ts we cannot tell whether a row exactly AT that ts was itself folded in
    (same-ts ties — see :func:`chat_max_seq`), so this under-approximates
    the covered seq range on purpose. That is the same conservative
    direction the GC coverage gate/retention boundary already takes (never
    treat a possibly-uncovered row as covered). Returns 0 (covers nothing)
    when there is no row strictly before ``watermark_ts``."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM chat_messages "
            "WHERE user_id = %s AND ts < %s",
            (user_id, watermark_ts),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


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


def _chat_body_cas_params(doc: dict) -> tuple:
    """JSONB parameters that exactly identify one inline crypto envelope."""
    return tuple(
        Jsonb(doc[field]) if field in doc else None
        for field in _CHAT_BODY_CAS_FIELDS
    )


def _normalize_chat_body_doc(doc: dict) -> dict:
    """Return the canonical write shape for one chat envelope.

    A document carrying a body is a new inline version even if a stale cached
    pointer accompanied it (content re-wrap does this).  Keeping that old key on
    the inline row makes later replay ambiguous, so strip it before persistence.
    Pointer-only documents are left untouched and handled by the replay-only
    branch in :func:`_chat_insert_on_cursor`.
    """
    out = dict(doc or {})
    if out.get("body_ct") is not None:
        out.pop("body_key", None)
        out.pop("body_ct_len", None)
    return out


_chat_outer_fence_users: ContextVar[frozenset[str]] = ContextVar(
    "chat_outer_fence_users",
    default=frozenset(),
)

# One Memory Garden mutation may span a read, arbitrary in-process mutation,
# and a full-set reconcile. The database transaction and advisory lock must
# therefore outlive the individual ``memory_load``/``memory_replace_all`` calls;
# taking the lock only inside the final replace leaves a stale snapshot free to
# delete a Capture card committed in between. The value is
# ``user_id -> (connection, post_commit_callbacks)``. Nested memory helpers
# reuse the outer connection instead of acquiring the same lock on another
# session or consuming another pool slot.
_memory_mutation_contexts: ContextVar[dict[str, tuple[object, list]]] = ContextVar(
    "memory_mutation_contexts",
    default={},
)


@contextmanager
def _chat_user_fence_held_by_outer_transaction(user_id: str):
    """Scope nested chat writes under an already-held shared deletion fence.

    The V2 effect applier owns a shared advisory lock on its outer connection
    while a synchronous sink commits through a second connection.  If an
    account deletion has already queued for the exclusive form, PostgreSQL's
    fair lock queue puts a second shared acquisition behind that waiter and the
    application deadlocks (outer waits for nested; delete waits for outer).

    Enter this scope only *after* the outer transaction acquired the shared
    lock.  Same-user nested transactions may then omit their redundant shared
    acquisition: the outer lock remains the deletion barrier until dispatch
    returns.  Exclusive acquisitions are never skipped.
    """
    normalized = str(user_id)
    token = _chat_outer_fence_users.set(
        _chat_outer_fence_users.get() | frozenset({normalized})
    )
    try:
        yield
    finally:
        _chat_outer_fence_users.reset(token)


def _lock_chat_user_fence_on_cursor(
    cur,
    user_id: str,
    *,
    exclusive: bool = False,
) -> None:
    """Fence account deletion against nested per-user chat transactions.

    Ordinary operations take a shared transaction lock. This deliberately lets
    an effect-outbox transaction and its nested sink connection coexist. Account
    deletion takes the exclusive form before touching any child/parent row, so it
    either precedes the whole nested dispatch or waits until every shared holder
    commits—without parent/child/lifecycle lock-order cycles.
    """
    if not exclusive and str(user_id) in _chat_outer_fence_users.get():
        return
    lock_fn = (
        "pg_advisory_xact_lock"
        if exclusive
        else "pg_advisory_xact_lock_shared"
    )
    cur.execute(
        f"SELECT {lock_fn}(hashtextextended('chat-user-fence:' || %s, 0))",
        (user_id,),
    )


def _lock_capture_consent_on_cursor(cur, user_id: str) -> None:
    """Serialize proactive Capture consent changes with the effect commit."""
    cur.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('capture-consent:' || %s, 0))",
        (str(user_id),),
    )


def _lock_memory_user_mutation_on_cursor(cur, user_id: str) -> None:
    """Serialize every primary Memory Garden mutation for one user.

    Global acquisition order is chat-user fence, then this memory fence, then
    any narrower consent/row locks. Keeping the established chat fence first
    matters when a V2 effect dispatch already owns it on an outer connection:
    reversing the two can deadlock behind a queued exclusive account deletion.
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('memory-user-mutation:' || %s, 0))",
        (str(user_id),),
    )


def _memory_mutation_context(user_id: str):
    return _memory_mutation_contexts.get().get(str(user_id))


def _defer_memory_post_commit(user_id: str, callback) -> None:
    context = _memory_mutation_context(user_id)
    if context is None:
        callback()
        return
    context[1].append(callback)


@contextmanager
def memory_user_mutation_fence(user_id: str):
    """Hold one cross-process mutation transaction across load→mutate→save.

    This is reentrant within the current context. All memory DB helpers detect
    the active connection and use it directly, so one logical mutation uses a
    single pool slot and its TEE propagation runs only after primary commit.
    """
    normalized = str(user_id)
    if _memory_mutation_context(normalized) is not None:
        yield
        return

    callbacks: list = []
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(cur, normalized)
                _lock_memory_user_mutation_on_cursor(cur, normalized)
            current = dict(_memory_mutation_contexts.get())
            current[normalized] = (conn, callbacks)
            token = _memory_mutation_contexts.set(current)
            try:
                yield
            finally:
                _memory_mutation_contexts.reset(token)

    # The primary transaction is durably committed before any best-effort
    # shadow work can observe/requeue its rows. A mirror failure must not turn a
    # committed user mutation into an apparent request failure.
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 — TEE shadow is best-effort
            log.warning(
                "[db] memory post-commit mirror deferred user=%s code=%s",
                normalized,
                type(exc).__name__.lower(),
            )


def _lock_chat_r2_lifecycle_on_cursor(cur, user_id: str) -> int:
    """Materialize and lock one user's durable chat-object generation."""
    _lock_chat_user_fence_on_cursor(cur, user_id)
    cur.execute(
        "INSERT INTO chat_r2_lifecycle (user_id) VALUES (%s) "
        "ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    cur.execute(
        "SELECT generation FROM chat_r2_lifecycle "
        "WHERE user_id=%s FOR UPDATE",
        (user_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("chat R2 lifecycle row disappeared")
    value = row["generation"] if isinstance(row, dict) else row[0]
    return int(value)


def _mark_chat_r2_inventory_pending_on_cursor(
    cur,
    user_id: str,
    *,
    advance_generation: bool,
) -> int:
    """Durably request a generation inventory while holding its row fence.

    Account deletion advances the generation in the same transaction that
    removes retained chat rows. The marker deliberately survives deletion of
    ``users``; the isolated R2 worker can therefore retry a failed LIST after
    the account and all ordinary request traffic are gone. Clear Chat does not
    call this helper because it archives ciphertext instead of retiring it.
    """
    generation = _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
    next_generation = generation + 1 if advance_generation else generation
    cur.execute(
        "UPDATE chat_r2_lifecycle SET "
        " generation=%s, inventory_pending=TRUE, "
        " inventory_next_attempt_at=now(), inventory_attempt_count=0, "
        " inventory_last_error='', updated_at=now() "
        "WHERE user_id=%s",
        (next_generation, user_id),
    )
    return next_generation


def _chat_body_referenced_on_cursor(cur, user_id: str, key: str) -> bool:
    cur.execute(
        "SELECT 1 FROM ("
        " SELECT doc FROM chat_messages WHERE user_id=%s "
        " UNION ALL "
        " SELECT doc FROM chat_message_archive WHERE user_id=%s"
        ") AS retained WHERE doc->>'body_key'=%s "
        "AND (NOT (doc ? 'body_ct') "
        "     OR doc->'body_ct' = 'null'::jsonb) "
        "LIMIT 1",
        (user_id, user_id, key),
    )
    return cur.fetchone() is not None


def _enqueue_chat_r2_cleanup_on_cursor(
    cur,
    user_id: str,
    key: str,
    generation: int | None,
    reason: str,
) -> None:
    """Persist one idempotent object-deletion intent on the caller's tx."""
    if not key:
        return
    cur.execute(
        "INSERT INTO chat_r2_cleanup "
        "(body_key,user_id,generation,reason) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (body_key) DO UPDATE SET "
        " user_id=EXCLUDED.user_id, generation=EXCLUDED.generation, "
        " reason=EXCLUDED.reason, attempt_count=0, last_attempt_at=NULL, "
        " next_attempt_at=now(), last_error=''",
        (key, user_id, generation, reason),
    )


def _defer_chat_r2_cleanup_on_cursor(cur, user_id: str, key: str, error: str) -> None:
    """Back off one failed object tombstone so poison rows cannot starve FIFO."""
    cur.execute(
        "UPDATE chat_r2_cleanup SET "
        " attempt_count=attempt_count+1, last_attempt_at=now(), "
        " next_attempt_at=now() + ("
        "   LEAST(3600, 5 * (1 << LEAST(attempt_count, 9)))"
        "   * interval '1 second'"
        " ), last_error=%s "
        "WHERE body_key=%s AND user_id=%s",
        (str(error or "R2 delete failed")[:1000], key, user_id),
    )


def _reconcile_one_chat_r2_cleanup(user_id: str, key: str) -> bool:
    """Delete one queued private key without holding a DB row lock over R2 I/O.

    The per-key advisory lock spans both short reference-check transactions and
    the DELETE. Legitimate promotion takes that same key lock; pointer-only input
    can only replay an already-live exact pointer, which the first reference
    check detects. Keys are private/immutable per upload, so no writer can create
    a new reference to an unreferenced retired key while the network call runs.
    """
    try:
        with get_pool().connection() as conn:
            acquired_row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (key,),
            ).fetchone()
            acquired = bool(acquired_row and acquired_row[0])
            if not acquired:
                return False
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                        cur.execute(
                            "SELECT 1 FROM chat_r2_cleanup "
                            "WHERE body_key=%s AND user_id=%s "
                            "AND next_attempt_at<=now() FOR UPDATE",
                            (key, user_id),
                        )
                        if cur.fetchone() is None:
                            return False
                        if _chat_body_referenced_on_cursor(cur, user_id, key):
                            # A live pointer is authoritative. A stale/duplicate
                            # cleanup intent must never delete through it.
                            cur.execute(
                                "DELETE FROM chat_r2_cleanup "
                                "WHERE body_key=%s AND user_id=%s",
                                (key, user_id),
                            )
                            return True
                # Never hold the per-user lifecycle row lock or a transaction
                # across object-store latency. The private-key advisory lock is
                # the cross-process serialization boundary for this object.
                deleted = object_storage.delete_chat_body(key, user_id)
                with conn.transaction():
                    with conn.cursor() as cur:
                        _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                        cur.execute(
                            "SELECT 1 FROM chat_r2_cleanup "
                            "WHERE body_key=%s AND user_id=%s FOR UPDATE",
                            (key, user_id),
                        )
                        if cur.fetchone() is None:
                            return False
                        if _chat_body_referenced_on_cursor(cur, user_id, key):
                            # This should only be the harmless live-reference
                            # case found in phase one. Keep the row if an object
                            # was unexpectedly deleted so operators see/retry the
                            # invariant violation rather than hiding data loss.
                            if deleted:
                                _defer_chat_r2_cleanup_on_cursor(
                                    cur,
                                    user_id,
                                    key,
                                    "reference appeared during private-key delete",
                                )
                                log.error(
                                    "[db] chat R2 key became referenced during "
                                    "delete (%s,%s)",
                                    user_id,
                                    key,
                                )
                                return False
                            cur.execute(
                                "DELETE FROM chat_r2_cleanup "
                                "WHERE body_key=%s AND user_id=%s",
                                (key, user_id),
                            )
                            return True
                        if deleted:
                            cur.execute(
                                "DELETE FROM chat_r2_cleanup "
                                "WHERE body_key=%s AND user_id=%s",
                                (key, user_id),
                            )
                            return True
                        _defer_chat_r2_cleanup_on_cursor(
                            cur, user_id, key, "R2 delete failed",
                        )
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (key,),
                )
    except Exception as exc:  # noqa: BLE001
        # A DELETE followed by an ambiguous DB commit is safe: S3 deletion is
        # idempotent and the durable row, if it survived, is retried.
        log.error("[db] chat R2 cleanup(%s,%s) failed: %s", user_id, key, exc)
        try:
            with get_pool().connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        _defer_chat_r2_cleanup_on_cursor(
                            cur, user_id, key, str(exc),
                        )
        except Exception as defer_exc:  # noqa: BLE001
            log.error(
                "[db] defer chat R2 cleanup(%s,%s) failed: %s",
                user_id,
                key,
                defer_exc,
            )
    return False


def _defer_chat_r2_inventory(
    user_id: str,
    expected_generation: int,
    error: str,
) -> None:
    """Back off one failed LIST without losing a post-deletion inventory marker."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE chat_r2_lifecycle SET "
                " inventory_attempt_count=inventory_attempt_count+1, "
                " inventory_next_attempt_at=now() + ("
                "   LEAST(3600, 5 * (1 << LEAST(inventory_attempt_count, 9)))"
                "   * interval '1 second'"
                " ), inventory_last_error=%s, updated_at=now() "
                "WHERE user_id=%s AND generation=%s AND inventory_pending",
                (str(error or "R2 inventory failed")[:1000], user_id, expected_generation),
            )
    except Exception as exc:  # noqa: BLE001
        log.error("[db] defer chat R2 inventory(%s) failed: %s", user_id, exc)


def _reconcile_one_chat_r2_inventory(
    user_id: str,
    expected_generation: int,
) -> bool:
    """Inventory one retired generation under a cross-process session lock.

    LIST happens outside a database transaction. The generation is checked again
    under the lifecycle row lock before any keys are queued or the durable marker
    is cleared, so a concurrent clear cannot be acknowledged by an older scan.
    """
    lock_name = f"chat-r2-inventory:{user_id}"
    try:
        with get_pool().connection() as conn:
            acquired_row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (lock_name,),
            ).fetchone()
            acquired = bool(acquired_row and acquired_row[0])
            if not acquired:
                return False
            try:
                try:
                    keys = object_storage.list_user_chat_body_keys(user_id)
                except Exception as exc:  # noqa: BLE001
                    _defer_chat_r2_inventory(
                        user_id, expected_generation, str(exc),
                    )
                    log.error("[db] chat R2 inventory(%s) failed: %s", user_id, exc)
                    return False
                with conn.transaction():
                    with conn.cursor() as cur:
                        current_generation = _lock_chat_r2_lifecycle_on_cursor(
                            cur, user_id,
                        )
                        cur.execute(
                            "SELECT inventory_pending FROM chat_r2_lifecycle "
                            "WHERE user_id=%s",
                            (user_id,),
                        )
                        pending_row = cur.fetchone()
                        pending = bool(
                            pending_row
                            and (
                                pending_row["inventory_pending"]
                                if isinstance(pending_row, dict)
                                else pending_row[0]
                            )
                        )
                        if (
                            not pending
                            or current_generation != int(expected_generation)
                        ):
                            return False
                        for key in keys:
                            key_generation = (
                                object_storage.chat_body_storage_generation(
                                    key, user_id,
                                )
                            )
                            retired = (
                                key_generation is not None
                                and key_generation < current_generation
                            )
                            # Once clear/account delete advanced the generation,
                            # legacy keys can only be retained by a live legacy
                            # pointer. New writers never create legacy keys.
                            retired_legacy = (
                                key_generation is None and current_generation > 0
                            )
                            if retired or retired_legacy:
                                _enqueue_chat_r2_cleanup_on_cursor(
                                    cur,
                                    user_id,
                                    key,
                                    key_generation,
                                    "retired_generation_inventory",
                                )
                        cur.execute(
                            "UPDATE chat_r2_lifecycle SET "
                            " inventory_pending=FALSE, "
                            " inventory_next_attempt_at=now(), "
                            " inventory_attempt_count=0, inventory_last_error='', "
                            " updated_at=now() "
                            "WHERE user_id=%s AND generation=%s",
                            (user_id, expected_generation),
                        )
                return True
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (lock_name,),
                )
    except Exception as exc:  # noqa: BLE001
        _defer_chat_r2_inventory(user_id, expected_generation, str(exc))
        log.error("[db] reconcile chat R2 inventory(%s) failed: %s", user_id, exc)
        return False


def reconcile_chat_r2_cleanup(
    user_id: str | None = None,
    *,
    limit: int = 25,
    include_inventory: bool = True,
    inventory_limit: int = 1,
) -> int:
    """Best-effort durable cleanup worker; safe under retries and concurrency.

    ``chat_r2_cleanup`` is the correctness boundary. A crash/ambiguous commit
    leaves rows for the next pass; retry scheduling prevents a poison FIFO head.
    Inventory is driven by durable lifecycle markers, including markers left by
    deleted accounts. This function must run only in the isolated cleanup loop;
    correctness-critical request/reply paths merely commit outbox rows.
    """
    if not object_storage.chat_files_enabled() or int(limit) <= 0:
        return 0
    if include_inventory and int(inventory_limit) > 0:
        inventory_sql = (
            "SELECT user_id,generation FROM chat_r2_lifecycle "
            "WHERE inventory_pending "
            "AND inventory_next_attempt_at <= now() "
            + ("AND user_id=%s " if user_id is not None else "")
            + "ORDER BY inventory_next_attempt_at,updated_at,user_id "
            "LIMIT %s"
        )
        inventory_params = (
            (user_id, int(inventory_limit))
            if user_id is not None
            else (int(inventory_limit),)
        )
        try:
            with get_pool().connection() as conn:
                inventory_rows = conn.execute(
                    inventory_sql, inventory_params,
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.error("[db] list chat R2 inventory work failed: %s", exc)
            inventory_rows = []
        for inventory_row in inventory_rows:
            _reconcile_one_chat_r2_inventory(
                str(inventory_row[0]), int(inventory_row[1]),
            )
    sql = (
        "SELECT user_id,body_key FROM chat_r2_cleanup "
        "WHERE next_attempt_at <= now() "
        + ("AND user_id=%s " if user_id is not None else "")
        + "ORDER BY next_attempt_at,created_at,body_key LIMIT %s"
    )
    params = (user_id, int(limit)) if user_id is not None else (int(limit),)
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.error("[db] list chat R2 cleanup failed: %s", exc)
        return 0
    completed = 0
    for row in rows:
        uid = str(row[0])
        key = str(row[1])
        if _reconcile_one_chat_r2_cleanup(uid, key):
            completed += 1
    return completed


def _offload_chat_body_after_commit(
    user_id: str,
    msg_id: str,
    doc: dict,
    expected_generation: int,
) -> None:
    """Best-effort heavy-body offload for an already-committed inline row.

    Before upload, a deletion tombstone is committed for the private key. A
    per-object PostgreSQL advisory lock then spans final validation, R2 PUT, and
    promotion. Cleanup takes the same lock, while the short validation/promotion
    transactions take the per-user generation fence. A committed promotion
    removes the tombstone atomically; every rollback, CAS loss, process death,
    or commit ambiguity leaves it for reconciliation. This closes the otherwise
    unavoidable PUT-to-DB crash gap without holding a user-wide row lock during
    network I/O.
    """
    if not (
        object_storage.chat_files_enabled()
        and isinstance(doc, dict)
        and doc.get("content_type") in _R2_OFFLOAD_CONTENT_TYPES
        and doc.get("body_ct") is not None
    ):
        return
    normalized_doc = _normalize_chat_body_doc(doc)
    content_type = str(normalized_doc.get("content_type") or "file")
    upload_version = object_storage.new_chat_body_upload_version()
    key = object_storage.chat_body_key(
        user_id,
        msg_id,
        content_type,
        upload_version=upload_version,
        storage_generation=expected_generation,
    )
    promoted = False
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    current_generation = _lock_chat_r2_lifecycle_on_cursor(
                        cur, user_id,
                    )
                    if current_generation != int(expected_generation):
                        return
                    cur.execute(
                        "SELECT 1 FROM chat_messages "
                        "WHERE user_id=%s AND msg_id=%s "
                        "  AND storage_generation=%s AND "
                        f"{_CHAT_BODY_CAS_PREDICATE} FOR UPDATE",
                        (
                            user_id,
                            msg_id,
                            expected_generation,
                            *_chat_body_cas_params(normalized_doc),
                        ),
                    )
                    if cur.fetchone() is None:
                        return
                    _enqueue_chat_r2_cleanup_on_cursor(
                        cur,
                        user_id,
                        key,
                        expected_generation,
                        "upload_guard",
                    )
        body_ct_len = len(normalized_doc["body_ct"])
        pointer = {"body_key": key, "body_ct_len": body_ct_len}
        with get_pool().connection() as conn:
            conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (key,),
            )
            try:
                upload_allowed = False
                with conn.transaction():
                    with conn.cursor() as cur:
                        current_generation = _lock_chat_r2_lifecycle_on_cursor(
                            cur, user_id,
                        )
                        cur.execute(
                            "SELECT 1 FROM chat_r2_cleanup "
                            "WHERE body_key=%s AND user_id=%s FOR UPDATE",
                            (key, user_id),
                        )
                        guard_exists = cur.fetchone() is not None
                        cur.execute(
                            "SELECT 1 FROM chat_messages "
                            "WHERE user_id=%s AND msg_id=%s "
                            "  AND storage_generation=%s AND "
                            f"{_CHAT_BODY_CAS_PREDICATE} FOR UPDATE",
                            (
                                user_id,
                                msg_id,
                                expected_generation,
                                *_chat_body_cas_params(normalized_doc),
                            ),
                        )
                        row_matches = cur.fetchone() is not None
                        upload_allowed = (
                            guard_exists
                            and row_matches
                            and current_generation == int(expected_generation)
                        )
                if upload_allowed:
                    object_storage.put_chat_body(
                        user_id,
                        msg_id,
                        normalized_doc["body_ct"],
                        content_type,
                        upload_version=upload_version,
                        storage_generation=expected_generation,
                    )
                    with conn.transaction():
                        with conn.cursor() as cur:
                            current_generation = _lock_chat_r2_lifecycle_on_cursor(
                                cur, user_id,
                            )
                            cur.execute(
                                "SELECT 1 FROM chat_r2_cleanup "
                                "WHERE body_key=%s AND user_id=%s FOR UPDATE",
                                (key, user_id),
                            )
                            guard_exists = cur.fetchone() is not None
                            if (
                                guard_exists
                                and current_generation == int(expected_generation)
                            ):
                                cur.execute(
                                    "UPDATE chat_messages "
                                    "SET doc = (doc - 'body_ct') || %s "
                                    "WHERE user_id=%s AND msg_id=%s "
                                    "  AND storage_generation=%s AND "
                                    f"{_CHAT_BODY_CAS_PREDICATE} RETURNING 1",
                                    (
                                        Jsonb(pointer),
                                        user_id,
                                        msg_id,
                                        expected_generation,
                                        *_chat_body_cas_params(normalized_doc),
                                    ),
                                )
                                promoted = cur.fetchone() is not None
                                if promoted:
                                    cur.execute(
                                        "DELETE FROM chat_r2_cleanup "
                                        "WHERE body_key=%s AND user_id=%s",
                                        (key, user_id),
                                    )
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (key,),
                )
    except Exception as e:  # noqa: BLE001
        log.error(
            "[db] chat body offload(%s,%s) failed, left inline: %s",
            user_id,
            msg_id,
            e,
        )
        # The upload guard was committed before PUT. Never perform network
        # cleanup on this write path: the isolated cleanup worker owns retries,
        # including ambiguous PUT/COMMIT outcomes.


def _chat_insert_on_cursor(
    cur, user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int,
    *, coverage_gated: bool = False,
) -> tuple[int, int]:
    """INSERT one durable chat message (ON CONFLICT upsert) on the caller's
    cursor/transaction. Returns
    ``(seq, storage_generation)``. Does NOT do R2 offload — the caller handles
    that post-commit with the exact generation pinned here.

    ``max_messages`` and ``coverage_gated`` remain in the
    signature only for source compatibility with legacy callers. They never
    authorize deletion. Conversation summaries and their watermarks are derived
    prompt indexes, not retention proofs; compaction must not destroy the raw
    encrypted transcript or an attached R2 body. Hot process memory is bounded
    independently by :func:`chat_load_recent_strict` and ``UserStore``.

    Row-factory agnostic: works whether ``cur`` is the default tuple cursor
    or a ``dict_row`` cursor (see :func:`chat_append_and_enqueue`, which opens
    a separate ``dict_row`` cursor on the same connection/transaction for the
    coalesce-or-insert job op)."""
    generation = _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
    stored_doc = _normalize_chat_body_doc(doc)
    cur.execute(
        "SELECT seq,doc,storage_generation FROM chat_messages "
        "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
        (user_id, msg_id),
    )
    existing = cur.fetchone()
    incoming_pointer = _is_chat_file_pointer(stored_doc)
    if incoming_pointer:
        incoming_key = str(stored_doc.get("body_key") or "")
        if not object_storage.chat_key_owned_by(incoming_key, user_id):
            raise ChatPointerReplayConflict("pointer key is not owned by user")
        if existing is None:
            raise ChatPointerReplayConflict(
                "pointer-only documents cannot create chat rows"
            )
        existing_doc = existing["doc"] if isinstance(existing, dict) else existing[1]
        existing_generation = int(
            existing["storage_generation"]
            if isinstance(existing, dict)
            else existing[2]
        )
        if (
            existing_generation != generation
            or not _is_chat_file_pointer(existing_doc)
            or str(existing_doc.get("body_key") or "") != incoming_key
        ):
            raise ChatPointerReplayConflict(
                "stale pointer does not match the current chat body"
            )
        # Exact same-key replay is a true no-op. In particular, do not replace
        # newer operational metadata with an older transport snapshot.
        seq = existing["seq"] if isinstance(existing, dict) else existing[0]
    elif existing is None:
        cur.execute(
            "INSERT INTO chat_messages "
            "(user_id,msg_id,ts,doc,storage_generation) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING seq",
            (user_id, msg_id, ts, Jsonb(stored_doc), generation),
        )
        inserted = cur.fetchone()
        seq = inserted["seq"] if isinstance(inserted, dict) else inserted[0]
    else:
        existing_generation = int(
            existing["storage_generation"]
            if isinstance(existing, dict)
            else existing[2]
        )
        if existing_generation != generation:
            raise RuntimeError("chat row belongs to a retired storage generation")
        cur.execute(
            "UPDATE chat_messages SET ts=%s,doc=%s,storage_generation=%s "
            "WHERE user_id=%s AND msg_id=%s RETURNING seq",
            (ts, Jsonb(stored_doc), generation, user_id, msg_id),
        )
        updated = cur.fetchone()
        seq = updated["seq"] if isinstance(updated, dict) else updated[0]
    return int(seq), generation


def _chat_append_impl(
    user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int,
    *, coverage_gated: bool = False,
) -> None:
    """Insert one durable chat message. Idempotent on msg_id. Raises on the
    primary database write (``chat_append_strict`` relies on this so a DB failure
    cannot be mistaken for delivery); R2 offload stays best-effort.

    ``max_messages`` and ``coverage_gated`` are accepted for compatibility but
    do not delete source rows. The per-process cache remains bounded separately.

    A heavy body_ct (``_R2_OFFLOAD_CONTENT_TYPES``: file, image) is offloaded to
    R2 when configured (``object_storage.chat_files_enabled()``); the row then
    keeps only the envelope metadata plus a ``body_key`` pointer, and
    ``chat_load`` reconstitutes ``body_ct`` from R2 transparently. Falls back to
    inline storage when R2 is unconfigured OR the upload fails. Crash-safe, same
    ordering as frame_upsert: the row is written inline (readable, no pointer)
    BEFORE the object exists and flipped to the pointer shape only AFTER the
    upload succeeds — a crash never leaves a pointer to a missing object."""
    with get_pool().connection() as conn:
        with conn.transaction():
            # 1) inline first — message readable, references no R2 object yet.
            with conn.cursor() as cur:
                _seq, storage_generation = _chat_insert_on_cursor(
                    cur, user_id, msg_id, ts, doc, max_messages,
                    coverage_gated=coverage_gated,
                )
    # 2/3) upload outside the transaction, then atomically flip the current row
    # to its pointer shape only after the object exists.
    _offload_chat_body_after_commit(
        user_id, msg_id, doc, storage_generation,
    )
    # Same-row replacement and explicit DELETE triggers commit exact retirement
    # intents. A normal append never queues an older row/body for retirement.
    # The isolated R2 worker drains legitimate replacement/delete intents; doing
    # so here would put object-store latency inside delivery transactions.
    # No retention cleanup follows an append. R2/TEE copies live for exactly as
    # long as the durable source row and are retired only by explicit user/account
    # deletion or replacement of that same row.


def chat_append_strict(
    user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int,
) -> None:
    """Persist one chat message or raise on the primary database write.

    V2 uses this path for model replies so a database failure cannot be mistaken
    for delivery. Optional R2 offload remains best-effort: the inline row is
    already durable before an upload is attempted.

    ``max_messages`` bounds only callers' hot caches; it never trims the durable
    source transcript.
    """
    _chat_append_impl(user_id, msg_id, ts, doc, max_messages, coverage_gated=True)


class ResidentReplyRejected(RuntimeError):
    """A resident reply lost the durable runtime/parent ownership race."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


def _same_reply_envelope(existing_doc, requested_doc) -> bool:
    existing_delivery_id = str(
        (existing_doc or {}).get("resident_delivery_id") or ""
    ) if isinstance(existing_doc, dict) else ""
    requested_delivery_id = str(
        (requested_doc or {}).get("resident_delivery_id") or ""
    ) if isinstance(requested_doc, dict) else ""
    if requested_delivery_id:
        # The resident consumer derives this id from the parent + stable bubble
        # ordinal and also uses it as the AEAD item id. Retries necessarily
        # rebuild fresh ciphertext, so compare the authenticated delivery key
        # and routing metadata rather than random nonce/ciphertext bytes.
        stable_delivery_fields = (
            "id", "role", "source", "visibility", "owner_user_id",
            "content_type", "reply_to_message_id", "resident_delivery_id",
        )
        return (
            existing_delivery_id == requested_delivery_id
            and all(
                existing_doc.get(field) == requested_doc.get(field)
                for field in stable_delivery_fields
            )
        )
    immutable_reply_fields = (
        "id", "role", "source", "v", "body_ct", "nonce",
        "K_user", "K_enclave", "enclave_pk_fpr", "visibility",
        "owner_user_id", "content_type", "reply_to_message_id",
    )
    if not isinstance(existing_doc, dict) or not isinstance(requested_doc, dict):
        return False
    if _is_chat_file_pointer(existing_doc) and requested_doc.get("body_ct") is not None:
        # Post-commit R2 offload intentionally replaces body_ct with a pointer.
        # A retry still has the original inline envelope, so compare every other
        # immutable crypto/routing field and use the persisted ciphertext length
        # to bridge the one deliberate shape change.
        pointer_fields = tuple(
            field for field in immutable_reply_fields if field != "body_ct"
        )
        return (
            all(
                existing_doc.get(field) == requested_doc.get(field)
                for field in pointer_fields
            )
            and existing_doc.get("body_ct_len") == len(requested_doc["body_ct"])
        )
    return all(
        existing_doc.get(field) == requested_doc.get(field)
        for field in immutable_reply_fields
    )


def chat_append_resident_reply(
    user_id: str,
    msg_id: str,
    ts: float,
    doc: dict,
    max_messages: int,
    *,
    parent_msg_id: str,
    replied_by: str,
) -> tuple[int, bool, dict, dict]:
    """Atomically commit a linked resident reply under the cutover fence.

    The runtime-state lock is the hand-off barrier shared with
    ``patch_blob_strict`` and the V2 reply sink.  Parent ownership, assistant
    insertion, and the answered marker then commit together, eliminating both
    the old read-cache race and the append/metadata crash window.

    Returns ``(reply_seq, inserted, persisted_parent_doc,
    persisted_reply_doc)``. Replaying the same envelope/delivery key against
    the same already-linked parent is idempotent and returns the row that
    actually won; a different reply, missing/invalid parent, or non-resident
    runtime fails closed.
    """
    parent_id = str(parent_msg_id or "").strip()
    if not parent_id:
        raise ValueError("parent_msg_id is required")
    reply_doc = _normalize_chat_body_doc(doc)
    if _is_chat_file_pointer(reply_doc) and not object_storage.chat_key_owned_by(
        str(reply_doc.get("body_key") or ""), user_id,
    ):
        raise ResidentReplyRejected("pointer_key_not_owned")
    reply_doc["reply_to_message_id"] = parent_id
    replied_fields = {
        "reply_status": "replied",
        "reply_message_id": msg_id,
        "replied_by": str(replied_by or ""),
        "replied_at": f"{float(ts):.3f}",
    }
    parent_changed = False

    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(cur, user_id)
                # Global order: runtime-state before any chat row. A cutover
                # already in progress wins; a reply already committing makes
                # cutover wait and then becomes visible to its cursor bridge.
                cur.execute(
                    "INSERT INTO v2_runtime_state (user_id) "
                    "SELECT %s WHERE EXISTS "
                    "(SELECT 1 FROM users WHERE user_id=%s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id),
                )
                cur.execute(
                    "SELECT hosted_runtime_state FROM v2_runtime_state "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                state_row = cur.fetchone()
                state = (
                    state_row["hosted_runtime_state"]
                    if isinstance(state_row, dict)
                    else state_row[0]
                ) if state_row else ""
                if state != "resident":
                    raise ResidentReplyRejected("runtime_not_resident")

                storage_generation = _lock_chat_r2_lifecycle_on_cursor(
                    cur, user_id,
                )

                cur.execute(
                    "SELECT doc FROM chat_messages "
                    "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                    (user_id, parent_id),
                )
                parent_row = cur.fetchone()
                if parent_row is None:
                    raise ResidentReplyRejected("reply_parent_not_found")
                parent_doc = (
                    parent_row["doc"]
                    if isinstance(parent_row, dict)
                    else parent_row[0]
                )
                if not isinstance(parent_doc, dict) or parent_doc.get("role") != "user":
                    raise ResidentReplyRejected("reply_parent_not_user")

                prior_reply_id = str(parent_doc.get("reply_message_id") or "")
                already_replied = (
                    parent_doc.get("reply_status") == "replied"
                    or bool(prior_reply_id)
                )
                if already_replied:
                    if prior_reply_id != msg_id:
                        raise ResidentReplyRejected("already_answered")
                    cur.execute(
                        "SELECT seq,doc,storage_generation FROM chat_messages "
                        "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                        (user_id, msg_id),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise ResidentReplyRejected("linked_reply_missing")
                    if isinstance(existing, dict):
                        seq = int(existing["seq"])
                        existing_doc = existing["doc"]
                    else:
                        seq = int(existing[0])
                        existing_doc = existing[1]
                    existing_storage_generation = int(
                        existing["storage_generation"]
                        if isinstance(existing, dict)
                        else existing[2]
                    )
                    if existing_storage_generation != storage_generation:
                        raise ResidentReplyRejected("reply_storage_generation_retired")
                    if not _same_reply_envelope(existing_doc, reply_doc):
                        raise ResidentReplyRejected("reply_id_collision")
                    inserted = False
                    persisted_reply_doc = existing_doc
                else:
                    if _is_chat_file_pointer(reply_doc):
                        raise ResidentReplyRejected("pointer_replay_missing_current_row")
                    cur.execute(
                        "INSERT INTO chat_messages "
                        "(user_id,msg_id,ts,doc,storage_generation) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (user_id,msg_id) DO NOTHING RETURNING seq",
                        (
                            user_id,
                            msg_id,
                            ts,
                            Jsonb(reply_doc),
                            storage_generation,
                        ),
                    )
                    inserted_row = cur.fetchone()
                    inserted = inserted_row is not None
                    if inserted:
                        seq = int(
                            inserted_row["seq"]
                            if isinstance(inserted_row, dict)
                            else inserted_row[0]
                        )
                        persisted_reply_doc = reply_doc
                    else:
                        cur.execute(
                            "SELECT seq,doc,storage_generation FROM chat_messages "
                            "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                            (user_id, msg_id),
                        )
                        existing = cur.fetchone()
                        if existing is None:
                            raise ResidentReplyRejected("reply_id_collision")
                        if isinstance(existing, dict):
                            seq = int(existing["seq"])
                            existing_doc = existing["doc"]
                        else:
                            seq = int(existing[0])
                            existing_doc = existing[1]
                        existing_storage_generation = int(
                            existing["storage_generation"]
                            if isinstance(existing, dict)
                            else existing[2]
                        )
                        if existing_storage_generation != storage_generation:
                            raise ResidentReplyRejected(
                                "reply_storage_generation_retired"
                            )
                        if not _same_reply_envelope(existing_doc, reply_doc):
                            raise ResidentReplyRejected("reply_id_collision")
                        persisted_reply_doc = existing_doc

                    cur.execute(
                        "UPDATE chat_messages SET doc=doc || %s "
                        "WHERE user_id=%s AND msg_id=%s "
                        "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                        "  AND COALESCE(doc->>'reply_message_id','')='' "
                        "RETURNING doc",
                        (Jsonb(replied_fields), user_id, parent_id),
                    )
                    updated_parent = cur.fetchone()
                    if updated_parent is None:
                        raise ResidentReplyRejected("already_answered")
                    parent_doc = (
                        updated_parent["doc"]
                        if isinstance(updated_parent, dict)
                        else updated_parent[0]
                    )
                    parent_changed = True

    _offload_chat_body_after_commit(
        user_id, msg_id, persisted_reply_doc, storage_generation,
    )
    # Both mirror operations are post-commit and best-effort. Chat inserts are
    # owned by tee_replicator; this direct mirror is only the in-place parent
    # mutation that its forward cursor would otherwise miss.
    if parent_changed:
        from tee_shadow import mirror

        mirror.execute(
            "UPDATE chat_messages SET doc=doc || %s "
            "WHERE user_id=%s AND msg_id=%s",
            (Jsonb(replied_fields), user_id, parent_id),
        )
    return seq, inserted, parent_doc, persisted_reply_doc


def chat_append_resident_message(
    user_id: str,
    msg_id: str,
    ts: float,
    doc: dict,
    max_messages: int,
) -> tuple[int, bool, dict]:
    """Commit an unlinked resident response under the runtime cutover fence.

    Proactive messages, verify replies, and infrastructure notices do not
    always have a parent user message, but they still must not leak out of a
    resident process after the user has moved to V2. Linked chat finals use the
    stronger :func:`chat_append_resident_reply` parent CAS instead.
    """
    message_doc = _normalize_chat_body_doc(doc)
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(cur, user_id)
                cur.execute(
                    "INSERT INTO v2_runtime_state (user_id) "
                    "SELECT %s WHERE EXISTS "
                    "(SELECT 1 FROM users WHERE user_id=%s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (user_id, user_id),
                )
                cur.execute(
                    "SELECT hosted_runtime_state FROM v2_runtime_state "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                state_row = cur.fetchone()
                state = (
                    state_row["hosted_runtime_state"]
                    if isinstance(state_row, dict)
                    else state_row[0]
                ) if state_row else ""
                if state != "resident":
                    raise ResidentReplyRejected("runtime_not_resident")

                storage_generation = _lock_chat_r2_lifecycle_on_cursor(
                    cur, user_id,
                )
                if _is_chat_file_pointer(message_doc):
                    if not object_storage.chat_key_owned_by(
                        str(message_doc.get("body_key") or ""), user_id,
                    ):
                        raise ResidentReplyRejected("pointer_key_not_owned")
                    cur.execute(
                        "SELECT seq,doc,storage_generation FROM chat_messages "
                        "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                        (user_id, msg_id),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise ResidentReplyRejected(
                            "pointer_replay_missing_current_row"
                        )
                    inserted = False
                else:
                    cur.execute(
                        "INSERT INTO chat_messages "
                        "(user_id,msg_id,ts,doc,storage_generation) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (user_id,msg_id) DO NOTHING "
                        "RETURNING seq",
                        (
                            user_id,
                            msg_id,
                            ts,
                            Jsonb(message_doc),
                            storage_generation,
                        ),
                    )
                    inserted_row = cur.fetchone()
                    inserted = inserted_row is not None
                    if inserted:
                        seq = int(
                            inserted_row["seq"]
                            if isinstance(inserted_row, dict)
                            else inserted_row[0]
                        )
                        persisted_doc = message_doc
                        existing = None
                    else:
                        cur.execute(
                            "SELECT seq,doc,storage_generation "
                            "FROM chat_messages "
                            "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                            (user_id, msg_id),
                        )
                        existing = cur.fetchone()
                if not inserted:
                    if existing is None:
                        raise ResidentReplyRejected("reply_id_collision")
                    if isinstance(existing, dict):
                        seq = int(existing["seq"])
                        existing_doc = existing["doc"]
                    else:
                        seq = int(existing[0])
                        existing_doc = existing[1]
                    existing_storage_generation = int(
                        existing["storage_generation"]
                        if isinstance(existing, dict)
                        else existing[2]
                    )
                    if existing_storage_generation != storage_generation:
                        raise ResidentReplyRejected(
                            "reply_storage_generation_retired"
                        )
                    if not _same_reply_envelope(existing_doc, message_doc):
                        raise ResidentReplyRejected("reply_id_collision")
                    persisted_doc = existing_doc

    _offload_chat_body_after_commit(
        user_id, msg_id, persisted_doc, storage_generation,
    )
    return seq, inserted, persisted_doc


def chat_settle_failed_input(
    user_id: str,
    msg_id: str,
    error_code: str,
) -> bool:
    """Mark one failed user turn settled and advance the V2 reply cursor.

    A terminal provider failure is still a completed settlement decision for
    ordering purposes. Leaving its user row beyond the durable cursor makes the
    next chat job answer that old row before the newly submitted message.
    """
    from model_api_runtime.v2 import cursor as v2_cursor

    message_id = str(msg_id or "").strip()
    if not message_id:
        return False
    persisted_runtime_doc: dict | None = None
    failed_fields = {
        "reply_status": "failed",
        "reply_failure_code": str(error_code or "turn_failed")[:200],
        "replied_by": "hosted_runtime_v2",
        "replied_at": f"{time.time():.3f}",
    }
    updated = False
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(cur, user_id)
                cur.execute(
                    "SELECT seq,doc FROM chat_messages "
                    "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                    (user_id, message_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                seq = int(row["seq"] if isinstance(row, dict) else row[0])
                doc = row["doc"] if isinstance(row, dict) else row[1]
                if not isinstance(doc, dict) or str(doc.get("role") or "") not in {
                    "user",
                    "human",
                }:
                    return False
                already_replied = (
                    str(doc.get("reply_status") or "") == "replied"
                    or bool(str(doc.get("reply_message_id") or ""))
                )
                if not already_replied:
                    cur.execute(
                        "UPDATE chat_messages SET doc=doc || %s "
                        "WHERE user_id=%s AND msg_id=%s",
                        (Jsonb(failed_fields), user_id, message_id),
                    )
                    updated = True
                persisted_runtime_doc = _advance_blob_int_on_cursor(
                    cur,
                    user_id,
                    "model_api_runtime",
                    v2_cursor.CURSOR_KEY,
                    seq,
                )

    if updated:
        from tee_shadow import mirror

        mirror.execute(
            "UPDATE chat_messages SET doc=doc || %s "
            "WHERE user_id=%s AND msg_id=%s",
            (Jsonb(failed_fields), user_id, message_id),
        )
    if persisted_runtime_doc is not None:
        _mirror_persisted_blob(
            user_id,
            "model_api_runtime",
            persisted_runtime_doc,
        )
    return True


def chat_append_effect_with_cursor(
    user_id: str,
    msg_id: str,
    ts: float,
    doc: dict,
    max_messages: int,
    reply_through_seq: int | None,
    *,
    connection=None,
    defer_post_commit: bool = False,
    require_cursor_advance: bool = False,
):
    """Atomically persist one deterministic V2 reply and optionally advance its cursor.

    ``msg_id`` is derived from the outbox effect id, making replay naturally
    idempotent.  A non-``None`` ``reply_through_seq`` is a terminal reply: its
    row, answered-parent markers, and ``v2_reply_cursor_seq`` commit together.
    ``None`` persists an intermediate reply without consuming any input.

    When ``connection`` is supplied, it is the outbox applier's existing
    connection.  The nested ``connection.transaction()`` below is only a
    savepoint, so reply/cursor/job/effect still have exactly one outer commit.
    ``defer_post_commit`` returns a thunk for R2/mirror work; callers must invoke
    it only after that outer commit.  The default wrapper behavior is unchanged.

    ``require_cursor_advance`` is reserved for delayed terminal-failure
    delivery. It suppresses the candidate atomically when a newer terminal
    reply already advanced the cursor through the captured failure frontier.

    Returns ``(seq, inserted)`` by default, or
    ``(seq, inserted, post_commit)`` when ``defer_post_commit`` is true. A
    replay returns the existing seq and still monotonically advances a terminal
    cursor, then skips duplicate cache/notification side effects at the Store
    layer.
    """
    from model_api_runtime.v2 import cursor as v2_cursor

    cursor_seq = (
        None if reply_through_seq is None else int(reply_through_seq)
    )
    if cursor_seq is not None and cursor_seq < 0:
        raise ValueError("reply_through_seq must be >= 0")
    if require_cursor_advance and cursor_seq is None:
        raise ValueError(
            "require_cursor_advance requires a terminal reply cursor"
        )

    effect_doc = _normalize_chat_body_doc(doc)
    replied_user_ids: list[str] = []
    persisted_runtime_doc: dict | None = None
    replied_fields = {
        "reply_status": "replied",
        "reply_message_id": msg_id,
        "replied_by": "hosted_runtime_v2",
        "replied_at": f"{float(ts):.3f}",
    }
    failure_error_class = str(
        effect_doc.get("turn_failure_error_class") or ""
    ).strip()
    if failure_error_class:
        replied_fields.update(
            {
                "reply_error_class": failure_error_class,
                "reply_blame": str(effect_doc.get("turn_failure_blame") or "").strip(),
                "reply_user_text": str(
                    effect_doc.get("turn_failure_user_text") or ""
                ).strip(),
            }
        )
    connection_scope = (
        nullcontext(connection)
        if connection is not None
        else get_pool().connection()
    )
    with connection_scope as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_user_fence_on_cursor(cur, user_id)
                # Lock/materialize the cursor row before deciding whether a
                # resident reply raced this V2 turn. If any newly-consumed user
                # input was answered after V2 assembled its prompt, abort before
                # inserting another assistant bubble. Conversely, once V2 wins
                # below it marks those parents replied in this same transaction,
                # so a late resident response is rejected by its existing CAS.
                previous_cursor = 0
                if cursor_seq is not None:
                    cur.execute(
                        "INSERT INTO user_blobs (user_id,kind,doc) "
                        "VALUES (%s,'model_api_runtime','{}'::jsonb) "
                        "ON CONFLICT (user_id,kind) DO NOTHING",
                        (user_id,),
                    )
                    cur.execute(
                        "SELECT doc FROM user_blobs "
                        "WHERE user_id=%s AND kind='model_api_runtime' FOR UPDATE",
                        (user_id,),
                    )
                    cursor_row = cur.fetchone()
                    cursor_doc = (
                        cursor_row["doc"]
                        if isinstance(cursor_row, dict)
                        else cursor_row[0]
                    ) if cursor_row else {}
                    raw_previous_cursor = str(
                        (cursor_doc or {}).get(v2_cursor.CURSOR_KEY, 0)
                    )
                    if not raw_previous_cursor.isdigit():
                        raise RuntimeError("invalid persisted V2 reply cursor")
                    previous_cursor = int(raw_previous_cursor)
                    if require_cursor_advance and cursor_seq <= previous_cursor:
                        if defer_post_commit:
                            return 0, False, lambda: None
                        return 0, False
                storage_generation = _lock_chat_r2_lifecycle_on_cursor(
                    cur, user_id,
                )
                if cursor_seq is not None:
                    cur.execute(
                        "SELECT msg_id FROM chat_messages "
                        "WHERE user_id=%s AND seq>%s AND seq<=%s "
                        "  AND doc->>'role' IN ('user','human') "
                        "  AND (doc->>'reply_status'='replied' "
                        "       OR COALESCE(doc->>'reply_message_id','') <> '') "
                        "LIMIT 1",
                        (user_id, previous_cursor, cursor_seq),
                    )
                    if cur.fetchone() is not None:
                        raise RuntimeError(
                            "V2 reply input was already answered by another runtime")

                if _is_chat_file_pointer(effect_doc):
                    if not object_storage.chat_key_owned_by(
                        str(effect_doc.get("body_key") or ""), user_id,
                    ):
                        raise ChatPointerReplayConflict(
                            "pointer key is not owned by user"
                        )
                    cur.execute(
                        "SELECT seq,doc,storage_generation FROM chat_messages "
                        "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                        (user_id, msg_id),
                    )
                    existing = cur.fetchone()
                    if existing is None:
                        raise ChatPointerReplayConflict(
                            "pointer-only documents cannot create V2 reply rows"
                        )
                    inserted = False
                else:
                    cur.execute(
                        "INSERT INTO chat_messages "
                        "(user_id,msg_id,ts,doc,storage_generation) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (user_id,msg_id) DO NOTHING RETURNING seq",
                        (
                            user_id,
                            msg_id,
                            ts,
                            Jsonb(effect_doc),
                            storage_generation,
                        ),
                    )
                    row = cur.fetchone()
                    inserted = row is not None
                    existing = None
                if inserted:
                    seq = int(row["seq"] if isinstance(row, dict) else row[0])
                    persisted_reply_doc = effect_doc
                else:
                    if existing is None:
                        cur.execute(
                            "SELECT seq,doc,storage_generation "
                            "FROM chat_messages "
                            "WHERE user_id=%s AND msg_id=%s FOR UPDATE",
                            (user_id, msg_id),
                        )
                        existing = cur.fetchone()
                    if existing is None:
                        raise RuntimeError("idempotent reply row disappeared")
                    if isinstance(existing, dict):
                        seq = int(existing["seq"])
                        existing_doc = existing["doc"]
                    else:
                        seq = int(existing[0])
                        existing_doc = existing[1]
                    existing_storage_generation = int(
                        existing["storage_generation"]
                        if isinstance(existing, dict)
                        else existing[2]
                    )
                    if existing_storage_generation != storage_generation:
                        raise RuntimeError(
                            "reply row belongs to retired storage generation"
                        )
                    # A client can choose chat envelope ids, while V2 reply ids
                    # are deterministic. Never let a colliding user/stale row
                    # masquerade as an idempotent reply and advance the input
                    # cursor without delivering this assistant envelope.
                    immutable_reply_fields = (
                        "id", "role", "source", "v", "body_ct", "nonce",
                        "K_user", "K_enclave", "enclave_pk_fpr", "visibility",
                        "owner_user_id", "content_type",
                        "turn_failure_error_class", "turn_failure_blame",
                        "turn_failure_user_text",
                    )
                    if not isinstance(existing_doc, dict) or any(
                        existing_doc.get(field) != effect_doc.get(field)
                        for field in immutable_reply_fields
                    ):
                        raise RuntimeError("reply id collision with different content")
                    if _is_chat_file_pointer(effect_doc) and (
                        not _is_chat_file_pointer(existing_doc)
                        or str(existing_doc.get("body_key") or "")
                        != str(effect_doc.get("body_key") or "")
                    ):
                        raise ChatPointerReplayConflict(
                            "stale V2 reply pointer does not match current body"
                        )
                    persisted_reply_doc = existing_doc

                if cursor_seq is not None:
                    # V2 -> resident rollback bridge. Link every still-unanswered
                    # user input consumed through this final reply to the
                    # deterministic assistant row. Resident poll/redelivery already
                    # treats these fields as the authoritative answered marker.
                    cur.execute(
                        "UPDATE chat_messages SET doc=doc || %s "
                        "WHERE user_id=%s AND seq>%s AND seq<=%s "
                        "  AND doc->>'role' IN ('user','human') "
                        "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                        "  AND COALESCE(doc->>'reply_message_id','')='' "
                        "RETURNING msg_id",
                        (
                            Jsonb(replied_fields),
                            user_id,
                            previous_cursor,
                            cursor_seq,
                        ),
                    )
                    replied_user_ids.extend(
                        str(
                            updated["msg_id"]
                            if isinstance(updated, dict)
                            else updated[0]
                        )
                        for updated in cur.fetchall()
                    )

                    persisted_runtime_doc = _advance_blob_int_on_cursor(
                        cur,
                        user_id,
                        "model_api_runtime",
                        v2_cursor.CURSOR_KEY,
                        cursor_seq,
                    )

    def _post_commit() -> None:
        _offload_chat_body_after_commit(
            user_id, msg_id, persisted_reply_doc, storage_generation,
        )
        if replied_user_ids:
            from tee_shadow import mirror
            mirror.execute(
                "UPDATE chat_messages SET doc=doc || %s "
                "WHERE user_id=%s AND msg_id=ANY(%s)",
                (Jsonb(replied_fields), user_id, replied_user_ids),
            )
        if persisted_runtime_doc is not None:
            _mirror_persisted_blob(
                user_id, "model_api_runtime", persisted_runtime_doc)

    if defer_post_commit:
        return seq, inserted, _post_commit
    _post_commit()
    return seq, inserted


def chat_append(user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int) -> None:
    """Best-effort legacy (pre-V2) durable chat write.

    ``max_messages`` is retained for API compatibility and only bounds the
    caller's hot cache. Legacy rollback traffic follows the same no-automatic-
    deletion source-retention rule as V2.
    """
    try:
        _chat_append_impl(user_id, msg_id, ts, doc, max_messages, coverage_gated=False)
    except Exception as e:
        log.error("[db] chat_append(%s,%s) failed: %s", user_id, msg_id, e)


class RuntimeControlChangedError(RuntimeError):
    """The hosted-runtime ownership tuple changed before a fenced write."""


def chat_append_and_enqueue(
    user_id: str, msg_id: str, ts: float, doc: dict, max_messages: int, lane: str,
    *, reason=None, trace_id=None, expected_generation: int | None = None,
    expected_runtime_state: str | None = None,
    expected_runtime_mode: str | None = None,
    client_msg_id: str | None = None,
    idempotency_window_sec: int | None = None,
) -> tuple[int, int | None]:
    """Atomically persist one chat message AND enqueue/coalesce its job in ONE
    transaction. Either both commit or neither does — closes the orphan-message
    gap where the message persisted but the process died before the job insert
    (spec A7). Returns ``(seq, job_id)``. ``job_id`` is ``None`` only when a
    supplied ``client_msg_id`` recovers an existing logical send; that retry
    neither inserts another message nor enqueues/coalesces another job.

    The optional client idempotency guard runs inside this same transaction and
    uses the same cross-process advisory lock as ``chat_append_idempotent``.
    This preserves the iOS retry contract after hosted resident execution is
    retired: a lost HTTP response cannot cause a duplicate V2 turn.

    Heavy file/image bodies follow the same crash-safe post-commit offload as
    other chat writes. The inline message and job still commit atomically; only
    after that transaction succeeds do we upload and flip the row to a pointer.
    ``max_messages`` bounds only process-local hot caches; durable source rows
    are never automatically trimmed here.

    One pool connection, one transaction, two cursors (a default cursor for
    the message INSERT, a ``dict_row`` cursor for the job coalesce-or-insert)
    — never two pool connections; that would break the atomicity this
    function exists to provide.

    Retry-on-``UniqueViolation``, mirroring ``jobs_store.enqueue_job``: two
    concurrent same-user/same-lane sends with no active job yet can both pass
    the coalesce's "no active job" ``SELECT ... FOR UPDATE`` check and both
    attempt the ``agent_jobs`` INSERT; the loser hits the single-flight unique
    index and would otherwise roll back — losing the message half too, since
    both writes share one transaction. Up to 3 retries re-run the WHOLE
    transaction (message INSERT + coalesce); the message INSERT is an
    idempotent upsert (``ON CONFLICT (user_id, msg_id) DO UPDATE``), so
    re-running it is a no-op and the retry's coalesce now sees the racer's
    committed job and coalesces instead of colliding. A final unguarded
    attempt is allowed to raise, same as ``enqueue_job``.
    """
    from model_api_runtime.v2 import jobs_store  # lazy — precedent db.py cutover import
    from psycopg.rows import dict_row

    if lane not in jobs_store.LANES:
        raise ValueError(f"unknown lane: {lane!r}")
    if (client_msg_id is None) != (idempotency_window_sec is None):
        raise ValueError(
            "client_msg_id and idempotency_window_sec must be supplied together"
        )
    if client_msg_id is not None:
        if not str(client_msg_id):
            raise ValueError("client_msg_id is required")
        if int(idempotency_window_sec or 0) <= 0:
            raise ValueError("idempotency_window_sec must be positive")
        if str(doc.get("client_msg_id") or "") != str(client_msg_id):
            raise ValueError("chat doc client_msg_id does not match guard key")
    fenced = expected_runtime_state is not None or expected_runtime_mode is not None
    if fenced and (
        expected_runtime_state is None
        or expected_runtime_mode is None
        or expected_generation is None
    ):
        raise ValueError(
            "runtime-control CAS requires expected state, mode, and generation")
    priority = jobs_store.LANE_PRIORITY.get(lane, 0)

    def _attempt() -> tuple[int, int | None, int, bool]:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as fence_cur:
                    _lock_chat_user_fence_on_cursor(fence_cur, user_id)
                if fenced:
                    # Match the cutover writer's runtime-row -> blob-row lock
                    # order.  Holding both locks until message+job commit makes
                    # this a true CAS on the tuple observed by chat/send: a
                    # concurrent disable either wins before us (we write
                    # nothing) or waits until the admitted V2 job is durable.
                    state_row = conn.execute(
                        "SELECT hosted_runtime_state, runtime_generation "
                        "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                        (user_id,),
                    ).fetchone()
                    if state_row is None:
                        raise RuntimeControlChangedError(
                            "runtime control row disappeared")
                    current_state, current_generation = state_row
                    blob_row = conn.execute(
                        "SELECT doc->>'hosted_runtime_mode' FROM user_blobs "
                        "WHERE user_id=%s AND kind='model_api_runtime' FOR UPDATE",
                        (user_id,),
                    ).fetchone()
                    current_mode = str(blob_row[0] or "") if blob_row else ""
                    if (
                        str(current_state) != str(expected_runtime_state)
                        or int(current_generation) != int(expected_generation)
                        or current_mode != str(expected_runtime_mode)
                    ):
                        raise RuntimeControlChangedError(
                            "hosted runtime control changed before enqueue")
                if client_msg_id is not None:
                    # Length-prefix the user id so concatenation is unambiguous
                    # without a NUL byte (PostgreSQL text rejects U+0000). This
                    # is deliberately identical to chat_append_idempotent so a
                    # retry crossing /model_api/chat/send and /chat/message
                    # still serializes on one logical-operation key.
                    lock_key = f"{len(user_id)}:{user_id}{client_msg_id}"
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                    duplicate = conn.execute(
                        "SELECT seq FROM chat_messages "
                        "WHERE user_id=%s AND doc->>'client_msg_id'=%s "
                        "AND ts >= EXTRACT(EPOCH FROM clock_timestamp()) - %s "
                        "ORDER BY seq DESC LIMIT 1",
                        (user_id, client_msg_id, idempotency_window_sec),
                    ).fetchone()
                    if duplicate is not None:
                        return int(duplicate[0]), None, 0, False
                with conn.cursor() as mc:
                    seq, storage_generation = _chat_insert_on_cursor(
                        mc, user_id, msg_id, ts, doc, max_messages,
                        coverage_gated=True,
                    )
                with conn.cursor(row_factory=dict_row) as jc:
                    job_id, _coalesced = jobs_store.coalesce_or_insert_on_cursor(
                        jc, user_id, lane, reason=reason, trace_id=trace_id,
                        priority=priority, deadline_at=None,
                        expected_generation=expected_generation,
                    )
        return seq, job_id, storage_generation, True

    def _finish(
        result: tuple[int, int | None, int, bool],
    ) -> tuple[int, int | None]:
        seq, job_id, storage_generation, inserted = result
        if not inserted:
            return seq, None
        # The primary message+job transaction is already committed. Offload the
        # new row's body without touching any older durable source row.
        _offload_chat_body_after_commit(
            user_id, msg_id, doc, storage_generation,
        )
        return seq, job_id

    for _ in range(3):
        try:
            result = _attempt()
        except psycopg.errors.UniqueViolation:
            continue  # 并发 racer 抢先建了 active job；重跑整个事务并 coalesce
        return _finish(result)
    return _finish(_attempt())


def chat_doc_for_seq(user_id: str, seq: int) -> dict | None:
    """Strictly load one persisted chat document by its per-user sequence."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id=%s AND seq=%s",
            (user_id, int(seq)),
        ).fetchone()
    return row[0] if row is not None else None


def reconcile_unenqueued_v2_messages() -> int:
    """Belt to :func:`chat_append_and_enqueue`'s braces (spec A7): find
    authoritative V2 users with an UNANSWERED user message beyond the durable
    reply cursor and NO active (pending/claimed/running) ``chat`` lane job,
    then single-flight enqueue one catch-up per message.

    The cursor, not the newest row's role, is authoritative. A turn can emit an
    intermediate assistant bubble, perform a mutation, and crash before its
    final reply consumes the input. In that state the newest row is assistant,
    but the original user row is still pending and must enter the mutation-free
    recovery path. Reply metadata independently excludes a resident-won input.

    A durable ``reason='reconcile'|'mutation_recovery', trace_id=<msg_id>`` job
    is the per-message marker across every terminal status. This is
    intentionally stronger than active-job single-flight: a provider/config
    failure must not cause the same unanswered input to be retried and billed
    every scheduler tick. A later message has a different id and remains
    eligible for one catch-up.
    """
    from model_api_runtime.v2 import jobs_store  # lazy — precedent db.py cutover import

    with get_pool().connection() as conn:
        candidates = conn.execute(
            "WITH owned AS ("
            "  SELECT mrt.user_id,rs.runtime_generation,"
            "    CASE WHEN COALESCE(mrt.doc->>'v2_reply_cursor_seq','0') "
            "      ~ '^[0-9]{1,18}$' "
            "    THEN (COALESCE(mrt.doc->>'v2_reply_cursor_seq','0'))::bigint "
            "    ELSE NULL END AS reply_cursor_seq "
            "  FROM user_blobs mrt "
            "  JOIN v2_runtime_state rs ON rs.user_id=mrt.user_id "
            "    AND rs.hosted_runtime_state='v2' "
            "  WHERE mrt.kind='model_api_runtime' "
            "    AND mrt.doc->>'hosted_runtime_mode'='db_action_v2'"
            "), latest AS ("
            "  SELECT DISTINCT ON (cm.user_id) "
            "         cm.user_id,cm.msg_id, "
            "         owned.runtime_generation,owned.reply_cursor_seq "
            "  FROM chat_messages cm "
            "  JOIN owned ON owned.user_id=cm.user_id "
            "  WHERE owned.reply_cursor_seq IS NOT NULL "
            "    AND cm.seq>owned.reply_cursor_seq "
            "    AND cm.doc->>'role' IN ('user','human') "
            "    AND COALESCE(cm.doc->>'source','') <> 'verify_ping' "
            "    AND (cm.doc->>'reply_status') IS DISTINCT FROM 'replied' "
            "    AND COALESCE(cm.doc->>'reply_message_id','')='' "
            "  ORDER BY cm.user_id,cm.seq DESC"
            "), eligible AS MATERIALIZED ("
            "  SELECT latest.user_id,latest.msg_id,latest.runtime_generation,"
            "         latest.reply_cursor_seq "
            "  FROM latest "
            "  WHERE NOT EXISTS ("
            "    SELECT 1 FROM agent_jobs active "
            "    WHERE active.user_id=latest.user_id AND active.lane='chat' "
            "      AND active.status IN ('pending','claimed','running')"
            "  ) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM agent_jobs recovery "
            "    WHERE recovery.user_id=latest.user_id AND recovery.lane='chat' "
            "      AND recovery.reason='mutation_recovery' "
            "      AND recovery.trace_id=latest.msg_id "
            "      AND recovery.status <> 'superseded'"
            "  )"
            ") "
            "SELECT eligible.user_id,eligible.msg_id,eligible.runtime_generation,"
            "       COALESCE(barrier.frontier IS NOT NULL,false) "
            "FROM eligible "
            "LEFT JOIN LATERAL ("
            "  SELECT MAX(source.frontier) AS frontier FROM ("
            "    SELECT MAX(attempt.input_frontier_seq) AS frontier "
            "    FROM v2_mcp_mutation_attempts attempt "
            "    WHERE attempt.user_id=eligible.user_id "
            "      AND attempt.input_frontier_seq>eligible.reply_cursor_seq "
            "      AND EXISTS ("
            "        SELECT 1 FROM agent_jobs job "
            "        WHERE job.id=attempt.job_id AND job.lane='chat'"
            "      ) "
            "    UNION ALL "
            "    SELECT MAX(effect.input_frontier_seq) AS frontier "
            "    FROM v2_effect_outbox effect "
            "    WHERE effect.user_id=eligible.user_id "
            "      AND effect.input_frontier_seq>eligible.reply_cursor_seq "
            "      AND EXISTS ("
            "        SELECT 1 FROM agent_jobs job "
            "        WHERE job.id=effect.job_id AND job.lane='chat'"
            "      )"
            "  ) source"
            ") barrier ON TRUE "
            "WHERE (barrier.frontier IS NOT NULL "
            "OR NOT EXISTS ("
            "  SELECT 1 FROM agent_jobs prior "
            "  WHERE prior.user_id=eligible.user_id AND prior.lane='chat' "
            "    AND prior.reason='reconcile' AND prior.trace_id=eligible.msg_id "
            "    AND prior.status <> 'superseded'"
            "))"
        ).fetchall()

    for uid, msg_id, runtime_generation, mutation_recovery in candidates:
        jobs_store.enqueue_job(
            str(uid),
            "chat",
            reason=("mutation_recovery" if mutation_recovery else "reconcile"),
            trace_id=str(msg_id),
            expected_generation=int(runtime_generation),
        )
    return len(candidates)


def reconcile_unenqueued_v2_message_for_user(
    user_id: str,
    *,
    reason: str = "runtime_cutover_recovery",
) -> bool:
    """Eagerly recover one unanswered chat row after V2 ownership changes.

    The periodic fleet sweeper is a final backstop, but a zero-touch cutover
    must not make an already-waiting iOS message sit for its next 60-second
    tick. Use the durable reply cursor rather than the latest chat row: a user
    message can precede the resident assistant row that lost the cutover race.
    A terminal job with the same trace id is a durable no-retry marker.
    """
    from model_api_runtime.v2 import jobs_store
    from psycopg.rows import dict_row

    def _attempt() -> bool:
        with get_pool().connection() as conn:
            with conn.transaction():
                # Match every ownership writer's runtime-row -> blob-row lock
                # order. Holding both through the job INSERT prevents a stale
                # cutover recovery from arriving after a newer generation and
                # superseding that newer generation's legitimate chat job.
                state_row = conn.execute(
                    "SELECT hosted_runtime_state,runtime_generation "
                    "FROM v2_runtime_state WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                ).fetchone()
                if state_row is None:
                    return False
                state, generation = state_row
                profile_row = conn.execute(
                    "SELECT doc FROM user_blobs "
                    "WHERE user_id=%s AND kind='model_api_runtime' FOR UPDATE",
                    (user_id,),
                ).fetchone()
                profile = profile_row[0] if profile_row is not None else None
                if (
                    str(state) != "v2"
                    or not isinstance(profile, dict)
                    or profile.get("hosted_runtime_mode") != "db_action_v2"
                ):
                    return False
                raw_cursor = str(profile.get("v2_reply_cursor_seq", 0))
                if not raw_cursor.isdigit():
                    raise RuntimeError("invalid persisted V2 reply cursor")
                cursor_seq = int(raw_cursor)
                candidate = conn.execute(
                    "WITH latest AS ("
                    "  SELECT cm.user_id,cm.msg_id FROM chat_messages cm "
                    "  WHERE cm.user_id=%s AND cm.seq>%s "
                    "  AND cm.doc->>'role' IN ('user','human') "
                    "  AND COALESCE(cm.doc->>'source','') <> 'verify_ping' "
                    "  AND (cm.doc->>'reply_status') IS DISTINCT FROM 'replied' "
                    "  AND COALESCE(cm.doc->>'reply_message_id','')='' "
                    "  ORDER BY cm.seq DESC LIMIT 1"
                    ") SELECT latest.msg_id FROM latest WHERE "
                    "NOT EXISTS ("
                    "  SELECT 1 FROM agent_jobs active "
                    "  WHERE active.user_id=latest.user_id AND active.lane='chat' "
                    "    AND active.status IN ('pending','claimed','running') "
                    "    AND active.expected_runtime_generation=%s"
                    ") "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_jobs prior "
                    "  WHERE prior.user_id=latest.user_id AND prior.lane='chat' "
                    "    AND prior.trace_id=latest.msg_id "
                    "    AND prior.status<>'superseded' "
                    "    AND (prior.status NOT IN "
                    "      ('pending','claimed','running') "
                    "      OR prior.expected_runtime_generation "
                    "        IS NOT DISTINCT FROM %s)"
                    ")",
                    (user_id, cursor_seq, int(generation), int(generation)),
                ).fetchone()
                if candidate is None:
                    return False
                with conn.cursor(row_factory=dict_row) as cur:
                    jobs_store.coalesce_or_insert_on_cursor(
                        cur,
                        user_id,
                        "chat",
                        reason=str(reason),
                        trace_id=str(candidate[0]),
                        priority=jobs_store.LANE_PRIORITY["chat"],
                        expected_generation=int(generation),
                    )
        return True

    # The independent fleet sweeper can race this targeted recovery without
    # taking the runtime lock. Let the single-flight unique index pick a winner,
    # then re-read the whole ownership/candidate decision like normal enqueue.
    for _ in range(3):
        try:
            return _attempt()
        except psycopg.errors.UniqueViolation:
            continue
    return _attempt()


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

    row = None
    storage_generation: int | None = None
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # Global order matches clear/account deletion: shared chat
                # fence -> lifecycle row -> idempotency key.  The lifecycle
                # lock covers both winner lookup and insert, so clear cannot
                # split those decisions across storage generations.
                _lock_chat_r2_lifecycle_on_cursor(cur, user_id)

                # Length-prefix the user id so concatenation is unambiguous
                # without a NUL byte (PostgreSQL text rejects U+0000).
                lock_key = f"{len(user_id)}:{user_id}{client_msg_id}"
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (lock_key,),
                )
                cur.execute(
                    "SELECT doc FROM chat_messages "
                    "WHERE user_id = %s AND doc->>'client_msg_id' = %s "
                    "AND ts >= EXTRACT(EPOCH FROM clock_timestamp()) - %s "
                    "ORDER BY seq DESC LIMIT 1",
                    (user_id, client_msg_id, window_sec),
                )
                row = cur.fetchone()
                if row is not None:
                    return row[0], False

                # Preserve normal msg-id semantics: an envelope-id collision
                # updates the same row, exactly like chat_append.  Reusing the
                # shared primitive also pins storage_generation and applies the
                # pointer-replay checks used by every other chat write.
                _seq, storage_generation = _chat_insert_on_cursor(
                    cur,
                    user_id,
                    msg_id,
                    ts,
                    doc,
                    max_messages,
                )
                cur.execute(
                    "SELECT doc FROM chat_messages "
                    "WHERE user_id = %s AND msg_id = %s",
                    (user_id, msg_id),
                )
                row = cur.fetchone()

    if row is None or storage_generation is None:
        raise RuntimeError("chat_idempotent_insert_returned_no_row")

    # Only the transaction winner gets here. The shared offload primitive
    # commits an exact-key cleanup guard before PUT and pins every later CAS to
    # this row's storage generation, so clear can safely win at any boundary.
    _offload_chat_body_after_commit(
        user_id,
        msg_id,
        doc,
        storage_generation,
    )
    return row[0], True


def chat_update_metadata(user_id: str, msg_id: str, fields: dict) -> dict | None:
    """Shallow-merge ``fields`` into the stored message doc. Returns the merged
    doc, or None if the message was not found."""
    # Pointer identity is not metadata. Allowing this helper to install one
    # would bypass the generation/replay protocol and could resurrect a key
    # after the cleanup worker's fenced reference check.
    if any(name in (fields or {}) for name in ("body_key", "body_ct_len")):
        log.error("[db] chat_update_metadata refused chat body pointer fields")
        return None
    sql = ("UPDATE chat_messages SET doc = doc || %s WHERE user_id = %s AND msg_id = %s "
           "RETURNING doc")
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                    cur.execute(sql, (Jsonb(fields), user_id, msg_id))
                    row = cur.fetchone()
    except Exception as e:
        log.error("[db] chat_update_metadata(%s,%s) failed: %s", user_id, msg_id, e)
        return None
    from tee_shadow import mirror
    mirror.execute(sql, (Jsonb(fields), user_id, msg_id))
    return row[0] if row is not None else None


def chat_reconcile_legacy_adjacent_reply(
    user_id: str,
    parent_msg_id: str,
    reply_msg_id: str,
) -> tuple[dict, dict] | None:
    """Atomically link one legacy chat reply to its immediately preceding turn.

    Older consumers could append an ordinary ``source=chat`` assistant message
    without ``reply_to_message_id`` and without settling the parent user row.
    The lost-turn backstop then treated that visibly answered turn as abandoned
    and ran the model again. Reconcile only the unambiguous legacy shape: both
    rows still exist, the parent is unanswered, the assistant reply is unlinked,
    and no conversation user message sits between their timestamps.
    """
    parent_fields = {
        "reply_status": "replied",
        "reply_message_id": reply_msg_id,
        "replied_by": "legacy_adjacent_reconcile",
    }
    reply_fields = {"reply_to_message_id": parent_msg_id}
    parent_doc: dict | None = None
    reply_doc: dict | None = None
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                    cur.execute(
                        "SELECT msg_id, ts, doc FROM chat_messages "
                        "WHERE user_id=%s AND msg_id=ANY(%s) "
                        "ORDER BY msg_id FOR UPDATE",
                        (user_id, [parent_msg_id, reply_msg_id]),
                    )
                    rows = {str(row[0]): (float(row[1]), row[2]) for row in cur.fetchall()}
                    if parent_msg_id not in rows or reply_msg_id not in rows:
                        return None
                    parent_ts, parent = rows[parent_msg_id]
                    reply_ts, reply = rows[reply_msg_id]
                    if (
                        parent.get("role") != "user"
                        or str(parent.get("source") or "") in (
                            "verify_ping", "resident_maintenance"
                        )
                        or parent.get("reply_status") == "replied"
                        or str(parent.get("reply_message_id") or "").strip()
                        or str(reply.get("role") or "") not in (
                            "openclaw", "assistant", "agent"
                        )
                        or str(reply.get("source") or "") not in ("", "chat")
                        or str(reply.get("reply_to_message_id") or "").strip()
                        or reply_ts <= parent_ts
                    ):
                        return None
                    cur.execute(
                        "SELECT 1 FROM chat_messages "
                        "WHERE user_id=%s AND ts>%s AND ts<%s "
                        "  AND doc->>'role'='user' "
                        "  AND COALESCE(doc->>'source','') "
                        "      NOT IN ('verify_ping','resident_maintenance') "
                        "LIMIT 1",
                        (user_id, parent_ts, reply_ts),
                    )
                    if cur.fetchone() is not None:
                        return None
                    cur.execute(
                        "UPDATE chat_messages SET doc=doc || %s "
                        "WHERE user_id=%s AND msg_id=%s "
                        "  AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                        "  AND COALESCE(doc->>'reply_message_id','')='' "
                        "RETURNING doc",
                        (Jsonb(parent_fields), user_id, parent_msg_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    parent_doc = row[0]
                    cur.execute(
                        "UPDATE chat_messages SET doc=doc || %s "
                        "WHERE user_id=%s AND msg_id=%s "
                        "  AND COALESCE(doc->>'reply_to_message_id','')='' "
                        "RETURNING doc",
                        (Jsonb(reply_fields), user_id, reply_msg_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError("legacy adjacent reply changed during reconcile")
                    reply_doc = row[0]
    except Exception as e:
        log.error(
            "[db] chat_reconcile_legacy_adjacent_reply(%s,%s,%s) failed: %s",
            user_id,
            parent_msg_id,
            reply_msg_id,
            e,
        )
        return None

    if parent_doc is None or reply_doc is None:
        return None
    from tee_shadow import mirror
    mirror.execute(
        "UPDATE chat_messages SET doc=doc || %s WHERE user_id=%s AND msg_id=%s",
        (Jsonb(parent_fields), user_id, parent_msg_id),
    )
    mirror.execute(
        "UPDATE chat_messages SET doc=doc || %s WHERE user_id=%s AND msg_id=%s",
        (Jsonb(reply_fields), user_id, reply_msg_id),
    )
    return parent_doc, reply_doc


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
    "), learned_vision AS ("
    "  UPDATE model_api_routes r SET "
    "    vision_test_status='unsupported',"
    "    last_vision_test_error='vision_model_required',"
    "    last_vision_test_at=now(),updated_at=now() "
    "  FROM won, inserted "
    "  WHERE inserted.reply_doc->>'turn_failure_error_class'="
    "    'vision_model_required' "
    "    AND r.user_id=%s AND r.is_active "
    "    AND r.id::text=COALESCE("
    "      won.parent_doc->>'vision_main_route_id','') "
    "    AND r.updated_at=NULLIF("
    "      won.parent_doc->>'vision_main_route_updated_at','')::timestamptz "
    "  RETURNING r.id"
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
                user_id,
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


def chat_finalize_reply_sequence_once(
    user_id: str,
    parent_msg_id: str,
    replies: list[tuple[str, float, dict]],
    replied_fields: dict,
) -> tuple[dict, list[dict]] | None:
    """Atomically answer one parent with a text primary and file follow-ups.

    The parent CAS and every reply INSERT share one transaction.  A duplicate
    id or malformed follow-up rolls the whole sequence back, so clients never
    observe a success bubble without the downloadable cards that belong below
    it.  Losing the parent CAS is the only normal ``None`` result.
    """
    if not replies:
        raise ValueError("reply sequence must not be empty")

    parent_doc = None
    inserted_docs: list[dict] = []
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                cur.execute(
                    "UPDATE chat_messages SET doc = doc || %s "
                    "WHERE user_id = %s AND msg_id = %s "
                    "AND (doc->>'reply_status') IS DISTINCT FROM 'replied' "
                    "AND COALESCE(doc->>'reply_message_id','') = '' "
                    "RETURNING doc",
                    (Jsonb(replied_fields), user_id, parent_msg_id),
                )
                parent_row = cur.fetchone()
                if parent_row is None:
                    return None
                parent_doc = parent_row[0]
                for reply_msg_id, reply_ts, reply_doc in replies:
                    cur.execute(
                        "INSERT INTO chat_messages (user_id, msg_id, ts, doc) "
                        "VALUES (%s, %s, %s, %s) RETURNING doc",
                        (
                            user_id,
                            reply_msg_id,
                            reply_ts,
                            Jsonb(reply_doc),
                        ),
                    )
                    inserted_docs.append(cur.fetchone()[0])

    from tee_shadow import mirror

    mirror.execute(
        _CHAT_FINALIZE_REPLY_PARENT_MIRROR_SQL,
        (Jsonb(replied_fields), user_id, parent_msg_id),
    )
    return parent_doc, inserted_docs


def chat_finalize_reply_post_commit(
    user_id: str, reply_doc: dict, max_messages: int
) -> None:
    """Run normal append maintenance after an atomic reply winner commits.

    Finalization must commit the parent CAS and inline encrypted reply together,
    so optional R2 offload happens afterwards. ``max_messages`` is retained for
    API compatibility and bounds only the process-local hot cache; it never
    trims durable source rows. Failures are logged and leave the already
    committed inline reply readable.
    """
    reply_msg_id = str(reply_doc.get("id") or "")
    offload = (
        object_storage.chat_files_enabled()
        and reply_doc.get("content_type") in _R2_OFFLOAD_CONTENT_TYPES
        and reply_doc.get("body_ct") is not None
    )
    try:
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

    except Exception as e:  # noqa: BLE001
        log.error(
            "[db] chat_finalize_reply_post_commit(%s,%s) failed: %s",
            user_id,
            reply_msg_id,
            e,
        )
        return

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
        "  AND ("
        # ...unless this row was CLAIMED AND DROPPED (a claim was stamped, the
        # lease expired, no reply landed). Then the conversation did not move
        # past it — the system took the turn and lost it, and its content may
        # have nothing to do with what came after. Mirrors
        # chat.service._claim_abandoned; kept here too because this CAS is the
        # authoritative decision (the cache-side pre-filter can be stale).
        "    (COALESCE(doc->>'reply_claimed_by','') <> '' "
        "     AND COALESCE(NULLIF(doc->>'reply_claim_expires_at','')::float8, 0) <= %s) "
        "    OR NOT EXISTS ("
        "      SELECT 1 FROM chat_messages n "
        "      WHERE n.user_id = chat_messages.user_id "
        "        AND n.ts > chat_messages.ts "
        "        AND n.doc->>'role' = 'user' "
        "        AND COALESCE(n.doc->>'source','') NOT IN ('verify_ping','resident_maintenance') "
        "        AND ((n.doc->>'reply_status') = 'replied' "
        "             OR COALESCE(n.doc->>'reply_message_id','') <> '')"
        "    )"
        "  ) "
    ) if redelivery else ""
    params: list = [Jsonb(fields), user_id, msg_id]
    if redelivery:
        params.append(now)  # abandoned-claim exemption inside unanswered_tail_sql
    else:
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
            with conn.transaction():
                with conn.cursor() as cur:
                    _lock_chat_r2_lifecycle_on_cursor(cur, user_id)
                    cur.execute(sql + " RETURNING 1", (user_id, msg_id))
                    row = cur.fetchone()
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
    # The DELETE trigger committed the exact persisted body key. The isolated
    # cleanup loop owns network deletion so this request cannot wedge on R2.
    return True


def chat_clear(user_id: str) -> int | None:
    """Atomically retire one user's complete *live* chat context.

    The raw transcript is only one input to a V2 turn. A history clear moves
    every encrypted source row into an immutable archive retained until account
    deletion, while removing it from all live-chat reads. It must also remove
    the encrypted summary, chat-derived artifact text views,
    pending effects, recovery barriers, and client status rows.  The existing
    per-user chat advisory fence is the linearization point: ordinary writers
    take it shared, while clear takes it exclusive *before* touching runtime
    state or any child row.  The runtime-generation bump then rejects a worker
    that was doing provider work without holding a database transaction.

    Terminal job metadata, encrypted trajectory/review rows, and the encrypted
    raw-chat archive are retained as debug records and are never prompt inputs.
    Active jobs/reviews are fenced into terminal states instead of deleting
    ``agent_jobs`` because trajectory rows intentionally cascade from their
    source job. Independent Memory Garden, identity, schedules, user-authored
    workspace/working-memory, skills, and content-free billing/token metrics are
    also preserved.

    Returns the number of raw chat rows deleted, or ``None`` if the database
    transaction failed.  Even an already-empty clear advances both the chat R2
    lifecycle and V2 runtime generation, so delayed pre-clear work cannot
    publish after the endpoint returns.
    """
    sql = "DELETE FROM chat_messages WHERE user_id = %s"
    persisted_runtime_doc: dict | None = None
    cleared_review_job_ids: list = []
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # Global order: chat fence -> R2 lifecycle -> runtime state
                    # -> job/effect/derived rows.  Effect application follows
                    # the same chat-fence-before-runtime-state order, avoiding
                    # an exclusive-clear/shared-writer ABBA cycle.
                    _lock_chat_user_fence_on_cursor(
                        cur, user_id, exclusive=True,
                    )
                    # Clear is a visibility boundary, not a retention boundary.
                    # Keep the R2 storage generation stable so archived body
                    # pointers remain readable; account deletion owns generation
                    # retirement and inventory cleanup.
                    _lock_chat_r2_lifecycle_on_cursor(cur, user_id)

                    # Materialize the authority row for a known user and bump
                    # it without changing hosted ownership.  Every active job
                    # pins the old value, so all later job/effect/trajectory
                    # writes fail their source-generation check.
                    cur.execute(
                        "INSERT INTO v2_runtime_state (user_id) "
                        "SELECT %s WHERE EXISTS ("
                        "  SELECT 1 FROM users WHERE user_id=%s"
                        ") ON CONFLICT (user_id) DO NOTHING",
                        (user_id, user_id),
                    )
                    cur.execute(
                        "UPDATE v2_runtime_state SET "
                        "runtime_generation=runtime_generation+1,updated_at=now() "
                        "WHERE user_id=%s RETURNING runtime_generation",
                        (user_id,),
                    )
                    generation_row = cur.fetchone()
                    clear_generation = int(
                        (
                            generation_row["runtime_generation"]
                            if isinstance(generation_row, dict)
                            else generation_row[0]
                        )
                        if generation_row is not None
                        else 1
                    )

                    # The active table is the user/agent-visible conversation.
                    # Preserve its encrypted source ledger first, then remove
                    # the live rows in the same transaction. The archive-aware
                    # trigger keeps any referenced R2 ciphertext alive.
                    cur.execute(
                        "INSERT INTO chat_message_archive "
                        "(user_id,source_seq,msg_id,ts,doc,storage_generation,"
                        "clear_generation) "
                        "SELECT user_id,seq,msg_id,ts,doc,storage_generation,%s "
                        "FROM chat_messages WHERE user_id=%s "
                        "ON CONFLICT (user_id,source_seq) DO NOTHING",
                        (clear_generation, user_id),
                    )

                    # Preserve immutable encrypted trajectory history by
                    # retaining its source jobs, but make every in-flight lane
                    # lose ownership before the clear commits.
                    cur.execute(
                        "UPDATE agent_jobs SET status='superseded',"
                        "last_error='chat_history_cleared',claimed_by=NULL,"
                        "lease_expires_at=NULL,finished_at=COALESCE(finished_at,now()) "
                        "WHERE user_id=%s "
                        "AND status IN ('pending','claimed','running')",
                        (user_id,),
                    )
                    cur.execute(
                        "UPDATE v2_trajectory_reviews SET status='failed',"
                        "claimed_by_job_id=NULL,last_error='chat_history_cleared',"
                        "finished_at=COALESCE(finished_at,now()) "
                        "WHERE user_id=%s AND status IN ('pending','running') "
                        "RETURNING source_job_id",
                        (user_id,),
                    )
                    cleared_review_job_ids = [r[0] for r in cur.fetchall()]

                    # A terminal-failure reconciler may already own one of
                    # these rows.  Deleting it here waits for that transaction;
                    # status/route cleanup below runs afterwards and therefore
                    # also erases anything the earlier reconciler committed.
                    cur.execute(
                        "DELETE FROM v2_terminal_failure_outbox WHERE user_id=%s",
                        (user_id,),
                    )
                    # Capture retry journals contain encrypted chat-derived
                    # card bodies.  Clear removes prepared batches and their
                    # exact frontier together; applied batches are deleted by
                    # the successful Capture commit itself.
                    cur.execute(
                        "DELETE FROM v2_capture_batches WHERE user_id=%s",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM user_blobs WHERE user_id=%s AND kind='capture_state'",
                        (user_id,),
                    )

                    # Sink markers have no user FK.  Remove both parent ids and
                    # deterministic workspace-batch child ids while the outbox
                    # rows still identify ownership.
                    cur.execute(
                        "DELETE FROM v2_effect_sink_applied AS sink "
                        "USING v2_effect_outbox AS effect "
                        "WHERE effect.user_id=%s AND ("
                        " sink.effect_id=effect.effect_id OR "
                        " position(effect.effect_id || ':item:' "
                        "          IN sink.effect_id)=1)",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM v2_effect_outbox WHERE user_id=%s",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM agent_action_queue AS action "
                        "USING agent_jobs AS job "
                        "WHERE action.job_id=job.id AND job.user_id=%s",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM v2_mcp_mutation_attempts WHERE user_id=%s",
                        (user_id,),
                    )

                    # Immutable summary segments/checkpoints are prompt-derived
                    # chat state.  They are never compacted/GCed automatically,
                    # but explicit Chat clear removes them under this same
                    # exclusive generation fence before deleting the CAS head.
                    cur.execute(
                        "DELETE FROM v2_conversation_summary_segments "
                        "WHERE user_id=%s",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM v2_conversation_summary WHERE user_id=%s",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM v2_workspace_entries "
                        "WHERE user_id=%s AND kind='artifact'",
                        (user_id,),
                    )
                    cur.execute(
                        "DELETE FROM runtime_state WHERE user_id=%s",
                        (user_id,),
                    )
                    cur.execute(sql, (user_id,))
                    deleted_count = int(cur.rowcount)

                    # Keep provider/route configuration but reset the chat
                    # cursor and user-visible failure state derived from the
                    # cleared turns.  A post-clear send starts on the new
                    # generation and rebuilds these fields normally.
                    cur.execute(
                        "UPDATE user_blobs SET doc="
                        "(doc-'v2_reply_cursor_seq') || jsonb_build_object("
                        " %s::text, (CASE WHEN COALESCE(doc->>%s,'') "
                        " ~ '^[0-9]{1,18}$' THEN (doc->>%s)::bigint "
                        " ELSE 0 END) + 1) "
                        "WHERE user_id=%s AND kind='model_api_runtime' "
                        "RETURNING doc",
                        (
                            _BLOB_REVISION_KEY,
                            _BLOB_REVISION_KEY,
                            _BLOB_REVISION_KEY,
                            user_id,
                        ),
                    )
                    runtime_row = cur.fetchone()
                    if runtime_row is not None:
                        persisted_runtime_doc = (
                            runtime_row["doc"]
                            if isinstance(runtime_row, dict)
                            else runtime_row[0]
                        )
                    cur.execute(
                        "UPDATE model_api_routes SET last_runtime_error='',"
                        "last_runtime_error_class='',updated_at=now() "
                        "WHERE user_id=%s",
                        (user_id,),
                    )

                    # Last by design.  A failure reconciler that began before
                    # its marker was deleted can only commit before this DELETE;
                    # job-scoped ordinary status writers share the chat fence.
                    cur.execute(
                        "DELETE FROM agent_status_events WHERE user_id=%s",
                        (user_id,),
                    )
    except Exception as e:
        log.error("[db] chat_clear(%s) failed: %s", user_id, e)
        return None
    # Clear deliberately queues no body deletion and no retired-generation
    # inventory: archived pointers remain durable until account deletion. The
    # plaintext TEE hot copy is still removed because it is live runtime state.
    from tee_shadow import mirror
    mirror.execute_many([
        (sql, (user_id,)),
        ("DELETE FROM runtime_state WHERE user_id = %s", (user_id,)),
        ("DELETE FROM user_blobs WHERE user_id = %s AND kind = 'capture_state'", (user_id,)),
        ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
         "AND table_name = 'chat_messages'", (user_id,)),
    ])
    # v2_trajectory_reviews rows fenced to 'failed' above are same-PK in-place
    # rewrites the append-only replicator cursor never revisits — requeue each
    # one so the next replicator pass re-derives the TEE plaintext.
    for source_job_id in cleared_review_job_ids:
        mirror.mark_pending(user_id, "v2_trajectory_reviews", str(source_job_id), "requeue")
    if persisted_runtime_doc is not None:
        _mirror_persisted_blob(
            user_id, "model_api_runtime", persisted_runtime_doc,
        )
    return deleted_count


# ---------------------------------------------------------------------------
# Memory moments (row-per-item)
# ---------------------------------------------------------------------------


def memory_profile_source_stats(user_id: str) -> tuple[int, str]:
    """Return content-free Garden freshness metadata for profile scheduling.

    This reads only row count and the envelope's plaintext ``updated_at``
    metadata. It never decrypts card content and fails loud so a DB outage
    cannot masquerade as an unchanged Garden.
    """

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*), COALESCE(max(doc->>'updated_at'), '') "
            "FROM memory_moments WHERE user_id = %s",
            (str(user_id),),
        ).fetchone()
    return int(row[0] or 0), str(row[1] or "")


def memory_load(user_id: str) -> list[dict]:
    try:
        context = _memory_mutation_context(user_id)
        if context is not None:
            rows = context[0].execute(
                "SELECT doc FROM memory_moments WHERE user_id = %s "
                "ORDER BY occurred_at, moment_id",
                (user_id,),
            ).fetchall()
        else:
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


def memory_profile_source_snapshot(user_id: str) -> dict:
    """Content-free Memory Garden fingerprint used by profile refresh policy.

    The profile generator itself still reads/decrypts every eligible card
    through the enclave readside.  This aggregate is deliberately DB-only so a
    normal chat turn can decide whether a seven-day-old profile is stale
    without disclosing or loading any card plaintext.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*)::bigint, "
            "COALESCE(max(doc->>'updated_at'), '') "
            "FROM memory_moments WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    return {
        "card_count": int(row[0]) if row and row[0] is not None else 0,
        "max_updated_at": str(row[1] or "") if row else "",
    }


def memory_upsert(user_id: str, moment_id: str, occurred_at: str, doc: dict) -> bool:
    """Single-row upsert. Returns True iff the write committed — callers that
    advance state on success (e.g. memory.upgrade / migration) MUST check it."""
    try:
        context = _memory_mutation_context(user_id)
        if context is None:
            with memory_user_mutation_fence(user_id):
                context = _memory_mutation_context(user_id)
                context[0].execute(
                    "INSERT INTO memory_moments (user_id, moment_id, occurred_at, doc) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id, moment_id) DO UPDATE SET "
                    "occurred_at = EXCLUDED.occurred_at, doc = EXCLUDED.doc",
                    (user_id, moment_id, occurred_at or "", Jsonb(doc)),
                )
        else:
            context[0].execute(
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
    _defer_memory_post_commit(
        user_id,
        lambda: mirror.mark_pending(
            user_id, "memory_moments", moment_id, "requeue"
        ),
    )
    return True


def memory_delete(user_id: str, moment_id: str) -> bool:
    sql = "DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s"
    try:
        context = _memory_mutation_context(user_id)
        if context is None:
            with memory_user_mutation_fence(user_id):
                context = _memory_mutation_context(user_id)
                cur = context[0].execute(sql, (user_id, moment_id))
        else:
            cur = context[0].execute(sql, (user_id, moment_id))
        deleted = cur.rowcount > 0
    except Exception as e:
        log.error("[db] memory_delete(%s,%s) failed: %s", user_id, moment_id, e)
        return False
    from tee_shadow import mirror
    _defer_memory_post_commit(
        user_id,
        lambda: mirror.execute_many([
            (sql, (user_id, moment_id)),
            ("DELETE FROM tee_pending_device_migration WHERE user_id = %s "
             "AND table_name = 'memory_moments' AND item_id = %s", (user_id, moment_id)),
        ]),
    )
    return deleted


def memory_replace_all(user_id: str, moments: list[dict]) -> None:
    """Atomically reconcile the stored moment set to `moments`. The final row
    set equals the input list (full-replace semantics preserved), but only rows
    that were removed are deleted and only rows whose doc changed are upserted,
    so a single-card edit no longer rewrites the user's entire garden. Used
    where the old code did load-list / mutate / save-whole-list."""
    removed_ids: list[str] = []
    survivor_ids: list[str] = []
    try:
        context = _memory_mutation_context(user_id)
        if context is None:
            with memory_user_mutation_fence(user_id):
                memory_replace_all(user_id, moments)
            return
        conn = context[0]
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
        # Callers may advance a durable frontier only after this write lands.
        # Treating a database failure as success silently loses memory effects.
        raise
    # Primary committed → propagate to the TEE shadow (best-effort). memory rows
    # are ciphertext→plaintext REPLICATED (not dual-written), and an in-place
    # edit keeps the same (occurred_at, moment_id) PK while a back-dated insert
    # lands BEHIND the append-only cursor — the replicator never revisits either.
    # So: mirror the pinned DELETEs for removed ids (same pattern as
    # frame_prune_to) + enqueue every survivor on the requeue lane. memory sets
    # are small (tens), so requeue-all-survivors is acceptable churn (brief §C3).
    from tee_shadow import mirror
    def _propagate() -> None:
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

    _defer_memory_post_commit(user_id, _propagate)


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
    COALESCE(r.reasoning_effort, ''), r.context_window_tokens,
    r.is_active, r.is_vision, r.test_status,
    COALESCE(to_char(r.last_test_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), ''),
    r.last_test_error, r.vision_test_status,
    COALESCE(to_char(r.last_vision_test_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), ''),
    r.last_vision_test_error, r.last_runtime_error, r.last_runtime_error_class,
    COALESCE(to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), ''),
    COALESCE(to_char(r.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), '')
"""


def _route_row_to_dict(row: tuple) -> dict:
    return {
        "id": row[0], "credential_id": row[1], "provider": row[2], "model": row[3],
        "credential_label": row[4], "api_key_hint": row[5], "base_url": row[6],
        "supports_responses": bool(row[7]), "reasoning_effort": row[8],
        "context_window_tokens": int(row[9]) if row[9] is not None else None,
        "is_active": bool(row[10]), "is_vision": bool(row[11]),
        "test_status": row[12], "last_test_at": row[13], "last_test_error": row[14],
        "vision_test_status": row[15], "last_vision_test_at": row[16],
        "last_vision_test_error": row[17], "last_runtime_error": row[18],
        "last_runtime_error_class": row[19],
        "created_at": row[20], "updated_at": row[21],
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


def model_api_config_delete_strict(user_id: str) -> bool:
    """Delete every provider credential/route and the frozen legacy blob.

    This is the all-config DELETE endpoint's fail-loud persistence primitive.
    Credentials cascade to routes, and the legacy blob is removed in the same
    transaction, so a database error cannot produce a partial success that the
    API reports as complete.  The ``model_api_runtime`` control blob is
    intentionally outside this function: it carries the V2 correctness cursor
    and is fenced/scrubbed first by ``prepare_model_api_delete``.
    """
    legacy_sql = "DELETE FROM user_blobs WHERE user_id = %s AND kind = 'model_api'"
    with get_pool().connection() as conn:
        with conn.transaction():
            credentials = conn.execute(
                "DELETE FROM model_api_credentials WHERE user_id = %s",
                (user_id,),
            ).rowcount
            legacy = conn.execute(legacy_sql, (user_id,)).rowcount

    # The primary transaction is authoritative.  Mirror the plaintext-safe
    # deletion only after it commits, matching delete_blob's shadow behavior.
    if legacy:
        from tee_shadow import mirror
        mirror.execute(legacy_sql, (user_id,))
    return bool(credentials or legacy)


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
                               provider: str | None = None,
                               base_url: str | None = None,
                               label: str | None = None,
                               api_key_envelope: dict | None = None,
                               api_key_hint: str | None = None,
                               supports_responses: bool | None = None) -> bool:
    """Update one credential and invalidate visual proof when its target changes."""
    sets, params = [], []
    if provider is not None:
        sets.append("provider = %s")
        params.append(provider)
    if base_url is not None:
        sets.append("base_url = %s")
        params.append(base_url)
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
            with conn.transaction():
                prior_identity = None
                if provider is not None or base_url is not None:
                    prior_identity = conn.execute(
                        "SELECT provider, base_url FROM model_api_credentials "
                        "WHERE user_id = %s AND id = %s FOR UPDATE",
                        (user_id, credential_id),
                    ).fetchone()
                cur = conn.execute(
                    f"UPDATE model_api_credentials SET {', '.join(sets)} "
                    "WHERE user_id = %s AND id = %s",
                    tuple(params),
                )
                identity_changed = bool(
                    prior_identity
                    and (
                        (provider is not None and provider != prior_identity[0])
                        or (base_url is not None and base_url != prior_identity[1])
                    )
                )
                if (
                    (api_key_envelope is not None or identity_changed)
                    and cur.rowcount > 0
                ):
                    conn.execute(
                        "UPDATE model_api_routes SET "
                        "vision_test_status = 'untested', "
                        "last_vision_test_error = '', last_vision_test_at = NULL, "
                        "updated_at = now() "
                        "WHERE user_id = %s AND credential_id = %s",
                        (user_id, credential_id),
                    )
        updated = cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_credential_update(%s,%s) failed: %s", user_id, credential_id, e)
        return False
    # Primary committed. Same-PK in-place rewrite (BYOK key rotation, label
    # edit, ...) that the append-only replicator cursor never revisits once
    # this row's PK has been seen — same requeue-lane pattern as
    # memory_upsert/world_book_upsert. Best-effort: mirror swallows failures.
    if updated:
        from tee_shadow import mirror
        mirror.mark_pending(user_id, "model_api_credentials", credential_id, "requeue")
    return updated


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


def model_api_route_get_with_envelope(user_id: str, route_id: str) -> dict | None:
    """Return one caller-owned route with its encrypted provider credential."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_ROUTE_COLUMNS}, c.api_key_envelope "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s AND r.id = %s",
                (user_id, route_id),
            ).fetchone()
        if row is None:
            return None
        out = _route_row_to_dict(row)
        out["api_key_envelope"] = row[-1]
        return out
    except Exception as e:
        log.error(
            "[db] model_api_route_get_with_envelope(%s,%s) failed: %s",
            user_id,
            route_id,
            e,
        )
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


def model_api_active_route_version(user_id: str) -> dict | None:
    """Return the active route's exact visual-capability fence.

    The microsecond timestamp is an internal compare-and-swap token, not a public
    route field. Setup probes and Hosted V1 failure learning use it so delayed
    provider results cannot mark a route the user changed in the meantime.
    """
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT r.id::text,c.provider,r.model,c.base_url,"
                "to_char(r.updated_at AT TIME ZONE 'UTC',"
                "  'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id=r.credential_id "
                "WHERE r.user_id=%s AND r.is_active",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "route_id": str(row[0]),
            "provider": str(row[1] or ""),
            "model": str(row[2] or ""),
            "base_url": str(row[3] or ""),
            "updated_at_token": str(row[4] or ""),
        }
    except Exception as e:
        log.error("[db] model_api_active_route_version(%s) failed: %s", user_id, e)
        return None


def model_api_vision_route(user_id: str) -> dict | None:
    """Return the dedicated vision route with its encrypted credential."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                f"SELECT {_ROUTE_COLUMNS}, c.api_key_envelope "
                "FROM model_api_routes r "
                "JOIN model_api_credentials c ON c.id = r.credential_id "
                "WHERE r.user_id = %s AND r.is_vision",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        out = _route_row_to_dict(row)
        out["api_key_envelope"] = row[-1]
        return out
    except Exception as e:
        log.error("[db] model_api_vision_route(%s) failed: %s", user_id, e)
        return None


def model_api_route_upsert(
    user_id: str,
    credential_id: str,
    model: str,
    reasoning_effort: str | None,
    context_window_tokens: int | None = None,
) -> str | None:
    """按 (credential_id, model) upsert。跨用户引用会被复合外键拒绝 → 返回 None。"""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO model_api_routes "
                "  (id, user_id, credential_id, model, reasoning_effort, "
                "   context_window_tokens) "
                "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s) "
                "ON CONFLICT (credential_id, model) DO UPDATE SET "
                "  reasoning_effort = EXCLUDED.reasoning_effort, "
                "  context_window_tokens = COALESCE("
                "      EXCLUDED.context_window_tokens, "
                "      model_api_routes.context_window_tokens), "
                "  updated_at = now() "
                "RETURNING id::text",
                (
                    user_id,
                    credential_id,
                    model,
                    reasoning_effort,
                    context_window_tokens,
                ),
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
    route_id 不存在/属于别人时把用户当前的 active route 误清掉，导致 Runtime V2
    找不到可执行 provider route。``FOR UPDATE`` 顺便锁住目标行，对并发也有好处。
    route_id 非法 UUID 字面量时 psycopg cast 抛异常，
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


def model_api_route_mark_vision_test(
    user_id: str,
    route_id: str,
    *,
    status: str,
    error: str = "",
    expected_updated_at: str = "",
) -> bool:
    try:
        with get_pool().connection() as conn:
            if expected_updated_at:
                cur = conn.execute(
                    "UPDATE model_api_routes SET vision_test_status = %s, "
                    "       last_vision_test_error = %s, "
                    "       last_vision_test_at = now(), updated_at = now() "
                    "WHERE user_id = %s AND id = %s "
                    "AND updated_at = %s::timestamptz",
                    (
                        status,
                        str(error or "")[:300],
                        user_id,
                        route_id,
                        expected_updated_at,
                    ),
                )
            else:
                cur = conn.execute(
                    "UPDATE model_api_routes SET vision_test_status = %s, "
                    "       last_vision_test_error = %s, "
                    "       last_vision_test_at = now(), updated_at = now() "
                    "WHERE user_id = %s AND id = %s",
                    (status, str(error or "")[:300], user_id, route_id),
                )
        return cur.rowcount > 0
    except Exception as e:
        log.error(
            "[db] model_api_route_mark_vision_test(%s,%s) failed: %s",
            user_id,
            route_id,
            e,
        )
        return False


def model_api_route_set_vision(user_id: str, route_id: str) -> bool:
    """Atomically assign one saved route as the user's V2 image observer."""
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                target = conn.execute(
                    "SELECT 1 FROM model_api_routes "
                    "WHERE user_id = %s AND id = %s FOR UPDATE",
                    (user_id, route_id),
                ).fetchone()
                if target is None:
                    return False
                conn.execute(
                    "UPDATE model_api_routes SET is_vision = FALSE, updated_at = now() "
                    "WHERE user_id = %s AND is_vision AND id != %s",
                    (user_id, route_id),
                )
                cur = conn.execute(
                    "UPDATE model_api_routes SET is_vision = TRUE, updated_at = now() "
                    "WHERE user_id = %s AND id = %s",
                    (user_id, route_id),
                )
        return cur.rowcount > 0
    except Exception as e:
        log.error("[db] model_api_route_set_vision(%s,%s) failed: %s", user_id, route_id, e)
        return False


def model_api_route_clear_vision(user_id: str) -> bool:
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE model_api_routes SET is_vision = FALSE, updated_at = now() "
                "WHERE user_id = %s AND is_vision",
                (user_id,),
            )
        return True
    except Exception as e:
        log.error("[db] model_api_route_clear_vision(%s) failed: %s", user_id, e)
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


def log_append_numbered(
    user_id: str,
    stream: str,
    doc: dict,
    *,
    number_field: str,
    ts: float,
    item_key: str,
) -> dict | None:
    """Append a log row with a per-item monotonic number assigned atomically.

    The advisory lock covers the empty-stream case where there is no row to lock
    yet, and also serializes overlapping workers appending attempts for the same
    logical item. The stored JSON is never patched after insertion.
    """
    numbered_doc = dict(doc)
    seq = None
    try:
        with get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"numbered-log:{stream}:{user_id}:{item_key}",),
                    )
                    cur.execute(
                        "SELECT COUNT(*) FROM user_logs "
                        "WHERE user_id = %s AND stream = %s AND item_key = %s",
                        (user_id, stream, item_key),
                    )
                    numbered_doc[number_field] = int(cur.fetchone()[0]) + 1
                    cur.execute(
                        "INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING seq",
                        (user_id, stream, ts, item_key, Jsonb(numbered_doc)),
                    )
                    seq = cur.fetchone()[0]
    except Exception as e:
        log.error("[db] log_append_numbered(%s,%s,%s) failed: %s",
                  user_id, stream, item_key, e)
        return None

    from tee_shadow import mirror
    mirror.execute(
        "INSERT INTO user_logs (user_id, stream, seq, ts, item_key, doc) "
        "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, stream, seq) DO NOTHING",
        (user_id, stream, seq, ts, item_key, Jsonb(numbered_doc)),
    )
    return numbered_doc


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
        "v2_usage_daily_dimensions",
        "v2_usage_daily_users",
        "v2_conversation_summary_segments",
        "v2_conversation_summary",
        "v2_turn_metrics",
        "chat_message_archive",
        "chat_messages",
        "memory_moments",
        "world_book_entries",
        "frame_envelopes",
        "user_logs",
        "user_blobs",
        "perception_items",
        "perception_daily",
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
    _no_tee_tables = {
        "v2_usage_daily_dimensions",
        "v2_usage_daily_users",
        "v2_conversation_summary_segments",
        "v2_conversation_summary",
        "v2_turn_metrics",
        "chat_message_archive",
        "genesis_import_chunks",
        "model_api_routes",
        "model_api_credentials",
    }
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
    """Durably schedule account cleanup without performing network I/O.

    ``delete_user`` advanced the lifecycle before its chat CASCADE. Following
    that persisted boundary avoids a whole-prefix sweep deleting a same-id
    account's newer-generation object if registration races reset cleanup.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                _mark_chat_r2_inventory_pending_on_cursor(
                    cur, user_id, advance_generation=False,
                )


def list_agent_status_events(user_id: str, *, after_id: int = 0, limit: int = 50) -> list[dict]:
    """托管运行时 v2（子项目 B/C 共用的单一读源）：按 id 升序返回该用户 after_id 之后
    的 agent_status_events 行。Plan C 的 chat/poll_core 长轮询游标读、jobs_store.
    list_status_events 都委托到这个原语——避免两处 SQL 各写一份、日后走形。"""
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT id, job_id, user_id, kind, label, detail_json, seq, "
            "       extract(epoch FROM created_at)::float8 AS created_at "
            "FROM agent_status_events "
            "WHERE user_id = %s AND id > %s ORDER BY id ASC LIMIT %s",
            (user_id, int(after_id), int(limit)),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def get_hosted_runtime_control_strict(user_id: str) -> tuple[str, str, int]:
    """Read routing mode, authoritative state, and generation together.

    The three values come from one PostgreSQL snapshot so callers never
    synthesize a control tuple from independent blob/state reads.  Missing
    rows retain the rollout-safe resident defaults; an unknown user is an
    error rather than a fabricated control record.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(blobs.doc->>'hosted_runtime_mode','resident_cli'), "
            "       COALESCE(state.hosted_runtime_state,'resident'), "
            "       COALESCE(state.runtime_generation,1) "
            "FROM users "
            "LEFT JOIN user_blobs AS blobs "
            "  ON blobs.user_id=users.user_id AND blobs.kind='model_api_runtime' "
            "LEFT JOIN v2_runtime_state AS state ON state.user_id=users.user_id "
            "WHERE users.user_id=%s",
            (user_id,),
        ).fetchone()
    if row is None:
        raise ValueError("unknown user runtime control")
    return str(row[0]), str(row[1]), int(row[2])


def get_runtime_generation(user_id: str) -> int:
    """Current monotonic runtime generation for the user. Lazily initializes the
    row at (resident, 1) on first read for a known user; returns 0 for an unknown
    user (no users row)."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_runtime_state (user_id) "
                "SELECT %s WHERE EXISTS (SELECT 1 FROM users u WHERE u.user_id = %s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id, user_id),
            )
            cur.execute(
                "SELECT runtime_generation FROM v2_runtime_state WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def advance_runtime_state(user_id: str, *, from_state: str, to_state: str) -> int | None:
    """CAS the cutover state resident<->draining<->v2 and bump generation by 1,
    atomically, only if the row is still in from_state. Returns the NEW generation,
    or None if the from_state no longer holds (lost race) — callers must treat None
    as 'someone else moved the machine; re-read', never as success. Also refuses an
    illegal transition (returns None without touching the row)."""
    from model_api_runtime.v2 import cutover
    if not cutover.is_valid_transition(from_state, to_state):
        return None
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            # Lazy-init the row at the default (resident, generation 1) so the very
            # FIRST cutover for a user works — without this the CAS below matches no
            # row and the transition silently no-ops (a user could never leave
            # resident). The from_state guard on the UPDATE still enforces legality,
            # so initializing to resident never lets an illegal start-state through.
            cur.execute(
                "INSERT INTO v2_runtime_state (user_id) "
                "SELECT %s WHERE EXISTS (SELECT 1 FROM users u WHERE u.user_id = %s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id, user_id),
            )
            cur.execute(
                "UPDATE v2_runtime_state "
                "SET hosted_runtime_state = %s, "
                "    runtime_generation = runtime_generation + 1, "
                "    updated_at = now() "
                "WHERE user_id = %s AND hosted_runtime_state = %s "
                "RETURNING runtime_generation",
                (to_state, user_id, from_state),
            )
            row = cur.fetchone()
    return int(row[0]) if row else None


def effect_enqueue(
    effect_id,
    user_id,
    job_id,
    effect_type,
    expected_generation,
    payload,
    *,
    input_frontier_seq: int | None = None,
) -> bool:
    """Insert one row into the generation-fenced effect outbox (spec A4). The
    ON CONFLICT (effect_id) DO NOTHING is the idempotency guarantee: re-enqueuing
    the same logical effect (same job_id/effect_type/ordinal, or same
    generation/effect_type/key for control-plane effects) is a no-op. Returns
    True if this call actually inserted the row, False if it already existed."""
    import json
    if input_frontier_seq is not None and (
        type(input_frontier_seq) is not int or input_frontier_seq < 0
    ):
        raise ValueError(
            "input_frontier_seq must be a non-negative integer or None"
        )
    frontier_seq = input_frontier_seq
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id,user_id,job_id,effect_type,expected_generation,payload,"
                " input_frontier_seq) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (effect_id) DO NOTHING",
                (effect_id, user_id, job_id, effect_type, int(expected_generation),
                 json.dumps(payload), frontier_seq),
            )
            inserted = cur.rowcount == 1
    return inserted


def effect_pending(user_id, *, due_prefix_only: bool = False) -> list[dict]:
    """Pending effects for a user in durable insertion order.

    ``due_prefix_only`` returns only the consecutive due prefix.  A failed head
    effect therefore preserves ordering and its exponential backoff instead of
    letting an eager turn-boundary drain retry it (or skip past it) immediately.
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            if due_prefix_only:
                cur.execute(
                    "WITH ordered AS ("
                    " SELECT effect_id,job_id,effect_type,expected_generation,payload,"
                    "        enqueue_seq,"
                    "        bool_and(next_attempt_at <= now()) OVER ("
                    "          ORDER BY enqueue_seq ROWS BETWEEN UNBOUNDED PRECEDING "
                    "          AND CURRENT ROW"
                    "        ) AS prefix_due "
                    " FROM v2_effect_outbox "
                    " WHERE user_id=%s "
                    "AND status IN ('pending','pending_fenced_v1')"
                    ") SELECT effect_id,job_id,effect_type,expected_generation,payload "
                    "FROM ordered WHERE prefix_due ORDER BY enqueue_seq",
                    (user_id,),
                )
            else:
                cur.execute(
                    "SELECT effect_id, job_id, effect_type, expected_generation, payload "
                    "FROM v2_effect_outbox WHERE user_id=%s "
                    "AND status IN ('pending','pending_fenced_v1') "
                    "ORDER BY enqueue_seq ASC",
                    (user_id,),
                )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows


def effect_pending_users(*, limit: int = 500) -> list[str]:
    """Users with pending outbox work, oldest effect first.

    This is the parent-process reconciliation work list.  Per-user application
    still re-reads and locks every row in ``apply_pending_effects``; this query
    is only a bounded hint and is safe to race with normal end-of-turn drains.
    """
    bounded_limit = max(1, min(int(limit), 5000))
    with get_pool().connection() as conn:
        rows = conn.execute(
            "WITH oldest AS ("
            " SELECT DISTINCT ON (user_id) user_id,enqueue_seq,next_attempt_at "
            " FROM v2_effect_outbox "
            " WHERE status IN ('pending','pending_fenced_v1') "
            " ORDER BY user_id,enqueue_seq"
            ") SELECT user_id FROM oldest WHERE next_attempt_at <= now() "
            "ORDER BY enqueue_seq ASC LIMIT %s",
            (bounded_limit,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def effect_mark(effect_id, status, *, error="") -> None:
    """Flip an outbox row's terminal status (applied|discarded). `error` is
    recorded on failure paths; callers that don't have one pass the default."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            if status == "applied":
                cur.execute(
                    "UPDATE v2_effect_outbox SET status=%s, applied_at=now(), "
                    "last_error=%s WHERE effect_id=%s",
                    (status, error, effect_id),
                )
            else:
                cur.execute(
                    "UPDATE v2_effect_outbox SET status=%s, last_error=%s "
                    "WHERE effect_id=%s",
                    (status, error, effect_id),
                )


def _effect_record_error_on_cursor(
    cur,
    effect_id: str,
    error: str,
    *,
    reconciliation_required: bool = False,
    max_attempts: int = 8,
) -> None:
    """Record one sanitized apply failure on the caller's transaction."""
    cur.execute(
        "UPDATE v2_effect_outbox "
        "SET last_error=%s, attempt_count=attempt_count+1, "
        "    last_attempt_at=now(), "
        "    next_attempt_at=now() + "
        "      (LEAST(3600, 15 * (1 << LEAST(attempt_count, 8))) * interval '1 second'), "
        "    status=CASE WHEN %s OR attempt_count+1 >= %s "
        "                THEN 'needs_reconciliation' ELSE status END "
        "WHERE effect_id=%s "
        "AND status IN ('pending','pending_fenced_v1')",
        (
            str(error)[:256],
            bool(reconciliation_required),
            max(1, int(max_attempts)),
            effect_id,
        ),
    )


def effect_record_error(
    effect_id: str,
    error: str,
    *,
    reconciliation_required: bool = False,
    max_attempts: int = 8,
) -> None:
    """Record one sanitized apply failure without terminalizing the effect.

    Transient failures remain in their original pending state with exponential
    sweeper backoff. Fenced replies deliberately retain
    ``pending_fenced_v1`` so a pre-0041 worker can never seize them during a
    rolling deploy.
    Ambiguous delivery, or a poison row that reaches ``max_attempts``, moves to
    explicit ``needs_reconciliation`` so it cannot hot-loop or block later
    effects forever. Only the caller-provided, already-sanitized summary is
    stored; raw exception text can contain user content or provider credentials
    and must never be passed here.
    """
    with get_pool().connection() as conn:
        _effect_record_error_on_cursor(
            conn,
            effect_id,
            error,
            reconciliation_required=reconciliation_required,
            max_attempts=max_attempts,
        )


def effect_outbox_health() -> dict:
    """Non-sensitive operator counters for pending/manual effect delivery."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT "
            "  count(*) FILTER "
            "    (WHERE status IN ('pending','pending_fenced_v1')), "
            "  count(*) FILTER (WHERE status='needs_reconciliation'), "
            "  COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at) FILTER "
            "    (WHERE status IN "
            "      ('pending','pending_fenced_v1','needs_reconciliation')))), 0) "
            "FROM v2_effect_outbox"
        ).fetchone()
    return {
        "pending": int(row[0] or 0),
        "needs_reconciliation": int(row[1] or 0),
        "oldest_unresolved_age_sec": float(row[2] or 0.0),
    }


class EffectDeliveryUncertainError(RuntimeError):
    """A prior process claimed an effect but never recorded completion.

    The durable write may or may not have landed, so automatic replay would
    choose blindly between loss and duplication. Callers must fail visibly and
    move the outbox row to explicit manual reconciliation.
    """


class EffectTerminalError(RuntimeError):
    """A sink hit a DETERMINISTIC, non-retryable failure applying this effect.

    Retrying a deterministic capability failure (e.g. a 4xx like 409
    ``identity_not_initialized`` — the write cannot succeed no matter how many
    times it runs) only wedges the user's conversation: the reconcile sweeper
    loops forever on a dead effect and the turn/job never settles. A sink raises
    this to tell the outbox to mark the effect ``discarded`` (terminal, no retry)
    instead of scheduling another attempt. It is NOT a delivery-uncertain state:
    the write provably did not land, so dropping it loses nothing.
    """


def effect_sink_claim(effect_id: str) -> bool:
    """Begin the generic sink delivery protocol for ``effect_id``.

    Returns ``True`` only for a brand-new claim.  A replay of a row whose sink
    has recorded ``completed`` returns ``False`` and safely no-ops.  A replay
    of an unresolved ``claimed`` row raises
    :class:`EffectDeliveryUncertainError`: a hard process death may have
    happened either before or after the real durable write, so neither an
    automatic retry nor an automatic success is safe.

    Successful sinks MUST call :func:`effect_sink_complete` after their durable
    write.  Ordinary write failures MUST call :func:`effect_sink_release`.
    Do not add a time-based claim sweeper: age cannot tell whether the target
    write landed and releasing an old claim can duplicate external effects.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO v2_effect_sink_applied (effect_id, claim_state) "
                    "VALUES (%s, 'claimed') ON CONFLICT (effect_id) DO NOTHING",
                    (effect_id,),
                )
                if cur.rowcount == 1:
                    return True
                cur.execute(
                    "SELECT claim_state FROM v2_effect_sink_applied "
                    "WHERE effect_id=%s FOR UPDATE",
                    (effect_id,),
                )
                row = cur.fetchone()
                if row and row[0] == "completed":
                    return False
                if row and row[0] == "claimed":
                    raise EffectDeliveryUncertainError(
                        "effect delivery uncertain after interrupted sink claim"
                    )
                raise RuntimeError("effect sink claim row has invalid state")


def effect_sink_complete(effect_id: str) -> None:
    """Record that the durable sink write for ``effect_id`` completed.

    This transition is idempotent.  Importantly, callers invoke it *outside*
    the write-failure handler: if this bookkeeping write fails after the real
    target write landed, the claim must remain unresolved rather than being
    released and risking a duplicate on replay.
    """
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE v2_effect_sink_applied "
                    "SET claim_state='completed', completed_at=COALESCE(completed_at, now()) "
                    "WHERE effect_id=%s AND claim_state='claimed'",
                    (effect_id,),
                )
                if cur.rowcount == 1:
                    return
                cur.execute(
                    "SELECT claim_state FROM v2_effect_sink_applied WHERE effect_id=%s",
                    (effect_id,),
                )
                row = cur.fetchone()
                if row and row[0] == "completed":
                    return
                raise RuntimeError("cannot complete an effect without an active claim")


def effect_sink_release(effect_id: str) -> None:
    """Undo only an unresolved claim after the durable write raised.

    Completed rows are deliberately retained, so an accidental late release
    cannot erase the replay guard for a write that already succeeded.
    """
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_effect_sink_applied "
            "WHERE effect_id=%s AND claim_state='claimed'",
            (effect_id,),
        )


# --------------------------------------------------------------------------- #
# Dual-runtime canary allowlist (spec 2026-07-21). Read by the reconciler and
# the admin surface ONLY — the send hot path reads the per-user fence, never
# this table, so an allowlist outage can not affect message delivery.
# --------------------------------------------------------------------------- #

_RUNTIME_ALLOWLIST_DESIRED = frozenset({"v2", "resident"})


def upsert_runtime_allowlist(user_id: str, desired: str, *,
                             updated_by: str = "", note: str = "") -> None:
    if desired not in _RUNTIME_ALLOWLIST_DESIRED:
        raise ValueError(f"desired must be one of {sorted(_RUNTIME_ALLOWLIST_DESIRED)}")
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO v2_user_allowlist (user_id, desired, updated_at, updated_by, note)
            VALUES (%s, %s, now(), %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                desired = EXCLUDED.desired,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by,
                note = EXCLUDED.note
            """,
            (user_id, desired, updated_by, note),
        )


def delete_runtime_allowlist(user_id: str) -> bool:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM v2_user_allowlist WHERE user_id = %s", (user_id,))
        return cur.rowcount > 0


def list_runtime_allowlist() -> list[dict]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT user_id, desired, updated_at, updated_by, note "
            "FROM v2_user_allowlist ORDER BY user_id").fetchall()
    return [
        {"user_id": r[0], "desired": r[1],
         "updated_at": r[2].isoformat() if r[2] else None,
         "updated_by": r[3], "note": r[4]}
        for r in rows
    ]


def get_runtime_allowlist_map() -> dict[str, str]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT user_id, desired FROM v2_user_allowlist").fetchall()
    return {r[0]: r[1] for r in rows}
