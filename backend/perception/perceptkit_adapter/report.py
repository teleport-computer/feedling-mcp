"""切换期的观测口：kit 和老路到底差在哪，唤醒都去哪了。

**没有这个，账记了也看不见。** 影子把每一次比对的结果写进了
`perceptkit_shadow_divergence`，唤醒的下场写进了发件箱和回执 —— 但那些都在
数据库里，而要看它们的时候（test 上跑着、真机在打数据）没人能连库。

所以这一份是只读汇总，给一个问题一个答案：

    还能不能切下去？    有没有字段两条路算出不同答案
    唤醒正常吗？        产生了多少、投出去多少、被 io 的闸挡下多少
    覆盖到哪了？        kit 见过多少信号 —— 干净的差异报告要配上分母才有意义

## 为什么把「被闸挡下」单独列出来

`conversation_suppressed` 看起来像失败，其实是**用户的免打扰在起作用**，是
系统正常工作的样子。混在失败里会让人去修一个不存在的问题；而真正该慌的
`enqueue_failed` 会淹在里面。
"""
from __future__ import annotations

from typing import Any

#: 一次最多列多少条差异。差异是有上限的（字段数 × 判定数），但真出了大范围
#: 不一致时，一个几千行的 JSON 谁也读不下去。
MAX_ROWS = 50


def build(conn: Any, *, subject_id: str | None = None) -> dict[str, Any]:
    """汇总。只读，不改任何东西。"""
    from . import compare

    rows = compare.summarize(conn, subject_id=subject_id)
    coverage = _coverage(conn, subject_id)
    return {
        "perception": {
            # 空 = 两条路对每个比过的字段都算出了同一个答案。
            "divergences": [_clean(r) for r in rows[:MAX_ROWS]],
            "divergence_total": len(rows),
            "agreements": _verdict_total(conn, "agree", subject_id),
        },
        "wakes": _wakes(conn, subject_id),
        "coverage": coverage,
    }


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for k, v in list(out.items()):
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "quantize"):        # Decimal
            out[k] = int(v)
    return out


def _verdict_total(conn: Any, verdict: str, subject_id: str | None) -> int:
    sql = ("SELECT COALESCE(SUM(occurrences), 0) FROM perceptkit_shadow_divergence "
           "WHERE verdict = %s")
    params: list[Any] = [verdict]
    if subject_id:
        sql += " AND subject_id = %s"
        params.append(subject_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


def _wakes(conn: Any, subject_id: str | None) -> dict[str, Any]:
    sql = """
      SELECT o.event_type, o.delivery_state, r.status, COUNT(*)
      FROM perceptkit_event_outbox o
      LEFT JOIN perceptkit_wake_receipt r ON r.event_id = o.event_id
      {where}
      GROUP BY 1, 2, 3 ORDER BY 4 DESC
    """.format(where="WHERE o.subject_id = %s" if subject_id else "")
    params = [subject_id] if subject_id else []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    by_state: list[dict[str, Any]] = []
    delivered = suppressed = failed = 0
    for event_type, state, status, n in rows:
        by_state.append({"event_type": event_type, "delivery_state": state,
                         "receipt": status, "count": int(n)})
        if status == "accepted":
            delivered += int(n)
        elif status == "conversation_suppressed":
            suppressed += int(n)
        elif status in ("enqueue_failed", "rejected"):
            failed += int(n)
    return {
        "produced": sum(r["count"] for r in by_state),
        "delivered": delivered,
        # **不是故障**：用户的免打扰 / 频率闸 / 还没开主动性。
        "suppressed_by_host": suppressed,
        # 这个才该看。
        "failed": failed,
        "detail": by_state[:MAX_ROWS],
    }


def _coverage(conn: Any, subject_id: str | None) -> dict[str, Any]:
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS

    from . import compare

    sql = "SELECT signal, COUNT(*) FROM perceptkit_current"
    params: list[Any] = []
    if subject_id:
        sql += " WHERE subject_id = %s"
        params.append(subject_id)
    sql += " GROUP BY 1 ORDER BY 2 DESC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        seen = {row[0]: int(row[1]) for row in cur.fetchall()}
    declared = compare.coverage(MINIMAL_SIGNALS)
    return {
        "signals_seen": seen,
        "signals_compared": declared["signals_compared"],
        "fields_compared": declared["fields_compared"],
        # 干净的差异报告只有配上这个才有意义。
        "observed_only": declared["signals_observed_only"],
        "shape_differs": sorted(declared["shape_differs"]),
    }


__all__ = ["MAX_ROWS", "build"]
