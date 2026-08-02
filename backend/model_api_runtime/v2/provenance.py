"""Provenance tagging + deterministic write gates (spec C4).

A user/wake seed is necessary for a durable write, but it is not sufficient after
the model has observed untrusted external content.  The unified loop therefore
removes all durable writes, fresh outbound web calls, and every outbound MCP tool
after a ``web_search``/``web_fetch``/MCP/task dispatch.  A task is conservatively
external because its child summary can transitively contain web or private-read
content.  The loop permits only exact ``web_fetch`` URLs returned by a preceding
search in the same turn, preserving the normal search -> read flow without letting
page text invent an exfiltration URL/query.  The seed gate below remains defence in
depth for direct executor callers and wake/user-less paths.
"""
from __future__ import annotations
from capabilities import registry as cap_registry

USER = "user"
WAKE_TRIGGER = "wake_trigger"
EXTERNAL = "external"
INTERNAL = "internal"

# A child summary can contain remote web text or user-editable workspace/memory
# content. Treat it exactly like other external model input in the parent loop.
EXTERNAL_READS = frozenset({"web_search", "web_fetch", "task"})
IDENTITY_WRITE_ACTIONS = frozenset({"identity_patch", "identity_nudge"})


def provenance_for_read(tool_name: str) -> str:
    return EXTERNAL if tool_name in EXTERNAL_READS else INTERNAL


def turn_has_write_authorization(seed: str) -> bool:
    return seed in (USER, WAKE_TRIGGER)


def write_gate(
    tool_name: str, *, turn_authorization: bool,
    identity_write_authorization: bool = True,
) -> tuple[bool, str]:
    """Reads are never gated. A WRITE_ACTIONS tool is allowed only when the turn holds a
    user/wake authorization; otherwise deterministically refused."""
    if tool_name not in cap_registry.WRITE_ACTIONS:
        return True, ""
    if tool_name in IDENTITY_WRITE_ACTIONS and not identity_write_authorization:
        return False, f"error: identity write refused in background turn for {tool_name}"
    if turn_authorization:
        return True, ""
    return False, f"write refused: no user/wake authorization in this turn for {tool_name}"
