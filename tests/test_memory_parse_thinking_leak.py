"""线上事故复现:记忆解析器把泄漏的思维链当成 JSON 吃了。

usr_450ee421e16a3b5a(2026-08-09)报「AI 不往记忆花园存东西了」。真相不是 capture
没被调用 —— 它照常起、模型照常跑、用户的 token 照常花,**结果在解析这一步被扔掉**。
全 prod 扫描 328 个活跃用户:`json_decode_error` 56 次 / 11 个用户,是 capture 失败的
头号原因,受影响的清一色是中转条目或 `-thinking` 模型。

机制:中转把推理以 ``<think>…</think>`` 混在正文里,而各解析器的 JSON 提取器
**从第一个 ``{``** 开始扫平衡括号。capture 的 prompt 要求模型输出 JSON,所以
**模型的思维链里天然全是大括号** —— 提取器一头扎进思维链,抓到一段「平衡但非法」
的片段,`json.loads` 必炸。

判据是可区分的,下面一并钉住,免得以后有人把两类错误混成一类:

    输出被 max_tokens 截断  -> no_json_object
    纯散文、没有大括号      -> no_json_object
    思维链里有伪 JSON       -> json_decode_error   <- 线上就是这个
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory.capture_prompt_v1 import parse_capture_cards  # noqa: E402
from memory.dream_prompt_v1 import parse_dream_consolidations  # noqa: E402

_GOOD_CAPTURE = (
    '{"cards":[{"action":"add","type":"event","target_id":null,"bucket":"生活",'
    '"threads":["骑行"],"summary":"周末沿江骑行",'
    '"content":"他说周末沿江骑了三十公里。","importance":0.6,"pulse":0.3}]}'
)
_GOOD_DREAM = (
    '{"consolidations":[{"op":"merge","card_ids":["a","b"],'
    '"rationale":"同一次骑行的连续记录",'
    '"result":{"bucket":"生活","threads":["骑行"],"summary":"周末骑行习惯",'
    '"content":"他周末常沿江骑行三十公里。","importance":0.6,"pulse":0.3}}],'
    '"questions_to_ask":[]}'
)

# 模型在「我该输出什么 JSON」这件事上想出声 —— 这是 capture prompt 必然诱发的形状。
_THINK_WITH_PSEUDO_JSON = (
    "<think>用户提到骑行。我打算写一张 {summary: 周末骑行} 这样的卡,"
    "字段照 {\"cards\": [...]} 的结构来。</think>\n"
)
_THINK_WITHOUT_BRACES = "<think>用户提到骑行,值得记一张卡。</think>\n"


def test_capture_survives_thinking_that_talks_about_json():
    """线上那一条:思维链里出现伪 JSON,解析必须照常拿到卡。"""
    cards, err = parse_capture_cards(_THINK_WITH_PSEUDO_JSON + _GOOD_CAPTURE, strict=True)

    assert err is None, f"思维链把解析噎死了:{err}"
    assert len(cards) == 1
    assert cards[0]["summary"] == "周末沿江骑行"


def test_dream_survives_thinking_that_talks_about_json():
    """dream 用的是同一份提取器,同一个暴露面。"""
    consolidations, _questions, err = parse_dream_consolidations(
        _THINK_WITH_PSEUDO_JSON + _GOOD_DREAM
    )

    assert err is None, f"思维链把 dream 解析噎死了:{err}"
    assert len(consolidations) == 1


def test_thinking_without_braces_was_already_fine_and_stays_fine():
    """回归护栏:没有大括号的思维链今天就能过,修复不许把它弄坏。"""
    cards, err = parse_capture_cards(_THINK_WITHOUT_BRACES + _GOOD_CAPTURE, strict=True)

    assert err is None
    assert len(cards) == 1


@pytest.mark.parametrize(
    "raw, why",
    [
        (_GOOD_CAPTURE[:60], "被 max_tokens 砍断"),
        ("这次聊天没什么值得记的。", "纯散文,没有大括号"),
    ],
)
def test_unparseable_shapes_keep_reporting_no_json_object(raw, why):
    """两类错误必须保持可区分。

    `no_json_object` = 压根没拿到一个平衡的 JSON 对象(截断/散文);
    `json_decode_error` = 拿到了一个平衡对象但它不是合法 JSON(= 抓错了 span)。
    把它们混成一类,下次这种事故就再也无法从 admin 上一眼认出来。
    """
    _cards, err = parse_capture_cards(raw, strict=True)

    assert err == "no_json_object", f"{why}:期望 no_json_object,拿到 {err!r}"


def test_json_decode_error_is_reserved_for_grabbing_the_wrong_span():
    """反向钉死上面那条判据:只有「平衡但非法」才配叫 json_decode_error。"""
    _cards, err = parse_capture_cards("{不是合法 JSON 但括号是平衡的}", strict=True)

    assert err is not None and err.startswith("json_decode_error")


def test_leading_prose_before_the_json_still_parses():
    """既有的宽容度不许回退:模型爱在 JSON 前面说两句人话。"""
    cards, err = parse_capture_cards("好的,我整理了一张:\n" + _GOOD_CAPTURE, strict=True)

    assert err is None
    assert len(cards) == 1


def test_thinking_text_never_leaks_into_a_card():
    """剥离不能变成「把思维链当正文收下」。

    这是修这个 bug 时最容易走偏的一步:用 ``strip_tag_markers`` 之类只去掉尖括号、
    把里面的话留在正文里,解析确实不炸了,但模型的内心独白就顺着 prose 一路飘进
    花园。卡片字段里出现思维链原文,和「不落卡」是同一级别的产品事故。
    """
    thinking_words = ["我打算写一张", "字段照", "结构来"]
    cards, err = parse_capture_cards(_THINK_WITH_PSEUDO_JSON + _GOOD_CAPTURE, strict=True)

    assert err is None
    blob = " ".join(
        str(cards[0].get(field) or "")
        for field in ("summary", "content", "bucket")
    ) + " ".join(str(t) for t in (cards[0].get("threads") or []))
    for word in thinking_words:
        assert word not in blob, f"思维链原文漏进卡片了:{word!r}"


# 另一半的锁在别处,别重复造:前台「<think> 必须被提取出来展示给用户」由
# tests/test_chat_resident_consumer.py 的 test_self_thinking_on_prefers_tagged_over_native /
# test_agent_turn_splits_inline_think_tag_from_cli_text 钉着。两边合起来才是完整契约 ——
# **前台展示、后台丢弃**,谁想「统一一下」都会撞红其中一边。
