from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents" / "skills" / "io-e2e" / "SKILL.md"
OPENAI_METADATA = SKILL.parent / "agents" / "openai.yaml"


def test_io_e2e_skill_is_discoverable_and_complete() -> None:
    text = SKILL.read_text(encoding="utf-8")
    metadata = OPENAI_METADATA.read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert text.startswith("---\nname: io-e2e\ndescription:")
    assert "TODO" not in text
    assert ".agents/skills/io-e2e/SKILL.md" in agents
    assert "!.agents/skills/io-e2e/**" in gitignore
    assert 'display_name: "IO E2E"' in metadata
    assert "$io-e2e" in metadata


def test_io_e2e_skill_uses_only_the_universal_control_cli() -> None:
    text = SKILL.read_text(encoding="utf-8")

    for command in ("plan", "run", "status", "watch", "results", "open", "cancel"):
        assert f"python3 -m tools.io_e2e {command}" in text

    assert "python3 -m tools.io_e2e run --ref test --wait --interval 10" in text
    assert "Never dispatch the qualification workflow directly" in text
    assert "gh workflow run" in text
    assert "Never read, request, print, copy, decode, or pass Codex OAuth" in text
    assert "TRUSTED_WORKFLOW_UNAVAILABLE" in text
    assert "never work around it by dispatching from a" in text


def test_io_e2e_skill_does_not_overclaim_branch_preview_or_evidence() -> None:
    text = SKILL.read_text(encoding="utf-8")
    prose = " ".join(text.split())

    assert "branch preview is not implemented yet" in prose
    assert "deployed_test` tests the already deployed test service" in prose
    assert "BLOCKED_EVIDENCE" in text
    assert "Missing evidence and observability gaps as unknowns, not diagnoses" in prose
    assert "deterministic cleanup outcomes separately" in prose
