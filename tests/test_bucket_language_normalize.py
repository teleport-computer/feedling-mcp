"""Deterministic bucket-language backstop (Bug: Chinese cards landing in English
common buckets like "Pets"). The model violates the bucket-language guidance ~1/3
of the time; this code backstop maps a wrong-language COMMON bucket back to the
card's own language via the fixed zh<->en pair map, at the single write chokepoint.
Pure + deterministic — no real model needed (which is exactly why unit tests can
finally cover this class of bug)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from memory_garden.prompts.buckets import normalize_bucket_language  # noqa: E402


def test_chinese_card_english_common_bucket_maps_to_chinese():
    # the reported bug: 用户十年前养过一只狗 landed in "Pets"
    assert normalize_bucket_language("Pets", "用户十年前养过一只狗") == "宠物"
    assert normalize_bucket_language("Work", "用户在做独立游戏") == "工作"
    assert normalize_bucket_language("Health", "用户有鼻炎") == "健康"


def test_english_card_chinese_common_bucket_maps_to_english():
    assert normalize_bucket_language("宠物", "user has a dog") == "Pets"
    assert normalize_bucket_language("健康", "user has rhinitis") == "Health"


def test_already_correct_language_is_unchanged():
    assert normalize_bucket_language("宠物", "用户养过一只狗") == "宠物"
    assert normalize_bucket_language("Pets", "user had a dog") == "Pets"


def test_custom_bucket_passes_through():
    # not in the common pair map — can't safely translate, leave it
    assert normalize_bucket_language("妈妈", "妈妈住在重庆") == "妈妈"
    assert normalize_bucket_language("the house", "we bought the house") == "the house"


def test_empty_bucket_is_untouched():
    assert normalize_bucket_language("", "用户养过狗") == ""
    assert normalize_bucket_language("  ", "text") == ""


def test_mixed_language_text_counts_as_chinese():
    # any CJK present => treat as a Chinese-garden card
    assert normalize_bucket_language("Pets", "用户养了 a dog 叫布丁") == "宠物"


def test_hook_applied_in_memory_inner_from_action():
    # the chokepoint every write path funnels through must apply the backstop
    from memory.actions import _memory_inner_from_action
    inner = _memory_inner_from_action(
        {"summary": "用户十年前养过一只狗", "bucket": "Pets", "type": "fact"}
    )
    assert inner["bucket"] == "宠物"
