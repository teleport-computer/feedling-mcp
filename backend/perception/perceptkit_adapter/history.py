"""从 kit 的日聚合读回老路的形状 —— 趋势工具那条路。

`perception_trend`（「我最近睡得比以前少吗」）读的是老表的日聚合。老路一停写，
那张表就冻在停写那天：历史搬得过去，但**新的每天不再往老表写**，于是用户从
那天起再问「这周怎么样」就答不出来。

所以这一层和 `readback.py` 是同一件事的两半：那个把 kit 的**当前值**翻回老路
的字段名，这个把 kit 的**日聚合**翻回老路的文档形状。

## 翻译是 backfill 的反向

`backfill.convert` 把老文档转成 kit 的；这里把 kit 的转回老的。**同一张映射
表，两个方向** —— 分开写两份映射，迟早会漂开，而漂开的表现是趋势算出一个
错的数字，不报错。

## 翻不回去的照旧读老表

有几个信号的两种形状不是一一对应的（音乐：老的按歌手统计分钟数，kit 只有
播放/暂停时长）。那些**不翻**，直接读老表 —— 硬翻会给出一个看起来对、
实际是编的数字。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: 趋势从 kit 读。**默认开**，出问题设成 0 立刻回到读老表。
ENV_FLAG = "FEEDLING_PERCEPTKIT_TREND"


def enabled() -> bool:
    if (os.environ.get(ENV_FLAG, "1") or "1").strip().lower() in (
            "0", "false", "no", "off"):
        return False
    from .. import store
    # 和影子、快照同一条：store 被换成测试假实现时不读真库。
    return getattr(store, "__name__", "") == "perception.store"


#: 老信号名 -> (kit 信号名, 反向转换函数)。不在这里的走「改字段名」。
#:
#: 只列**能翻回去**的。翻不回去的不进这张表，调用方照旧读老表 ——
#: 一个编出来的趋势数字比没有趋势糟得多。
def _kit_signal(old_signal: str) -> str | None:
    from .backfill import UNCONVERTIBLE, _key_map
    if old_signal in UNCONVERTIBLE:
        return None
    return _key_map().get(old_signal)


def _unrename(doc: Mapping[str, Any], mapping: Mapping[str, str],
              *, scale: Mapping[str, float] = {}) -> dict[str, Any]:
    """字段名和单位翻回老路那一侧。"""
    back = {v: k for k, v in mapping.items()}
    out: dict[str, Any] = {}
    for k, v in doc.items():
        name = back.get(k, k)
        factor = scale.get(k)
        if factor and isinstance(v, Mapping):
            v = {kk: (vv / factor if isinstance(vv, (int, float))
                      and not isinstance(vv, bool) else vv) for kk, vv in v.items()}
        elif factor and isinstance(v, (int, float)) and not isinstance(v, bool):
            v = v / factor
        out[name] = v
    return out


def _carry_at(src: Mapping[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """把 ``_at`` 带过去。

    它是聚合自己的记账（这天最后一次写入的时刻），老路的文档里一直有。
    形状换了之后某些信号不再产出它 —— 少一个下划线开头的内部字段看着无害，
    但「老路给什么、新路就给什么」是这次切换唯一的验收标准，
    每放过一个「应该没人用吧」，标准就松一格。
    """
    # 两种形状用不同的名字记同一件事：``main_of_day`` 记 ``_at``，
    # ``duration_by_state`` 记 ``_last_ts`` —— 都是「这天最后一次写入的时刻」。
    at = src.get("_at")
    if at is None:
        at = src.get("_last_ts")
    if at is not None:
        out["_at"] = at
    return out


def _sleep_back(doc: Mapping[str, Any]) -> dict[str, Any]:
    """kit 的 ``{"minutes": {"core": 250, ...}}`` -> 老的四个总数。

    只有 ``asleep`` 一个桶时（那天没有分期数据），还原成 ``asleep_minutes``；
    有分期时把三个桶填回去，**并把总数算出来** —— 老路的趋势问的是
    「睡了多久」，读的就是 asleep_minutes。
    """
    minutes = doc.get("minutes")
    if not isinstance(minutes, Mapping):
        return {}
    out: dict[str, Any] = {}
    for stage, key in (("core", "core_minutes"), ("deep", "deep_minutes"),
                       ("rem", "rem_minutes")):
        if stage in minutes:
            out[key] = minutes[stage]
    if "asleep" in minutes:
        out["asleep_minutes"] = minutes["asleep"]
    elif out:
        out["asleep_minutes"] = sum(v for v in out.values()
                                    if isinstance(v, (int, float)))
    return _carry_at(doc, out)


def _vitals_back(doc: Mapping[str, Any], steps_doc: Mapping[str, Any] | None
                 ) -> dict[str, Any]:
    """vo2_max 从代表值补回分布的形状，步数从独立信号取回来。

    老路的趋势数学按 ``{min,max,sum,count}`` 读。vo2_max 在 kit 里是一个
    代表值 —— 补成 count=1 的分布，**不假装它是一天里的多次测量**。
    """
    out = dict(doc)
    vo2 = out.get("vo2_max")
    if isinstance(vo2, (int, float)) and not isinstance(vo2, bool):
        out["vo2_max"] = {"min": vo2, "max": vo2, "sum": vo2, "count": 1}
    if steps_doc:
        sc = steps_doc.get("step_count")
        if isinstance(sc, Mapping) and sc.get("total") is not None:
            total = sc["total"]
            # 老路按 numeric_dist 存，当天代表值取 max —— 这里只有一个数，
            # 就让 min/max 都等于它。
            out["step_count"] = {"min": total, "max": total,
                                 "sum": total, "count": 1}
    return out


_READ = """
SELECT local_date, signal, typed_aggregate
FROM perceptkit_daily_aggregate
WHERE subject_id = %s AND signal = ANY(%s) AND aggregation_kind = 'daily'
ORDER BY local_date DESC
LIMIT %s
"""


def daily_rollups(user_id: str, old_signal: str, days: int) -> list[dict] | None:
    """老路 ``list_perception_daily`` 的 kit 版。

    返回 None = 这个信号翻不回去、或者 kit 那边一条都没有，调用方读老表。
    **空列表和 None 不是一回事**：空列表是「kit 说这几天没有数据」，
    None 是「别问我」。
    """
    kit_signal = _kit_signal(old_signal)
    if kit_signal is None:
        return None
    from .backfill import _FIELD_RENAMES, _FIELD_SCALES, _SPLITS

    from .ios_report import SPLIT_OFF
    # 一条老信号的历史现在散在好几个 kit 信号里（0.4.0 的拆分），
    # 要全读回来才能拼回老路那一份形状 —— 少读一个，那个指标的趋势
    # 就静默变成空的，而调用方看到的是"这项没有历史"。
    wanted = [kit_signal]
    split = _SPLITS.get(old_signal)
    if split:
        wanted.append(split[0])
    wanted += sorted({t for t, _f in SPLIT_OFF.get(old_signal, {}).values()})

    import db
    limit = max(1, min(int(days), 400)) * len(wanted)
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_READ, (user_id, wanted, limit))
            rows = cur.fetchall()
    if not rows:
        return None

    by_date: dict[Any, dict[str, Any]] = {}
    for day, signal, doc in rows:
        by_date.setdefault(day, {})[signal] = doc if isinstance(doc, dict) else {}

    out: list[dict] = []
    for day in sorted(by_date):
        # ★ 老表的 date 列是 **text**，kit 那张是 DATE。这个函数的调用方
        #   （趋势数学、按天对齐）拿到的必须是同一种东西 —— 一边给
        #   `'2026-08-31'` 一边给 `date(2026, 8, 31)`，比较、当字典键、
        #   序列化全会静默走岔。
        docs = by_date[day]
        # 主信号那份可能整天都没有（比如那天只测了体脂没称体重）——
        # 但拆出去的兄弟信号有。以前 `main is None: continue` 会把
        # 那一天整个丢掉。合并之后只要有任何一个信号有数据就算这天有。
        main = dict(docs.get(kit_signal) or {})
        for extra_signal in wanted[1:]:
            extra = docs.get(extra_signal)
            if isinstance(extra, dict):
                main.update(extra)
        if not main:
            continue
        if old_signal == "health_sleep":
            translated = _sleep_back(main)
        elif old_signal == "health_vitals":
            translated = _vitals_back(main, docs.get("steps"))
        else:
            translated = _unrename(main, _FIELD_RENAMES.get(old_signal, {}),
                                   scale=_FIELD_SCALES.get(old_signal, {}))
        out.append({"date": day.isoformat() if hasattr(day, "isoformat") else str(day),
                    "doc": translated})
    return out or None


__all__ = ["ENV_FLAG", "enabled", "daily_rollups"]
