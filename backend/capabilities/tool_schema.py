"""Tool-schema catalog (Plan C, Task 3 / C1).

Derives one `ToolSpec` per model-facing capability in `capabilities.registry.CAPABILITIES`
(everything except `chat_image_read`, which has no backend route and is never offered to
the model) plus a synthetic `reply` tool that the unified tool loop treats specially
(writes an immediate bubble instead of dispatching through the executor).

Each entry in `PARAMS` mirrors exactly the `params` fields each capability module reads —
see the module docstring/params usage cited per tool below. Do not add fields the
capability code does not consume; the executor will simply ignore them, but claiming they
exist misleads the model into fabricating structured junk.
"""
from __future__ import annotations

from provider_types import ToolSpec
from capabilities import registry

REPLY_TOOL = "reply"

_EXCLUDED = frozenset({"chat_image_read"})

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_NO_ARGS: dict = {"type": "object", "properties": {}}

PARAMS: dict[str, dict] = {
    # -- identity.py --
    # identity.get(store, ...): ignores params entirely.
    "identity_get": _NO_ARGS,
    # identity.patch(store, ...): params.get("patch") (dict), else falls back to
    # top-level self_introduction/signature strings.
    "identity_patch": {
        "type": "object",
        "properties": {
            "patch": {"type": "object"},
            "self_introduction": _STR,
            "signature": _STR,
        },
        "required": [],
    },

    # -- memory.py (backed by memory_core.index/fetch/actions) --
    # memory.index(store, ...): payload passed through to memory_index_core; only
    # "limit" is inspected directly (memory_core.index reads payload.get("limit")).
    "memory_index": {
        "type": "object",
        "properties": {"limit": _INT},
        "required": [],
    },
    # memory.search(store, ...): params.get("query") (required, non-empty) + optional
    # limit (passed through like index).
    "memory_search": {
        "type": "object",
        "properties": {"query": _STR, "limit": _INT},
        "required": ["query"],
    },
    # memory.fetch(store, ...) -> memory_core.fetch: payload.get("ids") must be a
    # list of non-empty strings.
    "memory_fetch": {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": _STR}},
        "required": ["ids"],
    },
    # memory.write(store, ...) -> memory_core.actions: payload.get("actions") (list),
    # with single-action shorthand handled server-side; the tool contract asks for
    # "actions" explicitly.
    "memory_write": {
        "type": "object",
        "properties": {"actions": {"type": "array", "items": {"type": "object"}}},
        "required": ["actions"],
    },

    # -- perception.py (backed by agent/perception_core.py) --
    # perception.snapshot: params.get("signals") (list or csv string).
    "perception_snapshot": {
        "type": "object",
        "properties": {"signals": {"type": "array", "items": _STR}},
        "required": [],
    },
    # perception.trend: params.get("signal"), params.get("field"), params.get("days").
    "perception_trend": {
        "type": "object",
        "properties": {"signal": _STR, "field": _STR, "days": _INT},
        "required": [],
    },
    # perception.history: params.get("signal"), params.get("days").
    "perception_history": {
        "type": "object",
        "properties": {"signal": _STR, "days": _INT},
        "required": [],
    },

    # -- screen.py (backed by screen/screen_read_core.py) --
    # screen.recent: params.get("limit").
    "screen_recent": {
        "type": "object",
        "properties": {"limit": _INT},
        "required": [],
    },
    # screen.read: params.get("frame_id") (defaults to latest frame if absent),
    # params.get("include_image") (bool).
    "screen_read": {
        "type": "object",
        "properties": {"frame_id": _STR, "include_image": _BOOL},
        "required": [],
    },

    # -- photo.py (backed by perception/perception_read_core.py) --
    # photo.recent: params.get("limit").
    "photo_recent": {
        "type": "object",
        "properties": {"limit": _INT},
        "required": [],
    },
    # photo.read: params.get("photo_id") or params.get("id") (required),
    # params.get("include_image") (bool).
    "photo_read": {
        "type": "object",
        "properties": {"photo_id": _STR, "include_image": _BOOL},
        "required": ["photo_id"],
    },

    # -- web.py (keyless facade over model_api_runtime/tools.py) --
    # web.search: params: {"query": str, "limit": int?} — per module docstring.
    "web_search": {
        "type": "object",
        "properties": {"query": _STR, "limit": _INT},
        "required": ["query"],
    },
    # web.fetch: params: {"url": str} — per module docstring.
    "web_fetch": {
        "type": "object",
        "properties": {"url": _STR},
        "required": ["url"],
    },

    # -- wake.py (backed by proactive/scheduled_wake_v2.py) --
    # wake.schedule: params.get("at") (required, ISO-ish time string), optional
    # "tz" and "reason" strings. NOTE: the field is "at", not "when".
    "schedule_wake": {
        "type": "object",
        "properties": {"at": _STR, "tz": _STR, "reason": _STR},
        "required": ["at"],
    },
    # wake.cancel: params.get("wake_id") or params.get("id") (required), optional
    # "reason" string.
    "cancel_wake": {
        "type": "object",
        "properties": {"wake_id": _STR, "reason": _STR},
        "required": ["wake_id"],
    },

    # -- synthetic reply tool --
    REPLY_TOOL: {
        "type": "object",
        "properties": {"text": _STR},
        "required": ["text"],
    },
}

DESCRIPTIONS: dict[str, str] = {
    "identity_get": "Read the persona's current identity/profile fields.",
    "identity_patch": "Update the persona's identity/profile (self_introduction, signature, or an explicit patch object).",
    "memory_index": "List recent memory cards, optionally capped by limit.",
    "memory_search": "Keyword-search memory cards by a required query string.",
    "memory_fetch": "Fetch specific memory cards by their ids.",
    "memory_write": "Write, update, or delete memory cards via a list of actions.",
    "perception_snapshot": "Read the latest perception snapshot for the given signals.",
    "perception_trend": "Read a trend summary for a perception signal over recent days.",
    "perception_history": "Read raw historical values for a perception signal over recent days.",
    "screen_recent": "List recent screen-share frame metadata.",
    "screen_read": "Read (decrypt) a specific screen-share frame, or the latest one if no frame_id is given.",
    "photo_recent": "List recent photos, optionally capped by limit.",
    "photo_read": "Read a specific photo by id, optionally including its decrypted image.",
    "web_search": "Search the public web (keyless DuckDuckGo scrape) for a query.",
    "web_fetch": "Fetch a URL and return its stripped text content.",
    "schedule_wake": "Schedule a future self-wake at a given time, with optional timezone and reason.",
    "cancel_wake": "Cancel a previously scheduled self-wake by its wake_id.",
    REPLY_TOOL: "Send an immediate reply bubble to the user with the given text.",
}


def build_tool_specs() -> list[ToolSpec]:
    specs = []
    for name in registry.CAPABILITIES:
        if name in _EXCLUDED:
            continue
        specs.append(ToolSpec(name=name, description=DESCRIPTIONS[name], parameters=PARAMS[name]))
    specs.append(ToolSpec(name=REPLY_TOOL, description=DESCRIPTIONS[REPLY_TOOL], parameters=PARAMS[REPLY_TOOL]))
    return specs
