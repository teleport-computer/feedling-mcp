"""Guard: the Flask parity facade ``backend/app.py`` stays deleted.

The ASGI migration (§13) ended with ``app.py``'s deletion; the only assembly
layer is ``backend/asgi_app.py`` and tests drive it via
``asgi_test_client.make_client()``. This guard keeps both facts true:

1. ``backend/app.py`` must not reappear (a revert or stale branch merge would
   silently resurrect the old startup chain and its import-time side effects);
2. no test regrows a dependency on the old facade (``import app as appmod`` /
   ``importlib.import_module("app")``).

Pure text/filesystem checks — no DB, safe for no-Postgres dev machines
(listed in conftest's ``_PURE_UNIT``).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_FACADE_IMPORT = re.compile(
    r'^\s*(import app as appmod|import app\s*$|from app import|'
    r'import backend\.app\b|from backend import app\b|from backend\.app import|'
    r'.*import_module\(\s*["\'](?:backend\.)?app["\']\s*\))',
    re.MULTILINE,
)


def test_app_py_stays_deleted():
    assert not (REPO / "backend" / "app.py").exists(), (
        "backend/app.py reappeared — the Flask parity facade was deleted at the "
        "end of the ASGI migration (§13); assembly lives in backend/asgi_app.py"
    )


def test_no_test_imports_the_old_facade():
    offenders = []
    for path in sorted((REPO / "tests").glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        if _FACADE_IMPORT.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders, (
        f"these tests import the deleted app.py facade: {offenders}; "
        "drive the backend via asgi_test_client.make_client() and import "
        "domain packages directly"
    )
