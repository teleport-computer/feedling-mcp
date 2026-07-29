"""Q3:干净但语言错的桶,在 capture/dream 路径上也归一到卡片语言。

背景:``normalize_bucket_language`` 原来只在明文 actions 路径跑;capture/dream/migrate/
history-import 提前封信封、绕过它,于是「中文卡被模型贴了英文 common 桶(Pets)」一直没归一。
本轮把归一收口进 ``sanitize_card_labels``(三条 parser 共用),用卡片正文判语言。

需要纯 stdlib(parser + card_text 都是纯函数),无 DB。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory.card_text import sanitize_card_labels  # noqa: E402
from memory.capture_prompt_v1 import parse_capture_cards  # noqa: E402


# --- 直接测 sanitize_card_labels(lang_text) --------------------------------

def test_chinese_card_english_common_bucket_normalized():
    bucket, _t, _r = sanitize_card_labels(
        bucket="Pets", threads=[], lang_text="用户十年前养过一只狗，很想念它"
    )
    assert bucket == "宠物"


def test_english_card_chinese_common_bucket_normalized():
    bucket, _t, _r = sanitize_card_labels(
        bucket="宠物", threads=[], lang_text="The user had a dog ten years ago"
    )
    assert bucket == "Pets"


def test_custom_bucket_not_touched():
    bucket, _t, _r = sanitize_card_labels(
        bucket="我和老王的恩怨", threads=[], lang_text="正文中文"
    )
    assert bucket == "我和老王的恩怨"


def test_no_lang_text_no_normalize():
    # 向后兼容:不传 lang_text 时不归一。
    bucket, _t, _r = sanitize_card_labels(bucket="Pets", threads=[])
    assert bucket == "Pets"


# --- 走真实 capture parser(端到端归一) ------------------------------------

def test_capture_lane_normalizes_bucket():
    cards, err = parse_capture_cards(
        '{"cards": [{"action": "add", "type": "fact",'
        ' "summary": "用户很喜欢自己的猫", "content": "上次视频里介绍了它",'
        ' "bucket": "Pets"}]}'
    )
    assert err is None, err
    assert len(cards) == 1
    assert cards[0]["bucket"] == "宠物"      # 中文卡 → 英文 common 桶被归一
