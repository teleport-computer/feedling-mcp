"""io 的卡片形状翻译 —— **不属于 Garden 内核**。

2026-08-17 从 `memory_garden/card_fields.py` 搬回来。理由（hx 2026-08-17 拍板）：

    内核应该只认**一种**卡片形状。谁想接进来，谁负责翻译成那一种。
    让内核去适应每种宿主的字段名，等于把所有宿主的历史都背进内核。

io 有两种历史形状，是 io 自己的债：

    摘要式   summary / content            当前写入端产出
    标题式   title / description / …      存量老卡

所以翻译这件事归 io：`to_garden_card()` 把两种都归一成内核认的那一种，
再交给内核。内核那边只读 summary / content，不需要知道 title 是什么。

`FieldMap` 留在这里是为了让别的接入方（Notion / Obsidian）也能复用同一套
翻译机制 —— 它是**适配器层**的工具，不是内核的配置项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FieldMap:
    """一个记忆库的字段映射。不可变 —— 换库＝换实例，不是改字段。"""

    #: 取「一句话摘要」的优先级顺序，第一个非空者胜出。
    #: ⚠️ 只能放**可公开**的字段：这个值会进 selector trace，并可能返回客户端。
    summary_fields: tuple[str, ...]

    #: 老一代的匹配字段。拼接时**原样 join**（含空字段留下的空位），
    #: 以保证纯老卡的 haystack 逐字节不变。顺序即历史顺序，不要动。
    legacy_match_fields: tuple[str, ...]

    #: 新一代的匹配字段。按顺序取值、逐项 strip、丢弃空项后再拼。
    canonical_match_fields: tuple[str, ...]

    #: 参与匹配但**不可外泄**的正文字段（进 `_search_content`，不进 summary）。
    private_text_fields: tuple[str, ...] = field(default=())


#: io 自己的映射。接别的库时不要改这里，另建实例。
DEFAULT_FIELD_MAP = FieldMap(
    # content 刻意不在此列 —— 见模块头「硬约束 1」。
    summary_fields=("summary", "description", "title", "context"),
    legacy_match_fields=("title", "description", "her_quote", "context", "linked_dimension"),
    canonical_match_fields=("summary", "content"),
    private_text_fields=("content",),
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def summary_of(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """取可公开的一句话摘要。取不到就返回空串 —— **不用正文兜底**。

    返回空串是合法结果：只有正文、没有摘要的卡就该显示为空，
    它靠 `private_text(card)` 进候选池，而不是靠把正文伪装成摘要。
    """
    if not isinstance(card, dict):
        return ""
    for key in field_map.summary_fields:
        text = _clean(card.get(key))
        if text:
            return text
    return ""


def private_text(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """只在内部参与匹配、绝不可序列化的正文。"""
    if not isinstance(card, dict):
        return ""
    parts = [_clean(card.get(key)) for key in field_map.private_text_fields]
    return " ".join(part for part in parts if part)


def text_for_match(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> str:
    """拼出参与相关性匹配的全部文本。

    规则精确到空格（见模块头「硬约束 2」）：

        纯老卡（新字段全空）  → 老字段原样 join，**一个字符都不改**
        纯新卡（老字段全空）  → 只拼新字段，不继承老 join 留下的空位
        混合卡                → 老字段原样 join + 单个空格 + 新字段

    混合卡的分数会变 —— 老信息完整保留，只是多了新信息，这是预期的。
    """
    if not isinstance(card, dict):
        return ""

    # 逐字复刻旧实现：不过滤空字段，空字段照样占一个位置。
    legacy_text = " ".join(
        str(card.get(key) or "") for key in field_map.legacy_match_fields
    )
    canonical_parts = [
        text
        for text in (_clean(card.get(key)) for key in field_map.canonical_match_fields)
        if text
    ]

    if not canonical_parts:
        return legacy_text
    if not legacy_text.strip():
        return " ".join(canonical_parts)
    return legacy_text + " " + " ".join(canonical_parts)


def has_matchable_text(card: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> bool:
    """这张卡有没有任何可参与匹配的文本。

    ⚠️ 判据是 `text_for_match`，不是「摘要非空」—— 只有 her_quote 或
    linked_dimension 的卡同样能参与匹配，不该被入口过滤掉。
    """
    return bool(text_for_match(card, field_map).strip())


# --------------------------------------------------------------------------- #
# 翻译成内核认的形状 —— 这是新边界的核心
# --------------------------------------------------------------------------- #

#: 内核认的卡片形状。字段是**固定的**，内核不会去适应别的名字。
GARDEN_CARD_FIELDS = ("id", "summary", "content", "bucket", "threads",
                      "occurred_at", "created_at", "is_sensitive", "source")


def to_garden_card(raw: dict, field_map: FieldMap = DEFAULT_FIELD_MAP) -> dict:
    """把 io 的任意一种卡形状，翻成内核认的那一种。

    两种历史形状都能进：

        摘要式   {"summary": "...", "content": "..."}
        标题式   {"title": "...", "description": "...", "her_quote": "...",
                  "context": "...", "linked_dimension": "..."}

    翻完之后内核只读 `summary` / `content` —— 它不需要知道 `title`、
    `her_quote`、`linked_dimension` 是什么。

    ⚠️ **漏翻译的后果是静默的**：内核读不到 summary 就当这张卡没文本，
    整张丢出候选池 —— 这正是 2026-08-16 那个「问狗答不知道」的事故形状。
    所以所有喂给内核的入口都必须先过这里，有守卫测试钉着。
    """
    if not isinstance(raw, dict):
        return {}
    out = {
        "summary": summary_of(raw, field_map),
        "content": private_text(raw, field_map) or _clean(raw.get("content")),
        "bucket": _clean(raw.get("bucket")),
        "threads": [t for t in (raw.get("threads") or []) if str(t or "").strip()],
    }
    # 老形状里承载文本的另外三个字段，并进 content —— 否则「只有原话」的卡
    # 翻完会变成空卡（它们在老形状里是正文的一部分）。
    extras = [_clean(raw.get(k)) for k in ("her_quote", "context")]
    extras = [e for e in extras if e and e not in out["content"]]
    if extras:
        out["content"] = " ".join([out["content"], *extras]).strip()
    # 老形状的 linked_dimension 是线索的前身
    if not out["threads"]:
        linked = _clean(raw.get("linked_dimension"))
        if linked:
            out["threads"] = [linked]
    # 元数据原样带过（内核只读不写）
    for key in ("id", "occurred_at", "created_at", "is_sensitive", "source", "type"):
        if key in raw:
            out[key] = raw[key]
    # 角色：内核靠它挑「打底卡」，不再靠标题前缀（那对新形状的卡完全失效）
    roles = roles_of(raw)
    if roles:
        out["roles"] = roles
    # 🔴 搜索语料必须**显式给出**，不能让内核从展示字段去猜。
    #
    # 2026-08-17 踩过：只给 summary/content 时，老卡的 title / her_quote /
    # linked_dimension 全部退出匹配 —— 卡片标题「深夜你说想换工作」翻完只剩
    # 描述「那次咖啡馆里」，问「换工作」直接召不回来（codex 拍出，已实测复现）。
    # 与上周那个「读错字段」的事故是同一个形状：卡在库里，但想不起来。
    #
    # 这里用改造前那套逐字拼接，因此**分数逐字节不变**。
    out["search_text"] = text_for_match(raw, field_map)
    return out


# --------------------------------------------------------------------------- #
# 角色标记：把 io 的「靠标题前缀认」翻译成内核认的显式字段
# --------------------------------------------------------------------------- #

#: 内核认的角色。内核只按这个字段挑「打底卡」，不认任何标题前缀。
ROLE_TURNING_POINT = "turning_point"
ROLE_CORRECTION = "correction"

_TURNING_PREFIX = "转折｜"
_CORRECTION_MARKERS = ("correction", "纠正", "设定更新", "边界更新")
_CORRECTION_SOURCES = {"model_api_correction", "user_correction", "settings_correction"}


def roles_of(raw: dict) -> list[str]:
    """io 的卡带哪些角色。

    ⚠️ 现在靠标题前缀 / 来源名判断 —— 这个做法本来就脆弱
    （codex 2026-08-16 指出：展示文案兼任了协议字段），而且**对新形状的卡完全失效**，
    因为新卡没有 title。这里是过渡实现：把脆弱的判断收在 io 这一处，
    内核那边只认干净的 `roles` 字段。

    下一步是让写入端直接产出显式角色，然后这里的前缀分支可以删掉。
    """
    if not isinstance(raw, dict):
        return []
    roles: list[str] = []
    title = str(raw.get("title") or "")
    if title.startswith(_TURNING_PREFIX):
        roles.append(ROLE_TURNING_POINT)
    source = str(raw.get("source") or "").strip().lower()
    if source in _CORRECTION_SOURCES or any(m in title.lower() for m in _CORRECTION_MARKERS):
        roles.append(ROLE_CORRECTION)
    return roles


def is_retired(raw: dict) -> bool:
    """这张卡是不是已经退出流通（归档 / 被取代 / 删除）。

    **生命周期是宿主的规则**，不是内核的：内核拿到的候选集应该已经满足
    宿主的可见性与生命周期约束。此前这套判断写在 `relevance.py` 里，
    等于让内核认识 io 的 `is_archived` / `archived_at` / `archive_reason`
    三个字段（codex 2026-08-17 指出）。

    ⚠️ 必须在 `to_garden_card()` **之前**调用 —— 翻译产物里没有这些字段。
    """
    if not isinstance(raw, dict):
        return True
    return bool(
        raw.get("is_archived") is True
        or str(raw.get("archived_at") or "").strip()
        or str(raw.get("archive_reason") or "").strip()
        or str(raw.get("superseded_by") or "").strip()
    )
