"""Shared low-level query helpers for Hosted Runtime V2 usage reporting."""

from __future__ import annotations

from typing import Protocol


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
