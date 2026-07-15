"""Framework-neutral identity operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``/v1/identity/*`` route bodies so both the Flask
adapter (``identity.routes``) and the native FastAPI router
(``identity.routes_asgi``) share one implementation and return byte-identical
responses.

E2E boundary (unchanged): identity cards are v1 E2E envelopes. The server NEVER
decrypts them. Reads (``get`` / ``verify`` / ``changes``) are plain store ops.
``init`` / ``replace`` persist the ciphertext envelope the caller supplies (or,
on the plaintext ``identity`` init path, build one via the SAME
``core.envelope._build_shared_envelope_for_store`` path memory.add uses — the
enclave still owns the plaintext). ``actions`` forwards the caller's credential
(api key OR runtime token) to the enclave (via ``identity.actions``), which owns
decrypt / re-encrypt. These functions take already-parsed params + the store (+
the credential where forwarded) as arguments — they never read ``flask.request``
— so no new server-side plaintext is ever introduced here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from bootstrap import gates as boot_gates
from core import envelope as core_envelope
from identity import actions as identity_actions_mod
from identity import service as identity_service


def get_identity(store) -> tuple[dict, int]:
    data = identity_service._load_identity(store)
    if data is None:
        return {"identity": None}, 200
    # Inject the live-computed days alongside the envelope. iOS decrypts the
    # envelope locally, but it never sees the anchor itself — it just reads
    # this top-level field. Same convention as the enclave proxy.
    enriched = dict(data)
    enriched["days_with_user"] = identity_service._live_days_with_user(data, store=store)
    return {"identity": enriched}, 200


def run_actions(
    store, payload: dict, *, api_key: str | None, runtime_token: str
) -> tuple[dict, int]:
    actions = payload.get("actions")
    if actions is None and isinstance(payload.get("action"), dict):
        actions = [payload["action"]]
    elif actions is None and (payload.get("type") or payload.get("action")):
        actions = [payload]
    if not isinstance(actions, list):
        return {"error": "actions required"}, 400
    return identity_actions_mod._execute_identity_actions(
        store,
        api_key,
        actions,
        runtime_token=runtime_token,
    )


def init_identity(store, payload: dict) -> tuple[dict, int]:
    """Initialize the identity card as a v1 envelope. See identity.routes for
    the full contract; this is a byte-identical relocation of that body."""
    existing = identity_service._load_identity(store)
    if existing is not None:
        return {"error": "already_initialized", "identity": existing}, 409
    gated = boot_gates._gate_bootstrap_for_identity_init(store)
    if gated is not None:
        # Historically a Flask response; the hook always returns None now, so this
        # branch is dead. Kept to preserve the exact call sequence + ordering.
        return gated

    envelope = payload.get("envelope")
    identity_plain = payload.get("identity")
    # Two ways to init:
    #   (A) pre-built `envelope` — iOS / official client builds it locally.
    #   (B) plaintext `identity` — a route-A agent has no crypto, so the server
    #       builds the envelope here via the same path memory.add and
    #       identity.profile_patch use (_build_shared_envelope_for_store). This
    #       restores the agent-driven init that lived in the now-removed MCP
    #       server (mcpsrv/tools_identity), without regressing the client path.
    if envelope is not None and identity_plain is not None:
        return {"error": "provide either envelope or identity, not both"}, 400
    if envelope is None and identity_plain is None:
        return {"error": "envelope or identity required"}, 400
    if identity_plain is not None:
        if not isinstance(identity_plain, dict):
            return {"error": "identity must be object"}, 400
        from identity import card_policy
        ok, err = card_policy.validate_full_identity_card(identity_plain)
        if not ok:
            return {"error": err}, 400
        inner = identity_actions_mod._identity_payload_from_plain(identity_plain)
        built, build_err = core_envelope._build_shared_envelope_for_store(
            store,
            json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        if built is None:
            # Mirrors memory.add: a build failure is usually missing user/enclave
            # public-key material, not a malformed request -> 409, not 400.
            return {"error": build_err or "identity_envelope_failed"}, 409
        envelope = built
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return {"error": f"envelope missing fields: {missing}"}, 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return {"error": "envelope.visibility must be 'shared' or 'local_only'"}, 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return {"error": "envelope with visibility=shared requires K_enclave"}, 400
    # Defense-in-depth: refuse envelopes whose claimed owner_user_id doesn't
    # match the authenticated caller. The enclave's AEAD AAD check would also
    # catch this later (decrypt fails on owner_user_id ≠ authorized_user_id),
    # but rejecting at write time keeps the on-disk state consistent with the
    # auth boundary. memory_add already does this — bring identity inline.
    if envelope["owner_user_id"] != store.user_id:
        return {"error": "envelope.owner_user_id does not match caller"}, 403

    # days_with_user is mandatory at init — Agent must compute and submit it.
    # We persist it as relationship_started_at (a fixed anchor) so subsequent
    # reads can compute the live count without going through the Agent again.
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return {"error": "days_with_user (non-negative int) required at init"}, 400
    relationship_anchor_evidence = str(payload.get("relationship_anchor_evidence") or "").strip()
    if len(relationship_anchor_evidence) < 8:
        return {
            "error": "relationship_anchor_evidence required at init",
            "required": (
                "Pass a concrete source for the earliest relationship date "
                "(transcript/session/file/message pointer or user-confirmed fresh start). "
                "Do not guess days_with_user."
            ),
        }, 400
    earliest_memory_date = identity_service._earliest_memory_date(store)
    if earliest_memory_date:
        computed_days = max(0, (datetime.now().date() - earliest_memory_date).days)
        if abs(computed_days - days_with_user) > 1:
            return {
                "error": "days_with_user_mismatch",
                "days_with_user": days_with_user,
                "computed_from_earliest_memory": computed_days,
                "earliest_memory_date": earliest_memory_date.isoformat(),
                "required": (
                    "Recompute days_with_user from the earliest memory's occurred_at "
                    "before calling feedling_identity_init."
                ),
            }, 400

    now = datetime.now().isoformat()
    identity = {
        "v": 1,
        "id": envelope.get("id") or uuid.uuid4().hex,
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": now,
        "updated_at": now,
        "replaced_at": now,
        "relationship_started_at": identity_service._anchor_from_days(days_with_user, store=store, prefer_memory=True),
        "relationship_anchor_source": "earliest_memory" if earliest_memory_date else "days_with_user",
        "relationship_anchor_evidence": relationship_anchor_evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity)
    boot_gates._log_bootstrap_event(store, "identity_written_v1", success=True)
    # Audit log: identity_init is always an "init" marker. The caller may
    # pass an `audit.reason` if it wants a custom one ("first day with this
    # user"); otherwise default to a neutral bootstrap-complete note.
    audit_payload = payload.get("audit") or {}
    identity_service._append_identity_change(store, {
        "action": "init",
        "reason": audit_payload.get("reason", "Identity card written for the first time."),
    })
    print(f"[identity:{store.user_id}] initialized v1 visibility={envelope['visibility']} anchor={identity['relationship_started_at']}")
    return {"status": "created", "identity": identity, "v": 1}, 201


def replace_identity(store, payload: dict) -> tuple[dict, int]:
    """Phase C part 3: replace the identity card in place. Byte-identical
    relocation of the Flask body; see identity.routes for the full contract."""
    existing = identity_service._load_identity(store)
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return {"error": "envelope required for replace; use /v1/identity/init for plaintext"}, 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return {"error": f"envelope missing fields: {missing}"}, 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return {"error": "envelope.visibility must be 'shared' or 'local_only'"}, 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return {"error": "envelope with visibility=shared requires K_enclave"}, 400
    # Defense-in-depth: same owner check identity_init now does. See comment
    # there for why.
    if envelope["owner_user_id"] != store.user_id:
        return {"error": "envelope.owner_user_id does not match caller"}, 403

    created_at = existing.get("created_at") if existing else now
    # Preserve the existing relationship anchor unless the caller explicitly
    # passes a new days_with_user. nudge / dimension rewrite must NOT bump the
    # anchor; only an intentional calibration ever should.
    days_with_user = payload.get("days_with_user")
    if days_with_user is not None:
        if not isinstance(days_with_user, int) or days_with_user < 0:
            return {"error": "days_with_user must be a non-negative int"}, 400
        relationship_started_at = identity_service._anchor_from_days(days_with_user)
        relationship_anchor_source = "user_calibrated"
        relationship_anchor_evidence = (payload.get("relationship_anchor_evidence") or "").strip()
        if not relationship_anchor_evidence and existing:
            relationship_anchor_evidence = existing.get("relationship_anchor_evidence", "")
    elif existing and existing.get("relationship_started_at"):
        relationship_started_at = existing["relationship_started_at"]
        relationship_anchor_source = existing.get("relationship_anchor_source", "")
        relationship_anchor_evidence = existing.get("relationship_anchor_evidence", "")
    else:
        # First-ever write through replace (no prior init). Reject so callers
        # are forced through init's mandatory days_with_user path.
        return {"error": "no relationship anchor on file; call /v1/identity/init first"}, 400

    identity = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else uuid.uuid4().hex),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": created_at,
        "updated_at": now,
        "replaced_at": now,
        "relationship_started_at": relationship_started_at,
        "relationship_anchor_source": relationship_anchor_source,
        "relationship_anchor_evidence": relationship_anchor_evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity)
    boot_gates._log_bootstrap_event(store, "identity_replaced_v1", success=True)
    # Audit log: replace can be a single-dimension nudge (MCP tool passes
    # `audit.action: "nudge"` with dimension/old/new/delta) or a full
    # rewrite (`audit.action: "replace"`). When no audit field, log a
    # generic replace marker — better than dropping the event entirely.
    audit_payload = payload.get("audit") or {}
    identity_service._append_identity_change(store, {
        "action": audit_payload.get("action", "replace"),
        "dimension": audit_payload.get("dimension"),
        "old_value": audit_payload.get("old_value"),
        "new_value": audit_payload.get("new_value"),
        "delta": audit_payload.get("delta"),
        "reason": audit_payload.get("reason", ""),
    })
    print(f"[identity:{store.user_id}] replaced v1 visibility={envelope['visibility']} anchor={relationship_started_at}")
    return {"status": "replaced", "identity": identity, "v": 1}, 200


def list_changes(store, *, limit_raw, since: str) -> tuple[dict, int]:
    """Read the identity-change audit log. ``limit_raw`` is the raw query value
    (defaulting to 50 when absent, matching Flask ``request.args.get``)."""
    try:
        limit = min(int(limit_raw), 200)
    except (TypeError, ValueError):
        return {"error": "invalid limit"}, 400
    changes = identity_service._load_identity_changes(store, since=since, limit=limit)
    return {"changes": changes, "total": len(changes)}, 200


def update_relationship_anchor(store, payload: dict) -> tuple[dict, int]:
    """Update only the relationship anchor (days_with_user), without touching
    the encrypted identity envelope."""
    existing = identity_service._load_identity(store)
    if existing is None:
        return {"error": "identity not initialized"}, 404

    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return {"error": "days_with_user (non-negative int) required"}, 400

    existing["relationship_started_at"] = identity_service._anchor_from_days(days_with_user)
    existing["relationship_anchor_source"] = "user_calibrated"
    existing["updated_at"] = datetime.now().isoformat()
    identity_service._save_identity(store, existing)
    print(f"[identity:{store.user_id}] anchor updated → {existing['relationship_started_at']} (days={days_with_user})")
    return {"status": "updated", "relationship_started_at": existing["relationship_started_at"]}, 200


def verify_identity(store) -> tuple[dict, int]:
    """Check identity card state. Returns shape + sanity of plaintext metadata;
    the dimensions / agent_name themselves are inside the envelope."""
    identity = identity_service._load_identity(store)
    if not identity:
        return {
            "written": False,
            "passing": False,
            "suggestions": [
                "Identity not yet written. Call feedling_identity_init "
                "after Pass 4 (memory verification with user)."
            ],
        }, 200

    issues = []
    suggestions = []

    days_with_user = identity_service._live_days_with_user(identity, store=store)
    if days_with_user < 0:
        issues.append({"type": "days_with_user_negative", "got": days_with_user})
    if days_with_user > 365 * 30:
        issues.append({"type": "days_with_user_implausible", "got": days_with_user})

    relationship_anchored = bool(identity.get("relationship_started_at"))
    if not relationship_anchored:
        issues.append({"type": "no_relationship_anchor"})
        suggestions.append(
            "relationship_started_at is missing. Use "
            "feedling_identity_set_relationship_days to set it."
        )
    relationship_anchor_evidence = str(identity.get("relationship_anchor_evidence") or "").strip()
    if not relationship_anchor_evidence:
        issues.append({"type": "no_relationship_anchor_evidence"})
        suggestions.append(
            "relationship_anchor_evidence is missing. Re-run identity bootstrap "
            "with a concrete transcript/session/file pointer for the earliest date."
        )

    return {
        "written": True,
        "days_with_user": days_with_user,
        "relationship_anchored": relationship_anchored,
        "relationship_anchor_source": identity.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": relationship_anchor_evidence,
        "created_at": identity.get("created_at", ""),
        "updated_at": identity.get("updated_at", ""),
        "issues": issues,
        "suggestions": suggestions,
        "passing": len(issues) == 0,
    }, 200
