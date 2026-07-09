"""Guard: V2 runtime CORE modules must not import `hosted`/`agent_runtime`.
Only serve_worker.py (the injection/entrypoint layer) may. Mirrors the
test_no_flask_anywhere guard pattern. AST-based so it can't be fooled by comments/strings."""
import ast
import pathlib

_V2 = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"
_CORE_MODULES = [
    "worker.py", "coalesce.py", "planner.py", "executor.py",
    "invalidation.py", "responder.py", "jobs_store.py", "status_stream.py",
]
_FORBIDDEN = ("hosted", "agent_runtime")


def _top_level_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import; module may be None
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_v2_core_modules_do_not_import_hosted_or_agent_runtime():
    offenders = {}
    for name in _CORE_MODULES:
        path = _V2 / name
        assert path.exists(), f"expected V2 core module missing: {name}"
        imported = _top_level_imports(path)
        bad = [f for f in _FORBIDDEN if f in imported]
        if bad:
            offenders[name] = bad
    assert not offenders, (
        "V2 core modules must not import hosted/agent_runtime "
        f"(dependency direction); offenders: {offenders}"
    )
