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
                "DELETE FROM chat_message_archive WHERE user_id IN (%s,%s)",
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
        status=jobs_store.CHAT_TURN_STATUS_OK,
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


def test_clear_shadow_only_counts_clears_that_happened_after_the_job_finished():
    """镜像对：同样是「completed 且无 effect 行」，只有清除发生在 job 结束之后的
    那一个才算「判不了」。

    宽谓词（该用户清过就算）会把两个都吞进判不了 —— 那是用不确定性洗白清除之后
    新发生的真缺陷。这条测试就是拦那一步的。
    """
    before = jobs_store.recent_chat_reliability(within_hours=24)
    seed_user(_CHAT_USER)
    set_v2_runtime_owner(_CHAT_USER)

    # 结束于清除之前 ⇒ 它的 effect 行会被那次清除删掉 ⇒ 判不了
    shadowed_job, _ = jobs_store.enqueue_job(_CHAT_USER, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed',claimed_at=created_at,"
            "started_at=created_at,finished_at=now()-interval '3 hours' "
            "WHERE id=%s",
            (shadowed_job,),
        )
        conn.execute(
            "INSERT INTO chat_message_archive "
            "(user_id,source_seq,msg_id,ts,doc,storage_generation,"
            " clear_generation,cleared_at) "
            "VALUES (%s,1,'m-1',0,'{}'::jsonb,0,1,now()-interval '2 hours')",
            (_CHAT_USER,),
        )

    # 结束于清除之后 ⇒ 那次清除删不到它 ⇒ 仍然判红
    later_job, _ = jobs_store.enqueue_job(_CHAT_USER, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed',claimed_at=created_at,"
            "started_at=created_at,finished_at=now()-interval '1 hour' "
            "WHERE id=%s",
            (later_job,),
        )

    delivery = jobs_store.recent_chat_reliability(within_hours=24)["reply_delivery"]
    before_delivery = before["reply_delivery"]

    assert (
        delivery["completed_without_final_applied"]
        == before_delivery["completed_without_final_applied"] + 2
    )
    assert (
        delivery["completed_without_final_applied_clear_shadowed"]
        == before_delivery["completed_without_final_applied_clear_shadowed"] + 1
    )


def _complete_chat_job_without_effect(
    user_id: str, *, last_error: str | None = None, finished_hours_ago: int = 1
):
    """一条 completed 的 chat job，且不写任何 final effect 行。"""
    job_id, _ = jobs_store.enqueue_job(user_id, "chat")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='completed',claimed_at=created_at,"
            "started_at=created_at,"
            "finished_at=now()-make_interval(hours => %s),"
            "last_error=%s WHERE id=%s",
            (finished_hours_ago, last_error, job_id),
        )
    return job_id


def test_benign_completion_paths_leave_the_defect_bucket_but_defects_stay():
    """三条合法完成路径各自的痕都要把它移出红桶；没有痕的那条必须留在红桶。

    ⭐ 这一格走 **DB**，不是喂计数：待证命题正是「这些 job 到底进不进分子」，
    在夹具里替它算好就等于绕开被测 SQL（我第一版就是这么错的）。
    ⛔ 常量一律从 shipped 模块读，不许在这里手写字面量。
    """
    before = jobs_store.recent_chat_reliability(within_hours=24)["reply_delivery"]
    seed_user(_CHAT_USER)
    set_v2_runtime_owner(_CHAT_USER)

    def _metric(job_id, *, model_calls, failed, status):
        jobs_store.record_whole_turn_metric(
            job_id, _CHAT_USER, "chat",
            prompt_tokens=None, completion_tokens=None, latency_ms=1_000,
            model_calls=model_calls, retries=0, failed=failed, status=status,
        )

    # benign ①：空 coalesced —— 没调过模型，正常收尾
    _metric(
        _complete_chat_job_without_effect(_CHAT_USER),
        model_calls=0, failed=False, status=jobs_store.CHAT_TURN_STATUS_OK,
    )
    # benign ②：迟到输入交接 —— 调过模型，痕在 turn status 上
    _metric(
        _complete_chat_job_without_effect(_CHAT_USER),
        model_calls=3, failed=False,
        status=jobs_store.CHAT_INPUT_ADVANCED_HANDOFF_STATUS,
    )
    # benign ③：legacy final 重生 —— 痕在 agent_jobs.last_error 上
    _metric(
        _complete_chat_job_without_effect(
            _CHAT_USER, last_error=jobs_store.LEGACY_FINAL_REGENERATION_REASON
        ),
        model_calls=2, failed=False, status=jobs_store.CHAT_TURN_STATUS_OK,
    )

    # ⭐ 镜像①：真缺陷 —— 调过模型、没有任何痕 ⇒ 必须留在红桶
    _metric(
        _complete_chat_job_without_effect(_CHAT_USER),
        model_calls=2, failed=False, status=jobs_store.CHAT_TURN_STATUS_OK,
    )
    # ⭐ 镜像②：slot 兜底恢复也硬写 model_calls=0，但它同时写 failed=True，
    # 而那个 0 的意思是「不知道跑到哪了」。它**不许**被当成 benign 洗白。
    _metric(
        _complete_chat_job_without_effect(_CHAT_USER),
        model_calls=0, failed=True, status="slot_failure:runtimeerror",
    )
    # ⭐ 镜像③：failed 这一格自己也是 best-effort 的，可能没写上。所以 E1 还要求
    # status 正好是「干净收尾」那个值：哨兵 0 配一个非 ok 的状态串 ⇒ 仍然留在红桶。
    # 少了 status 这一条，这一格就会被洗白，而它恰恰是出过事的那批。
    _metric(
        _complete_chat_job_without_effect(_CHAT_USER),
        model_calls=0, failed=False, status="slot_failure:cancelled",
    )
    # ⭐ 镜像④：反过来的一半 —— status 是干净值，但 failed 已经置起来了。
    # 两个字段是 ``record_whole_turn_metric`` 的两个独立入参，可以各自单独写对/写错，
    # 所以两道守卫各自都得有一格只有它能挡的用例；否则去掉任一道都测不出来
    # （突变验证里 M3 一度全绿，就是因为这一格当时缺席）。
    _metric(
        _complete_chat_job_without_effect(_CHAT_USER),
        model_calls=0, failed=True, status=jobs_store.CHAT_TURN_STATUS_OK,
    )

    # ⭐ 重叠格：既是 benign、又落在一次 Clear 之前。两个桶都认领它就会重复计数，
    # 于是 benign+shadowed 会超过总量、红被减两次。benign 优先，shadowed 不许再数。
    # （这一格是突变验证逼出来的：没有它，"把 shadowed 的 NOT benign 去掉" 这个
    # 突变全绿通过 —— 互斥性当时根本没有测试在管。）
    _metric(
        _complete_chat_job_without_effect(
            _CHAT_USER,
            last_error=jobs_store.LEGACY_FINAL_REGENERATION_REASON,
            finished_hours_ago=3,
        ),
        model_calls=2, failed=False, status=jobs_store.CHAT_TURN_STATUS_OK,
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_message_archive "
            "(user_id,source_seq,msg_id,ts,doc,storage_generation,"
            " clear_generation,cleared_at) "
            "VALUES (%s,7,'m-benign-clear',0,'{}'::jsonb,0,1,"
            "now()-interval '2 hours')",
            (_CHAT_USER,),
        )

    after = jobs_store.recent_chat_reliability(within_hours=24)["reply_delivery"]

    def _delta(key: str) -> int:
        return int(after[key]) - int(before[key])

    assert _delta("completed_without_final_applied") == 8, "八条都得进分子"
    assert _delta("completed_without_final_applied_benign") == 4
    assert _delta("completed_without_final_applied_clear_shadowed") == 0, (
        "重叠的那条已被 benign 认领，shadowed 不许再数一次"
    )

    # 三个桶互斥 ⇒ 两个非红桶加起来不会超过总量（超过就说明红被减了两次）
    assert (
        after["completed_without_final_applied_benign"]
        + after["completed_without_final_applied_clear_shadowed"]
        <= after["completed_without_final_applied"]
    )


def test_benign_marker_does_not_rescue_a_job_that_has_no_turn_metrics_row():
    """量具本身是 best-effort：行压根没写时必须留在红桶，不许当成 benign。

    ``model_calls`` 取不到时比较结果是 NULL —— 失效方向必须是假红，不是假绿。
    """
    before = jobs_store.recent_chat_reliability(within_hours=24)["reply_delivery"]
    seed_user(_CHAT_USER)
    set_v2_runtime_owner(_CHAT_USER)

    _complete_chat_job_without_effect(_CHAT_USER)  # 不写 v2_turn_metrics

    after = jobs_store.recent_chat_reliability(within_hours=24)["reply_delivery"]
    assert (
        int(after["completed_without_final_applied"])
        - int(before["completed_without_final_applied"])
    ) == 1
    assert (
        int(after["completed_without_final_applied_benign"])
        - int(before["completed_without_final_applied_benign"])
    ) == 0


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
        status=jobs_store.CHAT_TURN_STATUS_OK,
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
