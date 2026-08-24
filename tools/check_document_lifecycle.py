"""Validate repository-owned Markdown lifecycle metadata.

Enforcement is incremental by default: CI passes ``--changed-vs`` and only new
or modified Markdown must be classified. ``--all`` is the eventual full-repo
ratchet and can also render the classified-document inventory.
"""

from __future__ import annotations

import argparse
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

LIFECYCLES = ("current", "decision", "historical", "generated")
HISTORICAL_REASONS = ("implemented", "superseded", "rejected", "point-in-time")
MARKDOWN_SUFFIXES = frozenset((".md", ".mdx"))


def parse_metadata(text: str) -> tuple[dict[str, object], str | None]:
    """Return YAML front matter and a user-facing parse error."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "missing YAML front matter"

    try:
        closing_index = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}, "unterminated YAML front matter"

    try:
        loaded = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except yaml.YAMLError as exc:
        return {}, f"invalid YAML front matter: {exc.problem or type(exc).__name__}"
    if not isinstance(loaded, dict):
        return {}, "YAML front matter must be a mapping"
    return {str(key): value for key, value in loaded.items()}, None


def _relative(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _resolve_repo_path(repo_root: Path, relative: str) -> Path | None:
    """Resolve a repository-relative path, rejecting absolute paths and escapes."""

    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    resolved = (repo_root / candidate).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return resolved


def _lifecycle_of(path: Path) -> str:
    if path.suffix.lower() not in MARKDOWN_SUFFIXES or not path.is_file():
        return "unclassified"
    metadata, error = parse_metadata(path.read_text(encoding="utf-8"))
    if error is not None:
        return "unclassified"
    lifecycle = str(metadata.get("document_lifecycle") or "")
    return lifecycle if lifecycle in LIFECYCLES else "unclassified"


def validate_document(path: Path, repo_root: Path) -> list[str]:
    """Validate one Markdown document without mutating it."""

    relative = _relative(path, repo_root)
    metadata, parse_error = parse_metadata(path.read_text(encoding="utf-8"))
    if parse_error is not None:
        return [f"{relative}: {parse_error}"]

    lifecycle = str(metadata.get("document_lifecycle") or "")
    if lifecycle not in LIFECYCLES:
        return [
            f"{relative}: document_lifecycle must be one of "
            f"{', '.join(LIFECYCLES)}; got {lifecycle!r}"
        ]

    errors: list[str] = []
    owner = str(metadata.get("canonical_owner") or "").strip()
    if not owner:
        errors.append(f"{relative}: canonical_owner is required")

    if lifecycle == "historical":
        reason = str(metadata.get("historical_reason") or "")
        if reason not in HISTORICAL_REASONS:
            errors.append(
                f"{relative}: historical documents require historical_reason="
                f"{', '.join(HISTORICAL_REASONS[:-1])}, or {HISTORICAL_REASONS[-1]}"
            )
        if owner == "self":
            errors.append(
                f"{relative}: historical documents cannot be their own canonical_owner"
            )
        if reason == "superseded":
            successor = str(metadata.get("superseded_by") or "").strip()
            if not successor:
                errors.append(
                    f"{relative}: historical_reason=superseded requires superseded_by"
                )
            else:
                successor_path = _resolve_repo_path(repo_root, successor)
                if successor_path is None:
                    errors.append(
                        f"{relative}: superseded_by escapes repository: {successor}"
                    )
                elif not successor_path.exists():
                    errors.append(
                        f"{relative}: superseded_by does not exist: {successor}"
                    )
                else:
                    successor_lifecycle = _lifecycle_of(successor_path)
                    if successor_lifecycle not in ("current", "decision"):
                        errors.append(
                            f"{relative}: superseded_by must be current or decision "
                            f"Markdown: {successor} ({successor_lifecycle})"
                        )

    if lifecycle == "generated" and not str(metadata.get("generator") or "").strip():
        errors.append(f"{relative}: generated documents require a generator command")

    if not owner or owner == "self":
        return errors

    owner_path = _resolve_repo_path(repo_root, owner)
    if owner_path is None:
        errors.append(f"{relative}: canonical_owner escapes repository: {owner}")
        return errors
    if not owner_path.exists():
        errors.append(f"{relative}: canonical_owner does not exist: {owner}")
        return errors

    if lifecycle == "current":
        owner_parts = Path(owner).parts
        if "archive" in owner_parts:
            errors.append(
                f"{relative}: current canonical_owner cannot point into an archive: {owner}"
            )
            return errors
        if owner_path.suffix.lower() not in MARKDOWN_SUFFIXES:
            errors.append(
                f"{relative}: current canonical_owner must be current or decision "
                f"Markdown: {owner}"
            )
            return errors
        owner_metadata, owner_error = parse_metadata(
            owner_path.read_text(encoding="utf-8")
        )
        owner_lifecycle = str(owner_metadata.get("document_lifecycle") or "")
        if owner_error is not None or owner_lifecycle not in LIFECYCLES:
            errors.append(
                f"{relative}: current canonical_owner has no valid lifecycle "
                f"metadata: {owner}"
            )
        elif owner_lifecycle == "historical":
            errors.append(f"{relative}: current canonical_owner is historical: {owner}")
        elif owner_lifecycle == "generated":
            errors.append(
                f"{relative}: current canonical_owner cannot be generated: {owner}"
            )

    return errors


def _git_paths(repo_root: Path, args: Sequence[str]) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return tuple(path for path in result.stdout.split("\0") if path)


def _is_managed_markdown(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return Path(
        normalized
    ).suffix.lower() in MARKDOWN_SUFFIXES and not normalized.startswith(
        ("contracts/lib/", "vendor/")
    )


def changed_markdown_paths(repo_root: Path, changed_vs: str) -> tuple[str, ...]:
    """Return changed and untracked repo-owned Markdown paths."""

    changed = set(
        _git_paths(
            repo_root,
            ("diff", "--name-only", "--diff-filter=ACMR", "-z", changed_vs, "--"),
        )
    )
    changed.update(
        _git_paths(repo_root, ("ls-files", "--others", "--exclude-standard", "-z"))
    )
    return tuple(sorted(path for path in changed if _is_managed_markdown(path)))


def all_markdown_paths(repo_root: Path) -> tuple[str, ...]:
    paths = set(_git_paths(repo_root, ("ls-files", "-z")))
    existing = (
        path
        for path in paths
        if (resolved := _resolve_repo_path(repo_root, path)) is not None
        and resolved.is_file()
    )
    return tuple(sorted(path for path in existing if _is_managed_markdown(path)))


def classified_paths(
    repo_root: Path,
    paths: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for relative in paths:
        metadata, error = parse_metadata(
            (repo_root / relative).read_text(encoding="utf-8")
        )
        if error is None:
            lifecycle = str(metadata.get("document_lifecycle") or "")
            if lifecycle in LIFECYCLES:
                grouped[lifecycle].append(relative)
    return {
        lifecycle: tuple(sorted(grouped.get(lifecycle, ()))) for lifecycle in LIFECYCLES
    }


def render_report(records: Mapping[str, Sequence[str]]) -> str:
    lines = [
        "---",
        "document_lifecycle: generated",
        "canonical_owner: tools/check_document_lifecycle.py",
        "generator: python3 tools/check_document_lifecycle.py --all --report",
        "---",
        "# Document lifecycle inventory",
        "",
        "> Generated from lifecycle front matter. Unclassified documents are not counted.",
        "",
        "| Lifecycle | Classified documents |",
        "|---|---:|",
    ]
    for lifecycle in LIFECYCLES:
        lines.append(f"| `{lifecycle}` | {len(records.get(lifecycle, ())):,} |")
    for lifecycle in LIFECYCLES:
        paths = sorted(records.get(lifecycle, ()))
        if not paths:
            continue
        lines.extend(("", f"## {lifecycle}", ""))
        lines.extend(f"- `{path}`" for path in paths)
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--changed-vs")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--report", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    paths = (
        all_markdown_paths(repo_root)
        if args.all
        else changed_markdown_paths(repo_root, args.changed_vs)
    )
    if args.report:
        print(render_report(classified_paths(repo_root, paths)), end="")
        return 0

    errors = [
        error
        for relative in paths
        for error in validate_document(repo_root / relative, repo_root)
    ]
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"document lifecycle: {len(paths)} Markdown file(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
