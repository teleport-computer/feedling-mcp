"""V2 planner vocabulary guard (Task 1, D-round): BUG-1 (parity matrix §E) unreachable.

`chat_image_read` returns raw `image_b64` — if the planner can still emit it, the
loop gets more chances to pick it and flood the responder's grounding context with
truncated base64 instead of memory cards. This module pins the action OUT of the
closed vocabulary (both action sets and the LLM-facing prompt string) and pins that
a model naming it anyway gets it silently dropped by `validate_plan`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


def test_chat_image_read_is_not_emittable():
    from model_api_runtime.v2 import planner
    assert "chat_image_read" not in planner._READ_ACTIONS
    assert "chat_image_read" not in planner._WRITE_ACTIONS
    assert "chat_image_read" not in planner._PLANNER_SYSTEM
    # A model that names it anyway gets it silently dropped (BUG-1 unreachable).
    steps = planner.validate_plan({"plan": [
        {"type": "chat_image_read", "payload": {"message_id": "m1"}},
        {"type": "final_response", "payload": {}},
    ]})
    assert steps == [{"type": "final_response", "payload": {}}]


def test_compact_prior_summarises_and_truncates():
    from model_api_runtime.v2 import planner
    prior = {
        "memory_index": [{"ok": True, "data": {"items": [{"id": "a"}]}}],
        "web_fetch": [{"ok": False, "error": "timeout"},
                      {"ok": True, "data": {"text": "Z" * 5000}}],
    }
    out = planner._compact_prior(prior)
    assert out["memory_index"] == {"ok_count": 1, "fail_count": 0,
                                   "preview": '{"items": [{"id": "a"}]}'}
    assert out["web_fetch"]["ok_count"] == 1
    assert out["web_fetch"]["fail_count"] == 1
    assert len(out["web_fetch"]["preview"]) <= planner._PRIOR_PREVIEW_CHARS


def test_compact_prior_of_none_is_empty():
    from model_api_runtime.v2 import planner
    assert planner._compact_prior(None) == {}
    assert planner._compact_prior({}) == {}


def test_planner_user_payload_carries_prior_results():
    from model_api_runtime.v2 import planner
    payload = planner._planner_user_payload(
        coalesced_messages=[{"content": "hi"}], digest={}, memory_index={},
        perception_summary={}, runtime_state={}, lane="chat", reason="r",
        prior_action_results={"memory_index": [{"ok": True, "data": {"items": []}}]},
    )
    assert payload["prior_action_results"]["memory_index"]["ok_count"] == 1


def test_planner_user_payload_omits_prior_key_on_first_round():
    from model_api_runtime.v2 import planner
    payload = planner._planner_user_payload(
        coalesced_messages=[{"content": "hi"}], digest={}, memory_index={},
        perception_summary={}, runtime_state={}, lane="chat", reason="r",
        prior_action_results=None,
    )
    # First round must not carry a dead key — it costs tokens on every single turn.
    assert "prior_action_results" not in payload
