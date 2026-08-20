"""Repository guard for the plaintext/enclave trust boundary.

Direct decrypt transports are intentionally rare. New application code must go
through ``core.envelope`` (or add an explicit, reviewed shape gate at an approved
compatibility boundary) so a plaintext row cannot accidentally be posted.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

APPROVED_DIRECT_DECRYPT_HELPER = {
    "backend/capabilities/identity.py",
    "backend/content/content_core.py",
    "backend/core/envelope.py",
}

APPROVED_DIRECT_DECRYPT_HTTP = {
    "backend/agent_runtime/supervisor.py",
    "backend/core/enclave.py",
    "backend/genesis/worker.py",
    "tools/chat_resident_consumer.py",
}


def _python_files():
    yield from (ROOT / "backend").rglob("*.py")
    yield ROOT / "tools" / "chat_resident_consumer.py"


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


class _RuntimeStringVisitor(ast.NodeVisitor):
    """Visit executable bodies while ignoring docstrings and decorators."""

    values: list[str]

    def __init__(self) -> None:
        self.values = []

    def visit_Expr(self, node: ast.Expr) -> None:  # noqa: N802 - ast API
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast API
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802 - ast API
        if isinstance(node.value, str):
            self.values.append(node.value)


def _runtime_strings(tree: ast.AST) -> list[str]:
    visitor = _RuntimeStringVisitor()
    visitor.visit(tree)
    return visitor.values


def test_direct_enclave_decrypt_helper_has_a_reviewed_allowlist():
    found: set[str] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "_decrypt_envelope_via_enclave":
                found.add(_relative(path))

    assert found == APPROVED_DIRECT_DECRYPT_HELPER


def test_direct_enclave_decrypt_http_has_a_reviewed_allowlist():
    found: set[str] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any("/v1/envelope/decrypt" in value for value in _runtime_strings(tree)):
            found.add(_relative(path))

    assert found == APPROVED_DIRECT_DECRYPT_HTTP
