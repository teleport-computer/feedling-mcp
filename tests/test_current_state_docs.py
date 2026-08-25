"""Current-state documentation must agree with deployable runtime wiring."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _compose_environment(path: str, service: str) -> dict[str, object]:
    document = yaml.safe_load(_read(path))
    environment = document["services"][service]["environment"]
    assert isinstance(environment, dict)
    return environment


@pytest.mark.parametrize(
    "compose_path",
    (
        "deploy/docker-compose.phala.yaml",
        "deploy/docker-compose.phala.test.yaml",
        "deploy/docker-compose.phala.pre.yaml",
    ),
)
def test_current_state_matches_runtime_policy_and_default(compose_path: str) -> None:
    environment = _compose_environment(compose_path, "backend")
    current_state = _read("docs/CURRENT_STATE.md")

    assert environment["FEEDLING_HOSTED_RUNTIME_POLICY"] == "dual"
    assert environment["FEEDLING_RUNTIME_DEFAULT_DESIRED"] == "resident"
    assert "`FEEDLING_HOSTED_RUNTIME_POLICY=dual`" in current_state
    assert "`FEEDLING_RUNTIME_DEFAULT_DESIRED=resident`" in current_state


@pytest.mark.parametrize(
    "runner_compose_path",
    (
        "deploy/docker-compose.phala.prod.runner.yaml",
        "deploy/docker-compose.phala.runner.yaml",
        "deploy/docker-compose.phala.pre.runner.yaml",
    ),
)
def test_current_state_keeps_active_hosted_resident_wiring_visible(
    runner_compose_path: str,
) -> None:
    runner = yaml.safe_load(_read(runner_compose_path))["services"]["agent-runner"]
    current_state = _read("docs/CURRENT_STATE.md")

    assert runner["command"][-1] == "backend/agent_runtime/supervisor.py"
    assert "`backend/agent_runtime/`" in current_state
    assert "`tools/chat_resident_consumer.py`" in current_state
    assert "active hosted Resident" in current_state


def test_current_guides_link_to_the_current_state_entry_point() -> None:
    for path in (
        "README.md",
        "docs/PROJECT_OVERVIEW.md",
        "docs/testing/README.md",
        "docs/testing/RUNTIME_MAP.md",
        "docs/CHANGELOG.md",
    ):
        assert "CURRENT_STATE.md" in _read(path), path


def test_current_guides_do_not_repeat_superseded_v2_only_claims() -> None:
    stale_claims = (
        (
            "README.md",
            "hosted resident supervisors and per-user CLI processes are retired",
        ),
        ("docs/PROJECT_OVERVIEW.md", "当前 hosted backend manifest 固定为 `v2_only`"),
        ("docs/PROJECT_OVERVIEW.md", "Hosted Model API 只走 Runtime V2"),
        ("docs/PROJECT_OVERVIEW.md", "独立 runner CVM 中的 pooled `serve-worker`"),
        ("docs/PROJECT_OVERVIEW.md", "main CVM + Runtime V2 runner CVM"),
        ("docs/testing/README.md", "V1 托管已不再维护"),
        ("docs/testing/RUNTIME_MAP.md", "「V1 托管已不再维护」"),
    )

    for path, stale_claim in stale_claims:
        assert stale_claim not in _read(path), path


def test_agent_reading_order_uses_current_state_before_changelog() -> None:
    for path in ("AGENTS.md", "CLAUDE.md"):
        guide = _read(path)
        assert guide.index("docs/CURRENT_STATE.md") < guide.index(
            "docs/CHANGELOG.md"
        ), path


def test_changelog_is_history_not_current_runtime_authority() -> None:
    changelog_intro = _read("docs/CHANGELOG.md")[:900]

    assert "historical timeline" in changelog_intro
    assert (
        'source-of-truth for "where we are now" is this changelog'
        not in changelog_intro
    )
