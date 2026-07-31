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
    assert a["memory"] == {"summary": "编程偏好", "content": "用户最喜欢 Rust。",
                           "bucket": "偏好", "threads": []}
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
    assert out == [{"type": "memory.delete", "memory_id": "mem_9"}]


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
