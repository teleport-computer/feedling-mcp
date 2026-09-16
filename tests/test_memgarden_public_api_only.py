"""io production code uses only memgarden's public API.

## What counts as public

memgarden publishes two things (``memgarden/__init__.py``):

    memgarden.__all__          top-level names (GardenComponent, contracts types, ...)
    memgarden.STABLE_MODULES   submodules with a stable ``__all__`` (timestamps,
                               retrieval, related, text.card_guard, prompts.buckets ...)

Everything else (``prompts.capture``, ``prompts.dream``, ``scoring.*``,
``rendering`` ...) is an internal part that can be renamed in any release. An io
import of one means io silently depends on memgarden's orchestration internals;
that breaks on upgrade and, worse, keeps a second copy of the rules in io.

## What this scans

``backend/**/*.py`` and ``tools/**/*.py`` (not tests), with the AST, including
imports inside functions and ``importlib.import_module("memgarden...")`` literals:

    import memgarden.X [as a]            X must be a stable module
    from memgarden import a              a in memgarden.__all__, or memgarden.a stable
    from memgarden.X import a            X stable and a in X.__all__
    from memgarden.X import *            always rejected
    alias.attr[.more]                    the whole attribute chain is resolved:
    memgarden.X.attr[.more]              ``import memgarden[.X]`` binds ``memgarden``;
                                         after the longest stable module in the
                                         chain, the next name must be in that
                                         module's __all__; with no stable module,
                                         memgarden.<name> must be in memgarden.__all__

Complements ``test_orchestration_is_not_reimplemented.py`` (names of
orchestration functions), which cannot see an import routed through a shim.
"""
from __future__ import annotations

import ast
import importlib
import pathlib
import textwrap

import memgarden
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Transitional exemptions: file -> the exact non-public modules it may import.
#: Each entry names who removes it. The list only shrinks.
ALLOWED_INTERNAL: dict[str, set[str]] = {
    # Automatic-recall kill switch (FEEDLING_MEMORY_RECALL_UNIFIED_RANKER=0) falls
    # back to the deprecated legacy selector. TODO(feat/memx-garden-recall): delete
    # with the switch once the unified ranker has run on prod for a release.
    "backend/enclave/routes/chat.py": {"memgarden.scoring.relevance"},
}


def _stable() -> set[str]:
    return set(memgarden.STABLE_MODULES)


def _exports(module: str) -> set[str]:
    return set(getattr(importlib.import_module(module), "__all__", ()))


def violations(source: str, path: str = "<memory>") -> list[str]:
    """Every non-public memgarden use in ``source`` as ``path:line module[.name]``."""
    stable = _stable()
    top = set(memgarden.__all__)
    tree = ast.parse(source)
    found: list[str] = []
    aliases: dict[str, str] = {}

    def bad(node: ast.AST, what: str) -> None:
        found.append(f"{path}:{getattr(node, 'lineno', 0)} {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name != "memgarden" and not name.startswith("memgarden."):
                    continue
                if name != "memgarden" and name not in stable:
                    bad(node, name)
                if not alias.asname:
                    # ``import memgarden.X`` binds ``memgarden``, not ``X``.
                    aliases["memgarden"] = "memgarden"
                elif name == "memgarden" or name in stable:
                    aliases[alias.asname] = name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or (module != "memgarden" and not module.startswith("memgarden.")):
                continue
            for alias in node.names:
                if alias.name == "*":
                    bad(node, f"{module}.*")
                    continue
                if module == "memgarden":
                    sub = f"memgarden.{alias.name}"
                    if sub in stable:
                        aliases[alias.asname or alias.name] = sub
                    elif alias.name not in top:
                        bad(node, sub)
                    continue
                sub = f"{module}.{alias.name}"
                if sub in stable:  # from memgarden.prompts import buckets
                    aliases[alias.asname or alias.name] = sub
                elif module not in stable:
                    bad(node, sub)
                elif alias.name not in _exports(module):
                    bad(node, sub)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "import_module" and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
              and node.args[0].value.startswith("memgarden")):
            name = node.args[0].value
            if name != "memgarden" and name not in stable:
                bad(node, name)

    inner = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        # Only whole chains: ``a.b.c`` is checked once, not again as ``a.b``.
        if not isinstance(node, ast.Attribute) or id(node) in inner:
            continue
        attrs: list[str] = []
        root: ast.AST = node
        while isinstance(root, ast.Attribute):
            attrs.insert(0, root.attr)
            root = root.value
        if not isinstance(root, ast.Name) or root.id not in aliases:
            continue
        parts = aliases[root.id].split(".") + attrs
        floor = aliases[root.id].count(".") + 1
        cut = next((i for i in range(len(parts), floor - 1, -1)
                    if ".".join(parts[:i]) in stable), None)
        if cut is not None:
            if cut < len(parts) and parts[cut] not in _exports(".".join(parts[:cut])):
                bad(node, ".".join(parts[:cut + 1]))
        elif parts[1] not in top:  # root is the memgarden package itself
            bad(node, ".".join(parts))
    return found


def _sources():
    for root in ("backend", "tools"):
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in rel or "node_modules" in rel:
                continue
            yield rel, path.read_text("utf-8")


def _module_of(violation: str) -> str:
    what = violation.split(" ", 1)[1]
    for module in sorted({m for mods in ALLOWED_INTERNAL.values() for m in mods}, key=len, reverse=True):
        if what == module or what.startswith(module + "."):
            return module
    return what


def test_production_code_imports_only_public_memgarden_api():
    problems = []
    used_exemptions: dict[str, set[str]] = {}
    for rel, source in _sources():
        if "memgarden" not in source:
            continue
        for violation in violations(source, rel):
            module = _module_of(violation)
            if module in ALLOWED_INTERNAL.get(rel, set()):
                used_exemptions.setdefault(rel, set()).add(module)
                continue
            problems.append(violation)
    assert not problems, (
        "io must use memgarden's public API (memgarden.__all__ / STABLE_MODULES):\n  "
        + "\n  ".join(problems))
    # A stale exemption hides the next regression in that file: drop it once unused.
    stale = {rel: mods - used_exemptions.get(rel, set())
             for rel, mods in ALLOWED_INTERNAL.items() if mods - used_exemptions.get(rel, set())}
    assert not stale, f"remove unused exemptions: {stale}"


@pytest.mark.parametrize("snippet,expected", [
    ("from memgarden.scoring.relevance import _memory_relevance", "memgarden.scoring.relevance._memory_relevance"),
    ("from memgarden.prompts.dream import *", "memgarden.prompts.dream.*"),
    ("import memgarden.rendering", "memgarden.rendering"),
    ("from memgarden import scoring", "memgarden.scoring"),
    ("from memgarden.retrieval import _evaluate", "memgarden.retrieval._evaluate"),
    ("from memgarden import retrieval as r\nr._evaluate('q', [])", "memgarden.retrieval._evaluate"),
    ("def f():\n    from memgarden.prompts import capture\n", "memgarden.prompts.capture"),
    ("import importlib\nimportlib.import_module('memgarden.scoring.selector')", "memgarden.scoring.selector"),
    # Codex r4 M1: a dotted import binds ``memgarden``; resolve the whole chain.
    ("import memgarden.retrieval\nmemgarden.retrieval._evaluate('q', [])", "memgarden.retrieval._evaluate"),
    ("import memgarden\nmemgarden.retrieval._evaluate('q', [])", "memgarden.retrieval._evaluate"),
    ("import memgarden\nmemgarden.prompts.capture.build()", "memgarden.prompts.capture.build"),
    ("import memgarden.text.card_guard\nmemgarden.text.card_guard._strong", "memgarden.text.card_guard._strong"),
    ("import memgarden as mg\nmg.retrieval._evaluate", "memgarden.retrieval._evaluate"),
    ("from memgarden import text as t", "memgarden.text"),
])
def test_guard_goes_red_on_internal_use(snippet, expected):
    found = violations(textwrap.dedent(snippet))
    assert [v.split(" ", 1)[1] for v in found] == [expected]


@pytest.mark.parametrize("snippet", [
    "from memgarden import GardenComponent, STABLE_MODULES",
    "from memgarden import timestamps as t\nt.sort_key('x')",
    "from memgarden.prompts import buckets as b\nb.normalize_bucket_language",
    "from memgarden.retrieval import rank, Tokenizer",
    "import memgarden.related as rel\nrel.one_hop([], [])",
    "from memgarden.text import card_guard",
    "import memgarden.retrieval\nmemgarden.retrieval.rank([], [])",
    "import memgarden\nmemgarden.GardenComponent\nmemgarden.STABLE_MODULES",
    "import memgarden.text.card_guard as cg\ncg",
    "import memgarden.retrieval as r\nr.rank.__name__",
])
def test_guard_accepts_public_api(snippet):
    assert violations(snippet) == []
