"""Identity write actions (profile patch / nudge / days set) + executor."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime


import db
from core.store import UserStore

from bootstrap import gates as boot_gates
from core import util as core_util
from core import enclave as core_enclave
from core import envelope as core_envelope
from identity import service as identity_service

def _identity_action_text(value, max_chars: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].strip()


def _identity_plain_for_action(store: UserStore, api_key: str | None,
                               runtime_token: str = "") -> tuple[dict | None, str]:
    # Only pass runtime_token when present, so the api_key path keeps the original
    # 2-arg call shape (mocks/monkeypatches that predate the runtime_token param).
    if runtime_token:
        data, err = core_enclave._enclave_get_json_for_gate(
            "/v1/identity/get", api_key, runtime_token=runtime_token)
    else:
        data, err = core_enclave._enclave_get_json_for_gate("/v1/identity/get", api_key)
    if err:
        return None, err
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        return None, "identity_not_initialized"
    identity = data["identity"]
    status = identity.get("decrypt_status")
    if status and status != "ok":
        return None, str(status)
    return identity, ""


def _identity_payload_from_plain(identity: dict) -> dict:
    payload = {
        "agent_name": str(identity.get("agent_name") or "")[:80],
        "self_introduction": str(identity.get("self_introduction") or "")[:1200],
        "dimensions": identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else [],
    }
    for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"}:
            continue
        if identity.get(key):
            payload[key] = str(identity.get(key) or "")[:1200 if key in {"relationship_anchor", "tone_style", "custom_persona_prompt"} else 240]
    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        if isinstance(identity.get(key), list):
            payload[key] = [str(item)[:240] for item in identity[key][:12] if str(item or "").strip()]
    return payload


def _save_identity_action_payload(
    store: UserStore,
    payload: dict,
    *,
    audit: dict,
    event_type: str,
) -> tuple[dict | None, dict | None, str]:
    existing = identity_service._load_identity(store)
    if not existing:
        return None, None, "identity_not_initialized"
    if not existing.get("relationship_started_at"):
        return None, None, "identity_relationship_anchor_missing"
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=existing.get("id") or None,
    )
    if envelope is None:
        return None, None, err

    now = core_util._now_iso()
    identity = {
        "v": 1,
        "id": envelope.get("id") or existing.get("id") or uuid.uuid4().hex,
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        # replaced_at is the P5 concurrency baseline: it is stamped ONLY by
        # full-card writes (identity.init / identity.replace). Partial
        # mutations (profile_patch / dimension_nudge, both routed through
        # this helper) must carry it forward untouched, not drop it — this
        # dict is an explicit field list, not a copy of `existing`, so it
        # must be listed here or the raw blob overwrite in _save_identity
        # would silently erase it.
        "replaced_at": existing.get("replaced_at", ""),
        "relationship_started_at": existing.get("relationship_started_at", ""),
        "relationship_anchor_source": existing.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": existing.get("relationship_anchor_evidence", ""),
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity)
    boot_gates._log_bootstrap_event(store, event_type, success=True)
    change = identity_service._append_identity_change(store, audit)
    return identity, change, ""


def _identity_profile_patch(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
) -> tuple[dict, list[dict], int]:
    patch = action.get("patch") if isinstance(action.get("patch"), dict) else {}
    for key in identity_service._IDENTITY_PROFILE_FIELDS:
        if key in action and key not in patch:
            patch[key] = action[key]
    if not patch:
        return {"status": "error", "error": "patch_required", "action": "identity.profile_patch"}, [], 400

    from identity import card_policy
    ok, err = card_policy.validate_profile_patch(patch)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 400

    plain, err = _identity_plain_for_action(store, api_key, runtime_token=runtime_token)
    if plain is None:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409

    payload = _identity_payload_from_plain(plain)
    changed: list[str] = []
    audit_old = ""
    audit_new = ""

    if "agent_name" in patch:
        new_name = _identity_action_text(patch.get("agent_name"), 80).strip(" `\"'“”‘’。，,.;；:：!！?？")
        if not new_name:
            return {"status": "error", "error": "agent_name_empty", "action": "identity.profile_patch"}, [], 400
        if new_name.lower() in identity_service._IDENTITY_RUNTIME_LABELS:
            # Same error code as card_policy.validate_profile_patch (raw pre-check above) —
            # unify so punctuation-wrapped runtime labels ("`hermes`") that slip past the raw
            # pre-check still fail with the identical code once normalized here.
            return {"status": "error", "error": "agent_name_is_runtime_label", "action": "identity.profile_patch"}, [], 400
        old_name = str(payload.get("agent_name") or "")
        if new_name != old_name:
            payload["agent_name"] = new_name
            changed.append("agent_name")
            audit_old = old_name
            audit_new = new_name

    if "self_introduction" in patch:
        intro = str(patch.get("self_introduction") or "").strip()[:1200]
        if not intro:
            return {"status": "error", "error": "self_introduction_empty", "action": "identity.profile_patch"}, [], 400
        old_intro = str(payload.get("self_introduction") or "")
        if intro != old_intro:
            payload["self_introduction"] = intro
            changed.append("self_introduction")
            if not audit_old and not audit_new:
                audit_old = old_intro[:120]
                audit_new = intro[:120]

    for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"} or key not in patch:
            continue
        max_len = 1200 if key in {"relationship_anchor", "tone_style", "custom_persona_prompt"} else 240
        new_value = _identity_action_text(patch.get(key), max_len)
        old_value = str(payload.get(key) or "")
        if new_value != old_value:
            if new_value:
                payload[key] = new_value
            else:
                payload.pop(key, None)
            changed.append(key)
            if not audit_old and not audit_new:
                audit_old = old_value[:120]
                audit_new = new_value[:120]

    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        if key not in patch:
            continue
        raw_list = patch.get(key)
        if isinstance(raw_list, str):
            raw_list = [raw_list]
        if not isinstance(raw_list, list):
            return {"status": "error", "error": f"{key}_must_be_list", "action": "identity.profile_patch"}, [], 400
        values = [_identity_action_text(item, 240) for item in raw_list[:12]]
        values = [item for item in values if item]
        old_values = payload.get(key) if isinstance(payload.get(key), list) else []
        if values != old_values:
            if values:
                payload[key] = values
            else:
                payload.pop(key, None)
            changed.append(key)
            if not audit_old and not audit_new:
                audit_old = ", ".join(old_values)[:120]
                audit_new = ", ".join(values)[:120]

    if not changed:
        return {
            "status": "ok",
            "action": "identity.profile_patch",
            "changed_fields": [],
            "noop": True,
        }, [], 200

    reason = _identity_action_text(
        action.get("reason") or f"Identity profile updated: {', '.join(changed)}.",
        500,
    )
    identity, change, err = _save_identity_action_payload(
        store,
        payload,
        audit={
            "action": "profile_patch",
            "dimension": "profile",
            "old_value": audit_old,
            "new_value": audit_new,
            "reason": reason,
        },
        event_type="identity_action_profile_patch",
    )
    if identity is None:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409

    effect = {
        "type": "identity_updated",
        "action": "identity.profile_patch",
        "fields": changed,
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.profile_patch",
        "changed_fields": changed,
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": identity_service._live_days_with_user(identity, store=store),
        },
        "change": change or {},
    }, [effect], 200


def _identity_dimension_nudge(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
) -> tuple[dict, list[dict], int]:
    dimension_name = _identity_action_text(action.get("dimension") or action.get("dimension_name"), 80)
    if not dimension_name:
        return {"status": "error", "error": "dimension_required", "action": "identity.dimension_nudge"}, [], 400
    try:
        delta = int(action.get("delta"))
    except Exception:
        return {"status": "error", "error": "delta_required", "action": "identity.dimension_nudge"}, [], 400

    plain, err = _identity_plain_for_action(store, api_key, runtime_token=runtime_token)
    if plain is None:
        return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409

    payload = _identity_payload_from_plain(plain)
    dims = list(payload.get("dimensions") or [])
    matched = None
    for dim in dims:
        if isinstance(dim, dict) and str(dim.get("name") or "").strip().lower() == dimension_name.lower():
            matched = dim
            break
    if matched is None:
        return {"status": "error", "error": "dimension_not_found", "action": "identity.dimension_nudge"}, [], 404
    try:
        old_value = int(matched.get("value", 0))
    except Exception:
        old_value = 0
    new_value = old_value + delta
    from identity import card_policy
    ok, err = card_policy.validate_dimension_nudge(dimension_name, new_value)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 400
    if new_value == old_value:
        return {
            "status": "ok",
            "action": "identity.dimension_nudge",
            "changed_fields": [],
            "noop": True,
        }, [], 200
    matched["value"] = new_value
    reason = _identity_action_text(action.get("reason") or f"{dimension_name} adjusted by {delta:+d}.", 500)
    if reason:
        matched["last_nudge_reason"] = reason
    payload["dimensions"] = dims

    identity, change, err = _save_identity_action_payload(
        store,
        payload,
        audit={
            "action": "nudge",
            "dimension": dimension_name,
            "old_value": old_value,
            "new_value": new_value,
            "delta": delta,
            "reason": reason,
        },
        event_type="identity_action_dimension_nudge",
    )
    if identity is None:
        return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409
    effect = {
        "type": "identity_updated",
        "action": "identity.dimension_nudge",
        "fields": ["dimensions"],
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.dimension_nudge",
        "changed_fields": ["dimensions"],
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": identity_service._live_days_with_user(identity, store=store),
        },
        "change": change or {},
    }, [effect], 200


def _identity_relationship_days_set(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    try:
        days = int(action.get("days_with_user"))
    except Exception:
        return {"status": "error", "error": "days_with_user_required", "action": "identity.relationship_days_set"}, [], 400
    if days < 0:
        return {"status": "error", "error": "days_with_user_must_be_non_negative", "action": "identity.relationship_days_set"}, [], 400
    existing = identity_service._load_identity(store)
    if not existing:
        return {"status": "error", "error": "identity_not_initialized", "action": "identity.relationship_days_set"}, [], 409
    old_days = identity_service._live_days_with_user(existing, store=store)
    identity = dict(existing)
    identity["updated_at"] = core_util._now_iso()
    identity["relationship_started_at"] = identity_service._anchor_from_days(days)
    identity["relationship_anchor_source"] = "user_calibrated"
    evidence = _identity_action_text(action.get("relationship_anchor_evidence") or action.get("reason") or "", 500)
    if evidence:
        identity["relationship_anchor_evidence"] = evidence
    identity_service._save_identity(store, identity)
    boot_gates._log_bootstrap_event(store, "identity_action_relationship_days_set", success=True)
    change = identity_service._append_identity_change(store, {
        "action": "relationship_days",
        "dimension": "relationship_days",
        "old_value": old_days,
        "new_value": days,
        "delta": days - old_days,
        "reason": evidence or "Relationship day count recalibrated.",
    })
    effect = {
        "type": "identity_updated",
        "action": "identity.relationship_days_set",
        "fields": ["days_with_user"],
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.relationship_days_set",
        "changed_fields": ["days_with_user"],
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": days,
        },
        "change": change or {},
    }, [effect], 200


def _replace_relationship_anchor(action: dict) -> dict:
    """B2: build a relationship anchor from an identity.replace action ONLY when it carries
    an explicit relationship time (translating days_with_user → an ISO start date). The
    service layer applies the legality guard (valid date + evidence); an empty dict here
    means 'preserve the existing anchor'."""
    evidence = str(action.get("relationship_anchor_evidence") or "").strip()
    started = str(action.get("relationship_started_at") or "").strip()
    if not started:
        days = action.get("days_with_user")
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            return {}
        from datetime import date, timedelta
        started = (date.today() - timedelta(days=days)).isoformat()
    if not started or not evidence:
        return {}
    return {
        "relationship_started_at": started,
        "relationship_anchor_source": "genesis_resident_distill",
        "relationship_anchor_evidence": evidence,
    }


def _identity_replace_action(
    store: UserStore, api_key: str | None, action: dict, *, runtime_token: str = ""
) -> tuple[dict, list[dict], int]:
    """Full-card identity replace, server-build (reuses genesis
    ``replace_identity_preserving_anchor`` — server builds the shared envelope, agent sends
    plaintext). Used by the VPS resident-distill path when the agent locally re-derives
    identity from a persona doc. The relationship anchor is preserved unless the action
    carries an explicit relationship time (B2, see ``_replace_relationship_anchor``).

    HIGH-RISK (full overwrite) — gated so it is NOT a normal agent action (Codex P1):
      * server-build only: payload must NOT carry an `envelope`;
      * must run inside a resident-distill job context: `source=genesis_resident_distill`
        + `job_id` + `reason`, and that job must belong to the caller and be a live resident
        job (status=processing, resident_consumer_id set).
    """
    if action.get("envelope") is not None:
        return {"status": "error", "error": "envelope_not_allowed", "action": "identity.replace"}, [], 400
    source = str(action.get("source") or "").strip()
    job_id = str(action.get("job_id") or "").strip()
    reason = str(action.get("reason") or "").strip()
    if source != "genesis_resident_distill" or not job_id or not reason:
        return {"status": "error", "error": "identity_replace_requires_resident_distill_context",
                "action": "identity.replace"}, [], 403
    import db  # lazy — avoid import cycle
    job = db.genesis_get_job(store.user_id, job_id)
    if not job or job.get("status") != "processing" or not str(job.get("resident_consumer_id") or "").strip():
        return {"status": "error", "error": "not_a_live_resident_distill_job",
                "action": "identity.replace"}, [], 403
    identity_payload = action.get("identity")
    if not isinstance(identity_payload, dict) or not identity_payload:
        return {"status": "error", "error": "identity_required", "action": "identity.replace"}, [], 400
    from identity import card_policy
    ok, err = card_policy.validate_full_identity_card(identity_payload)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.replace"}, [], 400
    # P5 optimistic concurrency (Task 5): the resident consumer snapshots the outer
    # replaced_at baseline at job-creation time (Task 4's base_identity_replaced_at) and
    # forwards it here. If ANOTHER full init/replace has moved replaced_at since then, this
    # job's derive is stale (built off an outdated card) and must not clobber the newer one.
    # Omitted or "" (every existing caller, and legacy jobs pre-dating the baseline) skips the
    # check entirely — back-compat, not a security gate.
    base_identity_replaced_at = str(action.get("base_identity_replaced_at") or "").strip()
    if base_identity_replaced_at:
        current_identity = identity_service._load_identity(store)
        current_replaced_at = str((current_identity or {}).get("replaced_at") or "")
        if base_identity_replaced_at != current_replaced_at:
            return {"status": "error", "error": "identity_base_stale", "action": "identity.replace"}, [], 409
    # Cloud plaintext role-card uploads use the shared service helper as an
    # upsert. Resident identity.replace remains replace-only: its create path is
    # owned by the resident consumer protocol and must not leak through here.
    if not identity_service._load_identity(store):
        return {"status": "error", "error": "identity_not_initialized",
                "action": "identity.replace"}, [], 409
    from genesis import service as genesis_service  # lazy — avoid import cycle
    result = genesis_service.replace_identity_preserving_anchor(
        store, {"identity": identity_payload, "relationship_anchor": _replace_relationship_anchor(action)}
    )
    if result != "updated":
        status = 409 if result in ("identity_not_initialized", "identity_update_empty", "not_provided") else 400
        return {"status": "error", "error": result, "action": "identity.replace"}, [], status
    return {"status": "ok", "action": "identity.replace", "job_id": job_id}, [], 200


def _execute_identity_action(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
) -> tuple[dict, list[dict], int]:
    if not isinstance(action, dict):
        return {"status": "error", "error": "action_must_be_object"}, [], 400
    action_type = str(action.get("type") or action.get("action") or "").strip()
    if action_type == "identity.profile_patch":
        return _identity_profile_patch(store, api_key, action, runtime_token=runtime_token)
    if action_type == "identity.dimension_nudge":
        return _identity_dimension_nudge(store, api_key, action, runtime_token=runtime_token)
    if action_type == "identity.relationship_days_set":
        return _identity_relationship_days_set(store, action)
    if action_type == "identity.replace":
        return _identity_replace_action(store, api_key, action, runtime_token=runtime_token)
    return {
        "status": "error",
        "error": "unsupported_identity_action",
        "action": action_type,
        "supported": [
            "identity.profile_patch",
            "identity.dimension_nudge",
            "identity.relationship_days_set",
            "identity.replace",
        ],
    }, [], 400


def _execute_identity_actions(
    store: UserStore,
    api_key: str | None,
    actions: list[dict],
    *,
    runtime_token: str = "",
) -> tuple[dict, int]:
    if not isinstance(actions, list) or not actions:
        return {"status": "error", "error": "actions_required", "results": [], "effects": []}, 400
    results: list[dict] = []
    effects: list[dict] = []
    for action in actions[:10]:
        result, action_effects, status = _execute_identity_action(
            store,
            api_key,
            action,
            runtime_token=runtime_token,
        )
        results.append(result)
        effects.extend(action_effects)
        if status >= 400:
            return {
                "status": "error",
                "error": result.get("error", "identity_action_failed"),
                "results": results,
                "effects": effects,
            }, status
    return {"status": "ok", "results": results, "effects": effects}, 200
