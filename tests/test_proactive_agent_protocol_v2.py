from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive.agent_protocol_v2 import parse_agent_response_v2, agent_tool_calls_v2
from proactive.runtime_v2 import RuntimeSpineV2, TurnOutcomeV2, TurnRunnerV2, WakeEventV2


class FakeTurnStoreV2:
    def __init__(self) -> None:
        self.started = []
        self.recorded_actions = []
        self.completed = []

    def start_turn(self, user_id, context, lease, *, now=None):
        turn_id = f"turn_{len(self.started) + 1}"
        self.started.append({
            "turn_id": turn_id,
            "user_id": user_id,
            "trigger": context.trigger,
            "lease_id": lease.lease_id,
            "now": now,
        })
        return SimpleNamespace(turn_id=turn_id)

    def record_actions(self, user_id, turn_id, actions, *, now=None):
        self.recorded_actions.append({
            "user_id": user_id,
            "turn_id": turn_id,
            "actions": [dict(action) for action in actions],
            "now": now,
        })

    def complete_turn(self, user_id, turn_id, lease, *, outcome=None, now=None):
        self.completed.append({
            "user_id": user_id,
            "turn_id": turn_id,
            "lease_id": lease.lease_id,
            "outcome": outcome,
            "now": now,
        })


def test_agent_protocol_parses_messages_actions_and_background_request():
    raw = json.dumps({
        "messages": ["I'll check."],
        "actions": [
            {"type": "send_message", "text": "Starting now."},
            {"type": "schedule_wake", "at": "2026-06-20T09:00:00+08:00", "tz": "Asia/Shanghai", "note": "check in", "origin_refs": ["msg_1"], "repeat": "weekly"},
            {"type": "cancel_wake", "wake_id": "wake_old"},
            {"type": "needs_background", "request": {"tool": "memory.fetch", "ids": ["m1"]}},
        ],
    })

    parsed = parse_agent_response_v2(raw)

    assert parsed.messages == ("I'll check.", "Starting now.")
    assert [action["type"] for action in parsed.actions] == [
        "send_message",
        "send_message",
        "schedule_wake",
        "cancel_wake",
        "needs_background",
    ]
    assert parsed.needs_background is True
    assert parsed.background_request == {"tool": "memory.fetch", "ids": ["m1"]}
    assert parsed.actions[2]["repeat"] == "weekly"


def test_agent_protocol_malformed_or_internal_fragments_sleep_invisibly():
    raws = [
        "}",
        "reason: 'user may be resting; do not disturb'",
        '{"messages": ["hello"]',
        json.dumps({"messages": ["}"]}),
        json.dumps({"messages": ["reason: user may be resting"]}),
        json.dumps({"actions": [{"type": "send_message", "text": "reason: user may be resting"}]}),
        json.dumps({"actions": [{"type": "send_message", "text": "{\"cards\": []}"}]}),
    ]

    for raw in raws:
        parsed = parse_agent_response_v2(raw)

        assert parsed.messages == (), raw
        assert parsed.actions == ({"type": "sleep", "reason": "invalid_protocol"},), raw


def test_agent_protocol_torn_tail_fragments_sleep_invisibly():
    """Stream-cut relay tears one envelope: the head goes to the reasoning
    channel, a bare tail lands in a visible message. The head-anchored guard
    misses these; the shared orphan-tail check must drop them (proactive is
    fail-closed, so no reasoning-channel corroboration is needed here)."""
    tails = [
        'active.sleep","reason":"4:34 了她睡得很沉 早上再说"}]}',
        '":"5点了她还在睡 没动静"}]}',
        'type":"proactive.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}',
    ]
    for tail in tails:
        parsed = parse_agent_response_v2(json.dumps({"messages": [tail]}))
        assert parsed.messages == (), tail
        assert parsed.actions == ({"type": "sleep", "reason": "invalid_protocol"},), tail


def test_agent_protocol_real_message_still_delivered():
    """Guard against over-dropping: an ordinary reply must still pass."""
    parsed = parse_agent_response_v2(json.dumps({"messages": ["宝贝早上好呀，睡得好吗"]}))
    assert parsed.messages == ("宝贝早上好呀，睡得好吗",)


def test_agent_protocol_non_message_structures_stay_invisible():
    parsed = parse_agent_response_v2(json.dumps({"cards": []}))

    assert parsed.messages == ()
    assert parsed.actions == ({"type": "sleep", "reason": "not_now"},)


def test_turn_runner_malformed_visible_fragments_persist_only_sleep():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    store = FakeTurnStoreV2()
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="sleep_detected",
            created_at=11.0,
        )
    )
    runner = TurnRunnerV2(
        spine,
        turn_store=store,
        run_agent=lambda _context: {"messages": ["}", "reason: user may be resting"]},
    )

    result = runner.run_ready_turn("u1", now=11.0)

    assert result.status == "completed"
    assert result.outcome.messages == ()
    assert result.outcome.actions == ({"type": "sleep", "reason": "invalid_protocol"},)
    assert store.completed[0]["outcome"].messages == ()
    assert store.recorded_actions[0]["actions"] == [{"type": "sleep", "reason": "invalid_protocol"}]


def test_user_message_and_proactive_wake_use_same_runner_context_protocol():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    seen = []

    def run_agent(agent_context):
        seen.append(agent_context)
        return {"actions": [{"type": "sleep", "reason": "test"}]}

    runner = TurnRunnerV2(spine, run_agent=run_agent)
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=10.0,
            latency_sensitive=True,
        )
    )
    spine.submit(
        WakeEventV2(
            user_id="u2",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=20.0,
            change_digest="anchor: home -> cafe",
        )
    )

    user_result = runner.run_ready_turn("u1", now=10.0)
    proactive_result = runner.run_ready_turn("u2", now=20.0)

    assert user_result.status == "completed"
    assert proactive_result.status == "completed"
    assert [context["trigger"] for context in seen] == ["user_message", "arrived_at_anchor"]
    assert all(any(tool["name"] == "perception.now" for tool in context["tools"]) for context in seen)


def test_manual_wake_returning_only_sleep_is_marked_ignored_manual():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=30.0,
            latency_sensitive=True,
            manual=True,
        )
    )
    runner = TurnRunnerV2(
        spine,
        run_agent=lambda _context: {"actions": [{"type": "sleep", "reason": "not_now"}]},
    )

    result = runner.run_ready_turn("u1", now=30.0)

    assert result.status == "ignored_manual"
    assert result.contract_violation == "ignored_manual"
    assert result.outcome is not None
    assert result.outcome.messages == ()


def test_send_message_action_promotes_to_visible_message_and_satisfies_manual_contract():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=30.0,
            latency_sensitive=True,
            manual=True,
        )
    )
    runner = TurnRunnerV2(
        spine,
        run_agent=lambda _context: {"actions": [{"type": "send_message", "text": "audit only"}]},
    )

    result = runner.run_ready_turn("u1", now=30.0)

    assert result.status == "completed"
    assert result.outcome is not None
    assert result.outcome.messages == ("audit only",)
    assert [action["type"] for action in result.outcome.actions] == ["send_message"]


def test_plain_background_request_does_not_emit_foreground_chat_message():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=40.0,
            latency_sensitive=True,
        )
    )
    runner = TurnRunnerV2(
        spine,
        run_agent=lambda _context: {
            "needs_background": True,
            "background_request": {"tool": "memory.fetch", "ids": ["m1"]},
        },
    )

    result = runner.run_ready_turn("u1", now=40.0)

    assert result.status == "background_queued"
    assert result.outcome is not None
    assert result.outcome.messages == ()
    assert all(action.get("type") != "send_message" for action in result.outcome.actions)
    assert result.outcome.background_request == {"tool": "memory.fetch", "ids": ["m1"]}


def test_agent_context_does_not_passively_inject_perception_snapshot_values():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    captured = []
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=50.0,
            change_digest="anchor changed",
            payload={"perception": {"motion_state": "walking", "battery": {"level": 0.7}}},
        )
    )
    runner = TurnRunnerV2(
        spine,
        recent_chat_provider=lambda _user_id: ({"role": "user", "text": "hi"},),
        run_agent=lambda context: captured.append(context) or {"actions": [{"type": "sleep"}]},
    )

    result = runner.run_ready_turn("u1", now=50.0)

    assert result.status == "completed"
    assert captured
    context = captured[0]
    assert context["change_digest"] == "anchor changed"
    assert context["recent_chat"] == [{"role": "user", "text": "hi"}]
    assert "perception" not in context
    assert "payload" not in context
    assert "motion_state" not in json.dumps(context)
    assert "battery" not in json.dumps(context)


def test_user_message_context_omits_perception_digest_and_hints():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    captured = []
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="user_message",
            trigger="user_message",
            created_at=60.0,
            latency_sensitive=True,
            change_digest="should not be injected",
            presence_hints={"in_meeting": True},
        )
    )
    runner = TurnRunnerV2(
        spine,
        run_agent=lambda context: captured.append(context) or {"actions": [{"type": "sleep"}]},
    )

    result = runner.run_ready_turn("u1", now=60.0)

    assert result.status == "completed"
    context = captured[0]
    assert context["trigger"] == "user_message"
    assert "change_digest" not in context
    assert "presence_hints" not in context
    assert "tools" in context
    assert "time" in context
    assert "switches" in context
    assert "timezone" in context
    assert "local_time" in context


def test_agent_context_uses_settings_timezone_for_local_time():
    spine = RuntimeSpineV2(
        settings_resolver=lambda _user_id: {"timezone": "America/New_York"},
        merge_window_sec=0.0,
    )
    captured = []
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=1_788_000_000.0,
        )
    )
    runner = TurnRunnerV2(
        spine,
        run_agent=lambda context: captured.append(context) or {"actions": [{"type": "sleep"}]},
    )

    result = runner.run_ready_turn("u1", now=1_788_000_000.0)

    assert result.status == "completed"
    assert captured[0]["timezone"] == "America/New_York"
    assert captured[0]["local_time"].endswith("-04:00")


def test_agent_actions_are_persisted_as_v2_turn_action_records():
    spine = RuntimeSpineV2(merge_window_sec=0.0)
    store = FakeTurnStoreV2()
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=70.0,
        )
    )
    runner = TurnRunnerV2(
        spine,
        turn_store=store,
        run_agent=lambda _context: {
            "messages": ["I'll check later."],
            "actions": [
                {"type": "schedule_wake", "at": "2026-06-20T09:00:00+08:00", "tz": "Asia/Shanghai", "note": "follow up"}
            ],
        },
    )

    result = runner.run_ready_turn("u1", now=70.0)

    assert result.status == "completed"
    assert result.turn_id == "turn_1"
    assert store.started[0]["trigger"] == "arrived_at_anchor"
    assert store.completed[0]["turn_id"] == "turn_1"
    assert store.completed[0]["outcome"].messages == ("I'll check later.",)
    persisted = store.recorded_actions[0]["actions"]
    assert [action["type"] for action in persisted] == ["send_message", "schedule_wake"]


def test_parse_tool_calls_field():
    resp = parse_agent_response_v2('{"tool_calls": [{"name": "screen.read", "args": {"mode": "caption"}}]}')
    assert agent_tool_calls_v2(resp) == [("screen.read", {"mode": "caption"})]


def test_parse_tool_calls_absent_is_empty():
    resp = parse_agent_response_v2('{"messages": ["hi"]}')
    assert resp.tool_calls == ()
    assert agent_tool_calls_v2(resp) == []


def test_parse_tool_calls_drops_malformed_entries():
    resp = parse_agent_response_v2(
        '{"tool_calls": [{"name": "", "args": {}}, {"args": {"x": 1}}, '
        '{"name": "memory.fetch", "args": {"ids": ["m1"]}}, "nope"]}')
    assert agent_tool_calls_v2(resp) == [("memory.fetch", {"ids": ["m1"]})]
