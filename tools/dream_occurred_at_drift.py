#!/usr/bin/env python3
"""Dream 改写导致的 occurred_at 偏差:沿 supersedes 链追回原始值,给分布。

背景(T040/T063):V2 dream 把后继卡的 occurred_at 设成"最近一条聊天的时间",
而不是被合并源卡的原始事件时间。修复(521451a3)已把它改成"取源卡里最新的
occurred_at",但**存量卡还是错的**。

好消息:supersede 不删源卡 —— 源卡保留原始 occurred_at 并写 superseded_by,
后继卡写 supersedes[]。所以真实时间能沿链追回来。

判据(和修复后的实现保持一致):
    一张后继卡的"应有 occurred_at" = 它所有源卡里**最新**的那个
    源卡本身如果也是后继卡(多代链),先递归解出它的应有值

数据来源:GET /v1/admin/users/<uid>/memory-card-metadata(只读、无正文)
输出:只有计数、天数、id —— 不含任何卡正文。

自检:`python3 tools/dream_occurred_at_drift.py --selftest`
(同一套判据也被 tests/test_dream_occurred_at_drift.py 以 pytest 形式钉住) 用合成数据验证链式逻辑,
不需要任何网络或密钥。**先证明量具,再拿它去量真数据。**
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import ssl
import urllib.request
from datetime import datetime, timezone

UNRESOLVED = "unresolved"


def parse_ts(raw: str) -> datetime | None:
    """后端写过至少四种形状,全都要吃(T040 查实)。

    2026-08-13T21:43:04Z / 2026-08-13T17:27:18.618643(无时区微秒) /
    2026-06-18T00:00:00 / 2026-08-13(date-only)。
    无时区按 UTC 读 —— 与后端比较这些字符串的口径一致。
    """
    value = str(raw or "").strip()
    if not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_original(card_id: str, by_id: dict, memo: dict, seen: frozenset = frozenset()):
    """返回 (datetime|None, status)。status: ok / no_sources / broken_chain / cycle。

    多代链:源卡本身若也有 supersedes,递归解它的应有值。
    """
    if card_id in memo:
        return memo[card_id]
    if card_id in seen:
        return (None, "cycle")
    card = by_id.get(card_id)
    if card is None:
        return (None, "broken_chain")
    sources = [s for s in (card.get("supersedes") or []) if s]
    if not sources:
        # 叶子:它自己的 occurred_at 就是原始值
        result = (parse_ts(card.get("occurred_at")), "ok" if parse_ts(card.get("occurred_at")) else "no_sources")
        memo[card_id] = result
        return result
    best, status = None, "ok"
    for sid in sources:
        src = by_id.get(sid)
        if src is None:
            status = "broken_chain"
            continue
        if src.get("supersedes"):
            got, sub = resolve_original(sid, by_id, memo, seen | {card_id})
            if sub in ("broken_chain", "cycle") and status == "ok":
                status = sub
        else:
            got = parse_ts(src.get("occurred_at"))
        if got and (best is None or got > best):
            best = got
    if best is None and status == "ok":
        status = "no_sources"
    memo[card_id] = (best, status)
    return memo[card_id]


def _percentile(sorted_days: list[float], q: float) -> float:
    """线性插值分位数。小样本时 int(n*q)-1 会退化成 min(实测 n=2 时 p90 < median)。"""
    if not sorted_days:
        raise ValueError("empty")
    if len(sorted_days) == 1:
        return sorted_days[0]
    pos = q * (len(sorted_days) - 1)
    low = int(pos)
    frac = pos - low
    if low + 1 >= len(sorted_days):
        return sorted_days[-1]
    return sorted_days[low] + (sorted_days[low + 1] - sorted_days[low]) * frac


def analyse(cards: list[dict]) -> dict:
    by_id = {c["id"]: c for c in cards if c.get("id")}
    successors = [c for c in cards if (c.get("supersedes") or [])]
    memo: dict = {}
    drifts, recoverable, broken, nots = [], 0, 0, 0
    for card in successors:
        original, status = resolve_original(card["id"], by_id, memo)
        current = parse_ts(card.get("occurred_at"))
        if original is None or current is None:
            if status in ("broken_chain", "cycle"):
                broken += 1
            else:
                nots += 1
            continue
        recoverable += 1
        drifts.append(((current - original).total_seconds() / 86400.0, card["id"]))
    days = sorted(d for d, _ in drifts)
    out = {
        "cards_total": len(cards),
        "successors": len(successors),
        "recoverable": recoverable,
        "broken_chain": broken,
        "unresolvable": nots,
        "drift_days": {},
    }
    if days:
        out["drift_days"] = {
            "median": round(statistics.median(days), 2),
            "p90": round(_percentile(days, 0.90), 2),
            "max": round(max(days), 2),
            "min": round(min(days), 2),
            "over_30d": sum(1 for d in days if d > 30),
            "over_365d": sum(1 for d in days if d > 365),
            "worst_ids": [cid for _, cid in sorted(drifts, reverse=True)[:5]],
        }
    return out


def _ssl_context() -> ssl.SSLContext:
    """macOS 的 python.org 安装不带系统根证书,默认会 CERTIFICATE_VERIFY_FAILED
    (curl 却是好的,所以很容易误判成"端点坏了")。有 certifi 就用它。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_all(host: str, uid: str, token: str, page: int = 500) -> list[dict]:
    cards, offset = [], 0
    while True:
        url = f"{host}/v1/admin/users/{uid}/memory-card-metadata?limit={page}&offset={offset}"
        req = urllib.request.Request(url, headers={"X-Admin-Token": token})
        with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as resp:
            body = json.load(resp)
        batch = body.get("cards") or []
        cards += batch
        # 分页信息在 `pagination` 下,不在顶层。读顶层会拿到 None -> total=0 ->
        # 第一页就 return,**静默只取一页**(实测 2026-08-15:真实形状是
        # {"user_id":..,"cards":[..],"pagination":{"limit","offset","total","has_more"}})。
        page_info = body.get("pagination") or {}
        total = int(page_info.get("total") or 0)
        offset += len(batch)
        if not batch or offset >= total:
            if total and len(cards) < total:
                raise RuntimeError(f"分页未取全:{len(cards)}/{total} —— 拒绝用残缺样本出数")
            return cards


def selftest() -> int:
    """合成数据验证链式逻辑 —— 不碰网络。"""
    cards = [
        # 单代:后继 s1 的时间被写成 8/14,源卡真实是 6/18
        {"id": "src1", "occurred_at": "2026-06-18T00:00:00", "supersedes": [], "superseded_by": "s1"},
        {"id": "src2", "occurred_at": "2026-07-01T10:00:00Z", "supersedes": [], "superseded_by": "s1"},
        {"id": "s1", "occurred_at": "2026-08-14T13:00:00Z", "supersedes": ["src1", "src2"], "superseded_by": "s2"},
        # 二代:s2 合并了 s1(它自己也是后继)+ 一张 date-only 源卡
        {"id": "src3", "occurred_at": "2026-08-05", "supersedes": [], "superseded_by": "s2"},
        {"id": "s2", "occurred_at": "2026-08-14T14:00:00Z", "supersedes": ["s1", "src3"]},
        # 断链:源卡不在返回集里
        {"id": "s3", "occurred_at": "2026-08-14T15:00:00Z", "supersedes": ["ghost"]},
        # 非后继卡不该被统计
        {"id": "plain", "occurred_at": "2026-08-01T00:00:00Z", "supersedes": []},
    ]
    got = analyse(cards)
    checks = [
        ("后继卡计数", got["successors"], 3),
        ("可追回", got["recoverable"], 2),
        ("断链", got["broken_chain"], 1),
        # s1: 8/14 13:00 vs 源里最新 7/1 10:00 -> 44.1 天
        # s2: 8/14 14:00 vs max(s1 应有值 7/1, src3 8/5) = 8/5 00:00 -> 9.58 天
        ("中位偏差天数", got["drift_days"]["median"], 26.85),
    ]
    ok = True
    for label, actual, expect in checks:
        flag = "✅" if abs(actual - expect) < 0.05 else "❌"
        if flag == "❌":
            ok = False
        print(f"  {flag} {label:<14} 实得 {actual}  期望 {expect}")
    print("\n完整输出:", json.dumps(got, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--host", default="https://api.feedling.app")
    ap.add_argument("--user")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    token = os.environ.get("FEEDLING_ADMIN_KEY", "")
    if not token or not args.user:
        print("需要 FEEDLING_ADMIN_KEY 和 --user", file=sys.stderr)
        return 2
    cards = fetch_all(args.host, args.user, token)
    print(json.dumps({"user": args.user[:12] + "…", **analyse(cards)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
