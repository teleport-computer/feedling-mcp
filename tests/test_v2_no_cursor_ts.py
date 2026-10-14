"""Guard the stable-seq reply cursor against executable timestamp-cursor use.

Comments, docstrings and standalone string prose are not executable cursor
references. Used strings (including dictionary keys and SQL), identifiers and
attributes are. worker.py retains its documented legacy rollback exemption.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_V2_DIR = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"
_EXEMPT = {"worker.py"}


def _modules():
    for p in sorted(_V2_DIR.glob("*.py")):
        if p.name not in _EXEMPT:
            yield p


def _executable_cursor_ts_references(src: str) -> list[int]:
    """Return locations of forbidden executable cursor spellings.

    Ignore only standalone literal-string expressions: these include module,
    class and function docstrings and inert historical prose. A string assigned
    to a cursor key or passed to an accessor/query remains part of the guard.
    """
    tree = ast.parse(src)
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    offenders = []
    for node in ast.walk(tree):
        spellings = []
        if isinstance(node, ast.Name):
            spellings.append(node.id)
        elif isinstance(node, ast.Attribute):
            spellings.append(node.attr)
        elif isinstance(node, ast.arg):
            spellings.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            spellings.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            spellings.append(node.name)
        elif isinstance(node, ast.alias):
            spellings.extend([node.name, node.asname or ""])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in prose:
                spellings.append(node.value)
        if any("cursor_ts" in spelling for spelling in spellings):
            offenders.append(node.lineno)
    return sorted(set(offenders))


def test_no_cursor_ts_outside_worker_rollback_compatibility():
    offenders = {
        path.name: lines
        for path in _modules()
        if (lines := _executable_cursor_ts_references(path.read_text()))
    }
    assert not offenders, (
        "cursor_ts leaked into a v2 module outside the documented rollback "
        f"safe-point exemption {_EXEMPT}; the stable reply cursor must stay "
        f"seq-based (spec A1). Offenders: {offenders}"
    )


def test_new_cursor_module_is_seq_only():
    src = (_V2_DIR / "cursor.py").read_text()
    assert not _executable_cursor_ts_references(src)
    tree = ast.parse(src)
    assert any(isinstance(node, ast.Name) and node.id == "seq" for node in ast.walk(tree))


@pytest.mark.parametrize("source", [
    '# Historical cursor_ts comment\nseq = 1',
    '"""Historical cursor_ts module docstring"""\nseq = 1',
    'def load():\n    """Old cursor_ts documentation"""\n    return 1',
    '"Historical cursor_ts prose"\nseq = 1',
    'seq = 1  # cursor_ts was retired',
])
def test_cursor_guard_accepts_inert_prose(source):
    assert not _executable_cursor_ts_references(source)


@pytest.mark.parametrize("source", [
    'cursor_ts = 1',
    'raw = store.cursor_ts',
    'def load(cursor_ts):\n    return cursor_ts',
    'profile = {"cursor_ts": 1}',
    'raw = profile["cursor_ts"]',
    'raw = profile.get("v2_reply_cursor_ts")',
    'CURSOR_KEY = "v2_reply_cursor_ts"',
    'db.execute("SELECT cursor_ts FROM reply_cursors")',
    'load(cursor_ts=1)',
])
def test_cursor_guard_rejects_executable_timestamp_cursor(source):
    assert _executable_cursor_ts_references(source)
