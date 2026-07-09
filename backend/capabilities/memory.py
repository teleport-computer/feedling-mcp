"""Memory capabilities — thin facade over backend/memory/memory_core.py."""
from __future__ import annotations

from typing import Optional

from memory import memory_core
import memory_readside_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _post_enclave_for(runtime_token: Optional[str]):
    def _post(api_key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key, candidates, operation=operation, payload=payload,
            runtime_token=runtime_token)
    return _post


def _norm(body, status, *, default_msg: str) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=errors.cap_data(data))
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def index(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = memory_core.index(store, api_key, params or {},
                                     post_enclave=_post_enclave_for(runtime_token))
    return _norm(body, status, default_msg="memory index unavailable")


def search(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """Keyword/grep search over memory cards. Thin alias of index() that REQUIRES
    a non-empty query — the enclave read-side does the actual matching."""
    params = params or {}
    query = str(params.get("query") or "").strip()
    if not query:
        return err(errors.INVALID, "query is required for memory_search", retryable=False)
    body, status = memory_core.index(store, api_key, {**params, "query": query},
                                     post_enclave=_post_enclave_for(runtime_token))
    return _norm(body, status, default_msg="memory search unavailable")


def fetch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = memory_core.fetch(store, api_key, params or {},
                                     post_enclave=_post_enclave_for(runtime_token))
    return _norm(body, status, default_msg="memory fetch unavailable")


def write(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = memory_core.actions(store, api_key, params or {})
    return _norm(body, status, default_msg="memory write unavailable")
