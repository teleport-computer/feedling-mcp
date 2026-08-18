"""
Unit tests for the context_memories selection logic (enclave_app.py).
============================================================================

These cover the pure-function selection algorithm — no enclave or Flask
infrastructure required. The selection is what the agent reads alongside
chat history every time it polls; correctness here directly affects every
chat round.

Run with: pytest tests/test_context_memories.py -v
"""

from __future__ import annotations
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Pure-function module — zero native deps, imports cleanly anywhere Python runs.
from memory import card_shape  # noqa: E402
from memory_garden.scoring.relevance import (  # noqa: E402
    char_bigrams as _char_bigrams,
    bigram_jaccard as _bigram_jaccard,
    select_context_memories as _kernel_select,
    select_context_memories_with_trace as _kernel_select_with_trace,
)


# 2026-08-17 边界整理：内核只认翻译后的卡，且生命周期过滤归宿主。
# 在这层包住，34 个调用点就都自动走真实的三段式：
#     宿主过滤 → 宿主翻译 → 内核挑卡
# 这样测试跑的路径和线上完全一致 —— 而不是绕过入口直接喂内核。
def _for_garden(moments: list[dict]) -> list[dict]:
    return [card_shape.to_garden_card(m) for m in moments if not card_shape.is_retired(m)]


def _select_context_memories(moments, *args, **kwargs):
    return _kernel_select(_for_garden(moments), *args, **kwargs)


def _select_context_memories_with_trace(moments, *args, **kwargs):
    return _kernel_select_with_trace(_for_garden(moments), *args, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _moment(
    *,
    id: str,
    title: str,
    description: str = "",
    occurred_at: str = "2026-01-01T00:00:00",
    created_at: str = "2026-01-01T00:00:00",
    type: str = "",
    source: str = "",
) -> dict:
    # 工厂产出**原始卡**（io 的老形状）。翻译不在这里做 ——
    # 2026-08-17 踩过：工厂里翻译会让后续加的 is_archived / source 落在
    # 翻译产物上，把「归档过滤必须在翻译之前」这个真实入口问题掩盖掉。
    # 正确顺序见 _for_garden()：过滤 → 翻译 → 挑卡，与线上一致。
    return ({
        "id": id,
        "title": title,
        "description": description,
        "type": type,
        "occurred_at": occurred_at,
        "created_at": created_at,
        "source": source,
    })



def _retrieval_fixture_moments() -> list[dict]:
    return [
        _moment(
            id="toho",
            title="TOHO Project 老二次元偏好",
            description="用户是 toho project（东方Project）老二次元，喜欢东方 Project 相关内容。",
        ),
        _moment(
            id="pressure",
            title="近期项目压力",
            description="用户明天有一个项目要完成，觉得很累。",
        ),
        _moment(
            id="lemon",
            title="喜欢柠檬茶",
            description="用户晚上想喝清爽一点的饮料。",
        ),
        _moment(
            id="chickens",
            title="烧卖和蒸饺",
            description="用户养的两只科钦球母鸡叫烧卖和蒸饺。",
        ),
        _moment(
            id="a03",
            title="硅基 A03 模型偏好",
            description="用户提过想看更适合 LLM 的硅基 A03。",
        ),
        _moment(
            id="claude_vps",
            title="Claude Code VPS 工作流",
            description="用户在 VPS 上跑 Claude Code，并通过 Feedling 通信。",
        ),
        _moment(
            id="raw_json",
            title="原始JSON输出问题",
            description="第一条消息曾把原始 JSON 直接吐出来。",
        ),
        _moment(
            id="job_change",
            title="深夜你说想换工作",
            description="用户最近又在想换工作的事。",
        ),
        _moment(
            id="nyu",
            title="NYU beta 测试",
            description="Feedling iOS 在 NYU beta 中测试。",
        ),
        _moment(
            id="live_activity",
            title="灵动岛 start update 策略",
            description="push to start 可以拉起灵动岛，update 用于更新现有 Live Activity。",
        ),
        _moment(
            id="image_api",
            title="API 图片上传失败",
            description="API hosted route 之前不支持图片消息，发送图片会失败。",
        ),
        {
            **_moment(
                id="correction_global",
                title="用户更新称呼边界",
                description="以后不要再叫用户老师。",
                source="model_api_correction",
            ),
        },
        {
            **_moment(
                id="correction_lemon",
                title="柠檬茶纠正",
                description="用户纠正过柠檬茶不是冰的。",
                source="model_api_correction",
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Bigram utilities
# ---------------------------------------------------------------------------

def test_bigrams_empty():
    assert _char_bigrams("") == set()
    assert _char_bigrams("a") == set()  # single char → no bigrams


def test_bigrams_basic():
    assert _char_bigrams("abc") == {"ab", "bc"}


def test_bigrams_chinese():
    # Bigrams over Chinese characters work without tokenization.
    assert _char_bigrams("你好吗") == {"你好", "好吗"}


def test_bigrams_lowercased():
    assert _char_bigrams("ABC") == _char_bigrams("abc")


def test_jaccard_identical():
    g = _char_bigrams("hello world")
    assert _bigram_jaccard(g, g) == 1.0


def test_jaccard_disjoint():
    a = _char_bigrams("abc")
    b = _char_bigrams("xyz")
    assert _bigram_jaccard(a, b) == 0.0


def test_jaccard_partial():
    a = _char_bigrams("abcd")  # {ab, bc, cd}
    b = _char_bigrams("bcde")  # {bc, cd, de}
    # intersection = {bc, cd}, union = {ab, bc, cd, de} → 2/4 = 0.5
    assert _bigram_jaccard(a, b) == 0.5


def test_jaccard_empty_inputs():
    assert _bigram_jaccard(set(), set()) == 0.0
    assert _bigram_jaccard(set(), {"ab"}) == 0.0


# ---------------------------------------------------------------------------
# Selection logic
# ---------------------------------------------------------------------------

def test_select_empty_garden():
    assert _select_context_memories([], "anything") == []
    assert _select_context_memories([], "") == []


def test_select_returns_turning_points_first():
    moments = [
        _moment(id="r1", title="random card 1"),
        _moment(id="t1", title="转折｜你第一次说出真心话",
                occurred_at="2026-03-01T00:00:00"),
        _moment(id="t2", title="转折｜重要的一天",
                occurred_at="2026-04-01T00:00:00"),
    ]
    out = _select_context_memories(moments, "")
    out_ids = [m["id"] for m in out]
    # Both turning points present, newest first
    assert out_ids[:2] == ["t2", "t1"]


def test_select_caps_at_8():
    moments = [_moment(id=f"m{i}", title=f"card {i}") for i in range(20)]
    out = _select_context_memories(moments, "")
    assert len(out) <= 8


def test_select_dedupes_across_buckets():
    # A single card that is both a turning point AND most-recently created
    # should appear once, not twice.
    only = _moment(
        id="only",
        title="转折｜the single card",
        created_at="2027-12-31T00:00:00",  # most recent ever
        occurred_at="2026-01-01T00:00:00",
    )
    out = _select_context_memories([only], "")
    assert [m["id"] for m in out] == ["only"]


def test_select_relevance_uses_user_text():
    # Card whose title overlaps with user query should rank into top picks.
    moments = [
        _moment(id="a", title="random one"),
        _moment(id="b", title="random two"),
        _moment(id="c", title="深夜你说想换工作", description="那次咖啡馆里"),
        _moment(id="d", title="random three"),
        _moment(id="e", title="random four"),
        _moment(id="f", title="random five"),
    ]
    # User just said something about 换工作 — card c should surface
    out = _select_context_memories(moments, "我最近又在想换工作的事")
    assert "c" in [m["id"] for m in out]


def test_select_no_user_text_skips_relevance():
    # When latest_user_text is empty, relevance bucket contributes nothing,
    # output is just turning + recent.
    moments = [
        _moment(id="r1", title="card one",   created_at="2026-01-10T00:00:00"),
        _moment(id="r2", title="card two",   created_at="2026-01-09T00:00:00"),
        _moment(id="r3", title="card three", created_at="2026-01-08T00:00:00"),
    ]
    out = _select_context_memories(moments, "")
    # 0 turning + max 2 recent
    assert len(out) == 2
    assert [m["id"] for m in out] == ["r1", "r2"]


def test_select_orders_recent_by_created_at_desc():
    moments = [
        _moment(id="old",    title="o", created_at="2026-01-01T00:00:00"),
        _moment(id="newer",  title="n", created_at="2026-06-01T00:00:00"),
        _moment(id="newest", title="x", created_at="2026-12-01T00:00:00"),
    ]
    out = _select_context_memories(moments, "")
    # max 2 recent, newest first
    assert [m["id"] for m in out[:2]] == ["newest", "newer"]


def test_select_recent_compares_created_at_instants_not_strings():
    moments = [
        _moment(
            id="older_offset",
            title="older",
            created_at="2026-08-13T20:00:00+08:00",
        ),
        _moment(
            id="newer_z",
            title="newer",
            created_at="2026-08-13T13:00:00Z",
        ),
        _moment(id="bad", title="bad", created_at="garbage"),
    ]

    out = _select_context_memories(moments, "")

    assert [m["id"] for m in out] == ["newer_z", "older_offset"]


def test_select_handles_500_cards_under_budget():
    # Stress: 500 cards, request still completes quickly. Time budget
    # is generous (this asserts < 1 second; in practice << 100 ms).
    import time
    moments = [
        _moment(
            id=f"m{i}",
            title=f"transient title {i}",
            description=f"some description text number {i} " * 5,
            created_at=f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00",
        )
        for i in range(500)
    ]
    # Sprinkle a few turning points
    moments[100]["title"] = "转折｜special one"
    moments[300]["title"] = "转折｜special two"

    start = time.time()
    out = _select_context_memories(
        moments,
        "looking for something specific in description text",
    )
    elapsed = time.time() - start

    assert len(out) <= 8
    assert elapsed < 1.0, f"selection took {elapsed:.2f}s on 500 cards (budget 1.0s)"


def test_select_caps_turning_points_at_3():
    # 5 turning points should yield only the 3 newest in the turning bucket.
    moments = [
        _moment(id=f"t{i}", title=f"转折｜card {i}",
                occurred_at=f"2026-{i + 1:02d}-01T00:00:00")
        for i in range(5)
    ]
    out = _select_context_memories(moments, "")
    # 2026-08-17 边界整理：内核拿到的是翻译产物，没有 title —— 角色改看 roles。
    # 「靠标题前缀认转折卡」的识别逻辑已收到宿主侧（card_shape.roles_of）。
    turning_ids = [m["id"] for m in out if "turning_point" in (m.get("roles") or [])]
    # Top 3 by occurred_at desc should be t4, t3, t2
    assert turning_ids[:3] == ["t4", "t3", "t2"]


def test_turning_points_compare_instants_and_put_garbage_last():
    moments = [
        _moment(id="bad", title="转折｜bad", occurred_at="garbage"),
        _moment(
            id="older_offset",
            title="转折｜older",
            occurred_at="2026-08-13T20:00:00+08:00",
        ),
        _moment(id="newer_z", title="转折｜newer", occurred_at="2026-08-13T13:00:00Z"),
        _moment(id="date_only", title="转折｜date", occurred_at="2026-08-13"),
    ]

    out = _select_context_memories(moments, "")
    # 2026-08-17 边界整理：内核拿到的是翻译产物，没有 title —— 角色改看 roles。
    # 「靠标题前缀认转折卡」的识别逻辑已收到宿主侧（card_shape.roles_of）。
    turning_ids = [m["id"] for m in out if "turning_point" in (m.get("roles") or [])]

    assert turning_ids[:3] == ["newer_z", "older_offset", "date_only"]


def test_model_api_mode_skips_unrelated_recent_cards_and_keeps_corrections():
    moments = [
        _moment(
            id="food",
            title="烧卖和蒸饺",
            description="用户曾经聊过烧卖和蒸饺。",
            created_at="2026-06-05T12:00:00",
        ),
        _moment(
            id="drink",
            title="喜欢柠檬茶",
            description="用户晚上想喝清爽一点的饮料。",
            created_at="2026-06-01T12:00:00",
        ),
        {
            **_moment(
                id="correction",
                title="用户更新了 AI 设定",
                description="以后不要再使用烂梗王设定。",
                created_at="2026-06-04T12:00:00",
                source="model_api_correction",
            ),
        },
    ]

    out = _select_context_memories(
        moments,
        "晚上一起喝点什么？",
        mode="model_api",
    )

    out_ids = [m["id"] for m in out]
    assert "correction" in out_ids
    assert "drink" in out_ids
    assert "food" not in out_ids


def test_model_api_generic_project_does_not_recall_toho_project():
    moments = [
        _moment(
            id="toho",
            title="TOHO Project 老二次元偏好",
            description="用户是 toho project（东方Project）老二次元，喜欢东方 Project 相关内容。",
        ),
        _moment(
            id="pressure",
            title="近期项目压力",
            description="用户明天有一个项目要完成，觉得很累。",
        ),
    ]

    out, trace = _select_context_memories_with_trace(
        moments,
        "明天有一个project要完成，好累",
        mode="model_api",
    )

    out_ids = [m["id"] for m in out]
    assert "toho" not in out_ids
    assert "pressure" in out_ids
    rejected = {item["id"]: item for item in trace["rejected_sample"]}
    assert rejected["toho"]["reason"] == "weak_generic_overlap"


def test_model_api_entity_phrase_recalls_toho_project():
    moments = [
        _moment(
            id="toho",
            title="TOHO Project 老二次元偏好",
            description="用户是 toho project（东方Project）老二次元，喜欢东方 Project 相关内容。",
        ),
        _moment(id="work", title="项目压力", description="用户最近工作项目很多。"),
    ]

    out, trace = _select_context_memories_with_trace(
        moments,
        "我又在听东方Project的曲子",
        mode="model_api",
    )

    assert [m["id"] for m in out] == ["toho"]
    selected = trace["selected"][0]
    assert selected["confidence"] == "strong"
    assert selected["reason"] in {"entity_phrase_match", "phrase_match"}


def test_model_api_phrase_recalls_chicken_memory():
    moments = [
        _moment(
            id="chickens",
            title="你第一次告诉我你养了两只鸡当宠物",
            description="养了两只科钦球母鸡做宠物叫烧卖和蒸饺。",
        ),
        _moment(id="drink", title="喜欢柠檬茶", description="用户晚上想喝清爽一点的饮料。"),
    ]

    out = _select_context_memories(
        moments,
        "烧卖和蒸饺是谁？",
        mode="model_api",
    )

    assert [m["id"] for m in out] == ["chickens"]


def test_model_api_weak_single_char_does_not_recall_preference_card():
    moments = [
        _moment(
            id="preference",
            title="东方 Project 偏好",
            description="用户喜欢东方 Project 相关内容。",
        ),
    ]

    out, trace = _select_context_memories_with_trace(
        moments,
        "好累",
        mode="model_api",
    )

    assert out == []
    assert trace["rejected_sample"][0]["reason"] == "weak_generic_overlap"


def test_model_api_trace_includes_selection_metadata():
    moments = [
        _moment(
            id="chickens",
            title="烧卖和蒸饺",
            description="用户养的两只科钦球母鸡叫烧卖和蒸饺。",
        )
    ]

    out, trace = _select_context_memories_with_trace(
        moments,
        "你还记得烧卖和蒸饺吗？",
        mode="model_api",
    )

    assert out[0]["selection"]["bucket"] == "query"
    assert out[0]["selection"]["confidence"] == "strong"
    assert trace["selected"][0]["id"] == "chickens"
    assert any("烧卖" in unit or "蒸饺" in unit for unit in trace["selected"][0]["matched_units"])


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("我又在听东方Project的曲子", "toho"),
        ("toho project 的设定还在吗", "toho"),
        ("东方 project 老二次元这张卡还记得吗", "toho"),
        ("烧卖和蒸饺是谁？", "chickens"),
        ("晚上想喝点清爽的", "lemon"),
        ("我想喝柠檬茶", "lemon"),
        ("我们不看看更适合LLM的硅基A03?", "a03"),
        ("Claude Code 上下文断了", "claude_vps"),
        ("VPS 上跑 Claude Code 的用户怎么处理", "claude_vps"),
        ("你还记得换工作的事吗", "job_change"),
        ("NYU beta 又要测了吗", "nyu"),
        ("灵动岛 start update 策略怎么走", "live_activity"),
        ("API onboarding 图片上传失败", "image_api"),
        ("raw JSON 原始输出又出现了", "raw_json"),
        ("不要再叫我老师", "correction_global"),
        ("柠檬茶不是冰的", "correction_lemon"),
    ],
)
def test_model_api_entity_and_phrase_queries_recall_expected_cards(query, expected_id):
    out, trace = _select_context_memories_with_trace(
        _retrieval_fixture_moments(),
        query,
        mode="model_api",
    )

    out_ids = [m["id"] for m in out]
    assert expected_id in out_ids
    selected = {item["id"]: item for item in trace["selected"]}
    assert selected[expected_id]["confidence"] in {"strong", "medium"}
    assert selected[expected_id]["reason"] != "weak_generic_overlap"


@pytest.mark.parametrize(
    ("query", "forbidden_id"),
    [
        ("project deadline", "toho"),
        ("明天有一个project要完成，好累", "toho"),
        ("好累", "toho"),
        ("好累", "pressure"),
        ("api key 过期了吗", "image_api"),
        ("今天测试一下", "image_api"),
        ("你怎么看这个模型", "a03"),
        ("chat 页面卡住了", "claude_vps"),
        ("代码 prompt 怎么改", "toho"),
        ("我想完成任务", "pressure"),
        ("喝水", "lemon"),
        ("照片在哪里", "image_api"),
    ],
)
def test_model_api_generic_queries_do_not_recall_false_positive_cards(query, forbidden_id):
    moments = [
        m for m in _retrieval_fixture_moments()
        if not str(m.get("source") or "").endswith("correction")
    ]

    out, trace = _select_context_memories_with_trace(
        moments,
        query,
        mode="model_api",
    )

    out_ids = [m["id"] for m in out]
    assert forbidden_id not in out_ids
    rejected = {item["id"]: item for item in trace["rejected_sample"]}
    if forbidden_id in rejected:
        assert rejected[forbidden_id]["confidence"] == "weak"


def test_model_api_only_project_word_rejects_toho_even_when_toho_is_newest():
    moments = [
        _moment(
            id="pressure",
            title="近期项目压力",
            description="用户明天有一个项目要完成，觉得很累。",
            occurred_at="2026-01-01T00:00:00",
        ),
        _moment(
            id="toho",
            title="TOHO Project 老二次元偏好",
            description="用户是 toho project（东方Project）老二次元。",
            occurred_at="2026-12-31T00:00:00",
        ),
    ]

    out, trace = _select_context_memories_with_trace(moments, "project", mode="model_api")

    assert [m["id"] for m in out] == []
    assert {item["id"] for item in trace["rejected_sample"]} == {"toho"}


def test_model_api_mixed_alias_handles_spaces_between_chinese_and_english():
    moments = [
        _moment(
            id="toho",
            title="TOHO Project 老二次元偏好",
            description="用户喜欢东方 Project 和上海爱丽丝幻乐团相关内容。",
        )
    ]

    out = _select_context_memories(
        moments,
        "东方 Project 的曲子",
        mode="model_api",
    )

    assert [m["id"] for m in out] == ["toho"]


def test_model_api_keeps_at_most_two_global_corrections():
    moments = [
        {
            **_moment(id=f"corr{i}", title=f"边界更新 {i}", description=f"以后不要再使用称呼 {i}。", source="model_api_correction"),
        }
        for i in range(4)
    ]

    out = _select_context_memories(
        moments,
        "今天聊点别的",
        mode="model_api",
    )

    assert len(out) == 2
    assert all(m["id"].startswith("corr") for m in out)


def test_model_api_correction_tuple_compares_created_at_instants():
    moments = [
        {
            **_moment(
                id="bad",
                title="称呼边界",
                description="以后不要再使用旧称呼。",
                created_at="garbage",
                source="model_api_correction",
            ),
        },
        {
            **_moment(
                id="older_offset",
                title="称呼边界",
                description="以后不要再使用旧称呼。",
                created_at="2026-08-13T20:00:00+08:00",
                source="model_api_correction",
            ),
        },
        {
            **_moment(
                id="newer_z",
                title="称呼边界",
                description="以后不要再使用旧称呼。",
                created_at="2026-08-13T13:00:00Z",
                source="model_api_correction",
            ),
        },
    ]

    out = _select_context_memories(moments, "今天聊点别的", mode="model_api")

    assert [m["id"] for m in out] == ["newer_z", "older_offset"]


def test_model_api_keeps_at_most_three_query_relevant_cards():
    moments = [
        _moment(id=f"tea{i}", title=f"柠檬茶记忆 {i}", description=f"用户第 {i} 次提到柠檬茶。")
        for i in range(6)
    ]

    out = _select_context_memories(
        moments,
        "柠檬茶还记得吗",
        mode="model_api",
    )

    assert len(out) == 3


def test_model_api_query_tuple_compares_occurred_at_instants():
    moments = [
        _moment(
            id="bad",
            title="柠檬茶",
            description="用户喜欢柠檬茶。",
            occurred_at="garbage",
        ),
        _moment(
            id="older_offset",
            title="柠檬茶",
            description="用户喜欢柠檬茶。",
            occurred_at="2026-08-13T20:00:00+08:00",
        ),
        _moment(
            id="newer_z",
            title="柠檬茶",
            description="用户喜欢柠檬茶。",
            occurred_at="2026-08-13T13:00:00Z",
        ),
        _moment(
            id="date_only",
            title="柠檬茶",
            description="用户喜欢柠檬茶。",
            occurred_at="2026-08-13",
        ),
    ]

    out = _select_context_memories(moments, "柠檬茶", mode="model_api")

    assert [m["id"] for m in out] == ["newer_z", "older_offset", "date_only"]


def test_model_api_archived_cards_are_excluded_even_when_strong_match():
    moments = [
        {
            **_moment(
                id="archived_toho",
                title="TOHO Project 老二次元偏好",
                description="用户喜欢东方Project。",
            ),
            "is_archived": True,
        }
    ]

    out, trace = _select_context_memories_with_trace(
        moments,
        "东方Project",
        mode="model_api",
    )

    assert out == []
    assert trace["selected"] == []


def test_public_selector_strips_selection_metadata():
    out = _select_context_memories(
        _retrieval_fixture_moments(),
        "烧卖和蒸饺是谁？",
        mode="model_api",
    )

    assert out
    assert "selection" not in out[0]


def test_trace_keeps_rejected_sample_out_of_selected_cards():
    out, trace = _select_context_memories_with_trace(
        _retrieval_fixture_moments(),
        "明天有一个project要完成，好累",
        mode="model_api",
    )

    selected_ids = {m["id"] for m in out}
    rejected_ids = {item["id"] for item in trace["rejected_sample"]}
    assert "pressure" in selected_ids
    assert "toho" not in selected_ids
    assert "toho" in rejected_ids


def test_model_api_trace_records_query_units_for_debugging():
    _out, trace = _select_context_memories_with_trace(
        _retrieval_fixture_moments(),
        "明天有一个project要完成，好累",
        mode="model_api",
    )

    assert "project" in trace["query_units"]
    assert "project" in trace["query_weak_terms"]
    assert "明天" in trace["query_weak_terms"]


# ---------------------------------------------------------------------------
# Soft-recall index (P3 / D3): strict (model_api) mode attaches a compact
# `index_sample` of more cards by title/date so the hosted model can recall a
# card even when it didn't lexically match. Default mode does not attach it.
# ---------------------------------------------------------------------------

def _index_card(cid, title, occurred_at, mtype="fact"):
    return {
        "id": cid,
        "type": mtype,
        "title": title,
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "description": "",
    }


def test_strict_index_sample_excludes_selected_and_lists_rest():
    moments = [
        _index_card("a", "用户养了一只叫 Mochi 的猫", "2026-01-01"),
        _index_card("b", "转折｜第一次一起熬夜赶 deadline", "2026-02-01", "moment"),
        _index_card("c", "用户搬到了东京", "2026-03-01", "event"),
    ]
    # Query shares nothing lexically with any card → strict selects nothing,
    # so all three fall to the index.
    _selected, trace = _select_context_memories_with_trace(
        moments, "zzqqplugh", mode="model_api"
    )
    index = trace["index_sample"]
    selected_ids = {item["id"] for item in trace["selected"]}
    index_ids = {item["id"] for item in index}
    assert selected_ids & index_ids == set()  # disjoint from selected
    assert index_ids == {"a", "b", "c"}
    for item in index:  # compact shape only — no description/body
        assert set(item.keys()) == {"id", "type", "title", "occurred_at"}
    assert index[0]["id"] == "b"  # turning point sorts to the front


def test_strict_index_tuple_compares_occurred_at_instants_and_garbage_last():
    moments = [
        _index_card("bad", "unrelated bad", "garbage"),
        _index_card("older_offset", "unrelated older", "2026-08-13T20:00:00+08:00"),
        _index_card("newer_z", "unrelated newer", "2026-08-13T13:00:00Z"),
        _index_card("date_only", "unrelated date", "2026-08-13"),
    ]

    _selected, trace = _select_context_memories_with_trace(
        moments,
        "zzqqplugh",
        mode="model_api",
    )

    assert [item["id"] for item in trace["index_sample"]] == [
        "newer_z",
        "older_offset",
        "date_only",
        "bad",
    ]


def test_strict_index_sample_capped_at_20():
    moments = [
        _index_card(f"m{i}", f"卡片 {i}", f"2026-01-{(i % 27) + 1:02d}")
        for i in range(25)
    ]
    _selected, trace = _select_context_memories_with_trace(
        moments, "zzqqplugh", mode="model_api"
    )
    assert len(trace["index_sample"]) == 20


def test_default_mode_has_no_index_sample():
    moments = [_index_card("a", "用户养了一只叫 Mochi 的猫", "2026-01-01")]
    _selected, trace = _select_context_memories_with_trace(moments, "猫", mode="default")
    assert "index_sample" not in trace
