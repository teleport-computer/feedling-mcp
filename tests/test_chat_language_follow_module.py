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
WORKER = BACKEND / "model_api_runtime" / "v2" / "worker.py"
OLD_MODULE = "model_api_runtime.v2.language_follow"
NEW_MODULE = "chat.language_follow"
EXPECTED_CONSUMERS = {
    "backend/model_api_runtime/v2/tool_loop.py",
    "backend/model_api_runtime/v2/worker.py",
    "tests/test_v2_context.py",
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


def test_public_classifier_literals_survived_the_move() -> None:
    """Proves classifier literals are unchanged; cannot prove every source byte."""
    assert language_follow.MIN_LETTER_COUNT == 10
    assert language_follow.DOMINANT_SHARE == 0.60
    assert language_follow.MIXED_SCRIPT_SHELL_MIN_COUNT == 5
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


def test_visible_language_corrector_stays_removed_but_observation_survives() -> None:
    """Adding either retired symbol back must fail this independent AST guard."""
    module_tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    worker_tree = ast.parse(WORKER.read_text(encoding="utf-8"))

    module_names = {
        node.id for node in ast.walk(module_tree) if isinstance(node, ast.Name)
    }
    worker_names = {
        node.id for node in ast.walk(worker_tree) if isinstance(node, ast.Name)
    }
    worker_functions = {
        node.name
        for node in ast.walk(worker_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "CORRECTION_INSTRUCTION" not in module_names
    assert "visible_language_mismatch" not in worker_names
    assert {
        "_reply_language_follow_observation",
        "_emit_reply_language_follow_trace",
    } <= worker_functions
    assert "_SELF_THINKING_ABSENT_CORRECTION_INSTRUCTION" in worker_names


def test_thinking_language_corrector_stays_removed() -> None:
    """Adding the retired thinking-language gate back must fail this guard."""
    worker_source = WORKER.read_text(encoding="utf-8")
    worker_tree = ast.parse(worker_source)
    worker_names = {
        node.id for node in ast.walk(worker_tree) if isinstance(node, ast.Name)
    }
    worker_functions = {
        node.name
        for node in ast.walk(worker_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "_self_thinking_language_mismatch" not in worker_functions
    assert "thinking_language_correction_pending" not in worker_names
    assert "重写时保留 <think>…</think> 结构" not in worker_source


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
