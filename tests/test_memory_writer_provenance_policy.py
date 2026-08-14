"""Static parity guard for in-repo memory provenance writers.

The action executor owns the closed enums, but producer literals live in several
modules.  This test makes a newly invented producer value fail in CI instead of
silently rejecting every action at runtime (the Genesis import regression).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"
WRITER_PATHS = [
    *BACKEND.rglob("*.py"),
    ROOT / "tools" / "chat_resident_consumer.py",
]
sys.path.insert(0, str(BACKEND))

from memory_garden.types import (  # noqa: E402
    MEMORY_CAPTURE_MODE_VALUES,
    MEMORY_SOURCE_VALUES,
)


def _dict_items(node: ast.Dict):
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            yield key.value, value


def _assignments(scope: ast.AST) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for node in ast.walk(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not scope:
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            values[node.target.id] = node.value
    return values


def _scope_index(tree: ast.Module) -> dict[int, ast.AST]:
    scopes: dict[int, ast.AST] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[ast.AST] = [tree]

        def generic_visit(self, node):
            scopes[id(node)] = self.stack[-1]
            super().generic_visit(node)

        def _function(self, node):
            scopes[id(node)] = self.stack[-1]
            self.stack.append(node)
            super(Visitor, self).generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

    Visitor().visit(tree)
    return scopes


def _resolved_strings(
    node: ast.AST,
    local_values: dict[str, ast.AST],
    module_values: dict[str, ast.AST],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id not in seen:
        value = local_values.get(node.id) or module_values.get(node.id)
        if value is not None:
            return _resolved_strings(value, local_values, module_values, seen | {node.id})
        return set()
    if isinstance(node, (ast.BoolOp, ast.IfExp)):
        children = node.values if isinstance(node, ast.BoolOp) else (node.body, node.orelse)
        return set().union(*(
            _resolved_strings(child, local_values, module_values, seen)
            for child in children
        ))
    if isinstance(node, ast.Call):
        # Writer sanitizers preserve the first argument.  Do not descend into
        # ``raw.get("source")``: that string is a lookup key, not provenance.
        if isinstance(node.func, ast.Name) and node.func.id in {"clean_text", "_text"} and node.args:
            return _resolved_strings(node.args[0], local_values, module_values, seen)
        return set()
    return set()


def _memory_action_kind(node: ast.Dict) -> str:
    for key, value in _dict_items(node):
        if key == "type" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value if value.value.startswith("memory.") else ""
    return ""


def test_repo_memory_writer_provenance_matches_closed_policy():
    capture_modes: set[str] = set()
    memory_sources: set[str] = set()
    unresolved_capture_modes: list[str] = []

    for path in sorted(WRITER_PATHS):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        module_values = _assignments(tree)
        scope_index = _scope_index(tree)
        assignment_cache: dict[int, dict[str, ast.AST]] = {}

        def values_for(node: ast.AST) -> dict[str, ast.AST]:
            scope = scope_index[id(node)]
            if id(scope) not in assignment_cache:
                assignment_cache[id(scope)] = _assignments(scope)
            return assignment_cache[id(scope)]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            local_values = values_for(node)
            for key, value in _dict_items(node):
                if key == "capture_mode" and relative != "backend/memory/actions.py":
                    resolved = _resolved_strings(value, local_values, module_values)
                    capture_modes.update(resolved)
                    if not resolved:
                        unresolved_capture_modes.append(f"{relative}:{node.lineno}")

            if not _memory_action_kind(node) or relative == "backend/memory/actions.py":
                continue
            action_nodes: list[ast.AST] = list(ast.walk(node))
            # Some writers build ``memory = {...}`` first and reference that
            # local from the surrounding action dict (Genesis does this).
            for key, value in _dict_items(node):
                if key == "memory" and isinstance(value, ast.Name):
                    referenced = local_values.get(value.id)
                    if referenced is not None:
                        action_nodes.extend(ast.walk(referenced))
            for child in action_nodes:
                if not isinstance(child, ast.Dict):
                    continue
                for key, value in _dict_items(child):
                    if key == "source":
                        memory_sources.update(
                            _resolved_strings(value, local_values, module_values)
                        )

        # Runtime V2 seals the source into an envelope before building the
        # surrounding memory action, so audit those source keyword arguments too.
        envelope_builders = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"_memory_envelope_from_card", "_capture_build_envelope"}
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not isinstance(call.func, ast.Name) or call.func.id not in envelope_builders:
                continue
            local_values = values_for(call)
            source_kw = next((kw.value for kw in call.keywords if kw.arg == "source"), None)
            if source_kw is None:
                definition = envelope_builders[call.func.id]
                source_kw = next(
                    (
                        default
                        for arg, default in zip(
                            definition.args.kwonlyargs,
                            definition.args.kw_defaults,
                        )
                        if arg.arg == "source"
                    ),
                    None,
                )
            assert source_kw is not None, f"{relative}:{call.lineno}: envelope source missing"
            memory_sources.update(_resolved_strings(source_kw, local_values, module_values))

        # ``_to_actions`` accepts capture_mode as a parameter; every in-repo call
        # must pin it to a member of the same closed enum.
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not (isinstance(call.func, ast.Name) and call.func.id == "_to_actions"):
                continue
            capture_kw = next((kw.value for kw in call.keywords if kw.arg == "capture_mode"), None)
            assert capture_kw is not None, f"{relative}:{call.lineno}: capture_mode missing"
            resolved = _resolved_strings(capture_kw, values_for(call), module_values)
            assert resolved, f"{relative}:{call.lineno}: dynamic capture_mode is not auditable"
            capture_modes.update(resolved)

    # The only dynamic producer is extraction._to_actions; its call sites were
    # checked just above. Any second unresolved producer needs an explicit audit.
    assert len(unresolved_capture_modes) == 1
    assert unresolved_capture_modes[0].startswith(
        "backend/model_api_runtime/v2/extraction.py:"
    )
    assert capture_modes <= MEMORY_CAPTURE_MODE_VALUES
    assert memory_sources <= MEMORY_SOURCE_VALUES
    # Inventory assertion prevents the scanner itself from quietly missing the
    # currently deployed producers.
    assert capture_modes == {
        "agent_tool",
        "genesis_import",
        "genesis_resident_distill",
        "memory_capture",
        "memory_dream",
        "repair",
        "state",
    }
    assert memory_sources == {
        "genesis_import",
        "genesis_resident_distill",
        "hosted_runtime_state",
        "memory_capture",
        "memory_dream",
        "memory_migrate",
        "model_api_correction",
        "model_api_repair",
    }
