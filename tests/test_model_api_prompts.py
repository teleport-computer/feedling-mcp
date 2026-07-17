"""Pure-unit tests for the hosted foreground chat prompt builder
(model_api_runtime/prompts.py build_foreground_chat_messages).

Guards the prompt-construction side of:
  - P1a/P1b: the persona/voice layer + the user-authored custom_persona_prompt
    reach the model. The builder json.dumps the whole context_payload, so any
    fields hosted/context.py puts in `identity` surface in the prompt; the
    custom_persona_prompt precedence instruction must be in the system prompt.
  - P3: the soft-recall memory_index and its recall instruction reach the model.

No Flask, no DB — prompts.py imports cleanly with backend on sys.path.

Run:  python -m pytest tests/test_model_api_prompts.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime import prompts  # noqa: E402


def _system_blob(messages):
    return "\n".join(m["content"] for m in messages if m.get("role") == "system")


def _build(payload, user_message="在吗", recent=None):
    return prompts.build_foreground_chat_messages(
        context_payload=payload,
        recent_messages=recent or [],
        user_message=user_message,
    )


def _language_system_messages(messages):
    return [
        m["content"]
        for m in messages
        if m.get("role") == "system"
        and (
            str(m.get("content") or "").startswith("Reply language policy:")
            or str(m.get("content") or "").startswith("回复语言规则：")
        )
    ]


def test_custom_persona_prompt_precedence_instruction_present():
    # P1b: the system prompt must instruct the model to treat
    # custom_persona_prompt as the highest-priority persona directive.
    msgs = _build({"identity": {"agent_name": "Kai"}})
    blob = _system_blob(msgs)
    assert "custom_persona_prompt" in blob
    assert "highest-priority" in blob


def test_memory_index_recall_instruction_present():
    # P3: the system prompt must tell the model it may recall a card from
    # memory_index even if it wasn't a lexical candidate.
    blob = _system_blob(_build({"identity": {"agent_name": "Kai"}}))
    assert "memory_index" in blob


def test_persona_and_index_values_reach_the_prompt():
    # The builder serializes the whole context_payload, so persona fields and
    # the soft-recall index land in the prompt verbatim.
    payload = {
        "identity": {
            "agent_name": "Kai",
            "custom_persona_prompt": "像老朋友一样损我",
            "tone_style": "毒舌但温柔",
        },
        "context_memory_selection": {
            "memory_index": [
                {"id": "m1", "type": "fact", "title": "养了猫 Mochi", "occurred_at": "2026-01-01"},
            ],
        },
    }
    blob = _system_blob(_build(payload))
    assert "像老朋友一样损我" in blob   # custom_persona_prompt value
    assert "毒舌但温柔" in blob          # tone_style value
    assert "Mochi" in blob               # memory_index title


def test_user_message_is_appended():
    msgs = _build({"identity": {}}, user_message="今天好累")
    assert any(m["role"] == "user" and m["content"] == "今天好累" for m in msgs)


def test_empty_persona_does_not_crash_and_omits_nothing_required():
    # No persona fields set → builder still produces a valid system+user turn.
    msgs = _build({"identity": {"agent_name": ""}})
    assert msgs[0]["role"] == "system"
    assert any(m["role"] == "user" for m in msgs)


def test_foreground_reply_language_line_is_independent_system_message():
    payload = {
        "identity": {
            "custom_persona_prompt": (
                "You are a steady English companion with a concise, warm, direct voice."
            )
        },
        "context_memories": [
            {"summary": "中文记忆", "content": "用户最近记录了很多中文生活片段。"},
        ],
    }

    msgs = _build(payload, user_message="今天好累")
    language_lines = _language_system_messages(msgs)

    assert len(language_lines) == 1
    assert "Default reply language: English" in language_lines[0]
    assert "latest message is clearly in another language" in language_lines[0]
    assert not language_lines[0].startswith("Feedling runtime context JSON:")


def test_foreground_reply_language_falls_back_to_archive_language():
    msgs = _build({
        "identity": {},
        "context_memories": [],
        "archive_language": "en",
    })

    language_lines = _language_system_messages(msgs)

    assert len(language_lines) == 1
    assert "Default reply language: English" in language_lines[0]


def test_pending_confirmation_includes_reply_language_line():
    msgs = prompts.build_pending_confirmation_messages({
        "identity": {
            "custom_persona_prompt": (
                "You are a gentle English companion who asks short confirmation questions."
            )
        },
        "archive_language": "zh-Hans",
        "pending_updates": [
            {"target": "identity.language_preference", "changes": [{"field": "language_preference", "to": "en"}]},
        ],
    })

    language_lines = _language_system_messages(msgs)

    assert len(language_lines) == 1
    assert "Default reply language: English" in language_lines[0]
    assert msgs[-1]["role"] == "user"


def test_memory_capture_prompt_uses_v1_shape_and_existing_terms():
    msgs = prompts.build_memory_capture_messages(
        user_message="我养了一只狗叫蛋子。",
        assistant_reply="蛋子是什么样的狗？",
        context_payload={
            "existing_memory_terms": {"buckets": ["宠物"], "threads": ["蛋子"]},
            "context_memories": [],
            "identity": {},
        },
    )
    blob = _system_blob(msgs)
    payload = msgs[1]["content"]
    assert "Memory write guidance" in blob
    assert "summary" in blob
    assert "content" in blob
    assert "bucket" in blob
    assert "threads" in blob
    assert "title" not in blob
    assert "宠物" in payload
    assert "蛋子" in payload
