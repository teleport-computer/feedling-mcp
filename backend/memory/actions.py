"""Memory write actions (add / patch / retype / delete) + executor."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore

from bootstrap import gates as boot_gates
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import util as core_util
from identity import service as identity_service
from memory import service as memory_service

def _memory_action_text(value, max_chars: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].strip()


def _memory_plain_from_envelope(moment: dict, api_key: str | None) -> tuple[dict | None, str]:
    if moment.get("visibility") == "local_only":
        return None, "memory_local_only_agent_cannot_read"
    try:
        raw = core_enclave._decrypt_envelope_via_enclave(moment, api_key, purpose="memory_action")
        inner = json.loads(raw.decode("utf-8"))
        if not isinstance(inner, dict):
            return None, "memory_plaintext_not_object"
        return inner, ""
    except Exception as e:
        return None, f"memory_decrypt_failed:{type(e).__name__}:{str(e)[:180]}"


def _memory_inner_from_action(data: dict) -> dict:
    inner = {
        "title": _memory_action_text(data.get("title"), 180),
        "description": str(data.get("description") or "").strip()[:2000],
        "type": str(data.get("type") or "fact").strip().lower(),
    }
    if data.get("source"):
        inner["source"] = _memory_action_text(data.get("source"), 160)
    if data.get("her_quote"):
        inner["her_quote"] = str(data.get("her_quote") or "").strip()[:1000]
    if data.get("context"):
        inner["context"] = str(data.get("context") or "").strip()[:1000]
    if data.get("linked_dimension"):
        inner["linked_dimension"] = str(data.get("linked_dimension") or "").strip()[:160]
    if data.get("quoted_in_chat") is not None:
        try:
            inner["quoted_in_chat"] = max(0, int(data.get("quoted_in_chat")))
        except Exception:
            pass
    return inner


def _memory_validate_write(
    store: UserStore,
    moments: list,
    *,
    mem_type: str,
    anchor_ids: list,
    memory_id: str = "",
    enforce_reflection_cap: bool = True,
) -> tuple[bool, dict | None]:
    if mem_type not in memory_service.MEMORY_TYPES:
        return False, {"error": "type_invalid", "got": mem_type, "allowed": list(memory_service.MEMORY_TYPES)}
    if mem_type in ("insight", "reflection"):
        minimum = 1 if mem_type == "insight" else 2
        if not isinstance(anchor_ids, list) or len(anchor_ids) < minimum:
            return False, {
                "error": f"{mem_type}_requires_anchor",
                "min_anchors": minimum,
                "required": f"{mem_type} requires ≥{minimum} anchor_memory_ids.",
            }
        if memory_id and memory_id in anchor_ids:
            return False, {
                "error": "anchor_self_reference",
                "required": "A memory cannot anchor itself.",
            }
        ok, err = memory_service._validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return False, err
        if mem_type == "reflection" and enforce_reflection_cap:
            ok, err = memory_service._reflection_time_cap_ok(moments, identity_service._relationship_age_days(store))
            if not ok:
                return False, err
    return True, None


def _build_memory_envelope_for_store(
    store: UserStore,
    inner: dict,
    *,
    item_id: str | None = None,
) -> tuple[dict | None, str]:
    return core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=item_id,
    )


def _memory_record_from_envelope(store: UserStore, envelope: dict, *, existing: dict | None = None) -> dict:
    now = core_util._now_iso()
    moment = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else f"mom_{uuid.uuid4().hex[:12]}"),
        "type": str(envelope.get("type") or (existing or {}).get("type") or "fact"),
        "occurred_at": str(envelope.get("occurred_at") or (existing or {}).get("occurred_at") or now),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
        "source": str(envelope.get("source") or (existing or {}).get("source") or "live_conversation"),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    anchor_ids = envelope.get("anchor_memory_ids") or []
    if anchor_ids:
        moment["anchor_memory_ids"] = list(anchor_ids)
    return moment


def _memory_action_effect(action: str, memory_id: str, fields: list[str] | None = None) -> dict:
    return {
        "type": "memory_updated" if action not in {"memory.add", "memory.add_correction"} else "memory_added",
        "action": action,
        "memory_id": memory_id,
        "fields": fields or [],
    }


def _memory_add_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    raw = action.get("memory") if isinstance(action.get("memory"), dict) else action
    mem_type = str(raw.get("type") or "fact").strip().lower()
    title = _memory_action_text(raw.get("title"), 180)
    description = str(raw.get("description") or "").strip()[:2000]
    if not title:
        return {"status": "error", "error": "title_required", "action": "memory.add"}, [], 400
    if not description and mem_type not in {"quote", "event"}:
        return {"status": "error", "error": "description_required", "action": "memory.add"}, [], 400
    anchor_ids = raw.get("anchor_memory_ids") or action.get("anchor_memory_ids") or []
    if not isinstance(anchor_ids, list):
        return {"status": "error", "error": "anchor_memory_ids_must_be_list", "action": "memory.add"}, [], 400
    moments = memory_service._load_moments(store)
    ok, err = _memory_validate_write(store, moments, mem_type=mem_type, anchor_ids=anchor_ids)
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.add"}, [], 400

    inner = _memory_inner_from_action({**raw, "type": mem_type, "title": title, "description": description})
    envelope, env_err = _build_memory_envelope_for_store(store, inner)
    if envelope is None:
        return {"status": "error", "error": env_err, "action": "memory.add"}, [], 409
    envelope["type"] = mem_type
    envelope["occurred_at"] = _memory_action_text(raw.get("occurred_at") or core_util._now_iso(), 80)
    envelope["source"] = _memory_action_text(raw.get("source") or action.get("source") or "model_api_capture", 80)
    if anchor_ids:
        envelope["anchor_memory_ids"] = list(anchor_ids)
    moment = _memory_record_from_envelope(store, envelope)
    moments.append(moment)
    memory_service._save_moments(store, moments)
    boot_gates._log_bootstrap_event(store, "memory_action_added_v1", success=True)
    change = memory_service._append_memory_change(store, {
        "action": "add",
        "memory_id": moment["id"],
        "type": mem_type,
        "reason": _memory_action_text(action.get("reason") or "Memory added from chat/capture.", 500),
        "capture_mode": action.get("capture_mode") or "",
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    effect = _memory_action_effect(str(action.get("type") or "memory.add"), moment["id"], ["created"])
    return {
        "status": "ok",
        "action": str(action.get("type") or "memory.add"),
        "memory": {"id": moment["id"], "type": mem_type, "occurred_at": moment["occurred_at"]},
        "change": change,
    }, [effect], 201


def _memory_content_patch_action(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    patch = action.get("patch") if isinstance(action.get("patch"), dict) else {}
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.content_patch"}, [], 400
    if not patch:
        return {"status": "error", "error": "patch_required", "action": "memory.content_patch"}, [], 400

    moments = memory_service._load_moments(store)
    idx = next((i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == memory_id), None)
    if idx is None:
        return {"status": "error", "error": "not_found", "action": "memory.content_patch"}, [], 404
    existing = moments[idx]
    if existing.get("owner_user_id") != store.user_id:
        return {"status": "error", "error": "not_owned", "action": "memory.content_patch"}, [], 403
    inner, err = _memory_plain_from_envelope(existing, api_key)
    if inner is None:
        return {"status": "error", "error": err, "action": "memory.content_patch"}, [], 409

    merged = dict(inner)
    changed: list[str] = []
    for key, max_len in (
        ("title", 180),
        ("description", 2000),
        ("her_quote", 1000),
        ("context", 1000),
        ("linked_dimension", 160),
    ):
        if key in patch:
            new_val = str(patch.get(key) or "").strip()[:max_len]
            if new_val:
                merged[key] = new_val
            else:
                merged.pop(key, None)
            if merged.get(key, "") != inner.get(key, ""):
                changed.append(key)

    mem_type = str(patch.get("type") or existing.get("type") or merged.get("type") or "fact").strip().lower()
    if mem_type != existing.get("type"):
        changed.append("type")
    merged["type"] = mem_type
    occurred_at = _memory_action_text(patch.get("occurred_at") or existing.get("occurred_at") or core_util._now_iso(), 80)
    if occurred_at != existing.get("occurred_at"):
        changed.append("occurred_at")
    source = _memory_action_text(patch.get("source") or existing.get("source") or "live_conversation", 80)
    anchor_ids = patch.get("anchor_memory_ids", existing.get("anchor_memory_ids") or [])
    if not isinstance(anchor_ids, list):
        return {"status": "error", "error": "anchor_memory_ids_must_be_list", "action": "memory.content_patch"}, [], 400
    if anchor_ids != (existing.get("anchor_memory_ids") or []):
        changed.append("anchor_memory_ids")

    ok, validation_err = _memory_validate_write(
        store,
        moments,
        mem_type=mem_type,
        anchor_ids=anchor_ids,
        memory_id=memory_id,
        enforce_reflection_cap=False,
    )
    if not ok:
        return {"status": "error", **(validation_err or {}), "action": "memory.content_patch"}, [], 400
    if not changed:
        return {"status": "ok", "action": "memory.content_patch", "changed_fields": [], "noop": True}, [], 200

    envelope, env_err = _build_memory_envelope_for_store(store, _memory_inner_from_action(merged), item_id=memory_id)
    if envelope is None:
        return {"status": "error", "error": env_err, "action": "memory.content_patch"}, [], 409
    envelope["type"] = mem_type
    envelope["occurred_at"] = occurred_at
    envelope["source"] = source
    if anchor_ids:
        envelope["anchor_memory_ids"] = list(anchor_ids)
    updated = _memory_record_from_envelope(store, envelope, existing=existing)
    moments[idx] = updated
    memory_service._save_moments(store, moments)
    boot_gates._log_bootstrap_event(store, "memory_action_patched_v1", success=True)
    change = memory_service._append_memory_change(store, {
        "action": "content_patch",
        "memory_id": memory_id,
        "old_type": existing.get("type", ""),
        "new_type": mem_type,
        "fields": changed,
        "reason": _memory_action_text(action.get("reason") or "Memory updated from chat.", 500),
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    return {
        "status": "ok",
        "action": "memory.content_patch",
        "changed_fields": changed,
        "memory": {"id": memory_id, "type": mem_type, "occurred_at": occurred_at},
        "change": change,
    }, [_memory_action_effect("memory.content_patch", memory_id, changed)], 200


def _memory_retype_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    new_type = str(action.get("new_type") or action.get("memory_type") or action.get("to_type") or "").strip().lower()
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.retype"}, [], 400
    if new_type not in memory_service.MEMORY_TYPES:
        return {"status": "error", "error": "type_invalid", "got": new_type, "allowed": list(memory_service.MEMORY_TYPES), "action": "memory.retype"}, [], 400
    moments = memory_service._load_moments(store)
    idx = next((i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == memory_id), None)
    if idx is None:
        return {"status": "error", "error": "not_found", "action": "memory.retype"}, [], 404
    target = dict(moments[idx])
    anchor_ids = action.get("anchor_memory_ids") or []
    ok, err = _memory_validate_write(store, moments, mem_type=new_type, anchor_ids=anchor_ids, memory_id=memory_id, enforce_reflection_cap=False)
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.retype"}, [], 400
    old_type = target.get("type", "")
    if old_type == new_type and anchor_ids == (target.get("anchor_memory_ids") or []):
        return {"status": "ok", "action": "memory.retype", "changed_fields": [], "noop": True}, [], 200
    target["type"] = new_type
    target["updated_at"] = core_util._now_iso()
    target["retyped_at"] = target["updated_at"]
    if anchor_ids:
        target["anchor_memory_ids"] = list(anchor_ids)
    else:
        target.pop("anchor_memory_ids", None)
    moments[idx] = target
    memory_service._save_moments(store, moments)
    change = memory_service._append_memory_change(store, {
        "action": "retype",
        "memory_id": memory_id,
        "old_type": old_type,
        "new_type": new_type,
        "fields": ["type", "anchor_memory_ids"],
        "reason": _memory_action_text(action.get("reason") or "Memory type updated.", 500),
        "anchor_memory_ids": anchor_ids,
    })
    return {
        "status": "ok",
        "action": "memory.retype",
        "changed_fields": ["type", "anchor_memory_ids"],
        "memory": {"id": memory_id, "type": new_type},
        "change": change,
    }, [_memory_action_effect("memory.retype", memory_id, ["type", "anchor_memory_ids"])], 200


def _memory_delete_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.delete"}, [], 400
    moments = memory_service._load_moments(store)
    target = next((m for m in moments if isinstance(m, dict) and m.get("id") == memory_id), None)
    if target is None:
        return {"status": "error", "error": "not_found", "action": "memory.delete"}, [], 404
    new_moments = [m for m in moments if not (isinstance(m, dict) and m.get("id") == memory_id)]
    memory_service._save_moments(store, new_moments)
    change = memory_service._append_memory_change(store, {
        "action": "delete",
        "memory_id": memory_id,
        "type": target.get("type", ""),
        "reason": _memory_action_text(action.get("reason") or "Memory deleted from chat.", 500),
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
    })
    effect = {"type": "memory_deleted", "action": "memory.delete", "memory_id": memory_id, "fields": ["deleted"]}
    return {"status": "ok", "action": "memory.delete", "memory": {"id": memory_id}, "change": change}, [effect], 200


def _execute_memory_action(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    if not isinstance(action, dict):
        return {"status": "error", "error": "action_must_be_object"}, [], 400
    action_type = str(action.get("type") or action.get("action") or "").strip()
    if action_type in {"memory.add", "memory.add_correction"}:
        return _memory_add_action(store, action)
    if action_type == "memory.content_patch":
        return _memory_content_patch_action(store, api_key, action)
    if action_type == "memory.retype":
        return _memory_retype_action(store, action)
    if action_type == "memory.delete":
        return _memory_delete_action(store, action)
    return {
        "status": "error",
        "error": "unsupported_memory_action",
        "action": action_type,
        "supported": ["memory.add", "memory.add_correction", "memory.content_patch", "memory.retype", "memory.delete"],
    }, [], 400


def _execute_memory_actions(store: UserStore, api_key: str | None, actions: list[dict]) -> tuple[dict, int]:
    if not isinstance(actions, list) or not actions:
        return {"status": "error", "error": "actions_required", "results": [], "effects": []}, 400
    results: list[dict] = []
    effects: list[dict] = []
    for action in actions[:20]:
        result, action_effects, status = _execute_memory_action(store, api_key, action)
        results.append(result)
        effects.extend(action_effects)
        if status >= 400:
            return {
                "status": "error",
                "error": result.get("error", "memory_action_failed"),
                "results": results,
                "effects": effects,
            }, status
    return {"status": "ok", "results": results, "effects": effects}, 200
