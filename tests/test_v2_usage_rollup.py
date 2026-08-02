from __future__ import annotations

import asyncio
import os
import threading
import time as wall_time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import psycopg
from psycopg.rows import dict_row

import db
from conftest import seed_user
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import usage_rollup


ROLLUP_NAME = "hosted_v2_usage"
_FACT_METRICS = (
    "turns",
    "model_calls",
    "retries",
    "failed_turns",
    "usage_reported_calls",
    "cache_reported_calls",
    "unknown_usage_calls",
    "prompt_tokens_sum",
    "prompt_tokens_known_count",
    "completion_tokens_sum",
    "completion_tokens_known_count",
    "cache_read_tokens_sum",
    "cache_read_tokens_known_count",
    "cache_write_tokens_sum",
    "cache_write_tokens_known_count",
    "cache_miss_tokens_sum",
    "cache_miss_tokens_known_count",
)
_FACT_COLUMNS = tuple(
    f"{prefix}_{metric}"
    for prefix in ("all", "metered", "unknown")
    for metric in _FACT_METRICS
)


def _flatten_expected(**prefixes: tuple[int, ...]) -> dict[str, int]:
    assert set(prefixes) == {"all", "metered", "unknown"}
    return {
        f"{prefix}_{metric}": value
        for prefix, values in prefixes.items()
        for metric, value in zip(_FACT_METRICS, values, strict=True)
    }


@pytest.fixture(autouse=True)
def _clean_usage_rollup_state():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_usage_rollup_watermarks")
        conn.execute("DELETE FROM v2_usage_daily_dimensions")
        conn.execute("DELETE FROM v2_usage_daily_users")
        conn.execute("DELETE FROM v2_turn_metrics")
        conn.execute("DELETE FROM users WHERE user_id LIKE 'usr_rollup_%'")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_usage_rollup_watermarks")
        conn.execute("DELETE FROM v2_usage_daily_dimensions")
        conn.execute("DELETE FROM v2_usage_daily_users")
        conn.execute("DELETE FROM v2_turn_metrics")
        conn.execute("DELETE FROM users WHERE user_id LIKE 'usr_rollup_%'")


def _uid(prefix: str) -> str:
    return f"usr_rollup_{prefix}_{uuid.uuid4().hex[:10]}"


def _job_id() -> int:
    return uuid.uuid4().int % 8_000_000_000_000_000_000


def _insert_metric(
    *,
    created_at: datetime,
    updated_at: datetime | None = None,
    user_id: str | None,
    lane: str | None = "chat",
    provider: str | None = "openai",
    model: str | None = "gpt-5",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    model_calls: int = 0,
    retries: int = 0,
    failed: bool = False,
    usage_reported_calls: int = 0,
    cache_reported_calls: int = 0,
    latency_ms: int | None = None,
) -> int:
    job_id = _job_id()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics ("
            "job_id,user_id,lane,provider,model,prompt_tokens,completion_tokens,"
            "cache_read_tokens,cache_write_tokens,cache_miss_tokens,model_calls,"
            "retries,failed,status,usage_reported_calls,cache_reported_calls,"
            "latency_ms,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'done',%s,%s,%s,%s,%s)",
            (
                job_id,
                user_id,
                lane,
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                cache_read_tokens,
                cache_write_tokens,
                cache_miss_tokens,
                model_calls,
                retries,
                failed,
                usage_reported_calls,
                cache_reported_calls,
                latency_ms,
                created_at,
                updated_at or created_at,
            ),
        )
    return job_id


def _seed_overlap_day(local_day: date) -> str:
    uid = _uid("equivalence")
    seed_user(uid)
    base = datetime.combine(local_day, datetime.min.time(), tzinfo=timezone.utc)
    # 12:00 UTC is safely inside the same Asia/Shanghai local day.
    at = base + timedelta(hours=4)
    _insert_metric(
        created_at=at,
        user_id=uid,
        prompt_tokens=100,
        completion_tokens=20,
        cache_read_tokens=10,
        cache_miss_tokens=0,
        model_calls=2,
        retries=1,
        usage_reported_calls=2,
        cache_reported_calls=1,
        latency_ms=100,
    )
    _insert_metric(
        created_at=at + timedelta(minutes=1),
        user_id=uid,
        prompt_tokens=50,
        completion_tokens=None,
        model_calls=3,
        retries=2,
        failed=True,
        usage_reported_calls=1,
        cache_reported_calls=0,
        latency_ms=200,
    )
    _insert_metric(
        created_at=at + timedelta(minutes=2),
        user_id=uid,
        lane=None,
        provider="",
        model=None,
        model_calls=1,
        usage_reported_calls=0,
        latency_ms=None,
    )
    return uid


def _watermark() -> dict:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT bootstrap_complete,source_updated_at,source_id,"
            "dirty_from_day,dirty_through_day,refreshed_at,last_success_at,"
            "last_error,version FROM v2_usage_rollup_watermarks "
            "WHERE rollup_name=%s",
            (ROLLUP_NAME,),
        ).fetchone()
    assert row is not None
    keys = (
        "bootstrap_complete",
        "source_updated_at",
        "source_id",
        "dirty_from_day",
        "dirty_through_day",
        "refreshed_at",
        "last_success_at",
        "last_error",
        "version",
    )
    return dict(zip(keys, row, strict=True))


def test_recompute_one_day_preserves_overlapping_and_null_semantics():
    """Catches exclusive completeness buckets or NULL token fields coerced to known zero."""
    day = date(2026, 8, 1)
    uid = _seed_overlap_day(day)

    result = usage_rollup.recompute_local_day(day)

    assert result == {"users": 1, "dimensions": 2}
    with db.get_pool().connection() as conn:
        user_row = conn.execute(
            "SELECT all_turns,all_model_calls,all_retries,all_failed_turns,"
            "all_usage_reported_calls,all_unknown_usage_calls,"
            "all_prompt_tokens_sum,all_prompt_tokens_known_count,"
            "all_completion_tokens_sum,all_completion_tokens_known_count,"
            "metered_turns,metered_model_calls,metered_unknown_usage_calls,"
            "metered_prompt_tokens_sum,metered_prompt_tokens_known_count,"
            "unknown_turns,unknown_model_calls,unknown_prompt_tokens_sum,"
            "unknown_prompt_tokens_known_count "
            "FROM v2_usage_daily_users WHERE local_day=%s AND user_id=%s",
            (day, uid),
        ).fetchone()
        dims = conn.execute(
            "SELECT lane,provider,model,all_turns,metered_turns,unknown_turns,"
            "all_latency_samples,metered_latency_samples,unknown_latency_samples "
            "FROM v2_usage_daily_dimensions WHERE local_day=%s AND user_id=%s "
            "ORDER BY lane,provider,model",
            (day, uid),
        ).fetchall()

    assert user_row == (
        3,
        6,
        3,
        1,
        3,
        3,
        150,
        2,
        20,
        1,
        2,
        5,
        2,
        150,
        2,
        2,
        4,
        50,
        1,
    )
    assert dims == [
        ("chat", "openai", "gpt-5", 2, 2, 1, [100, 200], [100, 200], [200]),
        ("unknown", "unknown", "unknown", 1, 0, 1, [], [], []),
    ]


def test_every_user_and_dimension_subaggregate_matches_the_raw_fixture():
    """Catches any one of all 51 count/sum fields or exact arrays drifting."""
    day = date(2026, 8, 1)
    uid = _seed_overlap_day(day)
    usage_rollup.recompute_local_day(day)

    expected_user = _flatten_expected(
        all=(3, 6, 3, 1, 3, 1, 3, 150, 2, 20, 1, 10, 1, 0, 0, 0, 1),
        metered=(2, 5, 3, 1, 3, 1, 2, 150, 2, 20, 1, 10, 1, 0, 0, 0, 1),
        unknown=(2, 4, 2, 1, 1, 0, 3, 50, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    )
    expected_chat = _flatten_expected(
        all=(2, 5, 3, 1, 3, 1, 2, 150, 2, 20, 1, 10, 1, 0, 0, 0, 1),
        metered=(2, 5, 3, 1, 3, 1, 2, 150, 2, 20, 1, 10, 1, 0, 0, 0, 1),
        unknown=(1, 3, 2, 1, 1, 0, 2, 50, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    )
    empty = (0,) * len(_FACT_METRICS)
    unknown_only = (1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    expected_unknown_identity = _flatten_expected(
        all=unknown_only,
        metered=empty,
        unknown=unknown_only,
    )

    columns_sql = ",".join(_FACT_COLUMNS)
    with db.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {columns_sql} FROM v2_usage_daily_users "
                "WHERE local_day=%s AND user_id=%s",
                (day, uid),
            )
            user_row = cur.fetchone()
            cur.execute(
                f"SELECT lane,provider,model,{columns_sql},"
                "all_latency_samples,metered_latency_samples,unknown_latency_samples "
                "FROM v2_usage_daily_dimensions WHERE local_day=%s AND user_id=%s "
                "ORDER BY lane,provider,model",
                (day, uid),
            )
            dimensions = cur.fetchall()

    assert user_row == expected_user
    assert dimensions == [
        {
            "lane": "chat",
            "provider": "openai",
            "model": "gpt-5",
            **expected_chat,
            "all_latency_samples": [100, 200],
            "metered_latency_samples": [100, 200],
            "unknown_latency_samples": [200],
        },
        {
            "lane": "unknown",
            "provider": "unknown",
            "model": "unknown",
            **expected_unknown_identity,
            "all_latency_samples": [],
            "metered_latency_samples": [],
            "unknown_latency_samples": [],
        },
    ]


def test_recompute_preserves_one_canonical_null_user_row():
    """Catches nullable source attribution being dropped or duplicated during rebuild."""
    day = date(2026, 8, 5)
    _insert_metric(
        created_at=datetime(2026, 8, 5, 4, tzinfo=timezone.utc),
        user_id=None,
        lane=None,
        provider=None,
        model=None,
        prompt_tokens=None,
        completion_tokens=None,
        model_calls=1,
        usage_reported_calls=0,
    )
    assert usage_rollup.recompute_local_day(day) == {"users": 1, "dimensions": 1}
    assert usage_rollup.recompute_local_day(day) == {"users": 1, "dimensions": 1}
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*),min(all_prompt_tokens_known_count),"
            "min(unknown_turns) FROM v2_usage_daily_users "
            "WHERE local_day=%s AND user_id IS NULL",
            (day,),
        ).fetchone() == (1, 0, 1)


def test_day_replacement_is_idempotent_and_rolls_back_as_one_unit():
    """Catches append-on-refresh and a crash exposing half-replaced day facts."""
    day = date(2026, 8, 2)
    uid = _seed_overlap_day(day)
    assert usage_rollup.recompute_local_day(day) == {"users": 1, "dimensions": 2}

    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT all_prompt_tokens_sum,refreshed_at FROM v2_usage_daily_users "
            "WHERE local_day=%s AND user_id=%s",
            (day, uid),
        ).fetchone()
        conn.execute(
            "CREATE OR REPLACE FUNCTION fail_usage_dimension_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced rollup crash'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_usage_dimension_insert BEFORE INSERT "
            "ON v2_usage_daily_dimensions FOR EACH ROW "
            "EXECUTE FUNCTION fail_usage_dimension_insert()"
        )
    try:
        with pytest.raises(Exception, match="forced rollup crash"):
            usage_rollup.recompute_local_day(day)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS fail_usage_dimension_insert "
                "ON v2_usage_daily_dimensions"
            )
            conn.execute("DROP FUNCTION IF EXISTS fail_usage_dimension_insert()")

    with db.get_pool().connection() as conn:
        after_crash = conn.execute(
            "SELECT all_prompt_tokens_sum,refreshed_at FROM v2_usage_daily_users "
            "WHERE local_day=%s AND user_id=%s",
            (day, uid),
        ).fetchone()
        dim_count = conn.execute(
            "SELECT count(*) FROM v2_usage_daily_dimensions "
            "WHERE local_day=%s AND user_id=%s",
            (day, uid),
        ).fetchone()[0]
    assert after_crash == before
    assert dim_count == 2
    assert usage_rollup.recompute_local_day(day) == {"users": 1, "dimensions": 2}
    with db.get_pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_usage_daily_users "
                "WHERE local_day=%s AND user_id=%s",
                (day, uid),
            ).fetchone()[0]
            == 1
        )


def test_day_user_and_dimension_facts_share_one_repeatable_read_snapshot():
    """Catches the two INSERT aggregations observing different source versions."""
    day = date(2026, 8, 6)
    uid = _uid("day_snapshot")
    seed_user(uid)
    job_id = _insert_metric(
        created_at=datetime(2026, 8, 6, 4, tzinfo=timezone.utc),
        user_id=uid,
        prompt_tokens=10,
        completion_tokens=1,
        model_calls=1,
        usage_reported_calls=1,
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION pause_after_usage_users() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.5); RETURN NULL; END $$"
        )
        conn.execute(
            "CREATE TRIGGER pause_after_usage_users AFTER INSERT "
            "ON v2_usage_daily_users FOR EACH STATEMENT "
            "EXECUTE FUNCTION pause_after_usage_users()"
        )

    started = threading.Event()

    def update_source_during_rebuild():
        started.set()
        wall_time.sleep(0.15)
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            conn.execute(
                "UPDATE v2_turn_metrics SET prompt_tokens=99,updated_at=now() "
                "WHERE job_id=%s",
                (job_id,),
            )

    updater = threading.Thread(target=update_source_during_rebuild)
    updater.start()
    assert started.wait(timeout=1)
    try:
        usage_rollup.recompute_local_day(day)
    finally:
        updater.join(timeout=2)
        with db.get_pool().connection() as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS pause_after_usage_users ON v2_usage_daily_users"
            )
            conn.execute("DROP FUNCTION IF EXISTS pause_after_usage_users()")
    assert not updater.is_alive()
    with db.get_pool().connection() as conn:
        user_sum = conn.execute(
            "SELECT all_prompt_tokens_sum FROM v2_usage_daily_users "
            "WHERE local_day=%s AND user_id=%s",
            (day, uid),
        ).fetchone()[0]
        dimension_sum = conn.execute(
            "SELECT all_prompt_tokens_sum FROM v2_usage_daily_dimensions "
            "WHERE local_day=%s AND user_id=%s",
            (day, uid),
        ).fetchone()[0]
    assert (user_sum, dimension_sum) == (10, 10)


def test_bootstrap_and_changed_row_scans_are_bounded():
    """Catches an initial deployment attempting an unbounded history transaction."""
    uid = _uid("bounded")
    seed_user(uid)
    first = date(2026, 7, 1)
    for offset in range(4):
        local_day = first + timedelta(days=offset)
        _insert_metric(
            created_at=datetime.combine(
                local_day, datetime.min.time(), tzinfo=timezone.utc
            )
            + timedelta(hours=4),
            user_id=uid,
            prompt_tokens=offset + 1,
            completion_tokens=1,
            model_calls=1,
            usage_reported_calls=1,
        )

    first_tick = usage_rollup.run_maintenance_tick(
        max_days=2,
        max_changed_rows=2,
        now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert first_tick["status"] == "ok"
    assert first_tick["days_refreshed"] == 2
    assert first_tick["bootstrap_complete"] is False
    with db.get_pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT count(DISTINCT local_day) FROM v2_usage_daily_users "
                "WHERE user_id=%s",
                (uid,),
            ).fetchone()[0]
            == 2
        )

    second_tick = usage_rollup.run_maintenance_tick(
        max_days=2,
        max_changed_rows=2,
        now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert second_tick["days_refreshed"] == 2
    assert second_tick["bootstrap_complete"] is True
    assert _watermark()["dirty_from_day"] is None


def test_bootstrap_day_range_and_safe_cursor_share_one_snapshot(monkeypatch):
    """Catches a concurrent new day entering the cursor but not bootstrap's dirty range."""
    uid = _uid("bootstrap_snapshot")
    seed_user(uid)
    now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    first_day = date(2026, 6, 1)
    second_day = first_day + timedelta(days=1)
    first_job = _insert_metric(
        created_at=datetime(2026, 6, 1, 4, tzinfo=timezone.utc),
        updated_at=now - timedelta(days=1),
        user_id=uid,
        prompt_tokens=1,
        completion_tokens=1,
        model_calls=1,
        usage_reported_calls=1,
    )
    original = usage_rollup._cursor_max_on_cursor
    inserted = False

    def insert_between_day_range_and_cursor(cur, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            _insert_metric(
                created_at=datetime(2026, 6, 2, 4, tzinfo=timezone.utc),
                updated_at=now - timedelta(days=1) + timedelta(minutes=1),
                user_id=uid,
                prompt_tokens=2,
                completion_tokens=1,
                model_calls=1,
                usage_reported_calls=1,
            )
        return original(cur, **kwargs)

    monkeypatch.setattr(
        usage_rollup, "_cursor_max_on_cursor", insert_between_day_range_and_cursor
    )
    first = usage_rollup.run_maintenance_tick(
        max_days=1, overlap_seconds=600, now_utc=now
    )
    with db.get_pool().connection() as conn:
        first_source_id = conn.execute(
            "SELECT id FROM v2_turn_metrics WHERE job_id=%s", (first_job,)
        ).fetchone()[0]
    assert first["refreshed_days"] == [first_day.isoformat()]
    assert first["source_id"] == first_source_id

    second = usage_rollup.run_maintenance_tick(
        max_days=1, overlap_seconds=600, now_utc=now
    )
    assert second["refreshed_days"] == [second_day.isoformat()]


def test_forward_cursor_limits_rows_per_tick_and_eventually_rebuilds_all_days():
    """Catches max_changed_rows being ignored or later eligible updates skipped."""
    uid = _uid("cursor_batch")
    seed_user(uid)
    now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    assert (
        usage_rollup.run_maintenance_tick(overlap_seconds=600, now_utc=now)[
            "bootstrap_complete"
        ]
        is True
    )
    first_day = date(2026, 7, 1)
    for offset in range(5):
        day = first_day + timedelta(days=offset)
        _insert_metric(
            created_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            + timedelta(hours=4),
            updated_at=now - timedelta(hours=1),
            user_id=uid,
            prompt_tokens=offset + 1,
            completion_tokens=1,
            model_calls=1,
            usage_reported_calls=1,
        )
    with db.get_pool().connection() as conn:
        source_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM v2_turn_metrics ORDER BY updated_at,id"
            ).fetchall()
        ]

    ticks = [
        usage_rollup.run_maintenance_tick(
            max_days=2,
            max_changed_rows=2,
            overlap_seconds=600,
            now_utc=now,
        )
        for _ in range(3)
    ]
    assert [tick["source_id"] for tick in ticks] == [
        source_ids[1],
        source_ids[3],
        source_ids[4],
    ]
    assert ticks[0]["source_lag_seconds"] > 0
    assert ticks[1]["source_lag_seconds"] > 0
    assert ticks[2]["source_lag_seconds"] == 0
    assert [tick["refreshed_days"] for tick in ticks] == [
        [(first_day + timedelta(days=offset)).isoformat() for offset in (0, 1)],
        [(first_day + timedelta(days=offset)).isoformat() for offset in (2, 3)],
        [(first_day + timedelta(days=4)).isoformat()],
    ]
    assert (
        usage_rollup.run_maintenance_tick(
            max_days=2,
            max_changed_rows=2,
            overlap_seconds=600,
            now_utc=now,
        )["days_refreshed"]
        == 0
    )
    with db.get_pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT count(DISTINCT local_day) FROM v2_usage_daily_users "
                "WHERE user_id=%s",
                (uid,),
            ).fetchone()[0]
            == 5
        )


def test_tick_commits_each_day_before_a_later_day_crashes():
    """Catches nested savepoints turning a bounded tick into one long transaction."""
    uid = _uid("short_tx")
    seed_user(uid)
    first = date(2026, 7, 5)
    second = first + timedelta(days=1)
    for day in (first, second):
        _insert_metric(
            created_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
            + timedelta(hours=4),
            user_id=uid,
            prompt_tokens=1,
            completion_tokens=1,
            model_calls=1,
            usage_reported_calls=1,
        )
    with db.get_pool().connection() as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION fail_second_usage_day() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.local_day=DATE '2026-07-06' THEN "
            "RAISE EXCEPTION 'forced second day crash'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_second_usage_day BEFORE INSERT "
            "ON v2_usage_daily_dimensions FOR EACH ROW "
            "EXECUTE FUNCTION fail_second_usage_day()"
        )
    try:
        result = usage_rollup.run_maintenance_tick(max_days=2)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS fail_second_usage_day "
                "ON v2_usage_daily_dimensions"
            )
            conn.execute("DROP FUNCTION IF EXISTS fail_second_usage_day()")

    assert result["status"] == "error"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT array_agg(local_day ORDER BY local_day) "
            "FROM v2_usage_daily_users WHERE user_id=%s",
            (uid,),
        ).fetchone()[0] == [first]
    state = _watermark()
    assert state["dirty_from_day"] == second
    assert state["bootstrap_complete"] is False


def test_tick_releases_the_session_advisory_lock_before_returning_pool_connection():
    """Catches a session lock leaking into the pool after maintenance returns."""
    assert usage_rollup.run_maintenance_tick()["status"] == "ok"
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as contender:
        acquired = contender.execute(
            "SELECT pg_try_advisory_lock(%s)", (usage_rollup.ADVISORY_LOCK_KEY,)
        ).fetchone()[0]
        try:
            assert acquired is True
        finally:
            if acquired:
                contender.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (usage_rollup.ADVISORY_LOCK_KEY,),
                )


def test_incremental_cursor_overlap_discovers_a_late_updated_old_day():
    """Catches cursor-only refresh that misses a late commit behind its last scan."""
    day = date(2026, 6, 10)
    uid = _uid("late")
    seed_user(uid)
    created = datetime(2026, 6, 10, 4, tzinfo=timezone.utc)
    metric_id = _insert_metric(
        created_at=created,
        updated_at=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
        user_id=uid,
        prompt_tokens=10,
        completion_tokens=1,
        model_calls=1,
        usage_reported_calls=1,
    )
    now = datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)
    assert (
        usage_rollup.run_maintenance_tick(max_days=4, now_utc=now)["bootstrap_complete"]
        is True
    )

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_turn_metrics SET prompt_tokens=99, updated_at=%s "
            "WHERE job_id=%s",
            (datetime(2026, 8, 1, 0, 4, tzinfo=timezone.utc), metric_id),
        )
    refreshed = usage_rollup.run_maintenance_tick(
        max_days=1,
        overlap_seconds=600,
        now_utc=now + timedelta(minutes=11),
    )
    assert refreshed["days_refreshed"] == 1
    with db.get_pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT all_prompt_tokens_sum FROM v2_usage_daily_users "
                "WHERE local_day=%s AND user_id=%s",
                (day, uid),
            ).fetchone()[0]
            == 99
        )


def test_safe_lag_cursor_does_not_rebuild_without_new_source_updates():
    """Catches a fixed overlap window recreating the same dirty day forever."""
    day = date(2026, 6, 12)
    uid = _uid("safe_lag_idle")
    seed_user(uid)
    now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    _insert_metric(
        created_at=datetime(2026, 6, 12, 4, tzinfo=timezone.utc),
        updated_at=now - timedelta(days=1),
        user_id=uid,
        prompt_tokens=10,
        completion_tokens=1,
        model_calls=1,
        usage_reported_calls=1,
    )
    first = usage_rollup.run_maintenance_tick(
        max_days=1, overlap_seconds=600, now_utc=now
    )
    second = usage_rollup.run_maintenance_tick(
        max_days=1, overlap_seconds=600, now_utc=now
    )
    third = usage_rollup.run_maintenance_tick(
        max_days=1, overlap_seconds=600, now_utc=now + timedelta(minutes=1)
    )
    assert first["refreshed_days"] == [day.isoformat()]
    assert second["days_refreshed"] == 0
    assert third["days_refreshed"] == 0


def test_recent_source_update_waits_until_the_safe_horizon():
    """Catches the cursor consuming transactions still inside the lateness budget."""
    uid = _uid("safe_lag_recent")
    seed_user(uid)
    now = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    assert (
        usage_rollup.run_maintenance_tick(overlap_seconds=600, now_utc=now)[
            "bootstrap_complete"
        ]
        is True
    )

    day = date(2026, 7, 20)
    _insert_metric(
        created_at=datetime(2026, 7, 20, 4, tzinfo=timezone.utc),
        updated_at=now - timedelta(minutes=5),
        user_id=uid,
        prompt_tokens=7,
        completion_tokens=1,
        model_calls=1,
        usage_reported_calls=1,
    )
    still_recent = usage_rollup.run_maintenance_tick(
        max_days=1, overlap_seconds=600, now_utc=now
    )
    after_horizon = usage_rollup.run_maintenance_tick(
        max_days=1,
        overlap_seconds=600,
        now_utc=now + timedelta(minutes=6),
    )
    assert still_recent["days_refreshed"] == 0
    assert after_horizon["refreshed_days"] == [day.isoformat()]


def test_advisory_lock_competition_skips_without_waiting():
    """Catches multiple worker replicas rebuilding the same canonical day."""
    with db.get_pool().connection() as holder:
        assert holder.execute(
            "SELECT pg_try_advisory_lock(%s)", (usage_rollup.ADVISORY_LOCK_KEY,)
        ).fetchone()[0]
        try:
            assert usage_rollup.run_maintenance_tick()["status"] == "lock_busy"
        finally:
            holder.execute(
                "SELECT pg_advisory_unlock(%s)", (usage_rollup.ADVISORY_LOCK_KEY,)
            )


def test_watermark_cursor_requires_the_expected_cas_version():
    """Catches a stale maintainer overwriting fresher cursor and dirty-day state."""
    usage_rollup.ensure_watermark()
    old = _watermark()
    assert usage_rollup.compare_and_set_watermark(
        expected_version=old["version"],
        source_updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        source_id=123,
    )
    assert not usage_rollup.compare_and_set_watermark(
        expected_version=old["version"],
        source_updated_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        source_id=456,
    )
    current = _watermark()
    assert (current["source_updated_at"], current["source_id"]) == (
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        123,
    )


def test_user_deletion_cascades_from_both_rollup_facts():
    """Catches derived usage surviving account deletion after a successful rebuild."""
    day = date(2026, 8, 3)
    uid = _seed_overlap_day(day)
    usage_rollup.recompute_local_day(day)

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_usage_daily_users WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_usage_daily_dimensions WHERE user_id=%s",
                (uid,),
            ).fetchone()[0]
            == 0
        )


def test_record_whole_turn_metric_never_runs_rollup_refresh(monkeypatch):
    """Catches accidental rollup work being coupled to the provider/reply hot path."""
    uid = _uid("hotpath")
    seed_user(uid)
    calls = []
    monkeypatch.setattr(
        usage_rollup,
        "run_maintenance_tick",
        lambda **_kwargs: calls.append("refresh"),
    )
    jobs_store.record_whole_turn_metric(
        _job_id(),
        uid,
        "chat",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
        model_calls=1,
        retries=0,
        failed=False,
        status="done",
        usage_reported_calls=1,
    )
    assert calls == []


def test_worker_rollup_loop_is_default_on_opt_out_and_fail_open(monkeypatch):
    """Catches maintenance failure escaping its sibling worker loops or opt-out ignored."""
    attempts = []

    def fail_tick():
        attempts.append(1)
        raise RuntimeError("database unavailable")

    monkeypatch.delenv("FEEDLING_V2_USAGE_ROLLUP_ENABLED", raising=False)
    monkeypatch.setattr(usage_rollup, "run_maintenance_tick", fail_tick)

    async def run_default_on():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._usage_rollup_loop(stop, interval=0.01))
        while len(attempts) < 2:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(run_default_on())
    assert len(attempts) >= 2

    monkeypatch.setenv("FEEDLING_V2_USAGE_ROLLUP_ENABLED", "0")

    async def run_disabled():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._usage_rollup_loop(stop, interval=0.01))
        await asyncio.sleep(0.03)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)

    before = len(attempts)
    asyncio.run(run_disabled())
    assert len(attempts) == before


def test_worker_rollup_interval_bad_config_falls_back_instead_of_blocking_startup(
    monkeypatch,
):
    """Catches optional telemetry configuration aborting the worker process."""
    monkeypatch.setenv("FEEDLING_V2_USAGE_ROLLUP_INTERVAL_SEC", "not-a-number")
    assert serve_worker._usage_rollup_interval_seconds() == 300.0
    monkeypatch.setenv("FEEDLING_V2_USAGE_ROLLUP_INTERVAL_SEC", "0")
    assert serve_worker._usage_rollup_interval_seconds() == 300.0


def test_statement_timeout_rolls_back_the_day_and_remains_fail_open():
    """Catches a slow rollup statement escaping its deadline or publishing partial rows."""
    day = date(2026, 8, 4)
    uid = _seed_overlap_day(day)
    with db.get_pool().connection() as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION slow_usage_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(0.2); RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER slow_usage_insert BEFORE INSERT "
            "ON v2_usage_daily_users FOR EACH STATEMENT "
            "EXECUTE FUNCTION slow_usage_insert()"
        )
    try:
        result = usage_rollup.run_maintenance_tick(max_days=1, statement_timeout_ms=100)
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS slow_usage_insert ON v2_usage_daily_users"
            )
            conn.execute("DROP FUNCTION IF EXISTS slow_usage_insert()")
    assert result["status"] == "error"
    assert result["error"] == "QueryCanceled"
    with db.get_pool().connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_usage_daily_users WHERE user_id=%s", (uid,)
            ).fetchone()[0]
            == 0
        )
