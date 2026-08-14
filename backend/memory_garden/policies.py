"""落卡的三个策略档位 —— 共用一套结构，尺子各不相同。

## 为什么必须分档

同一件事「把材料变成记忆卡」，IO 里现在有两套实现：日常聊天走
``prompts/capture.py``，历史导入走 ``genesis/prompts.py``。共用的只有桶指引和
写入口，中间最核心的「什么值得记」各写一份 —— 这就是要消掉的半拟合。

但**不能把三把尺子统一成一把**：

    统一成「少而厚」   → 用户手动整理的 100 条事实只落 2 张卡，他会炸
    统一成「宁多勿漏」 → 日常聊天每句废话都变成卡，记忆库几天撑爆

所以收进一处的是**结构**（卡长什么样、怎么归桶、怎么去重、怎么写入），
分档保留的是**尺子**。见 ``docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md`` 第二节。

## 尺子文字的来源（三段的成色不一样，别混）

  - ``conversation_capture`` —— **逐字**取自 ``prompts/capture.py`` 原先内联的
    「你在找什么」段（用脚本抽出来的，不是手抄）。它是唯一已接线的档位，
    默认调用的产出与重构前字节一致，由基线快照测试守着。

  - ``history_import`` / ``curated_archive`` —— ⚠️ **是摘录改写，不是逐字子串**。
    原文散在 ``genesis/prompts.py`` 的 ``FACT_MAP_PROMPT`` 与
    ``FACT_*_KEEP_ALL_SUFFIX`` 里，与 genesis 自己的输出 schema、防火墙段、
    身份卡指令缠在一起，没法整段照搬。
    （codex code_review 2026-08-14 指出原 docstring 把三段都说成「逐字」。）

    **这两档目前不影响任何行为** —— ``build_capture_prompt`` 收到它们会直接抛
    ``NotImplementedError``，因为那个模板的其余部分（动作偏好/日期/tags/输出
    schema）还没随档位变。批 7 接 genesis 时，必须回到 ``genesis/prompts.py``
    逐条核对原文，并为每个档位建立与旧 prompt 对照的 golden，才能放开。

## 现状

本模块把三把尺子收拢到一处、用测试钉死它们不能被抹平。真正让 genesis 改调内核，
是批 7 的事（会动 onboarding 流程，需拍板）。
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 尺子文字（逐字摘自现有实现，勿改措辞）
# --------------------------------------------------------------------------- #

_RUBRIC_CONVERSATION_CAPTURE = """你找的是「值得记住的事」，不是「把每句话归档」——完整聊天记录本来就存着，你不必复述它。
你要挑的是：以后会塑造你对 TA 的理解、或 TA 会希望你记得的东西。

倾向（不是硬规则，你来判断）：
· 优先记「事件」——有前因后果、有场景、或透出 TA 状态的
  （"那天他开了一整天会、心率飙高，我催他休息，他嫌烦，我们吵了一架"）。
· 孤立的信息点（"今天喝了拿铁"）通常不必单独成卡——除非它是 TA 明确在意的、
  或反复出现的偏好（"我只喝燕麦奶""他总点 Blue Bottle"），那它值得作为偏好记下。
· 尺子是："这件事三个月后还重要吗？会不会改变我对 TA 的理解？TA 会希望我记得吗？"
  ——不是"它够不够大"。

克制：
· 宁少勿多。这一段如果只留一到两件事，是哪一两件？强迫自己归纳，
  别把一次聊天里的每个点都拆成一张卡。
· 一次「开会 + 心率高 + 吵架」是一张厚卡（一件事），不是三张薄卡。
· 没有值得记的，就什么都不写。大多数闲聊不必落卡，这很正常。"""

_RUBRIC_HISTORY_IMPORT = """抽出值得长期留存的【事实】候选：关于用户和这段关系的 durable 事实。
闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽。"""

_RUBRIC_CURATED_ARCHIVE = """本块是用户【手动整理好的长期记忆档案】，不是聊天记录：其中每条陈述基本都是
用户特意要长期留存的事实。

尽量【完整保留】每一条事实候选，不要用"闲聊/一次性/不够 durable"去过滤——
除非是空行、标题或明显无意义的重复。宁多勿漏。

把候选里的事实【尽量都写成卡】，不要为了"少而精"丢弃条目。仍然按已有记忆去重、
仍然归好 bucket/threads，但不要因"不够重要"而跳过用户特意整理的条目。"""


# --------------------------------------------------------------------------- #
# 档位
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapturePolicy:
    """一个来源用哪把尺子、以及配套的几个硬参数。

    ``selection_rubric`` 直接进 prompt；其余字段是确定性参数，
    由调用方在组 prompt 与解析结果时使用。
    """

    name: str
    selection_rubric: str
    max_cards: int | None           # None = 不限张数
    prefer_merge: bool              # 并入优于新增
    keep_dates: bool                # 原样保留 occurred_at
    seed_threads_from_tags: bool    # 把源里的 tags 播种进 threads


CONVERSATION_CAPTURE = CapturePolicy(
    name="conversation_capture",
    selection_rubric=_RUBRIC_CONVERSATION_CAPTURE,
    max_cards=2,
    prefer_merge=True,
    keep_dates=False,
    seed_threads_from_tags=False,
)

HISTORY_IMPORT = CapturePolicy(
    name="history_import",
    selection_rubric=_RUBRIC_HISTORY_IMPORT,
    max_cards=None,
    prefer_merge=True,
    keep_dates=True,
    seed_threads_from_tags=False,
)

CURATED_ARCHIVE = CapturePolicy(
    name="curated_archive",
    selection_rubric=_RUBRIC_CURATED_ARCHIVE,
    max_cards=None,
    prefer_merge=False,     # 宁多勿漏：不为了合并而丢条目
    keep_dates=True,
    seed_threads_from_tags=True,
)

POLICIES: dict[str, CapturePolicy] = {
    p.name: p for p in (CONVERSATION_CAPTURE, HISTORY_IMPORT, CURATED_ARCHIVE)
}

DEFAULT_POLICY = CONVERSATION_CAPTURE


class UnknownPolicyError(ValueError):
    """显式传了一个不认识的档位名。"""


def get_policy(name: str | None) -> CapturePolicy:
    """按名字取档位。

    ``None`` / 空串 → 回落到日常聊天档：代表「旧调用方没传」，
    退回现行为是安全的，接线过程中漏传不会炸掉落卡路径。

    **非空的未知名 → 抛 ``UnknownPolicyError``**：那基本只会是拼写或配置错误，
    而静默回落的后果不对称 —— ``curated_archive`` 拼错一个字母就会悄悄切成
    「宁少勿多」，把用户手工整理的上百条事实压成一两张卡，而且没有任何信号。
    （codex code_review 2026-08-14 指出，原实现对两种情况一视同仁地回落。）
    """
    if name is None:
        return DEFAULT_POLICY
    key = str(name).strip()
    if not key:
        return DEFAULT_POLICY
    try:
        return POLICIES[key]
    except KeyError:
        raise UnknownPolicyError(
            f"未知的落卡档位 {key!r}；可用的是：{', '.join(sorted(POLICIES))}"
        ) from None
