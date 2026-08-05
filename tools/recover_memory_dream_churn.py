#!/usr/bin/env python3
"""Recover original cards after the 2026-08 Runtime V2 Dream churn incident.

This tool is intentionally operator-only, single-user, dry-run by default, and
never deletes rows. It restores cards superseded during the selected incident
window and soft-retires Dream-authored churn cards while preserving every
ciphertext envelope for audit/reversal.

Examples:
    python tools/recover_memory_dream_churn.py \
      --env test --user-id usr_f10e9fd5ebd63ea0

    python tools/recover_memory_dream_churn.py \
      --env test --user-id usr_f10e9fd5ebd63ea0 --apply \
      --confirm-user-id usr_f10e9fd5ebd63ea0 --confirm-churn-stopped

    python tools/recover_memory_dream_churn.py \
      --env prod --user-id usr_81a0645d0d9c0746 --since 2026-07-29
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - operator environment
    raise SystemExit("psycopg is required: pip install 'psycopg[binary]'")


REPO_ROOT = Path(__file__).resolve().parent.parent
INCIDENT_REASON = "incident_recovery:memory_dream_churn_2026-08-03"


def _dsn(env: str) -> str:
    key = f"{env.upper()}_DATABASE_URL"
    if os.environ.get(key, "").strip():
        return os.environ[key].strip()
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} is required (environment or {env_file})")


def _source(doc: dict[str, Any]) -> str:
    return str(doc.get("source") or doc.get("capture_mode") or "unknown").strip() or "unknown"


def _is_dream(doc: dict[str, Any]) -> bool:
    return any(
        str(doc.get(key) or "").strip() == "memory_dream"
        for key in ("source", "capture_mode")
    )


def _is_active(doc: dict[str, Any]) -> bool:
    return (
        str(doc.get("status") or "active").strip().lower() == "active"
        and doc.get("is_archived") is not True
        and not str(doc.get("archived_at") or "").strip()
    )


def _iso_datetime(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise argparse.ArgumentTypeError("ISO date/time is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date/time: {raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _at_or_after(value: Any, since: datetime) -> bool:
    try:
        parsed = _iso_datetime(str(value or ""))
    except argparse.ArgumentTypeError:
        return False
    return parsed >= since


def summarize(docs: list[dict[str, Any]]) -> dict[str, Any]:
    sources = Counter(_source(doc) for doc in docs)
    active_sources = Counter(_source(doc) for doc in docs if _is_active(doc))
    return {
        "total": len(docs),
        "active": sum(1 for doc in docs if _is_active(doc)),
        "superseded": sum(
            1 for doc in docs if str(doc.get("status") or "").lower() == "superseded"
        ),
        "archived": sum(
            1
            for doc in docs
            if doc.get("is_archived") is True or str(doc.get("archived_at") or "").strip()
        ),
        "by_source": dict(sorted(sources.items())),
        "active_by_source": dict(sorted(active_sources.items())),
    }


def _change_day(entry: dict[str, Any]) -> str:
    """UTC day (YYYY-MM-DD) of one memory_changes row; '' when unparseable."""
    raw = str(entry.get("ts") or "").strip()
    try:
        return _iso_datetime(raw).date().isoformat()
    except argparse.ArgumentTypeError:
        return ""


def change_log_report(changes: list[dict[str, Any]]) -> dict[str, Any]:
    """只读侦查:硬删流水 + supersede 按天分布(usr_a40e 复原调查,2026-08-06)。

    回答两个执行前必答的问题:①用户那 5 次硬删发生在什么时候、删的是哪些卡
    (是不是他自己清掉了墓碑);②哪些晚上在吃卡、每晚吃几张(污染起点不用猜)。
    只输出 id/时间/动作元数据,绝不输出卡片内容字段。
    ⚠️ ``reason`` 刻意不输出:它是模型/客户端写的自由文本(≤500 字,
    worker._memory_tool_actions 透传模型给的 a.reason),完全可能含卡片内容,
    而这份报告会整体打进 GitHub Actions 日志(codex2 gatekeep P1,2026-08-05)。
    这里只给 content-free 的 ``has_reason`` 存在位。"""
    hard_deletes = [
        {
            "ts": str(entry.get("ts") or ""),
            "memory_id": str(entry.get("memory_id") or ""),
            "has_reason": bool(str(entry.get("reason") or "").strip()),
        }
        for entry in changes
        if str(entry.get("action") or "").strip().lower() == "delete"
    ]
    supersede_by_day: dict[str, dict[str, Any]] = {}
    for entry in changes:
        if str(entry.get("action") or "").strip().lower() != "supersede":
            continue
        day = _change_day(entry) or "unknown"
        bucket = supersede_by_day.setdefault(
            day, {"count": 0, "retired_cards": 0, "by_capture_mode": {}}
        )
        bucket["count"] += 1
        bucket["retired_cards"] += len(entry.get("supersedes") or [])
        mode = str(entry.get("capture_mode") or "unknown").strip() or "unknown"
        bucket["by_capture_mode"][mode] = bucket["by_capture_mode"].get(mode, 0) + 1
    return {
        "hard_deletes": hard_deletes,
        "supersede_by_day": dict(sorted(supersede_by_day.items())),
    }


def window_created_by_source(
    docs: list[dict[str, Any]], *, since: datetime | None
) -> dict[str, Any]:
    """窗内新建卡按来源分组(id+created_at)。resident_patch(agent 徒手 memory-patch)
    不在本工具的退休范围内,但必须被看见 —— 决定要不要扩范围靠这份清单。"""
    grouped: dict[str, list[dict[str, str]]] = {}
    for doc in docs:
        if since is not None and not _at_or_after(doc.get("created_at"), since):
            continue
        grouped.setdefault(_source(doc), []).append({
            "id": str(doc.get("id") or ""),
            "created_at": str(doc.get("created_at") or ""),
            "status": str(doc.get("status") or "active"),
        })
    return {source: rows for source, rows in sorted(grouped.items())}


def plan_rows(
    docs: list[dict[str, Any]], updated: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """逐卡计划清单(只列会被改动的行;只有元数据,无内容字段)。"""
    rows: list[dict[str, str]] = []
    for before, after in zip(docs, updated):
        if before == after:
            continue
        planned = (
            "restore"
            if str(after.get("status") or "").lower() == "active"
            else "retire"
        )
        rows.append({
            "id": str(after.get("id") or ""),
            "planned": planned,
            "source": _source(after),
            "created_at": str(after.get("created_at") or ""),
            "was_superseded_by": str(before.get("superseded_by") or ""),
        })
    return rows


def recovery_plan(
    docs: list[dict[str, Any]], *, now_iso: str, since: datetime | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    restored = 0
    retired_dream = 0
    untouched = 0
    updated: list[dict[str, Any]] = []
    for original in docs:
        doc = dict(original)
        dream_created_in_window = _is_dream(doc) and (
            since is None or _at_or_after(doc.get("created_at"), since)
        )
        superseded_in_window = (
            str(doc.get("status") or "").strip().lower() == "superseded"
            and (since is None or _at_or_after(doc.get("archived_at"), since))
        )
        if dream_created_in_window:
            before = dict(doc)
            doc["status"] = "superseded"
            doc["is_archived"] = True
            doc["archived_at"] = now_iso
            doc["archive_reason"] = INCIDENT_REASON
            doc["updated_at"] = now_iso
            if doc != before:
                retired_dream += 1
            else:
                untouched += 1
        elif superseded_in_window:
            doc["status"] = "active"
            doc["is_archived"] = False
            doc.pop("superseded_by", None)
            doc.pop("archived_at", None)
            doc.pop("archive_reason", None)
            doc["updated_at"] = now_iso
            doc["recovered_at"] = now_iso
            doc["recovery_reason"] = INCIDENT_REASON
            restored += 1
        else:
            untouched += 1
        updated.append(doc)
    return updated, {
        "restore_non_dream_superseded": restored,
        "retire_memory_dream": retired_dream,
        "untouched": untouched,
        "hard_deletes": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("test", "prod"), required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--since",
        type=_iso_datetime,
        default=None,
        help="Only undo churn at or after this ISO date/time (inclusive).",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-user-id", default="")
    parser.add_argument("--confirm-churn-stopped", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.apply and args.confirm_user_id != args.user_id:
        raise SystemExit("--confirm-user-id must exactly match --user-id")
    if args.apply and not args.confirm_churn_stopped:
        raise SystemExit("--apply requires --confirm-churn-stopped")

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with psycopg.connect(_dsn(args.env), row_factory=dict_row) as conn:
        with conn.transaction():
            user = conn.execute(
                "SELECT 1 FROM users WHERE user_id = %s", (args.user_id,)
            ).fetchone()
            if user is None:
                raise SystemExit(f"user not found in {args.env}: {args.user_id}")
            rows = conn.execute(
                "SELECT moment_id, doc FROM memory_moments "
                "WHERE user_id = %s ORDER BY occurred_at, moment_id FOR UPDATE",
                (args.user_id,),
            ).fetchall()
            docs = [dict(row["doc"] or {}) for row in rows]
            # 变更流水只读:硬删时间线 + supersede 按天(执行前的证据面)。
            change_rows = conn.execute(
                "SELECT doc FROM user_logs "
                "WHERE user_id = %s AND stream = 'memory_changes' ORDER BY seq",
                (args.user_id,),
            ).fetchall()
            changes = [dict(row["doc"] or {}) for row in change_rows]
            updated, plan = recovery_plan(docs, now_iso=now_iso, since=args.since)
            report = {
                "environment": args.env,
                "user_id": args.user_id,
                "mode": "apply" if args.apply else "dry-run",
                "since": (
                    args.since.isoformat().replace("+00:00", "Z")
                    if args.since is not None
                    else None
                ),
                "before": summarize(docs),
                "plan": plan,
                "plan_rows": plan_rows(docs, updated),
                "change_log": change_log_report(changes),
                "window_created_by_source": window_created_by_source(
                    docs, since=args.since
                ),
                "after": summarize(updated),
            }
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            if not args.apply:
                return 0
            for row, doc in zip(rows, updated):
                if doc == dict(row["doc"] or {}):
                    continue
                conn.execute(
                    "UPDATE memory_moments SET doc = %s "
                    "WHERE user_id = %s AND moment_id = %s",
                    (Jsonb(doc), args.user_id, row["moment_id"]),
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
