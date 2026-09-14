"""Safely inventory or migrate one user's legacy encrypted content."""
from __future__ import annotations

import argparse
import json
import os
import sys

from content import plaintext_migration


_APPLY_BLOCKED = (
    "--apply requires --allow-plaintext-rewrite and "
    f"{plaintext_migration.APPLY_ENV}=1"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="exact user_id (single user only)")
    parser.add_argument("--apply", action="store_true", help="perform CAS rewrites")
    parser.add_argument(
        "--allow-plaintext-rewrite",
        action="store_true",
        help="acknowledge the irreversible plaintext rewrite",
    )
    parser.add_argument("--json", action="store_true", help="print JSON counters")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and (
        not args.allow_plaintext_rewrite
        or os.environ.get(plaintext_migration.APPLY_ENV, "").strip() != "1"
    ):
        parser.error(_APPLY_BLOCKED)
    if args.allow_plaintext_rewrite and not args.apply:
        parser.error("--allow-plaintext-rewrite is only valid with --apply")

    try:
        result = plaintext_migration.run(args.user, apply=args.apply)
    except (PermissionError, ValueError) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2

    report = result.public_dict()
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"mode={'apply' if result.apply else 'dry-run'} "
            f"user_id={result.user_id} failures={result.failures}"
        )
        for status, count in result.counts.items():
            print(f"{status}={count}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
