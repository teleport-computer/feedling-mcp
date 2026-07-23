"""并发感知 trigger 各建一个独立 job —— 「一条消息回了两遍」的复现。

2026-07-22 prod (usr_7f30d63f)：用户拿起手机，三个感知事件在同一秒内各自建了
一个独立的主动唤醒 job（created_at 相隔 0.3s）：

    unlock_after_absence  22:23:43.70 → claimed 22:24:13 → 发出 2 条消息
    photo_added           22:23:43.39 → claimed 22:24:34 → 发出内容完全相同的 2 条
    arrived_at_anchor     22:23:41    → claimed 22:25:14 → 模型自己判断
                                        "刚发过消息了，不重复打扰" → 跳过

用户看到每条消息各出现两遍（重放间隔 38s）。

两条机制凑成这个结果，本测试各钉一条：

1. 节流是 **per-trigger** 的。`perception/service.py` 的
   `_last_v2_wake_ts(user_id, trigger)` 按 ``ev["trigger"] == trigger`` 过滤，
   `PHOTO_CLUSTER_SEC=30` 之类的冷却只压得住*同一个* trigger 的连发；
   三个*不同* trigger 在同一秒可以全部放行，没有任何跨 trigger 的全局闸门。
   → `test_dedupe_key_never_merges_distinct_triggers`

2. job 层**零合并**。`store.append_proactive_job` 直接 `db.log_append`，
   没有 coalesce/去重；每个 job 都会被 consumer 领走并跑一个完整 agent 回合。
   去重只剩模型在 prompt 里自觉判断"刚才是不是已经说过了"——
   prod 那次三次判断里两次决定发、一次决定不发。
   → `test_concurrent_triggers_each_queue_their_own_job`

期望的修复方向是 job 层 coalesce（同一用户短窗口内的多个 trigger 合成一次
唤醒）。修好后 `test_concurrent_triggers_each_queue_their_own_job` 应当反过来
断言"只剩一个待跑 job"。

Run:  python -m pytest tests/test_proactive_multi_trigger_burst.py -q
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from proactive.runtime_v2 import WakeEventV2  # noqa: E402

# 用户拿起手机那一刻同时到达的三个感知事件（prod 实例）。
BURST_TRIGGERS = ("unlock_after_absence", "photo_added", "arrived_at_anchor")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x51" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def test_dedupe_key_never_merges_distinct_triggers():
    """去重键含 trigger ⇒ 同秒到达的不同 trigger 在设计上就不会被合并。"""
    now = time.time()
    events = [
        WakeEventV2(user_id="usr_x", source="perception_event", trigger=t, created_at=now)
        for t in BURST_TRIGGERS
    ]
    keys = {e.dedupe_key for e in events}
    assert len(keys) == len(BURST_TRIGGERS), (
        f"期望三个 trigger 产生三个互不相同的 dedupe_key，实得 {keys}"
    )

    # 对照：同一个 trigger 重复到达确实会共用一个键（这条闸门是有效的），
    # 说明缺的不是「去重」而是「跨 trigger 的合并」。
    same = [
        WakeEventV2(user_id="usr_x", source="perception_event", trigger="photo_added", created_at=now)
        for _ in range(2)
    ]
    assert same[0].dedupe_key == same[1].dedupe_key


def test_concurrent_triggers_each_queue_their_own_job(user):
    """三个 trigger 同秒入队 ⇒ 三个独立 job 全部待跑，无任何合并。"""
    uid = user
    store = core_store.get_store(uid)
    now = time.time()

    for i, trigger in enumerate(BURST_TRIGGERS):
        store.append_proactive_job({
            "job_id": f"pj_burst_{i}",
            "source": "agent_initiated_proactive",
            "trigger": trigger,
            "status": "queued",
            "created_at": now + i * 0.3,   # prod 实测就是 0.3s 级别的间隔
            "ts": now + i * 0.3,
        })

    jobs = store.list_proactive_jobs(since_epoch=0, limit=100)
    queued = [j for j in jobs if j.get("status") == "queued"]

    assert len(queued) == len(BURST_TRIGGERS), (
        f"三个并发 trigger 各留下一个待跑 job（实得 {len(queued)} 个）—— "
        "job 层没有 coalesce，consumer 会为每个 job 跑一次完整 agent 回合，"
        "于是同样的话被说了多遍。修复应把它们合成一次唤醒。"
    )
    assert {j.get("trigger") for j in queued} == set(BURST_TRIGGERS)
