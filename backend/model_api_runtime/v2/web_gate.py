"""Which web tools this turn must NOT offer. Pure functions, no IO, no imports
beyond the standard library.

Seven call sites share this module: three lanes on the offer side (chat, wake,
subagent), the three dispatchers on the execute side, and the subagent
allowlist. Writing the rule seven times is exactly how the two halves of a gate
drift apart, so all of them go through here.

Nothing in this module touches the database or the settings store — the caller
resolves those and passes plain values in. That keeps it inside the V2
dependency-direction rule and makes every branch unit-testable without Postgres.
"""

from __future__ import annotations

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})

# Only the foreground chat turn may reach the network. Background turns
# (wake / scheduled / heartbeat / manual_wake / screen_watch, and any subagent
# spawned from them) stay closed even when the user's toggle is ON: searching
# there adds model rounds, tokens and latency, and is an outbound data flow the
# user never triggered. A settings-page switch labelled "web search" should not
# quietly authorise the background companion to go online.
#
# This is the only place a lane name is written down. Call sites pass their real
# `lane` variable rather than a literal, so a typo cannot silently reclassify a
# turn — and any lane not listed here fails closed by construction, which makes
# a future lane safe by default.
FOREGROUND_LANES = frozenset({"chat"})


def disabled_web_tools(*, user_enabled: bool, lane: str | None,
                       search_halted: bool, fetch_halted: bool) -> frozenset[str]:
    """The web tool names to withhold from this turn.

    Feeding the result into ``run_tool_loop(disabled_tool_names=...)`` makes the
    tools absent from the turn catalog, therefore absent from every round's
    request, therefore absent from ``offered_names`` — un-offered *and*
    un-executable, i.e. "this tool does not exist for this user this turn".
    """
    if not user_enabled or lane not in FOREGROUND_LANES:
        return WEB_TOOL_NAMES
    withheld = set()
    if search_halted:
        withheld.add("web_search")
    if fetch_halted:
        withheld.add("web_fetch")
    return frozenset(withheld)


def resolve_user_enabled(web_tools_enabled, user_id: str) -> bool:
    """Read the per-user preference through the injected callable, fail closed.

    A missing callable (``TurnDeps.web_tools_enabled`` defaults to ``None``
    because worker.py never imports ``hosted``), a raising callable, or a
    non-bool return all mean "disabled".

    ``value is True`` and never ``bool(value)``: ``bool("no")`` is True, so a
    corrupt or mistyped setting would otherwise read as web access switched ON.
    """
    if web_tools_enabled is None:
        return False
    try:
        value = web_tools_enabled(user_id)
    except Exception:  # noqa: BLE001 — a settings read must never fail the turn
        return False
    return value is True
