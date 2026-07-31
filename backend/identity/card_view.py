"""Plaintext identity-card view assembly — ONE implementation, two callers.

The identity card is a v1 E2E envelope: the store hands back ciphertext and
somebody has to turn a decrypted inner blob into the field shape readers expect.
Two places do that today:

  * the enclave's decrypt-and-serve route (``enclave/routes/identity.py``),
    which decrypts in-process with the content secret key, and
  * the V2 identity capability (``capabilities/identity.py``), which has no key
    and decrypts through the enclave's ``/v1/envelope/decrypt``.

They must produce the SAME shape. When this assembly was hand-copied instead of
shared, the copies drifted and the enclave forwarded only 9 of the 13 profile
fields, so every profile_patch silently erased the other 4 — the user-authored
``custom_persona_prompt`` among them (see card_policy.PROFILE_STRING_FIELDS).
This module exists so that class of drift takes a test failure, not a user's
persona.

Pure functions over plain dicts: no DB, no HTTP, no framework. That is what lets
the enclave (which must stay dependency-thin) import it, the same way it already
imports ``card_policy``.
"""

from __future__ import annotations

from identity import card_policy


def envelope_base(identity: dict) -> dict:
    """The fields readable WITHOUT decrypting — envelope metadata only.

    ``replaced_at`` is the P5 concurrency baseline stamped only by a full
    init/replace. It rides on the outer envelope (not inside the ciphertext)
    precisely so it stays available before/without a decrypt, which is how the
    resident consumer refreshes its baseline after an ``identity_base_stale``
    conflict. Older cards predate it, so it is added only when truthy — never as
    an empty key.
    """
    base = {
        "v": int(identity.get("v", 0)),
        "created_at": identity.get("created_at"),
        "updated_at": identity.get("updated_at"),
    }
    if identity.get("replaced_at"):
        base["replaced_at"] = identity.get("replaced_at")
    return base


def local_only_view(base: dict) -> dict:
    """The card the user has opted the agent OUT of reading.

    Not an error: the envelope metadata is still returned so a caller can tell
    "you may not read this" apart from "this failed", and no decrypt is spent.
    """
    return {
        **base,
        "visibility": "local_only",
        "decrypt_status": "local_only_agent_cannot_read",
    }


def plaintext_view(base: dict, inner: dict, identity: dict, *, days_with_user: int) -> dict:
    """Assemble the decrypted card.

    ``days_with_user`` is passed in rather than computed here: the two callers
    derive it differently (the backend has a UserStore and can apply the
    memory-garden anchor repair; the enclave only has the envelope's anchor), and
    silently picking one of those behaviors for both would change a live counter.

    The profile fields are driven off ``card_policy``'s canonical list, not a
    hand-written one. They feed the read-modify-write merge in
    ``identity.profile_patch`` / ``dimension_nudge``, which rebuilds the card from
    THIS view and re-encrypts it — so a field missing here is not merely hidden,
    it is ERASED on the next partial update.
    """
    view = {
        **base,
        "agent_name": inner.get("agent_name"),
        "self_introduction": inner.get("self_introduction"),
        "dimensions": inner.get("dimensions", []),
        "days_with_user": days_with_user,
        "category": inner.get("category", ""),
        "signature": inner.get("signature", []),
        "visibility": identity.get("visibility", "shared"),
        "decrypt_status": "ok",
    }
    # Additive: only present, non-empty values, so the shape stays stable for
    # older cards that predate a field (no empty keys invented for them).
    for key in card_policy.PROFILE_FIELDS:
        if key in view:
            continue  # already set unconditionally above
        if inner.get(key):
            view[key] = inner.get(key)
    return view
