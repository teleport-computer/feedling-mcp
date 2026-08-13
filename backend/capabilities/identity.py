"""Identity capabilities — facade over backend/identity/identity_core.py."""
from __future__ import annotations

import json

from core import enclave as core_enclave
from core import envelope as core_envelope
from identity import card_view, identity_core

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
    """Read the identity card AS PLAINTEXT.

    ``identity_core.get_identity`` returns the raw v1 envelope by design — the
    public ``GET /v1/identity/get`` serves iOS, which holds the user key and
    decrypts locally. The model holds no key, so this capability must go through
    the enclave, exactly like every memory readside already does.

    It used to accept ``api_key``/``runtime_token`` and drop both, returning the
    envelope untouched: the agent got ``body_ct`` and no agent_name /
    self_introduction on EVERY call, then truthfully reported that the card was
    unreadable. Ciphertext is never a valid answer here — a failure is returned
    as a failure (see the decrypt except-branch) so the cause stays visible
    instead of being laundered into a "successful" unreadable card.
    """
    body, status = identity_core.get_identity(store)
    if status != 200:
        return _norm(body, status, default_msg="identity unavailable")

    identity = body.get("identity") if isinstance(body, dict) else None
    if not isinstance(identity, dict):
        return ok(data=errors.cap_data(body))  # no card written yet
    if not any(key in identity for key in ("body", "body_b64", "body_ct")):
        # Compatibility with injected/older identity adapters that already
        # return a materialized plaintext view rather than a stored envelope.
        return ok(data=errors.cap_data(body))
    base = card_view.envelope_base(identity)
    if identity.get("visibility") == "local_only":
        return ok(data=errors.cap_data({"identity": card_view.local_only_view(base)}))

    try:
        shape = core_envelope.classify_envelope_shape(identity)
        if shape in ("plaintext_text", "plaintext_binary"):
            raw = core_envelope.read_plaintext_envelope_body(
                identity, owner_user_id=str(getattr(store, "user_id", "") or ""))
        else:
            raw = core_enclave._decrypt_envelope_via_enclave(
                identity, api_key, purpose="identity_get", runtime_token=runtime_token or "")
        inner = json.loads(raw.decode("utf-8"))
        if not isinstance(inner, dict):
            raise ValueError("identity_plaintext_not_object")
    except Exception as e:
        # Retryable: the dominant causes are enclave restarts / transient
        # unavailability, and the alternative (a terminal error) would leave the
        # agent believing it has no persona for the rest of the turn.
        return err(errors.UPSTREAM,
                   errors.cap_text(f"identity_decrypt_failed:{type(e).__name__}:{e}"),
                   retryable=True)

    # days_with_user was computed live by get_identity from the server-side
    # relationship anchor (with the memory-garden repair applied); the value baked
    # into the ciphertext is stale by construction, so the outer one wins.
    view = card_view.plaintext_view(
        base, inner, identity, days_with_user=identity.get("days_with_user", 0))
    return ok(data=errors.cap_data({"identity": view}))


# Profile fields the model may pass at the top level, as a shorthand for putting
# them inside `patch`. agent_name belongs here: without it a rename request could
# only land in self_introduction, so the displayed name went stale while the agent
# reported success. Values are passed through — card_policy owns the real rules
# (non-empty, not a runtime label like "claude").
_TOP_LEVEL_PROFILE_FIELDS = ("agent_name", "self_introduction", "signature", "relationship_days")


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


def rename_pairing_error(params) -> str | None:
    """LIVE-model-call-only pre-enqueue check: a rename (a non-empty
    ``agent_name``) must carry ``self_introduction`` in the SAME call. Returns
    an error code string the model can read and self-correct from, or None.

    Deliberately NOT part of ``merge_patch_fields`` / ``patch`` /
    ``tool_schema.validate_tool_args``: those also gate REPLAY of already-
    persisted effects, where re-rejecting a rename that was legal when written
    would turn it into a terminal discard (see ``merge_patch_fields``'s
    docstring). This runs ONLY on the live model-call path in
    ``v2.executor.dispatch_tool_calls`` — before the effect is enqueued — so the
    model gets a tool error it can fix THIS turn instead of the write silently
    failing at the sink at end-of-turn. The server-side gate in
    ``identity/actions.py`` (``card_policy.validate_rename_pairing``) stays as
    the final, authoritative defense; this only front-runs its message."""
    from identity import card_policy
    merged = merge_patch_fields(params or {})
    ok_, err_ = card_policy.validate_rename_pairing(merged)
    return None if ok_ else err_


def relationship_days_error(params) -> str | None:
    """LIVE-model-call-only pre-enqueue check: if the patch carries
    ``relationship_days``, it must be a valid non-negative int within the
    business cap. Returns a stable error code the model can self-correct from,
    or None.

    Deliberately NOT part of ``merge_patch_fields`` /
    ``tool_schema.validate_tool_args``: those also gate REPLAY of already-
    persisted effects, where re-rejecting a value that was legal when written
    would terminal-discard it. This runs ONLY on the live model-call path in
    ``v2.executor.dispatch_tool_calls`` — before the effect is enqueued — so a
    bad day count fails THIS turn with a fixable tool error instead of being
    enqueued and then 400-ing (or OverflowError-looping) at the sink. The
    server-side gate (``card_policy.validate_profile_patch``) stays as the final
    authority; this only front-runs its message, exactly like
    ``rename_pairing_error``."""
    from identity import card_policy
    merged = merge_patch_fields(params or {})
    if "relationship_days" not in merged:
        return None
    return card_policy.relationship_days_shape_error(merged.get("relationship_days"))


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
    action = {"type": "identity.profile_patch", "patch": patch_fields}
    # FROZEN anchor (item 1): when this capability runs as the identity SINK, the
    # producer (worker._write_tool_effect_payload) has already resolved
    # relationship_days -> an absolute relationship_started_at AT ENQUEUE TIME and
    # threaded it here as a trusted top-level param (stripped from the model args
    # in serve_worker._validate_decrypted_tool_effect, never model-authored). Pass
    # it down the call path as an EXPLICIT keyword arg (trusted_relationship_anchor)
    # — NOT by mutating the action dict — so _identity_profile_patch consumes the
    # FIXED anchor instead of recomputing from today's date on a delayed replay
    # (which would drift the day count and break idempotency). Routing it via the
    # call-path parameter (round-4 / Important 1) is what makes the frozen anchor
    # trusted only when it arrives through THIS sink; the public request path never
    # passes it, so a request body carrying relationship_started_at cannot forge
    # one. Absent on the direct request path — there resolution happens once,
    # inline, from relationship_days in _identity_profile_patch.
    frozen_anchor = params.get("relationship_started_at")
    trusted_anchor = (
        frozen_anchor.strip()
        if isinstance(frozen_anchor, str) and frozen_anchor.strip()
        else None
    )
    payload = {"action": action}
    body, status = identity_core.run_actions(
        store, payload, api_key=api_key, runtime_token=runtime_token or "",
        trusted_relationship_anchor=trusted_anchor)
    return _norm(body, status, default_msg="identity patch unavailable")


def nudge(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """Adjust one existing relationship/personality dimension score by a signed
    delta, routing to the ``identity.dimension_nudge`` action.

    Fail-closed on malformed args, mirroring ``patch``: a missing/blank dimension
    or a non-integer delta is deterministic bad input, so it maps to
    retryable=False (a terminal discard) rather than an infinite retry. Unlike
    ``patch`` there is no persisted-effect-replay concern here — this is a brand
    new capability, so nothing enqueued a dimension_nudge effect before this code
    existed. The per-dimension existence check and the |delta| ≤ 10 cap stay
    server-side in identity/actions.py (dimension_not_found → 404,
    nudge_delta_exceeds_cap → 400), surfaced back through ``_norm``.
    """
    params = params or {}
    dimension = params.get("dimension")
    if not isinstance(dimension, str) or not dimension.strip():
        return err(errors.INVALID, "identity_nudge: 'dimension' must be a non-empty string",
                   retryable=False)
    try:
        delta = int(params.get("delta"))
    except (TypeError, ValueError):
        return err(errors.INVALID, "identity_nudge: 'delta' must be an integer",
                   retryable=False)
    action = {"type": "identity.dimension_nudge", "dimension": dimension, "delta": delta}
    reason = params.get("reason")
    if isinstance(reason, str) and reason.strip():
        action["reason"] = reason
    payload = {"action": action}
    body, status = identity_core.run_actions(store, payload, api_key=api_key,
                                             runtime_token=runtime_token or "")
    return _norm(body, status, default_msg="identity nudge unavailable")
