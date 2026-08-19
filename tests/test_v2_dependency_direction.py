"""Guard: V2 runtime CORE modules must not import `hosted`/`agent_runtime`.
Only serve_worker.py (the injection/entrypoint layer) may. Mirrors the
test_no_flask_anywhere guard pattern. AST-based so it can't be fooled by comments/strings.
"""

import ast
import pathlib

_V2 = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"
_BACKEND = pathlib.Path(__file__).parent.parent / "backend"

# serve_worker.py is the assembly/entrypoint layer and is ALLOWED to import hosted/
# agent_runtime — it is the one place the injection happens. Everything else in v2/ is core.
_EXEMPT = {"serve_worker.py", "__init__.py"}

# Derived, not hand-listed. A hardcoded roster silently exempts every module added after it
# was written: admission.py, scheduler.py, context.py, and another runtime module had
# all escaped this guard that way. Deriving from the directory means a new v2 module is
# guarded the moment it exists, which is the only version of this rule that stays true.
_CORE_MODULES = sorted(p.name for p in _V2.glob("*.py") if p.name not in _EXEMPT)
_FORBIDDEN = ("hosted", "agent_runtime", "admin")


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


def test_v2_core_modules_do_not_import_higher_level_packages():
    offenders = {}
    for name in _CORE_MODULES:
        path = _V2 / name
        assert path.exists(), f"expected V2 core module missing: {name}"
        imported = _top_level_imports(path)
        bad = [f for f in _FORBIDDEN if f in imported]
        if bad:
            offenders[name] = bad
    assert not offenders, (
        "V2 core modules must not import hosted/agent_runtime/admin "
        f"(dependency direction); offenders: {offenders}"
    )


def test_profile_generation_module_has_only_pure_stdlib_dependencies():
    imported = _top_level_imports(_V2 / "profile.py")
    assert imported <= {
        "__future__",
        "dataclasses",
        "json",
        "math",
        "re",
        "typing",
        "unicodedata",
    }


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _enqueue_job_trace_findings(source: str, *, filename: str):
    tree = ast.parse(source, filename=filename)
    module_aliases = {"jobs_store"}
    function_aliases = {"enqueue_job"}
    direct_imports = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jobs_store" or alias.name.endswith(".jobs_store"):
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            for alias in node.names:
                if alias.name == "jobs_store":
                    module_aliases.add(alias.asname or alias.name)
                if (
                    module == "jobs_store" or module.endswith(".jobs_store")
                ) and alias.name in {"enqueue_job", "*"}:
                    direct_imports.append(node.lineno)
                    if alias.name == "enqueue_job":
                        function_aliases.add(alias.asname or alias.name)

    calls = []
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct_call = (
            isinstance(node.func, ast.Name)
            and node.func.id in function_aliases
        )
        module_call = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "enqueue_job"
            and _dotted_name(node.func.value) in module_aliases
        )
        if not (direct_call or module_call):
            continue
        calls.append(node.lineno)
        if not any(keyword.arg == "trace_id" for keyword in node.keywords):
            missing.append(node.lineno)

    return calls, missing, direct_imports


def test_production_enqueue_job_calls_explicitly_set_trace_id():
    missing = []
    direct_imports = []
    call_count = 0
    for path in sorted(_BACKEND.rglob("*.py")):
        calls, missing_lines, import_lines = _enqueue_job_trace_findings(
            path.read_text(), filename=str(path)
        )
        relative = path.relative_to(_BACKEND.parent)
        call_count += len(calls)
        missing.extend(f"{relative}:{line}" for line in missing_lines)
        direct_imports.extend(f"{relative}:{line}" for line in import_lines)

    assert call_count, "trace_id guard found no jobs_store.enqueue_job calls"
    assert not direct_imports and not missing, (
        "production enqueue_job calls must use the jobs_store module and explicitly "
        "pass trace_id=; "
        f"direct imports at: {', '.join(direct_imports) or 'none'}; "
        f"missing trace_id at: {', '.join(missing) or 'none'}"
    )


def test_enqueue_job_trace_guard_covers_alias_and_direct_import_forms():
    calls, missing, direct_imports = _enqueue_job_trace_findings(
        """
from model_api_runtime.v2 import jobs_store as js
from model_api_runtime.v2.jobs_store import enqueue_job as eq

js.enqueue_job("u", "heartbeat")
eq("u", "heartbeat")
enqueue_job("u", "heartbeat")
""",
        filename="alias_probe.py",
    )

    assert len(calls) == 3
    assert missing == calls
    assert len(direct_imports) == 1
