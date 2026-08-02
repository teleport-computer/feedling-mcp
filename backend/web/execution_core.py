"""Framework-neutral execution gate for the V1 web capability.

This is the SECURITY floor. V1 model tool authorization is static — the model is
told once which tools it may call and then remembers/guesses commands — so the
authorization decision cannot live in the prompt or the tool list. It lives HERE,
at the execution endpoint, and is re-checked on every single search/fetch:

    1. the user's own switch (``enabled``), re-read from the store every call
    2. the operator kill switch (``web_halted()``), per-tool

Only when both gates pass do we make the one real network-touching call into
``capabilities.web``. Any failure to positively confirm "enabled AND not halted"
fails CLOSED: we return an error and the network call count stays at zero. A
swallowed exception while reading a switch must never be read as permission.

Routes stay thin (CONTRIBUTING.md §1); everything decidable lives here so it can
be unit-tested without a database or the network. Same shape as
``settings_core``: pure functions the route invokes through ``run_db``.
"""

from __future__ import annotations

from capabilities import errors, web
from capabilities.types import CapabilityResult, err
from model_api_runtime.v2 import kill_switch


def _user_enabled(store) -> bool:
    """The user's saved preference, re-read every call. Fail CLOSED: if the
    switch is unreadable for any reason we treat web as OFF, never ON — an
    unreadable switch must not become an open door to the network."""
    try:
        return store.load_web_settings().get("enabled") is True
    except Exception:  # noqa: BLE001 — any read failure means "not positively enabled"
        return False


def _blocked(store, *, tool_index: int) -> CapabilityResult | None:
    """Shared authorization gate for both tools. Returns an error result to send
    back instead of running the tool, or ``None`` when the caller may proceed.

    Order matters: check the user's own switch first, then the operator halt.
    ``tool_index`` picks this tool's bit out of ``web_halted()``'s
    ``(search_halted, fetch_halted)`` tuple. ``web_halted`` already fails closed
    to ``True`` on any DB trouble and never raises."""
    # 1) user switch — re-read every call, fail closed.
    if not _user_enabled(store):
        return err(
            errors.DISABLED,
            "web access is turned off for this account",
            retryable=False,
        )
    # 2) operator kill switch — this specific tool.
    if kill_switch.web_halted()[tool_index]:
        return err(
            errors.UNAVAILABLE,
            "web access is temporarily unavailable",
            retryable=True,
        )
    return None


def run_search(store, params) -> CapabilityResult:
    blocked = _blocked(store, tool_index=0)
    if blocked is not None:
        return blocked
    return web.search(store, params=params)


def run_fetch(store, params) -> CapabilityResult:
    blocked = _blocked(store, tool_index=1)
    if blocked is not None:
        return blocked
    return web.fetch(store, params=params)


__all__ = ["run_search", "run_fetch"]
