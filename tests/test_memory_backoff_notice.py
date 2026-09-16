"""memory 退避（streak>=3）→ user_notices warning；恢复 resolve（spec Phase C / C3）。
Run:  python -m pytest tests/test_memory_backoff_notice.py -q
"""
from __future__ import annotations
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from notices import core as notices_core  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import capture_scheduler  # noqa: E402
from proactive import dream_scheduler  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _rows(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def test_capture_backoff_emits_only_at_streak_3():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE}
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 1
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 2
    assert "memory_backoff:capture" not in _rows(uid)                          # 前两次不发
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 3
    n = _rows(uid)["memory_backoff:capture"]
    assert n["source"] == "memory" and n["severity"] == "warning"
    assert "capture" in n["user_text"] and "3" in n["user_text"]              # 带 lane + streak


def test_capture_completed_resolves():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE}
    for _ in range(3):
        capture_scheduler.record_capture_job_status(store, job, status="failed")
    capture_scheduler.record_capture_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:capture"]["resolved"] is True


def test_dream_backoff_emits_only_at_streak_3_and_resolves():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "d", "dream_key": "dk", "source": capture_jobs.DREAM_JOB_SOURCE}
    dream_scheduler.record_dream_job_status(store, job, status="failed")  # 1
    dream_scheduler.record_dream_job_status(store, job, status="failed")  # 2
    assert "memory_backoff:dream" not in _rows(uid)
    dream_scheduler.record_dream_job_status(store, job, status="failed")  # 3
    n = _rows(uid)["memory_backoff:dream"]
    assert n["source"] == "memory" and n["severity"] == "warning"
    assert "dream" in n["user_text"] and "3" in n["user_text"]
    dream_scheduler.record_dream_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:dream"]["resolved"] is True


_BALANCE_REASON = ('capture_agent_call_failed:RuntimeError: cli agent exited 1: Failed to '
                   'authenticate. API Error: 401 {"error":"Insufficient balance"} (api_status=401)')


def test_v1_capture_notice_names_the_account_cause():
    """🔴 余额不足时提示要说清原因，否则用户不知道要去充值，记忆一直停着。

    2026-09-13 prod：触发过逃生阀的 42 人里 33 人是自己账号的问题。
    """
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE,
           "capture_result": {"status": "failed", "reason": _BALANCE_REASON}}
    for _ in range(3):
        capture_scheduler.record_capture_job_status(store, job, status="failed")
    n = _rows(uid)["memory_backoff:capture"]
    assert "API Key 无效" in n["user_text"] or "额度不足" in n["user_text"], n["user_text"]
    assert n["blame"] == "user_provider"
    assert "自动继续整理" in n["user_text"]
    capture_scheduler.record_capture_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:capture"]["resolved"] is True


def test_v1_non_account_failure_keeps_the_generic_notice():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE,
           "capture_result": {"status": "failed", "reason": "json_decode_error:JSONDecodeError"}}
    for _ in range(3):
        capture_scheduler.record_capture_job_status(store, job, status="failed")
    n = _rows(uid)["memory_backoff:capture"]
    assert "连续失败 3 次" in n["user_text"]


def test_v1_local_agent_timeout_keeps_the_generic_notice():
    """🔴 本机 agent 调用超时不能提示「你的模型服务暂时不可用」（09-15 审查）。"""
    for reason in (
        "capture_agent_call_failed:TimeoutExpired: Command '['claude', '-p']' timed out after 300 seconds",
        "capture_agent_call_failed:turn_timeout",
    ):
        uid = _uid(); seed_user(uid); store = get_store(uid)
        job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE,
               "capture_result": {"status": "failed", "reason": reason}}
        for _ in range(3):
            capture_scheduler.record_capture_job_status(store, job, status="failed")
        n = _rows(uid)["memory_backoff:capture"]
        assert "连续失败 3 次" in n["user_text"], n["user_text"]
        assert "模型服务" not in n["user_text"]
        assert n["blame"] != "user_provider"


def test_v2_capture_failures_now_notify_the_user():
    """🔴 V2 落卡失败以前**完全没有提示**（V2 不经过 V1 的状态记录函数）。

    状态由 jobs_store 的真实失败路径写入；提示只认本任务亲手累计的失败。
    """
    import asyncio

    from model_api_runtime.v2 import jobs_store, serve_worker, worker

    uid = _uid(); seed_user(uid)
    import conftest
    conftest.set_v2_runtime_owner(uid, generation=1)
    window = {"after_seq": 0, "through_seq": 3, "after_message_id": "",
              "until_message_id": "m3", "until_ts": 3.0}
    last_job_id = None
    for attempt in range(3):
        owner = f"notice-owner-{attempt}"
        job_id, _coalesced = jobs_store.enqueue_job(uid, "capture")
        claimed = jobs_store.claim_next_job(owner, lanes={"capture"})
        assert claimed is not None and int(claimed["id"]) == job_id
        assert jobs_store.mark_running(job_id, claimed_by=owner)
        assert jobs_store.fail_capture_job(
            job_id=job_id, user_id=uid, claimed_by=owner,
            error="extraction_failed:quota_insufficient", window=window)
        last_job_id = job_id

    deps = worker.TurnDeps(
        read_messages=lambda _u: [],
        resolve_provider=lambda _u: (object(), {}),
        mint_enclave_token=lambda _u: "rt",
        # 生产装配：经 _state_doc 归一化（用原始 blob 会漏掉白名单缺字段的 bug）
        read_capture_state=serve_worker._read_capture_state,
    )
    # 别的任务（例如关闭落卡被取消的那个）拿着旧次数返回 failed：不能发
    asyncio.run(worker._notify_capture_backoff(
        deps, {"lane": "capture", "user_id": uid, "id": 999999}, "failed"))
    assert "memory_backoff:capture" not in _rows(uid), "取消/失租的任务借旧状态发了提示"

    asyncio.run(worker._notify_capture_backoff(
        deps, {"lane": "capture", "user_id": uid, "id": last_job_id}, "failed"))
    n = _rows(uid)["memory_backoff:capture"]
    assert "额度不足" in n["user_text"] and n["blame"] == "user_provider"
    assert "7 天" in n["user_text"] and "较早的聊天可能无法补记" in n["user_text"], "提示不能承诺全部补记"

    # 其他 lane 不碰
    asyncio.run(worker._notify_capture_backoff(
        deps, {"lane": "chat", "user_id": uid, "id": last_job_id}, "failed"))


def test_v1_insufficient_balance_is_reported_as_quota_not_bad_key():
    """中转站回「401 Insufficient balance」：提示要说额度不足，不能让用户去重填 key。"""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE,
           "capture_result": {"status": "failed", "reason": _BALANCE_REASON}}
    for _ in range(3):
        capture_scheduler.record_capture_job_status(store, job, status="failed")
    text = _rows(uid)["memory_backoff:capture"]["user_text"]
    assert "额度不足" in text and "API Key" not in text, text


def test_skip_resolves_the_stale_backoff_notice():
    """跳过一批后，旧的「受阻、修好后补记」提示要清掉 —— 那批已经丢了，后面继续整理。"""
    uid = _uid(); seed_user(uid); store = get_store(uid)
    capture_jobs.notify_backoff(store, lane="capture", status="failed", streak=3,
                                account_code="quota_insufficient")
    assert _rows(uid)["memory_backoff:capture"]["resolved"] is False
    capture_jobs.notify_backoff(store, lane="capture", status="failed", streak=0,
                                account_code="", skipped=True)
    assert _rows(uid)["memory_backoff:capture"]["resolved"] is True
