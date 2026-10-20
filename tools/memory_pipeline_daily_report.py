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

Aggregation moved to ``admin.lane_rollup_summary`` so this tool needs only the
standard library, without parsing backend source files for constants.

Data source (admin-gated, content-free by construction):

* ``GET /v1/admin/lane-rollup/summary`` — pre-aggregated lane/route counts for
  two Beijing calendar days (``admin.lane_rollup_summary``). ``route`` is
  ``resident`` (V1: hosted resident + self-hosted VPS) or ``model_api``
  (V2 pooled worker). ``failure_codes`` are already sanitized to
  ``^[a-z0-9_:-]{1,120}$``. Dream declared skips come from the same
  cells' ``silent_declared``. ``--fixture`` reads this summary JSON shape.

Result semantics: the summary supplies conserved observed terminal buckets,
including skips, expiry, replacement and unmeasured splits. Completed status
is not proof that content was generated. A separate overlapping operational
classification retains the existing alert thresholds and rate denominator.
Exact sanitized codes and account exclusions remain in the summary/offline detail;
the compact message displays at most four investigation areas per lane.

Day boundary: the rollup freezes **Beijing** (Asia/Shanghai) days, so the
report covers the previous Beijing day, not a UTC day. Recomputing UTC days
would need a second aggregate over raw job tables — the exact unbounded read
the rollup exists to retire.

Output never contains user ids, message ids or any free text: counts, lane /
route labels and sanitized failure codes only.

Each lane has separate processed, skipped and failed columns. Unmeasured
outcomes remain explicit; missing coverage is unknown rather than zero. Actual
writes/no-change and pending/running counts are not supplied by this summary.
Rates and user counts remain in the unchanged alert policy and offline detail.

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
import base64
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

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
    # not in the closed user-unavailable exclusion set.
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
    if not math.isfinite(value):
        # float("nan") / float("inf") parse fine, but every comparison with nan is
        # False, so a nan threshold silently turns its rule off.
        print(f"ignoring non-finite {name}; using default {default}", file=sys.stderr)
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

HTTP_TIMEOUT_SEC = 60

def incomplete_notes(report: dict) -> list[str]:
    """Turn structured coverage caveats into the original report wording."""
    templates = {
        "missing_lane": "{lane} 没取到数据",
        "truncated": "{lane} 行数超过翻页上限，只统计了一部分",
        "partial": "{lane} · {route} 覆盖不足或尚未冻结，数字可能偏少，缺失不代表零",
        "unclassified": "{lane} · {route} 失败分类缺失，按原始失败计（可能含跳过/用户账号问题）",
    }
    return [templates[item["kind"]].format(
        lane=LANE_LABELS[item["lane"]], route=ROUTE_LABELS.get(item.get("route"), ""))
        for item in report["incomplete"]]


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{round(rate * 100)}%"


def _users(stats: Mapping[str, Any], key: str) -> str:
    """A user count, or ``-`` when the backend predates the field."""
    value = stats.get(key)
    return "-" if value is None else str(int(value))


#: Shown once per message. The per-attempt failure rate counts a user whose
#: night retried once per attempt (dream: up to 4 since 2026-09-16, T646), so a
#: change in retry policy moves the rate by itself; the two user counts on each
#: line count a repeatedly failing user once (a retry that succeeds still moves
#: them from 零成功 to 受影响, which is a real change).
RATE_CAVEAT = ("口径：按终态任务/记录计数，不是模型调用、metric 或生成卡片数；同一用户可有多次尝试。"
               "受影响/零成功为运维分类人数，零成功仅指当日无完成，不证明卡死。"
               "运维失败率=运维失败/(扣除声明跳过的完成+运维失败)，排除控制结果、账号不可用和 superseded；"
               "分类缺失时沿用保守告警口径，不代表已证实失败。")


def evaluate_attention(report: dict) -> list[str]:
    reasons: list[str] = []
    for lane in LANES:
        for route in ROUTES:
            cur = report['cells'][f'{lane}/{route}']['day']
            prev = report['cells'][f'{lane}/{route}']['previous']
            where = f"{LANE_LABELS[lane].split()[0]} · {ROUTE_LABELS[route]}"
            if cur.get('evidence_notes') or 'outcomes' not in cur:
                reasons.append(f"{where}：结果分型缺少证据或计数异常，见明细")
            if cur['stuck_users'] >= STUCK_USERS_ATTENTION:
                reasons.append(f"{where}：有运维失败且零完成 {cur['stuck_users']} 人"
                               f"（有失败、零成功，阈值 {STUCK_USERS_ATTENTION}）")
            ours = cur['causes'][GROUP_OURS]
            if ours['users'] >= OUR_SIDE_USERS_ATTENTION:
                reasons.append(f"{where}：我们这边的故障影响 {ours['users']} 人"
                               f"（阈值 {OUR_SIDE_USERS_ATTENTION}）")
            rate = cur['failure_rate']
            if rate is not None and cur['attempts'] >= MIN_ATTEMPTS_FOR_RATE:
                prev_rate = prev['failure_rate']
                if rate >= FAILURE_RATE_ATTENTION:
                    reasons.append(f"{where}：运维失败率 {_pct(rate)}（分母 {cur['attempts']}）")
                elif (prev_rate is not None and prev['attempts'] >= MIN_ATTEMPTS_FOR_RATE
                      and (rate - prev_rate) * 100 >= FAILURE_RATE_JUMP_PP):
                    reasons.append(f"{where}：运维失败率 {_pct(rate)}，前一天 {_pct(prev_rate)}")
            if (prev['active_users'] >= ACTIVE_DROP_MIN_PREVIOUS and not cur['partial']
                    and cur['active_users'] < prev['active_users'] * ACTIVE_DROP_RATIO):
                reasons.append(f"{where}：活跃用户 {cur['active_users']} 人，"
                               f"前一天 {prev['active_users']} 人（可能整条线没在跑）")
    for lane in LANES:
        n = report['live_stuck'].get(lane)
        if n is not None and n >= LIVE_STUCK_JOBS_ATTENTION:
            reasons.append(f"{LANE_LABELS[lane].split()[0]}：此刻有 {n} 个任务卡着没结束"
                           f"（阈值 {LIVE_STUCK_JOBS_ATTENTION}）")
    if report['incomplete']:
        reasons.append("数据不完整，见文末说明")
    return reasons


def _all_codes(codes: Mapping[str, int]) -> str:
    return "、".join(f"{code} {n}" for code, n in
                    sorted(codes.items(), key=lambda kv: (-kv[1], kv[0])))


def _cause_line(stats: dict) -> str:
    parts = []
    for group in GROUP_ORDER:
        cause = stats['causes'][group]
        if not cause['attempts']:
            continue
        parts.append(f"{GROUP_LABELS[group]} {cause['attempts']} 次/{cause['users']} 人"
                     f"（{_all_codes(cause['codes'])}）")
    return "；".join(parts)


def _lane_totals(report: dict, lane: str) -> dict | None:
    # A missing/partial route cannot silently become a zero in the lane total.
    if any(item["lane"] == lane for item in report.get("incomplete", [])):
        return None
    totals = dict.fromkeys(("processed", "skipped", "failed", "expired", "superseded",
                            "unknown_completed", "unknown_non_success"), 0)
    for route in ROUTES:
        cur = report.get("cells", {}).get(f"{lane}/{route}", {}).get("day", {})
        outcomes = cur.get("outcomes")
        if outcomes is None or cur.get("partial") or cur.get("unclassified"):
            return None
        for key in totals:
            value = outcomes.get(key)
            if type(value) is not int or value < 0:
                return None
            totals[key] += value
    return totals


def _brief_causes(report: dict, lane: str) -> str:
    # Use the existing disjoint diagnostic groups, plus their separately excluded
    # account records. Never also add reason_codes or terminal outcome counts.
    groups = dict.fromkeys(GROUP_ORDER, 0)
    service_codes = set()
    for route in ROUTES:
        cur = report.get("cells", {}).get(f"{lane}/{route}", {}).get("day", {})
        for group in GROUP_ORDER:
            codes = cur.get("causes", {}).get(group, {}).get("codes", {})
            groups[group] += sum(codes.values())
            if group == GROUP_PROVIDER:
                service_codes.update(code.rsplit(":", 1)[-1]
                                     for code, count in codes.items() if count)
        groups[GROUP_USER] += sum(cur.get("user_unavailable_codes", {}).values())
    service_label = "模型服务相关"
    if service_codes == {"upstream_unavailable"}:
        service_label = "服务不可用"
    elif service_codes and service_codes <= {"output_truncated", "upstream_unavailable"}:
        service_label = "模型回复截断/服务不可用" if "upstream_unavailable" in service_codes else "模型回复截断"
    labels = {GROUP_PROVIDER: service_label, GROUP_USER: "账号/模型配置",
              GROUP_OURS: "解析/处理", GROUP_UNKNOWN: "原因待查"}
    return "、".join(f"{labels[group]}{groups[group]}次" for group in
                    (GROUP_PROVIDER, GROUP_USER, GROUP_OURS, GROUP_UNKNOWN) if groups[group])


def render_message(report: dict) -> str:
    attention = evaluate_attention(report)
    head = "[需要关注]" if attention else "[正常]"
    lines = [f"{head} 记忆报告｜{report['day']}（北京时间，00:00–24:00）",
             "任务｜处理完成｜正常跳过｜失败"]
    totals = {lane: _lane_totals(report, lane) for lane in LANES}
    names = {"capture": "Capture", "dream": "Dream"}
    for lane in LANES:
        cur = totals[lane]
        if cur is None:
            lines.append(f"{names[lane]}｜不明｜不明｜不明")
            continue
        lines.append(f"{names[lane]}｜{cur['processed']}｜{cur['skipped']}｜{cur['failed']}")
        extra = []
        for key, label in (("expired", "过期"), ("superseded", "替代"),
                           ("unknown_completed", "完成结果不明"),
                           ("unknown_non_success", "失败/跳过未分型")):
            if cur[key]:
                extra.append(f"{label}{cur[key]}")
        if extra:
            lines.append(f"{names[lane]}另有：" + "、".join(extra) + "。")
    lines.append("完成后的实际写入/无变化尚无细分记录；排队和运行数未统计。")
    for lane in LANES:
        causes = _brief_causes(report, lane)
        if causes:
            lines.append(f"{names[lane]}失败相关原因：" + causes + "。")
    if any(_brief_causes(report, lane) for lane in LANES):
        lines.append("原因记录未逐项关联失败任务，不能相加还原失败数。")
    actions = []
    if any(value is None for value in totals.values()):
        actions.append("核对缺失或不完整的数据，暂不判断整体结果")
    for reason in attention:
        if "我们这边" in reason:
            action = "排查解析与处理异常，核对输出格式和处理日志"
        elif "零完成" in reason or "卡着" in reason:
            action = "检查未正常完成的任务，区分仍在执行、超时和账号阻塞"
        elif "活跃" in reason:
            action = "核对任务量下降是否符合实际使用情况"
        else:
            action = "检查失败增加的主要原因，优先核对模型服务和账号配置"
        if action not in actions:
            actions.append(action)
    for action in actions[:2]:
        lines.append("需处理：" + action + "。")
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


def fetch_summary(base_url: str, token: str, *, day: str,
                  opener: Opener = _default_opener) -> dict:
    return _get_json(base_url, "/v1/admin/lane-rollup/summary", {"day": day},
                     token, opener=opener)


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
                        help="Render from a saved summary JSON instead of the API")
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
            sources = fetch_summary(base_url, token, day=day, opener=opener)
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
        text = render_message(sources)
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
