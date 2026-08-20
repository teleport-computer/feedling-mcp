#!/usr/bin/env python3
"""Print T138's peak-based trace write-rate measurement as JSON."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report the persistent trace-rate ruler (peak, never average)."
    )
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    report = db.trace_write_stats_measurement(days=args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["measurement_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
