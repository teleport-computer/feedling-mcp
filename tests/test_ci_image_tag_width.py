"""CI image producer and deploy consumers must derive identical image tags."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parent.parent
ASSIGNMENT = re.compile(
    r'^\s*(?:COMMIT_SHORT|SHA_SHORT|TRIGGER_SHA_SHORT)=(.+)$',
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
    assert len(assignments) == 13
    assert set(assignments) == {'"${GITHUB_SHA:0:12}"'}


def test_pre_runner_deploy_forwards_pool_size_and_gates_liveness():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "PRE_FEEDLING_V2_MAX_WORKERS || '4'" in workflow
    assert '-e "FEEDLING_V2_MAX_WORKERS=$FEEDLING_V2_MAX_WORKERS"' in workflow
    assert "Post-deploy Runtime V2 liveness gate (pre)" in workflow
    assert 'headers={"X-Admin-Token": admin_token}' in workflow
    assert 'payload.get("live_worker_capacity")' in workflow
    assert 'payload.get("genesis_alive") is True' in workflow
    assert 'payload.get("worker_heartbeats")' in workflow
    assert 'expected_commit = os.environ.get("GITHUB_SHA", "")[:12]' in workflow
    assert "time.sleep(35)" in workflow
    assert "def heartbeat_age(row):" in workflow
    assert 'row.get("age_sec") or 9999' not in workflow
    assert 'f"-{expected_commit}"' in workflow
    assert 'f"-{expected_commit}:genesis"' in workflow
