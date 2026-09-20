"""Inventory or repair historical ciphertext for all effective-off users."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from content import plaintext_repair


_CONFIRMATION = "ALL-EFFECTIVE-OFF"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-plaintext-rewrite", action="store_true")
    parser.add_argument("--confirm-all-effective-off", default="")
    parser.add_argument("--start-after", default="")
    parser.add_argument("--user-limit", type=int, default=0)
    parser.add_argument("--row-limit", type=int, default=0)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--health-latency-sec", type=float, default=10.0)
    parser.add_argument("--health-poll-sec", type=float, default=5.0)
    parser.add_argument("--healthy-streak", type=int, default=2)
    parser.add_argument("--max-pause-sec", type=float, default=300.0)
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="record failed items and continue with later users",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and (
        not args.allow_plaintext_rewrite
        or args.confirm_all_effective_off != _CONFIRMATION
        or os.environ.get(plaintext_repair.APPLY_ENV, "").strip() != "1"
    ):
        parser.error(
            "--apply requires --allow-plaintext-rewrite, "
            f"--confirm-all-effective-off {_CONFIRMATION}, and "
            f"{plaintext_repair.APPLY_ENV}=1"
        )
    if not args.apply and (
        args.allow_plaintext_rewrite or args.confirm_all_effective_off
    ):
        parser.error("plaintext rewrite confirmations require --apply")

    try:
        result = plaintext_repair.run(
            apply=args.apply,
            start_after=args.start_after,
            user_limit=args.user_limit,
            row_limit=args.row_limit,
            rate=args.rate,
            workers=args.workers,
            health_probe=lambda: plaintext_repair.probe_enclave_health(
                max_latency_sec=args.health_latency_sec
            ),
            healthy_streak=args.healthy_streak,
            health_poll_sec=args.health_poll_sec,
            max_pause_sec=args.max_pause_sec,
            continue_on_failure=args.continue_on_failure,
            run_id=uuid.uuid4().hex,
        )
    except (ValueError, plaintext_repair.HealthGateError) as exc:
        print(f"repair refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - redact data and connection details
        print(f"repair failed: {type(exc).__name__.lower()}", file=sys.stderr)
        return 1

    report = result.public_dict()
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"mode={'apply' if report['apply'] else 'dry-run'} "
            f"users={report['users_completed']}/{report['users_selected']} "
            f"failures={report['failures']} "
            f"last_completed_user_id={report['last_completed_user_id']}"
        )
        for status, count in report["item_counts"].items():
            print(f"{status}={count}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
