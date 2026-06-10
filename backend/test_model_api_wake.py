"""Unit tests for the hosted proactive wake turn pure logic
(model_api_runtime/wake.py): the wake contract / wake event messages and the
{"actions":[...]} reply parser. No Flask, no DB.

Run:  ../.venv-test/bin/python -m pytest test_model_api_wake.py -q
"""
import json

from model_api_runtime import wake


def test_wake_turn_contract_message_shape():
    msg = wake.wake_turn_contract_message()
    assert msg["role"] == "system"
    # 非 judgment 契约的关键句必须在：不是命令说话 / sleep 兜底 / JSON-only
    assert "not a request to speak" in msg["content"]
    assert '{"actions":[{"type":"sleep"}]}' in msg["content"]
    assert "send_message" in msg["content"]
    assert "set_ai_state" in msg["content"]
    # manual 召唤的契约：用户主动召唤不许 sleep
    assert "manual=true" in msg["content"]


def test_build_wake_event_message_includes_trigger_and_hint():
    msg = wake.build_wake_event_message({
        "trigger": "perception_location",
        "context_hint": "她到了一个新地方：place_label = gym（之前 home）。",
        "user_state": "default",
        "ai_state": "present",
        "broadcast_state": "off",
        "created_at": "2026-06-10T12:00:00",
        "job_id": "pj_x",          # 不应泄漏进 payload
    })
    assert msg["role"] == "user"
    payload = json.loads(msg["content"].split("\n", 1)[1])
    assert payload["kind"] == "proactive_wake"
    assert payload["trigger"] == "perception_location"
    assert "gym" in payload["context_hint"]
    assert "job_id" not in payload
    # 缺省（感知 job 无 manual/forced 字段）必须是 False，不能缺失
    assert payload["manual"] is False
    assert payload["forced"] is False


def test_build_wake_event_message_carries_manual_flags():
    msg = wake.build_wake_event_message({
        "trigger": "manual_wake",
        "manual": True,
        "forced": True,
    })
    payload = json.loads(msg["content"].split("\n", 1)[1])
    assert payload["manual"] is True
    assert payload["forced"] is True


def test_parse_wake_actions_valid_mixed():
    raw = json.dumps({"actions": [
        {"type": "send_message", "text": "刚看到你到健身房啦"},
        {"type": "set_ai_state", "state": "watching"},
    ]})
    actions = wake.parse_wake_actions(raw)
    assert actions == [
        {"type": "send_message", "text": "刚看到你到健身房啦"},
        {"type": "set_ai_state", "state": "watching"},
    ]


def test_parse_wake_actions_code_fence_and_sleep():
    raw = '```json\n{"actions":[{"type":"sleep"}]}\n```'
    assert wake.parse_wake_actions(raw) == [{"type": "sleep"}]


def test_parse_wake_actions_unparseable_returns_none():
    assert wake.parse_wake_actions("我现在就想说说话！") is None
    assert wake.parse_wake_actions('{"reply": "wrong shape"}') is None
    assert wake.parse_wake_actions("") is None


def test_parse_wake_actions_empty_actions_coerces_to_sleep():
    assert wake.parse_wake_actions('{"actions": []}') == [{"type": "sleep"}]


def test_parse_wake_actions_caps_messages_and_drops_invalid():
    raw = json.dumps({"actions": [
        {"type": "send_message", "text": "1"},
        {"type": "send_message", "text": "2"},
        {"type": "send_message", "text": "3"},
        {"type": "send_message", "text": "4"},          # 超 cap，丢弃
        {"type": "set_ai_state", "state": "godmode"},   # 非法 state，丢弃
        {"type": "request_broadcast"},                   # 本期不支持，丢弃
        "not a dict",
    ]})
    actions = wake.parse_wake_actions(raw)
    assert [a["text"] for a in actions if a["type"] == "send_message"] == ["1", "2", "3"]
    assert all(a["type"] in {"send_message", "sleep"} for a in actions)


def test_parse_wake_actions_truncates_long_text():
    raw = json.dumps({"actions": [{"type": "send_message", "text": "x" * 9000}]})
    actions = wake.parse_wake_actions(raw)
    assert len(actions[0]["text"]) == 4000


def test_hosted_tick_trigger_mapping():
    # 与 resident consumer 的 broadcast→trigger 映射对齐，保证
    # _proactive_v2_auto_wake_block_reason 对托管心跳的机械拦截语义一致。
    assert wake.hosted_tick_trigger("on") == "heartbeat_broadcast_on"
    assert wake.hosted_tick_trigger("paused") == "heartbeat_broadcast_paused"
    assert wake.hosted_tick_trigger("off") == "heartbeat_broadcast_off"
    assert wake.hosted_tick_trigger("unknown") == "heartbeat_broadcast_off"
    assert wake.hosted_tick_trigger("") == "heartbeat_broadcast_off"
    assert wake.hosted_tick_trigger(None) == "heartbeat_broadcast_off"
