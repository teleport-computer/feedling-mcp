"""DB-backed contract tests for the operations dashboard aggregations."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user, set_v2_runtime_owner  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="operations dashboard query tests require PostgreSQL",
)

_CHAT_USER = "u_ops_chat_query"
_IMPORT_USER = "u_ops_import_query"
_PRODUCT_USER = "u_ops_product_query"
_BAD_DATE_USER = "u_ops_bad_date_query"


@pytest.fixture(autouse=True)
def _clean_rows():
    def clean() -> None:
        with db.get_pool().connection() as conn:
            conn.execute(
                "DELETE FROM user_logs WHERE user_id IN (%s,%s)",
                (_PRODUCT_USER, _BAD_DATE_USER),
            )
            conn.execute(
                "DELETE FROM chat_messages WHERE user_id IN (%s,%s)",
                (_PRODUCT_USER, _BAD_DATE_USER),
            )
            conn.execute(
                "DELETE FROM v2_effect_outbox WHERE user_id IN (%s,%s)",
                (_CHAT_USER, _IMPORT_USER),
            )
            conn.execute(
                "DELETE FROM v2_terminal_failure_outbox WHERE user_id IN (%s,%s)",
                (_CHAT_USER, _IMPORT_USER),
            )
            conn.execute(
                "DELETE FROM v2_turn_metrics WHERE user_id IN (%s,%s)",
                (_CHAT_USER, _IMPORT_USER),
            )
            conn.execute(
                "DELETE FROM agent_jobs WHERE user_id IN (%s,%s)",
                (_CHAT_USER, _IMPORT_USER),
            )
            conn.execute(
                "DELETE FROM genesis_import_jobs WHERE user_id IN (%s,%s)",
                (_CHAT_USER, _IMPORT_USER),
            )
            conn.execute(
                "DELETE FROM users WHERE user_id IN (%s,%s)",
                (_PRODUCT_USER, _BAD_DATE_USER),
            )

    clean()
    try:
        yield
    finally:
        clean()


def test_recent_chat_reliability_keeps_completed_without_reply_visible():
    seed_user(_CHAT_USER)
    set_v2_runtime_owner(_CHAT_USER)
    job_id, _created = jobs_store.enqueue_job(_CHAT_USER, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed',claimed_at=created_at,"
            "started_at=created_at,finished_at=created_at+interval '20 seconds' "
            "WHERE id=%s",
            (job_id,),
        )
    jobs_store.record_whole_turn_metric(
        job_id,
        _CHAT_USER,
        "chat",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=20_000,
        model_calls=1,
        retries=0,
        failed=False,
        status="ok",
        provider="openai",
        model="gpt-test",
    )

    report = jobs_store.recent_chat_reliability(within_hours=24)

    assert report["outcomes"]["admitted"] >= 1
    assert report["outcomes"]["completed"] >= 1
    assert report["reply_delivery"]["completed_without_final_applied"] >= 1
    assert report["latency"]["turn_p95_sec"] == pytest.approx(20)
    assert report["client_delivery_ack"] is None
    assert report["provider_attempt_accounting"] is None


def test_recent_chat_reliability_counts_only_true_final_reply_same_cohort():
    before = jobs_store.recent_chat_reliability(within_hours=24)
    seed_user(_CHAT_USER)
    set_v2_runtime_owner(_CHAT_USER)

    ordinary_job, _ = jobs_store.enqueue_job(_CHAT_USER, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed',claimed_at=created_at,"
            "started_at=created_at,finished_at=created_at+interval '5 seconds' "
            "WHERE id=%s",
            (ordinary_job,),
        )

    legacy_final_job, _ = jobs_store.enqueue_job(_CHAT_USER, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed',claimed_at=created_at,"
            "started_at=created_at,finished_at=created_at+interval '5 seconds' "
            "WHERE id=%s",
            (legacy_final_job,),
        )

    explicit_final_inflight_job, _ = jobs_store.enqueue_job(_CHAT_USER, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_effect_outbox "
            "(effect_id,user_id,job_id,effect_type,expected_generation,payload,"
            "status,created_at,applied_at) VALUES "
            "('ops-ordinary',%s,%s,'reply',1,'{}'::jsonb,'applied',now(),now()),"
            "('ops-legacy-final',%s,%s,'reply',1,"
            " '{\"reply_through_seq\":7}'::jsonb,'applied',now(),now()),"
            "('ops-explicit-final',%s,%s,'reply_final_fenced_v1',1,"
            " '{}'::jsonb,'applied',now(),now())",
            (
                _CHAT_USER,
                ordinary_job,
                _CHAT_USER,
                legacy_final_job,
                _CHAT_USER,
                explicit_final_inflight_job,
            ),
        )

    report = jobs_store.recent_chat_reliability(within_hours=24)
    before_outcomes = before["outcomes"]
    before_delivery = before["reply_delivery"]
    outcomes = report["outcomes"]
    delivery = report["reply_delivery"]

    assert outcomes["admitted"] == before_outcomes["admitted"] + 3
    assert report["settled_jobs"] == before["settled_jobs"] + 2
    assert delivery["final_effect_jobs"] == before_delivery["final_effect_jobs"] + 2
    assert delivery["final_effect_rows"] == before_delivery["final_effect_rows"] + 2
    assert delivery["final_applied_jobs"] == before_delivery["final_applied_jobs"] + 2
    assert (
        delivery["completed_without_final_applied"]
        == before_delivery["completed_without_final_applied"] + 1
    )
    assert report["server_final_reply_applied_rate"] == pytest.approx(
        delivery["final_applied_jobs"] / outcomes["admitted"]
    )
    assert report["server_final_reply_applied_rate"] <= 1.0
    assert report["terminal_completion_rate"] == pytest.approx(
        outcomes["completed"] / report["settled_jobs"]
    )


def test_recent_genesis_import_health_splits_done_from_artifact_evidence():
    seed_user(_IMPORT_USER)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO genesis_import_jobs "
            "(user_id,job_id,status,source_kind,total_chunks,total_bytes,metadata,"
            " memory_action_count,identity_status,created_at,updated_at,completed_at) "
            "VALUES (%s,'verified','done','history',1,100,"
            " '{\"mode\":\"onboarding\"}'::jsonb,2,'initialized',"
            " now()-interval '30 seconds',now(),now()),"
            "(%s,'unverified','done','history',1,100,"
            " '{\"mode\":\"onboarding\"}'::jsonb,0,'initialized',"
            " now()-interval '30 seconds',now(),now())",
            (_IMPORT_USER, _IMPORT_USER),
        )

    report = db.recent_genesis_import_health(within_hours=24)

    assert report["completed"] >= 2
    assert report["artifact_verified"] >= 1
    assert report["completed_unverified"] >= 1
    rows = {
        row["job_id"]: row for row in report["recent_jobs"]
        if row["user_id"] == _IMPORT_USER
    }
    assert rows["verified"]["artifact_evidence_complete"] is True
    assert rows["unverified"]["artifact_evidence_complete"] is False


def test_recent_admin_product_kpis_are_windowed_and_parse_dates_safely():
    before = db.recent_admin_product_kpis(within_hours=24)
    now = datetime.now(timezone.utc)
    seed_user(_PRODUCT_USER, created_at=now.isoformat())
    seed_user(_BAD_DATE_USER, created_at="2026-99-99T12:00:00Z")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_logs (user_id,stream,ts,item_key,doc) VALUES "
            "(%s,'tracking_events',%s,'session-1',"
            " '{\"type\":\"app_session_end\"}'::jsonb),"
            "(%s,'tracking_events',%s,'session-2',"
            " '{\"type\":\"app_session_end\"}'::jsonb)",
            (_PRODUCT_USER, now.timestamp(), _PRODUCT_USER, now.timestamp()),
        )
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES "
            "(%s,'reply-1',%s,"
            " '{\"role\":\"agent\",\"source\":\"hosted_v2\"}'::jsonb)",
            (_PRODUCT_USER, now.timestamp()),
        )

    report = db.recent_admin_product_kpis(within_hours=24)

    assert report["window_app_users"] == before["window_app_users"] + 1
    assert report["app_sessions"] == before["app_sessions"] + 2
    assert report["new_registered_accounts"] == before["new_registered_accounts"] + 1
    assert (
        report["unparseable_registration_rows"]
        == before["unparseable_registration_rows"] + 1
    )
    assert report["onboarding"]["coverage_complete"] is True
    assert report["onboarding"]["cohort_accounts"] == report["new_registered_accounts"]
    assert report["onboarding"]["first_genuine_reply"] >= 1


def test_recent_token_usage_total_includes_active_user_days_and_average():
    before = jobs_store.recent_token_usage_by_lane(within_hours=24)["total"] or {}
    seed_user(_CHAT_USER)
    set_v2_runtime_owner(_CHAT_USER)
    job_id, _created = jobs_store.enqueue_job(_CHAT_USER, "chat")
    jobs_store.record_whole_turn_metric(
        job_id,
        _CHAT_USER,
        "chat",
        prompt_tokens=100,
        completion_tokens=20,
        latency_ms=1_000,
        model_calls=3,
        retries=2,
        failed=False,
        status="ok",
        usage_reported_calls=3,
        provider="openai",
        model="gpt-test",
    )

    report = jobs_store.recent_token_usage_by_lane(within_hours=24)
    total = report["total"]

    assert report["active_user_day_timezone"] == "Asia/Shanghai"
    assert total is not None
    assert total["turns"] == int(before.get("turns") or 0) + 1
    assert total["model_active_users"] == int(before.get("model_active_users") or 0) + 1
    assert total["active_user_days"] == int(before.get("active_user_days") or 0) + 1
    assert total["model_calls"] == int(before.get("model_calls") or 0) + 3
    assert total["retries"] == int(before.get("retries") or 0) + 2
    assert total["total_tokens"] == int(before.get("total_tokens") or 0) + 120
    assert total["tokens_per_active_user_day"] == pytest.approx(
        total["total_tokens"] / total["active_user_days"]
    )
