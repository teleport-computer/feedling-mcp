"""Memory lane summaries moved from tools/memory_pipeline_daily_report.py.

Aggregation belongs beside the admin read surface: reuse the producer-owned
failure vocabularies directly, without the Actions tool parsing backend ASTs.
Only counts and sanitized codes leave this module; user sets stay internal.
Presentation text, attention thresholds and Lark delivery remain in the tool.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import db
from model_api_runtime.v2 import jobs_store
# Reusing them keeps the grouping on the producer-owned vocabularies instead
# of a third hand-written list: error_contract owns each registered code's
# ``blame``; capture_failure owns the memory-lane "our side" / provider-setup /
# parse classifications.
from memory import capture_failure
from notices import agent_call_failure, catalog, error_contract


BEIJING = ZoneInfo("Asia/Shanghai")
LANES = ("capture", "dream")
ROUTES = ("resident", "model_api")
GROUP_USER = "user_account"
GROUP_PROVIDER = "model_service"
GROUP_OURS = "our_side"
GROUP_UNKNOWN = "unknown"
GROUP_ORDER = (GROUP_USER, GROUP_PROVIDER, GROUP_OURS, GROUP_UNKNOWN)
LANE_ROLLUP_PAGE_LIMIT = 500
LANE_ROLLUP_MAX_PAGES = 40

#: Sanitizer placeholders: the real cause was free text and got discarded, so
#: nobody can say whose problem it was.
_UNKNOWN_CODES = frozenset({"runtime_failed", "unknown", "no_code",
                            error_contract.UNREGISTERED_ERROR_CLASS})
#: One scope prefix is stripped before classification.
_SCOPE_PREFIXES = tuple(sorted(
    {f"{p}:" for p in agent_call_failure.AGENT_CALL_FAILED_PREFIXES}
    | {"extraction_failed:", "provider_call_failed:"}
))
#: Memory-lane internal kinds that are not in the public error registry.
#: Model output our parser/validator rejected is counted as ours: the fix
#: (prompt, parser, escape valve) lives on our side, not in the user's account.
_OUR_SIDE_EXTRA_PREFIXES = (
    "queue_timeout",
    "scheduled_lease_timeout",
    "stale_runtime_generation",
    "runtime_state_",
    "turns_halted",
    "foreground_chat_preempted",
    "database_",
    "capture_",
    "dream_",
    "migrate_",
    "extraction_memory",
    "memory_",
    "invalid_card",
    "semantic_validation",
    "missing_consolidations_list",
)
#: V2 extraction's short names for registered provider classes.
_REGISTRY_ALIASES = {
    "output_truncated": "provider_output_truncated",
    "empty_reply": "provider_empty_reply",
}
_BLAME_GROUPS = {
    "user_provider": GROUP_USER,
    "user_environment": GROUP_USER,
    "provider_transient": GROUP_PROVIDER,
    "system": GROUP_OURS,
}


def classify_failure_code(code: object) -> str:
    """Return which group has to act on one sanitized failure code."""
    raw = str(code or "").strip().lower()
    if not raw or raw in _UNKNOWN_CODES:
        return GROUP_UNKNOWN
    # A bare ``capture_agent_call_failed`` (no class) says only that the agent
    # call failed — not whose fault it was. Without this it would fall through
    # to the ``capture_`` our-side prefix below.
    if raw in agent_call_failure.AGENT_CALL_FAILED_PREFIXES:
        return GROUP_UNKNOWN
    kind = raw
    for prefix in _SCOPE_PREFIXES:
        if raw.startswith(prefix):
            kind = raw[len(prefix):]
            break
    if not kind or kind in _UNKNOWN_CODES:
        return GROUP_UNKNOWN
    setup_prefix = capture_failure.PROVIDER_SETUP_ACCOUNT_CODE + ":"
    if kind.startswith(setup_prefix):
        # Only the "go fix it in Settings" resolver errors are the user's;
        # key decryption / runtime token failures are ours.
        tail = kind[len(setup_prefix):]
        return (GROUP_USER if tail in capture_failure.PROVIDER_SETUP_USER_ERRORS
                else GROUP_OURS)
    # Before the registry: lease/watchdog codes contain "timeout" and would
    # otherwise read as the user's model service being down.
    if kind.startswith(capture_failure.OUR_SIDE_FAILURE_PREFIXES):
        return GROUP_OURS
    spec = error_contract.spec_for(_REGISTRY_ALIASES.get(kind, kind),
                                   public_only=False)
    if spec is not None:
        if spec.code in _UNKNOWN_CODES:
            return GROUP_UNKNOWN
        return _BLAME_GROUPS.get(spec.blame, GROUP_UNKNOWN)
    if kind.startswith(capture_failure.DETERMINISTIC_FAILURE_KINDS):
        return GROUP_OURS
    if kind.startswith(_OUR_SIDE_EXTRA_PREFIXES):
        return GROUP_OURS
    return GROUP_UNKNOWN


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

@dataclass
class CauseStats:
    attempts: int = 0
    users: set = field(default_factory=set)
    codes: Counter = field(default_factory=Counter)


@dataclass
class LaneRouteStats:
    lane: str
    route: str
    users: set = field(default_factory=set)
    #: Real completions (dream skips excluded).
    completed: int = 0
    #: Raw terminal non-successes (``failed`` + ``expired``), for reference.
    failed_raw: int = 0
    #: What the failure rate counts.
    operational: int = 0
    control: int = 0
    user_unavailable: int = 0
    user_unavailable_users: set = field(default_factory=set)
    user_unavailable_codes: Counter = field(default_factory=Counter)
    skipped: int = 0
    #: Users with at least one operational failure that day (受影响). A user
    #: whose night retried several times (dream: up to 4x since 2026-09-16)
    #: adds 4 to ``operational`` but 1 here — repeated failures never count a
    #: user twice, so this is the number to compare across retry-policy changes.
    failed_users: int = 0
    #: Users with operational failures and zero real completions (零成功).
    stuck_users: int = 0
    #: Operational failures only, grouped by who has to act.
    causes: dict = field(default_factory=lambda: {g: CauseStats() for g in GROUP_ORDER})
    partial: bool = False
    #: V1 cells whose outcome columns were unmeasured or not conserved; their
    #: raw failures were counted as operational (fail loud, never hide).
    unclassified: bool = False

    @property
    def attempts(self) -> int:
        return self.completed + self.operational

    @property
    def failure_rate(self) -> float | None:
        return (self.operational / self.attempts) if self.attempts else None


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _add_cause(stats: LaneRouteStats, group: str, uid: str, code: str, n: int) -> None:
    cause = stats.causes[group]
    cause.attempts += n
    cause.users.add(uid)
    cause.codes[code] += n


def aggregate_day(rows: Iterable[Mapping[str, Any]], *, lane: str, route: str,
                  day: str, v1_outcomes_measured: bool = True) -> LaneRouteStats:
    """Fold lane-rollup rows of one (lane, route, day) into counts.

    Per row, terminal non-successes (``failed`` + ``expired``; ``superseded`` is
    replacement by design) split into control / user-unavailable / operational:

    - V2 (``model_api``): by code, exactly ``jobs_store.terminal_outcome_class``
      — control codes and ``catalog.USER_UNAVAILABLE_V2_OUTCOME_CODES`` leave,
      everything else (unknown codes, expiries, timeouts) is operational.
    - V1 (``resident``): the frozen ``operational_failures`` /
      ``control_outcomes`` / ``user_unavailable`` columns, used only when
      ``v1_outcomes_measured`` and they add up to ``failed``; otherwise the raw
      failures count as operational and the stats are flagged ``unclassified``.

    The cause breakdown covers operational failures only and always sums to
    ``operational``. A V1 cell's codes cannot be told apart per status (a
    skipped job's reason sits next to a failed job's), so when the codes left
    after removing user-unavailable codes do not match the operational count
    exactly (and the cell also had control outcomes that could own some of
    them), that cell's operational failures are shown as 未知 ``unattributed``
    rather than guessed into a group. Failures without a recorded code are
    ``no_code``. "Stuck" users have operational failures and zero real
    completions that day; "affected" users have at least one operational
    failure (with or without a completion).
    """
    stats = LaneRouteStats(lane=lane, route=route)
    per_user: dict[str, list[int]] = {}
    v2_control = jobs_store.CONTROL_OUTCOME_CODES if route == "model_api" else frozenset()
    skip_lane = lane in db.LANE_ROLLUP_SKIP_DECLARED_LANES
    for row in rows:
        if (str(row.get("lane")) != lane or str(row.get("route")) != route
                or str(row.get("day")) != day):
            continue
        uid = str(row.get("user_id") or "")
        completed_raw = _int(row.get("completed"))
        failed = _int(row.get("failed")) + _int(row.get("expired"))
        if not completed_raw and not failed:
            continue
        frozen = row.get("frozen") is not False
        if not frozen:
            stats.partial = True
        skipped = min(completed_raw, _int(row.get("silent_declared"))) if skip_lane else 0
        completed = completed_raw - skipped
        stats.users.add(uid)
        stats.completed += completed
        stats.skipped += skipped
        stats.failed_raw += failed
        raw_codes = row.get("failure_codes") if isinstance(row.get("failure_codes"), Mapping) else {}
        codes = {str(code): _int(n) for code, n in raw_codes.items() if _int(n)}

        if route == "model_api":
            user_codes = {c: n for c, n in codes.items()
                          if c in catalog.USER_UNAVAILABLE_V2_OUTCOME_CODES}
            control = sum(n for c, n in codes.items() if c in v2_control)
            user = sum(user_codes.values())
            operational = max(0, failed - control - user)
            remaining = {c: n for c, n in codes.items()
                         if c not in v2_control and c not in user_codes}
        else:
            user_codes = {c: n for c, n in codes.items()
                          if c in catalog.USER_UNAVAILABLE_V1_REASONS}
            columns = [row.get(k) for k in
                       ("operational_failures", "control_outcomes", "user_unavailable")]
            measured = (frozen and v1_outcomes_measured
                        and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                                for v in columns)
                        and sum(columns) == failed)
            if measured:
                operational, control, user = columns
            else:
                operational, control, user = failed, 0, 0
                if failed:
                    stats.unclassified = True
            if sum(user_codes.values()) != user:
                # The columns are authoritative; codes that do not line up with
                # them (a row frozen before a code joined the set) stay put.
                user_codes = {}
            remaining = {c: n for c, n in codes.items() if c not in user_codes}

        stats.operational += operational
        stats.control += control
        stats.user_unavailable += user
        if user:
            stats.user_unavailable_users.add(uid)
            stats.user_unavailable_codes.update(user_codes)
            if sum(user_codes.values()) < user:
                stats.user_unavailable_codes["unattributed"] += user - sum(user_codes.values())

        coded = sum(remaining.values())
        # V2 codes were split exactly above. A V1 cell's leftover codes all
        # belong to operational failures only when there were no control
        # outcomes to share them with.
        attributable = coded == operational or (
            coded < operational and (route == "model_api" or not control))
        if operational and attributable:
            for code, n in remaining.items():
                _add_cause(stats, classify_failure_code(code), uid, code, n)
            if operational > coded:
                _add_cause(stats, GROUP_UNKNOWN, uid, "no_code", operational - coded)
        elif operational:
            _add_cause(stats, GROUP_UNKNOWN, uid,
                       "no_code" if not coded else "unattributed", operational)
        totals = per_user.setdefault(uid, [0, 0])
        totals[0] += completed
        totals[1] += operational
    stats.failed_users = sum(1 for _ok, bad in per_user.values() if bad)
    stats.stuck_users = sum(1 for ok, bad in per_user.values() if bad and not ok)
    return stats


def live_stuck_total(stuck: object) -> int | None:
    """Live stuck jobs; V1 rows count only recently created jobs (see
    the tool's ``LIVE_STUCK_JOBS_ATTENTION``). Older backends without ``recent_count``
    fall back to the full count."""
    if not isinstance(stuck, Mapping):
        return None
    rows = stuck.get("rows")
    if not isinstance(rows, list):
        return _int(stuck.get("total"))
    total = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("route")) == "resident" and "recent_count" in row:
            total += _int(row.get("recent_count"))
        else:
            total += _int(row.get("count"))
    return total


def serialize_stats(stats: LaneRouteStats) -> dict:
    """Expose counts instead of user identities, including each cause group."""
    result = {name: getattr(stats, name) for name in (
        "completed", "failed_raw", "operational", "control", "user_unavailable",
        "skipped", "failed_users", "stuck_users", "partial", "unclassified",
        "attempts", "failure_rate")}
    result["active_users"] = len(stats.users)
    result["user_unavailable_users"] = len(stats.user_unavailable_users)
    result["user_unavailable_codes"] = dict(stats.user_unavailable_codes)
    result["causes"] = {
        group: {"attempts": cause.attempts, "users": len(cause.users),
                "codes": dict(cause.codes)}
        for group, cause in stats.causes.items()
    }
    return result


def build_summary(sources: Mapping[str, Any], *, day: str) -> dict:
    """``sources`` = {"lane_rollup": {lane: payload}}.

    Each lane payload is the (page-merged) ``/v1/admin/lane-rollup`` response
    for ``since_day=previous_day&until_day=day&lane=<lane>``.
    """
    previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    today: dict = {}
    previous: dict = {}
    live_stuck: dict = {}
    incomplete: list[dict] = []
    all_coverage: dict = {}
    for lane in LANES:
        payload = (sources.get("lane_rollup") or {}).get(lane)
        if not isinstance(payload, Mapping):
            live_stuck[lane] = None
            incomplete.append({"lane": lane, "kind": "missing_lane"})
            for route in ROUTES:
                today[(lane, route)] = LaneRouteStats(lane=lane, route=route, partial=True)
                previous[(lane, route)] = LaneRouteStats(lane=lane, route=route, partial=True)
            continue
        rows = list(payload.get("rows") or []) + list(payload.get("today_partial") or [])
        coverage = payload.get("coverage") or {}
        all_coverage[lane] = coverage
        outcomes_from = str((coverage.get("resident") or {}).get("outcomes_from") or "")
        for route in ROUTES:
            cur = aggregate_day(rows, lane=lane, route=route, day=day,
                                v1_outcomes_measured=bool(outcomes_from) and outcomes_from <= day)
            prev = aggregate_day(rows, lane=lane, route=route, day=previous_day,
                                 v1_outcomes_measured=(bool(outcomes_from)
                                                       and outcomes_from <= previous_day))
            through = str((coverage.get(route) or {}).get("through_day") or "")
            if not through or through < day:
                cur.partial = True
            today[(lane, route)] = cur
            previous[(lane, route)] = prev
        live_stuck[lane] = live_stuck_total(payload.get("stuck"))
        if payload.get("truncated"):
            incomplete.append({"lane": lane, "kind": "truncated"})
    for (lane, route), stats in today.items():
        if not isinstance((sources.get("lane_rollup") or {}).get(lane), Mapping):
            continue  # already reported as "没取到数据"
        if stats.partial:
            incomplete.append({"lane": lane, "route": route, "kind": "partial"})
        elif stats.unclassified:
            incomplete.append({"lane": lane, "route": route, "kind": "unclassified"})
    return {
        "day": day, "previous_day": previous_day,
        "cells": {
            f"{lane}/{route}": {"day": serialize_stats(stats),
                               "previous": serialize_stats(previous[(lane, route)])}
            for (lane, route), stats in today.items()
        },
        "live_stuck": live_stuck, "incomplete": incomplete, "coverage": all_coverage,
    }


#: A frozen cell's full identity (the table's primary key). Offset pages are
#: merged by it, so a backend whose ORDER BY does not reach this key cannot
#: double-count a cell that slides across a page boundary.
LANE_ROLLUP_CELL_KEY = ("user_id", "day", "route", "lane", "enqueue_source",
                        "access_path", "mode_source")


def fetch_lane_rollup(*, lane: str, since_day: str, until_day: str) -> dict:
    merged: dict | None = None
    seen: set = set()
    offset = 0

    def add(rows) -> None:
        for row in rows or []:
            key = tuple(str(row.get(k)) for k in LANE_ROLLUP_CELL_KEY)
            if key in seen:
                continue
            seen.add(key)
            merged["rows"].append(row)

    for _ in range(LANE_ROLLUP_MAX_PAGES):
        page = db.admin_lane_rollup(
            lane=lane, since_day=since_day, until_day=until_day,
            limit=LANE_ROLLUP_PAGE_LIMIT, offset=offset,
        )
        if merged is None:
            merged = dict(page)
            merged["rows"] = []
        add(page.get("rows"))
        returned = _int((page.get("pagination") or {}).get("returned"))
        total = _int((page.get("pagination") or {}).get("total"))
        offset += returned
        if returned < LANE_ROLLUP_PAGE_LIMIT or offset >= total:
            return merged
    assert merged is not None
    merged["truncated"] = True
    return merged


def default_day(now: datetime | None = None) -> str:
    current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    return (current.date() - timedelta(days=1)).isoformat()


def read_summary(*, day: str) -> dict:
    previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    sources = {
        "lane_rollup": {
            lane: fetch_lane_rollup(lane=lane, since_day=previous_day, until_day=day)
            for lane in LANES
        },
    }
    return build_summary(sources, day=day)
