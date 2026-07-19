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


def test_topology_gate_rejects_single_runner(tmp_path):
    empty = _run(tmp_path, "# no runners yet\n")
    assert empty.returncode == 1
    assert "production has 0" in empty.stdout

    result = _run(tmp_path, "# prod runners\nrunner-a\n")
    assert result.returncode == 1
    assert "at least 2" in result.stdout


def test_topology_gate_counts_unique_runner_ids(tmp_path):
    duplicate = _run(tmp_path, "runner-a\nrunner-a\n")
    assert duplicate.returncode == 1

    redundant = _run(tmp_path, "runner-a\n\n# separate failure domain\nrunner-b\n")
    assert redundant.returncode == 0
    assert "2 independent" in redundant.stdout


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
