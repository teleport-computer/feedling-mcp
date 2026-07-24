"""Guard: the stable-seq reply cursor (spec A1) never regresses to wall-clock ts.

Mirrors the source-guard half of ``tests/test_v2_no_gateway_dependency.py``: a
cheap static grep that catches an accidental reintroduction before it ships,
rather than proving behaviour end-to-end.

Scope: this guards the stable-seq cursor machinery and every V2 module except
``worker.py``, which still dual-writes the legacy timestamp rollback cursor
while the durable reply cursor itself is seq-based.
"""
from __future__ import annotations

import pathlib

_V2_DIR = pathlib.Path(__file__).parent.parent / "backend" / "model_api_runtime" / "v2"

_EXEMPT = {"worker.py"}


def _modules():
    for p in sorted(_V2_DIR.glob("*.py")):
        if p.name not in _EXEMPT:
            yield p


def _non_comment_code(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def test_no_cursor_ts_outside_worker_rollback_compatibility():
    offenders = []
    for path in _modules():
        code = _non_comment_code(path.read_text())
        if "cursor_ts" in code:
            offenders.append(path.name)
    assert not offenders, (
        "cursor_ts leaked into a v2 module outside the documented rollback "
        f"safe-point exemption {_EXEMPT}; the stable reply cursor must stay "
        f"seq-based (spec A1). Offenders: {offenders}"
    )


def test_new_cursor_module_is_seq_only():
    src = (_V2_DIR / "cursor.py").read_text()
    assert "cursor_ts" not in src
    assert "seq" in src  # sanity: the module actually references the seq cursor
