"""Deterministic tracked-file inventory for repository cleanup audits.

The inventory classifies surfaces; it never decides that a path is dead. Git's
index is the corpus boundary so local worktrees, environments, build output,
and ignored secrets cannot leak into the report.
"""

from __future__ import annotations

import argparse
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Inventory:
    counts: dict[str, int]
    paths_by_category: dict[str, tuple[str, ...]]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def classify_path(path: str) -> str:
    """Classify one repo-relative path without inferring liveness."""

    normalized = _normalize(path)
    parts = normalized.split("/")

    if (
        normalized == ".env"
        or normalized.endswith("/.env")
        or normalized.startswith((".worktrees/", "worktrees/", ".venv/", ".venv-"))
        or any(
            part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in parts
        )
    ):
        return "ignored-local"

    if normalized.startswith(
        ("backend/alembic/versions/", "backend/alembic_tee/versions/")
    ):
        return "migration"

    if normalized.startswith("docs-site/openapi/"):
        return "generated"

    if normalized.startswith(("contracts/lib/", "vendor/")):
        return "vendor"

    if normalized.startswith("tests/") or "/tests/" in normalized:
        return "test"

    if normalized.startswith(
        (
            "docs/superpowers/plans/",
            "docs/superpowers/specs/",
            "docs/archive/",
        )
    ):
        return "historical-review"

    if normalized.startswith(("tools/", "scripts/", "ops/")):
        return "tool-script"

    if normalized.startswith(
        (
            "backend/",
            "deploy/",
            "contracts/src/",
            "contracts/script/",
        )
    ):
        return "production"

    if normalized.startswith(("docs/", "docs-site/content/")) or normalized.endswith(
        ".md"
    ):
        return "documentation"

    if normalized.startswith(".github/") or normalized in {
        ".dockerignore",
        ".gitignore",
    }:
        return "repository-config"

    return "other"


def tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Return the sorted Git-index corpus for ``repo_root``."""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return tuple(sorted(path for path in result.stdout.split("\0") if path))


def build_inventory(paths: Iterable[str]) -> Inventory:
    grouped: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        normalized = _normalize(path)
        grouped[classify_path(normalized)].append(normalized)

    paths_by_category = {
        category: tuple(sorted(category_paths))
        for category, category_paths in sorted(grouped.items())
    }
    counts = {
        category: len(category_paths)
        for category, category_paths in paths_by_category.items()
    }
    return Inventory(counts=counts, paths_by_category=paths_by_category)


def render_markdown(inventory: Inventory) -> str:
    lines = [
        "# Repository tracked-file inventory",
        "",
        "> Generated from `git ls-files`; classifications describe repository surface, not liveness.",
        "",
        f"Tracked files: **{inventory.total:,}**",
        "",
        "| Classification | Files |",
        "|---|---:|",
    ]
    lines.extend(
        f"| `{category}` | {count:,} |" for category, count in inventory.counts.items()
    )
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--format", choices=("markdown",), default="markdown")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    inventory = build_inventory(tracked_paths(args.repo_root.resolve()))
    print(render_markdown(inventory), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
