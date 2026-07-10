# backend/tee_shadow/__main__.py
"""CLI: ``python -m backend.tee_shadow reconcile [--table T]`` /
``python -m backend.tee_shadow verify [--sample-rate R]``

RDS→TEE 明文表收敛入口（P2T4）。首次跑=存量回填；之后周期跑=双写失败补偿。
打印每张表的 JSON 报告；任一表 ``rds_rows != tee_rows``（没收敛住）时进程以
exit code 1 退出，供 Task 8 的 workflow / 停 RDS gate 消费。

``verify``（P2T7）是只读一致性核验：行数核算（含密文表的 rds==tee+pending）
+ 抽样字段比对，同样以 exit code 1/0 供停 RDS gate 消费——它是那道 gate 的
硬条件报告，reconcile 只管收敛、不管内容正确性。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python -m backend.tee_shadow ...` invoked from the repo root: every
# backend module (including reconciler.py's `import db`) does a bare,
# non-package-qualified import that only resolves once backend/ itself
# (not the repo root) is on sys.path — the same fixup tests/conftest.py does.
_backend_dir = Path(__file__).resolve().parent.parent
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from tee_shadow import reconciler  # noqa: E402 — path fixup above must run first
from tee_shadow import verify  # noqa: E402 — same path fixup requirement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.tee_shadow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    reconcile_p = sub.add_parser(
        "reconcile", help="RDS→TEE 明文表全表收敛（回填 + 补偿 + prune）"
    )
    reconcile_p.add_argument("--table", help="只收敛这一张表（默认全部 13 张明文表）")
    verify_p = sub.add_parser(
        "verify", help="RDS↔TEE 一致性验证：行数核算 + 抽样字段比对（停 RDS gate 硬条件）"
    )
    verify_p.add_argument("--sample-rate", type=float, default=0.02,
                          help="抽样比例（默认 0.02）")
    args = parser.parse_args(argv)

    if args.cmd == "reconcile":
        reports = (
            [reconciler.reconcile_table(args.table)]
            if args.table
            else reconciler.reconcile_all()
        )
        print(json.dumps(reports, indent=2))
        return 1 if any(r["rds_rows"] != r["tee_rows"] for r in reports) else 0
    if args.cmd == "verify":
        report = verify.run(sample_rate=args.sample_rate)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
