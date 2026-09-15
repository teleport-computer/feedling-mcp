#!/usr/bin/env python3
"""Daily Lark summary of memory pipeline health (capture + dream, V1 + V2).

Why this exists: in 2026-09 capture was silently broken for 3+ days (22-58
users with zero successful captures) and nobody knew until a user complained.
Every piece of the pipeline "failed normally" with backoff and no alert. This
tool turns the existing, content-free admin read surfaces into ONE short
Chinese group message per day, grouped by who has to act on each failure.

Where it runs: ``.github/workflows/memory-pipeline-daily.yml`` (scheduled
GitHub Actions). It only *reads* the prod admin API and posts to the existing
signed Lark bot. The CVM never holds the webhook secret and the backend gains
no outbound dependency; a dead backend still produces a "取数失败" message
instead of silence.

Data sources (both admin-gated, content-free by construction):

* ``GET /v1/admin/lane-rollup`` — frozen per-user per-lane cells for one
  Beijing calendar day (``db.admin_lane_rollup``). ``route`` is ``resident``
  (V1: hosted resident + self-hosted VPS) or ``model_api`` (V2 pooled worker).
  ``failure_codes`` are already sanitized to ``^[a-z0-9_:-]{1,120}$``.
  Dream skips (garden too small) come from the same cells' ``silent_declared``.

Failure semantics are the admin lane views' (``admin.data_track``): the
failure rate is ``operational failures / (real completions + operational
failures)``. Control outcomes (V1 ``skipped``, V2 ``capture_disabled`` /
``dream_disabled`` / ``turns_halted`` …) and failures proven to be the user's
own account (``notices.catalog`` user-unavailable sets) are shown on their own
informational line and never page anyone. V1 reads the frozen outcome columns;
V2 classifies ``failure_codes`` exactly like ``jobs_store.terminal_outcome_class``.

Day boundary: the rollup freezes **Beijing** (Asia/Shanghai) days, so the
report covers the previous Beijing day, not a UTC day. Recomputing UTC days
would need a second aggregate over raw job tables — the exact unbounded read
the rollup exists to retire.

Output never contains user ids, message ids or any free text: counts, lane /
route labels and sanitized failure codes only.

Any unexpected error (a changed response shape, a bug here) still posts a
content-free "没生成出来" notice naming only the exception type, and exits
non-zero.

Usage::

    # offline: render from a saved fixture, print only
    python tools/memory_pipeline_daily_report.py --fixture f.json --dry-run
    # live read, print only (no Lark send)
    FEEDLING_API_URL=https://test-api.feedling.app FEEDLING_ADMIN_TOKEN=... \\
        python tools/memory_pipeline_daily_report.py --dry-run
    # what the workflow runs
    python tools/memory_pipeline_daily_report.py

Secrets come from the environment only (never argv, so they cannot land in
process listings or workflow logs): ``FEEDLING_ADMIN_TOKEN``,
``LARK_BOT_WEBHOOK``, ``LARK_BOT_SECRET`` (optional; when set the message is
signed exactly like the deploy notices in ``.github/workflows/ci.yml``).
"""
from __future__ import annotations

import argparse
import ast
import base64
import functools
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

# Both modules are stdlib-only at import time. Reusing them keeps the grouping
# on the producer-owned vocabularies instead of a third hand-written list:
# error_contract owns each registered code's ``blame``; capture_failure owns
# the memory-lane "our side" / provider-setup / parse classifications.
from memory import capture_failure  # noqa: E402
from notices import agent_call_failure, catalog, error_contract  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent / "backend"


BEIJING = ZoneInfo("Asia/Shanghai")
LANES = ("capture", "dream")
ROUTES = ("resident", "model_api")
LANE_LABELS = {"capture": "落卡 capture", "dream": "做梦 dream"}
ROUTE_LABELS = {"resident": "V1 resident", "model_api": "V2 model_api"}

GROUP_USER = "user_account"
GROUP_PROVIDER = "model_service"
GROUP_OURS = "our_side"
GROUP_UNKNOWN = "unknown"
GROUP_ORDER = (GROUP_USER, GROUP_PROVIDER, GROUP_OURS, GROUP_UNKNOWN)
GROUP_LABELS = {
    # Operational failures whose code blames an account/config problem but is
    # not proven to be the user's own (those are on the "不算失败" line).
    GROUP_USER: "账号/配置类",
    GROUP_PROVIDER: "模型服务",
    GROUP_OURS: "我们这边",
    GROUP_UNKNOWN: "未知",
}

# ---- attention thresholds (message is marked 需要关注 when any rule fires) --
#
# 🔴 Provenance: every number below is an INITIAL value proposed by Claude Code
# on 2026-09-15 when this report was written. None was derived from a measured
# prod baseline; tune them after observing a few weeks of real reports. Each
# can be overridden without a code change through the environment variable
# named next to it (the workflow passes the same-named GitHub repository
# variable; an unset/empty or unparsable value keeps the default).

def _threshold(name: str, default: float, cast: type = int) -> Any:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        print(f"ignoring invalid {name}; using default {default}", file=sys.stderr)
        return default
    if value < 0:
        print(f"ignoring negative {name}; using default {default}", file=sys.stderr)
        return default
    return value


#: Users on one lane/route with failures and zero successes that day.
STUCK_USERS_ATTENTION = _threshold("MEMORY_REPORT_STUCK_USERS", 10)
#: Distinct users hit by our-side failures on one lane/route.
OUR_SIDE_USERS_ATTENTION = _threshold("MEMORY_REPORT_OUR_SIDE_USERS", 5)
#: Failure-rate rules only apply with at least this many terminal attempts,
#: so a 1-of-2 day on a quiet lane does not page anyone.
MIN_ATTEMPTS_FOR_RATE = _threshold("MEMORY_REPORT_MIN_ATTEMPTS_FOR_RATE", 20)
#: Absolute failure rate that is abnormal on its own.
FAILURE_RATE_ATTENTION = _threshold("MEMORY_REPORT_FAILURE_RATE", 0.5, float)
#: Day-over-day increase, in percentage points.
FAILURE_RATE_JUMP_PP = _threshold("MEMORY_REPORT_FAILURE_RATE_JUMP_PP", 15.0, float)
#: Active users dropping below this share of the previous day. A scheduler
#: that stops enqueuing produces no failures at all — only this rule sees it.
ACTIVE_DROP_RATIO = _threshold("MEMORY_REPORT_ACTIVE_DROP_RATIO", 0.5, float)
ACTIVE_DROP_MIN_PREVIOUS = _threshold("MEMORY_REPORT_ACTIVE_DROP_MIN_PREVIOUS", 10)
#: Non-terminal jobs past their deadline right now (lane-rollup ``stuck``).
#: V1 rows count only jobs created within the endpoint's
#: ``resident_recent_hours`` (24h): an older non-terminal V1 job is an orphan
#: left by a consumer that went away, not something stuck today.
LIVE_STUCK_JOBS_ATTENTION = _threshold("MEMORY_REPORT_LIVE_STUCK_JOBS", 20)

#: The override variables, in one place for the workflow wiring test.
THRESHOLD_ENV_VARS = (
    "MEMORY_REPORT_STUCK_USERS",
    "MEMORY_REPORT_OUR_SIDE_USERS",
    "MEMORY_REPORT_MIN_ATTEMPTS_FOR_RATE",
    "MEMORY_REPORT_FAILURE_RATE",
    "MEMORY_REPORT_FAILURE_RATE_JUMP_PP",
    "MEMORY_REPORT_ACTIVE_DROP_RATIO",
    "MEMORY_REPORT_ACTIVE_DROP_MIN_PREVIOUS",
    "MEMORY_REPORT_LIVE_STUCK_JOBS",
)

LANE_ROLLUP_PAGE_LIMIT = 500
LANE_ROLLUP_MAX_PAGES = 40
HTTP_TIMEOUT_SEC = 60

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

def _frozenset_literal(path: Path, name: str) -> frozenset:
    """Read a module-level ``NAME = frozenset({...literals})`` without importing.

    The workflow runs with the stdlib only, and the owning modules import the
    database driver. Reading the literal keeps one source of truth; a changed
    shape raises and the report says it could not be generated.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            value = node.value
            if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id == "frozenset" and len(value.args) == 1):
                return frozenset(ast.literal_eval(value.args[0]))
    raise LookupError(f"{name} literal not found in {path.name}")


@functools.lru_cache(maxsize=None)
def v2_control_outcome_codes() -> frozenset:
    """``jobs_store.CONTROL_OUTCOME_CODES`` (V2 control outcomes)."""
    return _frozenset_literal(_BACKEND / "model_api_runtime" / "v2" / "jobs_store.py",
                              "CONTROL_OUTCOME_CODES")


@functools.lru_cache(maxsize=None)
def skip_declared_lanes() -> frozenset:
    """``db.LANE_ROLLUP_SKIP_DECLARED_LANES``: lanes whose ``silent_declared``
    counts completions that never ran (dream: garden too small)."""
    return _frozenset_literal(_BACKEND / "db.py", "LANE_ROLLUP_SKIP_DECLARED_LANES")


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
    completions that day.
    """
    stats = LaneRouteStats(lane=lane, route=route)
    per_user: dict[str, list[int]] = {}
    v2_control = v2_control_outcome_codes() if route == "model_api" else frozenset()
    skip_lane = lane in skip_declared_lanes()
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
    stats.stuck_users = sum(1 for ok, bad in per_user.values() if bad and not ok)
    return stats


def live_stuck_total(stuck: object) -> int | None:
    """Live stuck jobs; V1 rows count only recently created jobs (see
    ``LIVE_STUCK_JOBS_ATTENTION``). Older backends without ``recent_count``
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


@dataclass
class Report:
    day: str
    previous_day: str
    today: dict          # (lane, route) -> LaneRouteStats
    previous: dict       # (lane, route) -> LaneRouteStats
    live_stuck: dict     # lane -> int | None
    incomplete: list     # human-readable data caveats
    attention: list = field(default_factory=list)


def build_report(sources: Mapping[str, Any], *, day: str) -> Report:
    """``sources`` = {"lane_rollup": {lane: payload}}.

    Each lane payload is the (page-merged) ``/v1/admin/lane-rollup`` response
    for ``since_day=previous_day&until_day=day&lane=<lane>``.
    """
    previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    today: dict = {}
    previous: dict = {}
    live_stuck: dict = {}
    incomplete: list[str] = []
    for lane in LANES:
        payload = (sources.get("lane_rollup") or {}).get(lane)
        if not isinstance(payload, Mapping):
            live_stuck[lane] = None
            incomplete.append(f"{LANE_LABELS[lane]} 没取到数据")
            for route in ROUTES:
                today[(lane, route)] = LaneRouteStats(lane=lane, route=route, partial=True)
                previous[(lane, route)] = LaneRouteStats(lane=lane, route=route, partial=True)
            continue
        rows = list(payload.get("rows") or []) + list(payload.get("today_partial") or [])
        coverage = payload.get("coverage") or {}
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
            incomplete.append(f"{LANE_LABELS[lane]} 行数超过翻页上限，只统计了一部分")
    for (lane, route), stats in today.items():
        if not isinstance((sources.get("lane_rollup") or {}).get(lane), Mapping):
            continue  # already reported as "没取到数据"
        if stats.partial:
            incomplete.append(
                f"{LANE_LABELS[lane]} · {ROUTE_LABELS[route]} 当天统计还没冻结，"
                "数字可能偏少、失败原因记为未知")
        elif stats.unclassified:
            incomplete.append(
                f"{LANE_LABELS[lane]} · {ROUTE_LABELS[route]} 失败分类缺失，"
                "按原始失败计（可能含跳过/用户账号问题）")
    report = Report(day=day, previous_day=previous_day, today=today,
                    previous=previous, live_stuck=live_stuck, incomplete=incomplete)
    report.attention = evaluate_attention(report)
    return report


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{round(rate * 100)}%"


def evaluate_attention(report: Report) -> list[str]:
    reasons: list[str] = []
    for lane in LANES:
        for route in ROUTES:
            cur = report.today[(lane, route)]
            prev = report.previous[(lane, route)]
            where = f"{LANE_LABELS[lane].split()[0]} · {ROUTE_LABELS[route]}"
            if cur.stuck_users >= STUCK_USERS_ATTENTION:
                reasons.append(f"{where}：完全卡死 {cur.stuck_users} 人"
                               f"（有失败、零成功，阈值 {STUCK_USERS_ATTENTION}）")
            ours = cur.causes[GROUP_OURS]
            if len(ours.users) >= OUR_SIDE_USERS_ATTENTION:
                reasons.append(f"{where}：我们这边的故障影响 {len(ours.users)} 人"
                               f"（阈值 {OUR_SIDE_USERS_ATTENTION}）")
            rate = cur.failure_rate
            if rate is not None and cur.attempts >= MIN_ATTEMPTS_FOR_RATE:
                prev_rate = prev.failure_rate
                if rate >= FAILURE_RATE_ATTENTION:
                    reasons.append(f"{where}：失败率 {_pct(rate)}")
                elif (prev_rate is not None and prev.attempts >= MIN_ATTEMPTS_FOR_RATE
                      and (rate - prev_rate) * 100 >= FAILURE_RATE_JUMP_PP):
                    reasons.append(f"{where}：失败率 {_pct(rate)}，前一天 {_pct(prev_rate)}")
            if (len(prev.users) >= ACTIVE_DROP_MIN_PREVIOUS and not cur.partial
                    and len(cur.users) < len(prev.users) * ACTIVE_DROP_RATIO):
                reasons.append(f"{where}：活跃用户 {len(cur.users)} 人，"
                               f"前一天 {len(prev.users)} 人（可能整条线没在跑）")
    for lane in LANES:
        n = report.live_stuck.get(lane)
        if n is not None and n >= LIVE_STUCK_JOBS_ATTENTION:
            reasons.append(f"{LANE_LABELS[lane].split()[0]}：此刻有 {n} 个任务卡着没结束"
                           f"（阈值 {LIVE_STUCK_JOBS_ATTENTION}）")
    if report.incomplete:
        reasons.append("数据不完整，见文末说明")
    return reasons


def _top_codes(codes: Mapping[str, int]) -> str:
    return "、".join(f"{code} {n}" for code, n in
                    sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))[:2])


def _cause_line(stats: LaneRouteStats) -> str:
    parts = []
    for group in GROUP_ORDER:
        cause = stats.causes[group]
        if not cause.attempts:
            continue
        parts.append(f"{GROUP_LABELS[group]} {cause.attempts} 次/{len(cause.users)} 人"
                     f"（{_top_codes(cause.codes)}）")
    return "；".join(parts)


def _not_counted_line(stats: LaneRouteStats) -> str:
    parts = []
    if stats.user_unavailable:
        parts.append(f"确认是用户自己账号问题 {stats.user_unavailable} 次/"
                     f"{len(stats.user_unavailable_users)} 人"
                     f"（{_top_codes(stats.user_unavailable_codes)}）")
    if stats.control:
        parts.append(f"跳过/关闭等控制结果 {stats.control} 次")
    if stats.skipped:
        parts.append(f"花园太小跳过 {stats.skipped} 次")
    return "；".join(parts)


def render_message(report: Report) -> str:
    lines = []
    head = "[需要关注]" if report.attention else "[正常]"
    lines.append(f"{head} 记忆管线日报 {report.day}（北京时间）")
    if report.attention:
        lines.extend(f"- {reason}" for reason in report.attention)
    for lane in LANES:
        lines.append("")
        lines.append(LANE_LABELS[lane])
        for route in ROUTES:
            cur = report.today[(lane, route)]
            prev = report.previous[(lane, route)]
            if not cur.users and not prev.users:
                lines.append(f"  {ROUTE_LABELS[route]}：当天没有任务")
                continue
            lines.append(f"  {ROUTE_LABELS[route]}：活跃 {len(cur.users)} 人｜"
                         f"成功 {cur.completed}｜失败 {cur.operational}"
                         f"（{_pct(cur.failure_rate)}，前一天 {_pct(prev.failure_rate)}）｜"
                         f"完全卡死 {cur.stuck_users} 人")
            causes = _cause_line(cur)
            if causes:
                lines.append(f"    失败原因：{causes}")
            not_counted = _not_counted_line(cur)
            if not_counted:
                lines.append(f"    不算失败：{not_counted}")
    stuck = [f"{LANE_LABELS[lane].split()[0]} {report.live_stuck[lane]}"
             for lane in LANES if report.live_stuck.get(lane) is not None]
    if stuck:
        lines.append("")
        lines.append("此刻卡着没结束的任务：" + " · ".join(stuck))
    if report.incomplete:
        lines.append("")
        lines.append("数据说明：" + "；".join(report.incomplete))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Lark
# --------------------------------------------------------------------------- #

def lark_sign(timestamp: str, secret: str) -> str:
    """Lark custom-bot signature, byte-for-byte the ci.yml deploy notice.

    The key is ``"{timestamp}\\n{secret}"`` and the message is empty — that is
    Lark's documented scheme, not a mistake.
    """
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(
        hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    ).decode("utf-8")


def lark_payload(text: str, *, secret: str = "", timestamp: str = "") -> dict:
    if not secret:
        return {"msg_type": "text", "content": {"text": text}}
    ts = timestamp or str(int(time.time()))
    return {"timestamp": ts, "sign": lark_sign(ts, secret),
            "msg_type": "text", "content": {"text": text}}


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — fixed https endpoints


def post_to_lark(webhook: str, payload: Mapping[str, Any], *,
                 opener: Opener = _default_opener) -> None:
    """POST and require Lark's own success code (HTTP 200 alone is not success:
    a bad signature comes back as 200 with ``code != 0``).

    Success means a JSON object that explicitly carries ``code`` and/or
    ``StatusCode`` (the older webhook field), every one present being the
    integer ``0``. Anything else raises ``RuntimeError`` so ``deliver`` reports
    the post as failed: an empty body, ``{}``, ``[]``, a missing code or
    ``"0"`` used to count as delivered (or crash outside ``deliver``) while
    nobody received the report (Codex review 2026-09-15).
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with opener(request, HTTP_TIMEOUT_SEC) as response:
        raw = response.read()
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("lark_response_not_json") from exc
    if not isinstance(parsed, Mapping):
        raise RuntimeError("lark_invalid_response")
    codes = [parsed[key] for key in ("code", "StatusCode") if key in parsed]
    if not codes or any(type(code) is not int for code in codes):
        raise RuntimeError("lark_invalid_response")
    rejected = next((code for code in codes if code != 0), None)
    if rejected is not None:
        raise RuntimeError(f"lark_rejected:code={rejected}")


# --------------------------------------------------------------------------- #
# Admin API reads
# --------------------------------------------------------------------------- #

class FetchError(RuntimeError):
    """A content-free description of which read failed (never a body)."""


def _get_json(base_url: str, path: str, params: Mapping[str, Any], token: str, *,
              opener: Opener) -> dict:
    url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"X-Admin-Token": token,
                                                   "Accept": "application/json"})
    try:
        with opener(request, HTTP_TIMEOUT_SEC) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{path} HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{path} {type(exc).__name__}") from None
    except ValueError:
        raise FetchError(f"{path} invalid_json") from None


#: A frozen cell's full identity (the table's primary key). Offset pages are
#: merged by it, so a backend whose ORDER BY does not reach this key cannot
#: double-count a cell that slides across a page boundary.
LANE_ROLLUP_CELL_KEY = ("user_id", "day", "route", "lane", "enqueue_source",
                        "access_path", "mode_source")


def fetch_lane_rollup(base_url: str, token: str, *, lane: str, since_day: str,
                      until_day: str, opener: Opener = _default_opener) -> dict:
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
        page = _get_json(base_url, "/v1/admin/lane-rollup", {
            "lane": lane, "since_day": since_day, "until_day": until_day,
            "limit": LANE_ROLLUP_PAGE_LIMIT, "offset": offset,
        }, token, opener=opener)
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


def fetch_sources(base_url: str, token: str, *, day: str,
                  opener: Opener = _default_opener) -> dict:
    previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return {
        "lane_rollup": {
            lane: fetch_lane_rollup(base_url, token, lane=lane,
                                    since_day=previous_day, until_day=day,
                                    opener=opener)
            for lane in LANES
        },
    }


def failure_message(day: str, reason: str, *, stage: str = "取数失败") -> str:
    return (f"[需要关注] 记忆管线日报 {day}（北京时间）没生成出来\n"
            f"- {stage}：{reason}\n"
            "- 今天没有数字，不代表管线正常；请看 GitHub Actions 的 memory-pipeline-daily 运行记录")


def default_day(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(BEIJING)
    return (current.date() - timedelta(days=1)).isoformat()


def main(argv: list[str] | None = None, *, opener: Opener = _default_opener,
         environ: Mapping[str, str] | None = None, out=None) -> int:
    env = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", default="",
                        help="Beijing day YYYY-MM-DD (default: yesterday in Beijing)")
    parser.add_argument("--base-url", default="",
                        help="API base URL (default: $FEEDLING_API_URL)")
    parser.add_argument("--fixture", default="",
                        help="Render from a saved sources JSON instead of the API")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the message instead of posting to Lark")
    args = parser.parse_args(argv)

    day = args.day or default_day()
    try:
        date.fromisoformat(day)
    except ValueError:
        parser.error("--day must be YYYY-MM-DD")

    webhook = str(env.get("LARK_BOT_WEBHOOK") or "").strip()
    secret = str(env.get("LARK_BOT_SECRET") or "").strip()

    def deliver(text: str) -> int:
        if args.dry_run:
            print(text, file=out)
            return 0
        if not webhook:
            print("LARK_BOT_WEBHOOK is not configured", file=sys.stderr)
            return 2
        try:
            post_to_lark(webhook, lark_payload(text, secret=secret), opener=opener)
        except RuntimeError as exc:
            print(f"lark post failed: {exc}", file=sys.stderr)
            return 3
        except urllib.error.HTTPError as exc:
            print(f"lark post failed: HTTP {exc.code}", file=sys.stderr)
            return 3
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"lark post failed: {type(exc).__name__}", file=sys.stderr)
            return 3
        print("posted memory pipeline daily report", file=out)
        return 0

    try:
        if args.fixture:
            sources = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        else:
            base_url = args.base_url or str(env.get("FEEDLING_API_URL") or "").strip()
            token = str(env.get("FEEDLING_ADMIN_TOKEN") or "").strip()
            if not base_url or not token:
                raise FetchError("FEEDLING_API_URL / FEEDLING_ADMIN_TOKEN 未配置")
            sources = fetch_sources(base_url, token, day=day, opener=opener)
    except FetchError as exc:
        # Silence is the failure mode this report exists to end: say we could
        # not measure, and still exit non-zero so the workflow run turns red.
        print(f"fetch failed: {exc}", file=sys.stderr)
        deliver(failure_message(day, str(exc)))
        return 1
    except (OSError, ValueError) as exc:  # unreadable/invalid --fixture
        print(f"fixture unreadable: {type(exc).__name__}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — see below
        return _report_crash(day, "取数出错", exc, deliver)
    try:
        text = render_message(build_report(sources, day=day))
    except Exception as exc:  # noqa: BLE001
        # A response shape change or a bug here must not die silently: the
        # workflow log is not where anyone looks. Only the exception type is
        # sent — its message could quote response data.
        return _report_crash(day, "生成报告出错", exc, deliver)
    return deliver(text)


def _report_crash(day: str, stage: str, exc: BaseException,
                  deliver: Callable[[str], int]) -> int:
    print(f"{stage}: {type(exc).__name__}", file=sys.stderr)
    deliver(failure_message(day, type(exc).__name__, stage=stage))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
