"""T653: user_logs.duration_sec column + covering partial index for the admin
fleet app_usage aggregate (alembic 0112 / alembic_tee 0047).

Three things must stay true together, or the index-only scan silently
degrades back to the 2.6 s heap walk that timed out prod:
  1. every ``INSERT INTO user_logs`` writer fills the column;
  2. the backfill/repair statement is idempotent and matches the writer rule;
  3. the read SQL keeps the old CASE semantics (NULL → 0, still a session).
"""
from pathlib import Path
import importlib
import re
import sys
import time
import uuid

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402  # noqa: E402

BACKEND = Path(__file__).parent.parent / "backend"
RDS_MIGRATION = BACKEND / "alembic" / "versions" / "0112_user_logs_duration_sec.py"
TEE_MIGRATION = BACKEND / "alembic_tee" / "versions" / "0047_user_logs_duration_sec.py"
INDEX_NAME = "ix_user_logs_app_session_end_usage"

# The exact expression the pre-T653 SQL used; kept here as the semantic oracle.
OLD_CASE_SQL = """
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
"""


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 1. every writer names the column -------------------------------------

def _insert_statements() -> list[tuple[str, str]]:
    """Collect the column list of every INSERT INTO user_logs in backend/.

    Only the repo's actual shape is folded (adjacent double-quoted literals
    split across lines). Any other concatenation shape leaves the column
    list unparsed and therefore *fails* the guard — fail-closed on purpose:
    a new writer written in a form this scan does not understand must be
    looked at, not silently trusted.
    """
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "alembic" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"INSERT INTO user_logs", text):
            window = text[match.start(): match.start() + 600]
            # Collapse Python string concatenation to the SQL that is sent.
            sql = re.sub(r'"\s*\n\s*"', "", window)
            head = sql.split("VALUES", 1)[0]
            found.append((f"{path.relative_to(BACKEND)}:{text.count(chr(10), 0, match.start()) + 1}", head))
    return found


def test_every_user_logs_insert_names_duration_sec():
    statements = _insert_statements()
    # Sanity: the scan must actually see the fleet of writers, not an empty set.
    assert len(statements) >= 17, statements
    missing = [where for where, head in statements if "duration_sec" not in head]
    assert not missing, f"user_logs writers without duration_sec: {missing}"


@pytest.mark.parametrize("stream,doc,expected", [
    ("tracking_events", {"type": "app_session_end", "payload": {"duration_sec": 42}}, 42),
    ("tracking_events", {"type": "app_session_end", "payload": {"duration_sec": "17"}}, 17),
    ("tracking_events", {"type": "app_session_end", "payload": {"duration_sec": 9999999999}}, 9999999999),
    ("tracking_events", {"type": "app_session_end", "payload": {"duration_sec": 12.0}}, None),
    ("tracking_events", {"type": "app_session_end", "payload": {"duration_sec": "abc"}}, None),
    ("tracking_events", {"type": "app_session_end", "payload": {"duration_sec": 12345678901}}, None),
    ("tracking_events", {"type": "app_session_end", "payload": {}}, None),
    ("tracking_events", {"type": "app_session_end", "payload": "nope"}, None),
    ("tracking_events", {"type": "app_session_end"}, None),
    ("tracking_events", {"type": "app_session_start", "payload": {"duration_sec": 42}}, None),
    ("device_events", {"type": "app_session_end", "payload": {"duration_sec": 42}}, None),
])
def test_writer_rule_matches_the_sql_regex(stream, doc, expected):
    assert db._user_log_duration_sec(stream, doc) == expected


# --- 2. schema on both chains, backfill idempotent --------------------------

def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s", (table,)
            ).fetchall()
        }


def _indexes(dsn: str, table: str) -> dict[str, str]:
    with psycopg.connect(dsn) as conn:
        return dict(conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND tablename=%s", (table,)
        ).fetchall())


@pytest.mark.parametrize("dsn_env", ["DATABASE_URL", "TEE_DATABASE_URL"])
def test_column_and_covering_index_exist_on_both_chains(dsn_env):
    import os
    dsn = os.environ[dsn_env]
    assert "duration_sec" in _columns(dsn, "user_logs")
    indexes = _indexes(dsn, "user_logs")
    assert INDEX_NAME in indexes, sorted(indexes)
    indexdef = indexes[INDEX_NAME]
    assert "(user_id, ts) INCLUDE (duration_sec)" in indexdef
    assert "stream = 'tracking_events'" in indexdef
    assert "app_session_end" in indexdef


def test_rds_and_tee_migrations_carry_identical_ddl():
    rds = _load_migration(RDS_MIGRATION)
    tee = _load_migration(TEE_MIGRATION)
    assert rds.ADD_COLUMN_SQL == tee.ADD_COLUMN_SQL
    assert rds.CREATE_INDEX_SQL == tee.CREATE_INDEX_SQL
    assert rds.BACKFILL_SQL == tee.BACKFILL_SQL
    assert rds.VACUUM_SQL == tee.VACUUM_SQL == "VACUUM (ANALYZE) user_logs"
    assert rds.INDEX_NAME == tee.INDEX_NAME == INDEX_NAME
    # The index predicate must be the read SQL's predicate, or the planner
    # cannot prove the WHERE and drops back to a heap scan.
    read_sql = Path(db.__file__).read_text(encoding="utf-8")
    assert "COALESCE(SUM(duration_sec), 0)::bigint AS foreground_sec" in read_sql
    assert "AND stream = 'tracking_events'\n                      AND doc->>'type' = 'app_session_end'" in read_sql


def _raw_session_end(uid: str, ts: float, payload_value, *, duration_sec):
    """Write a row the way pre-T653 code did: no duration_sec value."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_logs (user_id, stream, ts, item_key, doc, duration_sec) "
            "VALUES (%s, 'tracking_events', %s, %s, %s, %s)",
            (uid, ts, f"raw-{ts}", psycopg.types.json.Jsonb(
                {"type": "app_session_end", "payload": {"duration_sec": payload_value}}
            ), duration_sec),
        )


def test_backfill_is_idempotent_and_matches_writer_rule():
    rds = _load_migration(RDS_MIGRATION)
    uid = f"usr_t653_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    now = time.time()
    _raw_session_end(uid, now - 3, 7, duration_sec=None)        # old-writer row
    _raw_session_end(uid, now - 2, "abc", duration_sec=None)    # regex-fail row
    _raw_session_end(uid, now - 1, 5, duration_sec=99)          # already filled: untouched
    _raw_session_end(uid, now - 0.5, "12345678901", duration_sec=None)  # 11 digits: outside the rule
    with db.get_pool().connection() as conn:
        first = conn.execute(rds.BACKFILL_SQL).rowcount
        second = conn.execute(rds.BACKFILL_SQL).rowcount
        rows = conn.execute(
            "SELECT duration_sec FROM user_logs WHERE user_id=%s ORDER BY ts", (uid,)
        ).fetchall()
    assert [r[0] for r in rows] == [7, None, 99, None]
    assert (first, second) == (1, 0)


# --- 3. read semantics ----------------------------------------------------

def test_snapshot_app_usage_matches_old_case_sql_and_window_is_repairable():
    rds = _load_migration(RDS_MIGRATION)
    uid = f"usr_t653_{uuid.uuid4().hex[:12]}"
    seed_user(uid)
    now = time.time()
    assert db.log_append(uid, "tracking_events",
                         {"type": "app_session_end", "payload": {"duration_sec": 42}}, ts=now - 30)
    assert db.log_append(uid, "tracking_events",
                         {"type": "app_session_end", "payload": {"duration_sec": "abc"}}, ts=now - 20)
    assert db.log_append(uid, "tracking_events",
                         {"type": "app_session_start", "payload": {"duration_sec": 5}}, ts=now - 15)
    assert db.log_append(uid, "device_events", {"type": "x", "payload": {"duration_sec": 5}}, ts=now - 14)

    def old_reference():
        with db.get_pool().connection() as conn:
            row = conn.execute(OLD_CASE_SQL, ([uid],)).fetchone()
        return {"foreground_sec": int(row[1]), "sessions": int(row[2]), "last_at": row[3]}

    def new_reading():
        return db.admin_data_track_snapshot([uid])[uid]["app_usage"]

    assert new_reading() == old_reference() == {"foreground_sec": 42, "sessions": 2, "last_at": now - 20}

    # A row appended by pre-T653 code (no column value) is the documented
    # switch-over window: counted as a session, its duration reads 0 until the
    # idempotent backfill runs, after which both readings agree again.
    _raw_session_end(uid, now - 10, 8, duration_sec=None)
    assert new_reading() == {"foreground_sec": 42, "sessions": 3, "last_at": now - 10}
    assert old_reference() == {"foreground_sec": 50, "sessions": 3, "last_at": now - 10}
    with db.get_pool().connection() as conn:
        conn.execute(rds.BACKFILL_SQL)
    assert new_reading() == old_reference() == {"foreground_sec": 50, "sessions": 3, "last_at": now - 10}


def test_admin_lease_turns_jit_off_and_resets_it():
    with db._admin_data_track_connection() as conn:
        assert conn.execute("SHOW jit").fetchone()[0] == "off"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5s"
    with db.get_pool().connection() as conn:
        # RESET happened before the connection went back to the pool: the
        # session value equals the server's reset value, whatever it is.
        jit_now, jit_reset = conn.execute(
            "SELECT current_setting('jit'), reset_val FROM pg_settings WHERE name='jit'"
        ).fetchone()
        assert jit_now == jit_reset
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "0"


@pytest.mark.parametrize("path", [RDS_MIGRATION, TEE_MIGRATION])
def test_migration_runs_every_step_as_its_own_autocommit_statement(path):
    """ADD COLUMN's ACCESS EXCLUSIVE lock must not be held across the backfill,
    and CREATE INDEX CONCURRENTLY / VACUUM cannot run inside a transaction —
    so upgrade() must do all four inside op.get_context().autocommit_block()."""
    import ast
    module = ast.parse(path.read_text(encoding="utf-8"))
    upgrade = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    blocks = [n for n in ast.walk(upgrade) if isinstance(n, ast.With)
              and "autocommit_block" in ast.unparse(n.items[0].context_expr)]
    assert len(blocks) == 1
    inside = ast.unparse(blocks[0])
    order = [inside.index(token) for token in ("ADD_COLUMN_SQL", "BACKFILL_SQL", "CREATE_INDEX_SQL", "VACUUM_SQL")]
    assert order == sorted(order), inside
    outside = "\n".join(ast.unparse(stmt) for stmt in upgrade.body if stmt is not blocks[0])
    for token in ("ADD_COLUMN_SQL", "BACKFILL_SQL", "CREATE_INDEX_SQL", "VACUUM_SQL"):
        assert token not in outside, outside
