"""Normalized query contract for the Admin Hosted V2 Usage report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_PRESET = "30d"
_PRESET_DAYS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}
_COMPLETENESS = frozenset({"all", "metered", "unknown"})


class QueryArgs(Protocol):
    def get(self, key: str, default=None): ...


@dataclass(frozen=True)
class UsageQuery:
    """One immutable cohort shared by every Usage report section."""

    start_at_utc: datetime
    end_at_utc: datetime
    timezone: str = DEFAULT_TIMEZONE
    user_id: str | None = None
    lane: str | None = None
    provider: str | None = None
    model: str | None = None
    completeness: Literal["all", "metered", "unknown"] = "all"
    preset: Literal["24h", "7d", "30d", "90d", "custom"] = DEFAULT_PRESET
    start_date: str | None = None
    end_date: str | None = None


def metric_filter_sql(
    query: UsageQuery,
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


def has_dimension_filter(query: UsageQuery) -> bool:
    """Whether a fleet-wide activated-user denominator would be misleading."""

    return bool(
        query.lane
        or query.provider
        or query.model
        or query.completeness != "all"
    )


def _text(args: QueryArgs, key: str) -> str:
    return str(args.get(key) or "").strip()


def _optional_text(args: QueryArgs, key: str) -> str | None:
    return _text(args, key) or None


def _normalized_now(now_utc: datetime | None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def _timezone(raw: str) -> tuple[str, ZoneInfo]:
    name = raw or DEFAULT_TIMEZONE
    try:
        return name, ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)


def _iso_date(raw: str) -> date:
    parsed = date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("date must use YYYY-MM-DD")
    return parsed


def _custom_bounds(
    start_raw: str,
    end_raw: str,
    display_tz: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    try:
        start_day = _iso_date(start_raw)
        end_day = _iso_date(end_raw)
        exclusive_end_day = end_day + timedelta(days=1)
    except (OverflowError, ValueError):
        return None
    inclusive_days = (end_day - start_day).days + 1
    if inclusive_days < 1 or inclusive_days > 366:
        return None
    try:
        start_local = datetime.combine(start_day, time.min, tzinfo=display_tz)
        end_local = datetime.combine(exclusive_end_day, time.min, tzinfo=display_tz)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    if start_utc >= end_utc:
        return None
    return start_utc, end_utc


def parse_usage_query(
    args: QueryArgs,
    now_utc: datetime | None = None,
) -> UsageQuery:
    """Normalize untrusted query strings into one content-free report cohort."""

    now = _normalized_now(now_utc)
    timezone_name, display_tz = _timezone(_text(args, "timezone"))
    raw_preset = _text(args, "preset").lower()
    start_raw = _text(args, "start_date")
    end_raw = _text(args, "end_date")
    wants_custom = raw_preset == "custom" or (
        not raw_preset and bool(start_raw) and bool(end_raw)
    )

    preset = raw_preset if raw_preset in _PRESET_DAYS else DEFAULT_PRESET
    start_date = None
    end_date = None
    if wants_custom:
        custom_bounds = _custom_bounds(start_raw, end_raw, display_tz)
        if custom_bounds is not None:
            start_at_utc, end_at_utc = custom_bounds
            preset = "custom"
            start_date = start_raw
            end_date = end_raw
        else:
            start_at_utc = now - timedelta(days=_PRESET_DAYS[DEFAULT_PRESET])
            end_at_utc = now
            preset = DEFAULT_PRESET
    else:
        start_at_utc = now - timedelta(days=_PRESET_DAYS[preset])
        end_at_utc = now

    completeness = _text(args, "completeness").lower()
    if completeness not in _COMPLETENESS:
        completeness = "all"

    return UsageQuery(
        start_at_utc=start_at_utc,
        end_at_utc=end_at_utc,
        timezone=timezone_name,
        user_id=_optional_text(args, "user_id"),
        lane=_optional_text(args, "lane"),
        provider=_optional_text(args, "provider"),
        model=_optional_text(args, "model"),
        completeness=completeness,
        preset=preset,
        start_date=start_date,
        end_date=end_date,
    )
