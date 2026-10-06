"""Derive the reviewed production ``get_store_per_load_mode`` call inventory.

The checked-in snapshot is a multiset keyed by path and review reason.  It is
intentionally independent of source coordinates and function names: formatting,
comment-only churn, and file-local refactors do not change it, while every added
or removed call changes a count or adds/removes an entry.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


SCHEMA_VERSION = 1
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = Path("tests/fixtures/store_per_load_mode_sites.json")


@dataclass(frozen=True, order=True)
class ReviewedInventoryEntry:
    path: str
    reason: str
    count: int


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _review_reason(call: ast.Call) -> str:
    for keyword in call.keywords:
        if (
            keyword.arg == "reason"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value.strip()
    return ""


class _ReviewedVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if _call_name(node) == "get_store_per_load_mode":
            self.reasons.append(_review_reason(node))
        self.generic_visit(node)


def derive_reviewed_sites(
    repo_root: Path,
) -> tuple[ReviewedInventoryEntry, ...]:
    backend_root = repo_root / "backend"
    counts: Counter[tuple[str, str]] = Counter()
    for file_path in sorted(backend_root.rglob("*.py")):
        relative = file_path.relative_to(repo_root).as_posix()
        if relative == "backend/core/store.py":
            continue
        tree = ast.parse(file_path.read_text(), filename=relative)
        visitor = _ReviewedVisitor()
        visitor.visit(tree)
        counts.update((relative, reason) for reason in visitor.reasons)
    return tuple(
        ReviewedInventoryEntry(path=path, reason=reason, count=count)
        for (path, reason), count in sorted(counts.items())
    )


def inventory_document(repo_root: Path) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sites": [asdict(site) for site in derive_reviewed_sites(repo_root)],
    }


def render_inventory(repo_root: Path) -> str:
    return json.dumps(
        inventory_document(repo_root), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--write",
        action="store_true",
        help="replace the tracked snapshot instead of printing the inventory",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    rendered = render_inventory(repo_root)
    if not args.write:
        print(rendered, end="")
        return 0

    output = args.output
    if not output.is_absolute():
        output = repo_root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
