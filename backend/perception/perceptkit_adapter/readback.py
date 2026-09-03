"""把 kit 的表读回成老路的输出形状。

影子证明了 kit 算得和老路一样；这个模块是**用它的结果**。切换不是重写读取
层——agent、prompt、工具、iOS 全都按老路的字段名在读（`battery_level`、
`in_focus`、`now_playing`…），形状变一点都是一次跨端改动。所以这里只做一件
事：**把 kit 的答案翻译回那些名字**，输出形状逐字段不变。

## 翻译表就是当初的对比表

`compare.COMPARABLE` 记的是「kit 的这个字段 == 老路的那个字段」。影子拿它
证明两边一致，切换拿它做翻译——**同一张表，两个方向**。这不是省事：任何一
对没被影子验过的映射，都不该拿来给用户供数。

## kit 供不上的，继续走老路

23 个信号里 kit 覆盖了大部分，但有些字段 manifest 根本没建模（焦点的授权
状态、broadcast 的文字状态），有些形状不同（睡眠），有些等产品方拍板。
**那些字段照旧从老路读**，并且**记下来源** —— 一份分不清哪个字段来自哪条
路的输出，出了问题没法回溯。

## 时效性照老路的规矩

老路按每个信号的 `ttl_sec` 判「过期就当没有」，kit 有自己的 `current_ttl_sec`。
这里**用老路的**：切换要改的是数据来源，不是「什么算过期」。两件事一起改，
出了问题分不清是谁的。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: 来源标记，写进日志。不是给 agent 看的。
FROM_KIT = "kit"
FROM_LIVE = "live"


#: 老路那份值**比 manifest 建模的更全**的字段 —— 这些继续走老路。
#:
#: 不是 kit 算错了，是它按规范只留了一部分。拿 kit 的版本去替换，等于在
#: 「换数据来源」的同时**悄悄删掉几个 agent 现在读得到的东西**：
#:
#:   motion_state  老路存 iOS 整个对象（state + confidence + started_at）；
#:                 manifest 只建模了状态标签，confidence 还是不同类型（字符串
#:                 vs 0~1 的数），适配层直接丢掉了。
#:   now_playing   老路存整个播放对象（含 duration / media_type）；
#:                 manifest 没这两个字段。而且播放状态是**多对一**翻译过的
#:                 （seeking_forward → playing），翻不回去。
#:
#: 这两条要能换成 kit，得先扩 manifest —— 归产品方。在那之前，
#: **少给 agent 一点东西，比给它一个看不出来变窄了的版本更糟**。
LIVE_SHAPE_IS_RICHER: dict[str, str] = {
    "motion_state": "老路存 iOS 整个对象；manifest 只建模状态标签，丢了 confidence / started_at",
    "now_playing": "老路存整个播放对象；manifest 没有 duration / media_type，播放状态还是多对一翻译过的",
}

#: manifest 的词表翻回老路的词表。**只放能一一对应的** ——
#: 多对一的（播放的 seeking_forward → playing）翻不回来，那种字段整个走老路。
#:
#: 不做这层翻译的后果很具体：`app_state` 会从 `closed` 变成 `close`，
#: 读它的 prompt 和工具一个字都不知道。换数据来源不该顺带换词表。
REVERSE_VOCABULARY: dict[str, dict[str, str]] = {
    "app_state": {"open": "foreground", "close": "closed"},
}


def _live_cell_value(state: Mapping[str, Any], field: str) -> Any:
    cell = state.get(field)
    return cell.get("v") if isinstance(cell, Mapping) else None


def kit_fields(current: Mapping[str, Any]) -> dict[str, tuple[Any, float]]:
    """kit 的当前投影 -> ``{老路字段名: (值, 发生时刻的 epoch 秒)}``。

    **要带上发生时刻**：过期判据算的是「这个值多久以前的」，而值来自 kit 的
    时候，老路那个格子的时间戳跟它没有关系。只翻译值、拿老路的时间戳去判过期，
    结果是 kit 供的字段**永远不过期** —— 用户十五分钟前关掉的 app，一天以后
    还在快照里写着「正在用」。

    ``current`` 是 ``storage.get_current()`` 的返回：信号 -> 投影列表。
    非 ``observed`` 的信号一律给 None —— 和老路「没权限/没读到就是 None」
    对齐，**不能把 kit 留着的 last_known 当成当前值报出去**，那正是
    影子第一批修掉的错误之一。
    """
    from . import compare

    out: dict[str, Any] = {}
    for signal, pairs in compare.COMPARABLE.items():
        rows = current.get(signal) or ()
        if not rows:
            # ★ kit 这个信号一条记录都没有 —— 这**不是**「kit 说没有」，
            #   是「kit 还没见过」。两者差别很大：
            #
            #     kit 见过、状态是 unavailable  用户撤了权限 → 该报 None
            #     kit 一条都没有                影子开跑前的老用户 → 该走老路
            #
            #   不区分的话，切换那一刻每个老用户的 49 个字段一起变空 ——
            #   他们的数据一直都在，只是在另一张表里。
            continue
        best = max(rows, key=lambda p: (p.observed_at, p.dimension_key))
        observed = best.availability == "observed"
        typed = (best.typed_value or {}) if observed else {}
        if not isinstance(typed, Mapping):
            typed = {}
        for kit_field, live_where in pairs.items():
            name = live_where[0] if isinstance(live_where, tuple) else live_where
            value = typed.get(kit_field) if observed else None
            # 声明过的单位换算，反着走一遍（kit 存比率，老路存百分比）。
            bridge = compare.UNIT_BRIDGE.get((signal, kit_field))
            if bridge is not None and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                value = value * 100.0 if bridge(100.0) == 1.0 else value
            at = best.observed_at.timestamp()
            if isinstance(live_where, tuple):
                # 老路把整个对象存在一个字段里（now_playing）。逐个键拼回去，
                # 不是覆盖 —— 覆盖会让后写的那个键把前面的整份对象顶掉。
                holder, _ = out.setdefault(name, ({}, at))
                if isinstance(holder, dict):
                    holder[live_where[1]] = value
                continue
            out[name] = (value, at)
    return out


def merged_snapshot(
    live_state: Mapping[str, Any],
    kit_current: Mapping[str, Any],
    *,
    wanted: Mapping[str, float],
    now: float,
    stable_fields: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """合成一份快照：能用 kit 的用 kit，其余走老路。

    ``wanted`` 是 ``{字段名: 该字段的 ttl_sec}`` —— 由调用方按老路的目录算好
    传进来，这里不重新决定哪些字段该出现。

    返回三样：

        snapshot   给 agent 的那一份，字段名和形状与老路完全一致
        sources    每个字段来自哪条路，用于日志
        conflicts  两边都有值、但不一样的那些（这才是要看的东西）
    """
    from_kit = kit_fields(kit_current)
    snapshot: dict[str, Any] = {}
    sources: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []

    for field, ttl in wanted.items():
        live = _live_cell_value(live_state, field)
        cell = live_state.get(field)
        fresh = True
        if field not in stable_fields and isinstance(cell, Mapping):
            fresh = (now - float(cell.get("ts") or 0)) <= ttl
        live_effective = live if fresh else None

        if field in from_kit and field not in LIVE_SHAPE_IS_RICHER:
            value, observed_at = from_kit[field]
            vocab = REVERSE_VOCABULARY.get(field)
            if vocab and isinstance(value, str):
                value = vocab.get(value, value)
            # 过期判据按老路的目录，但时间戳用 kit 那条观测自己的 —— 值从哪来，
            # 就按哪边的时刻算它多老。
            if field not in stable_fields and (now - observed_at) > ttl:
                value = None
            snapshot[field] = value
            sources[field] = FROM_KIT
            # 两边都有值但不一样 —— 这就是切换期唯一要盯的东西。
            # 一边有一边没有不算冲突：kit 只见过影子开跑之后的数据。
            if (value is not None and live_effective is not None
                    and value != live_effective):
                conflicts.append({"field": field, "kit": value, "live": live_effective})
        else:
            snapshot[field] = live_effective
            sources[field] = FROM_LIVE
    return snapshot, sources, conflicts


def summarize(sources: Mapping[str, str], conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    """一行日志。**冲突要带字段名和两个值** —— 只报个数量的日志，
    等于告诉你"有问题"然后不告诉你是什么。"""
    kit_n = sum(1 for v in sources.values() if v == FROM_KIT)
    return {
        "fields": len(sources),
        "from_kit": kit_n,
        "from_live": len(sources) - kit_n,
        "conflicts": len(conflicts),
        # 截断是为了日志行不会因为一次大范围不一致变成几十 KB。
        "detail": conflicts[:10],
    }


__all__ = ["FROM_KIT", "FROM_LIVE", "LIVE_SHAPE_IS_RICHER", "REVERSE_VOCABULARY",
           "kit_fields", "merged_snapshot", "summarize"]
