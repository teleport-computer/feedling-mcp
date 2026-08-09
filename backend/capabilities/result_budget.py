"""One trusted result-budget policy, read by every layer that truncates.

History search spec §3.3.  A tool result passes two independent truncations on
its way to the model: the executor's per-result cap
(``executor._summarize_capability_result``) and the tool loop's per-result cap
plus same-batch water-fill (``tool_loop._normalize_tool_results``).  Both cut
**strings after serialization**, so raising only the executor's cap is useless:
the water-fill cuts the result back to ~2000 — and under the worst batch shape
(seven maxed-out siblings) to ~1000 — which slices a JSON payload mid-string.

So the numbers live here once and all three layers read them:

    history_fetch:  result_cap=4500  atomic_json=True  extra_batch_budget=2500
    history_search: result_cap=1800  atomic_json=True

* ``atomic_json`` — the water-fill reserves this result's full length up front
  and shares only the remainder with its siblings.  It is never cut, at any
  batch shape.  Only declare it for producers that shrink **structurally**
  before serializing (the history facade does); the cap is then a contract the
  producer already met, not a blind guillotine.
* ``extra_batch_budget`` — how much the same-batch total may rise while such a
  result is present.  Without it a 4500-char fetch would leave the other seven
  results ~500 characters each, which is a real product regression.

The facade decides *how* to shrink; it does not decide the numbers.  It marks
the result kind through the trusted ``ToolResult.metadata`` channel
(``RESULT_KIND_METADATA_KEY``) — the same channel the memory tools already use
for their truncation marker — and the loop looks the policy up from there.
Provider-supplied text can never reach this channel.

Values are env-overridable so a deployment can dial them back without a code
change; the override is read at call time so all three layers always agree.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

# metadata key carrying the trusted result kind (set at the capability boundary).
RESULT_KIND_METADATA_KEY = "result_budget_kind"

HISTORY_SEARCH_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_HISTORY_SEARCH_RESULT_MAX_CHARS"
HISTORY_FETCH_RESULT_MAX_CHARS_ENV = "FEEDLING_V2_HISTORY_FETCH_RESULT_MAX_CHARS"
DEFAULT_HISTORY_SEARCH_RESULT_MAX_CHARS = 1800
DEFAULT_HISTORY_FETCH_RESULT_MAX_CHARS = 4500
DEFAULT_HISTORY_FETCH_EXTRA_BATCH_CHARS = 2500


@dataclass(frozen=True)
class ResultBudget:
    """Per-tool serialization budget shared by executor and tool loop."""

    result_cap: int
    atomic_json: bool = False
    extra_batch_budget: int = 0


def _int_env(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)


def for_tool(tool_name) -> ResultBudget | None:
    """Policy for one tool name, or None = the caller's generic default."""
    name = str(tool_name or "")
    if name == "history_search":
        return ResultBudget(
            result_cap=_int_env(
                HISTORY_SEARCH_RESULT_MAX_CHARS_ENV,
                DEFAULT_HISTORY_SEARCH_RESULT_MAX_CHARS,
            ),
            atomic_json=True,
        )
    if name == "history_fetch":
        return ResultBudget(
            result_cap=_int_env(
                HISTORY_FETCH_RESULT_MAX_CHARS_ENV,
                DEFAULT_HISTORY_FETCH_RESULT_MAX_CHARS,
            ),
            atomic_json=True,
            extra_batch_budget=DEFAULT_HISTORY_FETCH_EXTRA_BATCH_CHARS,
        )
    return None


def for_metadata(metadata: Mapping[str, Any] | None) -> ResultBudget | None:
    """Policy for one already-produced ToolResult, via its trusted metadata."""
    if not isinstance(metadata, Mapping):
        return None
    return for_tool(metadata.get(RESULT_KIND_METADATA_KEY))
