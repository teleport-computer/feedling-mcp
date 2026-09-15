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
* ``GET /v1/admin/memory-dream-jobs?status=completed`` — V2 dream jobs, used
  only to count completed-but-skipped dreams (garden too small).

Day boundary: the rollup freezes **Beijing** (Asia/Shanghai) days, so the
report covers the previous Beijing day, not a UTC day. Recomputing UTC days
would need a second aggregate over raw job tables — the exact unbounded read
the rollup exists to retire.

Output never contains user ids, message ids or any free text: counts, lane /
route labels and sanitized failure codes only.

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
import base64
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
from notices import agent_call_failure, error_contract  # noqa: E402


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
    GROUP_USER: "用户自己的账号/配置",
    GROUP_PROVIDER: "模型服务",
    GROUP_OURS: "我们这边",
    GROUP_UNKNOWN: "未知",
}

# ---- attention thresholds (message is marked 需要关注 when any rule fires) --
#: Users on one lane/route with failures and zero successes that day.
STUCK_USERS_ATTENTION = 10
#: Distinct users hit by our-side failures on one lane/route.
OUR_SIDE_USERS_ATTENTION = 5
#: Failure-rate rules only apply with at least this many terminal attempts,
#: so a 1-of-2 day on a quiet lane does not page anyone.
MIN_ATTEMPTS_FOR_RATE = 20
#: Absolute failure rate that is abnormal on its own.
FAILURE_RATE_ATTENTION = 0.5
#: Day-over-day increase, in percentage points.
FAILURE_RATE_JUMP_PP = 15.0
#: Active users dropping below this share of the previous day. A scheduler
#: that stops enqueuing produces no failures at all — only this rule sees it.
ACTIVE_DROP_RATIO = 0.5
ACTIVE_DROP_MIN_PREVIOUS = 10
#: Non-terminal jobs past their deadline right now (lane-rollup ``stuck``).
LIVE_STUCK_JOBS_ATTENTION = 20

LANE_ROLLUP_PAGE_LIMIT = 500
LANE_ROLLUP_MAX_PAGES = 40
DREAM_JOBS_PAGE_LIMIT = 500
DREAM_JOBS_MAX_PAGES = 10
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
    completed: int = 0
    failed: int = 0
    stuck_users: int = 0
    causes: dict = field(default_factory=lambda: {g: CauseStats() for g in GROUP_ORDER})
    partial: bool = False

    @property
    def attempts(self) -> int:
        return self.completed + self.failed

    @property
    def failure_rate(self) -> float | None:
        return (self.failed / self.attempts) if self.attempts else None


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def aggregate_day(rows: Iterable[Mapping[str, Any]], *, lane: str, route: str,
                  day: str) -> LaneRouteStats:
    """Fold lane-rollup rows of one (lane, route, day) into counts.

    ``failed`` includes ``expired`` (V2 jobs that never ran before their
    deadline); ``superseded`` is replacement by design and is not counted.
    Failures without a recorded code land in 未知 as ``no_code`` so the cause
    breakdown always sums to ``failed``.
    """
    stats = LaneRouteStats(lane=lane, route=route)
    per_user: dict[str, list[int]] = {}
    for row in rows:
        if (str(row.get("lane")) != lane or str(row.get("route")) != route
                or str(row.get("day")) != day):
            continue
        uid = str(row.get("user_id") or "")
        completed = _int(row.get("completed"))
        failed = _int(row.get("failed")) + _int(row.get("expired"))
        if not completed and not failed:
            continue
        if row.get("frozen") is False:
            stats.partial = True
        stats.users.add(uid)
        stats.completed += completed
        stats.failed += failed
        totals = per_user.setdefault(uid, [0, 0])
        totals[0] += completed
        totals[1] += failed
        codes = row.get("failure_codes") or {}
        coded = 0
        if isinstance(codes, Mapping):
            for code, count in codes.items():
                n = _int(count)
                if not n:
                    continue
                coded += n
                cause = stats.causes[classify_failure_code(code)]
                cause.attempts += n
                cause.users.add(uid)
                cause.codes[str(code)] += n
        missing = failed - coded
        if missing > 0:
            cause = stats.causes[GROUP_UNKNOWN]
            cause.attempts += missing
            cause.users.add(uid)
            cause.codes["no_code"] += missing
    stats.stuck_users = sum(1 for ok, bad in per_user.values() if bad and not ok)
    return stats


def count_dream_skips(jobs: Iterable[Mapping[str, Any]], day: str) -> int:
    """Completed V2 dreams whose kernel judged the garden too small that day."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(),
                             tzinfo=BEIJING)
    end = start + timedelta(days=1)
    n = 0
    for job in jobs:
        if str(job.get("status")) != "completed" or job.get("outcome") != "skipped":
            continue
        finished = _parse_ts(job.get("finished_at"))
        if finished is not None and start <= finished < end:
            n += 1
    return n


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Report:
    day: str
    previous_day: str
    today: dict          # (lane, route) -> LaneRouteStats
    previous: dict       # (lane, route) -> LaneRouteStats
    live_stuck: dict     # lane -> int | None
    dream_skips: int | None
    incomplete: list     # human-readable data caveats
    attention: list = field(default_factory=list)


def build_report(sources: Mapping[str, Any], *, day: str) -> Report:
    """``sources`` = {"lane_rollup": {lane: payload}, "dream_jobs": payload|None}.

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
        for route in ROUTES:
            cur = aggregate_day(rows, lane=lane, route=route, day=day)
            prev = aggregate_day(rows, lane=lane, route=route, day=previous_day)
            through = str((coverage.get(route) or {}).get("through_day") or "")
            if not through or through < day:
                cur.partial = True
            today[(lane, route)] = cur
            previous[(lane, route)] = prev
        stuck = payload.get("stuck")
        live_stuck[lane] = _int(stuck.get("total")) if isinstance(stuck, Mapping) else None
        if payload.get("truncated"):
            incomplete.append(f"{LANE_LABELS[lane]} 行数超过翻页上限，只统计了一部分")
    for (lane, route), stats in today.items():
        if stats.partial and (sources.get("lane_rollup") or {}).get(lane) is not None:
            incomplete.append(
                f"{LANE_LABELS[lane]} · {ROUTE_LABELS[route]} 当天统计还没冻结，"
                "数字可能偏少、失败原因记为未知")
    dream_payload = sources.get("dream_jobs")
    dream_skips = None
    if isinstance(dream_payload, Mapping):
        dream_skips = count_dream_skips(dream_payload.get("jobs") or [], day)
        if dream_payload.get("truncated"):
            incomplete.append("V2 做梦任务超过翻页上限，跳过次数可能偏少")
    report = Report(day=day, previous_day=previous_day, today=today,
                    previous=previous, live_stuck=live_stuck,
                    dream_skips=dream_skips, incomplete=incomplete)
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


def _cause_line(stats: LaneRouteStats) -> str:
    parts = []
    for group in GROUP_ORDER:
        cause = stats.causes[group]
        if not cause.attempts:
            continue
        top = "、".join(f"{code} {n}" for code, n in
                       sorted(cause.codes.items(), key=lambda kv: (-kv[1], kv[0]))[:2])
        parts.append(f"{GROUP_LABELS[group]} {cause.attempts} 次/{len(cause.users)} 人（{top}）")
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
            if not cur.attempts and not prev.attempts:
                lines.append(f"  {ROUTE_LABELS[route]}：当天没有任务")
                continue
            summary = (f"  {ROUTE_LABELS[route]}：活跃 {len(cur.users)} 人｜"
                       f"成功 {cur.completed}｜失败 {cur.failed}"
                       f"（{_pct(cur.failure_rate)}，前一天 {_pct(prev.failure_rate)}）｜"
                       f"完全卡死 {cur.stuck_users} 人")
            if lane == "dream" and route == "model_api" and report.dream_skips is not None:
                summary += f"｜成功里跳过 {report.dream_skips} 次（花园太小）"
            lines.append(summary)
            causes = _cause_line(cur)
            if causes:
                lines.append(f"    失败原因：{causes}")
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
    a bad signature comes back as 200 with ``code != 0``)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with opener(request, HTTP_TIMEOUT_SEC) as response:
        raw = response.read()
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError as exc:
        raise RuntimeError("lark_response_not_json") from exc
    code = parsed.get("code", parsed.get("StatusCode"))
    if code not in (0, None):
        raise RuntimeError(f"lark_rejected:code={code}")


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


def fetch_lane_rollup(base_url: str, token: str, *, lane: str, since_day: str,
                      until_day: str, opener: Opener = _default_opener) -> dict:
    merged: dict | None = None
    offset = 0
    for _ in range(LANE_ROLLUP_MAX_PAGES):
        page = _get_json(base_url, "/v1/admin/lane-rollup", {
            "lane": lane, "since_day": since_day, "until_day": until_day,
            "limit": LANE_ROLLUP_PAGE_LIMIT, "offset": offset,
        }, token, opener=opener)
        if merged is None:
            merged = dict(page)
            merged["rows"] = list(page.get("rows") or [])
        else:
            merged["rows"].extend(page.get("rows") or [])
        returned = _int((page.get("pagination") or {}).get("returned"))
        total = _int((page.get("pagination") or {}).get("total"))
        offset += returned
        if returned < LANE_ROLLUP_PAGE_LIMIT or offset >= total:
            return merged
    assert merged is not None
    merged["truncated"] = True
    return merged


def fetch_dream_jobs(base_url: str, token: str, *, day: str,
                     opener: Opener = _default_opener) -> dict:
    """Newest-first completed V2 dreams until created_at is before ``day``."""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(),
                             tzinfo=BEIJING)
    # A dream finishing on ``day`` may have been created the evening before.
    stop_before = start - timedelta(days=1)
    jobs: list = []
    offset = 0
    for _ in range(DREAM_JOBS_MAX_PAGES):
        page = _get_json(base_url, "/v1/admin/memory-dream-jobs", {
            "status": "completed", "limit": DREAM_JOBS_PAGE_LIMIT, "offset": offset,
        }, token, opener=opener)
        batch = list(page.get("jobs") or [])
        jobs.extend(batch)
        offset += len(batch)
        oldest = _parse_ts(batch[-1].get("created_at")) if batch else None
        if (len(batch) < DREAM_JOBS_PAGE_LIMIT
                or not (page.get("pagination") or {}).get("has_more")
                or (oldest is not None and oldest < stop_before)):
            return {"jobs": jobs}
    return {"jobs": jobs, "truncated": True}


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
        "dream_jobs": fetch_dream_jobs(base_url, token, day=day, opener=opener),
    }


def failure_message(day: str, reason: str) -> str:
    return (f"[需要关注] 记忆管线日报 {day}（北京时间）没生成出来\n"
            f"- 取数失败：{reason}\n"
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
    return deliver(render_message(build_report(sources, day=day)))


if __name__ == "__main__":
    raise SystemExit(main())
