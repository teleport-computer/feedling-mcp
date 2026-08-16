from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import profile


def _reply(memory: str = "共同经历与承诺", style: str = "偏好直接温和的建议") -> str:
    return json.dumps({"memory": memory, "style": style}, ensure_ascii=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "reply_not_text"),
        ("", "reply_empty"),
        ("不是 JSON", "reply_not_json"),
        ('{"memory":"只有一边"}', "missing_field:style"),
        ('{"style":"只有一边"}', "missing_field:memory"),
        ('{"memory":"","style":"有效"}', "field_empty:memory"),
        ('{"memory":"有效","style":17}', "field_empty:style"),
        (
            '{"memory":"有效","style":"有效","extra":"不允许"}',
            "reply_not_json",
        ),
    ],
)
def test_validate_profile_reject_matrix_is_all_or_nothing(raw, expected):
    fields, reject = profile._validate_profile(raw)
    assert fields is None
    assert reject == expected


def test_validate_profile_accepts_fenced_json_and_strips_both_fields():
    fields, reject = profile._validate_profile(
        "```json\n" + _reply("  我们在上海认识  ", "  先共情再给方案  ") + "\n```"
    )

    assert reject == ""
    assert fields == {
        "memory": "我们在上海认识",
        "style": "先共情再给方案",
    }


def test_cjk_memory_character_limit_exact_and_plus_one():
    at_limit = "记" * profile.PROFILE_MEMORY_MAX_CHARS
    fields, reject = profile._validate_profile(_reply(at_limit, "偏好简洁回答"))
    assert reject == ""
    assert fields is not None
    assert len(fields["memory"]) == profile.PROFILE_MEMORY_MAX_CHARS

    fields, reject = profile._validate_profile(_reply(at_limit + "忆", "偏好简洁回答"))
    assert fields is None
    assert reject == (
        f"memory_chars_over_budget:{profile.PROFILE_MEMORY_MAX_CHARS + 1}"
    )


def test_cjk_style_character_limit_exact_and_plus_one():
    at_limit = "稳" * profile.PROFILE_STYLE_MAX_CHARS
    fields, reject = profile._validate_profile(_reply("共同经历保持更新", at_limit))
    assert reject == ""
    assert fields is not None
    assert len(fields["style"]) == profile.PROFILE_STYLE_MAX_CHARS

    fields, reject = profile._validate_profile(
        _reply("共同经历保持更新", at_limit + "定")
    )
    assert fields is None
    assert reject == f"style_chars_over_budget:{profile.PROFILE_STYLE_MAX_CHARS + 1}"


@pytest.mark.parametrize(
    ("memory", "style", "expected"),
    [
        ("TODO", "先共情", "placeholder_detected:memory"),
        ("共同经历", "[communication style]", "placeholder_detected:style"),
        ("【待填写事实】", "先共情", "placeholder_detected:memory"),
    ],
)
def test_placeholder_is_rejected_without_partial_salvage(memory, style, expected):
    fields, reject = profile._validate_profile(_reply(memory, style))
    assert fields is None
    assert reject == expected


def test_overlap_nfkc_casefold_and_punctuation_is_observation_only():
    zero = profile._overlap_observation("甲乙丙丁戊己", "春夏秋冬天地")
    low = profile._overlap_observation("abcdefghi", "abcdWXYZ")
    high = profile._overlap_observation(
        "ＡＢＣＤ，efgh",
        "abcd EF-GH",
    )

    assert zero.ratio == 0.0
    assert low.shared_grams == 1
    assert low.denominator_grams == 5
    assert low.ratio == pytest.approx(0.2)
    assert low.would_reject is False
    assert high.ratio == 1.0
    assert high.would_reject is True

    fields, reject, observed = profile._validate_profile_with_observation(
        _reply("ＡＢＣＤ，efgh", "abcd EF-GH")
    )
    assert reject == ""
    assert fields is not None
    assert observed == high


def test_reject_codes_never_copy_rejected_content():
    secret_memory = "绝密甲乙丙丁"
    secret_style = "隐私春夏秋冬"
    cases = [
        f"not-json-{secret_memory}",
        json.dumps({"memory": secret_memory}, ensure_ascii=False),
        _reply(secret_memory * 400, secret_style),
        _reply("[绝密占位内容]", secret_style),
    ]

    for raw in cases:
        fields, reject = profile._validate_profile(raw)
        assert fields is None
        assert reject
        assert secret_memory not in reject
        assert secret_style not in reject


def test_prompt_hard_codes_field_boundary_and_configured_character_budgets():
    messages = profile.build_profile_prompt(
        "garden cards",
        memory_max_chars=22,
        style_max_chars=13,
    )
    rendered = "\n".join(message["content"] for message in messages)

    assert "MEMORY=事实，STYLE=方式" in rendered
    assert "22 个 Unicode 字符" in rendered
    assert "13 个 Unicode 字符" in rendered
    assert "<UNTRUSTED_MEMORY_GARDEN>" in rendered
    assert "garden cards" in rendered


def test_generate_profile_single_call_returns_overlap_telemetry():
    calls = []
    events = []

    async def _llm(config, messages, **kwargs):
        calls.append((config, messages, kwargs))
        return {
            "reply": _reply("ＡＢＣＤefgh", "abcd-efgh"),
            "usage": {"input_tokens": 10},
        }

    async def _trajectory(kind, payload):
        events.append((kind, payload))

    usages = []
    result = asyncio.run(
        profile.generate_profile(
            provider_config="cfg",
            rendered_cards="cards",
            llm=_llm,
            usage_out=usages.append,
            trajectory_out=_trajectory,
        )
    )

    assert result.fields == {
        "memory": "ＡＢＣＤefgh",
        "style": "abcd-efgh",
    }
    assert result.reject_code == ""
    assert result.provider_calls == 1
    assert result.overlap is not None and result.overlap.would_reject is True
    assert len(calls) == 1
    assert usages == [{"input_tokens": 10}]
    assert events == [
        (
            "provider_request",
            {
                "tail_window": {
                    "lane": "profile",
                    "profile_cards_truncated": False,
                }
            },
        ),
        ("profile_overlap_observed", result.overlap.as_dict()),
    ]


def test_shape_error_bounces_once_with_content_free_correction():
    replies = [
        "invalid-json-with-SECRET-REPLY",
        _reply("共同经历", "偏好简洁"),
    ]
    prompts = []
    events = []

    async def _llm(_config, messages, **_kwargs):
        prompts.append(messages)
        return {"reply": replies[len(prompts) - 1]}

    async def _trajectory(kind, payload):
        events.append((kind, payload))

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards="SOURCE-CONTEXT",
            llm=_llm,
            trajectory_out=_trajectory,
        )
    )

    assert result.fields == {"memory": "共同经历", "style": "偏好简洁"}
    assert result.provider_calls == 2
    assert len(prompts) == 2
    correction = prompts[1][-1]["content"]
    assert "SECRET-REPLY" not in correction
    assert "SOURCE-CONTEXT" not in correction
    assert "JSON" in correction
    assert events[1] == (
        "profile_parse_bounced",
        {"reason": "reply_not_json"},
    )


@pytest.mark.parametrize(
    "first_reply",
    [
        '{"memory":"只有一边"}',
        _reply("记" * (profile.PROFILE_MEMORY_MAX_CHARS + 1), "有效方式"),
        _reply("有效事实", "稳" * (profile.PROFILE_STYLE_MAX_CHARS + 1)),
        _reply("TODO", "有效方式"),
    ],
)
def test_each_retryable_shape_family_gets_exactly_one_bounce(first_reply):
    calls = 0

    async def _llm(_config, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "reply": (first_reply if calls == 1 else _reply("有效事实", "有效方式"))
        }

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards="cards",
            llm=_llm,
        )
    )

    assert calls == 2
    assert result.provider_calls == 2
    assert result.fields == {"memory": "有效事实", "style": "有效方式"}


def test_shape_error_is_bounced_at_most_once():
    calls = 0
    rejects = []

    async def _llm(_config, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {"reply": _reply("[placeholder]", "沟通偏好")}

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards="cards",
            llm=_llm,
            reject_out=rejects.append,
        )
    )

    assert calls == 2
    assert result.provider_calls == 2
    assert result.fields is None
    assert result.reject_code == "placeholder_detected:memory"
    assert rejects == ["placeholder_detected:memory"]


@pytest.mark.parametrize(
    "terminal_reply",
    [
        None,
        "",
        _reply("", "有效方式"),
    ],
)
def test_non_retryable_parse_failures_are_terminal(terminal_reply):
    calls = 0

    async def _llm(_config, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {"reply": terminal_reply}

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards="cards",
            llm=_llm,
        )
    )

    assert calls == 1
    assert result.provider_calls == 1
    assert result.reject_code in {
        "reply_not_text",
        "reply_empty",
        "field_empty:memory",
    }


def test_provider_exception_is_not_treated_as_parse_retry():
    calls = 0
    usage = []

    async def _llm(_config, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider-secret")

    with pytest.raises(RuntimeError, match="provider-secret"):
        asyncio.run(
            profile.generate_profile(
                provider_config=object(),
                rendered_cards="cards",
                llm=_llm,
                usage_out=usage.append,
            )
        )

    assert calls == 1
    assert usage == [None]


def test_large_source_map_reduce_is_lossless_ordered_and_bounded():
    source = "甲" * (profile.PROFILE_SINGLE_CALL_MAX_CHARS + 1)
    map_sources = []
    calls = 0

    async def _llm(_config, messages, **_kwargs):
        nonlocal calls
        calls += 1
        if messages[0]["content"] == profile._PROFILE_MAP_SYSTEM_PROMPT:
            content = messages[1]["content"]
            prefix = "[来源片段 1]\n"
            start = content.index(prefix) + len(prefix)
            end = content.index("\n</UNTRUSTED_PROFILE_SOURCE>")
            map_sources.append(content[start:end])
            return {"reply": f"- 中间归纳 {calls}"}
        return {"reply": _reply("共同事实", "沟通方式")}

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards=source,
            llm=_llm,
        )
    )

    assert result.fields == {"memory": "共同事实", "style": "沟通方式"}
    assert result.provider_calls == 3
    assert calls == 3
    assert "".join(map_sources) == source


def test_impossible_large_source_raises_dedicated_exhaustion_before_paid_calls():
    source = "源" * (
        profile.PROFILE_SINGLE_CALL_MAX_CHARS * profile.PROFILE_MAX_PROVIDER_CALLS + 1
    )
    calls = 0

    async def _llm(_config, _messages, **_kwargs):
        nonlocal calls
        calls += 1
        return {"reply": "- should not run"}

    with pytest.raises(
        profile.ProfileGenerationExhausted,
        match=f"^profile_source_exceeds_budget:{len(source)}$",
    ):
        asyncio.run(
            profile.generate_profile(
                provider_config=object(),
                rendered_cards=source,
                llm=_llm,
            )
        )

    assert calls == 0


def test_invalid_map_summary_is_content_free_terminal_failure():
    source = "源" * (profile.PROFILE_SINGLE_CALL_MAX_CHARS + 1)

    async def _llm(_config, _messages, **_kwargs):
        return {"reply": "MAP-SECRET-NOT-A-BULLET"}

    result = asyncio.run(
        profile.generate_profile(
            provider_config=object(),
            rendered_cards=source,
            llm=_llm,
        )
    )

    assert result.provider_calls == 1
    assert result.reject_code == "map_line_not_bullet:0"
    assert "MAP-SECRET" not in result.reject_code


def test_fragmentation_prefers_newlines_without_loss_or_reordering():
    source = "一二三\n四五六七\n八九十"
    fragments = profile._fragments(source, max_chars=6)
    groups = profile._groups(fragments, max_chars=10)

    assert "".join(fragments) == source
    assert "".join(piece for group in groups for piece in group) == source
    assert all(len(fragment) <= 6 for fragment in fragments)
