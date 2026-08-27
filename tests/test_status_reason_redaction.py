"""Drift guard for the fail-closed ``status_reason`` vocabulary.

The redaction in ``notices/status_reason.py`` is fail-closed: a reason our own
code writes but that nobody declared collapses into the ``<redacted>`` bucket.
That is the safe direction, but it fails *silently* — the admin page and the
debug page keep rendering, just with one bucket less legible. Nothing goes red.

So the vocabulary needs a guard that goes red instead: every ``status_reason``
our own code writes as a literal must be sanctioned. When someone adds a new
lifecycle terminal, this test names it.

The sanitizer's own behaviour table (opaque single tokens, colon tails, URLs)
lives with the admin endpoint that first leaked, in
``tests/test_data_track.py``; this file only guards the vocabulary's coverage.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from notices import error_contract, status_reason  # noqa: E402
from proactive import gate  # noqa: E402


def _backend_sources() -> list[Path]:
    return sorted(p for p in BACKEND.rglob("*.py") if p.is_file())


def _status_reason_literals() -> dict[str, list[str]]:
    """Every ``"status_reason": "<literal>"`` written anywhere in backend/.

    Reads the dict key rather than the call site, so a new producer in a file
    nobody thought of is still caught.
    """
    found: dict[str, list[str]] = {}
    for path in _backend_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or key.value != "status_reason":
                    continue
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                where = f"{path.relative_to(BACKEND)}:{value.lineno}"
                found.setdefault(value.value, []).append(where)
    return found


def _rejecting_wake_control_reasons() -> dict[str, list[str]]:
    """Reasons ``WakeControlDecisionV2`` rejects with.

    ``poll_core.py`` copies ``decision.reason`` straight into ``status_reason``
    on the reject path only, so the accepting reasons are deliberately not
    collected here — they never reach this keyspace.
    """
    path = BACKEND / "proactive" / "controls_v2.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "WakeControlDecisionV2":
            continue
        if len(node.args) < 2:
            continue
        accepted, reason = node.args[0], node.args[1]
        if not isinstance(accepted, ast.Constant) or accepted.value is not False:
            continue
        if not isinstance(reason, ast.Constant) or not isinstance(reason.value, str):
            continue
        found.setdefault(reason.value, []).append(f"controls_v2.py:{reason.lineno}")
    return found


def test_scanners_find_their_producers():
    """The two scanners must be measuring something.

    A scan that silently matches nothing passes every assertion below while
    guarding nothing, so pin the shape of what it found rather than only a
    count: an AST refactor that breaks the walk fails here and not later.
    """
    literals = _status_reason_literals()
    assert "agent_greeted" in literals, literals
    assert any(
        loc.startswith("proactive/poll_core.py") for loc in literals["agent_greeted"]
    ), literals["agent_greeted"]
    assert len(literals) >= 4, literals

    rejects = _rejecting_wake_control_reasons()
    assert "ambient_disabled" in rejects, rejects
    assert len(rejects) >= 6, rejects
    # The accepting reasons must stay out; they are not this keyspace.
    assert "allowed" not in rejects
    assert "manual_bypass" not in rejects


def test_every_written_status_reason_literal_is_sanctioned():
    unsanctioned = {
        literal: locations
        for literal, locations in _status_reason_literals().items()
        if status_reason.sanitize_status_reason(literal) != literal
    }
    assert not unsanctioned, (
        "these reasons are written by our own code but are not declared in "
        "PROACTIVE_LIFECYCLE_REASONS or any producer registry, so they will "
        f"render as <redacted>: {unsanctioned}"
    )


def test_every_wake_control_rejection_reason_is_sanctioned():
    unsanctioned = {
        reason: locations
        for reason, locations in _rejecting_wake_control_reasons().items()
        if status_reason.sanitize_status_reason(reason) != reason
    }
    assert not unsanctioned, (
        "poll_core.py copies decision.reason into status_reason on the reject "
        f"path, so these would render as <redacted>: {unsanctioned}"
    )


def test_non_literal_producers_are_sanctioned():
    """Producers the AST scan cannot see, because they write a name.

    ``proactive_core.py`` writes ``gate.HEARTBEAT_THROTTLED_REASON`` and
    ``db.content_free_failure_code`` returns ``runtime_failed`` as its
    collapsed-failure placeholder. Both are literal in effect but invisible to
    a scan keyed on ``ast.Constant``, so they are named here.
    """
    throttled = gate.HEARTBEAT_THROTTLED_REASON
    assert status_reason.sanitize_status_reason(throttled) == throttled
    assert status_reason.sanitize_status_reason("runtime_failed") == "runtime_failed"


def test_registry_codes_survive_unchanged():
    """The vocabulary is derived, so a newly registered error class is
    displayable without editing the redaction module. This asserts the
    derivation actually holds rather than trusting the comprehension."""
    codes = [spec.code for spec in error_contract.all_specs()]
    assert codes, "error_contract registry is empty; the derivation guards nothing"
    redacted = [c for c in codes if status_reason.sanitize_status_reason(c) != c]
    assert not redacted, redacted


def test_undeclared_reason_is_redacted_whole():
    """The fail-closed direction, stated once here so the guard above has a
    counterpart: passing is not the default."""
    assert status_reason.sanitize_status_reason("not_a_declared_reason") == "<redacted>"
