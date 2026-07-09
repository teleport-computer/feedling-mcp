"""Identity capabilities — facade over backend/identity/identity_core.py."""
from __future__ import annotations

from identity import identity_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(body, status, *, default_msg) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=errors.cap_data(data))
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def get(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    body, status = identity_core.get_identity(store)
    return _norm(body, status, default_msg="identity unavailable")


def patch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    patch_fields = params.get("patch")
    if patch_fields is None:
        patch_fields = {k: params[k] for k in ("self_introduction", "signature") if k in params}
    payload = {"action": {"type": "identity.profile_patch", "patch": patch_fields}}
    body, status = identity_core.run_actions(store, payload, api_key=api_key,
                                             runtime_token=runtime_token or "")
    return _norm(body, status, default_msg="identity patch unavailable")
