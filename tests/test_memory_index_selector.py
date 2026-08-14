from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from memory_garden.scoring.selector import select_memory_index_items  # noqa: E402


def _index_item(
    mid: str,
    summary: str,
    *,
    buckets: list[str] | None = None,
    bucket: str = "",
    threads: list[str] | None = None,
    salience: str = "medium",
    sensitive: bool = False,
    open_thread: bool = False,
    score: float = 0.5,
) -> dict:
    return {
        "id": mid,
        "summary": summary,
        "bucket_refs": buckets or [],
        "bucket": bucket,
        "threads": threads or [],
        "status": "active",
        "salience": salience,
        "is_open_thread": open_thread,
        "is_sensitive": sensitive,
        "score": score,
    }


def test_selector_picks_cat_index_items_from_safe_summaries():
    result = select_memory_index_items(
        "你还记得我喜欢什么样的猫咪吗？",
        [
            _index_item("cat_shape", "用户喜欢圆脸、黏人、安静一点的猫，尤其偏爱橘猫和布偶。", buckets=["宠物偏好", "猫咪"]),
            _index_item("cat_comfort", "用户把猫叫作喵喵，看到猫会明显放松。", buckets=["情绪安抚", "猫咪"], salience="high"),
            _index_item("work_style", "用户更喜欢先看流程和例子，再进入技术细节。", buckets=["协作方式"]),
        ],
        cap=3,
    )

    assert set(result["selected_ids"][:2]) == {"cat_shape", "cat_comfort"}
    selected = {item["id"]: item for item in result["trace"]["selected"]}
    assert selected["cat_comfort"]["confidence"] in {"strong", "medium"}
    assert "work_style" not in selected


def test_selector_allows_concrete_single_character_pet_terms():
    result = select_memory_index_items(
        "我有只猫吗？",
        [
            _index_item("cat_name", "用户的橘猫因为长得像老虎，所以给猫取名武松。"),
            _index_item("bike", "用户提到喜欢一辆叫速比特卡戎的自行车。"),
        ],
        cap=3,
    )

    assert result["selected_ids"] == ["cat_name"]


def test_selector_uses_v1_bucket_and_threads_as_topic_context():
    result = select_memory_index_items(
        "蛋子是什么狗？",
        [
            _index_item("dog", "用户养了一只比熊。", bucket="宠物", threads=["蛋子", "狗狗"]),
            _index_item("cat", "用户有只猫叫武松。", bucket="宠物", threads=["武松", "猫"]),
        ],
        cap=3,
    )

    assert result["selected_ids"] == ["dog"]


def test_selector_skips_sensitive_items_unless_query_allows_sensitive():
    items = [
        _index_item("comfort", "用户低落时需要先被陪伴，不要马上给建议。", buckets=["安抚方式"]),
        _index_item("intimacy", "用户在亲密语境里有特定安抚偏好。", buckets=["亲密边界"], sensitive=True, salience="high"),
    ]

    normal = select_memory_index_items("我今天有点低落，你怎么安慰我？", items)
    assert normal["selected_ids"] == ["comfort"]
    skipped = {item["id"]: item for item in normal["trace"]["skipped_sample"]}
    assert skipped["intimacy"]["reason"] == "sensitive_not_allowed_for_query"

    sensitive = select_memory_index_items("我想聊一下亲密关系里的安抚边界", items)
    assert "intimacy" in sensitive["selected_ids"]
    assert sensitive["trace"]["allow_sensitive"] is True


def test_selector_reuses_generic_term_filtering_for_index_items():
    result = select_memory_index_items(
        "明天有一个 project 要完成，好累",
        [
            _index_item("toho", "用户是 TOHO Project 老二次元，喜欢东方 Project 相关内容。", buckets=["二次元"]),
            _index_item("pressure", "用户明天有一个项目要完成，最近工作压力很大。", buckets=["工作压力"]),
        ],
    )

    assert result["selected_ids"] == ["pressure"]
    skipped = {item["id"]: item for item in result["trace"]["skipped_sample"]}
    assert "toho" in skipped


def test_selector_does_not_fetch_unrelated_cards_from_single_chinese_chars():
    result = select_memory_index_items(
        "猫咪最近不吃饭，我有点担心",
        [
            _index_item("lark", "用户希望 agent 帮忙读 Lark 群消息并整理重点。", buckets=["工作流", "Lark"]),
            _index_item("presence", "用户情绪崩溃时，先需要被陪着和确认感受。", buckets=["安抚方式"]),
        ],
    )

    assert result["selected_ids"] == []
    skipped = {item["id"]: item for item in result["trace"]["skipped_sample"]}
    assert "lark" in skipped
    assert "presence" in skipped
