from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-pr-branch-flow.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "branch-flow.yml"


def _run(base: str | None = None, head: str | None = None) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(SCRIPT)]
    if base is not None:
        args.append(base)
    if head is not None:
        args.append(head)
    return subprocess.run(args, capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("base", "head"),
    [
        ("main", "test"),
        ("main", "pre"),
        ("test", "feature/example"),
        ("pre", "fix/example"),
    ],
)
def test_branch_flow_guard_allows_supported_pr_paths(base: str, head: str) -> None:
    result = _run(base, head)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("head", ["feature/example", "fix/example", "opt/example", "codex/example"])
def test_branch_flow_guard_rejects_development_prs_to_main(head: str) -> None:
    result = _run("main", head)

    assert result.returncode != 0
    assert "main only accepts pull requests from test or pre" in result.stderr


def test_branch_flow_guard_rejects_missing_branch_names() -> None:
    result = _run("main")

    assert result.returncode != 0
    assert "base and head branch names are required" in result.stderr


def test_branch_flow_workflow_cannot_be_skipped_by_deploy_pin_commits() -> None:
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"pull_request_target"}
    trigger = workflow["on"]["pull_request_target"]
    assert trigger["branches"] == ["main"]
    assert trigger["types"] == ["opened", "synchronize", "reopened"]

    checkout = workflow["jobs"]["branch-flow"]["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
