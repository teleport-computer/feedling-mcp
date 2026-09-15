"""io 侧的 Garden 组件适配层 —— 两条 runtime 共用。

## 它解决什么

在这之前，「落一次卡」的**拼装说明书**散在 io 的 23 个文件里：
拼提示词 → 调模型 → 解析 → 过闸 → 归一化 → 吐脏了怎么重问。
Garden 内部改个函数名，io 就编译不过；换一套记忆系统，这些调用点全部作废。

现在 io 只调 ``GardenComponent`` 的方法，编排在包里。

## 边界没变

    组件负责   什么值得记 · 写成几张 · 归哪个桶 · 挑哪几张 · 该不该整理
    io  负责   调模型（key 在 io）· 加解密 · 写库 · 定时器 · 权限 · 溯源

所以组件返回的是**「该这么改」的指令和原始卡**，不是「已经写好了」。
io 拿到之后自己封信封、自己落库。

## 为什么要这一层，而不是各处直接 new 一个

三件事必须两条 runtime 一致，各写一份就会漂：

1. **泄漏信号**（``IO_LEAK_SIGNALS``）—— 漏传等于 io 自己的 harmony 残片
   一个都拦不住，而闸门看起来还在工作
2. **重问上限** —— V1 和 V2 给不同的次数，同一个模型在托管和自建上
   会得到不同的重问行为
3. **轨迹口径** —— 两条 lane 的观测字段必须能横向对比，
   否则「只有 V2 变差了」这种问题查不出来
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Iterable, Mapping

from memgarden import CaptureRequest, GardenComponent, MaintenanceRequest
from memgarden.contracts import Step

from identity.user_naming import _naming_rule, sanitize_user_name
from memory.capture_prompt_v1 import IO_CONVERSATION_CAPTURE_POLICY
from memory.card_leak_signals import IO_LEAK_SIGNALS

# io 的落卡档位（``IO_CONVERSATION_CAPTURE_POLICY``，max_cards=50）由调用点经
# ``CaptureRequest.policy`` 传给组件 —— 走公开入口，不改内核的模块属性。
# 以前这里有一处对 ``memgarden.component.build_capture_prompt`` 的 monkeypatch
# （0.16.0 按对象 identity 选模板，replace 出来的同档 policy 会被误认）；
# 0.20.1 起模板按 ``policy.name`` / 标志位渲染，垫片已删。
# tests/test_garden_io_capture_policy.py 守着「上限真生效 + 不许再打补丁」。

#: 打回重问最多一次。两条 runtime 共用 —— 各给各的次数，
#: 同一个模型在托管和自建上会得到不同的重问行为。
MAX_CAPTURE_RETRIES = 1


class CallableModel:
    """把 io 现成的「给我一段提示词、还我一段文本」包成组件要的模型端口。

    io 的两条 runtime 各有自己的调用方式（V1 是 ``call_agent``，
    V2 是 provider 客户端），但都能收敛成这一个函数签名。
    **key 始终在 io 手里，组件拿不到。**
    """

    def __init__(self, call: Callable[[str], str]) -> None:
        self._call = call

    def complete(self, prompt: str, *, purpose: str = "") -> str:
        return self._call(prompt)


class AsyncCallableModel:
    """异步版 —— V2 全程 async，同步阻塞会卡住事件循环。"""

    def __init__(self, call) -> None:
        self._call = call

    async def complete(self, prompt: str, *, purpose: str = "") -> str:
        return await self._call(prompt)


def build_garden(
    model: Any,
    *,
    selection_policy=None,
    on_step: Callable[[Step], None] | None = None,
) -> GardenComponent:
    """给 io 用的组件实例。

    ⚠️ ``signals=IO_LEAK_SIGNALS`` 是**必须的**。漏了它，io 自己的
    harmony 标记、工具路由残片、报错回显一个都拦不住 —— 而闸门看起来还在工作，
    脏卡照样落库。这类 bug 只有用户在记忆列表里看到乱码时才会暴露。
    """
    return GardenComponent(
        model=model,
        selection_policy=selection_policy,
        signals=IO_LEAK_SIGNALS,
        max_capture_retries=MAX_CAPTURE_RETRIES,
        on_step=on_step,
    )


# --------------------------------------------------------------------------- #
# Capture：两条 runtime 共用的落卡请求
# --------------------------------------------------------------------------- #
#
# 2026-08-30（fd963bf9）两条 runtime 换成组件之后，这里的请求是 V1 / V2 各拼一份，
# 两份都漏了同样的三样东西，线上跑了半个月：
#
#   1. **已有记忆索引** —— 提示词里是 ``(none)``，模型抄不到任何 target_id，
#      只能 add，同一件事说两次就是两张卡。（V2 08-03 起有过这份索引；
#      V1 从来没有。）
#   2. **io 的称呼规则** —— 没传 ``naming_rule``，用的是内核默认那版
#      （英文花园里不禁「TA」占位符，和 io 的转写标签对不上）。Dream 同样漏了，
#      而且当时内核的 ``MaintenanceRequest`` 根本没有这个字段，见 ``open_dream_session``。
#   3. **洗过的名字** —— V2 把身份卡里的原始 ``user_preferred_name`` 直接交出去，
#      存成「用户」的人会在提示词里被叫做「用户」，正是称呼规则禁止的词。
#
# 各拼一份就是漏的原因，所以请求只在这里拼。

#: 索引预算。**显式传**，理由同 Dream：组件默认值换了，这里不跟着悄悄变。
#: 60 张是 V2 08-03 那版索引的张数；字数上限防一张异常长的摘要把提示词撑爆。
CAPTURE_INDEX_CARDS_LIMIT = 60
CAPTURE_INDEX_BUDGET_CHARS = 16_000
CAPTURE_INDEX_SUMMARY_CHARS = 400


_CAPTURE_INDEX_FIELDS = (
    "existing_cards", "index_cards_limit", "index_budget_chars", "index_summary_chars",
)


def capture_kernel_selects_index() -> bool:
    """装的 memgarden 认不认 ``CaptureRequest.existing_cards``（组件挑索引 + 校验 target）。

    自建 VPS 的 consumer 自更新时先切代码、再 pip 装依赖（见 Dream 的同名判据）。
    和 Dream 不同，落卡在旧组件上**降级跑**而不是失败：旧组件就是这次修复之前的
    行为（无索引、只能 add），比整窗口不落卡好；观测上报 ``kernel_outdated``。
    """
    names = {field.name for field in dataclasses.fields(CaptureRequest)}
    return all(name in names for name in _CAPTURE_INDEX_FIELDS)


def capture_existing_cards(items: Iterable[Any]) -> list[dict]:
    """把读侧 memory index 的条目翻成组件认识的「现有卡」。

    只留组件用得到的字段（id / summary / bucket / importance）—— 索引里还有时间、
    分数这些，交出去没用。非 active 的卡不算现有卡：模型不该去覆盖一张已经
    被取代或归档的卡，组件会把指向它的 target_id 当成不存在的打回。

    没有摘要的卡仍然保留：它不会被渲染进索引，但它是真卡，模型从对话里拿到它的
    id 去覆盖时不该被判成编造。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        mid = _one_line(item.get("id"))
        status = _one_line(item.get("status")).lower() or "active"
        if not mid or mid in seen or status != "active":
            continue
        seen.add(mid)
        card: dict = {"id": mid, "summary": _one_line(_first_text(item, _SUMMARY_KEYS, one_line=True))}
        bucket = _one_line(item.get("bucket") or item.get("category"))
        if bucket:
            card["bucket"] = bucket
        if item.get("importance") is not None:
            card["importance"] = item.get("importance")
        out.append(card)
    return out


def capture_request(
    *,
    window: str,
    locale: str,
    buckets: str,
    threads: str,
    identity: str,
    ai_name: str,
    user_name: str,
    existing_cards: list[dict] | None,
) -> CaptureRequest:
    """V1 / V2 落卡（含 V1 的服务端打回重问）唯一的请求构造点。

    ``existing_cards``：``capture_existing_cards`` 的结果。``None`` = 这次没读到
    （读侧失败 / 读不全），组件退回「无索引、不校验 target」—— 不能拿空列表冒充，
    空列表的意思是「确认这个人一张卡都没有」，会让任何 supersede 都被判成编造。

    ``user_name`` 可以是原始值：这里洗名字、按**同一个原始值**生成称呼规则
    （``_naming_rule`` 内部也洗，两者一致）。
    """
    index_fields = (
        dict(
            existing_cards=existing_cards,
            index_cards_limit=CAPTURE_INDEX_CARDS_LIMIT,
            index_budget_chars=CAPTURE_INDEX_BUDGET_CHARS,
            index_summary_chars=CAPTURE_INDEX_SUMMARY_CHARS,
        )
        if capture_kernel_selects_index()
        else {}
    )
    return CaptureRequest(
        window=window,
        locale=locale,
        buckets=buckets,
        threads=threads,
        identity=identity,
        ai_name=_one_line(ai_name),
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name, locale=locale),
        policy=IO_CONVERSATION_CAPTURE_POLICY,
        **index_fields,
    )


#: 内核「这次不整理」的判据里，只有这一种代表「花园还太小，整理没活可干」。
#: 它来自 ``memgarden.dreaming.needs_dream``，阈值归内核（此处不复制数字）。
#:
#: ``no_memory_cards`` 刻意不在里面：调度器只在有卡时才排 Dream，worker 看到
#: 0 张卡几乎一定是**卡片读取失败/降级**，把它记成「卡太少、跳过」就是把读失败
#: 伪装成正常。它仍走原来的 noop 路径（带 degraded_context 告警）。
MAINTENANCE_SKIP_REASONS = frozenset({"not_enough_new_cards"})


def maintenance_skip_reason(session: Any) -> str:
    """整理会话若被内核判为「花园太小、不必整理」，返回那个 content-free 理由；否则 ""。

    只用会话的公开契约：被判不需要的会话 ``result()`` 返回 ``needed=False``
    和 ``trace["reason"]``，且没有副作用。调用方应当只在「一次模型都没问、
    也没有结果」时才来问 —— 那正是被判掉的会话的样子。
    """
    try:
        outcome = session.result()
    except Exception:  # noqa: BLE001 — 判不出来就按原路径走，不改变终态
        return ""
    if getattr(outcome, "needed", True) or getattr(outcome, "error", None):
        return ""
    trace = getattr(outcome, "trace", None)
    reason = str(trace.get("reason") or "") if isinstance(trace, dict) else ""
    return reason if reason in MAINTENANCE_SKIP_REASONS else ""


# --------------------------------------------------------------------------- #
# Dream：两条 runtime 共用的整理请求、披露面与截断硬闸
# --------------------------------------------------------------------------- #
#
# 之前 V1 自己拼卡片区（摘要 500 / 正文 900 / 全文 2 万字硬切，切在半张卡中间，
# 切掉的卡却仍可被退休），V2 自己按 6 万字预算挑卡、渲染好的串又没人用 ——
# 组件那边只收到卡的 id 和摘要，模型看不到正文就去 thicken/merge。
# 现在两边都把**读到的整张卡**交给组件，由组件带正文渲染并统一截断；
# io 只负责：把老字段名翻成组件认识的名字、看组件实际给模型看了哪些卡、
# 以及拒绝动「只给模型看了一半」的卡。

#: Dream 渲染预算。**显式传**而不是依赖组件默认值：默认值换了，这里不跟着悄悄变。
#: 单卡正文上限取 io 写入端的正文上限（5000 字），按 io 规则写出的卡永远完整呈现。
DREAM_CARDS_LIMIT = 60
DREAM_CARDS_BUDGET_CHARS = 60_000
DREAM_CARD_BODY_CHARS = 5_000
DREAM_CARD_SUMMARY_CHARS = 2_000

_DREAM_BUDGET_FIELDS = (
    "cards_limit", "cards_budget_chars", "card_body_chars", "card_summary_chars",
)

#: 装的 memgarden 太老、不会带正文渲染卡片时的失败码（content-free）。
#: 老版本会把整理提示词退化成「只有 id 和摘要」—— 模型看不到正文就重写整张卡，
#: 旧正文随旧卡退休。宁可这一晚整理失败退避，也不能静默走那条路。
DREAM_KERNEL_OUTDATED = "dream_kernel_outdated"
#: 模型的整理方案**全部**碰了被截断的卡时的失败码（content-free）。
DREAM_TRUNCATED_CARD_REJECTED = "dream_truncated_card_rejected"

# 老卡的字段名。组件只认 summary / content / bucket / threads。
_SUMMARY_KEYS = ("summary", "title", "description")
_CONTENT_KEYS = ("content", "body", "text", "plaintext")


class DreamKernelOutdated(RuntimeError):
    """装的 memgarden 不支持带正文的 Dream 渲染。"""

    code = DREAM_KERNEL_OUTDATED


def dream_kernel_renders_card_bodies() -> bool:
    """装的 memgarden 能不能带正文渲染 Dream 卡片（看公开请求契约上有没有预算字段）。

    自建 VPS 的 consumer 自更新时先切代码、再 pip 装依赖；装依赖失败时新代码会
    跑在旧 memgarden 上。这里判出来，调用方就让这一晚的 Dream 失败，而不是
    拿旧组件的「只给标题」提示词去整理。
    """
    names = {field.name for field in dataclasses.fields(MaintenanceRequest)}
    return all(name in names for name in _DREAM_BUDGET_FIELDS)


def dream_kernel_accepts_naming_rule() -> bool:
    """装的 memgarden 认不认 ``MaintenanceRequest.naming_rule``。

    不认时**照旧跑**，不失败：旧组件用内核默认的称呼规则（中文逐字相同，英文版
    不点名禁「用户」/「TA」），和这次修复之前的行为一样，比整晚不整理好。
    自建 VPS 的 consumer 自更新先切代码、后装依赖，会短暂出现这种组合。
    """
    names = {field.name for field in dataclasses.fields(MaintenanceRequest)}
    return "naming_rule" in names


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _first_text(card: Mapping[str, Any], keys: tuple[str, ...], *, one_line: bool) -> str:
    for key in keys:
        value = card.get(key)
        if not isinstance(value, str):
            continue
        text = _one_line(value) if one_line else value.strip()
        if text:
            return value
    return ""


def dream_card(item: Any) -> dict | None:
    """把一张读到的卡翻成组件认识的形状；不能进 Dream 的返回 None。

    老字段名映射：title/description → summary，body/text/plaintext → content，
    category → bucket，单个 thread → threads。其余字段（id、occurred_at、
    retrieval_cues …）原样保留 —— 下游 mapper 还要用它们算事件时间。

    「不能进 Dream」与组件渲染时跳过的卡同一判据：没有 id（模型无法引用），
    或者既没摘要也没正文。先在这里滤掉，组件实际渲染的就恰好是这份列表的前 N 张，
    io 才能不猜地知道模型看过哪些卡。
    """
    if not isinstance(item, Mapping):
        return None
    card = dict(item)
    if not _one_line(card.get("id")):
        return None
    summary = _first_text(card, _SUMMARY_KEYS, one_line=True)
    content = _first_text(card, _CONTENT_KEYS, one_line=False)
    if not summary and not content:
        return None
    card["summary"] = summary
    card["content"] = content
    if not _one_line(card.get("bucket")) and isinstance(card.get("category"), str):
        card["bucket"] = card["category"]
    if not isinstance(card.get("threads"), list) and isinstance(card.get("thread"), str):
        card["threads"] = [card["thread"]]
    return card


@dataclasses.dataclass(frozen=True)
class DreamDisclosure:
    """组件这一晚实际给模型看了什么。只有 id 和计数，不含卡片内容。

    ``rendered_ids``  模型看到的卡（按顺序）
    ``truncated_ids`` 其中正文或摘要被截断、标了 TRUNCATED 的卡 —— 不许动
    ``omitted``       读到了、但预算内放不下而没给模型看的卡数
    ``skip_reason``   组件判「花园太小不必整理」时的理由，否则 ""
    """

    cards: tuple[dict, ...] = ()
    rendered_ids: tuple[str, ...] = ()
    truncated_ids: frozenset[str] = frozenset()
    omitted: int = 0
    skip_reason: str = ""
    needed: bool = False

    @property
    def partial(self) -> bool:
        """读到的卡没有全部给模型看（组件的总预算/张数上限截掉了一部分）。"""
        return self.needed and self.omitted > 0

    def editable_cards(self) -> list[dict]:
        """整理方案允许退休的卡：模型完整看过的那些。

        没给模型看的卡、只给看了一半的卡，都不在里面 —— 交给 mapper 的
        ``existing_cards`` 用这份，指向其余卡的方案会被结构判据拒掉。
        """
        allowed = set(self.rendered_ids) - set(self.truncated_ids)
        return [card for card in self.cards if _one_line(card.get("id")) in allowed]


def open_dream_session(
    garden: GardenComponent,
    *,
    cards: Iterable[Any],
    locale: str,
    ai_name: str,
    user_name: str,
    recent_conversations: str,
) -> tuple[Any, DreamDisclosure]:
    """开一个 Dream 会话，并告诉调用方组件实际披露了哪些卡。

    ``cards`` 是这一晚**读到的全部可整理卡**（不用预先按字数挑）：
    张数、总字数、单卡截断都由组件按上面的预算做。

    披露面从会话的公开 ``result()`` 读：会话刚建好、还没问模型时，它的 trace
    已带上渲染计数和 ``truncated_card_ids``，且不改会话状态（问模型、喂回复
    照常进行）。和 :func:`maintenance_skip_reason` 用的是同一个契约。
    """
    if not dream_kernel_renders_card_bodies():
        raise DreamKernelOutdated(DREAM_KERNEL_OUTDATED)
    eligible: list[dict] = []
    seen: set[str] = set()
    for item in cards:
        card = dream_card(item)
        mid = _one_line(card.get("id")) if card is not None else ""
        if card is None or mid in seen:
            # 重复 id 只留第一张：组件会把两张都渲染出来，披露面就对不上了。
            continue
        seen.add(mid)
        eligible.append(card)
    known_ids = tuple(_one_line(card.get("id")) for card in eligible)
    # io 的称呼规则，和 capture_request 一样按**原始** user_name 生成（内部也洗）。
    # 以前 Dream 不传，整理时退回内核默认规则：英文花园里不点名禁「用户」/「TA」，
    # 白天落卡守住的称呼夜里被重写。
    naming = (
        {"naming_rule": _naming_rule(user_name, locale=locale)}
        if dream_kernel_accepts_naming_rule() else {}
    )
    session = garden.maintenance_session(MaintenanceRequest(
        cards=eligible,
        all_cards=eligible,
        locale=locale,
        ai_name=ai_name,
        # 组件只把字面 "TA" 当未知标记；「用户」「user」这类占位名要在这里洗掉，
        # 否则会原样当成名字写进提示词。
        user_name=sanitize_user_name(user_name),
        recent_conversations=recent_conversations,
        # 墓碑卡守卫覆盖读到的全部卡（组件会再并入实际渲染的那些）。
        known_ids=known_ids,
        cards_limit=DREAM_CARDS_LIMIT,
        cards_budget_chars=DREAM_CARDS_BUDGET_CHARS,
        card_body_chars=DREAM_CARD_BODY_CHARS,
        card_summary_chars=DREAM_CARD_SUMMARY_CHARS,
        **naming,
    ))
    outcome = session.result()
    trace = outcome.trace if isinstance(getattr(outcome, "trace", None), dict) else {}
    skip = str(trace.get("reason") or "") if not outcome.needed else ""
    rendered_count = max(0, int(trace.get("cards_rendered") or 0))
    disclosure = DreamDisclosure(
        cards=tuple(eligible),
        rendered_ids=known_ids[:rendered_count] if outcome.needed else (),
        truncated_ids=frozenset(
            _one_line(mid) for mid in (trace.get("truncated_card_ids") or [])
        ),
        omitted=max(0, int(trace.get("cards_omitted") or 0)),
        skip_reason=skip if skip in MAINTENANCE_SKIP_REASONS else "",
        needed=bool(outcome.needed),
    )
    return session, disclosure


def reject_truncated_consolidations(
    consolidations: Iterable[Any],
    truncated_ids: Iterable[str],
) -> tuple[list[dict], int]:
    """宿主侧硬闸：丢掉动了被截断卡的整理方案，返回 ``(留下的, 丢掉的条数)``。

    提示词已经禁止模型把 TRUNCATED 卡放进 ``card_ids``，但提示词不是保证：
    模型只看过前 5000 字就去重写整张卡，后半段正文会随旧卡一起退休，
    而用户看不出发生了什么。这道闸是确定性的，不看内容。
    """
    blocked = {_one_line(mid) for mid in truncated_ids if _one_line(mid)}
    kept: list[dict] = []
    rejected = 0
    for row in consolidations or []:
        if not isinstance(row, dict):
            continue
        raw_ids = row.get("card_ids")
        ids = {_one_line(mid) for mid in (raw_ids if isinstance(raw_ids, list) else [])}
        if blocked and ids & blocked:
            rejected += 1
            continue
        kept.append(row)
    return kept, rejected


# --------------------------------------------------------------------------- #
# 观测：把组件汇报的步骤翻译成 io 原有的口径
# --------------------------------------------------------------------------- #

class BounceTracker:
    """把组件的步骤流翻译成 io 一直在用的 ``bounce`` 三态。

    **口径必须原样保住** —— 这几个值进了日志和指标，改了口径等于把历史数据
    和新数据割开，而看板不会告诉你这件事。

        ""               没重问
        bounced_ok       重问后救回来了
        bounced_empty    重问后模型选择「宁可留空」—— 这是 prompt 想要的结果，
                         不是失败
        bounced_failed   重问后还是脏的
    """

    def __init__(self) -> None:
        self.retried = False
        self.steps: list[Step] = []
        #: 重问之后仍没过语义检查、被组件丢掉的卡数。
        #:
        #: 必须传下去，否则 admin 上「模型想覆盖但没说覆盖哪张」会退化成
        #: 「这轮没什么值得记」—— 一次模型失败被伪装成正常的空结果，查不出来。
        #: 组件把坏卡丢在自己那一层（对的：不能吐出 target_id 为空的
        #: supersede），代价就是宿主原来靠数 cards 得到的这个计数没了，
        #: 只能由组件显式告诉宿主。
        self.dropped_semantic = 0
        #: 重问之后 target_id 仍不是现有卡、被组件丢掉的卡数。和上面分开：
        #: 「没说覆盖哪张」和「说了一张不存在的」是两种模型失败。
        self.dropped_unknown_target = 0
        #: 组件建提示词时报的索引计数（``prompt_built`` 步骤里带的）。
        self.index: dict = {}

    def __call__(self, step: Step) -> None:
        self.steps.append(step)
        if step.kind == "retrying":
            self.retried = True
        elif step.kind == "dropped" and step.detail.get("why") == "semantic":
            self.dropped_semantic += int(step.detail.get("cards") or 0)
        elif step.kind == "dropped" and step.detail.get("why") == "unknown_target":
            self.dropped_unknown_target += int(step.detail.get("cards") or 0)
        elif step.kind == "prompt_built" and step.purpose == "capture":
            for key in ("index_candidates", "index_cards", "index_chars"):
                if isinstance(step.detail.get(key), int):
                    self.index[key] = step.detail[key]

    def index_detail(self) -> dict:
        """这次落卡的索引观测量，内容无关：现有卡几张、渲染进索引几张、多少字、
        丢了几张指向不存在的卡。没交现有卡时为空。"""
        if not self.index:
            return {}
        return {**self.index, "dropped_unknown_target": self.dropped_unknown_target}

    def bounce(self, *, cards: list, error: str | None) -> str:
        if not self.retried:
            return ""
        if error:
            return "bounced_failed"
        if not cards:
            # 模型接受了「宁可留空」这条出路。
            return "bounced_empty"
        return "bounced_ok"

    def drain(self) -> list[Step]:
        """取走攒下的步骤并清空 —— 给异步宿主用。

        ``on_step`` 是同步回调，而 io 的 trajectory 是 async。中间隔一层缓冲：
        组件同步地攒，宿主在自己的 await 点把它们发出去。

        不这么做的话，会话模式下宿主会**丢掉 parse_bounced / semantic_bounced
        这些事件** —— 而它们正是「这轮为什么多花了一次调用」的唯一线索。
        换成组件之后可观测性净退步，那这层门面就是亏的。
        """
        out, self.steps = self.steps, []
        return out

    @property
    def reask_trigger(self) -> str:
        """这次重问是被什么触发的（format / semantic / truncation）。"""
        for s in self.steps:
            if s.kind == "retrying":
                kind = str(s.detail.get("kind") or "")
                return kind or "format"
        return ""
