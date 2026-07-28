#!/usr/bin/env python3
"""Audit resident users whose tested model route is still active.

Read-only by default:

    python scripts/audit_resident_model_routes.py

After reviewing the emitted user/route/lease rows, explicitly apply:

    python scripts/audit_resident_model_routes.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List onboarding-route/model-route conflicts and current V1 runner "
            "lease state. No writes occur unless --apply is supplied."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="deactivate the listed model_api route rows in one transaction",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    rows = db.audit_resident_active_model_routes(apply=args.apply)
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "conflicts": len(rows),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
