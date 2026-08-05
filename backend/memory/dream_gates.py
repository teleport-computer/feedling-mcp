"""Dream 出口的确定性硬闸 —— 只判「明显不对」,绝不判内容质量。

背景(2026-08-05,usr_a40e 墓碑卡事故):deepseek-v4-pro 把 supersede 语义理解反了,
在 result.summary/content 里写「已被 <卡id> 取代——原文」的记账注记,三道旧防线
(占位符闸/语义审查员/15% 增量栅栏)全没拦住。复盘定的产品哲学:

  - **拆掉所有内容质量判断**(15% 增量栅栏、语义审查员)——它们在拒绝内容上的
    可能性,且弱模型自审自查已被证明既误放也误杀,还多烧一次 BYOK 调用。
  - **硬约束放在出口的确定性代码里**,对模型不可见、零认知负担;不往 prompt 里
    堆散文。本模块只保留两类判据:
      1. 卡 id 泄漏:用户可见字段里出现花园里真实存在的卡 id —— 永远是协议泄漏,
         零误伤(与之相对,裸判「已被…取代」短语会误伤正常散文)。
      2. 爆炸半径保险丝:单次 dream 要退休的卡超过花园的绝大部分 —— 判「规模
         明显不对」,不看任何一条提案写了什么。834→1 事故的最后一道防线。

与 card_text.py 同风格:纯函数、零 I/O、只依赖 stdlib,resident(V1 consumer)与
V2 extraction/worker 两条运行时共用一份判据 —— 不再像旧栅栏那样两处复制。
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# 1) 卡 id 泄漏
# ---------------------------------------------------------------------------

# 太短的 id 当子串匹配会误伤(比如 4 位 hex 撞上普通英文),真实卡 id 远长于此。
_MIN_ID_LEN = 8


def known_id_in_text(text: str, known_ids) -> str:
    """``text`` 中出现的第一个真实卡 id;没有则返回空串。

    精确子串匹配 —— ``known_ids`` 必须来自当前花园(喂进 prompt 的那份 card_map),
    所以命中即泄漏,不存在「用户恰好聊到一串一样的 hex」的误伤空间。
    """
    s = str(text or "")
    if not s:
        return ""
    for known_id in known_ids or ():
        candidate = str(known_id or "").strip()
        if len(candidate) >= _MIN_ID_LEN and candidate in s:
            return candidate
    return ""


def result_id_leak(*, summary: str, content: str, known_ids) -> str | None:
    """result 硬字段的卡 id 泄漏体检。返回 ``None``=干净,否则 ``"<字段>_contains_card_id"``。

    这些字段用户会亲眼看到;卡 id 属于系统记账信息,出现即「语义写反」的强证据
    (模型在描述整理动作本身,而不是写整理后的内容)。
    """
    if known_id_in_text(summary, known_ids):
        return "summary_contains_card_id"
    if known_id_in_text(content, known_ids):
        return "content_contains_card_id"
    return None


# ---------------------------------------------------------------------------
# 2) 爆炸半径保险丝
# ---------------------------------------------------------------------------

def _fuse_ratio() -> float:
    try:
        value = float(os.environ.get("FEEDLING_DREAM_FUSE_RATIO", "0.8"))
    except (TypeError, ValueError):
        return 0.8
    return value if 0.0 < value <= 1.0 else 0.8


def _fuse_min_cards() -> int:
    try:
        value = int(os.environ.get("FEEDLING_DREAM_FUSE_MIN_CARDS", "10"))
    except (TypeError, ValueError):
        return 10
    return value if value > 0 else 10


def blast_radius_exceeded(retiring_count: int, active_count: int) -> bool:
    """单次 dream 要退休 ``retiring_count`` 张(花园现有 ``active_count`` 张)是否熔断。

    默认:退休数 > 活跃卡的 80% **且** ≥10 张。两个条件缺一不可 ——
    小花园(比如 5 张卡里整理 4 张)是正常整理,永不熔断;只有「一晚上几乎
    重写整个花园」这种规模才明显不对。熔断 = 整个 job 失败等人查,不部分执行:
    部分执行会把「哪些做了哪些没做」变成排查噩梦。

    env 可调:``FEEDLING_DREAM_FUSE_RATIO``(默认 0.8)、
    ``FEEDLING_DREAM_FUSE_MIN_CARDS``(默认 10)。
    """
    retiring = max(0, int(retiring_count or 0))
    active = max(0, int(active_count or 0))
    if retiring < _fuse_min_cards():
        return False
    if active <= 0:
        return False
    return retiring > active * _fuse_ratio()
