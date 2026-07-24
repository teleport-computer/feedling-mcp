"""Deep functional qualification driver for pre Runtime V2.

Runs the four area probes (memory / continuity / proactive / perception) across
the six provider key classes against the pre deployment, aggregates with the
qa-SOP failure taxonomy, and tears every synthetic account down.

    export NO_PROXY='*' \
      FEEDLING_E2E_API=https://pre-api.feedling.app \
      FEEDLING_E2E_ENCLAVE=https://7d18a1f2...-5003s.dstack-pha-prod9.phala.network \
      FEEDLING_ADMIN_TOKEN=...            # for proactive/perception admin observation
    python3 -m tools.e2e.deep                       # all 6 providers, all areas
    python3 -m tools.e2e.deep --providers anthropic-official --areas memory,continuity

Provider-independent backend invariants run once (on the first provider whose
setup succeeds). Model-touching cases run per provider.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config
from .client import E2EClient, TEST_API
from .config import HOSTED_CELLS
from .memory_probe import run_memory_probe
from .continuity_probe import run_continuity_probe
from .experience_probe import run_experience_probe
from .probe_common import (
    AGENT_ERROR, BLOCKED_CREDENTIAL, BLOCKED_DEPLOYMENT, BLOCKED_EVIDENCE,
    BLOCKING, PASS, worst,
)

# codex2's probes — import lazily so the driver still runs before they land, but a
# requested-yet-unavailable area is surfaced as AGENT_ERROR (never silently dropped).
_IMPORT_ERR: dict[str, str] = {}
try:
    from .proactive_probe import run_proactive_probe
except Exception as _e:  # noqa: BLE001
    run_proactive_probe = None
    _IMPORT_ERR["proactive"] = f"{type(_e).__name__}: {_e}"
try:
    from .perception_probe import run_perception_probe
except Exception as _e:  # noqa: BLE001
    run_perception_probe = None
    _IMPORT_ERR["perception"] = f"{type(_e).__name__}: {_e}"

SETUP_RETRY_SEC = 90.0


def _setup(c: E2EClient, cell, pool) -> tuple[bool, str, str]:
    """Try model candidates until the live self-test passes. Returns (ok, model, detail)."""
    models = cell.models or [m for m in [pool.get("E2E_RELAY_MODEL", "")] if m]
    base = cell.base_url(pool)
    deadline = time.time() + SETUP_RETRY_SEC
    detail = ""
    while time.time() < deadline:
        for model in models:
            payload = {"provider": cell.provider, "model": model, "api_key": cell.key(pool)}
            if base:
                payload["base_url"] = base
            r = c.post("/v1/model_api/setup", json=payload)
            if r.status_code == 200:
                body = r.json()
                ts = ((body.get("config") or {}).get("test_status")
                      or body.get("test_status") or "")
                if ts == "ok":
                    return True, model, f"model={model}"
                detail = f"model={model} test_status={ts or '?'}"
            elif r.status_code == 503 and any(
                    x in r.text for x in ("workers_unavailable", "runtime_policy_not_ready")):
                time.sleep(5)
                break
            else:
                detail = f"{model}: {r.status_code} {r.text[:100]}"
        else:
            break
    return False, "", detail


def run_provider(cell, pool, *, run_invariants: bool, areas: set[str]) -> dict:
    key = cell.key(pool)
    if not key:
        return {"cell": cell.name, "provider": cell.provider, "setup": "no_key",
                "probes": [{"area": a, "cases": [{"name": "provision",
                            "result": BLOCKED_CREDENTIAL,
                            "detail": f"no {cell.key_env} in pool"}]} for a in sorted(areas)]}

    runners = {
        "memory": run_memory_probe, "continuity": run_continuity_probe,
        "proactive": run_proactive_probe, "perception": run_perception_probe,
        "experience": run_experience_probe,
    }
    # context manager guarantees teardown AND _http.close() on every exit
    with E2EClient.provision(route="model_api") as c:
        ok, model, detail = _setup(c, cell, pool)
        cfg = {"cell": cell.name, "provider": cell.provider, "model": model,
               "key": key, "base_url": cell.base_url(pool) or "",
               "run_invariants": run_invariants and ok}
        if not ok:
            block = BLOCKED_DEPLOYMENT if "runtime" in detail or "503" in detail else BLOCKED_CREDENTIAL
            return {"cell": cell.name, "provider": cell.provider, "setup": f"FAIL {detail}",
                    "probes": [{"area": a, "cases": [{"name": "setup", "result": block,
                                "detail": detail}]} for a in sorted(areas)]}
        probes = []
        # continuity runs FIRST so its cold-start case measures the account's genuine
        # first chat turn (memory/capture also sends chat). experience runs LAST: its
        # error-attribution case removes the model config, breaking any later probe.
        for area in ("continuity", "memory", "proactive", "perception", "experience"):
            if area not in areas:
                continue
            runner = runners.get(area)
            if runner is None:
                # requested but the probe module failed to import — never silently
                # omit a requested area (that would let an incomplete run go green).
                probes.append({"area": area, "cases": [{"name": "probe_available",
                    "result": AGENT_ERROR,
                    "detail": f"{area}_probe unavailable: {_IMPORT_ERR.get(area, 'not importable')}"}]})
                continue
            probes.append(runner(c, cfg))
        return {"cell": cell.name, "provider": cell.provider, "setup": f"ok {detail}",
                "probes": probes}


def _all_cases(report: list[dict]) -> list[tuple]:
    out = []
    for prov in report:
        for probe in prov.get("probes", []):
            for case in probe.get("cases", []):
                out.append((prov["provider"], probe["area"], case["name"],
                            case["result"], case["detail"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="", help="comma list of cell names (default: all 6)")
    ap.add_argument("--areas", default="memory,continuity,proactive,perception,experience")
    ap.add_argument("--diagnostic", action="store_true",
                    help="tolerate BLOCKED_EVIDENCE (exit 0); default qualification mode "
                         "exits nonzero for EVERY non-PASS per qa SOP")
    args = ap.parse_args()

    # Fail closed: never qualify the wrong deployment. This suite exists to test
    # pre Runtime V2; an unset/wrong FEEDLING_E2E_API must abort, not silently run
    # against the default test env and report a green pre.
    if TEST_API.rstrip("/") != "https://pre-api.feedling.app":
        print(f"[deep] REFUSING to run: target is {TEST_API}, expected "
              f"https://pre-api.feedling.app (set FEEDLING_E2E_API)", file=sys.stderr)
        return 2

    known_areas = {"memory", "continuity", "proactive", "perception", "experience"}
    areas = {a.strip() for a in args.areas.split(",") if a.strip()}
    bad_areas = areas - known_areas
    if bad_areas or not areas:
        print(f"[deep] invalid --areas {sorted(bad_areas) or 'empty'}; "
              f"known: {sorted(known_areas)}", file=sys.stderr)
        return 2

    pool = config.load_keys()
    cells = HOSTED_CELLS
    if args.providers:
        want = {x.strip() for x in args.providers.split(",")}
        known = {c.name for c in HOSTED_CELLS}
        bad = want - known
        if bad:
            print(f"[deep] unknown --providers {sorted(bad)}; known: {sorted(known)}",
                  file=sys.stderr)
            return 2
        cells = [c for c in HOSTED_CELLS if c.name in want]
    if not cells:
        print("[deep] no provider cells selected", file=sys.stderr)
        return 2

    report: list[dict] = []
    invariants_done = False
    for cell in cells:
        run_inv = not invariants_done
        print(f"\n===== provider: {cell.name} (invariants={run_inv}) =====", flush=True)
        prov = run_provider(cell, pool, run_invariants=run_inv, areas=areas)
        report.append(prov)
        print(f"  setup: {prov['setup']}", flush=True)
        for probe in prov.get("probes", []):
            for case in probe["cases"]:
                print(f"    [{probe['area']:11s}] {case['name']:34s} {case['result']:18s} "
                      f"{case['detail'][:70]}", flush=True)
        # invariants counted as done once a provider actually ran them (setup ok)
        if run_inv and prov["setup"].startswith("ok"):
            invariants_done = True

    cases = _all_cases(report)
    if not cases:
        print("[deep] empty report — no cases executed", file=sys.stderr)
        return 2
    overall = worst([r for *_ , r, _ in cases])
    blocked = [(p, a, n, r, d) for (p, a, n, r, d) in cases if r in BLOCKING]
    evidence = [(p, a, n, r, d) for (p, a, n, r, d) in cases if r == BLOCKED_EVIDENCE]

    print("\n" + "=" * 72)
    print(f"OVERALL: {overall}   ({len(cases)} cases, {len(blocked)} release-blocking, "
          f"{len(evidence)} blocked-evidence)")
    if blocked:
        print("BLOCKING:")
        for (p, a, n, r, d) in blocked:
            print(f"  {r:14s} {p:22s} {a}/{n}: {d[:80]}")
    if evidence:
        tag = "tolerated in --diagnostic" if args.diagnostic else "NON-PASS in qualification mode"
        print(f"BLOCKED_EVIDENCE ({tag}):")
        for (p, a, n, r, d) in evidence:
            print(f"  {p:22s} {a}/{n}: {d[:80]}")
    stamp = int(time.time())
    out_path = f"/tmp/pre_v2_deep_{stamp}.json"
    with open(out_path, "w") as f:
        json.dump({"target": TEST_API, "overall": overall, "report": report}, f,
                  ensure_ascii=False, indent=1)
    print(f"full report → {out_path}")
    # qualification mode (default): ANY non-PASS fails (qa SOP — overall PASS needs
    # every status PASS). --diagnostic tolerates only BLOCKED_EVIDENCE.
    if args.diagnostic:
        return 1 if blocked else 0
    return 0 if overall == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
