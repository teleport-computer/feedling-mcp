"""Guard: the stable-seq reply cursor (spec A1) never regresses to wall-clock ts.

Mirrors the source-guard half of ``tests/test_v2_no_gateway_dependency.py``: a
cheap static grep that catches an accidental reintroduction before it ships,
rather than proving behaviour end-to-end.

Scope, precisely: this guards the NEW stable-seq cursor machinery this task
adds (``cursor.py`` + the ``db.chat_max_seq`` / ``db.chat_messages_after_seq``
read paths) and every OTHER v2 module against ever naming ``cursor_ts`` again.

It deliberately does NOT cover ``invalidation.py`` (and its one caller,
``worker.py``), which already use a wall-clock ``cursor_ts`` for a *different*
mechanism: the §8 replan safe-point state machine decides whether newer
in-memory (enclave-decrypted, not-yet-persisted-with-seq) messages invalidate
an in-flight plan. That mechanism operates over already-decrypted message
dicts assembled by injected ``TurnDeps.read_messages[_since]`` callables, not
over ``chat_messages`` rows, and repointing it to seq would require those
dicts to carry ``seq``, would change ``TurnDeps.read_messages_since``'s and
``invalidation.evaluate``'s public signatures, and would ripple into
``worker.py``'s last-replied-ts monotonicity logic and every test that injects
a ts-based fake reader. That is a separate, larger refactor properly out of
scope for the Task 5 stable-seq-cursor addition (see the PR A plan, Task 5
survey notes) — hence the explicit exemption below rather than silence.
"""
from __future__ import annotations

import pathlib

_V2_DIR = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"

# invalidation.py's cursor_ts is the pre-existing, deliberately-unrelated §8
# replan safe-point mechanism (see module docstring above); worker.py only
# passes a value through to it via the `coalesced_cursor_ts=` kwarg. Neither is
# part of the seq-based reply cursor this task introduces.
_EXEMPT = {"invalidation.py", "worker.py"}


def _modules():
    for p in sorted(_V2_DIR.glob("*.py")):
        if p.name not in _EXEMPT:
            yield p


def _non_comment_code(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def test_no_cursor_ts_outside_the_documented_exemption():
    offenders = []
    for path in _modules():
        code = _non_comment_code(path.read_text())
        if "cursor_ts" in code:
            offenders.append(path.name)
    assert not offenders, (
        "cursor_ts leaked into a v2 module outside the documented replan "
        f"safe-point exemption {_EXEMPT}; the stable reply cursor must stay "
        f"seq-based (spec A1). Offenders: {offenders}"
    )


def test_new_cursor_module_is_seq_only():
    src = (_V2_DIR / "cursor.py").read_text()
    assert "cursor_ts" not in src
    assert "seq" in src  # sanity: the module actually references the seq cursor
