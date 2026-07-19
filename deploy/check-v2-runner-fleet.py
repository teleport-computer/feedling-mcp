#!/usr/bin/env python3
"""Fail-closed post-deploy proof for every production Runtime V2 runner CVM."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from model_api_runtime.v2.runner_identity import (  # noqa: E402
    fleet_deployment_id,
    parse_fleet_worker_id,
    validate_build,
    validate_cvm_id,
)


# These waits deliberately exceed the backend's aggregate liveness windows.
# Reusing the same build on a rerun must not let a heartbeat from a container
# that stopped just before this job began satisfy the rollout proof.
DEFAULT_INITIAL_WAIT_SEC = 65.0
DEFAULT_TURN_MAX_AGE_SEC = 20.0
DEFAULT_GENESIS_MAX_AGE_SEC = 30.0


def read_inventory(path: Path) -> list[str]:
    values: list[str] = []
    for raw in path.read_text().splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        values.append(validate_cvm_id(value))
    if not values:
        raise ValueError(f"{path} contains no runner CVM identities")
    if len(values) != len(set(values)):
        raise ValueError(f"{path} contains duplicate runner CVM identities")
    return values


def _age(row: dict[str, Any]) -> float:
    try:
        value = row.get("age_sec")
        return float(value) if value is not None else float("inf")
    except (TypeError, ValueError):
        return float("inf")


def evaluate_payload(
    payload: dict[str, Any],
    *,
    cvm_ids: list[str],
    build: str,
    turn_max_age_sec: float = DEFAULT_TURN_MAX_AGE_SEC,
    genesis_max_age_sec: float = DEFAULT_GENESIS_MAX_AGE_SEC,
) -> tuple[bool, str]:
    """Return whether one payload proves the complete exact fleet identity set."""
    expected_deployments = {(cvm_id, build) for cvm_id in cvm_ids}
    policy = payload.get("runtime_policy") or {}
    try:
        eligible = int(policy.get("eligible_count") or 0)
        ready = int(policy.get("ready_count") or 0)
        inconsistent = int(policy.get("inconsistent_count") or 0)
    except (TypeError, ValueError):
        return False, f"invalid runtime_policy counters: {policy!r}"

    policy_ready = (
        policy.get("policy") == "v2_only"
        and policy.get("target_mode") == "db_action_v2"
        and ready == eligible
        and inconsistent == 0
    )

    fresh_turn: set[str] = set()
    fresh_genesis: set[str] = set()
    for row in payload.get("worker_heartbeats") or []:
        if not isinstance(row, dict):
            continue
        worker_id = str(row.get("worker_id") or "")
        kind = row.get("kind")
        if (
            kind == "turn"
            and _age(row) <= turn_max_age_sec
            and int(row.get("capacity") or 0) > 0
        ):
            fresh_turn.add(worker_id)
        elif kind == "genesis" and _age(row) <= genesis_max_age_sec:
            fresh_genesis.add(worker_id)

    # Compare exact current-build fleet sets. This rejects missing inventory
    # identities as well as an unlisted current-build worker. Previous-build or
    # ephemeral rows cannot substitute for an expected pair.
    turns_by_deployment: dict[tuple[str, str], list[str]] = {}
    for worker_id in fresh_turn:
        parsed = parse_fleet_worker_id(worker_id)
        if parsed is not None and parsed[1] == build:
            turns_by_deployment.setdefault(parsed[:2], []).append(worker_id)
    genesis_by_deployment: dict[tuple[str, str], list[str]] = {}
    for worker_id in fresh_genesis:
        if not worker_id.endswith(":genesis"):
            continue
        parsed = parse_fleet_worker_id(worker_id.removesuffix(":genesis"))
        if parsed is not None and parsed[1] == build:
            genesis_by_deployment.setdefault(parsed[:2], []).append(worker_id)

    observed_deployments = set(turns_by_deployment)
    missing_turn = sorted(expected_deployments - observed_deployments)
    extra_turn = sorted(observed_deployments - expected_deployments)
    duplicate_turn = sorted(
        deployment
        for deployment, worker_ids in turns_by_deployment.items()
        if len(worker_ids) != 1
    )
    missing_genesis: list[tuple[str, str]] = []
    extra_genesis = sorted(set(genesis_by_deployment) - expected_deployments)
    mismatched_boot: list[tuple[str, str]] = []
    for deployment in sorted(expected_deployments):
        turn_ids = turns_by_deployment.get(deployment, [])
        genesis_ids = genesis_by_deployment.get(deployment, [])
        if not genesis_ids:
            missing_genesis.append(deployment)
        elif len(turn_ids) == 1 and genesis_ids != [f"{turn_ids[0]}:genesis"]:
            mismatched_boot.append(deployment)

    ok = (
        policy_ready
        and payload.get("genesis_alive") is True
        and missing_turn == []
        and extra_turn == []
        and duplicate_turn == []
        and missing_genesis == []
        and extra_genesis == []
        and mismatched_boot == []
    )
    detail = (
        f"build={build} policy={policy.get('policy')} "
        f"target={policy.get('target_mode')} ready={ready}/{eligible} "
        f"inconsistent={inconsistent} expected_cvm_count={len(cvm_ids)} "
        f"missing_turn={[fleet_deployment_id(*item) for item in missing_turn]} "
        f"extra_turn={[fleet_deployment_id(*item) for item in extra_turn]} "
        f"duplicate_turn={[fleet_deployment_id(*item) for item in duplicate_turn]} "
        f"missing_genesis={[fleet_deployment_id(*item) for item in missing_genesis]} "
        f"extra_genesis={[fleet_deployment_id(*item) for item in extra_genesis]} "
        f"mismatched_boot={[fleet_deployment_id(*item) for item in mismatched_boot]}"
    )
    return ok, detail


def fetch_metrics(base_url: str, admin_token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/admin/v2-metrics",
        headers={"X-Admin-Token": admin_token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("v2-metrics response must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--initial-wait-sec", type=float, default=DEFAULT_INITIAL_WAIT_SEC)
    parser.add_argument("--attempts", type=int, default=30)
    parser.add_argument("--poll-sec", type=float, default=5.0)
    args = parser.parse_args(argv)

    token = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not token:
        raise SystemExit("FEEDLING_ADMIN_TOKEN is required")
    build = validate_build(args.build)
    cvm_ids = read_inventory(args.inventory)
    if args.initial_wait_sec < DEFAULT_INITIAL_WAIT_SEC:
        raise SystemExit(
            f"--initial-wait-sec must be >= {DEFAULT_INITIAL_WAIT_SEC:.0f}s "
            "to outlive prior heartbeat freshness"
        )

    print(
        f"waiting {args.initial_wait_sec:.0f}s before proving "
        f"{len(cvm_ids)} Runtime V2 runner CVM(s) at build {build}"
    )
    time.sleep(args.initial_wait_sec)
    last_error = "no response"
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            payload = fetch_metrics(args.base_url, token)
            ok, detail = evaluate_payload(payload, cvm_ids=cvm_ids, build=build)
            if ok:
                print(f"production Runtime V2 fleet proof passed: {detail}")
                return 0
            last_error = detail
        except Exception as exc:  # transient startup/network window
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < args.attempts:
            time.sleep(max(0.0, args.poll_sec))

    print(f"production Runtime V2 fleet proof failed: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
