"""Atomic Shanghai-day rollups for canonical provider attempts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

import pytest
from psycopg.rows import dict_row


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from accounts import registry  # noqa: E402
from admin import usage as admin_usage  # noqa: E402
from model_api_runtime.v2 import jobs_store, provider_attempt_rollup  # noqa: E402

from conftest import seed_user  # noqa: E402


LOCAL_DAY = date(2026, 7, 1)
REFRESHED_AT = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)


def _attempt_id(number: int) -> str:
    value = f"{number:012x}"
    return f"00000000-0000-5000-8000-{value}"


def _insert_attempt(
    conn,
    *,
    number: int,
    user_id: str,
    job_id: int,
    call_id: str,
    outer: int = 1,
    inner: int = 1,
    retry_kind: str = "initial",
    requested_provider: str = "asked",
    requested_model: str = "asked-model",
    resolved_provider: str = "served",
    resolved_model: str = "served-model",
    outcome: str = "succeeded",
    usage_known: bool = True,
    possibly_billed: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    ttft_ms: float | None = None,
    cost: Decimal | None = None,
    currency: str | None = None,
    started_at: str = "2026-07-01T00:00:00+00:00",
) -> str:
    attempt_id = _attempt_id(number)
    conn.execute(
        "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,job_id,call_id,"
        "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
        "resolved_provider,requested_model,resolved_model,transport,started_at,"
        "finished_at,state,outcome,error_class,input_tokens,output_tokens,"
        "reasoning_tokens,cache_read_tokens,cache_write_tokens,cache_miss_tokens,"
        "usage_known,possibly_billed,latency_ms,ttft_ms,cost,currency,source,"
        "completeness,revision) VALUES (%s,%s,'chat',%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "'responses',%s,%s,'completed',%s,'none',%s,%s,%s,%s,%s,%s,%s,%s,10,%s,"
        "%s,%s,'runtime_recorder',%s,2)",
        (
            attempt_id,
            user_id,
            job_id,
            call_id,
            outer,
            inner,
            retry_kind,
            requested_provider,
            resolved_provider,
            requested_model,
            resolved_model,
            started_at,
            started_at,
            outcome,
            input_tokens,
            output_tokens,
            reasoning_tokens,
            cache_read_tokens,
            cache_write_tokens,
            cache_miss_tokens,
            usage_known,
            possibly_billed,
            ttft_ms,
            cost,
            currency,
            "complete" if usage_known else "usage_unknown",
        ),
    )
    return attempt_id


@pytest.fixture(scope="module")
def attempt_rollup_rows():
    user_id = "u_attempt_rollup_builder"
    seed_user(user_id, created_at="2026-06-01T00:00:00+00:00")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_turn_metrics (job_id,user_id,lane,provider,model,"
            "model_calls,usage_reported_calls,status,created_at) VALUES "
            "(92001,%s,'chat','turn-provider','turn-model',3,2,'rollup-day',"
            "'2026-06-30T16:30:00+00:00'),"
            "(92002,%s,'chat','turn-provider','turn-model',1,1,'outside-day',"
            "'2026-06-29T16:30:00+00:00')",
            (user_id, user_id),
        )
        conn.execute(
            "INSERT INTO llm_rate_cards (provider,model,version,currency,"
            "input_cost_per_million,output_cost_per_million,"
            "reasoning_cost_per_million,cache_read_cost_per_million,"
            "cache_write_cost_per_million,cache_miss_cost_per_million,effective_at)"
            " VALUES ('served','served-model','old','USD',2,4,6,0,2,2,"
            "'2026-06-01T00:00:00+00:00'),"
            "('served','served-model','new','USD',200,400,600,0,200,200,"
            "'2026-07-01T12:00:00+00:00')"
        )
        first = _insert_attempt(
            conn,
            number=1,
            user_id=user_id,
            job_id=92001,
            call_id="call-retry",
            input_tokens=100,
            output_tokens=20,
            reasoning_tokens=5,
            cache_read_tokens=10,
            cache_write_tokens=2,
            cache_miss_tokens=4,
            ttft_ms=90,
            cost=Decimal("0.50"),
            currency="USD",
            started_at="2026-07-01T11:59:59+00:00",
        )
        _insert_attempt(
            conn,
            number=2,
            user_id=user_id,
            job_id=92001,
            call_id="call-retry",
            outer=3,
            retry_kind="failover",
            resolved_provider="fallback",
            resolved_model="fallback-model",
            outcome="failed",
            usage_known=False,
            possibly_billed=True,
            ttft_ms=10,
        )
        _insert_attempt(
            conn,
            number=3,
            user_id=user_id,
            job_id=92001,
            call_id="call-inner",
            inner=2,
            retry_kind="compatibility_retry",
            input_tokens=50,
            output_tokens=10,
            reasoning_tokens=0,
            cache_read_tokens=5,
            cache_write_tokens=1,
            cache_miss_tokens=2,
            ttft_ms=50,
            started_at="2026-07-01T12:00:00+00:00",
        )
        _insert_attempt(
            conn,
            number=4,
            user_id=user_id,
            job_id=92001,
            call_id="call-boundary",
            outer=2,
            retry_kind="outer_retry",
            input_tokens=10,
            output_tokens=0,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cache_miss_tokens=0,
            ttft_ms=30,
            started_at="2026-07-01T11:59:59+00:00",
        )
        _insert_attempt(
            conn,
            number=5,
            user_id=user_id,
            job_id=92002,
            call_id="call-retry",
            outer=2,
            retry_kind="outer_retry",
            usage_known=False,
            started_at="2026-06-29T16:30:00+00:00",
        )
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections (attempt_id,user_id,"
            "revision,reason_code,input_tokens_delta,output_tokens_delta,cost_delta,"
            "currency) VALUES (%s,%s,3,'late_usage',-7,3,.10,'USD'),"
            "(%s,%s,4,'invoice_adjustment',NULL,NULL,-.20,'EUR')",
            (first, user_id, first, user_id),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason,generation) VALUES (%s,%s,'test',2)",
            (provider_attempt_rollup.ROLLUP_NAME, LOCAL_DAY),
        )
    try:
        yield user_id
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
            conn.execute(
                "DELETE FROM llm_rate_cards WHERE false"
            )
        with registry._users_lock:
            registry._users[:] = [
                row for row in registry._users if row.get("user_id") != user_id
            ]


def test_recompute_local_day_folds_attempts_corrections_rates_gaps_and_ttft(
    attempt_rollup_rows,
):
    """Dropping any identity, signed delta, rate boundary, gap, or TTFT order breaks it."""
    result = provider_attempt_rollup.recompute_local_day(
        LOCAL_DAY, refreshed_at=REFRESHED_AT
    )
    assert result == {"status": "ok", "dimensions": 3, "memberships": 4}

    with db.get_pool().connection() as conn:
        dimensions = conn.execute(
            "SELECT requested_provider,resolved_provider,effective_usage_known,"
            "cost_kind,currency,attempts,retry_attempts,failover_attempts,"
            "failed_attempts,possibly_billed_attempts,input_tokens_sum,"
            "input_tokens_known_count,output_tokens_sum,authoritative_cost_attempts,"
            "estimated_cost_attempts,unknown_cost_attempts,cost_amount,ttft_samples "
            "FROM llm_usage_daily_attempt_dimensions WHERE local_day=%s AND user_id=%s "
            "ORDER BY resolved_provider,effective_usage_known",
            (LOCAL_DAY, attempt_rollup_rows),
        ).fetchall()
        memberships = conn.execute(
            "SELECT call_id,resolved_provider,effective_usage_known,"
            "missing_outer_ordinals,missing_inner_ordinals "
            "FROM llm_usage_daily_call_memberships WHERE local_day=%s AND user_id=%s "
            "ORDER BY call_id,resolved_provider",
            (LOCAL_DAY, attempt_rollup_rows),
        ).fetchall()
        dirty = conn.execute(
            "SELECT count(*) FROM llm_usage_rollup_dirty_days "
            "WHERE rollup_name=%s AND local_day=%s",
            (provider_attempt_rollup.ROLLUP_NAME, LOCAL_DAY),
        ).fetchone()[0]

    assert dimensions == [
        ("asked", "fallback", False, "unknown", None, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, Decimal("0E-8"), [10.0]),
        ("asked", "served", True, "authoritative", None, 1, 0, 0, 0, 0, 93, 1, 23, 1, 0, 0, Decimal("0.40000000"), [90.0]),
        ("asked", "served", True, "estimated", "USD", 2, 2, 0, 0, 0, 60, 2, 10, 0, 2, 0, Decimal("0.01302000"), [30.0, 50.0]),
    ]
    assert memberships == [
        ("call-boundary", "served", True, 1, 0),
        ("call-inner", "served", True, 0, 1),
        ("call-retry", "fallback", False, 0, 0),
        ("call-retry", "served", True, 0, 0),
    ]
    assert dirty == 0


def test_recompute_local_day_is_idempotent_and_matches_raw_attempt_overview(
    attempt_rollup_rows,
):
    first = provider_attempt_rollup.recompute_local_day(LOCAL_DAY)
    second = provider_attempt_rollup.recompute_local_day(LOCAL_DAY)
    assert first == second

    query = admin_usage.UsageQuery(
        start_at_utc=datetime(2026, 6, 30, 16, tzinfo=timezone.utc),
        end_at_utc=datetime(2026, 7, 1, 16, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        user_id=attempt_rollup_rows,
    )
    with db.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            raw = jobs_store._usage_attempt_snapshot(cur, query)["attempts"]
        rolled = conn.execute(
            "SELECT sum(attempts),sum(retry_attempts),sum(failover_attempts),"
            "sum(failed_attempts),sum(possibly_billed_attempts),sum(input_tokens_sum),"
            "sum(output_tokens_sum),sum(cardinality(ttft_samples)) "
            "FROM llm_usage_daily_attempt_dimensions WHERE local_day=%s AND user_id=%s",
            (LOCAL_DAY, attempt_rollup_rows),
        ).fetchone()
        calls = conn.execute(
            "SELECT count(DISTINCT call_id),sum(missing_outer_ordinals),"
            "sum(missing_inner_ordinals) FROM (SELECT call_id,"
            "max(missing_outer_ordinals) AS missing_outer_ordinals,"
            "max(missing_inner_ordinals) AS missing_inner_ordinals "
            "FROM llm_usage_daily_call_memberships WHERE local_day=%s AND user_id=%s "
            "GROUP BY call_id) x",
            (LOCAL_DAY, attempt_rollup_rows),
        ).fetchone()
        rolled_identities = {}
        for scope, provider_column, model_column in (
            ("requested_models", "requested_provider", "requested_model"),
            ("resolved_models", "resolved_provider", "resolved_model"),
        ):
            rows = conn.execute(
                f"SELECT d.{provider_column},d.{model_column},sum(d.attempts),"
                "CASE WHEN sum(d.input_tokens_known_count)=0 THEN NULL "
                "ELSE sum(d.input_tokens_sum) END,"
                "(SELECT count(DISTINCT m.call_id) FROM "
                "llm_usage_daily_call_memberships m WHERE m.local_day=%s "
                "AND m.user_id=%s "
                f"AND m.{provider_column}=d.{provider_column} "
                f"AND m.{model_column}=d.{model_column}) "
                "FROM llm_usage_daily_attempt_dimensions d WHERE d.local_day=%s "
                "AND d.user_id=%s "
                f"GROUP BY d.{provider_column},d.{model_column}",
                (LOCAL_DAY, attempt_rollup_rows, LOCAL_DAY, attempt_rollup_rows),
            ).fetchall()
            rolled_identities[scope] = {
                (row[0], row[1]): (row[2], row[3], row[4]) for row in rows
            }
        cost_rows = conn.execute(
            "SELECT currency,"
            "sum(cost_amount) FILTER (WHERE cost_kind='authoritative'),"
            "sum(cost_amount) FILTER (WHERE cost_kind='estimated'),"
            "sum(authoritative_cost_attempts),sum(estimated_cost_attempts),"
            "sum(unknown_cost_attempts) FROM llm_usage_daily_attempt_dimensions "
            "WHERE local_day=%s AND user_id=%s GROUP BY currency "
            "ORDER BY currency NULLS LAST",
            (LOCAL_DAY, attempt_rollup_rows),
        ).fetchall()

    overview = raw["overview"]
    assert rolled == (
        overview["attempts"],
        overview["retry_attempts"],
        overview["failover_attempts"],
        overview["failed_attempts"],
        overview["possibly_billed_attempts"],
        overview["input_tokens"],
        overview["output_tokens"],
        4,
    )
    assert calls == (
        overview["logical_calls"],
        raw["coverage"]["missing_outer_ordinals"],
        raw["coverage"]["missing_inner_ordinals"],
    )
    for scope in ("requested_models", "resolved_models"):
        assert rolled_identities[scope] == {
            (row["provider"], row["model"]): (
                row["attempts"], row["input_tokens"], row["logical_calls"]
            )
            for row in raw[scope]
        }
    assert [
        {
            "currency": row[0],
            "authoritative_cost": row[1],
            "estimated_cost": row[2],
            "authoritative_attempts": row[3],
            "estimated_attempts": row[4],
            "unknown_attempts": row[5],
        }
        for row in cost_rows
    ] == raw["costs"]


def test_recompute_local_day_executes_one_shared_business_pipeline(
    attempt_rollup_rows, monkeypatch
):
    """Splitting dimensions and memberships must not rerun cohort or pricing."""
    statements = []

    def observe(*, section, statement, params):
        statements.append((section, statement, params))

    monkeypatch.setattr(provider_attempt_rollup, "_rollup_sql_observer", observe)

    result = provider_attempt_rollup.recompute_local_day(LOCAL_DAY)

    assert result == {"status": "ok", "dimensions": 3, "memberships": 4}
    rebuilds = [
        (sql, params)
        for section, sql, params in statements
        if section == "day_rebuild"
    ]
    assert len(rebuilds) == 1
    statement, params = rebuilds[0]
    assert statement.count("turn_cohort AS MATERIALIZED") == 1
    assert statement.count("attempt_base AS MATERIALIZED") == 1
    assert statement.count("correction AS MATERIALIZED") == 1
    assert statement.count("rate_ranges AS MATERIALIZED") == 1
    assert statement.count("priced AS MATERIALIZED") == 1
    assert statement.count(
        "INSERT INTO llm_usage_daily_attempt_dimensions"
    ) == 1
    assert statement.count(
        "INSERT INTO llm_usage_daily_call_memberships"
    ) == 1
    with db.get_pool().connection() as conn:
        explained = conn.execute(
            "EXPLAIN (FORMAT JSON) " + statement, params
        ).fetchone()[0][0]["Plan"]

    def modify_relations(node):
        relations = []
        if node.get("Node Type") == "ModifyTable":
            relations.append(node.get("Relation Name"))
        for child in node.get("Plans", []):
            relations.extend(modify_relations(child))
        return relations

    assert sorted(modify_relations(explained)) == [
        "llm_usage_daily_attempt_dimensions",
        "llm_usage_daily_call_memberships",
    ]


def test_recompute_local_day_rolls_back_both_tables_and_keeps_dirty_claim(
    attempt_rollup_rows,
):
    provider_attempt_rollup.recompute_local_day(LOCAL_DAY)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason) VALUES (%s,%s,'retry')",
            (provider_attempt_rollup.ROLLUP_NAME, LOCAL_DAY),
        )
        before = conn.execute(
            "SELECT count(*),sum(attempts) FROM llm_usage_daily_attempt_dimensions "
            "WHERE local_day=%s",
            (LOCAL_DAY,),
        ).fetchone()
        conn.execute(
            "CREATE OR REPLACE FUNCTION test_reject_attempt_rollup() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER test_reject_attempt_rollup BEFORE INSERT ON "
            "llm_usage_daily_attempt_dimensions FOR EACH STATEMENT EXECUTE FUNCTION "
            "test_reject_attempt_rollup()"
        )
    try:
        result = provider_attempt_rollup.recompute_local_day(LOCAL_DAY)
        assert result == {
            "status": "error",
            "dimensions": 0,
            "memberships": 0,
            "error": "RaiseException",
        }
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT count(*),sum(attempts) FROM "
                "llm_usage_daily_attempt_dimensions WHERE local_day=%s",
                (LOCAL_DAY,),
            ).fetchone() == before
            assert conn.execute(
                "SELECT count(*) FROM llm_usage_daily_call_memberships "
                "WHERE local_day=%s",
                (LOCAL_DAY,),
            ).fetchone()[0] == 4
            assert conn.execute(
                "SELECT count(*) FROM llm_usage_rollup_dirty_days "
                "WHERE rollup_name=%s AND local_day=%s",
                (provider_attempt_rollup.ROLLUP_NAME, LOCAL_DAY),
            ).fetchone()[0] == 1
    finally:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DROP TRIGGER IF EXISTS test_reject_attempt_rollup ON "
                "llm_usage_daily_attempt_dimensions"
            )
            conn.execute("DROP FUNCTION IF EXISTS test_reject_attempt_rollup()")


def test_recompute_local_day_rejects_bad_input_without_raising():
    assert provider_attempt_rollup.recompute_local_day("2026-07-01") == {
        "status": "error",
        "dimensions": 0,
        "memberships": 0,
        "error": "TypeError",
    }
