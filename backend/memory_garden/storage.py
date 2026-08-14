"""存储 port —— 内核只对着这个接口说话，实现由调用方注入。

## 为什么后端不只是数据库

IO 自己的实现是 Postgres + enclave 信封，但同一个内核将来可能挂在**另一个记忆
系统**上（mem0 / engram / 用户自己的库）。对方有自己的格式和规矩，不一定支持
我们的全部操作。会真撞上的一例：

    内核：把这三张旧卡标记为「被取代」（保留链条，不删）
    Postgres 适配器：好，改个状态字段
    某个外部记忆库：我没有「被取代」这个概念，只能删掉或覆盖内容

所以接口留一个口子：**适配器声明自己支持哪些能力，内核遇到不支持的就降级**，
而不是假设所有后端都支持全部能力。这条现在定成本几乎为零；等适配器都写完再改，
全部要返工。

## 降级必须显式

⚠️ 静默降级会让用户以为功能都在，实际记忆库在悄悄变乱 —— 本项目在别处踩过
静默失败的坑（工具被三层静默丢弃，模型把它说成「没权限」）。所以
``plan_degradations`` 对每一项缺失能力都产出一条**带后果说明**的记录，
由调用方决定报到哪（日志 / 指标 / 用户可见）。

IO 的 Postgres 适配器声明支持全部能力，**所以 IO 侧行为不受这个机制影响**；
降级只发生在外部适配器上。

## 现状

本模块只定义接口与降级规划，不接任何真实存储。把 IO 现有的
锁 / 信封 / 全量替换包成适配器，是后续批次的事（会动写入路径，需 hx 拍板）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# 能力声明
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Capabilities:
    """一个存储后端能做什么。适配器在注入时声明。

    默认全 True 是有意的：**IO 自己的后端支持全部能力**，
    只有外部适配器才需要显式关掉某几项。
    """

    supports_supersede: bool = True
    """能不能「标记为被取代」而不是删掉。

    做梦消矛盾、capture 的 supersede 都依赖它。不支持时旧卡只能被覆盖，
    前后链条会丢 —— 这是 IO 记忆的一条红线（永远不硬删）。
    """

    supports_atomic_batch: bool = True
    """能不能把一批 mutation 当作一个原子单位。

    「写新卡 + 标记旧卡」必须一起成或一起败。不支持时会出现两张 active 卡，
    或者旧卡退休了新卡没写成。
    """

    supports_custom_fields: bool = True
    """能不能原样保留 bucket / threads 这些自定义字段。

    不支持时只能塞进对方的 metadata 或正文，检索与展示都会退化。
    """

    supports_metadata_sort: bool = True
    """能不能按元数据（重要度 / 时间 / 状态）排序并分页。

    不支持时每轮选卡要把全部卡拉回本地再排，量大了扛不住。
    """


@dataclass(frozen=True)
class Degradation:
    """一条降级记录：少了什么能力、退化成什么、后果是什么。

    三个字段都必填 —— ``risk`` 存在的意义就是让降级没法被静默吞掉。
    """

    capability: str
    fallback: str
    risk: str


# 能力 → (退化成什么, 后果)。文案写死在这里，保证任何调用方报出来的口径一致。
_DEGRADATION_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "supports_supersede",
        "退化成直接覆盖旧卡内容",
        "前后矛盾的链条会丢失，「这张卡被哪张取代」不可追溯；违反「永远不硬删」这条红线",
    ),
    (
        "supports_atomic_batch",
        "退化成逐条写入",
        "中途失败会留下半完成状态：两张 active 卡，或旧卡已退休而新卡没写成",
    ),
    (
        "supports_custom_fields",
        "退化成把 bucket/threads 塞进对方的 metadata 或正文",
        "按桶/线索的检索与展示失效，做梦的归并判断也拿不到结构信息",
    ),
    (
        "supports_metadata_sort",
        "退化成把全部卡拉回本地再排序",
        "记忆量大时每轮选卡的延迟和内存都会失控",
    ),
)


def plan_degradations(caps: Capabilities) -> list[Degradation]:
    """按能力声明算出这个后端要承受哪些降级。

    全支持返回空列表。**调用方必须把非空结果上报**（日志/指标/用户可见），
    不允许丢弃 —— 静默降级正是这套机制要防的东西。
    """
    return [
        Degradation(capability=name, fallback=fallback, risk=risk)
        for name, fallback, risk in _DEGRADATION_RULES
        if not getattr(caps, name)
    ]


def describe_degradations(degradations: list[Degradation]) -> str:
    """把降级列表渲染成一段人能读的说明，供日志或界面直接用。"""
    if not degradations:
        return "无降级：该存储后端支持全部能力。"
    lines = ["该存储后端缺少以下能力，已降级运行："]
    for d in degradations:
        lines.append(f"  · {d.capability}：{d.fallback} —— 风险：{d.risk}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 存储 port
# --------------------------------------------------------------------------- #


@runtime_checkable
class StoragePort(Protocol):
    """内核对存储的全部要求。

    读侧返回的是**信封**（明文元数据 + 密文正文原样），内核只在明文元数据上
    打分排序；解密由适配器在内核挑完候选之后另做一步。

    写侧只有一个入口 ``apply``，因为「写新卡 + 标记旧卡」必须原子。
    不提供 save/update/delete 三个独立方法 —— 那样在并发下会丢卡
    （IO 现有实现用跨进程 advisory fence 包住整个 load→mutate→save）。
    """

    def capabilities(self) -> Capabilities:
        """声明这个后端支持哪些能力。"""
        ...

    def load(self, tenant: str, **filters: Any) -> list[dict]:
        """取出该租户的卡信封。不解密。"""
        ...

    def apply(
        self,
        tenant: str,
        mutations: list[dict],
        *,
        idempotency_key: str,
        expected_revision: Any | None = None,
    ) -> list[dict]:
        """把一批 mutation 作为一个原子单位写入，返回每个动作的结果。

        ``idempotency_key`` 保证重放不产生第二份；``expected_revision``
        做 CAS，防止基于过期快照覆盖别人刚写的卡。
        """
        ...
