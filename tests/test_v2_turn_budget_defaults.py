"""Source-level guards for Runtime V2's deployment-safe turn defaults."""
from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def _assignment_default(path: Path, target: str, env_name: str) -> tuple[int, object]:
    tree = ast.parse(path.read_text())
    matches: list[tuple[int, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not any(
            isinstance(item, ast.Name) and item.id == target
            for item in node.targets
        ):
            continue
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call) or len(call.args) < 2:
                continue
            call_name = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else call.func.id if isinstance(call.func, ast.Name) else ""
            )
            if (
                call_name in {"get", "_positive_float_env"}
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == env_name
                and isinstance(call.args[1], ast.Constant)
            ):
                matches.append((node.lineno, call.args[1].value))
    assert len(matches) == 1, (
        f"expected one {target}/{env_name} assignment in {path}; "
        f"found line/default pairs {matches}"
    )
    return matches[0]


def test_default_turn_provider_round_limit_is_fifteen():
    line, default = _assignment_default(
        ROOT / "backend/model_api_runtime/v2/worker.py",
        "_TURN_MAX_LLM_CALLS",
        "FEEDLING_V2_TURN_MAX_LLM_CALLS",
    )
    assert default == "15", f"worker.py:{line} default={default!r}, expected '15'"


def test_default_absolute_timeout_covers_fifteen_round_envelope():
    line, default = _assignment_default(
        ROOT / "backend/model_api_runtime/v2/serve_worker.py",
        "_TURN_ABSOLUTE_TIMEOUT_SEC",
        "FEEDLING_V2_TURN_ABSOLUTE_TIMEOUT_SEC",
    )
    assert default == "3000", (
        f"serve_worker.py:{line} default={default!r}, expected '3000'"
    )
