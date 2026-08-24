"""Incremental document-lifecycle contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from tools.check_document_lifecycle import (  # noqa: E402
    changed_markdown_paths,
    parse_metadata,
    render_report,
    validate_document,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _document(
    *,
    lifecycle: str,
    canonical_owner: str,
    body: str = "# Example\n",
    historical_reason: str | None = None,
    generator: str | None = None,
) -> str:
    lines = [
        "---",
        f"document_lifecycle: {lifecycle}",
        f"canonical_owner: {canonical_owner}",
    ]
    if historical_reason is not None:
        lines.append(f"historical_reason: {historical_reason}")
    if generator is not None:
        lines.append(f"generator: {generator}")
    return "\n".join([*lines, "---", body])


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def test_parse_metadata_requires_front_matter() -> None:
    metadata, error = parse_metadata("# No lifecycle\n")

    assert metadata == {}
    assert error == "missing YAML front matter"


def test_validate_document_rejects_missing_and_invalid_lifecycle(
    tmp_path: Path,
) -> None:
    missing = _write(tmp_path / "missing.md", "# Missing\n")
    invalid = _write(
        tmp_path / "invalid.md",
        _document(lifecycle="stale", canonical_owner="self"),
    )

    assert validate_document(missing, tmp_path) == [
        "missing.md: missing YAML front matter"
    ]
    assert validate_document(invalid, tmp_path) == [
        "invalid.md: document_lifecycle must be one of current, decision, historical, generated; got 'stale'"
    ]


def test_current_document_cannot_delegate_authority_to_archive(tmp_path: Path) -> None:
    current = _write(
        tmp_path / "docs/current.md",
        _document(
            lifecycle="current",
            canonical_owner="docs/archive/old.md",
        ),
    )
    _write(
        tmp_path / "docs/archive/old.md",
        _document(
            lifecycle="historical",
            canonical_owner="docs/current.md",
            historical_reason="superseded",
        ),
    )

    assert validate_document(current, tmp_path) == [
        "docs/current.md: current canonical_owner cannot point into an archive: docs/archive/old.md"
    ]


def test_current_document_cannot_delegate_authority_to_historical_doc(
    tmp_path: Path,
) -> None:
    owner = _write(
        tmp_path / "docs/old.md",
        _document(
            lifecycle="historical",
            canonical_owner="docs/current.md",
            historical_reason="implemented",
        ),
    )
    assert owner.exists()
    current = _write(
        tmp_path / "docs/current.md",
        _document(lifecycle="current", canonical_owner="docs/old.md"),
    )

    assert validate_document(current, tmp_path) == [
        "docs/current.md: current canonical_owner is historical: docs/old.md"
    ]


def test_current_document_owner_must_itself_be_classified(tmp_path: Path) -> None:
    _write(tmp_path / "docs/owner.md", "# Unclassified owner\n")
    current = _write(
        tmp_path / "docs/current.md",
        _document(lifecycle="current", canonical_owner="docs/owner.md"),
    )

    assert validate_document(current, tmp_path) == [
        "docs/current.md: current canonical_owner has no valid lifecycle metadata: docs/owner.md"
    ]


def test_lifecycle_specific_metadata_is_required(tmp_path: Path) -> None:
    historical = _write(
        tmp_path / "historical.md",
        _document(lifecycle="historical", canonical_owner="docs/current.md"),
    )
    generated = _write(
        tmp_path / "generated.md",
        _document(lifecycle="generated", canonical_owner="tools/generate.py"),
    )

    assert validate_document(historical, tmp_path) == [
        "historical.md: historical documents require historical_reason=implemented, superseded, rejected, or point-in-time",
        "historical.md: canonical_owner does not exist: docs/current.md",
    ]
    assert validate_document(generated, tmp_path) == [
        "generated.md: generated documents require a generator command",
        "generated.md: canonical_owner does not exist: tools/generate.py",
    ]


def test_superseded_document_requires_a_live_successor(tmp_path: Path) -> None:
    current = _write(
        tmp_path / "docs/current.md",
        _document(lifecycle="current", canonical_owner="self"),
    )
    assert current.exists()
    superseded = _write(
        tmp_path / "docs/old.md",
        _document(
            lifecycle="historical",
            canonical_owner="docs/current.md",
            historical_reason="superseded",
        ),
    )

    assert validate_document(superseded, tmp_path) == [
        "docs/old.md: historical_reason=superseded requires superseded_by"
    ]


def test_changed_markdown_paths_uses_git_diff_and_untracked_files(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Tests")
    _write(tmp_path / "old.md", "# Existing but not classified yet\n")
    _write(tmp_path / "unchanged.md", "# Also existing\n")
    _git(tmp_path, "add", "old.md", "unchanged.md")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    _write(tmp_path / "old.md", "# Changed\n")
    _write(tmp_path / "new.md", "# New\n")
    _write(tmp_path / "notes.txt", "not markdown\n")

    assert changed_markdown_paths(tmp_path, base) == ("new.md", "old.md")


def test_render_report_is_deterministic_and_marks_itself_generated() -> None:
    rendered = render_report(
        {
            "current": ("docs/CURRENT_STATE.md", "README.md"),
            "historical": ("docs/old.md",),
        }
    )

    assert rendered.startswith(
        "---\n"
        "document_lifecycle: generated\n"
        "canonical_owner: tools/check_document_lifecycle.py\n"
        "generator: python3 tools/check_document_lifecycle.py --all --report\n"
        "---\n"
    )
    assert "| `current` | 2 |" in rendered
    assert "| `historical` | 1 |" in rendered
    assert rendered.index("README.md") < rendered.index("docs/CURRENT_STATE.md")
