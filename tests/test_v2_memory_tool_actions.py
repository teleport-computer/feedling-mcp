"""worker._memory_tool_actions: translate the model's plaintext memory_write
tool actions into the server memory-action shape (no envelope — the plaintext
write path builds the E2E envelope). Pure unit, no DB/LLM.

Regression for the memory_write tool dying with turn_failed:runtimeerror: the
raw model actions were passed straight to memory_core.actions, which requires
either a fully-built E2E envelope or the nested {"memory": {...}} plaintext shape
with summary/content — a bare {op/content} guess was rejected with
title_required/400.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import worker


def test_add_maps_to_memory_add_with_nested_plaintext_and_no_envelope():
    out = worker._memory_tool_actions([
        {"op": "add", "summary": "编程偏好", "content": "用户最喜欢 Rust。", "bucket": "偏好"}])
    assert len(out) == 1
    a = out[0]
    assert a["type"] == "memory.add"
    assert "envelope" not in a  # server builds the E2E envelope from plaintext
    # threads 不出现在翻译结果里 —— 模型没传就不替它写。add 没有可继承的旧卡,
    # actions 落卡时照样补成 []（已验证与旧行为逐字段相同）;而 update 那条路
    # 「缺席 vs 空数组」是旧卡标签保不保得住的分水岭,见
    # test_update_without_bucket_or_threads_does_not_author_empty_ones。
    assert a["memory"] == {"summary": "编程偏好", "content": "用户最喜欢 Rust。",
                           "bucket": "偏好"}
    assert a["capture_mode"] == "agent_tool"
    assert a["reason"]


def test_add_without_summary_falls_back_to_content_prefix():
    out = worker._memory_tool_actions([{"op": "add", "content": "x" * 200}])
    assert out[0]["memory"]["summary"] == "x" * 80
    assert out[0]["memory"]["content"] == "x" * 200


def test_update_maps_to_supersede_with_target():
    out = worker._memory_tool_actions([
        {"op": "update", "target_id": "mem_1", "summary": "新", "content": "更新后的内容"}])
    a = out[0]
    assert a["type"] == "memory.supersede"
    assert a["supersedes"] == "mem_1"
    assert a["memory"]["content"] == "更新后的内容"
    assert "envelope" not in a


def test_delete_maps_to_memory_delete():
    out = worker._memory_tool_actions([{"op": "delete", "target_id": "mem_9"}])
    assert out == [{
        "type": "memory.delete",
        "memory_id": "mem_9",
        "reason": "Written by the agent via the memory_write tool.",
    }]


def test_reason_is_forwarded_to_add_update_and_delete_with_bound():
    long_reason = "r" * 1200
    add, update, delete = worker._memory_tool_actions([
        {"op": "add", "summary": "s", "content": "c", "reason": "because"},
        {
            "op": "update", "target_id": "m1", "summary": "s", "content": "c",
            "reason": long_reason,
        },
        {"op": "delete", "target_id": "m2", "reason": "obsolete"},
    ])
    assert add["reason"] == "because"
    assert update["reason"] == "r" * 1000
    assert delete["reason"] == "obsolete"


def test_update_without_target_is_discarded_not_rewritten():
    out = worker._memory_tool_actions([{"op": "update", "summary": "s", "content": "c"}])
    assert out == []


def test_delete_without_target_and_unknown_op_are_discarded():
    assert worker._memory_tool_actions([{"op": "delete", "summary": "s"}]) == []
    assert worker._memory_tool_actions([{"op": "frobnicate", "summary": "s"}]) == []


def test_lenient_synonyms_action_title_description():
    out = worker._memory_tool_actions([
        {"action": "add", "title": "标题", "description": "正文", "threads": ["t1"]}])
    a = out[0]
    assert a["type"] == "memory.add"
    assert a["memory"]["summary"] == "标题"
    assert a["memory"]["content"] == "正文"
    assert a["memory"]["threads"] == ["t1"]


def test_memory_dot_prefixed_op_normalized():
    out = worker._memory_tool_actions([
        {"type": "memory.add", "summary": "s", "content": "c"}])
    assert out[0]["type"] == "memory.add"


def test_none_and_non_dict_entries_are_skipped():
    assert worker._memory_tool_actions(None) == []
    assert worker._memory_tool_actions(["nope", 3, None]) == []


def test_update_without_bucket_or_threads_does_not_author_empty_ones():
    """模型没传 bucket/threads 时,翻译层不能替它写一个空值。

    现场(2026-08-10):V2 上每次改记忆卡,桶都掉回「未分类」、标签全清空。
    改卡走的是 supersede(新写一张替换旧的),actions 那边靠
    `{**inherited, **raw}` 从旧卡继承 bucket/threads —— 但 raw 里带着
    bucket=""/threads=[],空值照样覆盖继承值。

    根因在翻译层丢了「没传」和「显式传空」的区别。threads 尤其致命:
    schema 根本不允许模型传,所以每一次 update 都必然清空标签。
    """
    out = worker._memory_tool_actions([
        {"op": "update", "target_id": "mem_1", "summary": "新", "content": "更新后的内容"}])
    inner = out[0]["memory"]

    assert "bucket" not in inner, "没传桶就不该出现这个键,否则会盖掉旧卡的桶"
    assert "threads" not in inner, "没传线索就不该出现这个键,否则会清空旧卡的标签"


def test_update_with_explicit_bucket_still_overrides():
    """模型确实传了就照传 —— 别把修复做成「永远不能改桶」。"""
    out = worker._memory_tool_actions([{
        "op": "update", "target_id": "mem_1", "summary": "新", "content": "内容",
        "bucket": "健康",
    }])
    assert out[0]["memory"]["bucket"] == "健康"


def test_add_still_carries_whatever_the_model_gave():
    """add 没有可继承的旧卡,行为不变。"""
    out = worker._memory_tool_actions([{
        "op": "add", "summary": "s", "content": "c", "bucket": "爱好",
    }])
    assert out[0]["memory"]["bucket"] == "爱好"


def test_model_can_supply_threads_importance_and_pulse():
    """这三个字段后端一直在消费,schema 却不允许模型传。

    - threads:worker 早就在转发它,卡在 schema 那道闸 → 卡片标签永远是空的
    - importance/pulse:actions 默认 0.5/0.3,于是 V2 每张卡权重完全一样,
      而这两个值直接参与 ambient 排序(memory_readside_core)

    共享的 MEMORY_WRITE_RULES_V1 里明确教了模型这三个字段的含义 ——
    「prompt 要模型写、schema 禁止模型写」是当前最矛盾的一处。
    """
    from capabilities.tool_schema import validate_tool_args

    err = validate_tool_args("memory_write", {"actions": [{
        "op": "add", "summary": "s", "content": "c", "bucket": "爱好",
        "threads": ["自行车", "瓜车"], "importance": 0.8, "pulse": 0.6,
    }]})
    assert err is None, f"schema 该放行这三个字段,却拒了:{err}"

    out = worker._memory_tool_actions([{
        "op": "add", "summary": "s", "content": "c",
        "threads": ["自行车"], "importance": 0.8, "pulse": 0.6,
    }])
    inner = out[0]["memory"]
    assert inner["threads"] == ["自行车"]
    assert inner["importance"] == 0.8
    assert inner["pulse"] == 0.6


def test_zero_importance_is_not_swallowed_as_missing():
    """importance=0 / pulse=0 是合法取值,不能被 `or default` 吞成「没传」。"""
    out = worker._memory_tool_actions([{
        "op": "add", "summary": "s", "content": "c", "importance": 0, "pulse": 0,
    }])
    inner = out[0]["memory"]
    assert inner["importance"] == 0, "0 被当成没传了"
    assert inner["pulse"] == 0, "0 被当成没传了"


def test_update_still_inherits_when_scores_absent():
    """没传评分时仍然不写这两个键 —— 和 bucket/threads 同一条规矩。"""
    out = worker._memory_tool_actions([
        {"op": "update", "target_id": "mem_1", "summary": "新", "content": "内容"}])
    inner = out[0]["memory"]
    assert "importance" not in inner
    assert "pulse" not in inner
