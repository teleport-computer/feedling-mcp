#!/usr/bin/env python3
"""Wake empty Runtime V2 profiles and rescue permanently stuck retry metadata.

The tool is content-free and dry-run by default.  It never decrypts MEMORY or
STYLE and never reads Garden cards.  ``--execute`` is required for writes; a
production execution additionally requires an explicit Seven-approval flag.

Examples:
    python tools/backfill_v2_profiles.py --env test
    python tools/backfill_v2_profiles.py --env test --user-id usr_example
    python tools/backfill_v2_profiles.py --env test --user-id usr_example --execute
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

try:
    import psycopg
except ImportError:  # pragma: no cover - operator environment
    raise SystemExit("psycopg is required: pip install 'psycopg[binary]'")


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import db  # noqa: E402
from model_api_runtime.v2 import profile_store  # noqa: E402
from model_api_runtime.v2 import worker  # noqa: E402


OUTCOMES = (
    "rescued_provider_config",
    "rescued_terminal",
    "enqueued_empty",
    "skipped_fresh",
    "skipped_disabled",
    "failed",
)
OUTCOME_SET = frozenset(OUTCOMES)
ENQUEUE_REASON = "operator_profile_backfill"


def _dsn(env: str) -> str:
    key = f"{str(env).upper()}_DATABASE_URL"
    direct = os.environ.get(key, "").strip()
    if direct:
        return direct
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} is required (environment or {env_file})")


def _select_user_ids(dsn: str, allowlist: Iterable[str] = ()) -> list[str]:
    """Select only Runtime V2 users with at least one completed Genesis job."""

    wanted = sorted({str(value).strip() for value in allowlist if str(value).strip()})
    sql = (
        "SELECT DISTINCT genesis.user_id "
        "FROM genesis_import_jobs AS genesis "
        "JOIN v2_runtime_state AS runtime ON runtime.user_id=genesis.user_id "
        "WHERE genesis.status='done' "
        "AND runtime.hosted_runtime_state='v2' "
    )
    params: tuple[Any, ...] = ()
    if wanted:
        sql += "AND genesis.user_id = ANY(%s) "
        params = (wanted,)
    sql += "ORDER BY genesis.user_id"
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [str(row[0]) for row in rows]


def _metadata(raw: Any) -> tuple[str, str, dict | None]:
    if raw is None:
        return "missing", "", None
    document = profile_store.validate_profile_document(raw)
    attempt = document.get("last_attempt") or {}
    return (
        str(document.get("state") or ""),
        str(attempt.get("retry_disposition") or ""),
        document,
    )


def _planned_outcome(raw: Any, *, force_all: bool) -> str:
    state, disposition, document = _metadata(raw)
    if document is not None and document.get("disabled") is True:
        return "skipped_disabled"
    if (
        document is not None
        and state != "ok"
        and disposition in profile_store.PROFILE_STUCK_RETRY_DISPOSITIONS
    ):
        return f"rescued_{disposition}"
    if force_all or state in {"missing", "empty"}:
        return "enqueued_empty"
    return "skipped_fresh"


def rescue_stuck_profile(
    user_id: str,
    *,
    read_profile: Callable[[str, str], Any] = db.get_blob_strict,
    compare_and_swap: Callable[..., bool] = db.set_blob_if_unchanged,
) -> str:
    """CAS-clear a permanent retry disposition, preserving both envelopes."""

    result = profile_store.repair_stuck_profile_retry(
        str(user_id),
        read_profile=read_profile,
        compare_and_swap=compare_and_swap,
    )
    if result.status == "repaired":
        return f"rescued_{result.disposition}"
    return result.status


def _enqueue_profile(user_id: str) -> bool:
    return asyncio.run(
        worker._enqueue_profile_if_due(
            str(user_id),
            reason=ENQUEUE_REASON,
            force=True,
            # The operator command targets the deployed profile lane.  Its own
            # process does not inherit the target CVM's rollout environment.
            enabled=True,
        )
    )


def _row(user_id: str, outcome: str, state: str, disposition: str) -> dict[str, str]:
    if outcome not in OUTCOME_SET:
        raise AssertionError(f"unknown profile backfill outcome: {outcome}")
    return {
        "user_id": str(user_id),
        "outcome": outcome,
        "state": str(state),
        "disposition": str(disposition),
    }


def run_batch(
    user_ids: Iterable[str],
    *,
    execute: bool,
    force_all: bool,
    read_profile: Callable[[str, str], Any] = db.get_blob_strict,
    rescue: Callable[[str], str] | None = None,
    enqueue: Callable[[str], bool] = _enqueue_profile,
) -> list[dict[str, str]]:
    """Process users independently; one failure never aborts the batch."""

    rescue_one = rescue or (lambda uid: rescue_stuck_profile(uid))
    rows: list[dict[str, str]] = []
    for user_id in sorted({str(value) for value in user_ids}):
        state = "unknown"
        disposition = ""
        try:
            raw = read_profile(user_id, profile_store.PROFILE_BLOB_KIND)
            state, disposition, _document = _metadata(raw)
            planned = _planned_outcome(raw, force_all=force_all)
            outcome = planned
            if execute and planned.startswith("rescued_"):
                outcome = rescue_one(user_id)
            elif execute and planned == "enqueued_empty":
                outcome = "enqueued_empty" if enqueue(user_id) else "skipped_fresh"
            rows.append(_row(user_id, outcome, state, disposition))
        except Exception:  # noqa: BLE001 - output must stay content-free
            rows.append(_row(user_id, "failed", state, disposition))
    return rows


def _print_report(
    rows: list[dict[str, str]], *, execute: bool, force_all: bool
) -> None:
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    counts = Counter(row["outcome"] for row in rows)
    print("SUMMARY")
    print("| outcome | count |")
    print("|---|---:|")
    for outcome in OUTCOMES:
        print(f"| {outcome} | {counts.get(outcome, 0)} |")
    print(
        json.dumps(
            {
                "mode": "execute" if execute else "dry-run",
                "force_all": bool(force_all),
                "selected_users": len(rows),
                "counts": {outcome: counts.get(outcome, 0) for outcome in OUTCOMES},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("test", "prod"), required=True)
    parser.add_argument(
        "--user-id",
        action="append",
        default=[],
        help="Restrict to one greylisted user (repeatable).",
    )
    parser.add_argument("--force-all", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="execute",
        action="store_false",
        help="Print the content-free plan without writing (default).",
    )
    mode.add_argument(
        "--execute",
        dest="execute",
        action="store_true",
        help="Apply rescue/enqueue actions. Omit for the default dry-run.",
    )
    parser.set_defaults(execute=False)
    parser.add_argument(
        "--confirm-prod-seven-approved",
        action="store_true",
        help="Required with --env prod --execute after Seven explicitly approves.",
    )
    return parser


def _validate_execution_gate(args: argparse.Namespace) -> None:
    if args.confirm_prod_seven_approved and not (
        args.env == "prod" and args.execute
    ):
        raise SystemExit(
            "--confirm-prod-seven-approved is only valid with --env prod --execute"
        )
    if args.env == "prod" and args.execute and not args.confirm_prod_seven_approved:
        raise SystemExit(
            "prod execution requires Seven approval and "
            "--confirm-prod-seven-approved"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _validate_execution_gate(args)
    dsn = _dsn(args.env)
    os.environ["DATABASE_URL"] = dsn
    user_ids = _select_user_ids(dsn, args.user_id)
    rows = run_batch(
        user_ids,
        execute=bool(args.execute),
        force_all=bool(args.force_all),
    )
    _print_report(rows, execute=bool(args.execute), force_all=bool(args.force_all))
    return 0 if all(row["outcome"] != "failed" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
