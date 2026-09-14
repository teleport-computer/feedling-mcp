"""Memory-maintenance（capture/dream/migrate）失败退避（2026-07-06 修复的回归测试）。

背景：坏钥用户的 capture 窗口永远失败，而 min_interval 只看
last_capture_completed_at（仅 completed 时更新），failed 属
CAPTURE_RETRYABLE_TERMINAL 会立即重新入队 → 每个调度 tick（现网 ~75s）
重试一次、无上限，prod 实测 40 用户 2 天灌了 7412 条 failed，还在持续烧
用户的 key/配额。

契约：
- terminal=failed 后，同一窗口在指数退避窗口内不再入队
  （reason="failure_backoff"）；base=FEEDLING_MAINTENANCE_FAIL_BACKOFF_BASE_SEC
  （默认 600s）× 2^(streak-1)，上限 FEEDLING_MAINTENANCE_FAIL_BACKOFF_MAX_SEC
  （默认 6h）。
- completed 重置 streak。
- 手动 force（debug 面板）绕过退避。
- dream / migrate 两条 maintenance lane 同样退避。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="feedling-capture-backoff-test-")
os.environ.setdefault("FEEDLING_DATA_DIR", _DATA_DIR)
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from proactive import capture_scheduler  # noqa: E402
from proactive import dream_scheduler  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402

from conftest import seed_user  # noqa: E402


def _store(tmp_path, monkeypatch, user_id: str):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    core_store._stores.clear()
    store = core_store.UserStore(user_id)
    seed_user(store.user_id)
    return store


def _fail_job(store, job, fail_at, recorder):
    """镜像 proactive_core.job_status 的两步：job 本体落 failed + 调 recorder。
    直接调 recorder 是为了用显式 now 卡退避边界（job_status 只用墙钟）。"""
    failed = store.update_proactive_job(job["job_id"], {"status": "failed"})
    recorder(store, failed, status="failed", now=fail_at)


def _seed_chat(store, msg_id: str) -> float:
    msg = store.append_chat("user", "chat", {
        "id": msg_id,
        "body_ct": f"ct_{msg_id}",
        "nonce": f"nonce_{msg_id}",
        "K_user": f"ku_{msg_id}",
        "K_enclave": f"ke_{msg_id}",
    })
    return float(msg["ts"])


def test_capture_failed_window_backs_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "10")
    store = _store(tmp_path, monkeypatch, "usr_capture_backoff")
    t0 = _seed_chat(store, "m1")

    t1 = t0 + 20
    first = capture_scheduler.tick_quiet_capture(store, now=t1)
    assert first["enqueued"] is True
    job = first["job"]

    fail_at = t1 + 5
    _fail_job(store, job, fail_at, capture_scheduler.record_capture_job_status)

    # 退避窗口内（streak=1 → 600s）：不得重新入队
    retry = capture_scheduler.tick_quiet_capture(store, now=fail_at + 80)
    assert retry["enqueued"] is False
    assert retry["reason"] == "failure_backoff"

    # 退避期满：允许重试同一窗口
    after = capture_scheduler.tick_quiet_capture(store, now=fail_at + 601)
    assert after["enqueued"] is True


def test_capture_backoff_doubles_then_resets_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "10")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    store = _store(tmp_path, monkeypatch, "usr_capture_backoff_double")
    t0 = _seed_chat(store, "m1")

    t1 = t0 + 20
    first = capture_scheduler.tick_quiet_capture(store, now=t1)
    assert first["enqueued"] is True
    _fail_job(store, first["job"], t1 + 5, capture_scheduler.record_capture_job_status)
    second = capture_scheduler.tick_quiet_capture(store, now=t1 + 5 + 601)
    assert second["enqueued"] is True
    fail2_at = t1 + 5 + 610
    _fail_job(store, second["job"], fail2_at, capture_scheduler.record_capture_job_status)

    # streak=2 → 1200s：600s 后仍在退避
    blocked = capture_scheduler.tick_quiet_capture(store, now=fail2_at + 700)
    assert blocked["enqueued"] is False
    assert blocked["reason"] == "failure_backoff"

    third = capture_scheduler.tick_quiet_capture(store, now=fail2_at + 1201)
    assert third["enqueued"] is True

    # completed 重置 streak：新窗口失败后回到基础退避
    done = store.update_proactive_job(third["job"]["job_id"], {"status": "completed"})
    capture_scheduler.record_capture_job_status(
        store, done, status="completed", now=fail2_at + 1210,
    )
    state = capture_scheduler.load_capture_state(store)
    assert int(state.get("capture_fail_streak") or 0) == 0


def test_force_capture_bypasses_failure_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "10")
    store = _store(tmp_path, monkeypatch, "usr_capture_backoff_force")
    t0 = _seed_chat(store, "m1")

    t1 = t0 + 20
    first = capture_scheduler.tick_quiet_capture(store, now=t1)
    assert first["enqueued"] is True
    _fail_job(store, first["job"], t1 + 5, capture_scheduler.record_capture_job_status)

    forced = capture_scheduler.force_capture(store, now=t1 + 30)
    assert forced["enqueued"] is True


def test_dream_failed_job_backs_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    store = _store(tmp_path, monkeypatch, "usr_dream_backoff")
    db.memory_replace_all(store.user_id, [{
        "v": 1,
        "id": "mem_backoff",
        "type": "fact",
        "owner_user_id": store.user_id,
        "visibility": "shared",
        "body_ct": "ct_mem",
        "nonce": "nonce_mem",
        "K_user": "ku_mem",
        "K_enclave": "ke_mem",
        "occurred_at": "2026-06-20T00:00:00Z",
        "created_at": "2026-06-20T00:00:00Z",
        "updated_at": "2026-06-20T00:00:00Z",
        "status": "active",
        "importance": 0.6,
        "pulse": 0.3,
    }])

    first = dream_scheduler.tick_memory_dream(store, now=1000.0)
    assert first["enqueued"] is True
    _fail_job(store, first["job"], 1005.0, dream_scheduler.record_dream_job_status)

    retry = dream_scheduler.tick_memory_dream(store, now=1080.0)
    assert retry["enqueued"] is False
    assert retry["reason"] == "failure_backoff"

    after = dream_scheduler.tick_memory_dream(store, now=1005.0 + 601)
    assert after["enqueued"] is True


def test_migrate_failed_job_backs_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "1")
    store = _store(tmp_path, monkeypatch, "usr_migrate_backoff")

    now = 2_000_000.0
    first = capture_scheduler.tick_quiet_migrate(store, now=now)
    assert first["enqueued"] is True
    _fail_job(store, first["job"], now + 5, capture_scheduler.record_migrate_job_status)

    retry = capture_scheduler.tick_quiet_migrate(store, now=now + 80)
    assert retry["enqueued"] is False
    assert retry["reason"] == "failure_backoff"

    after = capture_scheduler.tick_quiet_migrate(store, now=now + 5 + 601)
    assert after["enqueued"] is True


def test_job_status_route_records_migrate_failure(tmp_path, monkeypatch):
    """proactive_core.job_status 是 consumer 上报终态的真实入口——migrate 失败
    必须像 capture/dream 一样被记入退避状态。"""
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "1")
    from proactive import proactive_core
    store = _store(tmp_path, monkeypatch, "usr_migrate_route_backoff")

    first = capture_scheduler.tick_quiet_migrate(store, now=3_000_000.0)
    assert first["enqueued"] is True
    body, code = proactive_core.job_status(
        store, first["job"]["job_id"], {"status": "failed", "reason": "boom"},
    )
    assert code == 200

    state = capture_scheduler.load_capture_state(store)
    assert int(state.get("migrate_fail_streak") or 0) == 1
    assert float(state.get("last_migrate_failed_at") or 0.0) > 0.0


def test_duplicate_failed_report_does_not_double_streak(tmp_path, monkeypatch):
    """consumer 可能对同一 job 重复上报终态 failed（重试/幂等重放）——streak
    只在状态真正转变为 failed 的那次推进，否则退避会被无故翻倍。"""
    monkeypatch.setenv("FEEDLING_MIGRATE_ENABLE", "1")
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "10")
    from proactive import proactive_core

    # migrate lane（route 级）
    store = _store(tmp_path, monkeypatch, "usr_migrate_dup_failed")
    first = capture_scheduler.tick_quiet_migrate(store, now=4_000_000.0)
    assert first["enqueued"] is True
    job_id = first["job"]["job_id"]
    proactive_core.job_status(store, job_id, {"status": "failed", "reason": "boom"})
    proactive_core.job_status(store, job_id, {"status": "failed", "reason": "boom again"})
    state = capture_scheduler.load_capture_state(store)
    assert int(state.get("migrate_fail_streak") or 0) == 1

    # capture lane（route 级）
    store2 = _store(tmp_path, monkeypatch, "usr_capture_dup_failed")
    t0 = _seed_chat(store2, "m1")
    first2 = capture_scheduler.tick_quiet_capture(store2, now=t0 + 20)
    assert first2["enqueued"] is True
    job2_id = first2["job"]["job_id"]
    proactive_core.job_status(store2, job2_id, {"status": "failed", "reason": "boom"})
    proactive_core.job_status(store2, job2_id, {"status": "failed", "reason": "boom again"})
    state2 = capture_scheduler.load_capture_state(store2)
    assert int(state2.get("capture_fail_streak") or 0) == 1


def test_v1_skip_keeps_the_seq_cursor_honest(tmp_path, monkeypatch):
    """🔴 V1 逃生阀跳过时，不能把 seq 游标写成「0 且已初始化」。

    V1 的窗口来自 _current_window()，**没有 through_seq**。以前跳过时照样写
    seq=0 + capture_seq_initialized=True，调度器从此信任这个 0，从历史起点
    重新发现消息 —— 已经跳过/记过的消息又被算成「新消息」。

    这里走真实调度器生成的窗口（不手工拼 through_seq，那正是之前测试漏掉它的原因）。
    """
    from memory import capture_failure

    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "10")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    store = _store(tmp_path, monkeypatch, "usr_capture_skip_seq_honest")
    _seed_chat(store, "m1")
    t = _seed_chat(store, "m2") + 20

    for attempt in range(1, capture_failure.CAPTURE_POISON_SKIP_AFTER + 1):
        tick = capture_scheduler.tick_quiet_capture(store, now=t)
        assert tick["enqueued"] is True, (attempt, tick.get("reason"))
        assert "through_seq" not in (tick["job"].get("capture_window") or {}), (
            "V1 窗口带上了 through_seq —— 这条测试的前提变了，重新看一下跳过补丁")
        failed = store.update_proactive_job(tick["job"]["job_id"], {
            "status": "failed",
            "status_reason": "json_decode_error:JSONDecodeError",
        })
        t += 5
        capture_scheduler.record_capture_job_status(store, failed, status="failed", now=t)
        t += 6 * 3600 + 1  # 跳过退避上限

    state = capture_scheduler.load_capture_state(store)
    assert int(state["capture_skipped_windows"]) == 1
    assert state["last_captured_until_message_id"] == "m2"
    assert state["capture_seq_initialized"] is False, "拿不到 seq 却声明已初始化"
    # 下次读游标按 m2 翻译出真实 seq：m1/m2 不再被当成新消息。
    assert capture_scheduler._live_messages_after_capture(store, state) == []


def test_v1_account_failures_never_skip_the_window(tmp_path, monkeypatch):
    """🔴 V1 用户余额不足：连续失败多少次都不跳过这批，充值后还能补上。

    2026-09-13 prod：触发过旧逃生阀的 42 人里 33 人是自己的账号问题（余额不足/密钥失效），
    跳过只是替他们一批批丢记忆。原因文本取自 prod 真实形状。
    """
    monkeypatch.setenv("FEEDLING_CAPTURE_QUIET_SEC", "10")
    monkeypatch.setenv("FEEDLING_CAPTURE_MIN_INTERVAL_SEC", "0")
    store = _store(tmp_path, monkeypatch, "usr_capture_account_no_skip")
    _seed_chat(store, "m1")
    t = _seed_chat(store, "m2") + 20
    reason = ('capture_agent_call_failed:RuntimeError: cli agent exited 1: Failed to '
              'authenticate. API Error: 401 {"error":"Insufficient balance"} (api_status=401)')
    for attempt in range(1, 9):
        tick = capture_scheduler.tick_quiet_capture(store, now=t)
        assert tick["enqueued"] is True, (attempt, tick.get("reason"))
        failed = store.update_proactive_job(tick["job"]["job_id"], {
            "status": "failed",
            "capture_result": {"status": "failed", "reason": reason},
        })
        t += 5
        capture_scheduler.record_capture_job_status(store, failed, status="failed", now=t)
        t += 6 * 3600 + 1

    state = capture_scheduler.load_capture_state(store)
    assert int(state["capture_skipped_windows"]) == 0
    assert state["last_captured_until_message_id"] == ""
    assert int(state["capture_fail_streak"]) == 8
