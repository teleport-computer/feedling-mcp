"""Structural guards for the one-loop-for-every-model V2 runtime.

The retired JSON planner, staged agent loop, invalidation/replan helper, and
standalone responder are deleted rather than kept as compatibility surfaces.
Chat and wake both execute the provider-native ``tool_loop.run_tool_loop`` and
provider identity is not part of the turn API.
"""
from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).parent.parent
_V2 = _ROOT / "backend" / "model_api_runtime" / "v2"
_WORKER = _V2 / "worker.py"
_LOADTEST_COMPARE = _ROOT / "scripts" / "loadtest" / "compare_tokens.py"
_RETIRED_MODULES = ("planner.py", "agent_loop.py", "invalidation.py", "responder.py")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _async_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _called_names(node: ast.AST) -> set[str]:
    return {
        ast.unparse(call.func)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    }


def test_retired_staged_pipeline_modules_are_deleted():
    remaining = [name for name in _RETIRED_MODULES if (_V2 / name).exists()]
    assert remaining == [], f"retired V2 compatibility modules remain: {remaining}"


def test_worker_turn_api_has_no_provider_identity_tier_parameter():
    tree = _tree(_WORKER)
    process_job = _async_function(tree, "process_job")
    assert "is_official" not in {
        arg.arg for arg in (*process_job.args.args, *process_job.args.kwonlyargs)
    }

    turn_deps = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TurnDeps"
    )
    fields = {
        node.target.id
        for node in turn_deps.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "is_official" not in fields


def test_chat_and_wake_use_the_same_native_tool_loop():
    tree = _tree(_WORKER)
    for function_name in ("process_job", "_run_wake"):
        function = _async_function(tree, function_name)
        calls = _called_names(function)
        assert "v2_tool_loop.run_tool_loop" in calls, function_name
        assert all("is_official" not in call for call in calls), function_name


def test_worker_does_not_import_retired_modules():
    retired_stems = {Path(name).stem for name in _RETIRED_MODULES}
    imported: set[str] = set()
    for node in ast.walk(_tree(_WORKER)):
        if isinstance(node, ast.ImportFrom) and node.module == "model_api_runtime.v2":
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
    assert imported.isdisjoint(retired_stems), imported & retired_stems


def test_token_rollback_gate_uses_the_production_unified_loop():
    code = _LOADTEST_COMPARE.read_text()
    assert "v2_tool_loop.run_tool_loop(" in code
    for retired in ("planner", "agent_loop", "invalidation", "responder"):
        assert f"v2_{retired}." not in code
        assert f"import {retired}" not in code
