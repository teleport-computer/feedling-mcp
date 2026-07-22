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


# Profile fields the model may pass at the top level, as a shorthand for putting
# them inside `patch`. agent_name belongs here: without it a rename request could
# only land in self_introduction, so the displayed name went stale while the agent
# reported success. Values are passed through — card_policy owns the real rules
# (non-empty, not a runtime label like "claude").
_TOP_LEVEL_PROFILE_FIELDS = ("agent_name", "self_introduction", "signature")


def merge_patch_fields(params) -> dict:
    """Fold top-level profile params into an explicit ``patch`` object.

    ``patch`` used to win outright, which silently discarded any top-level field
    sent alongside it — ``{"agent_name": "老6", "patch": {"self_introduction": …}}``
    reached the server with the rename stripped out, reproducing the exact bug this
    capability was widened to fix. Merging both sides fixes that. On the SAME key
    the explicit ``patch`` still wins, preserving the pre-change reading.

    Deliberately a pure normalization that never rejects. tool_schema's validator
    calls it, and that validator ALSO gates replay of already-persisted effects
    (serve_worker validates a decrypted effect through it). A new rejection rule
    there would re-interpret payloads enqueued by a pre-upgrade worker as invalid,
    and a validation failure becomes a plain RuntimeError, which the outbox treats
    as retryable — so a legal-when-written effect would retry forever instead of
    applying. Rejections belong in ``patch`` below, where retryable=False maps to a
    terminal discard.
    """
    params = params or {}
    explicit = params.get("patch")
    explicit = dict(explicit) if isinstance(explicit, dict) else {}
    top_level = {k: params[k] for k in _TOP_LEVEL_PROFILE_FIELDS if k in params}
    return {**top_level, **explicit}


def patch(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    raw_patch = params.get("patch")
    if raw_patch is not None and not isinstance(raw_patch, dict):
        # Fail closed. The model-facing schema already refuses this, but the
        # capability is callable without the validator; coercing it to {} would
        # apply the top-level fields and report success for a partly malformed
        # call. Deterministic, so retryable=False -> terminal discard, not retry.
        return err(errors.INVALID, "identity_patch: 'patch' must be an object",
                   retryable=False)
    patch_fields = merge_patch_fields(params)
    payload = {"action": {"type": "identity.profile_patch", "patch": patch_fields}}
    body, status = identity_core.run_actions(store, payload, api_key=api_key,
                                             runtime_token=runtime_token or "")
    return _norm(body, status, default_msg="identity patch unavailable")
