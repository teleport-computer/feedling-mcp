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

from memgarden import GardenComponent, MaintenanceRequest
from memgarden.contracts import Step

from identity.user_naming import sanitize_user_name
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

    def __call__(self, step: Step) -> None:
        self.steps.append(step)
        if step.kind == "retrying":
            self.retried = True
        elif step.kind == "dropped" and step.detail.get("why") == "semantic":
            self.dropped_semantic += int(step.detail.get("cards") or 0)

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
