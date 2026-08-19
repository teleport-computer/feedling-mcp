"""把挑卡过程压成一条**不含任何正文**的可落库记录。

## 为什么需要它

2026-08-17 之前，「每轮自动注入了哪几张卡」在服务端**一个字都查不到**：
`context.build` 只记了屏幕相关字段，挑卡的 trace 只在请求显式带
`context_trace=1` 时返回给调用方，不落库。

代价是真实发生过的：排查「旧记忆想不起来」时，我据此推断「V1 只有 3 个相关
名额，所以卡被挤掉了」——后来用对照实验推翻。**如果当时有这条记录，
那个错误判断根本不会出现。**

## 隐私边界（这个模块存在的另一半理由）

挑卡的原始 trace 里**带卡片摘要**（`skipped_sample[].summary`），
那是给实时调试用的、随响应回去、不落盘。落库的记录必须是内容无关的：

    记      计数、id、理由标签、耗时、走了哪套规则
    不记    摘要、正文、桶名、线索名、查询原文

查询只记**指纹**（sha256 前 12 位）——同一个问法能跨轮次对上号，
但复原不出原文。这与 `memory.search.called` 已有的做法一致。
"""
from __future__ import annotations

import collections
import hashlib
from typing import Any

#: 落库时保留的 id 上限。id 本身不含内容，但没必要无限长。
_MAX_IDS = 12

#: 拒绝理由是**闭集**（selector/relevance 里写死的那几种），可以安全落库。
#: 冒出集合外的值时原样截断保留 —— 那说明有人加了新理由，日志该跟上。
_KNOWN_REASONS = frozenset({
    "no_query_overlap",
    "no_index_topic_match",
    "sensitive_not_allowed_for_query",
    "below_min_score",
    "not_active",
    "duplicate",
})


def query_fingerprint(text: str) -> str:
    """查询的指纹 —— 能跨轮次对上号，复原不出原文。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _reason_of(item: Any) -> str:
    if not isinstance(item, dict):
        return "unknown"
    reason = str(item.get("reason") or "").strip()
    if not reason:
        return "unknown"
    return reason if reason in _KNOWN_REASONS else reason[:40]


def _ids_of(items: Any, limit: int = _MAX_IDS) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid:
            out.append(mid[:64])
        if len(out) >= limit:
            break
    return out


def injection_record(
    *,
    mode: str,
    query: str,
    candidate_pool: int,
    selection_trace: dict | None,
    injected_ids: list[str],
    cap: int,
    duration_ms: float | None = None,
) -> dict:
    """一轮自动注入的内容无关记录。

    `mode` 是「走了哪套挑法」—— 两套规则并存期间，这是最要紧的一个字段：
    没有它就分不清一条记录该不该拿来跟另一条比。

    `candidate_pool` 是**打分前**的候选数（受 50/200 窗口影响），
    `index_count` 是真正进了打分的数量。两者差很多就说明有卡被窗口截掉了 ——
    这正是「第 51 张之后的旧卡失联」那个问题的观测点。
    """
    trace = selection_trace if isinstance(selection_trace, dict) else {}
    rejected = trace.get("rejected_sample")
    if not isinstance(rejected, list):
        inner = trace.get("selector_trace")
        rejected = inner.get("skipped_sample") if isinstance(inner, dict) else []
    rejected = rejected if isinstance(rejected, list) else []

    selected = trace.get("selected") if isinstance(trace.get("selected"), list) else []

    # 分桶那套会在选中项上标 bucket（turning/recent/relevant），
    # 纯相关性那套没有 —— 缺就是缺，不要伪造成 0。
    buckets = collections.Counter(
        str(item.get("bucket") or "") for item in selected if isinstance(item, dict)
    )

    record: dict[str, Any] = {
        "mode": str(mode or "unknown"),
        "query_fingerprint": query_fingerprint(query),
        "query_empty": not str(query or "").strip(),
        "counts": {
            "candidate_pool": int(candidate_pool),
            "index_count": int(trace.get("index_count") or 0),
            "injected": len(injected_ids),
            "rejected_sampled": len(rejected),
            "cap": int(cap),
        },
        "injected_ids": _ids_of(injected_ids),
        "rejected_reasons": dict(collections.Counter(_reason_of(r) for r in rejected)),
    }
    # 过滤掉没有 bucket 标记的项；全都没标记就整个字段不出现 ——
    # 空字典是噪音，读日志的人会以为「有桶但都是 0」。
    labelled = {k: v for k, v in buckets.items() if k}
    if labelled:
        record["by_bucket"] = labelled
    if duration_ms is not None:
        record["dur_ms"] = round(float(duration_ms), 1)
    return record


def injection_summary(record: dict) -> str:
    """一句人话，给 admin 面板扫一眼用。"""
    counts = record.get("counts") or {}
    injected = counts.get("injected", 0)
    pool = counts.get("candidate_pool", 0)
    mode = record.get("mode", "?")
    if injected == 0:
        return f"注入 0 张（{mode}，候选 {pool}）"
    return f"注入 {injected} 张（{mode}，候选 {pool}）"


def assert_content_free(record: dict) -> None:
    """守卫：落库前确认这条记录里没有正文。

    调用方不必用它 —— 它是给测试用的。放在这里是因为「什么算内容」
    的判断属于这个模块的职责，不该散落在测试里。
    """
    banned = ("summary", "content", "bucket_name", "threads", "query", "title")
    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in banned:
                    raise AssertionError(f"落库记录里出现了内容字段：{path}{k}")
                walk(v, f"{path}{k}.")
        elif isinstance(node, list):
            for item in node:
                walk(item, path)
    walk(record)
