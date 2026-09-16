#!/usr/bin/env python3
"""Alert on recorded enclave timeout/transport-error windows (stdlib only).

The backend returns aligned, complete windows of terminal trace events; this
is not a full call success rate (some successful traces are suppressed/batched).
Adjacent buckets reduce duplicate transition messages; this stateless reporter
cannot guarantee exactly-once delivery across delayed/retried Actions runs.
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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import enclave_health_contract as contract  # noqa: E402 — stdlib-only peer

CST = timezone(timedelta(hours=8))
HTTP_TIMEOUT_SEC = 30
THRESHOLD_ENV_VARS = (
    "ENCLAVE_ALERT_WINDOW_MINUTES", "ENCLAVE_ALERT_MIN_UNAVAILABLE",
    "ENCLAVE_ALERT_UNAVAILABLE_RATE",
)


def thresholds(environ):
    """Empty overrides use defaults; malformed overrides fail visibly."""
    values = []
    for name, default, cast, low, high in (
        (THRESHOLD_ENV_VARS[0], 15, int, 1, 1440),
        (THRESHOLD_ENV_VARS[1], 20, int, 0, None),
        (THRESHOLD_ENV_VARS[2], 0.20, float, 0, 1),
    ):
        raw = str(environ.get(name) or "").strip()
        value = cast(raw) if raw else default
        if not math.isfinite(value) or value < low or (high is not None and value > high):
            raise ValueError("invalid_threshold")
        values.append(value)
    return tuple(values)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp_without_timezone")
    return parsed


def validate_health(payload, window_minutes):
    """Reject missing/inconsistent evidence instead of interpreting it as zero."""
    if not isinstance(payload, dict) or set(payload) != contract.PAYLOAD_KEYS:
        raise ValueError("invalid_health_shape")
    if type(payload["window_minutes"]) is not int or payload["window_minutes"] != window_minutes:
        raise ValueError("window_mismatch")
    end = _timestamp(payload["calculated_at"])
    if end.timestamp() % (window_minutes * 60) != 0:
        raise ValueError("unaligned_window")
    for name, offset in (("current", 0), ("previous", 1)):
        window = payload[name]
        if not isinstance(window, dict) or set(window) != contract.WINDOW_KEYS:
            raise ValueError("invalid_window_shape")
        for key in contract.COUNT_KEYS:
            if type(window[key]) is not int or window[key] < 0:
                raise ValueError("invalid_count")
        expected_end = end - timedelta(minutes=window_minutes * offset)
        if (_timestamp(window["end_at"]) != expected_end
                or _timestamp(window["start_at"]) != expected_end - timedelta(minutes=window_minutes)):
            raise ValueError("window_boundary_mismatch")
        unavailable = window["timeout"] + window["transport_error"]
        denominator = window["done"] + unavailable
        if (window["unavailable"] != unavailable
                or window["calls"] != denominator + window["http_401"] + window["http_403"] + window["http_other"]
                or window["users_affected"] > window["calls"] - window["done"]):
            raise ValueError("inconsistent_counts")
        rate = window["unavailable_rate"]
        if denominator:
            if (type(rate) not in (float, int) or not math.isfinite(rate)
                    or not math.isclose(rate, unavailable / denominator, abs_tol=1e-12)):
                raise ValueError("inconsistent_rate")
        elif rate is not None:
            raise ValueError("unmeasured_rate")
        purposes = window["top_purposes"]
        if not isinstance(purposes, list) or len(purposes) > 5:
            raise ValueError("invalid_purposes")
        seen = set()
        for item in purposes:
            if not isinstance(item, dict) or set(item) != {"purpose", "count"}:
                raise ValueError("invalid_purpose_shape")
            if (not isinstance(item["purpose"], str) or item["purpose"] not in contract.PURPOSE_LABELS
                    or item["purpose"] in seen or type(item["count"]) is not int or item["count"] <= 0):
                raise ValueError("invalid_purpose")
            seen.add(item["purpose"])
        if sum(item["count"] for item in purposes) > window["calls"] - window["done"]:
            raise ValueError("inconsistent_purposes")
    return payload


def triggered(window, *, min_unavailable=20, rate=0.20):
    return (window["unavailable"] >= min_unavailable
            and window["unavailable_rate"] is not None
            and window["unavailable_rate"] >= rate)


def alert_state(payload, *, run_minute, min_unavailable=20, rate=0.20):
    if type(run_minute) is not int or not 0 <= run_minute <= 59:
        raise ValueError("invalid_run_minute")
    current = triggered(payload["current"], min_unavailable=min_unavailable, rate=rate)
    previous = triggered(payload["previous"], min_unavailable=min_unavailable, rate=rate)
    if current and not previous:
        return "开始"
    if current and previous:
        return "持续中" if run_minute == 0 else "静默"
    if not current and previous:
        return "已恢复"
    return "静默"


def render_message(payload, state):
    current = payload["current"]
    start = _timestamp(current["start_at"]).astimezone(CST)
    end = _timestamp(current["end_at"]).astimezone(CST)
    phenomenon = "enclave 解密超时或传输错误增多" if state != "已恢复" else "enclave 解密告警条件不再满足"
    rate = current["unavailable_rate"]
    rate_label = f"{rate:.0%}" if rate is not None else "量不到（无成功/不可用终态事件）"
    purposes = ", ".join(f"{p['purpose']} {p['count']}" for p in current["top_purposes"]) or "无"
    text = (f"[{state}] {phenomenon}\n"
            f"{payload['window_minutes']} 分钟窗口 {start:%Y-%m-%d %H:%M}–{end:%H:%M} CST\n"
            f"超时 {current['timeout']} / 传输错误 {current['transport_error']} / 成功 {current['done']} → 不可用率 {rate_label}\n"
            f"失败波及用户 {current['users_affected']};主要 purpose: {purposes}\n"
            f"附注:HTTP 401 ×{current['http_401']} / 403 ×{current['http_403']} / 其他错误 ×{current['http_other']}（不参与判定）\n"
            "口径:仅已记录的终态 trace；不代表全部调用。\n"
            "查:api.feedling.app/admin/data-track 或 phala logs enclave")
    if rate is None:
        text += "\n当前无可用分母，不能据此确认服务恢复。"
    return text


def _default_opener(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_health(base_url, token, *, window_minutes, opener=_default_opener):
    url = (f"{base_url.rstrip('/')}/v1/admin/enclave-decrypt-health?"
           + urllib.parse.urlencode({"window_minutes": window_minutes}))
    request = urllib.request.Request(url, headers={"X-Admin-Token": token, "Accept": "application/json"})
    with opener(request, HTTP_TIMEOUT_SEC) as response:
        return json.loads(response.read())


def lark_payload(text, *, secret="", timestamp=""):
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = timestamp or str(int(time.time()))
        sign = base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()).decode()
        payload.update(timestamp=ts, sign=sign)
    return payload


def post_to_lark(webhook, payload, *, opener=_default_opener):
    request = urllib.request.Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with opener(request, HTTP_TIMEOUT_SEC) as response:
        body = json.loads(response.read())
    if not isinstance(body, dict):
        raise ValueError("invalid_lark_response")
    codes = [body[key] for key in ("code", "StatusCode") if key in body]
    if not codes or any(type(code) is not int or code != 0 for code in codes):
        raise ValueError("lark_rejected")


def main(argv=None, *, environ=None, opener=_default_opener, out=None, now=None):
    env = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--run-minute", type=int, default=None)
    args = parser.parse_args(argv)

    def deliver(text):
        if args.dry_run:
            print(text, file=out)
            return 0
        webhook = str(env.get("LARK_BOT_WEBHOOK") or "").strip()
        if not webhook:
            print("LARK_BOT_WEBHOOK is not configured", file=sys.stderr)
            return 2
        try:
            post_to_lark(webhook, lark_payload(text, secret=env.get("LARK_BOT_SECRET", "")), opener=opener)
        except Exception as exc:  # boundary: only type, never URL/body/credentials
            print(f"lark post failed: {type(exc).__name__}", file=sys.stderr)
            return 3
        print("posted enclave decrypt alert", file=out)
        return 0

    try:
        window, minimum, rate = thresholds(env)
        minute = args.run_minute if args.run_minute is not None else (now or datetime.now(timezone.utc)).minute
        if args.fixture:
            payload = json.loads(Path(args.fixture).read_text())
        else:
            base_url = args.base_url or str(env.get("FEEDLING_API_URL") or "").strip()
            token = str(env.get("FEEDLING_ADMIN_TOKEN") or "").strip()
            if not base_url or not token:
                raise ValueError("missing_api_configuration")
            payload = fetch_health(base_url, token, window_minutes=window, opener=opener)
        validate_health(payload, window)
        state = alert_state(payload, run_minute=minute, min_unavailable=minimum, rate=rate)
        text = render_message(payload, state) if state != "静默" else ""
    except Exception as exc:  # changed shapes/network/config must produce an unmeasured notice
        reason = type(exc).__name__
        print(f"enclave health unavailable: {reason}", file=sys.stderr)
        deliver(f"[量不到] enclave 解密健康取数或判定失败（{reason}）\n没有有效数字，不代表正常；请查 GitHub Actions enclave-decrypt-monitor。")
        return 1
    if not text:
        print("静默：当前无需发送 enclave 告警。", file=out)
        return 0
    return deliver(text)


if __name__ == "__main__":
    raise SystemExit(main())
