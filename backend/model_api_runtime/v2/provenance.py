"""Provenance tagging + deterministic write gate (spec C4).

Every tool observation is tagged with where its authorization comes from. A durable-write
tool_call is refused when the turn holds no user/wake authorization — i.e. a purely web-driven
round cannot self-authorize a memory_write/identity_patch/schedule. Fixed rule in code, independent
of what the tool content says, so a prompt-injecting page can never grant itself write access.
"""
from __future__ import annotations
from capabilities import registry as cap_registry

USER = "user"
WAKE_TRIGGER = "wake_trigger"
EXTERNAL = "external"
INTERNAL = "internal"

_EXTERNAL_READS = frozenset({"web_search", "web_fetch"})


def provenance_for_read(tool_name: str) -> str:
    return EXTERNAL if tool_name in _EXTERNAL_READS else INTERNAL


def turn_has_write_authorization(seed: str) -> bool:
    return seed in (USER, WAKE_TRIGGER)


def write_gate(tool_name: str, *, turn_authorization: bool) -> tuple[bool, str]:
    """Reads are never gated. A WRITE_ACTIONS tool is allowed only when the turn holds a
    user/wake authorization; otherwise deterministically refused."""
    if tool_name not in cap_registry.WRITE_ACTIONS:
        return True, ""
    if turn_authorization:
        return True, ""
    return False, f"write refused: no user/wake authorization in this turn for {tool_name}"
