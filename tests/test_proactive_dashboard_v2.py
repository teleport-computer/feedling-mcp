from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import reqctx  # noqa: E402
from proactive import dashboard  # noqa: E402
from proactive.observability_v2 import (  # noqa: E402
    METRIC_TURN_COMPLETED,
    METRIC_WAKE_SUBMITTED,
    RUNTIME_METRICS_STREAM_V2,
)
from proactive.scheduled_wake_v2 import SCHEDULED_WAKE_STREAM_V2  # noqa: E402
from proactive.store_v2 import (  # noqa: E402
    BACKGROUND_JOB_STREAM_V2,
    TURN_ACTION_STREAM_V2,
    TURN_STREAM_V2,
    WAKE_STREAM_V2,
)
from proactive.tool_executor_v2 import TOOL_TRACE_STREAM_V2  # noqa: E402


class _FakeStore:
    user_id = "usr_dashboard_v2"

    def __init__(self):
        self.chat_lock = threading.Lock()
        self.frames_lock = threading.Lock()
        self.chat_messages = []
        self.frames_meta = []
        self.section_requests = []

    def ensure_sections(self, sections, **kwargs):
        self.section_requests.append((frozenset(sections), dict(kwargs)))
        return True

    def list_gate_decisions(self, limit=1000):
        return [{
            "decision_id": "gd_v2",
            "schema_version": 2,
            "decision_type": "wake_event",
            "ts": 100.0,
            "should_reach_out": True,
            "trigger": "perception_event",
            "reason": "wake_created",
        }]

    def list_proactive_jobs(self, limit=1000):
        return []

    def list_device_events(self, limit=500):
        return []

    def list_gate_reviews(self, limit=500):
        return []

    def load_proactive_settings(self):
        return {"timezone": "UTC"}


def test_dashboard_reads_v2_turn_action_tool_records_and_round3_labels(monkeypatch):
    streams = {
        WAKE_STREAM_V2: [{
            "wake_id": "wake_1",
            "status": "drained",
            "source": "perception_event",
            "trigger": "arrived_at_anchor",
            "created_at": 100.0,
            "change_digest": "anchor changed",
        }],
        TURN_STREAM_V2: [{
            "turn_id": "turn_1",
            "status": "completed",
            "created_at": 101.0,
            "completed_at": 102.0,
            "trigger": "arrived_at_anchor",
            "wake_ids": ["wake_1"],
            "outcome": {"messages": ["hello"], "actions": [{"type": "send_message", "text": "hello"}]},
        }],
        TURN_ACTION_STREAM_V2: [{
            "turn_id": "turn_1",
            "action_id": "act_1",
            "action_type": "send_message",
            "created_at": 102.0,
            "action": {"type": "send_message", "text": "hello"},
        }],
        BACKGROUND_JOB_STREAM_V2: [{
            "job_id": "bg_1",
            "status": "completed",
            "created_at": 103.0,
            "request": {"tool": "memory.fetch"},
        }],
        SCHEDULED_WAKE_STREAM_V2: [{
            "timer_id": "sched_1",
            "status": "pending",
            "created_at": 104.0,
            "note": "check in",
        }],
        RUNTIME_METRICS_STREAM_V2: [
            {"user_id": "usr_dashboard_v2", "name": METRIC_WAKE_SUBMITTED, "value": 1, "data": {"accepted": True}, "ts": 100.0},
            {"user_id": "usr_dashboard_v2", "name": METRIC_TURN_COMPLETED, "value": 1, "data": {"wake_count": 1, "latency_ms": 2000}, "ts": 102.0},
        ],
        TOOL_TRACE_STREAM_V2: [{
            "call_id": "tool_1",
            "turn_id": "turn_1",
            "wake_id": "wake_1",
            "name": "perception.now",
            "cost_class": "fast",
            "outcome": "ok",
            "latency_ms": 3.5,
            "ts": 102.2,
        }],
    }

    monkeypatch.setattr(
        dashboard.db,
        "log_read",
        lambda _user_id, stream, limit=100, since_epoch=0.0: list(streams.get(stream, []))[-limit:],
    )

    store = _FakeStore()
    snapshot = dashboard._proactive_debug_snapshot(store)
    assert store.section_requests == [
        (
            frozenset(
                {dashboard.StoreSection.CHAT, dashboard.StoreSection.FRAMES}
            ),
            {"reason": "first_use", "strict": True},
        )
    ]
    assert snapshot["counts"]["v2_turns"] == 1
    assert snapshot["counts"]["v2_turn_actions"] == 1
    assert snapshot["counts"]["v2_tool_traces"] == 1
    assert snapshot["v2_health"]["wake_volume"] == 1
    assert snapshot["v2_health"]["turn_count"] == 1

    with reqctx.bind(query_string="lang=en&detail=1"):
        html = dashboard._render_proactive_dashboard(snapshot)

    assert "Runtime V2 Wakes And Turns" in html
    assert "Runtime V2 Actions And Tool Traces" in html
    assert "perception.now" in html
    assert "turn_1" in html
    assert "value='good_presence'" in html
    assert "value='correct_true'" not in html
