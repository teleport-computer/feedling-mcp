"""Retention sweep for the PerceptKit tables.

**This is the only thing in this package that permanently deletes user data,
and it is dry-run by default.** Nothing schedules it. Turning it on is a
deliberate act by a person who has read a dry-run report first.

That default is not caution for its own sake. An earlier attempt at this was
pulled from the release precisely because its retention numbers would have
deleted data irreversibly, and a retention bug is invisible from the outside:
the system keeps working, users just quietly lose history nobody notices
until someone asks a question the data can no longer answer.

## Where the numbers come from

The manifest, and nowhere else. Each signal declares how long details live
and how long aggregates live -- two separate numbers, because the usual shape
is short details with permanent aggregates. A signal with no declared
retention is **skipped, not defaulted**: guessing a number here deletes real
data on a guess.

## Four things this sweep will not do

Delete aggregates for a signal whose aggregates are permanent.

Delete dedupe identities alongside the details they guard. Once details are
gone the identity is the only thing standing between a replayed report and a
permanent aggregate counted twice, with no way to undo it.

Delete anything for a signal missing from the manifest -- including signals
that were removed from it. A signal that disappears from the manifest is far
more likely to be a mistake than an instruction to erase its history.

Delete more than ``max_rows`` in one round. An unbounded DELETE against a
large table takes locks for long enough to be its own incident.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from perceptkit.manifest.types import PERMANENT, SignalDefinition

#: Rows removed per table per round. A sweep that needs more comes back for
#: the rest next round; one that takes an unbounded lock is its own incident.
DEFAULT_MAX_ROWS = 2000


@dataclass
class SweepPlan:
    """What a sweep would remove, per signal. A dry run returns only this."""

    observations: dict[str, int] = field(default_factory=dict)
    aggregates: dict[str, int] = field(default_factory=dict)
    #: ``(signal, why)`` -- signals deliberately left alone.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: True once rows have actually been removed.
    applied: bool = False

    @property
    def total(self) -> int:
        return sum(self.observations.values()) + sum(self.aggregates.values())


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


def plan_sweep(
    conn: Any,
    signals: Mapping[str, SignalDefinition],
    *,
    now: datetime,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> SweepPlan:
    """Count what a sweep would remove. **Reads only.**

    Run this first, read the numbers, and only then consider ``run_sweep``.
    """
    plan = SweepPlan()
    for key in sorted(signals):
        sig = signals[key]

        if not sig.stores_history:
            plan.skipped.append((key, "keeps no history; nothing to sweep"))
            continue
        if sig.history_retention_days == PERMANENT:
            plan.skipped.append((key, "details are permanent"))
        else:
            cutoff = _cutoff(now, sig.history_retention_days)
            plan.observations[key] = _count(
                conn,
                "SELECT count(*) FROM (SELECT 1 FROM perceptkit_observation "
                "WHERE signal = %s AND occurred_at < %s LIMIT %s) t",
                (key, cutoff, max_rows),
            )

        agg_days = sig.effective_aggregate_retention_days
        if agg_days == PERMANENT:
            plan.skipped.append((key, "aggregates are permanent"))
            continue
        agg_cutoff = _cutoff(now, agg_days).date()
        plan.aggregates[key] = _count(
            conn,
            "SELECT count(*) FROM (SELECT 1 FROM perceptkit_daily_aggregate "
            "WHERE signal = %s AND local_date < %s LIMIT %s) t",
            (key, agg_cutoff, max_rows),
        )
    return plan


def run_sweep(
    conn: Any,
    signals: Mapping[str, SignalDefinition],
    *,
    now: datetime,
    max_rows: int = DEFAULT_MAX_ROWS,
    dry_run: bool = True,
) -> SweepPlan:
    """Remove expired rows. ``dry_run=True`` by default -- it counts and stops.

    Passing ``dry_run=False`` deletes user data and cannot be undone. Do it
    from a place where a person decided to, not from a scheduler default.
    """
    plan = plan_sweep(conn, signals, now=now, max_rows=max_rows)
    if dry_run:
        return plan

    for key in sorted(plan.observations):
        sig = signals[key]
        # Details only. The dedupe identities in perceptkit_dedupe_identity are
        # deliberately never touched here: once the details are gone they are
        # the only thing keeping a replayed report from counting twice into a
        # permanent aggregate, and that cannot be undone.
        _exec(
            conn,
            "DELETE FROM perceptkit_observation WHERE ctid IN ("
            "  SELECT ctid FROM perceptkit_observation"
            "   WHERE signal = %s AND occurred_at < %s LIMIT %s)",
            (key, _cutoff(now, sig.history_retention_days), max_rows),
        )
    for key in sorted(plan.aggregates):
        sig = signals[key]
        _exec(
            conn,
            "DELETE FROM perceptkit_daily_aggregate WHERE ctid IN ("
            "  SELECT ctid FROM perceptkit_daily_aggregate"
            "   WHERE signal = %s AND local_date < %s LIMIT %s)",
            (key, _cutoff(now, sig.effective_aggregate_retention_days).date(),
             max_rows),
        )
    plan.applied = True
    return plan


def _count(conn: Any, sql: str, params: tuple) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _exec(conn: Any, sql: str, params: tuple) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def format_plan(plan: SweepPlan) -> str:
    """A dry-run report meant to be read by a person before anything is deleted."""
    lines = ["Retention sweep -- dry run" if not plan.applied else
             "Retention sweep -- APPLIED (rows are gone)", ""]
    for label, counts in (("observations", plan.observations),
                          ("daily aggregates", plan.aggregates)):
        rows = {k: v for k, v in counts.items() if v}
        if rows:
            lines.append(f"{label}:")
            lines += [f"  {k:24} {v}" for k, v in sorted(rows.items())]
    if not plan.total:
        lines.append("nothing is expired")
    if plan.skipped:
        lines += ["", "left alone:"]
        lines += [f"  {k:24} {why}" for k, why in plan.skipped]
    return "\n".join(lines)


__all__ = ["DEFAULT_MAX_ROWS", "SweepPlan", "plan_sweep", "run_sweep",
           "format_plan"]
