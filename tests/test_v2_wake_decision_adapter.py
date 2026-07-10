"""serve_worker._wake_decision_for_user —— 装配层适配器，把真实的
proactive.gate._build_proactive_v2_wake_decision（只读，读 settings/frames/
device-events 判断该不该唤醒，不做 enqueue）包一层薄壳，供 D3 的 V2 proactive
scheduler 复用。真 DB（真 store + 真 gate），不打桩 gate 内部逻辑——这样激活门
（activation gate）/broadcast 抑制/其余地雷都零漂移保留。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from conftest import seed_user  # noqa: E402
import db  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402
from proactive import gate as proactive_gate  # noqa: E402
from model_api_runtime.v2.serve_worker import _wake_decision_for_user  # noqa: E402


def _enable_v2(uid: str) -> None:
    db.patch_blob_strict(uid, hosted_config_store.MODEL_API_RUNTIME_BLOB, {
        "hosted_runtime_mode": hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    })


def test_wake_decision_blocks_unactivated_user():
    """Landmine 3: 未激活用户（从未 first_chat_ok_at）零烧钱唤醒——
    should_wake=False，block_reason 是 gate 的 ACTIVATION_PENDING_REASON。"""
    uid = "usr_wake_decision_unactivated"
    seed_user(uid)
    _enable_v2(uid)
    # 不设置 first_chat_ok_at：新鲜 proactive settings 保持未激活。

    decision = _wake_decision_for_user(uid)

    assert decision["should_wake"] is False
    assert decision["block_reason"] == proactive_gate.ACTIVATION_PENDING_REASON == "activation_pending"


def test_wake_decision_activated_user_wakes():
    """已激活（first_chat_ok_at 已设）+ 默认 settings（ambient=True 默认开、
    broadcast_state 默认 "unknown" 非 off/paused）——heartbeat 应当放行。"""
    uid = "usr_wake_decision_activated"
    seed_user(uid)
    _enable_v2(uid)
    store = core_store.get_store(uid)
    # first_chat_ok_at is NOT settable via save_proactive_settings (it's not in
    # its `allowed` key whitelist — core/store.py:604) — the only writer is the
    # dedicated store.mark_first_chat_ok().
    store.mark_first_chat_ok(at_iso="2026-07-01T00:00:00")

    decision = _wake_decision_for_user(uid)

    # must-have：不再被激活门挡住
    assert decision["block_reason"] != proactive_gate.ACTIVATION_PENDING_REASON
    # 更强断言：默认 settings 下（ambient 默认 True、broadcast_state 默认
    # "unknown" 不在 {"off","paused"} 内）heartbeat 应该确定性放行。gate.py 在
    # should_wake_agent=True 时把 "reason" 设成字面量 "wake_created"（不是空
    # 串——参见 _build_proactive_v2_wake_decision 尾部 `reason = "wake_created"
    # if should_wake_agent else block_reason`），适配器把 d["reason"] 原样转发
    # 成 block_reason，所以这里断言的是 "wake_created" 而非 ""。
    assert decision["should_wake"] is True
    assert decision["block_reason"] == "wake_created"
    assert decision["wake_interval_sec"] > 0
