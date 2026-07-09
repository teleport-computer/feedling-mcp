"""Action-type → capability dispatch table for the V2 executor (Plan C)."""
from __future__ import annotations

from typing import Callable

from capabilities import memory, perception, screen, photo, identity, chat, web
from capabilities import errors
from capabilities.types import CapabilityResult, err


CAPABILITIES: dict[str, Callable[..., CapabilityResult]] = {
    "identity_get": lambda store, **kw: identity.get(store, **kw),
    "identity_patch": lambda store, **kw: identity.patch(store, **kw),
    "memory_index": lambda store, **kw: memory.index(store, **kw),
    "memory_fetch": lambda store, **kw: memory.fetch(store, **kw),
    "memory_write": lambda store, **kw: memory.write(store, **kw),
    "memory_search": lambda store, **kw: memory.search(store, **kw),
    "perception_snapshot": lambda store, **kw: perception.snapshot(store, **kw),
    "perception_trend": lambda store, **kw: perception.trend(store, **kw),
    "perception_history": lambda store, **kw: perception.history(store, **kw),
    "screen_recent": lambda store, **kw: screen.recent(store, **kw),
    "screen_read": lambda store, **kw: screen.read(store, **kw),
    "photo_recent": lambda store, **kw: photo.recent(store, **kw),
    "photo_read": lambda store, **kw: photo.read(store, **kw),
    "chat_image_read": lambda store, **kw: chat.image_read(store, **kw),
    "web_search": lambda store, **kw: web.search(store, **kw),
    "web_fetch": lambda store, **kw: web.fetch(store, **kw),
}

WRITE_ACTIONS = frozenset({"memory_write", "identity_patch"})
READ_ACTIONS = frozenset(set(CAPABILITIES) - WRITE_ACTIONS)


def run_capability(action_type: str, store, *, api_key=None, runtime_token=None,
                   params=None) -> CapabilityResult:
    fn = CAPABILITIES.get(action_type)
    if fn is None:
        return err(errors.INVALID, f"unknown capability: {action_type}", retryable=False)
    return fn(store, api_key=api_key, runtime_token=runtime_token, params=params)
