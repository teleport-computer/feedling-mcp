"""Real PostgreSQL regression guards for lossless partition maintenance."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import nullcontext
from datetime import date, timedelta
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "backend"))
from admin import trace_events_partitions as partitions  # noqa: E402


@pytest.fixture
def trace_db():
    # Isolated schema in the suite's disposable database, never a live DSN.
    schema = "trace_preserve_" + uuid.uuid4().hex
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute("CREATE TABLE trace_events (id bigserial, ts timestamptz NOT NULL, "
                     "payload text, PRIMARY KEY(id,ts)) PARTITION BY RANGE(ts)")
        conn.execute("CREATE INDEX ON trace_events(ts)")
        conn.execute("CREATE INDEX ON trace_events(payload,ts DESC) WHERE payload IS NOT NULL")
        conn.execute("CREATE TABLE trace_events_default PARTITION OF trace_events DEFAULT")
        try:
            yield conn
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def insert(conn, day, payload):
    conn.execute("INSERT INTO trace_events(ts,payload) VALUES (%s,%s)",
                 (partitions._bounds(day)[0], payload))


def snapshot(conn):
    rows = conn.execute("SELECT id,ts,payload,tableoid::regclass::text "
                        "FROM trace_events ORDER BY id").fetchall()
    children = conn.execute("SELECT inhrelid::regclass::text FROM pg_inherits "
                            "WHERE inhparent='trace_events'::regclass ORDER BY 1").fetchall()
    return rows, children


def test_preserves_old_partition_and_every_stranded_row(trace_db):
    conn = trace_db
    today = date(2026, 11, 17)
    old = today - timedelta(days=1000)
    partitions._create_partition(conn, old)
    insert(conn, old, "old-partition")
    for day in [old - timedelta(days=2), today - timedelta(days=60), today]:
        insert(conn, day, "stranded:" + day.isoformat())
    before = [r[:3] for r in snapshot(conn)[0]]
    report = partitions.maintain(conn, today=today, future_days=2)
    assert [r[:3] for r in snapshot(conn)[0]] == before
    assert conn.execute("SELECT count(*) FROM trace_events_default").fetchone()[0] == 0
    assert report["moved_rows"] == 3
    assert all(r[3] != "trace_events_default" for r in snapshot(conn)[0])
    first = snapshot(conn)
    again = partitions.maintain(conn, today=today, future_days=2)
    assert again["moved_rows"] == 0
    assert snapshot(conn) == first


def test_sparse_dates_do_not_prebuild_empty_historical_gaps(trace_db):
    today = date(2026, 11, 17)
    days = [today - timedelta(days=10000 + 100 * n) for n in range(12)]
    for d in days:
        insert(trace_db, d, d.isoformat())
    partitions.maintain(trace_db, today=today, future_days=2)
    names = {r[0] for r in snapshot(trace_db)[1]}
    assert names == {"trace_events_default"} | {
        partitions._name(d) for d in days + [today + timedelta(days=n) for n in range(3)]
    }
    assert len(snapshot(trace_db)[0]) == len(days)


def test_failed_attach_rolls_back_rows_and_ddl(trace_db):
    today = date(2026, 11, 17)
    insert(trace_db, today, "must-survive")
    # A real PostgreSQL CHECK failure after the row relocation, during ATTACH.
    trace_db.execute("CREATE FUNCTION refuse_attach() RETURNS event_trigger "
                     "LANGUAGE plpgsql AS $$ BEGIN "
                     "IF TG_TAG='ALTER TABLE' THEN RAISE EXCEPTION 'injected attach failure'; "
                     "END IF; END $$")
    trigger = "trace_attach_" + uuid.uuid4().hex
    trace_db.execute(sql.SQL("CREATE EVENT TRIGGER {} ON ddl_command_end "
                            "EXECUTE FUNCTION refuse_attach()").format(sql.Identifier(trigger)))
    before = snapshot(trace_db)
    try:
        with pytest.raises(psycopg.Error, match="injected attach failure"):
            partitions.maintain(trace_db, today=today, future_days=1)
        assert snapshot(trace_db) == before
    finally:
        trace_db.execute(sql.SQL("DROP EVENT TRIGGER {}").format(sql.Identifier(trigger)))


def test_each_day_commits_and_releases_parent_lock(trace_db, monkeypatch):
    today = date(2026, 11, 17)
    insert(trace_db, today - timedelta(days=90), "first")
    insert(trace_db, today, "second")
    schema = trace_db.execute("SELECT current_schema()").fetchone()[0]
    actual = partitions._repair_day
    seen = []
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as observer:
        observer.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

        def checked(conn, day, **kwargs):
            result = actual(conn, day, **kwargs)
            # Another connection must see committed moved rows AND acquire the
            # parent lock between batches (not just a nested savepoint).
            with observer.transaction():
                observer.execute("LOCK TABLE ONLY trace_events IN ACCESS EXCLUSIVE MODE NOWAIT")
                seen.append(observer.execute("SELECT count(*) FROM trace_events").fetchone()[0])
            return result

        monkeypatch.setattr(partitions, "_repair_day", checked)
        partitions.maintain(trace_db, today=today, future_days=1)
    assert seen == [2, 2, 2]


def test_writer_contention_times_out_without_changing_data(trace_db):
    today = date(2026, 11, 17)
    insert(trace_db, today, "preserve")
    before = snapshot(trace_db)
    schema = trace_db.execute("SELECT current_schema()").fetchone()[0]
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as blocker:
        blocker.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        with blocker.transaction():
            blocker.execute("LOCK TABLE ONLY trace_events IN ROW EXCLUSIVE MODE")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                partitions.maintain(trace_db, today=today, future_days=0, lock_timeout_ms=50)
    assert snapshot(trace_db) == before
    # Failure also releases the session advisory lock, allowing a fresh owner.
    partitions.maintain(trace_db, today=today, future_days=0)
    assert len(snapshot(trace_db)[0]) == 1


def test_outer_transaction_cannot_silently_defeat_batch_commits(trace_db):
    with trace_db.transaction():
        trace_db.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="no outer transaction"):
            partitions.maintain(trace_db)


def test_copy_does_not_block_writes_or_resurrect_cleared_rows(trace_db, monkeypatch):
    today = date(2026, 11, 17)
    insert(trace_db, today, "clear-me")
    insert(trace_db, today, "update-me")
    schema = trace_db.execute("SELECT current_schema()").fetchone()[0]
    copy = partitions._copy_day
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as writer:
        writer.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        writer.execute("SET lock_timeout='200ms'")

        def concurrent(conn, day):
            copy(conn, day)
            writer.execute("DELETE FROM trace_events WHERE payload='clear-me'")
            writer.execute("UPDATE trace_events SET payload='updated' WHERE payload='update-me'")
            insert(writer, today, "new-row")

        monkeypatch.setattr(partitions, "_copy_day", concurrent)
        report = partitions.maintain(trace_db, today=today, future_days=0)
    assert report["moved_rows"] == 2
    assert {r[2] for r in snapshot(trace_db)[0]} == {"updated", "new-row"}


def test_repaired_partition_indexes_owner_and_app_parent_permissions(trace_db):
    today = date(2026, 11, 17)
    old = today - timedelta(days=1)
    partitions._create_partition(trace_db, old)
    insert(trace_db, today, "row")
    partitions.maintain(trace_db, today=today, future_days=0)

    def shape(table):
        return trace_db.execute(
            "SELECT indisprimary,indisunique,indkey::text,indclass::text,"
            "pg_get_expr(indpred,indrelid) FROM pg_index "
            "WHERE indrelid=%s::regclass ORDER BY 1,2,3,4,5", (table,)
        ).fetchall()

    assert len(shape(partitions._name(today))) == 3
    assert shape(partitions._name(today)) == shape(partitions._name(old))
    assert trace_db.execute("SELECT relowner,relacl FROM pg_class WHERE oid=%s::regclass",
                            (partitions._name(today),)).fetchone() == trace_db.execute(
        "SELECT relowner,relacl FROM pg_class WHERE oid=%s::regclass",
        (partitions._name(old),)).fetchone()
    role = "trace_app_" + uuid.uuid4().hex
    schema = trace_db.execute("SELECT current_schema()").fetchone()[0]
    trace_db.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
    try:
        trace_db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)))
        trace_db.execute(sql.SQL("GRANT SELECT,INSERT ON trace_events TO {}").format(sql.Identifier(role)))
        trace_db.execute(sql.SQL("GRANT USAGE ON SEQUENCE trace_events_id_seq TO {}").format(sql.Identifier(role)))
        with trace_db.transaction():
            trace_db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            insert(trace_db, today, "app-insert")
            assert trace_db.execute("SELECT count(*) FROM trace_events").fetchone()[0] == 2
    finally:
        trace_db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        trace_db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_scheduled_large_backlog_never_copies_or_moves_rows(trace_db, monkeypatch):
    today = date(2026, 11, 17)
    trace_db.execute("INSERT INTO trace_events(ts,payload) "
                     "SELECT %s, n::text FROM generate_series(1,10001) n",
                     (partitions._bounds(today)[0],))
    before = [r[:3] for r in snapshot(trace_db)[0]]
    actual = partitions._repair_day

    def no_copy(*args):
        raise AssertionError("scheduled maintenance must not copy backlog")

    def bounded(conn, day, **kwargs):
        assert kwargs["repair_default"] is False
        assert kwargs["lock_timeout_ms"] <= 100
        assert kwargs["statement_timeout_ms"] <= 250
        return actual(conn, day, **kwargs)

    monkeypatch.setattr(partitions, "_copy_day", no_copy)
    monkeypatch.setattr(partitions, "_repair_day", bounded)
    report = partitions.maintain(trace_db, today=today, future_days=1, repair_default=False)
    assert report["large_backlog"] is True
    assert report["mode"] == "future_only"
    assert report["moved_rows"] == 0
    assert report["default_rows_after"] == 10001
    assert {"day": today.isoformat(), "reason": "occupied_date"} in report["deferred"]
    assert [r[:3] for r in snapshot(trace_db)[0]] == before


def test_future_only_defers_lock_contention_instead_of_waiting_long(trace_db):
    today = date(2026, 11, 17)
    schema = trace_db.execute("SELECT current_schema()").fetchone()[0]
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as blocker:
        blocker.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        with blocker.transaction():
            blocker.execute("LOCK TABLE ONLY trace_events IN ROW EXCLUSIVE MODE")
            report = partitions.maintain(trace_db, today=today, future_days=0, repair_default=False)
    assert report["created"] == []
    assert report["deferred"] == [{"day": today.isoformat(), "reason": "LockNotAvailable"}]
    assert snapshot(trace_db)[0] == []


@pytest.mark.parametrize("flags,repairs", [([], False), (["--scheduled"], False),
                                         (["--repair-default"], True)])
def test_cli_requires_explicit_repair_window(trace_db, monkeypatch, capsys, flags, repairs):
    import json

    today = partitions._beijing_today()
    insert(trace_db, today, "manual-window-only")
    monkeypatch.setattr(sys, "argv", ["trace-maintain", "--future-days", "0", *flags])
    monkeypatch.setattr(partitions, "_migration_database_url", lambda: "unused-local-fixture")
    monkeypatch.setattr(partitions.psycopg, "connect", lambda *a, **k: nullcontext(trace_db))
    if repairs:
        assert partitions.main() == 0
    else:
        with pytest.raises(RuntimeError, match="explicit repair window required"):
            partitions.main()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == ("repair" if repairs else "future_only")
    assert report["moved_rows"] == int(repairs)
    assert len(snapshot(trace_db)[0]) == 1


def test_workflows_use_lossless_owner_command_and_guard_production():
    import yaml

    root = Path(__file__).parents[1]
    scheduled = yaml.safe_load((root / ".github/workflows/tee-trace-partitions.yml").read_text())
    triggers = scheduled.get("on", scheduled.get(True))
    assert triggers["schedule"] == [{"cron": "37 19 * * *"}]
    assert scheduled["jobs"]["maintain"]["strategy"]["matrix"]["environment"] == ["test", "prod"]
    steps = scheduled["jobs"]["maintain"]["steps"]
    assert steps[0]["env"]["PROD_ENABLED"] == "${{ vars.TRACE_PARTITIONS_PROD_ENABLED }}"
    assert '"$PROD_ENABLED" != true' in steps[0]["run"]
    for path in ["tee-migrate.yml", "tee-trace-partitions.yml", "rds-trace-partitions.yml"]:
        workflow = (root / ".github/workflows" / path).read_text()
        assert "python -m admin.trace_events_partitions" in workflow
        assert "--retention-days" not in workflow


@pytest.mark.parametrize("workflow", ["tee-migrate.yml", "tee-trace-partitions.yml", "rds-trace-partitions.yml"])
@pytest.mark.parametrize("signature,marker,expected", [
    ("conn, retention_days=30, future_days=60", "", 1),
    ("conn, retention_days=30", "LOSSLESS_PARTITION_MAINTENANCE=True", 1),
    ("conn, future_days=60", "LOSSLESS_PARTITION_MAINTENANCE=True", 0),
])
def test_workflow_executes_fail_closed_guard_on_checked_out_module(
    workflow, signature, marker, expected, tmp_path,
):
    import yaml
    path = Path(__file__).parents[1] / ".github/workflows" / workflow
    jobs = yaml.safe_load(path.read_text())["jobs"]
    steps = next(iter(jobs.values()))["steps"]
    guard = next(s for s in steps if s.get("name") == "Require lossless trace maintenance code")
    package = tmp_path / "admin"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "trace_events_partitions.py").write_text(f"{marker}\ndef maintain({signature}): pass\n")
    result = subprocess.run(["bash", "-e", "-c", guard["run"]],
                            env={**os.environ, "PYTHONPATH": str(tmp_path),
                                 "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stderr
    if expected:
        assert "checked-out trace maintenance is not lossless" in result.stderr

