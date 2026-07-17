#!/usr/bin/env python3
"""P0 release smoke — one command, all cells, one result table.

  python3 tools/e2e/p0.py                     # everything configured
  python3 tools/e2e/p0.py --only anthropic-official,vps-claude-code
  python3 tools/e2e/p0.py --list              # show cells and key status

Exit code: 0 = no hard failures (skips/warns allowed), 1 = at least one FAIL
(protocol §8: any P0 fail blocks test→main). Test env only; every account
created here is deleted in teardown.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.config import HOSTED_CELLS, VPS_CELLS, KEYS_FILE, load_keys  # noqa: E402
from tools.e2e.hosted import run_hosted_cell  # noqa: E402
from tools.e2e.vps import run_vps_cell  # noqa: E402

_ICON = {"ok": "✅", "fail": "❌", "skip": "⏭️", "warn": "⚠️"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated cell names")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    pool = load_keys()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    known_cells = {cell.name for cell in [*HOSTED_CELLS, *VPS_CELLS]}
    unknown = sorted(only - known_cells)
    if unknown:
        ap.error(f"unknown cell(s): {', '.join(unknown)}")

    if args.list:
        for cell in HOSTED_CELLS:
            print(f"hosted  {cell.name:26s} key={'yes' if cell.key(pool) else 'NO (' + cell.key_env + ')'}")
        for cell in VPS_CELLS:
            print(f"vps     {cell.name:26s} binary={cell.needs_binary}")
        print(f"key pool: {KEYS_FILE} ({'present' if KEYS_FILE.exists() else 'ABSENT'})")
        return 0

    results = []
    t0 = time.time()
    for cell in HOSTED_CELLS:
        if only and cell.name not in only:
            continue
        print(f"\n=== hosted:{cell.name} ===", flush=True)
        try:
            results.append(run_hosted_cell(cell, pool))
        except Exception as e:  # noqa: BLE001 — one cell must not kill the matrix
            results.append({"cell": cell.name, "result": "fail",
                            "steps": [("crash", "fail", f"{type(e).__name__}: {e}")]})
        _print_cell(results[-1])
    for cell in VPS_CELLS:
        if only and cell.name not in only:
            continue
        print(f"\n=== vps:{cell.name} ===", flush=True)
        try:
            results.append(run_vps_cell(cell))
        except Exception as e:  # noqa: BLE001
            results.append({"cell": cell.name, "result": "fail",
                            "steps": [("crash", "fail", f"{type(e).__name__}: {e}")]})
        _print_cell(results[-1])

    # -- summary table (paste-ready for the release report, protocol §8) -----
    print("\n" + "=" * 62)
    print(f"P0 冒烟结果 · {time.strftime('%Y-%m-%d %H:%M')} · {time.time() - t0:.0f}s")
    print("| cell | result | detail |")
    print("|---|---|---|")
    for r in results:
        bad = [s for s in r["steps"] if s[1] == "fail"]
        warn = [s for s in r["steps"] if s[1] == "warn"]
        detail = (bad or warn or [("", "", "all steps green")])[0][2] or \
                 (bad or warn or [("", "", "")])[0][0]
        detail = detail.replace("|", "\\|").replace("\n", " ")
        print(f"| {r['cell']} | {_ICON.get(r['result'], r['result'])} {r['result']} | {detail[:80]} |")
    hard_fail = p0_blocks_release(results)
    print("=" * 62)
    print("阻断判定:", "❌ P0 FAIL — 不许 test→main" if hard_fail else "✅ P0 通过")
    return 1 if hard_fail else 0


def p0_blocks_release(results: list[dict]) -> bool:
    """A single failed cell blocks test-to-main promotion."""
    return any(result.get("result") == "fail" for result in results)


def _print_cell(r: dict) -> None:
    for name, status, detail in r["steps"]:
        print(f"  {_ICON.get(status, status)} {name}" + (f" — {detail}" if detail else ""))


if __name__ == "__main__":
    sys.exit(main())
