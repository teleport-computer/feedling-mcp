"""Bounded/fail-open maintenance for provider-attempt daily rollups."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from conftest import seed_user
from model_api_runtime.v2 import provider_attempt_rollup, serve_worker


ROLLUP_NAME = provider_attempt_rollup.ROLLUP_NAME


def _attempt_id() -> str:
    return str(uuid.uuid4())


def _job_id() -> int:
    return uuid.uuid4().int % 8_000_000_000_000_000_000


@pytest.fixture(autouse=True)
def _clean_reconciler_state():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM llm_usage_rollup_dirty_days WHERE rollup_name=%s", (ROLLUP_NAME,))
        conn.execute("DELETE FROM llm_usage_rollup_watermarks WHERE rollup_name=%s", (ROLLUP_NAME,))
        conn.execute("DELETE FROM llm_usage_daily_call_memberships")
        conn.execute("DELETE FROM llm_usage_daily_attempt_dimensions")
        conn.execute("DELETE FROM llm_provider_attempts")
        conn.execute("TRUNCATE llm_rate_cards")
        conn.execute("DELETE FROM v2_turn_metrics")
        conn.execute("DELETE FROM users WHERE user_id LIKE 'attempt_reconcile_%'")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM llm_usage_rollup_dirty_days WHERE rollup_name=%s", (ROLLUP_NAME,))
        conn.execute("DELETE FROM llm_usage_rollup_watermarks WHERE rollup_name=%s", (ROLLUP_NAME,))
        conn.execute("DELETE FROM llm_usage_daily_call_memberships")
        conn.execute("DELETE FROM llm_usage_daily_attempt_dimensions")
        conn.execute("DELETE FROM llm_provider_attempts")
        conn.execute("TRUNCATE llm_rate_cards")
        conn.execute("DELETE FROM v2_turn_metrics")
        conn.execute("DELETE FROM users WHERE user_id LIKE 'attempt_reconcile_%'")


def _seed_turn_attempt(*, local_day: date, stale: bool = False) -> tuple[str, int, str]:
    user_id = f"attempt_reconcile_{uuid.uuid4().hex[:10]}"
    seed_user(user_id)
    job_id = _job_id()
    attempt_id = _attempt_id()
    created_at = datetime.combine(local_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=4)
    started_at = created_at - (timedelta(hours=2) if stale else timedelta(seconds=1))
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics (job_id,user_id,lane,model_calls,status,created_at,updated_at) "
            "VALUES (%s,%s,'chat',1,'done',%s,%s)",
            (job_id, user_id, created_at, created_at),
        )
        conn.execute(
            "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,job_id,call_id,"
            "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
            "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
            "outcome,error_class,usage_known,possibly_billed,source,completeness,revision,"
            "created_at,updated_at) VALUES (%s,%s,'chat',%s,%s,1,1,'initial','asked',"
            "'served','asked-model','served-model','responses',%s,'started','unknown',"
            "'none',false,false,'runtime_recorder','started_only',0,%s,%s)",
            (attempt_id, user_id, job_id, f"call-{attempt_id}", started_at, started_at, started_at),
        )
    return user_id, job_id, attempt_id


def _dirty_days() -> list[date]:
    with db.get_pool().connection() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT local_day FROM llm_usage_rollup_dirty_days "
                "WHERE rollup_name=%s ORDER BY local_day",
                (ROLLUP_NAME,),
            ).fetchall()
        ]


def test_reconciler_is_default_on_with_explicit_false_like_opt_out(monkeypatch):
    monkeypatch.delenv("FEEDLING_PROVIDER_ATTEMPT_ROLLUP_ENABLED", raising=False)
    assert provider_attempt_rollup.enabled()
    for value in ("0", "false", "NO", "off"):
        monkeypatch.setenv("FEEDLING_PROVIDER_ATTEMPT_ROLLUP_ENABLED", value)
        assert not provider_attempt_rollup.enabled()


def test_bootstrap_enqueues_sparse_days_in_bounded_batches_and_replay_requeues():
    for day in (date(2026, 7, 1), date(2026, 7, 3), date(2026, 7, 9)):
        _seed_turn_attempt(local_day=day)
    first = provider_attempt_rollup.run_maintenance_tick(max_days=1, max_dirty_days=2)
    assert first["status"] == "ok"
    assert first["days_refreshed"] == 1
    assert not first["bootstrap_complete"]
    second = provider_attempt_rollup.run_maintenance_tick(max_days=2, max_dirty_days=2)
    assert second["status"] == "ok"
    assert second["days_refreshed"] == 2
    assert second["bootstrap_complete"]

    generation = provider_attempt_rollup.request_replay()
    assert generation == 1
    replay = provider_attempt_rollup.run_maintenance_tick(max_days=1, max_dirty_days=1)
    assert replay["status"] == "ok"
    assert replay["days_refreshed"] == 1
    assert replay["replay_generation"] == 1


def test_stale_started_is_bounded_first_then_attempt_cursor_dirties_its_turn_day():
    days = (date(2026, 7, 10), date(2026, 7, 11))
    attempts = [_seed_turn_attempt(local_day=day, stale=True)[2] for day in days]
    result = provider_attempt_rollup.run_maintenance_tick(
        max_stale_rows=1, max_changed_rows=10, max_dirty_days=10, max_days=1,
        stale_after_seconds=60,
    )
    assert result["status"] == "ok"
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT attempt_id,possibly_billed,revision FROM llm_provider_attempts "
            "WHERE attempt_id=ANY(%s) ORDER BY attempt_id", (attempts,)
        ).fetchall()
    assert sum(bool(row[1]) for row in rows) == 1
    # Billing uncertainty is not a provider-event revision. A later complete
    # rev1 must still satisfy the recorder's strict incoming-revision gate.
    assert sum(int(row[2]) for row in rows) == 0
    stale_id = next(row[0] for row in rows if row[1])
    with db.get_pool().connection() as conn:
        completed = conn.execute(
            "UPDATE llm_provider_attempts SET state='completed',outcome='succeeded',"
            "finished_at=now(),revision=1 WHERE attempt_id=%s AND 1>revision "
            "RETURNING state,revision", (stale_id,),
        ).fetchone()
    assert completed == ("completed", 1)


def test_attempt_update_and_late_correction_advance_independent_cursors_atomically(monkeypatch):
    day = date(2026, 7, 12)
    user_id, _job, attempt_id = _seed_turn_attempt(local_day=day)
    provider_attempt_rollup.run_maintenance_tick(max_days=5)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE llm_provider_attempts SET usage_known=true,updated_at=now() WHERE attempt_id=%s", (attempt_id,))
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections "
            "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
            "VALUES (%s,%s,2,'late_usage',3)", (attempt_id, user_id),
        )

    original = provider_attempt_rollup._upsert_dirty_days

    def fail_after_dirty(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after dirty")

    monkeypatch.setattr(provider_attempt_rollup, "_upsert_dirty_days", fail_after_dirty)
    failed = provider_attempt_rollup.run_maintenance_tick(max_days=1)
    assert failed["status"] == "error"
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT attempt_updated_at,attempt_updated_id,late_correction_id "
            "FROM llm_usage_rollup_watermarks WHERE rollup_name=%s", (ROLLUP_NAME,)
        ).fetchone()
        assert state[2] == 0
        assert _dirty_days() == []

    monkeypatch.setattr(provider_attempt_rollup, "_upsert_dirty_days", original)
    ok = provider_attempt_rollup.run_maintenance_tick(max_days=0)
    assert ok["status"] == "ok"
    assert _dirty_days() == [day]
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT attempt_updated_id,late_correction_id FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name=%s", (ROLLUP_NAME,)
        ).fetchone()
    assert state[0] == attempt_id
    assert state[1] > 0


def test_turn_created_at_move_dirties_old_and_new_shanghai_days():
    old_day = date(2026, 7, 14)
    new_day = date(2026, 7, 16)
    _user, job_id, _attempt = _seed_turn_attempt(local_day=old_day)
    provider_attempt_rollup.run_maintenance_tick(max_days=5)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_turn_metrics SET created_at=%s,updated_at=now() WHERE job_id=%s",
            (datetime(2026, 7, 16, 4, tzinfo=timezone.utc), job_id),
        )
    assert _dirty_days() == [old_day, new_day]


def test_turn_created_at_change_within_same_shanghai_day_does_not_block_update():
    day = date(2026, 7, 17)
    _user, job_id, _attempt = _seed_turn_attempt(local_day=day)
    provider_attempt_rollup.run_maintenance_tick(max_days=5)
    with db.get_pool().connection() as conn:
        updated = conn.execute(
            "UPDATE v2_turn_metrics SET created_at=created_at+interval '1 hour',"
            "updated_at=now() WHERE job_id=%s RETURNING job_id", (job_id,),
        ).fetchone()
    assert updated == (job_id,)
    assert _dirty_days() == []


def test_rate_card_append_dirties_only_matching_effective_interval_days():
    days = (date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22))
    seeded = [_seed_turn_attempt(local_day=day) for day in days]
    with db.get_pool().connection() as conn:
        for day, (_uid, _job, attempt_id) in zip(days, seeded, strict=True):
            conn.execute(
                "UPDATE llm_provider_attempts SET started_at=%s,state='completed',"
                "finished_at=%s,outcome='succeeded',completeness='complete',"
                "updated_at=%s WHERE attempt_id=%s",
                (datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=4),) * 3 + (attempt_id,),
            )
        conn.execute(
            "INSERT INTO llm_rate_cards (provider,model,version,currency,effective_at) "
            "VALUES ('served','served-model','old','USD','2026-07-20T00:00:00Z'),"
            "('served','served-model','next','USD','2026-07-22T00:00:00Z')"
        )
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_rate_cards (provider,model,version,currency,effective_at) "
            "VALUES ('served','served-model','middle','USD','2026-07-21T00:00:00Z')"
        )
    provider_attempt_rollup.run_maintenance_tick(max_days=0, max_dirty_days=10)
    assert _dirty_days() == [date(2026, 7, 21)]


def test_rate_card_interval_overflow_advances_cursor_via_bounded_replay():
    for day in (date(2026, 7, 27), date(2026, 7, 28)):
        _seed_turn_attempt(local_day=day)
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_rate_cards (provider,model,version,currency,effective_at) "
            "VALUES ('served','served-model','wide','USD','2026-07-01T00:00:00Z')"
        )
    result = provider_attempt_rollup.run_maintenance_tick(max_days=0, max_dirty_days=1)
    assert result["status"] == "ok"
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT replay_generation,bootstrap_complete,completed_through_day,"
            "rate_card_version FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
            (ROLLUP_NAME,),
        ).fetchone()
    assert state == (1, False, None, "wide")
    assert len(_dirty_days()) <= 1


def test_discovery_enforces_one_global_dirty_day_budget_across_all_four_streams():
    seeded = [_seed_turn_attempt(local_day=date(2026, 7, 29) + timedelta(days=i)) for i in range(3)]
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE llm_provider_attempts SET updated_at=clock_timestamp() WHERE attempt_id=%s", (seeded[0][2],))
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections "
            "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
            "VALUES (%s,%s,2,'late_usage',1)", (seeded[1][2], seeded[1][0]),
        )
        conn.execute(
            "UPDATE v2_turn_metrics SET status='changed',updated_at=clock_timestamp() "
            "WHERE job_id=%s", (seeded[2][1],),
        )
    result = provider_attempt_rollup.run_maintenance_tick(max_days=0, max_dirty_days=1)
    assert result["status"] == "ok"
    assert len(_dirty_days()) <= 1


def test_same_day_rows_advance_the_source_row_budget_not_the_dirty_day_budget():
    seeded = [_seed_turn_attempt(local_day=date(2026, 8, 1)) for _ in range(4)]
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        for _uid, _job, attempt_id in seeded:
            conn.execute(
                "UPDATE llm_provider_attempts SET updated_at=clock_timestamp() "
                "WHERE attempt_id=%s", (attempt_id,),
            )
        expected = conn.execute(
            "SELECT attempt_id FROM llm_provider_attempts "
            "ORDER BY updated_at DESC,attempt_id DESC LIMIT 1"
        ).fetchone()[0]
    observed = []
    original = provider_attempt_rollup._reconciler_source_observer
    provider_attempt_rollup._reconciler_source_observer = (
        lambda **item: observed.append(item)
    )
    try:
        result = provider_attempt_rollup.run_maintenance_tick(
            max_days=0, max_changed_rows=4, max_dirty_days=1
        )
    finally:
        provider_attempt_rollup._reconciler_source_observer = original
    assert result["status"] == "ok"
    with db.get_pool().connection() as conn:
        actual = conn.execute(
            "SELECT attempt_updated_id FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name=%s", (ROLLUP_NAME,),
        ).fetchone()[0]
    assert actual == expected
    assert sum(item["fetched"] for item in observed) <= 4
    assert sum(item["advanced"] for item in observed) <= 4


def test_global_source_budget_reserves_fair_progress_under_attempt_backlog(monkeypatch):
    seeded = [_seed_turn_attempt(local_day=date(2026, 7, 20) + timedelta(days=i)) for i in range(3)]
    attempt_backlog = [seeded[0]] + [
        _seed_turn_attempt(local_day=date(2026, 7, 20)) for _ in range(7)
    ]
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        # Attempt remains continuously backlogged while the two later streams
        # each have one row that must still advance this tick.
        for _uid, _job, attempt_id in attempt_backlog:
            conn.execute(
                "UPDATE llm_provider_attempts SET updated_at=clock_timestamp() "
                "WHERE attempt_id=%s", (attempt_id,),
            )
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections "
            "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
            "VALUES (%s,%s,2,'late_usage',1)", (seeded[1][2], seeded[1][0]),
        )
        correction_head = conn.execute(
            "SELECT max(id) FROM llm_provider_attempt_corrections"
        ).fetchone()[0]
        conn.execute(
            "UPDATE v2_turn_metrics SET status='fair',updated_at=clock_timestamp() "
            "WHERE job_id=%s", (seeded[2][1],),
        )
        turn_head = conn.execute(
            "SELECT id FROM v2_turn_metrics WHERE job_id=%s", (seeded[2][1],)
        ).fetchone()[0]
    observed = []
    monkeypatch.setattr(
        provider_attempt_rollup,
        "_reconciler_source_observer",
        lambda **item: observed.append(item),
    )
    result = provider_attempt_rollup.run_maintenance_tick(
        max_days=0, max_changed_rows=4, max_dirty_days=1
    )
    assert result["status"] == "ok"
    assert sum(item["fetched"] for item in observed) <= 4
    assert sum(item["advanced"] for item in observed) <= 4
    with db.get_pool().connection() as conn:
        state = conn.execute(
            "SELECT late_correction_id,turn_metric_id FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name=%s", (ROLLUP_NAME,),
        ).fetchone()
    assert state == (correction_head, turn_head)


def test_tick_exposes_safe_backlog_lag_and_default_budget_catches_old_overload(monkeypatch):
    assert provider_attempt_rollup.DEFAULT_MAX_CHANGED_ROWS == 6_000
    seeded = [_seed_turn_attempt(local_day=date(2026, 7, 30)) for _ in range(5)]
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        for _uid, _job, attempt_id in seeded:
            conn.execute(
                "UPDATE llm_provider_attempts SET updated_at=clock_timestamp() "
                "WHERE attempt_id=%s", (attempt_id,),
            )
    first = provider_attempt_rollup.run_maintenance_tick(
        max_days=0, max_changed_rows=4, max_dirty_days=1
    )
    assert first["source_backlog"]["attempt"] is True
    assert first["source_lag_seconds"] >= 0
    second = provider_attempt_rollup.run_maintenance_tick(
        max_days=0, max_changed_rows=4, max_dirty_days=1
    )
    assert second["source_backlog"]["attempt"] is False
    results = {"attempt": {**second, "dirty_pending": False}}
    assert serve_worker._reporting_maintenance_delay(results, 300.0) == 300.0
    results["attempt"]["source_backlog"]["attempt"] = True
    assert serve_worker._reporting_maintenance_delay(results, 300.0) == 5.0
    results["attempt"]["status"] = "error"
    assert serve_worker._reporting_maintenance_delay(results, 300.0) == 300.0


def test_default_page_catches_writer_rate_above_old_2000_per_cadence_limit():
    user_id, job_id, _attempt = _seed_turn_attempt(local_day=date(2026, 7, 29))
    provider_attempt_rollup.run_maintenance_tick(max_days=10)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,job_id,call_id,"
            "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
            "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
            "outcome,error_class,source,completeness,revision,updated_at) "
            "SELECT '10000000-0000-5000-8000-'||lpad(to_hex(n),12,'0'),%s,'chat',%s,"
            "'writer-call-'||n,1,1,'initial','asked','served','asked-model','served-model',"
            "'responses',now()-interval '1 minute','completed','succeeded','none',"
            "'runtime_recorder','complete',1,clock_timestamp() "
            "FROM generate_series(1,2101) n",
            (user_id, job_id),
        )
        head = conn.execute(
            "SELECT attempt_id FROM llm_provider_attempts "
            "ORDER BY updated_at DESC,attempt_id DESC LIMIT 1"
        ).fetchone()[0]
    result = provider_attempt_rollup.run_maintenance_tick(max_days=0, max_dirty_days=1)
    assert result["status"] == "ok"
    assert result["source_backlog"]["attempt"] is False
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT attempt_updated_id FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name=%s", (ROLLUP_NAME,),
        ).fetchone()[0] == head


def test_cancel_after_stale_phase_does_not_start_bootstrap(monkeypatch):
    cancel = threading.Event()
    bootstrap_calls = []

    def reconcile_then_cancel(*args, **kwargs):
        cancel.set()
        return 0

    monkeypatch.setattr(provider_attempt_rollup, "_reconcile_stale_started", reconcile_then_cancel)
    monkeypatch.setattr(
        provider_attempt_rollup, "_bootstrap_batch",
        lambda *args, **kwargs: bootstrap_calls.append(1),
    )
    result = provider_attempt_rollup.run_maintenance_tick(cancel_event=cancel)
    assert result["status"] == "cancelled"
    assert bootstrap_calls == []


def test_advisory_contention_builder_failure_and_cancel_remain_fail_open(monkeypatch):
    day = date(2026, 7, 25)
    _seed_turn_attempt(local_day=day)
    with db.get_pool().connection() as holder:
        assert holder.execute(
            "SELECT pg_try_advisory_lock(%s)", (provider_attempt_rollup.ADVISORY_LOCK_KEY,)
        ).fetchone()[0]
        try:
            assert provider_attempt_rollup.run_maintenance_tick()["status"] == "lock_busy"
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (provider_attempt_rollup.ADVISORY_LOCK_KEY,))

    cancel = threading.Event()
    cancel.set()
    assert provider_attempt_rollup.run_maintenance_tick(cancel_event=cancel)["status"] == "cancelled"

    monkeypatch.setattr(
        provider_attempt_rollup, "recompute_local_day",
        lambda *_args, **_kwargs: {"status": "error", "error": "Injected"},
    )
    failed = provider_attempt_rollup.run_maintenance_tick(max_days=1)
    assert failed["status"] == "error"
    assert _dirty_days() == [day]


def test_generation_cas_retains_newer_dirty_claim():
    day = date(2026, 7, 26)
    _seed_turn_attempt(local_day=day)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason,generation) VALUES (%s,%s,'replay',2)",
            (ROLLUP_NAME, day),
        )
    result = provider_attempt_rollup.recompute_local_day(day, expected_generation=1)
    assert result["status"] == "error"
    assert _dirty_days() == [day]


def test_unlock_failure_discards_physical_session_and_releases_lock(monkeypatch):
    original_execute = psycopg.Connection.execute
    failed_connections = []

    def fail_unlock(self, query, *args, **kwargs):
        if "pg_advisory_unlock" in str(query):
            failed_connections.append(self)
            raise psycopg.OperationalError("forced unlock failure")
        return original_execute(self, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(psycopg.Connection, "execute", fail_unlock)
        assert provider_attempt_rollup.run_maintenance_tick()["status"] == "ok"

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as contender:
        acquired = contender.execute(
            "SELECT pg_try_advisory_lock(%s)", (provider_attempt_rollup.ADVISORY_LOCK_KEY,)
        ).fetchone()[0]
        try:
            assert len(failed_connections) == 1
            assert failed_connections[0].closed is True
            assert acquired is True
        finally:
            if acquired:
                contender.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (provider_attempt_rollup.ADVISORY_LOCK_KEY,),
                )


def test_existing_worker_maintenance_loop_runs_attempt_tick_and_isolates_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(serve_worker.usage_rollup, "enabled", lambda: False)
    monkeypatch.setattr(provider_attempt_rollup, "enabled", lambda: True)

    def fail_tick(**kwargs):
        calls.append(kwargs.get("cancel_event"))
        raise RuntimeError("optional reporting failure")

    monkeypatch.setattr(provider_attempt_rollup, "run_maintenance_tick", fail_tick)

    async def run_loop():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._usage_rollup_loop(stop, interval=0.01))
        while len(calls) < 2:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run_loop())
    assert len(calls) >= 2
    assert all(isinstance(item, threading.Event) for item in calls)
