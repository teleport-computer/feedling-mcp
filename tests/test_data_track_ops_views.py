"""Operations dashboard: evidence wording, windows, and dispatch (no DB)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import admin_core  # noqa: E402
from admin import data_track as dt  # noqa: E402


def _imports() -> dict:
    return {
        "window_hours": 24,
        "started": 4,
        "users": 3,
        "completed": 3,
        "artifact_verified": 2,
        "completed_unverified": 1,
        "failed": 1,
        "processing": 0,
        "stuck_over_15m": 0,
        "terminal_success_rate": 0.75,
        "artifact_verified_rate": 2 / 3,
        "p50_complete_sec": 20,
        "p95_complete_sec": 50,
        "failure_reasons": [{"error_code": "provider_call_failed:transient", "count": 1}],
        "recent_jobs": [{
            "user_id": "usr_0123456789abcdef",
            "job_id": "job-1",
            "status": "done",
            "source_kind": "history",
            "import_mode": "onboarding",
            "artifact_evidence_complete": False,
            "has_identity_evidence": True,
            "has_source_material": True,
            "memory_action_count": 0,
            "identity_status": "initialized",
            "error_code": "",
            "created_at": "2026-08-03T00:00:00Z",
            "updated_at": "2026-08-03T00:01:00Z",
            "age_since_update_sec": 10,
        }],
    }


def _chat() -> dict:
    return {
        "window_hours": 24,
        "outcomes": {
            "admitted": 10,
            "started": 10,
            "completed": 9,
            "failed": 1,
            "expired": 0,
            "superseded": 0,
            "in_flight": 0,
            "users": 4,
        },
        "reply_delivery": {
            "final_effect_jobs": 9,
            "final_effect_rows": 9,
            "final_applied_jobs": 9,
            "final_pending_jobs": 0,
            "final_reconciliation_jobs": 0,
            "final_discarded_jobs": 0,
            "duplicate_final_effect_jobs": 0,
            "completed_without_final_applied": 0,
        },
        "failure_delivery": {
            "failure_rows": 1,
            "fallback_reply_delivered": 1,
            "fallback_reply_pending": 0,
            "error_status_delivered": 1,
            "runtime_error_delivered": 1,
        },
        "settled_jobs": 10,
        "terminal_completion_rate": 0.9,
        "server_final_reply_applied_rate": 0.9,
        "latency": {
            "queue_p50_sec": 1,
            "queue_p95_sec": 2,
            "queue_p99_sec": 3,
            "processing_p50_sec": 20,
            "processing_p95_sec": 40,
            "processing_p99_sec": 50,
            "turn_p50_sec": 21,
            "turn_p95_sec": 42,
            "turn_p99_sec": 53,
            "server_applied_p50_sec": 22,
            "server_applied_p95_sec": 44,
            "server_applied_p99_sec": 55,
        },
        "model_breakdown": [{
            "provider": "openai",
            "model": "gpt-test",
            "turns": 10,
            "model_calls": 12,
            "retries": 2,
            "failed_turns": 1,
            "p50_ms": 20_000,
            "p95_ms": 40_000,
            "p99_ms": 50_000,
        }],
        "failure_reasons": [{"code": "turn_failed:providererror", "count": 1}],
        "recent_jobs": [],
        "client_delivery_ack": None,
        "provider_attempt_accounting": None,
    }


def _runtime() -> dict:
    return {
        "pool": {
            "live_workers": 1,
            "capacity": 4,
            "inflight": 0,
            "pending": 0,
            "oldest_pending_age_sec": None,
        }
    }


def _product(*, covered: bool = True) -> dict:
    return {
        "window_hours": 24,
        "window_app_users": 12,
        "app_sessions": 30,
        "new_registered_accounts": 4,
        "unparseable_registration_rows": 1,
        "onboarding": {
            "definition": "registered_cohort_to_first_genuine_reply",
            "cohort_accounts": 4,
            "configured": 3 if covered else None,
            "content_ready": 3 if covered else None,
            "first_genuine_reply": 2 if covered else None,
            "completion_rate": 0.5 if covered else None,
            "coverage_complete": covered,
        },
    }


def _usage() -> dict:
    return {
        "window_hours": 24,
        "active_user_day_timezone": "Asia/Shanghai",
        "lanes": {},
        "total": {
            "turns": 20,
            "model_active_users": 5,
            "active_user_days": 8,
            "model_calls": 25,
            "retries": 2,
            "failed_turns": 1,
            "usage_reported_calls": 20,
            "usage_coverage": 0.8,
            "total_tokens": 80_000,
            "tokens_per_active_user_day": 10_000.0,
        },
    }


def test_overview_never_claims_client_ack_or_provider_accounting():
    with admin_core.bind("view=overview&hours=24"):
        page = dt._render_ops_overview_page(
            _imports(), _chat(), _runtime(), _product(), _usage(), within_hours=24
        )

    assert "客户端接收或已读 ACK：<b>不可用</b>" in page
    assert "final reply applied / admitted Runtime turns" in page
    assert "final reply applied / 已结算 chat" not in page
    assert "真实 provider attempt / possibly-billed / authoritative cost" in page
    assert "不可判定" in page
    assert "记忆导入" in page
    assert "聊天可靠性" in page
    assert "Token 与模型" in page
    assert "窗口内 App 活跃账号" in page
    assert "V2 模型活跃用户日" in page
    assert "平均每个 V2 活跃用户日 Token" in page
    assert "10.0k" in page
    assert "不是北京自然日 DAU" in page


def test_overview_keeps_incomplete_onboarding_and_usage_unknown():
    with admin_core.bind("view=overview&hours=24"):
        page = dt._render_ops_overview_page(
            _imports(), _chat(), _runtime(), _product(covered=False), None,
            within_hours=24,
        )

    assert "注册 cohort 事件覆盖不完整" in page
    assert "完成率显示未知" in page
    assert "<div class='metric-value'>—</div>" in page
    assert "不会被伪装成 0" in page


def test_import_page_splits_terminal_done_from_artifact_evidence():
    with admin_core.bind("view=imports&hours=24"):
        page = dt._render_imports_page(_imports(), within_hours=24)

    assert "Terminal done" in page
    assert "Artifact 证据通过" in page
    assert "Done 但证据不足" in page
    assert "终态完成，证据不足" in page
    assert "provider_call_failed:transient" in page
    assert "不读取导入正文" in page


def test_chat_page_defines_server_applied_not_device_received():
    with admin_core.bind("view=chat&hours=24"):
        page = dt._render_chat_reliability_page(_chat(), within_hours=24)

    assert "服务端 applied" in page
    assert "不是客户端 ACK" in page
    assert "不等于设备收到或用户已读" in page
    assert "Admitted → final reply 服务端 applied" in page
    assert "终态完成率" in page
    assert "普通中间 reply 不计" in page
    assert "消息进入 Runtime" in page
    assert "生成 final effect" in page


def test_latency_page_has_four_stages_and_p99():
    with admin_core.bind("view=latency&hours=24"):
        page = dt._render_latency_page(_chat(), within_hours=24)

    assert "排队" in page
    assert "模型与工具处理" in page
    assert "整轮 job" in page
    assert "用户侧最接近值" in page
    assert "p99" in page
    assert "whole-turn" in page


def test_admin_core_dispatches_ops_views_and_preserves_selected_window(monkeypatch):
    seen: dict[str, int] = {}

    def product(**kwargs):
        seen["product"] = kwargs["within_hours"]
        return _product()

    def usage(**kwargs):
        seen["usage"] = kwargs["within_hours"]
        return _usage()

    monkeypatch.setattr(admin_core.db, "recent_genesis_import_health", lambda **_kw: _imports())
    monkeypatch.setattr(admin_core.jobs_store, "recent_chat_reliability", lambda **_kw: _chat())
    monkeypatch.setattr(admin_core.jobs_store, "recent_runtime_health", lambda **_kw: _runtime())
    monkeypatch.setattr(admin_core.db, "recent_admin_product_kpis", product)
    monkeypatch.setattr(admin_core.jobs_store, "recent_token_usage_by_lane", usage)

    overview = admin_core.page_html("view=overview&hours=168")
    imports = admin_core.page_html("view=imports&hours=168")
    chat = admin_core.page_html("view=chat&hours=168")
    latency = admin_core.page_html("view=latency&hours=168")

    assert "最近 168 小时" in overview
    assert "记忆导入" in imports
    assert "聊天可靠性" in chat
    assert "回复延迟" in latency
    for page in (overview, imports, chat, latency):
        assert "hours=168" in page
    assert seen == {"product": 168, "usage": 168}


def test_ops_window_rejects_arbitrary_hours():
    with admin_core.bind("view=overview&hours=72"):
        assert dt._ops_window_hours() == 24
