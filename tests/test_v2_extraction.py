import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

from model_api_runtime.v2 import extraction


def _env(inner):
    return {"body_ct": "CT", "_inner": inner}


# ---------- extract ----------

def test_extract_returns_parsed_on_success(monkeypatch):
    async def _fake(cfg, messages, **kw):
        return {
            "reply": '{"cards": []}',
            "usage": {"prompt_tokens": 12, "cache_read_tokens": 8},
        }

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    seen = []
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["ok"], None),
        usage_out=seen.append))
    assert parsed == ["ok"] and err is None
    assert seen == [{"prompt_tokens": 12, "cache_read_tokens": 8}]


def test_extract_forwards_reliable_attempt_progress(monkeypatch):
    seen = []

    async def _fake(cfg, messages, **kw):
        callback = kw["progress_cb"]
        callback("attempt_start", 1)
        callback("attempt_complete", 1)
        return {"reply": '{"cards": []}'}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["ok"], None),
        progress_cb=lambda stage, attempt: seen.append((stage, attempt))))

    assert parsed == ["ok"] and err is None
    assert seen == [("attempt_start", 1), ("attempt_complete", 1)]


def test_extract_returns_reason_on_provider_error(monkeypatch):
    async def _boom(cfg, messages, **kw):
        raise RuntimeError("402 no credit")

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _boom)
    seen = []
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["x"], None),
        usage_out=seen.append))
    assert parsed is None
    assert err.startswith("provider_call_failed:")
    assert seen == [None]


def test_extract_returns_reason_on_parse_error(monkeypatch):
    async def _fake(cfg, messages, **kw):
        return {"reply": "garbage"}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (None, "bad_json")))
    assert parsed is None and err == "bad_json"


def test_extract_treats_empty_reply_as_a_reason_not_a_crash(monkeypatch):
    async def _fake(cfg, messages, **kw):
        return {"reply": "   "}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["never"], None)))
    assert parsed is None and err == "empty_reply"


# ---------- cards_to_actions ----------

def test_cards_to_actions_add_and_supersede():
    cards = [
        {"action": "add", "summary": "s1", "content": "c1", "bucket": "b", "threads": ["t"]},
        {"action": "supersede", "target_id": "m_old", "summary": "s2", "content": "c2"},
    ]
    actions, added, superseded = extraction.cards_to_actions(
        cards, occurred_at="2026-07-10T10:00:00Z", source_ids=["m1"], build_envelope=_env)
    assert added == 1 and superseded == 1
    assert actions[0]["type"] == "memory.add"
    assert actions[0]["capture_mode"] == "memory_capture"
    assert actions[0]["source_chat_message_ids"] == ["m1"]
    assert actions[0]["envelope"]["occurred_at"] == "2026-07-10T10:00:00Z"
    assert actions[0]["envelope"]["type"] == "event"
    assert actions[0]["envelope"]["source"] == "memory_capture"
    assert actions[1]["type"] == "memory.supersede"
    assert actions[1]["supersedes"] == "m_old"
    # the envelope carries the card's inner fields, built by the injected callable
    assert actions[0]["envelope"]["_inner"]["summary"] == "s1"
    assert actions[0]["envelope"]["_inner"]["threads"] == ["t"]


def test_merge_or_supersede_without_target_degrades_to_add():
    """Ported verbatim from the resident: a merge with no target_id is an add."""
    actions, added, superseded = extraction.cards_to_actions(
        [{"action": "merge", "summary": "s"}],
        occurred_at="T", source_ids=[], build_envelope=_env)
    assert added == 1 and superseded == 0
    assert actions[0]["type"] == "memory.add"


def test_unknown_action_yields_nothing_and_nonempty_cards_raise():
    """Non-empty cards that produce zero actions is a hard error — the model returned
    something we don't understand, and silently writing nothing would hide it."""
    with pytest.raises(ValueError, match="capture_no_memory_actions"):
        extraction.cards_to_actions(
            [{"action": "frobnicate"}], occurred_at="T", source_ids=[], build_envelope=_env)


def test_empty_cards_is_not_an_error():
    actions, added, superseded = extraction.cards_to_actions(
        [], occurred_at="T", source_ids=[], build_envelope=_env)
    assert actions == [] and added == 0 and superseded == 0


# ---------- consolidations_to_actions ----------

def test_consolidations_to_actions_supersedes_when_target_present():
    actions, added, superseded = extraction.consolidations_to_actions(
        [{
            "op": "merge",
            "card_ids": ["m1", "m2"],
            "result": {"summary": "merged", "content": "merged body"},
        }],
        occurred_at="T", source_ids=[], build_envelope=_env)
    assert added == 0 and superseded == 2
    assert actions[0]["type"] == "memory.supersede"
    assert actions[0]["supersedes"] == ["m1", "m2"]
    assert actions[0]["capture_mode"] == "memory_dream"
    assert actions[0]["envelope"]["type"] == "fact"
    assert actions[0]["envelope"]["occurred_at"] == "T"


def test_nonempty_actions_require_occurred_at():
    with pytest.raises(ValueError, match="memory_occurred_at_required"):
        extraction.cards_to_actions(
            [{"action": "add", "summary": "s"}],
            occurred_at="", source_ids=[], build_envelope=_env,
        )


def test_extraction_is_pure():
    import pathlib
    src = pathlib.Path(extraction.__file__).read_text()
    for forbidden in ("import hosted", "from hosted", "agent_runtime", "jobs_store",
                      "core.store", "psycopg", "memory_core"):
        assert forbidden not in src, f"extraction.py must not reference {forbidden}"
