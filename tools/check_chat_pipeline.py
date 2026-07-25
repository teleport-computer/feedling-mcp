#!/usr/bin/env python3
"""
Feedling Chat Pipeline Self-Check
===================================
Diagnoses whether the full chat loop (user message → agent → reply) is healthy.

Usage:
  FEEDLING_API_URL=http://localhost:5001 \
  FEEDLING_API_KEY=<key> \
  python tools/check_chat_pipeline.py

Exit codes:
  0  OK   — all checks passed
  1  WARN — connected but no active consumer or no recent loop
  2  FAIL — cannot reach endpoint or API key rejected

Checks:
  1. Feedling backend reachable
  2. API key accepted
  3. Resident consumer process running
  4. Recent closed loop (user message followed by assistant reply in last 10 min)
"""

import os
import subprocess
import sys
import time

try:
    import httpx
except ImportError:
    print("FAIL  missing dependency: pip install httpx")
    sys.exit(2)

FEEDLING_API_URL = os.environ.get("FEEDLING_API_URL", "http://127.0.0.1:5001").rstrip("/")
FEEDLING_API_KEY = os.environ.get("FEEDLING_API_KEY", "")
LOOP_WINDOW_SECONDS = int(os.environ.get("LOOP_WINDOW_SECONDS", "600"))


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

_results: list[tuple[str, str, str]] = []  # (status, label, detail)

OK   = "OK  "
WARN = "WARN"
FAIL = "FAIL"


def record(status: str, label: str, detail: str = "") -> None:
    _results.append((status, label, detail))
    icon = {"OK  ": "✓", "WARN": "⚠", "FAIL": "✗"}.get(status, "?")
    line = f"  [{icon}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def exit_code() -> int:
    statuses = {r[0] for r in _results}
    if FAIL in statuses:
        return 2
    if WARN in statuses:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Check 1: backend reachable
# ---------------------------------------------------------------------------

def check_reachable() -> bool:
    try:
        r = httpx.get(f"{FEEDLING_API_URL}/v1/chat/history", timeout=5)
        if r.status_code in (200, 401, 403):
            record(OK, "Backend reachable", f"HTTP {r.status_code}")
            return True
        record(FAIL, "Backend reachable", f"unexpected HTTP {r.status_code}")
        return False
    except Exception as e:
        record(FAIL, "Backend reachable", str(e))
        return False


# ---------------------------------------------------------------------------
# Check 2: API key valid
# ---------------------------------------------------------------------------

def check_api_key() -> bool:
    if not FEEDLING_API_KEY:
        record(FAIL, "API key", "FEEDLING_API_KEY is not set")
        return False
    try:
        r = httpx.get(
            f"{FEEDLING_API_URL}/v1/chat/history",
            headers={"X-API-Key": FEEDLING_API_KEY},
            params={"limit": 1},
            timeout=5,
        )
        if r.status_code == 200:
            record(OK, "API key accepted")
            return True
        elif r.status_code == 401:
            record(FAIL, "API key", "401 Unauthorized — key is wrong or expired")
            return False
        else:
            record(WARN, "API key", f"unexpected HTTP {r.status_code}")
            return True
    except Exception as e:
        record(FAIL, "API key", str(e))
        return False


# ---------------------------------------------------------------------------
# Check 3: resident consumer process running
# ---------------------------------------------------------------------------

def check_consumer_process() -> bool:
    # Check systemd first
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "feedling-chat-resident"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() == "active":
            record(OK, "Resident consumer (systemd)", "feedling-chat-resident active")
            return True
        elif result.stdout.strip() in ("inactive", "failed", "dead"):
            # Don't FAIL yet — might be running as a plain process
            pass
    except Exception:
        pass

    # Check for the Python process by script name
    try:
        result = subprocess.run(
            ["pgrep", "-f", "chat_resident_consumer"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split()
            record(OK, "Resident consumer (process)", f"pid(s): {', '.join(pids)}")
            return True
    except Exception:
        pass

    record(WARN, "Resident consumer", "not running — start feedling-chat-resident.service or run tools/chat_resident_consumer.py")
    return False


# ---------------------------------------------------------------------------
# Check 4: recent closed loop
# ---------------------------------------------------------------------------

def check_recent_loop() -> bool:
    try:
        r = httpx.get(
            f"{FEEDLING_API_URL}/v1/chat/history",
            headers={"X-API-Key": FEEDLING_API_KEY},
            params={"limit": 50},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        messages = data.get("messages") or data.get("history") or []
    except Exception as e:
        record(WARN, "Recent loop check", f"could not fetch history: {e}")
        return False

    if not messages:
        record(WARN, "Recent loop check", "no messages in history yet")
        return False

    cutoff = time.time() - LOOP_WINDOW_SECONDS
    recent = [m for m in messages if float(m.get("timestamp", 0)) >= cutoff]

    if not recent:
        record(
            WARN,
            "Recent loop check",
            f"no messages in last {LOOP_WINDOW_SECONDS // 60} min — is anyone chatting?",
        )
        return False

    roles = {m.get("role") for m in recent}
    if "user" in roles and "assistant" in roles:
        last_reply_ts = max(
            float(m["timestamp"]) for m in recent if m.get("role") == "assistant"
        )
        age = int(time.time() - last_reply_ts)
        record(OK, "Recent loop check", f"last assistant reply {age}s ago")
        return True

    if "user" in roles and "assistant" not in roles:
        oldest_unanswered = min(
            float(m["timestamp"]) for m in recent if m.get("role") == "user"
        )
        wait = int(time.time() - oldest_unanswered)
        record(
            WARN,
            "Recent loop check",
            f"user message unanswered for {wait}s — consumer may be down",
        )
        return False

    record(WARN, "Recent loop check", "only assistant messages in window — no user activity")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\nFeedling Chat Pipeline Self-Check")
    print(f"  url: {FEEDLING_API_URL}")
    print(f"  window: last {LOOP_WINDOW_SECONDS // 60} min\n")

    reachable = check_reachable()
    key_ok = False
    if reachable:
        key_ok = check_api_key()

    check_consumer_process()

    if key_ok:
        check_recent_loop()
    else:
        record(WARN, "Recent loop check", "skipped — API key not validated")

    code = exit_code()
    label = {0: "OK", 1: "WARN", 2: "FAIL"}[code]
    print(f"\nOverall: {label}\n")
    sys.exit(code)


if __name__ == "__main__":
    main()
