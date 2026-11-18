"""Real PostgreSQL races between trace maintenance and atomic batch writes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
import os
from pathlib import Path
import re
import runpy
import sys
import threading
import time
import uuid

import psycopg
from psycopg import sql
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'backend'))
import db
from admin import trace_events_partitions as partitions


@pytest.fixture
def race_db(monkeypatch):
    schema = 'trace_retry_' + uuid.uuid4().hex
    dsn = os.environ['DATABASE_URL']
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        migration = runpy.run_path(str(Path(__file__).parents[1] /
                                      'backend/alembic_tee/versions/0033_trace_events.py'))['_UP']
        c.execute(re.sub(r'DO \$\$.*?END \$\$;', '', migration, flags=re.S))
        c.execute('CREATE TABLE user_blobs(user_id text,kind text,doc jsonb,PRIMARY KEY(user_id,kind))')

        class Pool:
            attempts = 0
            pids = []
            after_failure = None

            @contextmanager
            def connection(self):
                self.attempts += 1
                try:
                    with psycopg.connect(dsn, autocommit=True) as w:
                        w.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                        self.pids.append(w.info.backend_pid)
                        yield w
                except psycopg.errors.CheckViolation:
                    if self.after_failure:
                        self.after_failure()
                    raise

        pool = Pool()
        monkeypatch.setattr(db, 'get_pool', lambda: pool)
        try:
            yield c, schema, pool
        finally:
            c.execute('SET search_path TO public')
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def event(day, name):
    return {'ts': partitions._bounds(day)[0].timestamp() + 3600,
            'subsystem': 'agent', 'type': name, 'status': 'ok'}


def test_late_batch_reroutes_after_attach_without_partial_batch_duplicates(race_db):
    c, _, pool = race_db
    today = date(2026, 11, 19)
    old = today - timedelta(days=1)
    lo, hi = partitions._bounds(old)
    target = partitions._name(old)
    partitions._create_partition(c, today)
    c.execute("INSERT INTO trace_events(user_id,ts,subsystem,type,status) "
              "VALUES ('seed',%s,'agent','seed','ok')", (lo,))
    c.execute(sql.SQL('CREATE TABLE {} (LIKE trace_events INCLUDING ALL)').format(sql.Identifier(target)))
    c.execute(sql.SQL('INSERT INTO {} SELECT * FROM trace_events_default').format(sql.Identifier(target)))
    outcome = {}

    def write():
        try:
            outcome['inserted'] = db.insert_trace_events_strict('late-user',
                                      [event(today, 'current'), event(old, 'late')])
        except BaseException as exc:
            outcome['error'] = exc

    th = threading.Thread(target=write)
    try:
        with c.transaction():
            c.execute('LOCK TABLE ONLY trace_events IN SHARE UPDATE EXCLUSIVE MODE')
            c.execute('LOCK TABLE trace_events_default IN ACCESS EXCLUSIVE MODE')
            th.start()
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pool.pids and c.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s "
                        "AND relation='trace_events_default'::regclass AND NOT granted)",
                        (pool.pids[0],),
                    ).fetchone()[0]:
                        break
                    time.sleep(.01)
                else:
                    pytest.fail('writer never reached DEFAULT lock')
                c.execute('DELETE FROM trace_events_default WHERE ts >= %s AND ts < %s', (lo, hi))
                c.execute(sql.SQL('ALTER TABLE trace_events ATTACH PARTITION {} FOR VALUES FROM ({}) TO ({})')
                          .format(sql.Identifier(target), sql.Literal(lo), sql.Literal(hi)))
            finally:
                # Transaction commits before joining the blocked writer.
                pass
    finally:
        if th.ident is not None:
            th.join(timeout=10)
    assert not th.is_alive()
    assert 'error' not in outcome, repr(outcome.get('error'))
    assert outcome['inserted'] == 2
    assert pool.attempts == 2
    assert c.execute("SELECT type,count(*) FROM trace_events WHERE user_id='late-user' "
                     'GROUP BY type ORDER BY type').fetchall() == [('current', 1), ('late', 1)]
    assert c.execute("SELECT tableoid::regclass::text FROM trace_events "
                     "WHERE user_id='late-user' AND type='late'").fetchone()[0] == target


def test_permanent_check_violation_has_one_retry_and_no_partial_rows(race_db):
    c, _, pool = race_db
    c.execute("ALTER TABLE trace_events_default ADD CONSTRAINT reject_bad CHECK(type <> 'bad')")
    today = date(2026, 11, 19)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.insert_trace_events_strict('permanent', [event(today, 'good'), event(today, 'bad')])
    assert pool.attempts == 2
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='permanent'").fetchone()[0] == 0


def test_retry_rechecks_clear_tombstone(race_db):
    c, _, pool = race_db
    c.execute("ALTER TABLE trace_events_default ADD CONSTRAINT reject_bad CHECK(type <> 'bad')")
    today = date(2026, 11, 19)

    def clear_after_rollback():
        c.execute("INSERT INTO user_blobs VALUES ('cleared','v1_flow_trace_enabled', "
                  "jsonb_build_object('cleared_at',%s::double precision))",
                  (partitions._bounds(today + timedelta(days=1))[0].timestamp(),))

    pool.after_failure = clear_after_rollback
    assert db.insert_trace_events_strict('cleared', [event(today, 'bad')]) == 0
    assert pool.attempts == 2
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='cleared'").fetchone()[0] == 0


@pytest.mark.parametrize('past', [True, False])
def test_past_repair_allows_current_partition_writes_but_today_keeps_fence(race_db, past):
    c, schema, _ = race_db
    today = date(2026, 11, 19)
    day = today - timedelta(days=1) if past else today
    if past:
        partitions._create_partition(c, today)
    c.execute("INSERT INTO trace_events(user_id,ts,subsystem,type,status) "
              "VALUES ('seed',%s,'agent','seed','ok')", (partitions._bounds(day)[0],))
    acquired, release = threading.Event(), threading.Event()
    outcome = {}

    class Paused:
        def __init__(self, conn):
            self.conn = conn
            self.info = conn.info

        def transaction(self):
            return self.conn.transaction()

        def execute(self, query, *args, **kwargs):
            answer = self.conn.execute(query, *args, **kwargs)
            if query == 'LOCK TABLE trace_events_default IN ACCESS EXCLUSIVE MODE':
                acquired.set()
                assert release.wait(10)
            return answer

    def repair():
        try:
            with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as owner:
                owner.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                outcome['pid'] = owner.info.backend_pid
                outcome['report'] = partitions.maintain(Paused(owner), today=today, future_days=0)
        except BaseException as exc:
            outcome['error'] = exc

    th = threading.Thread(target=repair)
    th.start()
    try:
        assert acquired.wait(10)
        modes = {x[0] for x in c.execute("SELECT mode FROM pg_locks WHERE pid=%s "
                                       "AND relation='trace_events'::regclass AND granted",
                                       (outcome['pid'],)).fetchall()}
        if past:
            assert 'ShareUpdateExclusiveLock' in modes
            assert 'AccessExclusiveLock' not in modes
        else:
            assert 'AccessExclusiveLock' in modes
        with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as writer:
            writer.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            writer.execute("SET lock_timeout='150ms'")
            if past:
                writer.execute("INSERT INTO trace_events(user_id,ts,subsystem,type,status) "
                               "VALUES ('current',%s,'agent','current','ok')", (partitions._bounds(today)[0],))
            else:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    writer.execute("INSERT INTO trace_events(user_id,ts,subsystem,type,status) "
                                   "VALUES ('current',%s,'agent','current','ok')", (partitions._bounds(today)[0],))
    finally:
        release.set()
        th.join(timeout=10)
    assert not th.is_alive()
    assert 'error' not in outcome, repr(outcome.get('error'))
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='seed'").fetchone()[0] == 1


def test_non_check_write_failure_is_not_retried(race_db):
    c, _, pool = race_db
    c.execute("CREATE FUNCTION refuse_trace() RETURNS trigger LANGUAGE plpgsql AS $$ "
              "BEGIN RAISE EXCEPTION 'not a partition race' USING ERRCODE='40001'; END $$")
    c.execute('CREATE TRIGGER refuse_trace BEFORE INSERT ON trace_events_default '
              'FOR EACH ROW EXECUTE FUNCTION refuse_trace()')
    with pytest.raises(psycopg.errors.SerializationFailure):
        db.insert_trace_events_strict('other-error', [event(date(2026, 11, 19), 'event')])
    assert pool.attempts == 1
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='other-error'").fetchone()[0] == 0


@pytest.mark.parametrize('strict', [True, False])
def test_clear_plans_after_attach_and_removes_moved_rows(race_db, strict):
    c, _, pool = race_db
    old = date(2026, 11, 18)
    lo, hi = partitions._bounds(old)
    target = partitions._name(old)
    c.execute("INSERT INTO trace_events(user_id,ts,subsystem,type,status) "
              "VALUES ('clear-target',%s,'agent','seed','ok')", (lo,))
    c.execute(sql.SQL('CREATE TABLE {} (LIKE trace_events INCLUDING ALL)').format(sql.Identifier(target)))
    c.execute(sql.SQL('INSERT INTO {} SELECT * FROM trace_events_default').format(sql.Identifier(target)))
    outcome = {}

    def clear():
        try:
            if strict:
                outcome['deleted'] = db.clear_trace_events_strict(
                    'clear-target', flag_kind='v1_flow_trace_enabled',
                    cleared_at=hi.timestamp(), enabled_if_missing=True)
            else:
                outcome['deleted'] = db.delete_trace_events_for_user('clear-target')
        except BaseException as exc:
            outcome['error'] = exc

    th = threading.Thread(target=clear)
    try:
        with c.transaction():
            c.execute('LOCK TABLE ONLY trace_events IN SHARE UPDATE EXCLUSIVE MODE')
            c.execute('LOCK TABLE trace_events_default IN ACCESS EXCLUSIVE MODE')
            th.start()
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if pool.pids and c.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s "
                    "AND relation IN ('trace_events'::regclass,'trace_events_default'::regclass) "
                    "AND NOT granted)", (pool.pids[0],),
                ).fetchone()[0]:
                    break
                time.sleep(.01)
            else:
                pytest.fail('clear never waited on maintenance')
            c.execute('DELETE FROM trace_events_default')
            c.execute(sql.SQL('ALTER TABLE trace_events ATTACH PARTITION {} FOR VALUES FROM ({}) TO ({})')
                      .format(sql.Identifier(target), sql.Literal(lo), sql.Literal(hi)))
    finally:
        if th.ident is not None:
            th.join(timeout=10)
    assert not th.is_alive()
    assert 'error' not in outcome, repr(outcome.get('error'))
    assert outcome['deleted'] == 1
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='clear-target'").fetchone()[0] == 0


def test_batch_with_two_past_days_can_survive_two_partition_check_failures(race_db):
    c, _, pool = race_db
    today = date(2026, 11, 19)
    first, second = today - timedelta(days=2), today - timedelta(days=1)
    # Real CHECK failures model two successive DEFAULT route invalidations;
    # the separate ATTACH race test covers PostgreSQL's stale-route mechanism.
    c.execute("ALTER TABLE trace_events_default ADD CONSTRAINT next_attach "
              "CHECK(ts <> TIMESTAMPTZ '2026-11-17 01:00+08')")
    failures = []

    def next_partition():
        failures.append(True)
        c.execute('ALTER TABLE trace_events_default DROP CONSTRAINT next_attach')
        if len(failures) == 1:
            c.execute("ALTER TABLE trace_events_default ADD CONSTRAINT next_attach "
                      "CHECK(ts <> TIMESTAMPTZ '2026-11-18 01:00+08')")

    pool.after_failure = next_partition
    assert db.insert_trace_events_strict('two-days',
        [event(today, 'current'), event(first, 'first'), event(second, 'second')]) == 3
    assert pool.attempts == 3
    assert len(failures) == 2
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='two-days'").fetchone()[0] == 3


def test_clear_waits_for_user_before_parent_to_avoid_queued_fence_deadlock(race_db):
    c, schema, pool = race_db
    day = date(2026, 11, 19)
    partitions._create_partition(c, day)
    outcome = {}

    def clear():
        try:
            outcome['deleted'] = db.clear_trace_events_strict(
                'lock-order', flag_kind='v1_flow_trace_enabled',
                cleared_at=partitions._bounds(day + timedelta(days=1))[0].timestamp(),
                enabled_if_missing=True)
        except BaseException as exc:
            outcome['error'] = exc

    th = threading.Thread(target=clear)
    try:
        with c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended('lock-order',184))")
            th.start()
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if pool.pids and c.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s "
                    "AND locktype='advisory' AND NOT granted)", (pool.pids[0],),
                ).fetchone()[0]:
                    break
                time.sleep(.01)
            else:
                pytest.fail('clear never waited for user fence')
            assert not c.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s "
                                 "AND relation='trace_events'::regclass "
                                 "AND mode='ShareUpdateExclusiveLock' AND granted)",
                                 (pool.pids[0],)).fetchone()[0]
            # A current/future maintenance fence must be able to finish while clear
            # waits for the existing writer's user lock. Holding SUE while waiting
            # would let a queued AEX block that writer's next ROW EXCLUSIVE request.
            with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as owner:
                owner.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                with owner.transaction():
                    owner.execute('LOCK TABLE ONLY trace_events IN ACCESS EXCLUSIVE MODE NOWAIT')
            c.execute("SET LOCAL lock_timeout='150ms'")
            c.execute("INSERT INTO trace_events(user_id,ts,subsystem,type,status) "
                      "VALUES ('lock-order',%s,'agent','writer','ok')", (partitions._bounds(day)[0],))
    finally:
        if th.ident is not None:
            th.join(timeout=10)
    assert not th.is_alive()
    assert 'error' not in outcome, repr(outcome.get('error'))
    assert outcome['deleted'] == 1


def test_permanent_multi_day_check_error_stops_at_day_budget(race_db):
    c, _, pool = race_db
    c.execute("ALTER TABLE trace_events_default ADD CONSTRAINT reject_bad CHECK(type <> 'bad')")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.insert_trace_events_strict('bounded-multiday',
            [event(date(2026, 11, 17), 'bad'), event(date(2026, 11, 18), 'bad')])
    assert pool.attempts == 3
    assert c.execute("SELECT count(*) FROM trace_events WHERE user_id='bounded-multiday'").fetchone()[0] == 0
