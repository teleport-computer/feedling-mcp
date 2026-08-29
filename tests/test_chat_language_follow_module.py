"""Static and isolated-import guards for the shared language-follow leaf."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from chat import language_follow  # noqa: E402


MODULE = BACKEND / "chat" / "language_follow.py"
OLD_MODULE = "model_api_runtime.v2.language_follow"
NEW_MODULE = "chat.language_follow"
EXPECTED_CONSUMERS = {
    "backend/model_api_runtime/v2/tool_loop.py",
    "backend/model_api_runtime/v2/worker.py",
    "tests/test_v2_context.py",
    "tests/test_v2_worker_tool_loop.py",
    "tests/test_v2_worker_unit_telemetry.py",
}


def _imports(path: Path, import_root: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    module_parts = list(path.relative_to(import_root).with_suffix("").parts)
    package_parts = module_parts[:-1]
    if module_parts[-1] == "__init__":
        package_parts = module_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                keep = len(package_parts) - (node.level - 1)
                relative_base = package_parts[:max(keep, 0)]
                if node.module:
                    relative_base.extend(node.module.split("."))
                base = ".".join(relative_base)
            if base:
                imported.add(base)
                imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def test_shared_module_is_a_stdlib_leaf_with_closed_consumers() -> None:
    """Proves direct absolute/relative imports and consumers, not dynamic ones."""
    module_imports = _imports(MODULE, BACKEND)
    assert {name.split(".", 1)[0] for name in module_imports} == {
        "__future__",
        "typing",
        "unicodedata",
    }

    consumers: set[str] = set()
    stale_consumers: set[str] = set()
    this_test = Path(__file__).resolve()
    for parent, import_root in (
        (BACKEND, BACKEND),
        (ROOT / "tools", ROOT),
        (ROOT / "tests", ROOT),
    ):
        for path in parent.rglob("*.py"):
            if path.resolve() == this_test:
                continue
            imports = _imports(path, import_root)
            relative = path.relative_to(ROOT).as_posix()
            if NEW_MODULE in imports:
                consumers.add(relative)
            if any(
                name == OLD_MODULE or name.startswith(f"{OLD_MODULE}.")
                for name in imports
            ):
                stale_consumers.add(relative)

    assert consumers == EXPECTED_CONSUMERS
    assert stale_consumers == set()


def test_public_literals_survived_the_move() -> None:
    """Proves four public literals are unchanged; cannot prove every source byte."""
    assert language_follow.MIN_LETTER_COUNT == 10
    assert language_follow.DOMINANT_SHARE == 0.60
    assert language_follow.WRITING_SYSTEMS == frozenset({
        "han",
        "latin",
        "kana",
        "hangul",
        "cyrillic",
        "other",
        "mixed",
        "indeterminate",
    })
    assert language_follow.CORRECTION_INSTRUCTION == (
        "你刚才这条回复,语言和这个人正在说的语言对不上。除非这个人要求过你用别的语言,"
        "否则用这个人的语言把同一条回复重说一遍:内容、语气、分寸都不变,只换语言。"
        "要是这个人确实要求过现在这种语言,就原样重复原回复。"
    )


def test_shared_module_imports_in_an_isolated_interpreter() -> None:
    """Proves a clean isolated import works; cannot prove consumer dependency closure."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import pathlib, sys; sys.path.insert(0, sys.argv[1]); "
                "import chat.language_follow as module; print(module.__file__)"
            ),
            str(BACKEND),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert Path(result.stdout.strip()).resolve() == MODULE.resolve()
