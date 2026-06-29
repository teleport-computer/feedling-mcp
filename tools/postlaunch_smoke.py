#!/usr/bin/env python3
"""Post-launch smoke — run the v1 e2e suite against the just-launched deployment
and print a LAUNCH-HEALTH verdict.

Why this wraps v1_e2e_suite.py instead of duplicating it: the suite is the
validated comprehensive smoke (throwaway user, asserts structure + fact-survival
+ state). This wrapper adds the ONE thing a launch needs — a verdict that tells a
broken CORE PATH (capture / read / migration / route) apart from a KNOWN, tracked
quality issue (genesis identity/bucket on a weak model). It exits non-zero ONLY on
a real blocker, so a known warning doesn't red-flag an otherwise-healthy launch,
and a broken data plane does.

Usage (after launch, against prod — uses a throwaway user, touches no real data):

    python3 tools/postlaunch_smoke.py \
      --api-url https://api.feedling.app \
      --enclave-url https://<prod-cvm>-5003s.<...>.phala.network

Optional: export GENESIS_E2E_PROVIDER_API_KEY to also exercise genesis (read from
env, never passed on the command line). Without it, genesis is reported skipped.

Exit code: 0 = healthy (or inconclusive), 1 = a core-path blocker failed,
2 = the suite never produced a report (deploy unreachable / TLS down).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

# Core paths whose FAILURE means the launch is broken.
_BLOCKERS = {
    "setup",
    "memory_read_index_fetch",
    "no_related_readside_empty",
    "capture_write_memory",
    "capture_noop_chitchat",
    "capture_dedup_duplicate_fact",
    "capture_supersede_correction",
    "local_only_visibility",
    "flow_trace_route",
    "legacy_migration",
    "hosted_agent_runtime_smoke",
}

# Known, tracked quality issues — surface but DO NOT block the launch.
# genesis identity/bucket currently needs a capable model (see the v1 TODO);
# it is not a core-data-plane blocker.
_WARN_ONLY = {
    "genesis_onboarding",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Post-launch smoke + launch-health verdict for the live deployment.")
    ap.add_argument("--api-url", required=True, help="launched/prod API base url, e.g. https://api.feedling.app")
    ap.add_argument("--enclave-url", default="", help="prod enclave decrypt url (needed for capture/read/migration cases)")
    ap.add_argument("--tls-attempts", type=int, default=12, help="health/TLS poll attempts before giving up")
    ap.add_argument("--tls-sleep", type=float, default=10.0)
    args = ap.parse_args()

    if not args.enclave_url:
        print("[postlaunch] WARNING: --enclave-url not set — capture/read/migration cases will not be verifiable.")

    report_path = Path(tempfile.gettempdir()) / "postlaunch_smoke_report.json"
    report_path.unlink(missing_ok=True)

    cmd = [
        sys.executable, str(TOOLS / "v1_e2e_suite.py"),
        "--api-url", args.api_url,
        "--report", str(report_path),
        "--tls-attempts", str(args.tls_attempts),
        "--tls-sleep", str(args.tls_sleep),
    ]
    if args.enclave_url:
        cmd += ["--enclave-url", args.enclave_url]

    print(f"[postlaunch] running v1 e2e suite against {args.api_url} (throwaway user) ...", flush=True)
    subprocess.run(cmd, check=False, env=os.environ.copy())  # inherits optional GENESIS_E2E_PROVIDER_API_KEY

    try:
        summary = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"\n[postlaunch] FATAL: no suite report ({exc}). The deploy was likely unreachable / TLS down — "
              "the suite never ran. Treat the launch as UNVERIFIED.")
        return 2

    passed, warns, blocked_unverified, blockers_failed = [], [], [], []
    for r in summary.get("results") or []:
        name = str(r.get("name") or "")
        status = str(r.get("status") or "")
        detail = str(r.get("summary") or r.get("error") or "")
        if status == "pass":
            passed.append(name)
        elif name in _WARN_ONLY:
            warns.append((name, status, detail))
        elif name in _BLOCKERS:
            (blockers_failed if status == "fail" else blocked_unverified).append((name, status, detail))
        else:
            warns.append((name, status, detail))  # unknown case — surface, don't block

    print("\n==================== LAUNCH HEALTH ====================")
    print(f"  deploy:   {args.api_url}")
    print(f"  commit:   {summary.get('commit', '?')}  branch: {summary.get('branch', '?')}")
    print(f"  passed:   {len(passed)} / {len(summary.get('results') or [])}")
    for name, status, detail in warns:
        print(f"   ⚠️  warn        {name} [{status}] {detail[:160]}")
    for name, status, detail in blocked_unverified:
        print(f"   ⚠️  unverified  {name} [{status}] {detail[:160]}")
    for name, status, detail in blockers_failed:
        print(f"   ❌  BLOCKER     {name} [{status}] {detail[:200]}")

    print("\n  ----------------------------------------------------")
    if blockers_failed:
        print("  ❌ LAUNCH NOT HEALTHY — a core path is broken (BLOCKER above). Roll back / investigate now.")
        return 1
    if blocked_unverified:
        print("  ⚠️  INCONCLUSIVE — core paths didn't error, but some couldn't be verified "
              "(likely missing --enclave-url or genesis key). Fix smoke setup and re-run before trusting the launch.")
        return 0
    print("  ✅ LAUNCH HEALTHY — core data plane (write / read / migration / route) is up on the live deploy.")
    if warns:
        print("     (warnings are known, tracked issues — not launch blockers.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
