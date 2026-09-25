#!/usr/bin/env python3
"""Proactive-wake open rate, reported per model family (T723 E).

Open rate = spoke / (spoke + model-chosen silence) over heartbeat jobs. Only
turns where the model actually chose count: failed jobs, system-written sleeps
(provider circuit open, a reply the system emptied), superseded and expired
jobs are excluded and itemised, never dropped.

Always read it per model family: on prod 2026-09-17..24 one family was 46% of
decisions at 7%, so a working prompt change (33% -> 44% elsewhere) showed as a
flat fleet total. The model comes from the job's ``agent.model.call.done``
trace events, joined on ``trace_id`` (the model-call events carry no job_id).

Read-only. Output is counts and model labels only, never user ids or text.

Usage::

    FEEDLING_REPORT_DSN=postgresql://... python tools/wake_open_rate_report.py \\
        --since 2026-09-17T04:06Z --until 2026-09-24T04:06Z [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping

# ``wake_result='sleep'`` rows the system wrote, not the model's stay_silent.
# Pinned against every system sleep producer in the worker by
# tests/test_wake_open_rate_report.py; a new one must be added here.
SYSTEM_SLEEP_REASONS = {
    "provider_circuit_open": "circuit",
    "empty_visible_reply_suppressed": "empty_reply_suppressed",
}
CIRCUIT_SLEEP_REASON = "provider_circuit_open"
LANE = "heartbeat"

# Order matters: the first matching needle names the family.
_FAMILIES = (
    ("glm", "glm"),
    ("gemini", "gemini"),
    ("deepseek", "deepseek"),
    ("claude", "claude"),
    ("grok", "grok"),
    ("gpt", "gpt"),
    ("minimax", "minimax"),
)

_SQL = """
with j as (
  select a.status, a.wake_result, coalesce(a.wake_result_reason, '') reason, a.trace_id
  from agent_jobs a
  where a.lane = %(lane)s and a.created_at >= %(since)s and a.created_at < %(until)s
),
m as (
  select distinct on (t.trace_id) t.trace_id, coalesce(t.model, '') model
  from trace_events t
  where t.ts >= %(since)s - interval '1 hour' and t.ts < %(until)s + interval '1 hour'
    and t.type = 'agent.model.call.done'
    and t.trace_id in (select trace_id from j where trace_id is not null and trace_id <> '')
  order by t.trace_id, t.ts desc
)
select j.status, j.wake_result, j.reason, m.model
from j left join m on m.trace_id = j.trace_id
"""


def model_family(model: str | None) -> str:
    """Stable, content-free family label for a provider model string."""
    if model is None:
        return "no_trace"
    lowered = str(model).lower()
    for needle, family in _FAMILIES:
        if needle in lowered:
            return family
    return "other" if lowered.strip() else "no_trace"


def classify(status: str, wake_result: str | None, reason: str) -> str:
    """One of spoke / silent / excluded:<why>."""
    if wake_result == "sleep":
        system = SYSTEM_SLEEP_REASONS.get(reason)
        return f"excluded:{system}" if system else "silent"
    if status == "completed" and wake_result is None:
        return "spoke"
    if status in {"failed", "superseded", "expired"}:
        return f"excluded:{status}"
    return "excluded:other"


def summarize(rows: Iterable[tuple[str, str | None, str, str | None]]) -> dict:
    families: dict[str, dict[str, int]] = {}
    total: dict[str, int] = {}
    for status, wake_result, reason, model in rows:
        kind = classify(status, wake_result, reason or "")
        for bucket in (families.setdefault(model_family(model), {}), total):
            bucket[kind] = bucket.get(kind, 0) + 1

    def _render(counts: Mapping[str, int]) -> dict:
        spoke, silent = counts.get("spoke", 0), counts.get("silent", 0)
        chosen = spoke + silent
        return {
            "spoke": spoke,
            "silent": silent,
            "open_rate": round(spoke / chosen, 4) if chosen else None,
            "excluded": {k.split(":", 1)[1]: v for k, v in sorted(counts.items()) if k.startswith("excluded:")},
        }

    ordered = sorted(families.items(), key=lambda kv: -(kv[1].get("spoke", 0) + kv[1].get("silent", 0)))
    return {"total": _render(total), "by_family": {name: _render(c) for name, c in ordered}}


def fetch(conn, *, since: str, until: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(_SQL, {"lane": LANE, "since": since, "until": until})
        return list(cur.fetchall())


def render_text(report: Mapping) -> str:
    def _pct(rate):
        return "-" if rate is None else f"{rate * 100:.1f}%"

    lines = ["family      open    spoke  silent  excluded"]
    for name, row in [("TOTAL", report["total"]), *report["by_family"].items()]:
        excluded = ", ".join(f"{k}={v}" for k, v in row["excluded"].items()) or "-"
        lines.append(f"{name:<10} {_pct(row['open_rate']):>6} {row['spoke']:>7} {row['silent']:>7}  {excluded}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    dsn = os.environ.get("FEEDLING_REPORT_DSN")
    if not dsn:
        print("FEEDLING_REPORT_DSN is required (read-only DSN)", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(dsn) as conn:
        report = summarize(fetch(conn, since=args.since, until=args.until))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
