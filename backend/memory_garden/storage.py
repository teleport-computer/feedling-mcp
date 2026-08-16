"""存储 port —— 内核只对着这个接口说话，实现由调用方注入。

## 为什么后端不只是数据库

IO 自己的实现是 Postgres + enclave 信封，但同一个内核将来可能挂在**另一个记忆
系统**上（mem0 / engram / 用户自己的库）。对方有自己的格式和规矩，不一定支持
我们的全部操作。会真撞上的一例：

    内核：把这三张旧卡标记为「被取代」（保留链条，不删）
    Postgres 适配器：好，改个状态字段
    某个外部记忆库：我没有「被取代」这个概念，只能删掉或覆盖内容

所以接口留一个口子：**适配器声明自己支持哪些能力**。这条现在定成本几乎为零；
等适配器都写完再改，全部要返工。

## 能力分两类，不能一视同仁

这是 codex code_review（2026-08-14）纠正的一处：原实现把所有缺失能力都当成
「可降级」，写条日志继续跑。但其中两项缺了**没有正确的降级路径**：

    缺 supersede    → 降级成「覆盖旧卡」会破坏「永远不硬删」这条红线，
                      前后链条丢了就不可追溯，写日志救不回来
    缺 atomic_batch → 降级成「逐条写」会留下半完成状态（两张 active 卡，
                      或旧卡已退休而新卡没写成），这是数据损坏不是体验下降

所以它们是**正确性前置条件**：缺了就拒绝对应操作，而不是降级。
另外两项（custom_fields / metadata_sort）缺了只是能力退化，可以降级但必须上报。

## 声明必须显式

``Capabilities`` 没有默认值：外部适配器要逐项写清楚。原实现默认全 True 是
fail-open —— 适配器漏声明会被当成「全支持」，正好错在最危险的方向。
IO 自己的适配器用 ``FULL_CAPABILITIES``。

## 现状

本模块只定义接口与降级规划，不接任何真实存储。把 IO 现有的
锁 / 信封 / 全量替换包成适配器，是后续批次的事（会动写入路径，需拍板）。

## ⚠️ 这个接口还没定完（codex review 2026-08-14）

已知的四处不足，**接第一个真实适配器之前必须先定**，否则所有适配器都要返工：

1. ``mutations`` 现在是 ``list[dict]``，无法从类型判断这批操作需要哪些能力。
   应该改成 ``Add`` / ``Merge`` / ``Supersede`` 之类的类型。
2. ``ensure_supported`` 依赖每个调用方**记得**手工调用。应该在统一的
   executor / wrapper 里按 mutation 类型自动校验，而不是靠自觉。
3. 读侧只定义了 ``load``，没定义「挑完候选之后怎么取内容」（index → fetch →
   decrypt 那一段现在还在 IO 侧的 ``memory_readside_core``，没进 port）。
4. 冲突与部分失败没有标准表达：``RevisionConflict`` / ``IdempotencyConflict`` /
   ``UnsupportedMutation`` 都还没定义，调用方只能靠约定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Capabilities:
    """一个存储后端能做什么。**每一项都必须显式声明** —— 没有默认值。

    不给默认值是有意的：适配器漏写一项时，我们宁可它报错，
    也不要被静默当成「支持」。
    """

    supports_supersede: bool
    """能不能「标记为被取代」而不是删掉。

    ⚠️ **正确性前置条件，不可降级。** 做梦消矛盾、capture 的 supersede 都依赖它。
    缺了只能拒绝该操作 —— 降级成覆盖会破坏「永远不硬删」，链条丢了不可追溯。
    """

    supports_atomic_batch: bool
    """能不能把一批 mutation 当作一个原子单位。

    ⚠️ **正确性前置条件，不可降级。**「写新卡 + 标记旧卡」必须一起成或一起败。
    缺了只能拒绝复合 mutation —— 逐条写会留下半完成状态。
    """

    supports_custom_fields: bool
    """能不能原样保留 bucket / threads 这些自定义字段。

    可降级：塞进对方的 metadata 或正文，检索与展示会退化但数据不损坏。
    """

    supports_metadata_sort: bool
    """能不能按元数据（重要度 / 时间 / 状态）排序并分页。

    可降级：把候选拉回本地再排，量大时延迟和内存变差但结果正确。
    """


#: IO 自己的 Postgres + enclave 适配器：全部支持。
FULL_CAPABILITIES = Capabilities(
    supports_supersede=True,
    supports_atomic_batch=True,
    supports_custom_fields=True,
    supports_metadata_sort=True,
)

#: 缺了就必须拒绝对应操作的能力（不是「降级后继续」）。
CORRECTNESS_CRITICAL = frozenset({"supports_supersede", "supports_atomic_batch"})


@dataclass(frozen=True)
class Degradation:
    """一条能力退化：少了什么、退化成什么、代价是什么。

    只用于**可降级**的能力。正确性前置条件不产出 Degradation，
    而是让相应操作直接被 ``ensure_supported`` 拒绝。
    """

    capability: str
    fallback: str
    cost: str


@dataclass(frozen=True)
class UnsupportedOperation(RuntimeError):
    """缺少正确性前置条件时，对应操作被拒绝。"""

    capability: str
    operation: str
    why: str

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        return f"{self.operation} 需要 {self.capability}：{self.why}"


_DEGRADABLE_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "supports_custom_fields",
        "把 bucket/threads 塞进对方的 metadata 或正文",
        "按桶/线索的检索与展示失效，做梦的归并判断拿不到结构信息",
    ),
    (
        "supports_metadata_sort",
        "把候选拉回本地再排序",
        "记忆量大时每轮选卡的延迟和内存变差",
    ),
)

_CRITICAL_REASONS: dict[str, str] = {
    "supports_supersede": (
        "降级成覆盖会破坏「永远不硬删」——旧卡的前后链条丢失且不可追溯，"
        "写日志救不回来"
    ),
    "supports_atomic_batch": (
        "降级成逐条写会留下半完成状态：两张 active 卡，或旧卡已退休而新卡没写成"
    ),
}


def plan_degradations(caps: Capabilities) -> list[Degradation]:
    """算出这个后端要承受哪些**可接受的**能力退化。

    只覆盖 custom_fields / metadata_sort。正确性前置条件不在这里 ——
    那两项缺失时不是「降级运行」，而是相应操作必须被拒绝，见 ``ensure_supported``。

    **调用方必须把非空结果上报**（日志/指标/用户可见），不允许丢弃。
    """
    return [
        Degradation(capability=name, fallback=fallback, cost=cost)
        for name, fallback, cost in _DEGRADABLE_RULES
        if not getattr(caps, name)
    ]


def missing_critical(caps: Capabilities) -> list[str]:
    """列出缺失的正确性前置条件。非空即代表这个后端只能跑受限的操作集。"""
    return sorted(name for name in CORRECTNESS_CRITICAL if not getattr(caps, name))


def ensure_supported(caps: Capabilities, *, operation: str, requires: str) -> None:
    """在执行需要前置条件的操作前调用；不满足直接抛。

    例：做梦要 supersede 一批旧卡之前
    ``ensure_supported(caps, operation="dream.supersede", requires="supports_supersede")``
    """
    if getattr(caps, requires):
        return
    raise UnsupportedOperation(
        capability=requires,
        operation=operation,
        why=_CRITICAL_REASONS.get(requires, "该后端不支持这项能力"),
    )


def describe_capabilities(caps: Capabilities) -> str:
    """渲染成一段给工程看的说明（日志/诊断用），措辞偏技术。"""
    lines: list[str] = []
    critical = missing_critical(caps)
    if critical:
        lines.append("⚠️ 该后端缺少正确性前置条件，相关操作会被拒绝：")
        for name in critical:
            lines.append(f"  · {name}：{_CRITICAL_REASONS[name]}")
    degradations = plan_degradations(caps)
    if degradations:
        lines.append("该后端缺少以下能力，已降级运行：")
        for d in degradations:
            lines.append(f"  · {d.capability}：{d.fallback} —— 代价：{d.cost}")
    if not lines:
        return "该存储后端支持全部能力，无降级、无受限操作。"
    return "\n".join(lines)


# 给接入方看的话术：不出现字段名，说清楚「你会失去什么」。
_USER_FACING_CRITICAL: dict[str, str] = {
    "supports_supersede": (
        "这个记忆库不支持「标记为被取代」，只能覆盖或删除。"
        "为避免记忆被不可追溯地改掉，整理记忆时的消矛盾会被跳过"
    ),
    "supports_atomic_batch": (
        "这个记忆库不支持把一批改动作为整体提交。"
        "为避免出现改了一半的状态，需要多步完成的整理（合并、取代）会被跳过"
    ),
}

_USER_FACING_DEGRADED: dict[str, str] = {
    "supports_custom_fields": (
        "这个记忆库不能原样保存「桶」和「线索」，它们会被折叠进正文。"
        "按桶浏览和分类展示会不可用"
    ),
    "supports_metadata_sort": (
        "这个记忆库不能按重要度排序，需要每次把记忆全部取回本地再排。"
        "记忆变多之后回复会变慢"
    ),
}


def describe_for_user(caps: Capabilities) -> list[str]:
    """给**接入方**看的说明：接了这个记忆库，你会失去什么。

    与 ``describe_capabilities`` 的区别是措辞 —— 这里不出现
    ``supports_supersede`` 这类字段名，只讲后果。

    hx 2026-08-14 定：降级信息给用户看，让接入方知道自己失去了什么，
    而不是只写进日志。返回空列表代表「什么都没损失」，调用方可以不展示。
    """
    out: list[str] = []
    for name in missing_critical(caps):
        text = _USER_FACING_CRITICAL.get(name)
        if text:
            out.append(text)
    for d in plan_degradations(caps):
        text = _USER_FACING_DEGRADED.get(d.capability)
        if text:
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# 快照与写入结果（CAS 协议）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Snapshot:
    """一次读取的结果：未解密的卡视图 + 这批数据的版本号。

    ``revision`` 是调用方做 CAS 的凭据 —— 基于这份快照算出来的 mutation
    必须带着它回来，否则并发写会基于过期快照覆盖别人刚写的卡。
    原实现只返回卡列表，调用方无从获得合法 token（codex code_review 2026-08-14）。
    """

    cards: list[dict]
    """未解密的卡。**「信封」是 IO 适配器的内部概念，不是领域模型** ——
    对 mem0 / SQLite 这类后端不成立。内核只要求：每张卡带得动打分需要的明文
    元数据（重要度 / 情绪强度 / 最近被想起 / 状态），内容部分不透明即可。
    （codex review 2026-08-14 指出原字段名 ``envelopes`` 把 IO 假设写进了 port。）
    """

    revision: Any


@dataclass(frozen=True)
class ApplyResult:
    """一次写入的结果：每个动作的结局 + 写完之后的新版本号。

    ``revision`` 让调用方可以接着做下一次 CAS，不必重新 load。
    """

    results: list[dict] = field(default_factory=list)
    revision: Any = None


# --------------------------------------------------------------------------- #
# 存储 port
# --------------------------------------------------------------------------- #


@runtime_checkable
class StoragePort(Protocol):
    """内核对存储的全部要求。

    读侧返回的是**信封**（明文元数据 + 密文正文原样），内核只在明文元数据上
    打分排序；解密由适配器在内核挑完候选之后另做一步。

    写侧只有 ``apply`` 一个入口，因为「写新卡 + 标记旧卡」必须原子。
    不提供 save/update/delete 三个独立方法 —— 那样在并发下会丢卡
    （IO 现有实现用跨进程 advisory fence 包住整个 load→mutate→save）。
    """

    def capabilities(self) -> Capabilities:
        """声明这个后端支持哪些能力。每一项都要显式给。"""
        ...

    def load(self, tenant: str, **filters: Any) -> Snapshot:
        """取出该租户的卡 + 版本号。不解密（若该后端有加密的话）。"""
        ...

    def apply(
        self,
        tenant: str,
        mutations: list[dict],
        *,
        idempotency_key: str,
        expected_revision: Any,
    ) -> ApplyResult:
        """把一批 mutation 作为一个原子单位写入。

        ``expected_revision`` 来自先前 ``load`` 的 ``Snapshot.revision``；
        与当前不符时适配器应拒绝写入（CAS 失败），由调用方重读后重算。
        纯新增（不依赖旧快照）可以传 ``None``。

        ``idempotency_key`` 保证同一批重放不产生第二份。
        """
        ...
