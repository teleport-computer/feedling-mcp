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


def test_retention_days_default_clamps_short_values_and_accepts_longer(monkeypatch):
    monkeypatch.delenv("FEEDLING_PROVIDER_ATTEMPT_RETENTION_DAYS", raising=False)
    assert provider_attempt_rollup.retention_days() == 400
    for value in ("", "nope", "nan"):
        monkeypatch.setenv("FEEDLING_PROVIDER_ATTEMPT_RETENTION_DAYS", value)
        assert provider_attempt_rollup.retention_days() == 400
    monkeypatch.setenv("FEEDLING_PROVIDER_ATTEMPT_RETENTION_DAYS", "30")
    assert provider_attempt_rollup.retention_days() == 400
    monkeypatch.setenv("FEEDLING_PROVIDER_ATTEMPT_RETENTION_DAYS", "730")
    assert provider_attempt_rollup.retention_days() == 730


def test_next_cutoff_prunes_late_orphan_older_than_published_boundary():
    published = date(2026, 7, 10)
    user_id = f"attempt_reconcile_{uuid.uuid4().hex[:10]}"
    seed_user(user_id)
    attempt_id = _attempt_id()
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,retained_from) VALUES (%s,true,%s)",
            (ROLLUP_NAME, published),
        )
        conn.execute(
            "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,call_id,"
            "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
            "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
            "outcome,error_class,source,completeness,revision) VALUES "
            "(%s,%s,'chat','late-orphan',1,1,'initial','asked','served','asked-model',"
            "'served-model','responses',%s,'completed','succeeded','none',"
            "'runtime_recorder','complete',1)",
            (
                attempt_id,
                user_id,
                datetime(2026, 7, 8, 15, tzinfo=timezone.utc),
            ),
        )

    with db.get_pool().connection() as conn:
        provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=published,
            max_rows=10,
            timeout_ms=5_000,
            now_utc=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
    with db.get_pool().connection() as conn:
        result = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=published + timedelta(days=1),
            max_rows=10,
            timeout_ms=5_000,
            now_utc=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempts WHERE attempt_id=%s",
            (attempt_id,),
        ).fetchone()[0] == 0
        state = conn.execute(
            "SELECT retained_from,retention_pending_from "
            "FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
            (ROLLUP_NAME,),
        ).fetchone()
    assert result["complete"] is True
    assert state == (published + timedelta(days=1), None)


def test_next_cutoff_prunes_turn_moved_behind_published_boundary_and_dirty_claim():
    published = date(2026, 7, 10)
    user_id, job_id, attempt_id = _seed_turn_attempt(local_day=published)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections "
            "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
            "VALUES (%s,%s,2,'late_usage',1)",
            (attempt_id, user_id),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,retained_from) VALUES (%s,true,%s)",
            (ROLLUP_NAME, published),
        )
        moved_at = datetime.combine(
            published - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ) + timedelta(hours=4)
        conn.execute(
            "UPDATE v2_turn_metrics SET created_at=%s,updated_at=clock_timestamp() "
            "WHERE job_id=%s",
            (moved_at, job_id),
        )
    assert _dirty_days() == [published - timedelta(days=1), published]

    with db.get_pool().connection() as conn:
        result = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=published + timedelta(days=1),
            max_rows=20,
            timeout_ms=5_000,
            now_utc=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempts WHERE attempt_id=%s",
            (attempt_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempt_corrections "
            "WHERE attempt_id=%s", (attempt_id,),
        ).fetchone()[0] == 0
        state = conn.execute(
            "SELECT retained_from,retention_pending_from "
            "FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
            (ROLLUP_NAME,),
        ).fetchone()
    assert result["complete"] is True
    assert _dirty_days() == []
    assert state == (published + timedelta(days=1), None)


def test_retention_pending_fence_precedes_multi_page_destructive_state():
    cutoff = date(2026, 7, 10)
    _seed_turn_attempt(local_day=cutoff - timedelta(days=1))
    _seed_turn_attempt(local_day=cutoff - timedelta(days=2))
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)", (ROLLUP_NAME,),
        )
    with db.get_pool().connection() as conn:
        first = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=1,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        state = conn.execute(
            "SELECT retained_from,retention_pending_from "
            "FROM llm_usage_rollup_watermarks WHERE rollup_name=%s",
            (ROLLUP_NAME,),
        ).fetchone()
    assert first["complete"] is False
    assert state == (None, cutoff)


def test_max_retention_rows_is_one_global_fair_budget_and_cascades_are_separate():
    cutoff = date(2026, 7, 10)
    user_id, _job_id_value, attempt_id = _seed_turn_attempt(
        local_day=cutoff - timedelta(days=1)
    )
    old_day = cutoff - timedelta(days=1)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_provider_attempt_corrections "
            "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
            "VALUES (%s,%s,2,'late_usage',1)", (attempt_id, user_id),
        )
        conn.execute(
            "INSERT INTO llm_usage_daily_attempt_dimensions "
            "(local_day,user_id,cohort_lane,requested_provider,requested_model,"
            "resolved_provider,resolved_model,effective_usage_known,cost_kind,"
            "attempts,unknown_cost_attempts) VALUES (%s,%s,'chat','asked','asked-model',"
            "'served','served-model',false,'unknown',1,1)", (old_day, user_id),
        )
        conn.execute(
            "INSERT INTO llm_usage_daily_call_memberships "
            "(local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,"
            "resolved_provider,resolved_model,effective_usage_known) VALUES "
            "(%s,%s,'chat','budget-call','asked','asked-model','served',"
            "'served-model',false)", (old_day, user_id),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason,generation) VALUES (%s,%s,'source_change',0)",
            (ROLLUP_NAME, old_day),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)", (ROLLUP_NAME,),
        )

    progress = set()
    cascaded = 0
    for tick in range(8):
        with db.get_pool().connection() as conn:
            result = provider_attempt_rollup._run_retention_batch(
                conn,
                cutoff=cutoff,
                max_rows=1,
                timeout_ms=5_000,
                now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc)
                + timedelta(seconds=tick),
            )
        explicit = {
            "attempts": result["attempts_deleted"],
            "dimensions": result["dimensions_deleted"],
            "memberships": result["memberships_deleted"],
            "dirty_days": result["dirty_days_deleted"],
        }
        assert sum(explicit.values()) <= 1
        progress.update(key for key, value in explicit.items() if value)
        cascaded += result["corrections_cascaded"]
        if result["complete"]:
            break
    assert progress == {"attempts", "dimensions", "memberships", "dirty_days"}
    assert cascaded == 1
    assert result["complete"] is True


def test_retention_is_bounded_cascades_corrections_and_publishes_watermark_last():
    cutoff = date(2026, 7, 10)
    old_user, _old_job, old_attempt = _seed_turn_attempt(
        local_day=cutoff - timedelta(days=1)
    )
    keep_user, _keep_job, keep_attempt = _seed_turn_attempt(local_day=cutoff)
    orphan_user = f"attempt_reconcile_{uuid.uuid4().hex[:10]}"
    seed_user(orphan_user)
    orphan_attempt = _attempt_id()
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE llm_provider_attempts SET state='completed',outcome='succeeded',"
            "finished_at=started_at,completeness='complete',revision=1 "
            "WHERE attempt_id=ANY(%s)",
            ([old_attempt, keep_attempt],),
        )
        conn.execute(
            "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,call_id,"
            "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
            "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
            "outcome,error_class,usage_known,possibly_billed,source,completeness,revision) "
            "VALUES (%s,%s,'chat','orphan-old',1,1,'initial','asked','served',"
            "'asked-model','served-model','responses',%s,'completed','succeeded','none',"
            "false,false,'runtime_recorder','complete',1)",
            (
                orphan_attempt,
                orphan_user,
                datetime(2026, 7, 8, 15, 59, tzinfo=timezone.utc),
            ),
        )
        for attempt_id, user_id in (
            (old_attempt, old_user),
            (orphan_attempt, orphan_user),
        ):
            conn.execute(
                "INSERT INTO llm_provider_attempt_corrections "
                "(attempt_id,user_id,revision,reason_code,input_tokens_delta) "
                "VALUES (%s,%s,2,'late_usage',1)",
                (attempt_id, user_id),
            )
        conn.execute(
            "INSERT INTO llm_usage_daily_attempt_dimensions "
            "(local_day,user_id,cohort_lane,requested_provider,requested_model,"
            "resolved_provider,resolved_model,effective_usage_known,cost_kind,"
            "attempts,unknown_cost_attempts) VALUES (%s,%s,'chat','asked','asked-model',"
            "'served','served-model',false,'unknown',1,1)",
            (cutoff - timedelta(days=1), old_user),
        )
        conn.execute(
            "INSERT INTO llm_usage_daily_call_memberships "
            "(local_day,user_id,cohort_lane,call_id,requested_provider,requested_model,"
            "resolved_provider,resolved_model,effective_usage_known) "
            "VALUES (%s,%s,'chat','old-call','asked','asked-model','served',"
            "'served-model',false)",
            (cutoff - timedelta(days=1), old_user),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason,generation) VALUES (%s,%s,'source_change',0)",
            (ROLLUP_NAME, cutoff - timedelta(days=1)),
        )
        conn.execute(
            "INSERT INTO llm_rate_cards (provider,model,version,currency,effective_at) "
            "VALUES ('served','served-model','retention-test','USD','2020-01-01Z')"
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)",
            (ROLLUP_NAME,),
        )

    with db.get_pool().connection() as conn:
        first = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=1,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert first["status"] == "ok"
    assert first["attempts_deleted"] == 1
    assert first["complete"] is False
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT retained_from FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name=%s", (ROLLUP_NAME,),
        ).fetchone()[0] is None

    result = first
    for tick in range(1, 10):
        with db.get_pool().connection() as conn:
            result = provider_attempt_rollup._run_retention_batch(
                conn,
                cutoff=cutoff,
                max_rows=1,
                timeout_ms=5_000,
                now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc)
                + timedelta(seconds=tick),
            )
        assert sum(
            result[key]
            for key in (
                "attempts_deleted",
                "dimensions_deleted",
                "memberships_deleted",
                "dirty_days_deleted",
            )
        ) <= 1
        if result["complete"]:
            break
    assert result["status"] == "ok"
    assert result["complete"] is True
    assert result["retained_from"] == cutoff
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT attempt_id FROM llm_provider_attempts ORDER BY attempt_id"
        ).fetchall() == [(keep_attempt,)]
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempt_corrections"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM llm_usage_daily_attempt_dimensions "
            "WHERE local_day<%s", (cutoff,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM llm_usage_daily_call_memberships "
            "WHERE local_day<%s", (cutoff,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM llm_usage_rollup_dirty_days "
            "WHERE rollup_name=%s AND local_day<%s", (ROLLUP_NAME, cutoff),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM llm_rate_cards").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM v2_turn_metrics").fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(*) FROM users WHERE user_id=ANY(%s)",
            ([old_user, keep_user, orphan_user],),
        ).fetchone()[0] == 3


def test_retention_skips_locked_attempt_and_does_not_publish_early():
    cutoff = date(2026, 7, 10)
    locked = _seed_turn_attempt(local_day=cutoff - timedelta(days=2))[2]
    unlocked = _seed_turn_attempt(local_day=cutoff - timedelta(days=1))[2]
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)", (ROLLUP_NAME,),
        )
    with psycopg.connect(os.environ["DATABASE_URL"]) as holder:
        holder.execute(
            "SELECT attempt_id FROM llm_provider_attempts WHERE attempt_id=%s FOR UPDATE",
            (locked,),
        )
        with db.get_pool().connection() as conn:
            result = provider_attempt_rollup._run_retention_batch(
                conn,
                cutoff=cutoff,
                max_rows=1,
                timeout_ms=5_000,
                now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        assert result["attempts_deleted"] == 1
        assert result["complete"] is False
        with db.get_pool().connection() as conn:
            remaining = conn.execute(
                "SELECT attempt_id FROM llm_provider_attempts ORDER BY attempt_id"
            ).fetchall()
            assert remaining == [(locked,)]
            assert unlocked not in {row[0] for row in remaining}
            assert conn.execute(
                "SELECT retained_from FROM llm_usage_rollup_watermarks "
                "WHERE rollup_name=%s", (ROLLUP_NAME,),
            ).fetchone()[0] is None


def test_retention_parent_selection_plan_is_bounded_and_index_driven(monkeypatch):
    cutoff = date(2026, 7, 10)
    user_id, _job_id_value, _attempt_id_value = _seed_turn_attempt(
        local_day=cutoff - timedelta(days=1)
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,call_id,"
            "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
            "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
            "outcome,error_class,source,completeness,revision) "
            "SELECT '40000000-0000-5000-8000-'||lpad(to_hex(n),12,'0'),%s,'chat',"
            "'current-retention-call-'||n,1,1,'initial','asked','served','asked-model',"
            "'served-model','responses','2026-08-01T00:00:00Z'::timestamptz"
            "+(n*interval '1 second'),'completed','succeeded','none',"
            "'runtime_recorder','complete',1 FROM generate_series(1,3000) n",
            (user_id,),
        )
        conn.execute("ANALYZE llm_provider_attempts")
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)", (ROLLUP_NAME,),
        )
    observed = []
    monkeypatch.setattr(
        provider_attempt_rollup,
        "_retention_sql_observer",
        lambda **fields: observed.append(fields),
    )
    with db.get_pool().connection() as conn:
        provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=1,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    statements = {
        item["section"]: item["statement"] for item in observed
        if item["statement"] is not None
    }
    cutoff_at = datetime(2026, 7, 9, 16, tzinfo=timezone.utc)
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL enable_seqscan=off")
            plans = [
                conn.execute(
                    "EXPLAIN (FORMAT JSON) "
                    + statements["attempt_delete_job"],
                    (cutoff_at, 10),
                ).fetchone()[0][0],
                conn.execute(
                    "EXPLAIN (FORMAT JSON) "
                    + statements["attempt_delete_orphan"],
                    (cutoff_at, 10),
                ).fetchone()[0][0],
            ]

    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    indexes = {
        node.get("Index Name")
        for plan in plans
        for node in nodes(plan["Plan"])
        if node.get("Index Name")
    }
    details = [
        {
            key: node.get(key)
            for key in ("Node Type", "Relation Name", "Alias", "Index Name", "Filter")
        }
        for plan in plans
        for node in nodes(plan["Plan"])
    ]
    assert "ix_v2_turn_metrics_created_at" in indexes
    assert "ix_llm_provider_attempts_runtime_job" in indexes, details
    assert "ix_llm_provider_attempts_retention_started" in indexes


def test_retention_same_published_cutoff_checks_late_data_without_mutation(monkeypatch):
    cutoff = date(2026, 7, 10)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,retained_from) VALUES (%s,true,%s)",
            (ROLLUP_NAME, cutoff),
        )
    observed = []
    monkeypatch.setattr(
        provider_attempt_rollup,
        "_retention_sql_observer",
        lambda **fields: observed.append(fields),
    )
    with db.get_pool().connection() as conn:
        result = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=500,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert result["complete"] is True
    assert result["retained_from"] == cutoff
    assert result["attempts_deleted"] == 0
    assert result["dimensions_deleted"] == 0
    assert result["memberships_deleted"] == 0
    assert result["dirty_days_deleted"] == 0
    assert result["retention_pending_from"] is None
    assert {item["section"] for item in observed} >= {
        "attempt_delete_job",
        "attempt_delete_orphan",
    }


def test_retention_skips_expired_turns_without_attempts_before_candidate_limit():
    cutoff = date(2026, 7, 10)
    user_id = f"attempt_reconcile_{uuid.uuid4().hex[:10]}"
    seed_user(user_id)
    jobs = [_job_id() for _ in range(5)]
    attempt_id = _attempt_id()
    with db.get_pool().connection() as conn:
        for offset, job_id in enumerate(jobs, start=1):
            created_at = datetime.combine(
                cutoff - timedelta(days=offset),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            conn.execute(
                "INSERT INTO v2_turn_metrics "
                "(job_id,user_id,lane,model_calls,status,created_at,updated_at) "
                "VALUES (%s,%s,'chat',1,'done',%s,%s)",
                (job_id, user_id, created_at, created_at),
            )
        conn.execute(
            "INSERT INTO llm_provider_attempts (attempt_id,user_id,lane,job_id,call_id,"
            "outer_attempt_ordinal,inner_attempt_ordinal,retry_kind,requested_provider,"
            "resolved_provider,requested_model,resolved_model,transport,started_at,state,"
            "outcome,error_class,source,completeness,revision) VALUES "
            "(%s,%s,'chat',%s,'oldest-only',1,1,'initial','asked','served',"
            "'asked-model','served-model','responses',%s,'completed','succeeded','none',"
            "'runtime_recorder','complete',1)",
            (
                attempt_id,
                user_id,
                jobs[-1],
                datetime(2026, 7, 5, tzinfo=timezone.utc),
            ),
        )
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)", (ROLLUP_NAME,),
        )
    with db.get_pool().connection() as conn:
        result = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=1,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert result["attempts_deleted"] == 1
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempts WHERE attempt_id=%s",
            (attempt_id,),
        ).fetchone()[0] == 0


def test_retention_failure_and_cancel_roll_back_and_remain_fail_open(monkeypatch):
    cutoff = date(2026, 7, 10)
    attempt_id = _seed_turn_attempt(local_day=cutoff - timedelta(days=1))[2]
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks (rollup_name,bootstrap_complete) "
            "VALUES (%s,true)", (ROLLUP_NAME,),
        )

    def fail_after_attempt_delete(**fields):
        if fields["section"] == "after_attempt_delete":
            raise RuntimeError("injected retention failure")

    monkeypatch.setattr(
        provider_attempt_rollup, "_retention_sql_observer", fail_after_attempt_delete
    )
    with db.get_pool().connection() as conn:
        failed = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=10,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert failed["status"] == "error"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempts WHERE attempt_id=%s",
            (attempt_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT retained_from FROM llm_usage_rollup_watermarks "
            "WHERE rollup_name=%s", (ROLLUP_NAME,),
        ).fetchone()[0] is None

    cancel = threading.Event()

    def cancel_after_attempt_delete(**fields):
        if fields["section"] == "after_attempt_delete":
            cancel.set()

    monkeypatch.setattr(
        provider_attempt_rollup, "_retention_sql_observer", cancel_after_attempt_delete
    )
    with db.get_pool().connection() as conn:
        cancelled = provider_attempt_rollup._run_retention_batch(
            conn,
            cutoff=cutoff,
            max_rows=10,
            timeout_ms=5_000,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            cancel_event=cancel,
        )
    assert cancelled["status"] == "cancelled"
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM llm_provider_attempts WHERE attempt_id=%s",
            (attempt_id,),
        ).fetchone()[0] == 1


def test_retention_runs_only_after_current_dirty_work_is_finished(monkeypatch):
    day = date(2026, 7, 20)
    _seed_turn_attempt(local_day=day)
    calls = []
    monkeypatch.setattr(
        provider_attempt_rollup,
        "_run_retention_batch",
        lambda *args, **kwargs: calls.append(kwargs) or {
            "status": "ok", "complete": False, "attempts_deleted": 0,
            "dimensions_deleted": 0, "memberships_deleted": 0,
            "dirty_days_deleted": 0, "retained_from": None,
        },
    )
    pending = provider_attempt_rollup.run_maintenance_tick(max_days=0)
    assert pending["dirty_pending"] is True
    assert calls == []
    clean = provider_attempt_rollup.run_maintenance_tick(max_days=5)
    assert clean["dirty_pending"] is False
    assert len(calls) == 1


def test_replay_and_dirty_selection_never_rebuild_before_retained_from(monkeypatch):
    cutoff = date(2026, 7, 10)
    old_day = cutoff - timedelta(days=1)
    new_day = cutoff + timedelta(days=1)
    _seed_turn_attempt(local_day=old_day)
    _seed_turn_attempt(local_day=new_day)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_watermarks "
            "(rollup_name,bootstrap_complete,retained_from,replay_generation) "
            "VALUES (%s,false,%s,1)", (ROLLUP_NAME, cutoff),
        )
        state = provider_attempt_rollup._bootstrap_batch(
            conn,
            max_dirty_days=10,
            now_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            timeout_ms=5_000,
        )
    assert state["completed_through_day"] == new_day
    assert _dirty_days() == [new_day]

    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO llm_usage_rollup_dirty_days "
            "(rollup_name,local_day,reason,generation) VALUES (%s,%s,'source_change',1) "
            "ON CONFLICT DO NOTHING", (ROLLUP_NAME, old_day),
        )
    rebuilt = []
    monkeypatch.setattr(
        provider_attempt_rollup,
        "recompute_local_day",
        lambda day, **_kwargs: rebuilt.append(day) or {
            "status": "error", "error": "stop-after-observation",
        },
    )
    provider_attempt_rollup.run_maintenance_tick(max_days=1)
    assert rebuilt == [new_day]


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
    clean = {**second, "dirty_pending": False}
    assert serve_worker._reporting_lane_delay(clean, 300.0) == 300.0
    clean["source_backlog"]["attempt"] = True
    assert serve_worker._reporting_lane_delay(clean, 300.0) == 5.0
    clean["status"] = "error"
    assert serve_worker._reporting_lane_delay(clean, 300.0) == 300.0


def test_default_page_catches_static_batch_above_old_2000_page_limit():
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


def test_mixed_worker_loop_catches_up_pending_lane_without_accelerating_error_lane(
    monkeypatch,
):
    usage_calls = []
    attempt_calls = []
    monkeypatch.setattr(serve_worker.usage_rollup, "enabled", lambda: True)
    monkeypatch.setattr(provider_attempt_rollup, "enabled", lambda: True)
    monkeypatch.setattr(serve_worker, "_REPORTING_CATCH_UP_SECONDS", 0.01)

    def pending_usage(**_kwargs):
        usage_calls.append(1)
        return {
            "status": "ok",
            "bootstrap_complete": True,
            "dirty_pending": True,
            "source_backlog": {},
        }

    def failed_attempt(**_kwargs):
        attempt_calls.append(1)
        return {"status": "error", "error": "Injected"}

    monkeypatch.setattr(serve_worker.usage_rollup, "run_maintenance_tick", pending_usage)
    monkeypatch.setattr(provider_attempt_rollup, "run_maintenance_tick", failed_attempt)

    async def run_loop():
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker._usage_rollup_loop(stop, interval=10.0))
        while len(usage_calls) < 3:
            await asyncio.sleep(0.003)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(run_loop())
    assert len(usage_calls) >= 3
    assert attempt_calls == [1]
