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


# ---------- parse_retry(内容闸打回)----------

def _stub_provider(replies):
    """provider 桩:按顺序吐 replies,并记录每次收到的 prompt。"""
    seen_prompts = []

    async def _fake(cfg, messages, **kw):
        seen_prompts.append(messages[0]["content"])
        return {"reply": replies[len(seen_prompts) - 1]}

    return _fake, seen_prompts


def test_extract_bounces_format_error_once_and_recovers(monkeypatch):
    fake, prompts = _stub_provider(["placeholder", "real"])
    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P",
        parse=lambda raw: (None, "invalid_card_content:content_empty"),
        parse_retry=extraction.ParseRetry(
            should_retry=lambda e: e.startswith("invalid_card_content"),
            build_prompt=lambda prompt, e: f"{prompt}|redo:{e}",
            parse=lambda raw: (["clean"], None),
        )))
    assert parsed == ["clean"] and err is None
    # 第二问必须带着原 prompt 的上下文 + 具体哪里没填
    assert prompts == ["P", "P|redo:invalid_card_content:content_empty"]


def test_extract_bounces_at_most_once(monkeypatch):
    fake, prompts = _stub_provider(["placeholder", "still placeholder"])
    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P",
        parse=lambda raw: (None, "invalid_card_content:summary_empty"),
        parse_retry=extraction.ParseRetry(
            should_retry=lambda e: True,
            build_prompt=lambda prompt, e: prompt,
            parse=lambda raw: (None, "invalid_card_content:summary_empty"),
        )))
    assert parsed is None and err == "invalid_card_content:summary_empty"
    assert len(prompts) == 2  # 不反复捶打 provider


def test_extract_does_not_bounce_non_format_errors(monkeypatch):
    fake, prompts = _stub_provider(["garbage", "unused"])
    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P",
        parse=lambda raw: (None, "no_json_object"),
        parse_retry=extraction.ParseRetry(
            should_retry=lambda e: e.startswith("invalid_card_content"),
            build_prompt=lambda prompt, e: prompt,
            parse=lambda raw: (["never"], None),
        )))
    # JSON 根本没出来 / provider 挂了,各有自己的路径;重问同一段 prompt 没有意义
    assert parsed is None and err == "no_json_object"
    assert len(prompts) == 1


def test_extract_reports_the_retry_hops_own_failure(monkeypatch):
    calls = {"n": 0}

    async def _fake(cfg, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"reply": "placeholder"}
        raise RuntimeError("402 no credit")

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P",
        parse=lambda raw: (None, "invalid_card_content:summary_empty"),
        parse_retry=extraction.ParseRetry(
            should_retry=lambda e: True,
            build_prompt=lambda prompt, e: prompt,
            parse=lambda raw: (["never"], None),
        )))
    # 重问这一跳自己挂了就如实报,别伪装成原来的格式问题
    assert parsed is None and err.startswith("provider_call_failed:")


def test_extract_records_the_bounce_in_the_trajectory(monkeypatch):
    fake, _prompts = _stub_provider(["placeholder", "real"])
    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", fake)
    events = []

    async def _record(kind, payload):
        events.append((kind, payload))

    asyncio.run(extraction.extract(
        provider_config=object(), prompt="P",
        parse=lambda raw: (None, "invalid_card_content:content_empty"),
        trajectory_out=_record,
        parse_retry=extraction.ParseRetry(
            should_retry=lambda e: True,
            build_prompt=lambda prompt, e: prompt,
            parse=lambda raw: (["clean"], None),
        )))
    bounced = [p for kind, p in events if kind == "parse_bounced"]
    assert bounced == [{"reason": "invalid_card_content:content_empty"}]


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


def test_merge_or_supersede_without_target_is_discarded():
    """An invalid merge must never be rewritten into a different legal action."""
    actions, added, superseded = extraction.cards_to_actions(
        [{"action": "merge", "summary": "s"}],
        occurred_at="T", source_ids=[], build_envelope=_env)
    assert added == 0 and superseded == 0
    assert actions == []


def test_missing_target_is_discarded_while_valid_card_is_preserved():
    actions, added, superseded = extraction.cards_to_actions(
        [
            {"action": "supersede", "summary": "invalid"},
            {"action": "add", "summary": "valid", "content": "valid body"},
        ],
        occurred_at="T",
        source_ids=[],
        build_envelope=_env,
    )
    assert added == 1 and superseded == 0
    assert [action["type"] for action in actions] == ["memory.add"]
    assert actions[0]["envelope"]["_inner"]["summary"] == "valid"


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
