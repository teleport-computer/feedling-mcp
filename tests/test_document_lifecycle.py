"""Incremental document-lifecycle contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from tools.check_document_lifecycle import (  # noqa: E402
    all_markdown_paths,
    changed_markdown_paths,
    classified_paths,
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


def test_current_document_owner_must_be_markdown_not_source(tmp_path: Path) -> None:
    _write(tmp_path / "tools/owner.py", "OWNER = True\n")
    current = _write(
        tmp_path / "docs/current.md",
        _document(lifecycle="current", canonical_owner="tools/owner.py"),
    )

    assert validate_document(current, tmp_path) == [
        "docs/current.md: current canonical_owner must be current or decision Markdown: tools/owner.py"
    ]


def test_canonical_owner_cannot_escape_repository(tmp_path: Path) -> None:
    outside = _write(tmp_path.parent / "outside-owner.md", "# Outside\n")
    assert outside.exists()
    current = _write(
        tmp_path / "docs/current.md",
        _document(lifecycle="current", canonical_owner="../outside-owner.md"),
    )

    assert validate_document(current, tmp_path) == [
        "docs/current.md: canonical_owner escapes repository: ../outside-owner.md"
    ]


def test_mdx_can_be_a_current_canonical_owner(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs-site/content/docs/architecture.mdx",
        _document(lifecycle="decision", canonical_owner="self"),
    )
    current = _write(
        tmp_path / "docs/current.md",
        _document(
            lifecycle="current",
            canonical_owner="docs-site/content/docs/architecture.mdx",
        ),
    )

    assert validate_document(current, tmp_path) == []


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


@pytest.mark.parametrize("successor_lifecycle", ("historical", "generated"))
def test_superseded_successor_must_be_current_or_decision(
    tmp_path: Path,
    successor_lifecycle: str,
) -> None:
    _write(
        tmp_path / "docs/current.md",
        _document(lifecycle="current", canonical_owner="self"),
    )
    successor_metadata = _document(
        lifecycle=successor_lifecycle,
        canonical_owner=(
            "docs/current.md"
            if successor_lifecycle == "historical"
            else "tools/generate.py"
        ),
        historical_reason=(
            "implemented" if successor_lifecycle == "historical" else None
        ),
        generator=(
            "python3 tools/generate.py" if successor_lifecycle == "generated" else None
        ),
    )
    _write(tmp_path / "docs/successor.md", successor_metadata)
    if successor_lifecycle == "generated":
        _write(tmp_path / "tools/generate.py", "print('generated')\n")
    superseded = _write(
        tmp_path / "docs/old.md",
        "---\n"
        "document_lifecycle: historical\n"
        "canonical_owner: docs/current.md\n"
        "historical_reason: superseded\n"
        "superseded_by: docs/successor.md\n"
        "---\n"
        "# Old\n",
    )

    assert validate_document(superseded, tmp_path) == [
        "docs/old.md: superseded_by must be current or decision Markdown: "
        f"docs/successor.md ({successor_lifecycle})"
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
    _write(tmp_path / "docs-site/content/docs/public.mdx", "# Public\n")
    _write(tmp_path / "notes.txt", "not markdown\n")

    assert changed_markdown_paths(tmp_path, base) == (
        "docs-site/content/docs/public.mdx",
        "new.md",
        "old.md",
    )


def _force_rewritten_checkout(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    remote.mkdir()
    source.mkdir()
    _git(remote, "init", "--bare", "-q")
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "tests@example.invalid")
    _git(source, "config", "user.name", "Tests")
    _write(source / "old.md", "# Existing but not classified yet\n")
    _git(source, "add", "old.md")
    _git(source, "commit", "-q", "-m", "old history")
    old_head = _git(source, "rev-parse", "HEAD")
    _git(source, "remote", "add", "origin", remote.as_uri())
    _git(source, "push", "-q", "origin", "HEAD:refs/heads/test")

    _git(source, "switch", "--orphan", "rewritten")
    _write(
        source / "new.md",
        _document(lifecycle="current", canonical_owner="self"),
    )
    _git(source, "add", "new.md")
    _git(source, "commit", "-q", "-m", "rewritten history")
    _git(source, "push", "-q", "--force", "origin", "HEAD:refs/heads/test")

    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--branch",
            "test",
            "--depth=1",
            remote.as_uri(),
            str(checkout),
        ],
        check=True,
    )
    missing = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{old_head}^{{commit}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert missing.returncode != 0

    return checkout, old_head


def _run_changed_lifecycle_cli(
    checkout: Path,
    changed_vs: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "check_document_lifecycle.py"),
            "--repo-root",
            str(checkout),
            "--changed-vs",
            changed_vs,
            "--fetch-missing-base-from",
            "origin",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_cli_fetches_missing_full_sha_before_lifecycle_diff(tmp_path: Path) -> None:
    """A force-rewritten push can leave event.before outside the checkout."""

    checkout, old_head = _force_rewritten_checkout(tmp_path)

    result = _run_changed_lifecycle_cli(checkout, old_head)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "document lifecycle: 1 Markdown file(s) valid\n"
    assert _git(checkout, "cat-file", "-t", old_head) == "commit"


def test_cli_fails_closed_when_full_sha_is_unavailable(tmp_path: Path) -> None:
    checkout, _old_head = _force_rewritten_checkout(tmp_path)
    unavailable_head = "f" * 40

    result = _run_changed_lifecycle_cli(checkout, unavailable_head)

    assert result.returncode != 0
    assert "document lifecycle:" not in result.stdout
    assert subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "cat-file",
            "-e",
            f"{unavailable_head}^{{commit}}",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0


def test_all_report_uses_only_the_tracked_repository_corpus(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Tests")
    _write(
        tmp_path / "tracked.md",
        _document(lifecycle="current", canonical_owner="self"),
    )
    _git(tmp_path, "add", "tracked.md")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _write(
        tmp_path / "local-notes.md",
        _document(lifecycle="current", canonical_owner="self"),
    )

    paths = all_markdown_paths(tmp_path)
    report = render_report(classified_paths(tmp_path, paths))

    assert paths == ("tracked.md",)
    assert "tracked.md" in report
    assert "local-notes.md" not in report


def test_all_markdown_paths_ignores_tracked_files_missing_from_checkout(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Tests")
    removed = _write(
        tmp_path / "removed.md",
        _document(lifecycle="historical", canonical_owner="self"),
    )
    _git(tmp_path, "add", "removed.md")
    _git(tmp_path, "commit", "-q", "-m", "base")
    removed.unlink()

    assert all_markdown_paths(tmp_path) == ()


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
