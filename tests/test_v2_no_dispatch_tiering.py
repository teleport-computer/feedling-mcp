"""Guard: no `is_official` -> rule/official dispatch in V2 production (PR C Task 9 /
"C9b — delete the dispatch/planner/responder-hot-path/agent_loop machinery").

Locks in the Global Constraint: "No behavior tiering: NO `is_official ->
rule_plan/official_plan` dispatch in V2 production." Production does not even run
the obsolete provider-identity classifier. Same tool catalog + loop for all models.
Chat (Task 7) and wake (Task 8) both migrated off the old two-layer
while/replan (`invalidation.py`) + json_planner (`planner.py`'s `plan`/`rule_plan`/
`official_plan`) + forced `responder.respond` round-trip pipeline onto the single
provider-native `tool_loop.run_tool_loop`.

The old modules remain only as compatibility/test-helper surfaces; neither the
production worker nor the D4 token rollback gate may use the staged pipeline. The
durable invariant is that both execute `tool_loop.run_tool_loop` and never branch
turn behavior on `is_official`.
"""
import ast
import pathlib

_V2 = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"
_WORKER = _V2 / "worker.py"
_LOADTEST_COMPARE = pathlib.Path(__file__).parent.parent / "scripts" / "loadtest" / "compare_tokens.py"

_FORBIDDEN_CALL_PATTERNS = (
    "official_plan(",
    "rule_plan(",
    "v2_planner.plan(",
    "planner.plan(",
    "agent_loop.run_turn(",
    "v2_agent_loop.run_turn(",
    "responder.respond(",
    "v2_responder.respond(",
)

# worker.py used to alias-import these two modules; PR C9b removed both imports
# entirely (the third old-pipeline module, `invalidation.py`, keeps a live,
# non-dispatch use elsewhere in worker.py's coalesce/fold machinery and is out of
# this guard's scope).
_FORBIDDEN_IMPORT_MODULES = {"planner", "agent_loop"}


def _non_comment_source(path: pathlib.Path) -> str:
    """Strip full-line `#` comments before substring-matching, so a comment/docstring
    explaining what Task 7/8 replaced (there are many, deliberately, across this
    codebase) can't false-positive this guard. Mirrors the pattern already used by
    tests/test_v2_no_gateway_dependency.py's `_modules()`/source-scan guard."""
    src = path.read_text()
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def test_worker_never_calls_the_old_staged_pipeline():
    code = _non_comment_source(_WORKER)
    offenders = [p for p in _FORBIDDEN_CALL_PATTERNS if p in code]
    assert not offenders, (
        "worker.py (the production turn driver) must not call into the old "
        f"planner/responder/agent_loop pipeline; found: {offenders}"
    )


def test_worker_does_not_import_planner_or_agent_loop():
    tree = ast.parse(_WORKER.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "model_api_runtime.v2":
            for alias in node.names:
                imported.add(alias.name)
    offenders = imported & _FORBIDDEN_IMPORT_MODULES
    assert not offenders, f"worker.py must not import the old dispatch modules: {offenders}"


def test_process_job_never_branches_on_is_official():
    """The optional compatibility keyword may remain for direct legacy tests,
    but nothing inside `process_job` may read it in an `if` test — that would
    resurrect is_official -> behavior dispatch."""
    tree = ast.parse(_WORKER.read_text())
    process_job = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "process_job"
    )
    offenders = [
        ast.dump(node.test)
        for node in ast.walk(process_job)
        if isinstance(node, ast.If) and "is_official" in ast.dump(node.test)
    ]
    assert not offenders, f"process_job must not branch on is_official: {offenders}"


def test_run_turn_never_calls_is_official():
    """The old classifier must be absent from the production hot path, not
    merely computed and then ignored."""
    tree = ast.parse(_WORKER.read_text())
    run_turn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_turn"
    )
    calls = [
        ast.dump(node.func)
        for node in ast.walk(run_turn)
        if isinstance(node, ast.Call)
    ]
    assert not any("is_official" in call for call in calls)


def test_run_wake_never_branches_on_is_official():
    """Same invariant for the wake lane's own handler (`_run_wake`, Task 8) —
    `is_official` isn't even threaded into it (it's not one of its parameters), but
    this guard is here so an `is_official` re-introduction into that function would
    also be caught even if a future change added the parameter back."""
    tree = ast.parse(_WORKER.read_text())
    run_wake = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_wake"
    )
    offenders = [
        ast.dump(node.test)
        for node in ast.walk(run_wake)
        if isinstance(node, ast.If) and "is_official" in ast.dump(node.test)
    ]
    assert not offenders, f"_run_wake must not branch on is_official: {offenders}"


def test_token_rollback_gate_uses_the_production_unified_loop():
    """D4 must measure what production runs, not the retired staged pipeline."""
    code = _non_comment_source(_LOADTEST_COMPARE)
    assert "v2_tool_loop.run_tool_loop(" in code
    offenders = [pattern for pattern in _FORBIDDEN_CALL_PATTERNS if pattern in code]
    assert not offenders, f"token rollback gate uses retired pipeline: {offenders}"
