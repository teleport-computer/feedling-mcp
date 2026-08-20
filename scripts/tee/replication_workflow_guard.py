#!/usr/bin/env python3
"""Build and validate TEE replication workflow payloads without silent fallback."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path


def _optional_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    value = int(raw)
    if value < 0 or str(value) != raw:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _dry_run() -> bool:
    raw = os.environ.get("DRY_RUN_IN", "true").strip().lower()
    if raw not in ("true", "false"):
        raise ValueError("DRY_RUN_IN must be true or false")
    return raw == "true"


def build() -> int:
    payload = {
        "action": os.environ.get("ACTION", "").strip(),
        "table": os.environ.get("TABLE_IN", "").strip() or None,
        "dry_run": _dry_run(),
        "confirm": os.environ.get("CONFIRM_IN", "").strip() or None,
        "qps": _optional_float("QPS_IN"),
        "expected_stale": _optional_int("EXPECTED_STALE_IN"),
    }
    print(json.dumps(payload, separators=(",", ":")))
    return 0


def check(http_code: str, response_path: str) -> int:
    code = int(http_code)
    if code < 200 or code >= 300:
        return 1
    body = json.loads(Path(response_path).read_text())
    if not _dry_run() and os.environ.get("ACTION") in ("reflow", "prune"):
        if body.get("ok") is not True or body.get("failures") != 0:
            return 1
    return 0


def main(argv: list[str]) -> int:
    try:
        if argv == ["build"]:
            return build()
        if len(argv) == 3 and argv[0] == "check":
            return check(argv[1], argv[2])
        raise ValueError("usage: replication_workflow_guard.py build|check HTTP_CODE RESPONSE")
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(f"tee replication workflow guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
