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

    # One producer assignment plus every main/test/pre main deploy wait and
    # the release preflights. Dual-runtime coexistence (Task 11, post-review
    # correction): ALL THREE standalone runner CVMs (test/pre/prod) are V1
    # agent-runner — test's `deploy-test-runner-cvm` job was also restored to
    # V1 form (it always should have matched origin/test's live
    # feedling-io-agents-test CVM; the V2-only shape was drift, never fully
    # deployed). V1 agent-runner has no FEEDLING_V2_DEPLOYED_BUILD
    # fleet-identity marker to slice a SHA for, so the assignment count
    # dropped from 13 (three fewer: test/pre/prod runner DEPLOYED_BUILD).
    # Image pinning is centralized in deploy/pin-runtime-release.sh and
    # slices the same trigger SHA exactly once. A variable-length
    # `git rev-parse --short` can drift between clone depths.
    assert len(assignments) == 10
    assert set(assignments) == {'"${GITHUB_SHA:0:7}"'}


def test_pre_runner_deploy_is_v1_agent_runner_not_pooled_v2():
    # Dual-runtime coexistence (Task 11): pooled Runtime V2 moved onto the
    # main pre CVM as the serve-worker container, so the standalone pre
    # runner CVM deploy job reverted to V1 agent-runner env vars and dropped
    # every V2-only post-deploy gate (no v2_worker_heartbeats identity exists
    # on a V1 runner to check).
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    pre_runner_job = workflow.split("\n  deploy-pre-runner-cvm:\n", 1)[1].split(
        "\n  # ====", 1
    )[0]

    assert '-e "AGENT_MAX_CHILDREN=$AGENT_MAX_CHILDREN"' in pre_runner_job
    assert '-e "AGENT_RUNTIME_USERS=$AGENT_RUNTIME_USERS"' in pre_runner_job
    assert '-e "FEEDLING_HOST_ALL=$FEEDLING_HOST_ALL"' in pre_runner_job
    assert "FEEDLING_V2_MAX_WORKERS" not in pre_runner_job
    assert "FEEDLING_V2_RUNNER_CVM_ID" not in pre_runner_job
    assert "Post-deploy Runtime V2 liveness gate (pre)" not in pre_runner_job
    assert "check-v2-runner-fleet.py" not in pre_runner_job
    assert "prompt_cache_canary.py" not in pre_runner_job


def test_test_runner_deploy_is_v1_agent_runner_not_pooled_v2():
    # Dual-runtime coexistence (Task 11, post-review correction): test's
    # standalone runner CVM is V1 agent-runner — same as prod/pre, not the
    # "unchanged, stays V2" claim from the pre-review version of this task
    # (that claim was based on a stale repo file, not test's actual live
    # feedling-io-agents-test CVM topology).
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    test_runner_job = workflow.split("\n  deploy-test-runner-cvm:\n", 1)[1].split(
        "\n  deploy-pre-cvm:\n", 1
    )[0]

    assert '-e "AGENT_MAX_CHILDREN=$AGENT_MAX_CHILDREN"' in test_runner_job
    assert '-e "AGENT_RUNTIME_USERS=$AGENT_RUNTIME_USERS"' in test_runner_job
    assert '-e "FEEDLING_HOST_ALL=$FEEDLING_HOST_ALL"' in test_runner_job
    assert "FEEDLING_V2_MAX_WORKERS" not in test_runner_job
    assert "FEEDLING_V2_RUNNER_CVM_ID" not in test_runner_job
    assert "Post-deploy Runtime V2 fleet identity gate (test)" not in test_runner_job
    assert "check-v2-runner-fleet.py" not in test_runner_job


def test_prod_runner_deploy_is_v1_agent_runner_not_pooled_v2():
    # Dual-runtime coexistence (Task 11): mirrors the pre-runner assertion
    # above for the production fleet — restored (from origin/test) to V1
    # agent-runner, no V2 fleet-identity post-deploy gate.
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    prod_runner_job = workflow.split("\n  deploy-prod-runner-cvm:\n", 1)[1].split(
        "\n  # ====", 1
    )[0]

    assert '-e "AGENT_MAX_CHILDREN=$AGENT_MAX_CHILDREN"' in prod_runner_job
    assert '-e "AGENT_RUNTIME_USERS=$AGENT_RUNTIME_USERS"' in prod_runner_job
    assert '-e "FEEDLING_HOST_ALL=$FEEDLING_HOST_ALL"' in prod_runner_job
    assert "FEEDLING_V2_RUNNER_CVM_ID" not in prod_runner_job
    assert "FEEDLING_V2_DEPLOYED_BUILD" not in prod_runner_job
    assert "Post-deploy Runtime V2 fleet identity gate (prod)" not in prod_runner_job
    assert "check-v2-runner-fleet.py" not in prod_runner_job
