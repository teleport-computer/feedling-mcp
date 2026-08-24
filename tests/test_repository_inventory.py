"""Repository-cleanup inventory contracts.

These tests keep the audit corpus honest: classification distinguishes protected
or non-production surfaces, and tracked-file discovery delegates to Git instead
of walking local worktrees, virtual environments, or secret files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from tools.repository_inventory import (  # noqa: E402
    build_inventory,
    classify_path,
    main,
    render_markdown,
    tracked_paths,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("backend/alembic/versions/0101_chat_change_events.py", "migration"),
        ("backend/alembic_tee/versions/0037_chat_poll_index.py", "migration"),
        ("docs/superpowers/plans/2026-08-24-example.md", "historical-review"),
        ("docs/superpowers/specs/example.md", "historical-review"),
        ("docs-site/openapi/public.json", "generated"),
        ("contracts/lib/forge-std/src/Vm.sol", "vendor"),
        ("backend/model_api_runtime/v2/worker.py", "production"),
        ("tests/test_v2_worker.py", "test"),
        ("tools/deploy_canary.py", "tool-script"),
        ("docs/testing/README.md", "documentation"),
        (".worktrees/old-branch/backend/app.py", "ignored-local"),
        (".venv/lib/python3.12/site-packages/httpx/__init__.py", "ignored-local"),
        (".env", "ignored-local"),
    ],
)
def test_classify_path_separates_runtime_history_and_protected_surfaces(
    path: str,
    expected: str,
) -> None:
    """Moving a precedence rule to the wrong branch must change this result."""

    assert classify_path(path) == expected


def test_build_inventory_counts_categories_without_calling_anything_dead() -> None:
    inventory = build_inventory(
        [
            "backend/app.py",
            "backend/alembic/versions/0001_baseline.py",
            "docs/superpowers/plans/old.md",
            "tools/probe.py",
        ]
    )

    assert inventory.counts == {
        "historical-review": 1,
        "migration": 1,
        "production": 1,
        "tool-script": 1,
    }
    assert "dead" not in inventory.counts
    assert inventory.paths_by_category["production"] == ("backend/app.py",)


def test_tracked_paths_uses_git_index_and_excludes_ignored_local_files(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".worktrees/\n.venv/\n.env\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "app.py").write_text("APP = True\n")
    (tmp_path / ".worktrees" / "old").mkdir(parents=True)
    (tmp_path / ".worktrees" / "old" / "stale.py").write_text("STALE = True\n")
    (tmp_path / ".env").write_text("SECRET=not-for-inventory\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "backend/app.py"],
        check=True,
    )

    assert tracked_paths(tmp_path) == (".gitignore", "backend/app.py")


def test_render_markdown_reports_counts_without_liveness_claims() -> None:
    inventory = build_inventory(["README.md", "backend/app.py", "backend/store.py"])

    assert render_markdown(inventory) == (
        "# Repository tracked-file inventory\n"
        "\n"
        "> Generated from `git ls-files`; classifications describe repository "
        "surface, not liveness.\n"
        "\n"
        "Tracked files: **3**\n"
        "\n"
        "| Classification | Files |\n"
        "|---|---:|\n"
        "| `documentation` | 1 |\n"
        "| `production` | 2 |\n"
    )


def test_main_renders_the_git_index_corpus(tmp_path: Path, capsys) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("# Fixture\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "app.py").write_text("APP = True\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "README.md", "backend/app.py"],
        check=True,
    )

    assert main(["--repo-root", str(tmp_path), "--format", "markdown"]) == 0
    assert capsys.readouterr().out == (
        "# Repository tracked-file inventory\n"
        "\n"
        "> Generated from `git ls-files`; classifications describe repository "
        "surface, not liveness.\n"
        "\n"
        "Tracked files: **2**\n"
        "\n"
        "| Classification | Files |\n"
        "|---|---:|\n"
        "| `documentation` | 1 |\n"
        "| `production` | 1 |\n"
    )
