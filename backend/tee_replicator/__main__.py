"""CLI: python -m backend.tee_replicator run --table chat_messages --qps 2 [--dry-run]

Task 8 的 admin workflow 通过它触发一次复制。表名限定在 worker._TABLES 内。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python -m backend.tee_replicator ...` invoked from the repo root: every
# backend module (including tee_replicator.worker's bare `import db`) does a
# non-package-qualified import that only resolves once backend/ itself (not the
# repo root) is on sys.path — same fixup as tee_shadow/__main__.py.
_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from tee_replicator import worker  # noqa: E402 — path fixup above must run first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tee_replicator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run a single decrypt-replication pass over one table")
    run.add_argument("--table", required=True, choices=sorted(worker._TABLES),
                     help="which RDS ciphertext table to replicate into the TEE plaintext DB")
    run.add_argument("--qps", type=float, default=2.0,
                     help="throttle: sleeps len(batch)/qps between batches")
    run.add_argument("--dry-run", action="store_true",
                     help="decrypt + count but write nothing to the TEE (reports would_copy)")
    run.add_argument("--limit", type=int, default=None,
                     help="cap total rows scanned this pass (default: all)")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        report = worker.run_table(args.table, qps=args.qps, dry_run=args.dry_run,
                                  limit=args.limit)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
