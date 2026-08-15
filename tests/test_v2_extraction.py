import asyncio
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

from memory.capture_prompt_v1 import (
    build_capture_retry_prompt,
    parse_capture_cards,
)
from memory.card_text import is_retryable_parse_error
from memory.dream_prompt_v1 import build_dream_prompt
from model_api_runtime.v2 import extraction, worker


def _env(inner):
    return {"body_ct": "CT", "_inner": inner}


# ---------- extract ----------

def test_extraction_lane_output_budgets_are_independent_and_dream_is_larger():
    assert extraction.DREAM_MAX_OUTPUT_TOKENS > extraction.CAPTURE_MAX_OUTPUT_TOKENS
    assert extraction.max_output_tokens_for_lane("capture") == (
        extraction.CAPTURE_MAX_OUTPUT_TOKENS
    )
    assert extraction.max_output_tokens_for_lane("dream") == (
        extraction.DREAM_MAX_OUTPUT_TOKENS
    )


@pytest.mark.parametrize(
    ("lane", "env_name"),
    [
        ("capture", "FEEDLING_V2_CAPTURE_MAX_OUTPUT_TOKENS"),
        ("dream", "FEEDLING_V2_DREAM_MAX_OUTPUT_TOKENS"),
    ],
)
def test_extraction_lane_output_budget_has_its_own_env(monkeypatch, lane, env_name):
    try:
        with monkeypatch.context() as env:
            env.setenv(env_name, "1777")
            reloaded = importlib.reload(extraction)
            assert reloaded.max_output_tokens_for_lane(lane) == 1777
    finally:
        # The module object is shared with worker; restore startup-resolved values
        # before the next test observes it.
        importlib.reload(extraction)


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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "auth_invalid"),
        (402, "quota_insufficient"),
        (404, "model_not_found"),
        (429, "rate_limited"),
        (503, "upstream_unavailable"),
    ],
)
def test_extract_provider_failure_reason_is_stable_and_content_free(
    monkeypatch, status, expected
):
    async def _boom(cfg, messages, **kw):
        raise extraction.provider_client.ProviderError(
            "secret upstream response body", status_code=status
        )

    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", _boom
    )
    parsed, err = asyncio.run(
        extraction.extract(
            provider_config=object(),
            prompt="P",
            parse=lambda raw: (["never"], None),
        )
    )
    assert parsed is None
    assert err == f"provider_call_failed:{expected}"
    assert "secret" not in err


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


def test_extract_attributes_length_stop_before_parsing_and_records_safe_shape(
    monkeypatch,
):
    limit = extraction.CAPTURE_MAX_OUTPUT_TOKENS

    async def _truncated(_cfg, _messages, **_kwargs):
        return {
            "reply": '{"cards":[{"summary":"half',
            "stop_reason": "length",
            "usage": {
                "prompt_tokens": 16576,
                "completion_tokens": limit,
            },
        }

    monkeypatch.setattr(
        extraction.provider_client,
        "reliable_chat_completion_async",
        _truncated,
    )
    details = []
    events = []

    async def _record(kind, payload):
        if kind == "extraction_output_truncated":
            events.append(payload)

    def _must_not_parse(_reply):
        raise AssertionError("a length-stopped response must be attributed before parsing")

    parsed, err = asyncio.run(
        extraction.extract(
            provider_config=object(),
            prompt="P",
            parse=_must_not_parse,
            max_tokens=limit,
            trajectory_out=_record,
            failure_detail_out=details.append,
        )
    )

    expected = {
        "stop_reason": "length",
        "completion_tokens": limit,
        "max_tokens": limit,
    }
    assert parsed is None and err == "output_truncated"
    assert details == [expected]
    assert events == [expected]


def test_extract_attributes_length_stop_on_existing_parse_retry(monkeypatch):
    limit = extraction.CAPTURE_MAX_OUTPUT_TOKENS
    calls = 0

    async def _responses(_cfg, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"reply": "bad format", "stop_reason": "end_turn"}
        return {
            "reply": '{"cards":[{"summary":"half',
            "stop_reason": "length",
            "usage": {"completion_tokens": limit},
        }

    monkeypatch.setattr(
        extraction.provider_client,
        "reliable_chat_completion_async",
        _responses,
    )
    details = []
    parsed, err = asyncio.run(
        extraction.extract(
            provider_config=object(),
            prompt="P",
            parse=lambda _raw: (None, "invalid_card_content:summary_empty"),
            max_tokens=limit,
            failure_detail_out=details.append,
            parse_retry=extraction.ParseRetry(
                should_retry=lambda _err: True,
                build_prompt=lambda prompt, _err: prompt + "|retry",
                parse=lambda _raw: (None, "no_json_object"),
            ),
        )
    )

    assert parsed is None and err == "output_truncated"
    assert calls == 2
    assert details == [{
        "stop_reason": "length",
        "completion_tokens": limit,
        "max_tokens": limit,
    }]


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


def test_extract_reasks_once_after_json_decode_error(monkeypatch):
    fake, prompts = _stub_provider([
        '{"cards": [not valid]}',
        '{"cards": []}',
    ])
    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", fake
    )

    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(),
        prompt="P",
        parse=parse_capture_cards,
        parse_retry=extraction.ParseRetry(
            should_retry=is_retryable_parse_error,
            build_prompt=build_capture_retry_prompt,
            parse=lambda reply: parse_capture_cards(reply, strict=False),
        ),
    ))

    assert parsed == [] and err is None
    assert len(prompts) == 2


def test_v2_worker_uses_the_shared_retryable_parse_predicate():
    assert worker.is_retryable_parse_error is is_retryable_parse_error


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


def test_extract_semantic_bounce_shares_the_one_retry_budget(monkeypatch):
    fake, prompts = _stub_provider(["missing-target", "corrected"])
    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", fake
    )

    def _parse(raw):
        target = "mem_1" if raw == "corrected" else ""
        return ([{"action": "supersede", "target_id": target}], None)

    parsed, err = asyncio.run(
        extraction.extract(
            provider_config=object(),
            prompt="P",
            parse=_parse,
            parse_retry=extraction.ParseRetry(
                should_retry=lambda _e: False,
                build_prompt=lambda prompt, _e: prompt,
                parse=_parse,
                semantic_reasons=lambda cards: (
                    ["missing target"] if not cards[0].get("target_id") else []
                ),
                build_semantic_prompt=lambda prompt, _reasons: prompt + "|semantic",
            ),
        )
    )
    assert err is None
    assert parsed[0]["target_id"] == "mem_1"
    assert prompts == ["P", "P|semantic"]


def test_extract_semantic_retry_keeps_valid_cards_when_full_batch_is_returned(
    monkeypatch,
):
    fake, prompts = _stub_provider(["mixed", "corrected-full-batch"])
    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", fake
    )

    def _parse(raw):
        if raw == "mixed":
            return (
                [
                    {"action": "add", "summary": "valid"},
                    {"action": "supersede", "target_id": ""},
                ],
                None,
            )
        return (
            [
                {"action": "add", "summary": "valid"},
                {"action": "supersede", "target_id": "mem_1"},
            ],
            None,
        )

    def _semantic_reasons(cards):
        return [
            "missing target"
            for card in cards
            if card.get("action") == "supersede" and not card.get("target_id")
        ]

    parsed, err = asyncio.run(
        extraction.extract(
            provider_config=object(),
            prompt="P",
            parse=_parse,
            parse_retry=extraction.ParseRetry(
                should_retry=lambda _e: False,
                build_prompt=lambda prompt, _e: prompt,
                parse=_parse,
                semantic_reasons=_semantic_reasons,
                build_semantic_prompt=lambda prompt, _reasons: prompt + "|semantic",
            ),
        )
    )

    assert err is None
    assert parsed == [
        {"action": "add", "summary": "valid"},
        {"action": "supersede", "target_id": "mem_1"},
    ]
    assert prompts == ["P", "P|semantic"]


def test_extract_does_not_issue_third_call_after_format_retry(monkeypatch):
    fake, prompts = _stub_provider(["bad-format", "still-missing-target", "unused"])
    monkeypatch.setattr(
        extraction.provider_client, "reliable_chat_completion_async", fake
    )

    parsed, err = asyncio.run(
        extraction.extract(
            provider_config=object(),
            prompt="P",
            parse=lambda _raw: (None, "invalid_card_content:summary_empty"),
            parse_retry=extraction.ParseRetry(
                should_retry=lambda _e: True,
                build_prompt=lambda prompt, _e: prompt + "|format",
                parse=lambda _raw: ([{"action": "supersede", "target_id": ""}], None),
                semantic_reasons=lambda cards: (
                    ["missing target"] if not cards[0].get("target_id") else []
                ),
                build_semantic_prompt=lambda prompt, _reasons: prompt + "|semantic",
            ),
        )
    )
    assert parsed is None
    assert err == "semantic_validation_failed_after_retry"
    assert prompts == ["P", "P|format"]


def test_extraction_failure_codes_are_allowlisted_and_drop_raw_details():
    assert worker._extraction_failure_code(
        RuntimeError("provider_call_failed:quota_insufficient")
    ) == "extraction_failed:quota_insufficient"
    assert worker._extraction_failure_code(
        RuntimeError("provider_call_failed:sk-live-secret")
    ) == "extraction_failed:unknown"
    assert worker._extraction_failure_code(
        RuntimeError("json_decode_error:JSONDecodeError")
    ) == "extraction_failed:json_decode_error"
    assert worker._extraction_failure_code(
        RuntimeError("extraction_memory_write_rejected:private-db-detail")
    ) == "extraction_failed:memory_write_rejected"
    assert worker._extraction_failure_code(
        RuntimeError("output_truncated")
    ) == "extraction_failed:output_truncated"


def test_extract_provider_reason_records_failure_not_false_success(monkeypatch):
    async def _failed_extract(**_kwargs):
        return None, "provider_call_failed:rate_limited"

    events = []

    async def _failure(user_id, error_class):
        events.append(("failure", user_id, error_class))

    async def _success(user_id, *, latency_ms=None):
        events.append(("success", user_id, latency_ms))

    monkeypatch.setattr(extraction, "extract", _failed_extract)
    monkeypatch.setattr(worker, "_record_provider_failure_class", _failure)
    monkeypatch.setattr(worker, "_record_provider_success", _success)
    result = asyncio.run(
        worker._extract_with_provider_health("u_provider", prompt="P")
    )
    assert result == (None, "provider_call_failed:rate_limited")
    assert events == [("failure", "u_provider", "rate_limited")]


def test_extract_empty_reply_records_route_success(monkeypatch):
    async def _empty_extract(**_kwargs):
        return None, "empty_reply"

    events = []

    async def _failure(user_id, error_class):
        events.append(("failure", user_id, error_class))

    async def _success(user_id, *, latency_ms=None):
        events.append(("success", user_id, latency_ms))

    monkeypatch.setattr(extraction, "extract", _empty_extract)
    monkeypatch.setattr(worker, "_record_provider_failure_class", _failure)
    monkeypatch.setattr(worker, "_record_provider_success", _success)
    result = asyncio.run(
        worker._extract_with_provider_health("u_provider", prompt="P")
    )
    assert result == (None, "empty_reply")
    assert len(events) == 1
    assert events[0][0:2] == ("success", "u_provider")


def test_provider_health_uses_only_catalog_classes(monkeypatch):
    seen = []

    def _record(user_id, *, error_class):
        seen.append((user_id, error_class))

    monkeypatch.setattr(worker.provider_health, "record_failure", _record)
    asyncio.run(
        worker._record_provider_failure_class("u_provider", "provider_config")
    )
    assert seen == [("u_provider", "unknown")]


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
    cards = [
        _existing("m1", occurred_at="2026-02-01T00:00:00Z"),
        _existing("m2", occurred_at="2026-05-01T00:00:00Z"),
    ]
    actions, added, superseded = extraction.consolidations_to_actions(
        [{
            "op": "merge",
            "card_ids": ["m1", "m2"],
            "rationale": "同一事件的两条记录",
            "result": {"summary": "merged", "content": "merged body"},
        }],
        occurred_at="2099-01-01T00:00:00Z", source_ids=[], build_envelope=_env,
        existing_cards=cards)
    assert added == 0 and superseded == 2
    assert actions[0]["type"] == "memory.supersede"
    assert actions[0]["supersedes"] == ["m1", "m2"]
    assert actions[0]["capture_mode"] == "memory_dream"
    assert actions[0]["envelope"]["type"] == "fact"
    assert actions[0]["envelope"]["occurred_at"] == "2026-05-01T00:00:00Z"


def _existing(
    memory_id,
    *,
    source="memory_capture",
    created_at="2026-07-01T00:00:00Z",
    occurred_at="2026-06-01T00:00:00Z",
):
    return {
        "id": memory_id,
        "summary": f"{memory_id} 的旧摘要",
        "content": f"{memory_id} 的旧卡正文，包含需要完整保留的具体事实。",
        "source": source,
        "created_at": created_at,
        "occurred_at": occurred_at,
    }


def _merge(ids, *, content="合并后的正文包含两张旧卡的全部具体事实，并保留原始上下文。"):
    return {
        "op": "merge",
        "card_ids": list(ids),
        "rationale": "同一事件或同一线索的演进",
        "result": {"summary": "合并摘要", "content": content},
    }


def test_dream_guard_has_no_quantity_cap():
    cards = [_existing(f"m{i}") for i in range(13)]
    consolidations = [_merge([f"m{i}", f"m{i + 1}"]) for i in range(0, 12, 2)]

    actions, _added, superseded = extraction.consolidations_to_actions(
        consolidations,
        occurred_at="2026-08-01T00:00:00Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=cards,
    )

    assert len(actions) == 6
    assert superseded == 12
    touched = {memory_id for action in actions for memory_id in action["supersedes"]}
    assert touched == {f"m{i}" for i in range(12)}
    assert "m12" not in touched


def test_dream_guard_rejects_unknown_and_overlapping_targets():
    cards = [_existing("m1"), _existing("m2"), _existing("m3")]
    actions, _added, superseded = extraction.consolidations_to_actions(
        [
            _merge(["m1", "m2"]),
            _merge(["m2", "m3"]),
            _merge(["missing", "m3"]),
        ],
        occurred_at="2026-08-01T00:00:00Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=cards,
    )

    assert len(actions) == 1
    assert actions[0]["supersedes"] == ["m1", "m2"]
    assert superseded == 2


def test_dream_one_to_one_rewrite_is_not_content_judged():
    # 2026-08-05 复盘:15% 增量栅栏(内容质量判断)已拆除。一次「更精炼但更准」
    # 的 1:1 改写是合法整理,mapper 不再评判文本长没长、够不够新。
    card = _existing("m1")
    actions, added, superseded = extraction.consolidations_to_actions(
        [{"op": "thicken", "card_ids": ["m1"],
          "rationale": "同一卡片改写得更准确", "result": {
            "summary": "改写摘要", "content": "更短但更准的改写。",
        }}],
        occurred_at="2026-08-01T00:00:00Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=[card],
    )

    assert len(actions) == 1
    assert superseded == 1


def test_dream_guard_allows_prior_run_dream_output_to_evolve_immediately():
    fresh = _existing(
        "dream-new",
        source="memory_dream",
        created_at="2026-07-30T00:00:00Z",
    )
    other = _existing("m2")
    actions, added, superseded = extraction.consolidations_to_actions(
        [_merge(["dream-new", "m2"])],
        occurred_at="2026-08-01T00:00:00Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=[fresh, other],
    )

    assert added == 0 and superseded == 2
    assert len(actions) == 1


def test_dream_guard_accepts_low_text_overlap_evolution_after_review():
    cards = [
        {"id": "plan", "summary": "想去京都看红叶", "content": "希望今年秋天成行。",
         "occurred_at": "2026-03-01T00:00:00Z"},
        {"id": "ticket", "summary": "已经订好机票", "content": "11 月飞往关西机场。",
         "occurred_at": "2026-05-01T00:00:00Z"},
    ]

    actions, _added, superseded = extraction.consolidations_to_actions(
        [_merge(["plan", "ticket"], content="京都计划已从意向推进到 11 月出票。")],
        occurred_at="2026-08-03T00:00:00Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=cards,
    )

    assert len(actions) == 1 and superseded == 2


def test_dream_merge_uses_latest_source_occurred_at_not_worker_now():
    cards = [
        _existing("feb", occurred_at="2026-02-08T09:00:00Z"),
        _existing("mar", occurred_at="2026-03-17T10:00:00+08:00"),
        _existing("may", occurred_at="2026-05-03"),
    ]

    actions, _added, superseded = extraction.consolidations_to_actions(
        [_merge(["feb", "mar", "may"])],
        occurred_at="2099-12-31T23:59:59Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=cards,
    )

    assert superseded == 3
    assert actions[0]["envelope"]["occurred_at"] == "2026-05-03"


def test_dream_merge_with_today_source_naturally_keeps_today():
    cards = [
        _existing("old", occurred_at="2026-02-08T09:00:00Z"),
        _existing("today", occurred_at="2026-08-14T07:30:00Z"),
    ]

    actions, _added, _superseded = extraction.consolidations_to_actions(
        [_merge(["old", "today"])],
        occurred_at="2099-12-31T23:59:59Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=cards,
    )

    assert actions[0]["envelope"]["occurred_at"] == "2026-08-14T07:30:00Z"


@pytest.mark.parametrize("bad_value", ["", None, "not-a-date", "1970-ish"])
def test_dream_merge_fails_closed_when_any_source_time_is_unusable(bad_value):
    cards = [
        _existing("valid", occurred_at="2026-02-08T09:00:00Z"),
        _existing("bad", occurred_at=bad_value),
    ]

    with pytest.raises(ValueError, match="dream_source_occurred_at_unavailable"):
        extraction.consolidations_to_actions(
            [_merge(["valid", "bad"])],
            occurred_at="2099-12-31T23:59:59Z",
            source_ids=[],
            build_envelope=_env,
            existing_cards=cards,
        )


def test_dream_prompt_pairs_evolution_example_with_independent_health_example():
    prompt = build_dream_prompt(
        ai_name="小柒",
        user_name="阿霖",
        cards="[]",
        recent_conversations="[]",
    )

    assert "想去京都看红叶" in prompt and "已经订了京都机票" in prompt
    assert "坚持骑行" in prompt and "最近失眠" in prompt
    assert "两件独立的事，不能合并" in prompt


def test_dream_guard_rejects_rationale_free_proposal():
    cards = [_existing("m1"), _existing("m2")]
    no_rationale = _merge(["m1", "m2"])
    no_rationale["rationale"] = ""

    actions, added, superseded = extraction.consolidations_to_actions(
        [no_rationale],
        occurred_at="2026-08-03T00:00:00Z",
        source_ids=[],
        build_envelope=_env,
        existing_cards=cards,
    )
    assert (actions, added, superseded) == ([], 0, 0)


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
