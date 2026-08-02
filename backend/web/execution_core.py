"""Framework-neutral execution gate for the V1 web capability.

This is the SECURITY floor. V1 model tool authorization is static — the model is
told once which tools it may call and then remembers/guesses commands — so the
authorization decision cannot live in the prompt or the tool list. It lives HERE,
at the execution endpoint, and is re-checked on every single search/fetch:

    1. the user's own switch (``enabled``), re-read from the store every call
    2. the operator kill switch (``web_halted()``), per-tool
    3. a per-user rate limit (this batch) — the abuse ceiling

Only when all gates pass do we make the one real network-touching call into
``capabilities.web``. Any failure to positively confirm "enabled AND not halted
AND under budget" fails CLOSED: we return an error and the network call count
stays at zero. A swallowed exception while reading a switch must never be read
as permission.

Why a rate limit here at all: V1 reuses the V2 web *engine* (search + fetch) but
NOT the V2 tool_loop's outer budgets (``max_tool_calls_per_turn`` etc.). The V1
endpoint is stateless — there is no "turn" to count against — so without a floor
here a model that loops on search, or anyone holding an API key, could drive
unbounded outbound requests and use the backend as a free web proxy. The limit
is the floor that V1 otherwise lacks.

Routes stay thin (CONTRIBUTING.md §1); everything decidable lives here so it can
be unit-tested without a database or the network. Same shape as
``settings_core``: pure functions the route invokes through ``run_db``.
"""

from __future__ import annotations

import json
import os

from core import ratelimit
from capabilities import errors, web
from capabilities.types import CapabilityResult, err, ok
from model_api_runtime.v2 import kill_switch

# Per-user outbound web budget. search + fetch share ONE bucket per user, so the
# ceiling is on *total* network-touching web calls, not per-tool — that is what
# stops a loop of cheap searches or a key used as a free proxy. Default 20 calls
# per rolling 60s window; override with FEEDLING_WEB_RATE="<count>/<seconds>".
_DEFAULT_WEB_RATE: tuple[int, float] = (20, 60.0)
_WEB_RATE_ENV = "FEEDLING_WEB_RATE"

# NOTE: per-worker process memory (see core.ratelimit). Under gunicorn -w N the
# real ceiling is ≈ N × the configured limit — abuse damping, not exact quota.
# A model/consumer for one user pins to one worker per connection, so in the
# common case a single user's calls land on one worker and the limit bites as
# configured; the N× slack only appears when the same user spreads calls across
# workers. Good enough for an abuse floor; not a billing meter.
_web_limiter = ratelimit.SlidingWindowLimiter()

# Total character budget for the packed search result set. Each result item is
# already per-field capped by ``errors.cap_data`` (strings <= 2000, lists <= 50),
# but nothing bounded the AGGREGATE — 50 items could serialize to ~100 KB. V1
# returns this JSON straight to the model without V2's downstream 2000-char cut,
# so the aggregate ceiling has to live here. Items are packed whole until the
# budget is spent (never a raw string-cut of the JSON, which would emit invalid
# JSON); a ``truncated`` flag reports when items were dropped.
_DEFAULT_SEARCH_CHAR_BUDGET = 6000
_SEARCH_CHAR_BUDGET_ENV = "FEEDLING_WEB_SEARCH_CHAR_BUDGET"


def _user_enabled(store) -> bool:
    """The user's saved preference, re-read every call. Fail CLOSED: if the
    switch is unreadable for any reason we treat web as OFF, never ON — an
    unreadable switch must not become an open door to the network."""
    try:
        return store.load_web_settings().get("enabled") is True
    except Exception:  # noqa: BLE001 — any read failure means "not positively enabled"
        return False


def _rate_key(store) -> str:
    """Per-user bucket key. Missing/blank user_id collapses to one shared bucket
    — that only over-restricts (all unknowns share a budget), never opens the
    door wider, which is the safe direction for an abuse floor."""
    return getattr(store, "user_id", "") or "<unknown-user>"


def _rate_limited(store) -> CapabilityResult | None:
    """Charge one unit against the user's shared web budget. Returns an error
    result when over budget (nothing spent on the network), else ``None``.

    This is the LAST gate on purpose: it runs only after the user switch and the
    operator halt have already said "yes", so we never spend budget on a call we
    were going to reject anyway."""
    limit, window = ratelimit.rate_from_env(_WEB_RATE_ENV, _DEFAULT_WEB_RATE)
    allowed, retry_after = _web_limiter.allow(_rate_key(store), limit, window)
    if allowed:
        return None
    return err(
        errors.RATE_LIMITED,
        f"web rate limit exceeded; retry in ~{int(retry_after) + 1}s",
        retryable=True,
    )


def _blocked(store, *, tool_index: int) -> CapabilityResult | None:
    """Shared authorization gate for both tools. Returns an error result to send
    back instead of running the tool, or ``None`` when the caller may proceed.

    Order matters: user's own switch first, then the operator halt, then the
    per-user rate limit. ``tool_index`` picks this tool's bit out of
    ``web_halted()``'s ``(search_halted, fetch_halted)`` tuple. ``web_halted``
    already fails closed to ``True`` on any DB trouble and never raises."""
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
    # 3) per-user rate limit — the abuse ceiling, charged only once we're past
    #    the two authorization gates above.
    limited = _rate_limited(store)
    if limited is not None:
        return limited
    return None


def _budget_search_results(result: CapabilityResult) -> CapabilityResult:
    """Bound the AGGREGATE size of a search result set by packing whole items
    until a total-character budget is spent, then flagging ``truncated``.

    Passes non-search results through unchanged (identity preserved) so this is
    a no-op for anything without a ``results`` list. Never string-cuts the JSON:
    it drops surplus items whole, so the payload is always valid JSON."""
    if not result.ok:
        return result
    results = result.data.get("results")
    if not isinstance(results, list):
        return result  # nothing to budget; keep the exact object we were handed

    budget = _search_char_budget()
    kept: list = []
    used = 0
    for item in results:
        piece = len(json.dumps(item, ensure_ascii=False, default=str))
        # Always keep the first item even if it alone blows the budget: it is
        # already per-field capped upstream, and returning *something* beats an
        # empty result set that reads as "the web found nothing".
        if kept and used + piece > budget:
            break
        kept.append(item)
        used += piece

    truncated = len(kept) < len(results)
    # ``truncated`` goes first: if any downstream stage hard-cuts the serialized
    # dict, a trailing flag would be the first thing dropped — the same ordering
    # rule ``web.fetch`` already follows for its own truncation flag.
    data: dict = {"truncated": truncated}
    for key, value in result.data.items():
        data[key] = kept if key == "results" else value
    return ok(data=data, trace=result.trace, warnings=result.warnings)


def _search_char_budget() -> int:
    raw = os.environ.get(_SEARCH_CHAR_BUDGET_ENV, "").strip()
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return _DEFAULT_SEARCH_CHAR_BUDGET


def run_search(store, params) -> CapabilityResult:
    blocked = _blocked(store, tool_index=0)
    if blocked is not None:
        return blocked
    return _budget_search_results(web.search(store, params=params))


def run_fetch(store, params) -> CapabilityResult:
    blocked = _blocked(store, tool_index=1)
    if blocked is not None:
        return blocked
    return web.fetch(store, params=params)


__all__ = ["run_search", "run_fetch"]
