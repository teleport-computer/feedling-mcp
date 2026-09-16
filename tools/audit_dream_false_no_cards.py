#!/usr/bin/env python3
"""Read-only audit: users whose Dream ledger was advanced by a false "no cards".

Thin operator CLI over ``backend/proactive/dream_ledger_audit.py`` (selection
rules, limits and the repair design are documented there). It connects with a
direct database DSN (``<ENV>_DATABASE_URL``) and never writes.

Without database access, use the admin API instead — the same selector behind
``GET /v1/admin/memory/dream-false-no-cards`` and the compare-and-set repair
``POST /v1/admin/memory/dream-false-no-cards/repair`` (runbook:
``deploy/DEPLOYMENTS.md``, "False no-cards Dream ledger repair").

Examples::

    python tools/audit_dream_false_no_cards.py --env prod \
      --window 2026-09-10T18:00:00Z/2026-09-10T20:00:00Z \
      --window 2026-09-13T18:00:00Z/2026-09-13T20:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from proactive import dream_ledger_audit  # noqa: E402  (stdlib-only module)


def parse_window(value: str):
    try:
        return dream_ledger_audit.parse_window(value)
    except dream_ledger_audit.InvalidWindow as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _dsn(env: str) -> str:
    key = f"{env.upper()}_DATABASE_URL"
    if os.environ.get(key, "").strip():
        return os.environ[key].strip()
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} is required (environment or {env_file})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=("test", "pre", "prod"), required=True)
    parser.add_argument(
        "--window", type=parse_window, action="append", required=True,
        help="START/END ISO instants of an incident window (repeatable).",
    )
    parser.add_argument("--user-id", action="append", default=[],
                        help="Restrict to these users (repeatable).")
    parser.add_argument(
        "--ledger-tolerance-sec", type=float,
        default=dream_ledger_audit.DEFAULT_LEDGER_TOLERANCE_SEC,
    )
    parser.add_argument(
        "--max-job-age-days", type=float,
        default=dream_ledger_audit.DEFAULT_MAX_JOB_AGE_DAYS,
        help="Without --user-id: only consider Dream jobs enqueued at most this "
             "long before the earliest window start (index-friendly scan bound; "
             "the report then says partial=true). Ignored with --user-id.",
    )
    parser.add_argument(
        "--statement-timeout-sec", type=float,
        default=dream_ledger_audit.DEFAULT_STATEMENT_TIMEOUT_SEC,
        help="Postgres statement_timeout for every read (transaction-local).",
    )
    return parser


def main(argv: list[str] | None = None, *, connect=None) -> int:
    args = _parser().parse_args(argv)
    if connect is None:
        try:
            import psycopg
        except ImportError:  # pragma: no cover - operator environment
            raise SystemExit("psycopg is required: pip install 'psycopg[binary]'")
        dsn = _dsn(args.env)
        connect = lambda: psycopg.connect(dsn)  # noqa: E731
    with connect() as conn:
        with conn.transaction():
            report = dream_ledger_audit.collect(
                conn,
                windows=args.window,
                user_ids=args.user_id or None,
                ledger_tolerance_sec=args.ledger_tolerance_sec,
                max_job_age_days=args.max_job_age_days,
                statement_timeout_sec=args.statement_timeout_sec,
            )
    report["environment"] = args.env
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
