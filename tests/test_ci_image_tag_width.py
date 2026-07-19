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

    # One producer assignment plus every main/test/pre main+runner deploy wait
    # and pin assignment. A variable-length `git rev-parse --short` can produce
    # different widths in the publisher's shallow clone and deploy's full clone.
    # The shared production preflight adds one assignment and proves both
    # backend + mandatory runner images before either live CVM is mutated.
    assert len(assignments) == 15
    assert set(assignments) == {'"${GITHUB_SHA:0:7}"'}


def test_pre_runner_deploy_forwards_pool_size_and_gates_liveness():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "PRE_FEEDLING_V2_MAX_WORKERS || '4'" in workflow
    assert '-e "FEEDLING_V2_MAX_WORKERS=$FEEDLING_V2_MAX_WORKERS"' in workflow
    assert "Post-deploy Runtime V2 liveness gate (pre)" in workflow
    assert 'headers={"X-Admin-Token": admin_token}' in workflow
    assert 'payload.get("live_worker_capacity")' in workflow
    assert 'payload.get("genesis_alive") is True' in workflow
    assert 'runtime_policy.get("policy") == expected_policy' in workflow
    assert 'runtime_policy.get("target_mode") == expected_mode' in workflow
    assert 'runtime_policy.get("inconsistent_count") or 0' in workflow
    assert "vars.DEPLOY_PRE_RUNNER_CVM == 'true'" not in workflow
    assert 'payload.get("worker_heartbeats")' in workflow
    assert 'expected_commit = os.environ.get("GITHUB_SHA", "")[:7]' in workflow
    assert "time.sleep(35)" in workflow
    assert "def heartbeat_age(row):" in workflow
    assert 'row.get("age_sec") or 9999' not in workflow
    assert 'f"-{expected_commit}"' in workflow
    assert 'f"-{expected_commit}:genesis"' in workflow


def test_prod_runner_deploy_binds_every_inventory_cvm_to_exact_build():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert 'DEPLOYED_BUILD="${GITHUB_SHA:0:7}"' in workflow
    assert '-e "FEEDLING_V2_RUNNER_CVM_ID=$CVM_ID"' in workflow
    assert '-e "FEEDLING_V2_DEPLOYED_BUILD=$DEPLOYED_BUILD"' in workflow
    assert "Post-deploy Runtime V2 fleet identity gate (prod)" in workflow
    assert "deploy/check-v2-runner-fleet.py" in workflow
