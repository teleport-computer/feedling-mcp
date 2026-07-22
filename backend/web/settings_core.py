"""Framework-neutral read/write for the web-search toggle.

Routes stay thin (CONTRIBUTING.md); everything decidable lives here so it can be
unit-tested without a database.

The contract deliberately does NOT collapse into one "on/off" boolean, because
three independent things decide whether a turn actually reaches the network:

- ``enabled``            the user's saved preference. Only the user writes it.
- ``runtime_supported``  whether this account's runtime even has the tools.
                         Self-hosted (``resident_cli``) users run their own
                         consumer, which never loads the V2 tool loop — for them
                         the switch is inert no matter what it says.
- the operator halts     ``web_search`` and ``web_fetch`` stop independently, so
                         "half open" is a real state and must be reportable.

An earlier version reported ``effective = enabled and not search_halted``. That
was wrong twice over: it told a self-hosted user their toggle was in effect when
their runtime has no web tools at all, and it reported "not effective" while
``web_fetch`` was still perfectly usable.

The switch covers every lane, background companion included, so ``effective``
carries no lane qualifier.
"""

from __future__ import annotations

from model_api_runtime.v2 import kill_switch, web_gate

_TOOL_ORDER = ("web_search", "web_fetch")


def _runtime_supported(store) -> bool:
    """True when this account's chat actually runs on the V2 tool loop.

    Lazy import: chat/web routes must not own hosted startup (same idiom as
    ``chat.poll_core`` and ``chat.service``).
    """
    from hosted import config_store as hosted_config_store

    try:
        return bool(hosted_config_store.hosted_runtime_v2_enabled_strict(store))
    except Exception:  # noqa: BLE001 — a control read must not 500 the settings page
        return False


def get_settings(
    store,
    *,
    halted_reader=kill_switch.web_halted,
    runtime_supported_reader=_runtime_supported,
) -> dict:
    """Both readers are injectable so this module stays unit-testable; the
    defaults are the DB-backed ones."""
    enabled = store.load_web_settings().get("enabled") is True
    supported = bool(runtime_supported_reader(store))
    search_halted, fetch_halted = halted_reader()

    tools = {
        "web_search": {"available": supported and not search_halted},
        "web_fetch": {"available": supported and not fetch_halted},
    }
    live = [name for name in _TOOL_ORDER if tools[name]["available"]]

    if not live:
        status = "unavailable"
    elif len(live) < len(_TOOL_ORDER):
        status = "degraded"
    else:
        status = "available"

    return {
        "enabled": enabled,
        "runtime_supported": supported,
        "status": status,
        # What the user actually gets. Every lane follows this one switch —
        # foreground chat and the background companion alike — so there is no
        # per-lane qualifier to report.
        "effective": enabled and bool(live),
        "tools": tools,
    }


def update_settings(store, payload, **readers) -> dict:
    """Strict booleans: ``{"enabled": "no"}`` is a client bug, not a request to
    switch web ON, so it is rejected rather than coerced.

    Persistence is strict — a swallowed write would tell the user the switch is
    on while the next turn still reads the old value.
    """
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise ValueError("enabled is required")
    if not isinstance(payload["enabled"], bool):
        raise ValueError("enabled must be boolean")
    store.save_web_settings_strict({"enabled": payload["enabled"]})
    return get_settings(store, **readers)


__all__ = ["get_settings", "update_settings", "web_gate"]
