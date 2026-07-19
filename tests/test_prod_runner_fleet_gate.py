from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "deploy" / "check-v2-runner-fleet.py"
SPEC = importlib.util.spec_from_file_location("check_v2_runner_fleet", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

from model_api_runtime.v2 import runner_identity


BUILD = "abcdef1"
CVMS = ["cvm-a", "cvm-b"]


def test_gate_wait_outlives_turn_and_genesis_freshness_windows():
    assert gate.DEFAULT_INITIAL_WAIT_SEC > 60
    assert gate.DEFAULT_TURN_MAX_AGE_SEC < gate.DEFAULT_INITIAL_WAIT_SEC
    assert gate.DEFAULT_GENESIS_MAX_AGE_SEC < gate.DEFAULT_INITIAL_WAIT_SEC


def _row(worker_id, kind, *, age=1.0, capacity=0):
    return {
        "worker_id": worker_id,
        "kind": kind,
        "capacity": capacity,
        "age_sec": age,
    }


def _payload(cvm_ids=CVMS, *, build=BUILD):
    rows = []
    for index, cvm_id in enumerate(cvm_ids, start=1):
        worker_id = runner_identity.fleet_worker_id(cvm_id, build, f"{index:012x}")
        rows.append(_row(worker_id, "turn", capacity=4))
        rows.append(_row(f"{worker_id}:genesis", "genesis"))
    return {
        "worker_heartbeats": rows,
        "genesis_alive": True,
        "runtime_policy": {
            "policy": "v2_only",
            "target_mode": "db_action_v2",
            "eligible_count": 7,
            "ready_count": 7,
            "inconsistent_count": 0,
        },
    }


def test_complete_exact_inventory_build_passes():
    ok, detail = gate.evaluate_payload(_payload(), cvm_ids=CVMS, build=BUILD)
    assert ok is True, detail


def test_one_cvm_identity_cannot_stand_in_for_complete_inventory():
    payload = _payload(["cvm-a"])
    ok, detail = gate.evaluate_payload(payload, cvm_ids=CVMS, build=BUILD)
    assert ok is False
    assert gate.fleet_deployment_id("cvm-b", BUILD) in detail


def test_stale_or_previous_build_rows_cannot_satisfy_expected_identity():
    expected_a = gate.fleet_deployment_id("cvm-a", BUILD)
    payload = _payload(["cvm-a"])
    payload["worker_heartbeats"][0]["age_sec"] = 31
    previous_b = runner_identity.fleet_worker_id(
        "cvm-b", "1234567", "000000000002"
    )
    payload["worker_heartbeats"].extend(
        [
            _row(previous_b, "turn", capacity=4),
            _row(f"{previous_b}:genesis", "genesis"),
        ]
    )

    ok, detail = gate.evaluate_payload(payload, cvm_ids=CVMS, build=BUILD)

    assert ok is False
    assert expected_a in detail
    assert gate.fleet_deployment_id("cvm-b", BUILD) in detail


def test_unlisted_current_build_identity_is_rejected():
    payload = _payload([*CVMS, "cvm-rogue"])
    ok, detail = gate.evaluate_payload(payload, cvm_ids=CVMS, build=BUILD)
    assert ok is False
    assert "cvm-rogue" in detail


def test_turn_and_genesis_must_share_the_same_boot_identity():
    payload = _payload()
    genesis = next(
        row
        for row in payload["worker_heartbeats"]
        if row["worker_id"].startswith(gate.fleet_deployment_id("cvm-b", BUILD))
        and row["kind"] == "genesis"
    )
    genesis["worker_id"] = runner_identity.fleet_worker_id(
        "cvm-b", BUILD, "ffffffffffff"
    ) + ":genesis"

    ok, detail = gate.evaluate_payload(payload, cvm_ids=CVMS, build=BUILD)
    assert ok is False
    assert "mismatched_boot" in detail


def test_two_fresh_process_boots_for_one_deployment_are_rejected():
    payload = _payload()
    extra = runner_identity.fleet_worker_id("cvm-a", BUILD, "ffffffffffff")
    payload["worker_heartbeats"].extend(
        [
            _row(extra, "turn", capacity=4),
            _row(f"{extra}:genesis", "genesis"),
        ]
    )

    ok, detail = gate.evaluate_payload(payload, cvm_ids=CVMS, build=BUILD)
    assert ok is False
    assert "duplicate_turn" in detail


@pytest.mark.parametrize(
    "policy_update",
    [
        {"policy": "something_else"},
        {"target_mode": "resident_cli"},
        {"ready_count": 6},
        {"inconsistent_count": 1},
    ],
)
def test_runtime_policy_must_be_v2_only_and_fully_reconciled(policy_update):
    payload = _payload()
    payload["runtime_policy"].update(policy_update)
    ok, _detail = gate.evaluate_payload(payload, cvm_ids=CVMS, build=BUILD)
    assert ok is False


def test_inventory_rejects_duplicates(tmp_path):
    inventory = tmp_path / "ids.txt"
    inventory.write_text("cvm-a\ncvm-a\n")
    with pytest.raises(ValueError, match="duplicate"):
        gate.read_inventory(inventory)
