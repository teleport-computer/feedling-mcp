"""Identity HTTP surface: /v1/identity/*."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore
from flask import Blueprint, Response
import threading

from accounts import auth
from bootstrap import gates as boot_gates
from identity import actions as identity_actions_mod
from identity import service as identity_service
from memory import service as memory_service

bp = Blueprint("identity", __name__)

@bp.route("/v1/identity/actions", methods=["POST"])
def identity_actions():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    actions = payload.get("actions")
    if actions is None and isinstance(payload.get("action"), dict):
        actions = [payload["action"]]
    elif actions is None and (payload.get("type") or payload.get("action")):
        actions = [payload]
    if not isinstance(actions, list):
        return jsonify({"error": "actions required"}), 400
    body, status = identity_actions_mod._execute_identity_actions(store, api_key, actions)
    return jsonify(body), status



@bp.route("/v1/identity/get", methods=["GET"])
def identity_get():
    store = auth.require_user()
    data = identity_service._load_identity(store)
    if data is None:
        return jsonify({"identity": None})
    # Inject the live-computed days alongside the envelope. iOS decrypts the
    # envelope locally, but it never sees the anchor itself — it just reads
    # this top-level field. Same convention as the enclave proxy.
    enriched = dict(data)
    enriched["days_with_user"] = identity_service._live_days_with_user(data, store=store)
    return jsonify({"identity": enriched})


@bp.route("/v1/identity/init", methods=["POST"])
def identity_init():
    """Initialize the identity card as a v1 envelope. body_ct wraps
    {agent_name, self_introduction, dimensions} serialized as JSON.
    Plaintext metadata: id, created_at, updated_at. See DESIGN_E2E.md §3.2.

    Bootstrap gate: requires memory_count >= the per-age floor (see
    memory_service._memory_floor_for_days). Identity must be DERIVED from memories per
    skill protocol; writing identity without depth proportional to the
    relationship age means the Agent skipped the depth pass.
    See boot_gates._gate_bootstrap_for_identity_init.
    """
    store = auth.require_user()
    existing = identity_service._load_identity(store)
    if existing is not None:
        return jsonify({"error": "already_initialized", "identity": existing}), 409
    gated = boot_gates._gate_bootstrap_for_identity_init(store)
    if gated is not None:
        return gated

    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    # Defense-in-depth: refuse envelopes whose claimed owner_user_id doesn't
    # match the authenticated caller. The enclave's AEAD AAD check would also
    # catch this later (decrypt fails on owner_user_id ≠ authorized_user_id),
    # but rejecting at write time keeps the on-disk state consistent with the
    # auth boundary. memory_add already does this — bring identity inline.
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    # days_with_user is mandatory at init — Agent must compute and submit it.
    # We persist it as relationship_started_at (a fixed anchor) so subsequent
    # reads can compute the live count without going through the Agent again.
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return jsonify({"error": "days_with_user (non-negative int) required at init"}), 400
    relationship_anchor_evidence = str(payload.get("relationship_anchor_evidence") or "").strip()
    if len(relationship_anchor_evidence) < 8:
        return jsonify({
            "error": "relationship_anchor_evidence required at init",
            "required": (
                "Pass a concrete source for the earliest relationship date "
                "(transcript/session/file/message pointer or user-confirmed fresh start). "
                "Do not guess days_with_user."
            ),
        }), 400
    earliest_memory_date = identity_service._earliest_memory_date(store)
    if earliest_memory_date:
        computed_days = max(0, (datetime.now().date() - earliest_memory_date).days)
        if abs(computed_days - days_with_user) > 1:
            return jsonify({
                "error": "days_with_user_mismatch",
                "days_with_user": days_with_user,
                "computed_from_earliest_memory": computed_days,
                "earliest_memory_date": earliest_memory_date.isoformat(),
                "required": (
                    "Recompute days_with_user from the earliest memory's occurred_at "
                    "before calling feedling_identity_init."
                ),
            }), 400

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
    return jsonify({"status": "created", "identity": identity, "v": 1}), 201


@bp.route("/v1/identity/replace", methods=["POST"])
def identity_replace():
    """Phase C part 3: replace the identity card in place. Used by MCP
    to implement `identity.nudge` on v1 cards — MCP fetches the
    decrypted card from the enclave, mutates one dimension, re-wraps,
    POSTs here. Same envelope shape as `/v1/identity/init` but does NOT
    409 when a card already exists. Preserves the original `created_at`
    so the card's history tracking is intact.
    """
    store = auth.require_user()
    existing = identity_service._load_identity(store)
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return jsonify({"error": "envelope required for replace; use /v1/identity/init for plaintext"}), 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    # Defense-in-depth: same owner check identity_init now does. See comment
    # there for why.
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    created_at = existing.get("created_at") if existing else now
    # Preserve the existing relationship anchor unless the caller explicitly
    # passes a new days_with_user. nudge / dimension rewrite must NOT bump the
    # anchor; only an intentional calibration ever should.
    days_with_user = payload.get("days_with_user")
    if days_with_user is not None:
        if not isinstance(days_with_user, int) or days_with_user < 0:
            return jsonify({"error": "days_with_user must be a non-negative int"}), 400
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
        return jsonify({"error": "no relationship anchor on file; call /v1/identity/init first"}), 400

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
    return jsonify({"status": "replaced", "identity": identity, "v": 1})


@bp.route("/v1/identity/changes", methods=["GET"])
def identity_changes():
    """Read the identity-change audit log. Used by iOS to render the
    "最近的变化" feed in IdentityView and to detect new events for
    local push notifications.

    Query params:
      since: ISO timestamp; only return entries with ts > since
      limit: cap on number of entries returned (default 50, max 200)

    Response: {"changes": [...], "total": N}. Entries are newest-first.
    Each entry has {id, ts, action, [dimension, old_value, new_value,
    delta, reason]}. Server doesn't decrypt anything — these fields are
    plaintext metadata supplied by the writing path (MCP tools call
    /v1/identity/replace with an `audit` payload).
    """
    store = auth.require_user()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    since = request.args.get("since", "")
    changes = identity_service._load_identity_changes(store, since=since, limit=limit)
    return jsonify({"changes": changes, "total": len(changes)})


@bp.route("/v1/identity/relationship_anchor", methods=["POST"])
def identity_relationship_anchor():
    """Update only the relationship anchor (days_with_user), without touching
    the encrypted identity envelope.

    Used by the bootstrap calibration step: Agent estimates days, sends the
    initial card, asks the user "we've known each other ~N days, right?",
    and on correction calls this endpoint to fix the anchor — no envelope
    re-encryption needed.
    """
    store = auth.require_user()
    existing = identity_service._load_identity(store)
    if existing is None:
        return jsonify({"error": "identity not initialized"}), 404

    payload = request.get_json(silent=True) or {}
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return jsonify({"error": "days_with_user (non-negative int) required"}), 400

    existing["relationship_started_at"] = identity_service._anchor_from_days(days_with_user)
    existing["relationship_anchor_source"] = "user_calibrated"
    existing["updated_at"] = datetime.now().isoformat()
    identity_service._save_identity(store, existing)
    print(f"[identity:{store.user_id}] anchor updated → {existing['relationship_started_at']} (days={days_with_user})")
    return jsonify({"status": "updated", "relationship_started_at": existing["relationship_started_at"]})


# Note: /v1/identity/nudge no longer exists on the backend. Identity cards
# are v1 ciphertext; mutation happens inside the enclave via MCP's
# decrypt-mutate-rewrap flow (see backend/mcp_server.py `_identity_nudge_v1`).



@bp.route("/v1/identity/verify", methods=["GET"])
def identity_verify():
    """Check identity card state. Returns shape + sanity of plaintext
    metadata; the dimensions / agent_name themselves are inside the
    envelope and were validated at envelope-build time
    (mcp_server.py _check_identity_quality)."""
    store = auth.require_user()
    identity = identity_service._load_identity(store)
    if not identity:
        return jsonify({
            "written": False,
            "passing": False,
            "suggestions": [
                "Identity not yet written. Call feedling_identity_init "
                "after Pass 4 (memory verification with user)."
            ],
        })

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

    return jsonify({
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
    })
