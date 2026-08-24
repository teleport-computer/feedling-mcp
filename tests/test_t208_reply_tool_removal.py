from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import provider_client
from admin import admin_core, data_track
from capabilities import tool_schema
from conftest import seed_user, set_v2_runtime_owner
from model_api_runtime.v2 import jobs_store, tool_loop


_CONFIG = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="test-key",
)


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, config, messages, *, tools=None, **kwargs):
        self.calls.append({"config": config, "messages": messages, "tools": tools, **kwargs})
        return self.responses.pop(0)


async def _no_dispatch(_calls):
    return []


async def _no_fold():
    return []


def _no_usage(_usage):
    return None


def _messages(_transcript):
    return [{"role": "user", "content": "answer once"}]


def test_reply_tool_is_removed_without_touching_other_delivery_tools():
    names = {spec.name for spec in tool_schema.build_tool_specs()}

    assert "reply" not in names
    assert {"send_file", "generate_image", "stay_silent"} <= names


def test_repeated_empty_final_still_raises_empty_reply_after_one_retry(monkeypatch):
    provider = _Provider([
        {
            "reply": "",
            "reasoning": "no visible answer",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {},
        },
        {
            "reply": "",
            "reasoning": "still no visible answer",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    visible = []

    async def on_reply(text, *, final, reasoning=""):
        visible.append((text, final))

    with pytest.raises(tool_loop.ProviderEmptyReply, match="empty_reply"):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_CONFIG,
            build_messages=_messages,
            dispatch_tools=_no_dispatch,
            on_reply=on_reply,
            fold_new_messages=_no_fold,
            add_usage=_no_usage,
            max_calls=5,
        ))

    assert len(provider.calls) == 2
    assert "without visible text" in provider.calls[1]["messages"][0]["content"]
    assert visible == []


def test_admin_chat_page_renders_both_reply_regression_rates():
    report = {
        "outcomes": {"admitted": 2, "started": 2, "completed": 1, "failed": 1},
        "reply_delivery": {},
        "failure_delivery": {},
        "reply_quality": {
            "settled_turns": 2,
            "multi_reply_turns": 1,
            "multi_reply_turn_rate": 0.5,
            "empty_reply_failures": 1,
            "empty_reply_failure_rate": 0.5,
        },
        "settled_jobs": 2,
        "failure_reasons": [],
        "recent_jobs": [],
    }

    with admin_core.bind("view=chat&hours=24"):
        page = data_track._render_chat_reliability_page(report, within_hours=24)

    assert "≥2 条可见回复的回合" in page
    assert "Empty reply 失败" in page
    assert page.count("1 / 50.0%") == 2
    assert "reply_planned" in page
    assert "turn_failed:empty_reply" in page


def _envelope(user_id: str, item_id: str) -> dict:
    return {
        "v": 1,
        "id": item_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": base64.b64encode(b"content-free-test-event").decode(),
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="T208 admin metric mutation proof requires PostgreSQL",
)
def test_admin_reply_quality_metrics_detect_duplicate_mutant_and_empty_failure():
    """A reverted reply path emits a second reply_planned event and is observable."""
    uid = "u_t208_reply_quality_mutant"
    baseline = jobs_store.recent_chat_reliability(within_hours=24)["reply_quality"]
    seed_user(uid)
    set_v2_runtime_owner(uid)
    try:
        reply_job, _ = jobs_store.enqueue_job(uid, "chat")
        jobs_store.append_trajectory_event(
            reply_job,
            uid,
            event_kind="reply_planned",
            idempotency_key="0000_reply_planned",
            payload_envelope=_envelope(uid, "t208-reply-0"),
            payload_bytes=64,
        )
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE agent_jobs SET status='completed',finished_at=now() WHERE id=%s",
                (reply_job,),
            )

        clean = jobs_store.recent_chat_reliability(within_hours=24)["reply_quality"]
        assert clean["settled_turns"] == baseline["settled_turns"] + 1
        assert clean["reply_planned_observed_turns"] == (
            baseline["reply_planned_observed_turns"] + 1
        )
        assert clean["multi_reply_turns"] == baseline["multi_reply_turns"]
        assert clean["empty_reply_failures"] == baseline["empty_reply_failures"]

        # Mutation proof: this is the observable residue produced if the removed
        # intermediate reply path returns and the turn also publishes its final.
        jobs_store.append_trajectory_event(
            reply_job,
            uid,
            event_kind="reply_planned",
            idempotency_key="0001_reply_planned",
            payload_envelope=_envelope(uid, "t208-reply-1"),
            payload_bytes=64,
        )
        empty_job, _ = jobs_store.enqueue_job(uid, "chat")
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE agent_jobs SET status='failed',finished_at=now(),"
                "last_error='turn_failed:empty_reply' WHERE id=%s",
                (empty_job,),
            )

        mutated = jobs_store.recent_chat_reliability(within_hours=24)["reply_quality"]
        assert mutated["settled_turns"] == baseline["settled_turns"] + 2
        assert mutated["reply_planned_observed_turns"] == (
            baseline["reply_planned_observed_turns"] + 1
        )
        assert mutated["multi_reply_turns"] == baseline["multi_reply_turns"] + 1
        assert mutated["empty_reply_failures"] == baseline["empty_reply_failures"] + 1
        assert mutated["multi_reply_turn_rate"] == pytest.approx(
            mutated["multi_reply_turns"] / mutated["settled_turns"]
        )
        assert mutated["empty_reply_failure_rate"] == pytest.approx(
            mutated["empty_reply_failures"] / mutated["settled_turns"]
        )
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
