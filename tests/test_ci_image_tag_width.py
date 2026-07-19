"""CI image producer and deploy consumers must derive identical image tags."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parent.parent
# Capture ONLY the quoted GITHUB_SHA slice, ignoring any trailing inline comment
# (the mainline CI #906 lines carry a `# fixed 7-char slice…` note).
ASSIGNMENT = re.compile(
    r'^\s*(?:COMMIT_SHORT|SHA_SHORT|TRIGGER_SHA_SHORT|DEPLOYED_BUILD)='
    r'("\$\{GITHUB_SHA:0:\d+\}")',
    re.MULTILINE,
)


def test_image_tags_use_one_fixed_width_github_sha_prefix():
    workflows = (
        ROOT / ".github" / "workflows" / "docker-publish.yml",
        ROOT / ".github" / "workflows" / "ci.yml",
    )
    assignments = [
        value.strip()
        for workflow in workflows
        for value in ASSIGNMENT.findall(workflow.read_text())
    ]

    # One producer assignment plus every main/test/pre main+runner deploy wait,
    # fleet-build marker, and release preflight. Image pinning is centralized in
    # deploy/pin-runtime-release.sh and slices the same trigger SHA exactly once.
    # A variable-length `git rev-parse --short` can drift between clone depths.
    assert len(assignments) == 13
    assert set(assignments) == {'"${GITHUB_SHA:0:7}"'}


def test_pre_runner_deploy_forwards_pool_size_and_gates_exact_fleet_liveness():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "PRE_FEEDLING_V2_MAX_WORKERS || '4'" in workflow
    assert '-e "FEEDLING_V2_MAX_WORKERS=$FEEDLING_V2_MAX_WORKERS"' in workflow
    assert '-e "FEEDLING_V2_RUNNER_CVM_ID=$FEEDLING_V2_RUNNER_CVM_ID"' in workflow
    assert '-e "FEEDLING_V2_DEPLOYED_BUILD=$DEPLOYED_BUILD"' in workflow
    assert "Post-deploy Runtime V2 liveness gate (pre)" in workflow
    assert "python3 deploy/check-v2-runner-fleet.py" in workflow
    assert "--inventory deploy/pre-runner-cvm-id.txt" in workflow
    assert "vars.DEPLOY_PRE_RUNNER_CVM == 'true'" not in workflow


def test_test_runner_deploy_uses_the_same_closed_world_fleet_gate():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "validate-test-runtime-prerequisites:" in workflow
    assert "Post-deploy Runtime V2 fleet identity gate (test)" in workflow
    assert "--inventory deploy/test-runner-cvm-id.txt" in workflow
    assert '-e "FEEDLING_V2_RUNNER_CVM_ID=$FEEDLING_V2_RUNNER_CVM_ID"' in workflow
    assert '-e "FEEDLING_V2_DEPLOYED_BUILD=$DEPLOYED_BUILD"' in workflow


def test_prod_runner_deploy_binds_every_inventory_cvm_to_exact_build():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert 'DEPLOYED_BUILD="${GITHUB_SHA:0:7}"' in workflow
    assert '-e "FEEDLING_V2_RUNNER_CVM_ID=$CVM_ID"' in workflow
    assert '-e "FEEDLING_V2_DEPLOYED_BUILD=$DEPLOYED_BUILD"' in workflow
    assert "Post-deploy Runtime V2 fleet identity gate (prod)" in workflow
    assert "deploy/check-v2-runner-fleet.py" in workflow
