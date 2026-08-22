"""io 的每条落卡路径都必须带着 io 自己的识别器。

## 为什么不是「检查有没有传参数」

内核的 ``signals`` 有默认值（通用集），所以**漏传不会报错，只会静默失去防护** ——
调用方以为有闸，实际 io 那几种残片一个都拦不住。这是最难发现的一类回归：没有异常、
没有红测试、线上要等到又一张卡的桶被填成 ``analysis to=functions.memory_write``
才会知道。

所以这里不查管道，**查行为**：把 2026-07-28 那次事故的原始残片喂进每条真实路径，
断言它被拒。漏传了这条就会红，改再多中间层也骗不过去。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from memory.card_leak_signals import IO_LEAK_SIGNALS  # noqa: E402
from memgarden.text import card_guard, card_text  # noqa: E402
from memgarden.text.leak_signals import GENERIC_SIGNALS  # noqa: E402

#: 2026-07-28 真实事故串的三个成分。整卡 JSON 合法、有实义字符，
#: 所以 payload 级拒绝和占位符检测都拦不住 —— 只有字段级判据能抓。
HARMONY_ROUTE = "analysis to=functions.memory_write"
HARMONY_TOKEN = "<|channel|>commentary"
TORN_TAIL_PLUS_ERROR = 'output error code: 400 …relationship"}]}'


# --------------------------------------------------------------------------- #
# 内核的默认值确实拦不住 io 的残片 —— 这正是必须传 IO_LEAK_SIGNALS 的理由
# --------------------------------------------------------------------------- #


def test_generic_signals_alone_do_not_catch_io_specific_leaks():
    """如果这条红了，说明有人把 io 专有指纹又塞回内核默认值里了。

    内核默认值只含与协议无关的判据。io 的 harmony token 和工具路由**应当**漏过去 ——
    它们是 io 的线格式，不该出现在一个通用内核里。
    """
    assert card_guard.field_pollution_reason(HARMONY_ROUTE, GENERIC_SIGNALS) is None
    assert card_guard.field_pollution_reason(HARMONY_TOKEN, GENERIC_SIGNALS) is None


def test_io_signals_do_catch_them():
    assert card_guard.field_pollution_reason(HARMONY_ROUTE, IO_LEAK_SIGNALS)
    assert card_guard.field_pollution_reason(HARMONY_TOKEN, IO_LEAK_SIGNALS)
    assert card_guard.hard_field_pollution_reason(HARMONY_ROUTE, IO_LEAK_SIGNALS)


# --------------------------------------------------------------------------- #
# 每条真实写入路径
# --------------------------------------------------------------------------- #


def test_v2_write_path_rejects_the_accident_string():
    """V2 的 memory_write 工具路径（worker 的翻译层）。"""
    from model_api_runtime.v2 import worker

    src = pathlib.Path(worker.__file__).read_text(encoding="utf-8")
    # 行为断言够不着这层（要跑起整个 worker），退而求其次：确认两个调用点都带了识别器。
    # 这两处一旦漏传，上面的语义测试抓不到，只有这里能抓。
    assert src.count("signals=IO_LEAK_SIGNALS") >= 2, (
        "V2 worker 的 card_text_rejection / sanitize_card_labels 少传了识别器"
    )


def test_kernel_text_gate_rejects_when_given_io_signals():
    """内核的硬字段闸 —— io 传自己的识别器时必须拒。"""
    assert card_text.card_text_rejection(
        summary=HARMONY_ROUTE, content="正常内容", signals=IO_LEAK_SIGNALS
    )
    assert card_text.card_text_rejection(
        summary="正常摘要", content=HARMONY_TOKEN, signals=IO_LEAK_SIGNALS
    )


def test_soft_fields_are_cleaned_not_rejected():
    """软字段（桶/线索）的定义就是「能就地修好」，脏了要清洗而不是丢整张卡。"""
    bucket, threads, reasons = card_text.sanitize_card_labels(
        bucket=HARMONY_ROUTE,
        threads=["工作", HARMONY_TOKEN],
        lang_text="老王换工作了",
        signals=IO_LEAK_SIGNALS,
    )
    assert bucket != HARMONY_ROUTE, "脏桶没被清掉"
    assert HARMONY_TOKEN not in threads, "脏线索没被清掉"
    assert "工作" in threads, "干净的线索被误杀了"
    assert reasons, "清洗了却没留下理由，线上查不到"


@pytest.mark.parametrize("clean", [
    "老王不吃辣，一吃就胃疼",
    "他在调 {\"cards\": []} 的解析",          # 开发者正常记忆：单个弱证据
    "preserve to=functions.x literally",     # 字面讨论工具名：单个弱证据
])
def test_normal_developer_memories_survive_the_hard_gate(clean):
    """误杀代价高（整卡丢弃），所以硬字段要两个弱证据才判脏。

    这三条各只命中一个弱证据，必须活下来 —— 用户里有开发者，这类内容是正常记忆。
    """
    assert card_guard.hard_field_pollution_reason(clean, IO_LEAK_SIGNALS) is None


# --------------------------------------------------------------------------- #
# 静态守卫：每一个调用点都必须带识别器
# --------------------------------------------------------------------------- #


def test_every_guard_call_site_passes_io_signals():
    """内核那几个闸函数的 signals 有默认值，**漏传不会报错，只会静默失去防护**。

    这条不是补充，是必需的：写这批时我用 grep 扫了一遍，自认为都接上了；
    改用 AST 扫描才发现漏了 5 处，其中 4 处在 V1 consumer 的 pre-seal 路径上 ——
    那条路当时跑的是通用集，io 自己的残片一个都拦不住，而且没有任何测试会红
    （管道通、参数有默认、行为测试也覆盖不到那条具体分支）。

    grep 抓不住的原因：调用可能跨行、可能是 `a.b.fn(...)` 形式、
    `hard_field_pollution_reason` 还会被 `field_pollution_reason` 的模式误匹配。
    只能按语法树逐个调用节点看。
    """
    import ast

    GUARDS = {
        "hard_field_pollution_reason", "field_pollution_reason",
        "bucket_pollution_reason", "card_text_rejection",
        "sanitize_card_labels", "parse_migrated_cards",
    }
    repo = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((repo / "backend").rglob("*.py")) + list((repo / "tools").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name in GUARDS and "IO_LEAK_SIGNALS" not in ast.unparse(node):
                offenders.append(f"{path.relative_to(repo)}:{node.lineno} {name}")
    assert not offenders, (
        "这些调用点漏了 IO_LEAK_SIGNALS —— 会静默退回通用集，io 的残片拦不住：\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# parser：两条 runtime 拿到的必须是绑了 io 识别器的那个
# --------------------------------------------------------------------------- #


def test_shell_parsers_are_wrapped_not_the_raw_kernel_ones():
    """两条 runtime 都从 memory.*_prompt_v1 取 parser，那层必须已经绑好识别器。

    **这条是 codex 2026-08-23 抓到的洞。** 内核 parser 内部会调文本闸，signals
    不传就退回通用集 —— io 的残片一个都拦不住，而且不报错。宿主侧扫自己的调用点
    发现不了，因为漏传发生在包里面。

    绑在壳这一层（而不是让每个 runtime 调用点自己传）是结构性保证：调用点会增加，
    漏一个就是一条无声失防的路。
    """
    import memgarden.prompts.capture as kernel_capture
    import memgarden.prompts.dream as kernel_dream
    from memory.capture_prompt_v1 import parse_capture_cards
    from memory.dream_prompt_v1 import parse_dream_consolidations

    assert parse_capture_cards is not kernel_capture.parse_capture_cards, (
        "壳没有包装 parse_capture_cards —— runtime 拿到的是裸内核版本"
    )
    assert parse_dream_consolidations is not kernel_dream.parse_dream_consolidations, (
        "壳没有包装 parse_dream_consolidations"
    )


ACCIDENT_CARD = (
    '{"cards":[{"action":"add","type":"fact",'
    '"summary":"' + HARMONY_ROUTE + '",'
    '"content":"正常正文内容写在这里，长度足够通过实义字数检查",'
    '"bucket":"工作","threads":["加班"],"importance":0.5,"pulse":0.3}]}'
)


def test_capture_parser_rejects_the_accident_string_end_to_end():
    """走 runtime 真正用的那个 parser —— 事故串必须在这里就被拒。

    拦不住的后果不是「多一张脏卡」：解析完立刻封加密信封，下游看不到明文，
    再也没有第二道闸。
    """
    from memory.capture_prompt_v1 import parse_capture_cards

    cards, err = parse_capture_cards(ACCIDENT_CARD, policy="conversation_capture",
                                     strict=False)
    assert not cards, f"事故串落成卡了：{cards}"
    assert err and "protocol_leak" in err, f"拒了但理由不对：{err}"


def test_clean_card_still_passes_the_same_parser():
    """闸要拦得住脏的，也要放得过干净的 —— 只测前者会掩盖误杀。"""
    from memory.capture_prompt_v1 import parse_capture_cards

    clean = (
        '{"cards":[{"action":"add","type":"fact","summary":"老王不吃辣",'
        '"content":"吃辣就胃疼，点菜时要避开辣菜，这是他反复提过的偏好",'
        '"bucket":"健康","threads":["饮食"],"importance":0.6,"pulse":0.3}]}'
    )
    cards, err = parse_capture_cards(clean, policy="conversation_capture", strict=False)
    assert len(cards) == 1 and err is None, f"干净的卡被误杀了：{err}"
