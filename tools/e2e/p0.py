#!/usr/bin/env python3
"""P0 release smoke — one command, all cells, one result table.

  python3 tools/e2e/p0.py                     # everything configured
  python3 tools/e2e/p0.py --only anthropic-official,vps-claude-code
  python3 tools/e2e/p0.py --list              # show cells and key status

Exit code: 0 = no hard failures (skips/warns allowed), 1 = at least one FAIL
(protocol §8: any P0 fail blocks test→main). Test env only; every account
from a successful cell is deleted in teardown; failed cells retain a bounded
diagnostic site for seven days.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.config import HOSTED_CELLS, VPS_CELLS, KEYS_FILE, load_keys  # noqa: E402
from tools.e2e.hosted import run_hosted_cell  # noqa: E402
from tools.e2e.vps import run_vps_cell  # noqa: E402

_ICON = {"ok": "✅", "fail": "❌", "skip": "⏭️", "warn": "⚠️"}
_ADMIN_TOKEN_FILE = Path.home() / ".feedling" / "data-track-admin-token"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated cell names")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--cleanup-orphans", action="store_true",
                    help="delete accounts left behind by crashed runs (leak manifest)")
    ap.add_argument("--cleanup-expired-failures", action="store_true",
                    help="delete preserved failure sites after their 7-day retention")
    args = ap.parse_args()

    if args.cleanup_orphans:
        return _cleanup_orphans()
    if args.cleanup_expired_failures:
        return _cleanup_expired_failures()

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


def _admin_confirms_absent(api_url: str, user_id: str) -> tuple[bool | None, str]:
    """Return True only for admin 404; None when no local admin token exists."""
    import httpx as _httpx

    try:
        token = _ADMIN_TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        return None, "admin token unavailable"
    if not token:
        return None, "admin token empty"
    try:
        r = _httpx.get(
            f"{api_url}/v1/admin/data-track/users/{user_id}",
            headers={"X-Admin-Token": token}, timeout=30, verify=False,
        )
    except _httpx.TransportError as e:
        return False, f"admin verification transport error: {e}"
    if r.status_code == 404:
        return True, "admin confirmed 404"
    return False, f"admin verification returned {r.status_code}: {r.text[:80]}"


def _remove_failure_evidence(user_id: str) -> None:
    from tools.e2e.client import _FAILURES_DIR

    target = _FAILURES_DIR / user_id
    if target.is_dir() and target.parent == _FAILURES_DIR:
        shutil.rmtree(target)


def _cleanup_orphans(*, only_user_ids: set[str] | None = None) -> int:
    """Sweep ~/.feedling-e2e-orphans: reset each leaked account with its stored
    key. Manifest entries are removed ONLY on proof of deletion: 200 (deleted
    now) or 404 (account already gone). 401 means the key is invalid — the
    account may well still exist, so the entry is KEPT as the evidence trail
    (codex2 R1). Every manifest is validated through _refuse_prod before any
    request: a corrupted/hand-edited manifest must not let the sweeper POST a
    destructive reset at a non-test host. Bad files are reported, kept, and
    never abort the rest of the sweep."""
    import json as _json

    import httpx as _httpx

    from tools.e2e.client import _ORPHANS_DIR, _refuse_prod
    files = sorted(_ORPHANS_DIR.glob("*.json")) if _ORPHANS_DIR.exists() else []
    if only_user_ids is not None:
        files = [f for f in files if f.stem in only_user_ids]
    if not files:
        print("no orphaned e2e accounts recorded")
        return 0
    remaining = 0
    for f in files:
        try:
            creds = _json.loads(f.read_text())
            api_url = str(creds["api_url"])
            user_id = str(creds["user_id"])
            api_key = str(creds["api_key"])
            _refuse_prod(api_url)               # the package's test-only line holds here too
        except Exception as e:  # noqa: BLE001 — one bad manifest must not stop the sweep
            print(f"  ❌ {f.name}: bad/unsafe manifest ({type(e).__name__}: {e}); kept")
            remaining += 1
            continue
        try:
            r = _httpx.post(f"{api_url}/v1/account/reset",
                            headers={"X-API-Key": api_key},
                            json={"confirm": "delete-all-data"},
                            timeout=30, verify=False)
        except _httpx.TransportError as e:
            print(f"  ⏳ {user_id}: transport error ({e}); kept for next sweep")
            remaining += 1
            continue
        if r.status_code in (200, 404):
            state = "deleted" if r.status_code == 200 else "already gone (404)"
            verified, verify_detail = _admin_confirms_absent(api_url, user_id)
            if verified is False:
                print(f"  ❌ {user_id}: {state}, but {verify_detail}; kept")
                remaining += 1
                continue
            suffix = f"; {verify_detail}" if verified else "; reset response is deletion proof"
            print(f"  ✅ {user_id}: {state}{suffix}")
            f.unlink(missing_ok=True)
            _remove_failure_evidence(user_id)
        else:
            # 401 lands here on purpose: invalid key ≠ deleted account.
            print(f"  ❌ {user_id}: {r.status_code} {r.text[:80]}; kept")
            remaining += 1
    return 1 if remaining else 0


def _cleanup_expired_failures() -> int:
    """Seven-day retention sweep. Account deletion precedes local evidence removal."""
    from tools.e2e.client import FAILURE_RETENTION_DAYS, _FAILURES_DIR

    if not _FAILURES_DIR.exists():
        print("no preserved e2e failure sites recorded")
        return 0
    now = datetime.now(timezone.utc)
    expired: set[str] = set()
    remaining = 0
    for run_dir in sorted(p for p in _FAILURES_DIR.iterdir() if p.is_dir()):
        try:
            manifest = json.loads((run_dir / "evidence.json").read_text())
            created = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", "+00:00"))
            days = int(manifest.get("retention_days", FAILURE_RETENTION_DAYS))
            if (now - created).total_seconds() >= days * 86400:
                expired.add(str(manifest["user_id"]))
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {run_dir.name}: unreadable failure manifest ({e}); kept")
            remaining += 1
    if not expired:
        print("no preserved e2e failure sites are past retention")
        return 1 if remaining else 0
    cleanup_rc = _cleanup_orphans(only_user_ids=expired)
    return 1 if (remaining or cleanup_rc) else 0


def _print_cell(r: dict) -> None:
    for name, status, detail in r["steps"]:
        print(f"  {_ICON.get(status, status)} {name}" + (f" — {detail}" if detail else ""))


if __name__ == "__main__":
    sys.exit(main())
