"""Shared low-level query helpers for Hosted Runtime V2 usage reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RollupPartition:
    """Disjoint Shanghai-day source selection for one usage request."""

    rollup_days: tuple[date, ...]
    raw_days: tuple[date, ...]
    retained_from: date | None = None
    retention_pending_from: date | None = None
    retention_truncated: bool = False
    retention_partial_reason: str | None = None


class UsageQueryLike(Protocol):
    start_at_utc: object
    end_at_utc: object
    user_id: str | None
    lane: str | None
    provider: str | None
    model: str | None
    completeness: str


def metric_filter_sql(
    query: UsageQueryLike,
    *,
    alias: str = "m",
    include_dimensions: bool = True,
) -> tuple[str, tuple[object, ...]]:
    """Return one bound-parameter metric cohort used by every report section."""

    if alias not in {"m", "metric"}:
        raise ValueError("unsupported SQL alias")
    clauses = [
        f"{alias}.created_at >= %s",
        f"{alias}.created_at < %s",
    ]
    params: list[object] = [query.start_at_utc, query.end_at_utc]
    if query.user_id:
        clauses.append(f"COALESCE({alias}.user_id, 'unknown') = %s")
        params.append(query.user_id)
    if include_dimensions:
        for field, value in (
            ("lane", query.lane),
            ("provider", query.provider),
            ("model", query.model),
        ):
            if value:
                clauses.append(
                    f"COALESCE(NULLIF({alias}.{field}, ''), 'unknown') = %s"
                )
                params.append(value)
        if query.completeness == "metered":
            clauses.append(f"{alias}.usage_reported_calls > 0")
        elif query.completeness == "unknown":
            clauses.append(
                f"{alias}.usage_reported_calls < {alias}.model_calls"
            )
    return " AND ".join(clauses), tuple(params)


def has_dimension_filter(query: UsageQueryLike) -> bool:
    """Whether a fleet-wide activated-user denominator would be misleading."""

    return bool(
        query.lane
        or query.provider
        or query.model
        or query.completeness != "all"
    )


def rollup_partition(
    query: UsageQueryLike,
    *,
    dirty_from_day: date | None = None,
    dirty_through_day: date | None = None,
) -> RollupPartition | None:
    """Return exact, disjoint rollup/raw days or ``None`` for raw fallback.

    Completeness prefixes overlap by design; selecting one never subtracts the
    others.  Callers may still use a narrow raw auxiliary for a metric (such as
    unknown-and-metered turn count) that isn't represented by one prefix.
    """

    if getattr(query, "timezone", None) != "Asia/Shanghai":
        return None
    start = query.start_at_utc
    end = query.end_at_utc
    if not isinstance(start, datetime) or not isinstance(end, datetime) or start >= end:
        return None
    first = start.astimezone(SHANGHAI).date()
    last = (end - timedelta(microseconds=1)).astimezone(SHANGHAI).date()
    rollup_days: list[date] = []
    raw_days: list[date] = []
    day = first
    while day <= last:
        day_start = datetime.combine(day, time.min, tzinfo=SHANGHAI).astimezone(
            timezone.utc
        )
        day_end = datetime.combine(
            day + timedelta(days=1), time.min, tzinfo=SHANGHAI
        ).astimezone(timezone.utc)
        dirty = bool(
            dirty_from_day is not None
            and dirty_through_day is not None
            and dirty_from_day <= day <= dirty_through_day
        )
        if start <= day_start and day_end <= end and not dirty:
            rollup_days.append(day)
        else:
            raw_days.append(day)
        day += timedelta(days=1)
    return RollupPartition(tuple(rollup_days), tuple(raw_days))
