"""Framework-neutral read/write for the web-search toggle.

Route layer stays thin (CONTRIBUTING.md); everything decidable lives here so it
can be unit-tested without a database.

Three fields, deliberately distinct:

- ``enabled``   the user's saved preference. Only the user ever writes it.
- ``available`` whether web is usable right now (operator kill switch).
- ``effective`` ``enabled and available`` — derived, never stored.

Collapsing them would mean an operator halting web rewrites the user's own
choice, and restoring the feature would leave everyone switched off with no
signal that they need to go back and re-enable it.
"""

from __future__ import annotations

from model_api_runtime.v2 import kill_switch


def get_settings(store, *, halted_reader=kill_switch.web_halted) -> dict:
    """``halted_reader`` is injectable so this module stays unit-testable —
    the default is the DB-backed control-table read."""
    enabled = store.load_web_settings().get("enabled") is True
    search_halted, fetch_halted = halted_reader()
    # The product surface is called "web search", so availability tracks search.
    # `not (search_halted and fetch_halted)` would report the feature available
    # while search — the thing the switch is named after — is down.
    available = not search_halted
    return {
        "enabled": enabled,
        "available": available,
        "effective": enabled and available,
        "unavailable_reason": None if available else "globally_disabled",
        # Half-open state is surfaced explicitly rather than hidden behind the
        # single `available` bit.
        "capabilities": {"search": not search_halted, "fetch": not fetch_halted},
    }


def update_settings(store, payload, *, halted_reader=kill_switch.web_halted) -> dict:
    """Strict booleans: ``{"enabled": "no"}`` is a client bug, not a request to
    switch web ON, so it is rejected rather than coerced."""
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise ValueError("enabled is required")
    if not isinstance(payload["enabled"], bool):
        raise ValueError("enabled must be boolean")
    store.save_web_settings({"enabled": payload["enabled"]})
    return get_settings(store, halted_reader=halted_reader)
