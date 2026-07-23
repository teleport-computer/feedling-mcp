"""并发唤醒任务合并成一个回合（proactive job coalescing）。

2026-07-22 prod (usr_7f30d63f)：用户拿起手机，三个感知事件各建一个独立任务
（`unlock_after_absence` / `photo_added` / `arrived_at_anchor`，created 相隔
0.3s），consumer 为**每个任务各跑一次完整 agent 回合**，前两次说了内容完全
相同的话（间隔 38s），第三次模型才自己判断「刚发过消息了，不重复打扰」。
用户看到每句话出现两遍。

去重此前完全依赖模型自觉，而 `ee81d317` 放宽守卫后连那道闸门也没了。
本模块要求的是机制性合并：**同一用户短窗口内的多次唤醒 = 一个回合**，
并把所有 trigger 交给模型，让它一次把话说完。

⚠️ 关键约束（prod 实测得出）：那三个任务虽然创建只差 0.3s，**领取时间却是
22:24:13 / 22:24:34 / 22:25:14**，分散在不同的轮询批次里。所以只做「单批内
合并」抓不到真实场景，必须同时有跨批次的冷却窗口。

不该被合并的：用户自己设的定时提醒（`scheduled_wake`，每条有独立意图）、
首次登场（introduction / `post_spawn_genesis`）、以及独立轻量的 screen-watch 车道。

Run:  python -m pytest tests/test_proactive_job_coalescing.py -q
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_coalesce_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake = types.ModuleType("content_encryption")
    _fake.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake)

import tools.chat_resident_consumer as crc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._self_wake_streak = 0
    crc._proactive_fail_streak = 0
    crc._proactive_backoff_until = 0.0
    crc._provider_payment_cooldown_until = 0.0
    # 合并的跨批冷却也是模块级状态，必须逐测试重置。
    crc._last_proactive_turn_ts = 0.0
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")
    monkeypatch.setattr(crc, "_proactive_perception_digest", lambda: None)
    yield


def _wake_job(job_id: str, trigger: str, ts: float) -> dict:
    return {
        "schema_version": 2,
        "job_id": job_id,
        "source": crc.PROACTIVE_JOB_SOURCE,
        "trigger": trigger,
        "ts": ts,
        "created_at": ts,
    }


@pytest.fixture()
def spy(monkeypatch):
    """记录 agent 回合与状态更新。回合返回「不说话」，避免触及发送链路。"""
    rec = {"turns": [], "statuses": []}

    def _agent(message, images=None, image_paths=None, **kw):
        rec["turns"].append(message)
        return {"actions": [{"type": "proactive.sleep", "reason": "test"}], "messages": []}

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc, "update_proactive_job_status",
        lambda job_id, status, reason="", **kw: rec["statuses"].append((job_id, status, reason)),
    )
    monkeypatch.setattr(crc, "post_reply", lambda *a, **k: rec.setdefault("posted", []).append(a))
    return rec


def _coalesced(rec) -> list[tuple[str, str, str]]:
    return [s for s in rec["statuses"] if "coalesce" in (s[2] or "")]


def test_same_batch_event_wakes_run_one_turn(spy):
    """prod 那三个 trigger 同批到达 ⇒ 只跑一次回合，其余标记为已合并。"""
    jobs = [
        _wake_job("pj_a", "unlock_after_absence", 1000.0),
        _wake_job("pj_b", "photo_added", 1000.3),
        _wake_job("pj_c", "arrived_at_anchor", 1000.6),
    ]
    crc._process_proactive_jobs(jobs)

    assert len(spy["turns"]) == 1, (
        f"三个并发 trigger 应合成一个 agent 回合，实际跑了 {len(spy['turns'])} 次"
    )
    assert len(_coalesced(spy)) == 2, "另外两个任务应被标记为已合并（可追溯）"


def test_all_triggers_reach_the_prompt(spy):
    """合并不能丢信息：三个 trigger 都要出现在那一次回合的提示里。"""
    jobs = [
        _wake_job("pj_a", "unlock_after_absence", 1000.0),
        _wake_job("pj_b", "photo_added", 1000.3),
        _wake_job("pj_c", "arrived_at_anchor", 1000.6),
    ]
    crc._process_proactive_jobs(jobs)

    assert len(spy["turns"]) == 1
    prompt = spy["turns"][0]
    for trig in ("unlock_after_absence", "photo_added", "arrived_at_anchor"):
        assert trig in prompt, f"合并后的提示里缺了 trigger {trig}：\n{prompt}"


def test_cross_batch_wake_inside_window_is_coalesced(spy):
    """prod 的真实形状：三个任务分散在不同轮询批次（领取相隔 20~60s）。"""
    assert crc.PROACTIVE_COALESCE_WINDOW_SEC >= 60, "窗口需覆盖 prod 实测的 61s 领取跨度"

    crc._process_proactive_jobs([_wake_job("pj_a", "unlock_after_absence", 1000.0)])
    assert len(spy["turns"]) == 1

    # 21 秒后的第二批 —— prod 里正是这一批发出了重复的话。
    crc._last_proactive_turn_ts = crc._last_proactive_turn_ts or 1000.0
    crc._process_proactive_jobs([_wake_job("pj_b", "photo_added", 1021.0)])

    assert len(spy["turns"]) == 1, "窗口内的后续唤醒不应再跑一个回合"
    assert len(_coalesced(spy)) == 1


def test_wake_after_window_runs_again(spy, monkeypatch):
    """窗口过后必须恢复正常唤醒 —— 合并是去重，不是静音。"""
    crc._process_proactive_jobs([_wake_job("pj_a", "unlock_after_absence", 1000.0)])
    assert len(spy["turns"]) == 1

    later = crc._last_proactive_turn_ts + crc.PROACTIVE_COALESCE_WINDOW_SEC + 1
    monkeypatch.setattr(crc.time, "time", lambda: later)
    crc._process_proactive_jobs([_wake_job("pj_b", "photo_added", later)])

    assert len(spy["turns"]) == 2, "超出合并窗口后应当正常唤醒"


def test_scheduled_wake_is_never_coalesced(spy):
    """用户自己设的提醒每条都有独立意图，不能被合并掉。"""
    jobs = [
        _wake_job("pj_evt", "unlock_after_absence", 1000.0),
        _wake_job("pj_rem1", "scheduled_wake", 1000.2),
        _wake_job("pj_rem2", "scheduled_wake", 1000.4),
    ]
    crc._process_proactive_jobs(jobs)

    assert len(spy["turns"]) == 3, (
        f"两条定时提醒 + 一个事件唤醒应各自成回合，实际 {len(spy['turns'])} 次"
    )
    assert _coalesced(spy) == []


def test_introduction_is_never_coalesced(spy):
    """首次登场是一次性的仪式，不参与合并。"""
    jobs = [
        _wake_job("pj_evt", "photo_added", 1000.0),
        {
            "schema_version": 2, "job_id": "pj_intro", "source": crc.PROACTIVE_JOB_SOURCE,
            "trigger": "post_spawn_genesis", "ts": 1000.2, "created_at": 1000.2,
        },
    ]
    crc._process_proactive_jobs(jobs)
    assert len(spy["turns"]) == 2
    assert _coalesced(spy) == []
