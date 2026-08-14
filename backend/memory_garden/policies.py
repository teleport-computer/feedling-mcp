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

## 尺子文字的来源

三段 rubric 全部**逐字摘自现有实现**，本模块不重写措辞 —— 本批的验收是
「行为逐字节不变」，改措辞等于改行为：

  - ``conversation_capture`` ← ``memory_garden/prompts/capture.py`` 的「你在找什么」段
  - ``history_import``       ← ``genesis/prompts.py`` 的 ``FACT_MAP_PROMPT`` 过滤句
  - ``curated_archive``      ← ``genesis/prompts.py`` 的 ``FACT_*_KEEP_ALL_SUFFIX``

## 现状

本模块只把三把尺子收拢到一处并用测试钉死差异。真正让 genesis 改调内核，
是批 7 的事（会动 onboarding 流程，需 hx 拍板）。
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


def get_policy(name: str | None) -> CapturePolicy:
    """按名字取档位；``None`` 或未知名回落到日常聊天档。

    回落而不是抛错，是为了让接线过程中任何一处漏传 policy 都退回现行为，
    不至于在生产上炸掉一条落卡路径。
    """
    if not name:
        return DEFAULT_POLICY
    return POLICIES.get(str(name).strip(), DEFAULT_POLICY)
