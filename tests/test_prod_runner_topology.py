from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "deploy" / "check-prod-runner-topology.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    ids = tmp_path / "ids.txt"
    ids.write_text(body)
    return subprocess.run(
        ["bash", str(SCRIPT), str(ids)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ},
    )


def test_topology_gate_warns_but_does_not_block_below_two_runners(tmp_path):
    # Dual-runtime coexistence (2026-07-21 design, Task 11): the prod runner
    # fleet is V1 agent-runner again (V1 resident is the fallback, not pooled
    # Runtime V2), so this script's ≥2-CVM redundancy check now defaults to a
    # warning, not a hard block (the default polarity + disable switch are
    # new as of Task 11, not a restoration of origin/test's script — see
    # check-prod-runner-topology.sh's header comment). A hard "≥2 CVMs" block
    # would now permanently wedge every prod main-CVM deploy — only one
    # runner CVM is currently provisioned.
    empty = _run(tmp_path, "# no runners yet\n")
    assert empty.returncode == 0
    assert "production has only 0 standalone runner CVM(s)" in empty.stdout

    result = _run(tmp_path, "# prod runners\nrunner-a\n")
    assert result.returncode == 0
    assert "production has only 1 standalone runner CVM(s)" in result.stdout


def test_topology_gate_hard_fails_when_explicitly_enforced(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("runner-a\n")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(ids)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PROD_RUNNER_TOPOLOGY_ENFORCE": "true"},
    )
    assert result.returncode == 1
    assert "at least 2 are required" in result.stdout


def test_topology_gate_counts_unique_runner_ids(tmp_path):
    duplicate = _run(tmp_path, "runner-a\nrunner-a\n")
    assert duplicate.returncode == 0
    assert "production has only 1 standalone runner CVM(s)" in duplicate.stdout

    redundant = _run(tmp_path, "runner-a\n\n# separate failure domain\nrunner-b\n")
    assert redundant.returncode == 0
    assert "2 independent" in redundant.stdout


def test_topology_gate_can_be_disabled(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("# empty\n")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(ids)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ},
        cwd=None,
    )
    # sanity: with no third arg, the gate defaults to enabled (matches CI's
    # `${{ vars.DEPLOY_PROD_RUNNER_CVM || 'true' }}` default)
    assert result.returncode == 0
    disabled = subprocess.run(
        ["bash", str(SCRIPT), str(ids), str(ROOT / "deploy" / "prod-cvm-id.txt"), "false"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ},
    )
    assert disabled.returncode == 0
    assert "redundancy gate is inactive" in disabled.stdout


def test_topology_gate_rejects_the_main_cvm_as_a_runner(tmp_path):
    main_id = (ROOT / "deploy" / "prod-cvm-id.txt").read_text().strip()
    result = _run(tmp_path, f"runner-a\n{main_id}\n")
    assert result.returncode == 1
    assert "main CVM" in result.stdout


def test_main_cvm_membership_check_runs_even_when_redundancy_gate_is_disabled(
    tmp_path,
):
    # Code-review regression: an earlier version of this script let the
    # `enabled=false` early-return skip the "main CVM must never appear in
    # the runner inventory" footgun guard too. That guard is unconditional —
    # it must fire regardless of whether the redundancy preflight is active.
    main_id = (ROOT / "deploy" / "prod-cvm-id.txt").read_text().strip()
    ids = tmp_path / "ids.txt"
    ids.write_text(f"runner-a\n{main_id}\n")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(ids), str(ROOT / "deploy" / "prod-cvm-id.txt"), "false"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ},
    )
    assert result.returncode == 1
    assert "main CVM" in result.stdout
    assert "topology gate is inactive" not in result.stdout


def test_main_deploy_depends_on_topology_preflight():
    workflow = WORKFLOW.read_text()
    assert "validate-prod-runner-topology:" in workflow
    deploy = workflow.split("\n  deploy-cvm:\n", 1)[1].split("\n  deploy-test-cvm:\n", 1)[0]
    assert "validate-prod-runner-topology" in "\n".join(deploy.split("\n", 8)[0:8])


def test_prod_inventory_and_fleet_gates_trigger_the_shared_preflight():
    workflow = WORKFLOW.read_text()
    prod_filter = workflow.split("\n  detect-cvm-changes:\n", 1)[1].split(
        "\n  detect-cvm-changes-test:\n", 1
    )[0]
    for path in (
        "deploy/prod-cvm-id.txt",
        "deploy/prod-runner-cvm-ids.txt",
        "deploy/check-prod-runner-topology.sh",
        "deploy/check-v2-runner-fleet.py",
    ):
        assert path in prod_filter
    assert "tools/chat_resident_consumer.py" not in prod_filter

    preflight = workflow.split("\n  validate-prod-runner-topology:\n", 1)[1].split(
        "\n  detect-cvm-changes-pre:\n", 1
    )[0]
    assert "feedling feedling-agent-runner" in preflight
    assert "docker manifest inspect" in preflight
    assert 'phala cvms get "$CVM_ID"' in preflight
    assert "production CVM $CVM_ID does not exist" in preflight
    assert "Build and verify the production E2B template" in preflight
    assert "deploy/e2b/runtime-v2/template-tag.txt" in preflight
