"""Memory write actions (add / patch / retype / delete) + executor."""

import hashlib
import json
import re
import uuid


import db
from core.store import UserStore

from bootstrap import gates as boot_gates
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import util as core_util
from identity import service as identity_service
from memory import service as memory_service
from memory.prompts_v1 import normalize_bucket_language

def _memory_action_text(value, max_chars: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].strip()


def _memory_action_occurred_at(raw: dict, envelope: dict | None = None, *, default: str = "") -> str:
    if isinstance(raw, dict) and "occurred_at" in raw:
        return _memory_action_text(raw.get("occurred_at"), 80)
    if isinstance(envelope, dict) and "occurred_at" in envelope:
        return _memory_action_text(envelope.get("occurred_at"), 80)
    return _memory_action_text(default, 80)


def _memory_action_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(1.0, parsed))


def _memory_action_list(value, max_items: int = 8, max_chars: int = 80) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _memory_action_text(item, max_chars)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _memory_supersedes_list(action: dict, *, max_items: int = 20) -> list[str]:
    for key in ("supersedes", "old_id", "target_id", "memory_id"):
        if key not in action:
            continue
        ids = _memory_action_list(action.get(key), max_items=max_items, max_chars=160)
        if ids:
            return ids
    return []


def _memory_default_bucket(value) -> str:
    mem_type = str(value or "").strip().lower()
    if mem_type in {"moment", "quote"}:
        return "我们的关系"
    if mem_type in {"fact", "event"}:
        return "未分类"
    return "未分类"


def _memory_content_from_action(data: dict, summary: str) -> str:
    content = str(data.get("content") or "").strip()[:5000]
    if content:
        return content
    description = str(data.get("description") or summary or "").strip()[:2000]
    quote = str(data.get("verbatim") or data.get("her_quote") or "").strip()[:1000]
    context = str(data.get("context") or "").strip()[:1000]
    parts = [f"记忆: {description or summary}"]
    if quote or context:
        parts.append(f"上下文: {quote or context}")
    else:
        # Card text is user-visible: no "用户"/system labels (and "TA" is the
        # app surface's name for the AI). Subject-free reads naturally.
        parts.append("上下文: 对话中明确提到。")
    follow_up = str(data.get("follow_up") or "").strip()[:1000]
    parts.append(f"使用提示: {follow_up or '自然使用这条记忆，不要机械复述。'}")
    return "\n".join(parts)


def _memory_inheritable_inner_fields(inner: dict | None) -> dict:
    if not isinstance(inner, dict):
        return {}
    fields: dict = {}
    bucket = _memory_action_text(inner.get("bucket") or _memory_default_bucket(inner.get("type")), 80)
    if bucket:
        fields["bucket"] = bucket
    threads = _memory_action_list(inner.get("threads"), max_items=8, max_chars=80)
    if not threads:
        threads = _memory_action_list(inner.get("linked_dimension"), max_items=1, max_chars=80)
    if threads:
        fields["threads"] = threads
    return fields


def _memory_plain_from_envelope(moment: dict, api_key: str | None, runtime_token: str = "") -> tuple[dict | None, str]:
    if moment.get("visibility") == "local_only":
        return None, "memory_local_only_agent_cannot_read"
    try:
        raw = core_enclave._decrypt_envelope_via_enclave(
            moment, api_key, purpose="memory_action", runtime_token=runtime_token)
        inner = json.loads(raw.decode("utf-8"))
        if not isinstance(inner, dict):
            return None, "memory_plaintext_not_object"
        return inner, ""
    except Exception as e:
        return None, f"memory_decrypt_failed:{type(e).__name__}:{str(e)[:180]}"


def _memory_inner_from_action(data: dict) -> dict:
    summary = str(data.get("summary") or data.get("description") or data.get("title") or "").strip()[:2000]
    threads = _memory_action_list(data.get("threads"), max_items=8, max_chars=80)
    if not threads:
        linked = _memory_action_list(data.get("linked_dimension"), max_items=1, max_chars=80)
        threads.extend(linked)
    content = _memory_content_from_action(data, summary)
    bucket = _memory_action_text(data.get("bucket") or _memory_default_bucket(data.get("type")), 80)
    # Backstop: the model still labels a Chinese card with an English common bucket (and
    # vice versa) despite the guidance. Map it back to the card's own language here — the
    # single chokepoint every write path funnels through — so no path can leak an
    # off-language common bucket. Custom buckets pass through unchanged.
    bucket = normalize_bucket_language(bucket, f"{summary}\n{content}")
    return {
        "summary": summary,
        "content": content,
        "bucket": bucket,
        "threads": threads,
    }


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
                "error": "anchor_required",
                "detail": {"mem_type": mem_type},
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


def _memory_validate_prebuilt_envelope(
    store: UserStore,
    moments: list,
    envelope: dict,
    *,
    memory_id: str = "",
    enforce_reflection_cap: bool = True,
) -> tuple[bool, dict | None]:
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [field for field in required if not envelope.get(field)]
    if missing:
        return False, {"error": "envelope_missing_fields", "missing": missing}
    if envelope["visibility"] not in ("shared", "local_only"):
        return False, {"error": "envelope_visibility_invalid", "allowed": ["shared", "local_only"]}
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return False, {"error": "envelope_shared_requires_K_enclave"}
    occurred_at = _memory_action_text(envelope.get("occurred_at"), 80)
    if not occurred_at:
        return False, {
            "error": "occurred_at_required",
            "required": "occurred_at is required as plaintext metadata for memory ordering.",
        }
    if envelope["owner_user_id"] != store.user_id:
        return False, {"error": "not_owned", "required": "envelope.owner_user_id must match caller."}
    mem_type = str(envelope.get("type") or "").strip().lower()
    if not mem_type:
        return False, {
            "error": "type_required",
            "allowed": list(memory_service.MEMORY_TYPES),
            "required": "type is mandatory and must be one of moment/quote/fact/event/insight/reflection.",
        }
    anchor_ids = envelope.get("anchor_memory_ids") or []
    if not isinstance(anchor_ids, list):
        return False, {"error": "anchor_memory_ids_must_be_list"}
    return _memory_validate_write(
        store,
        moments,
        mem_type=mem_type,
        anchor_ids=anchor_ids,
        memory_id=memory_id,
        enforce_reflection_cap=enforce_reflection_cap,
    )


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


def _memory_record_from_prebuilt_envelope(store: UserStore, envelope: dict, *, existing: dict | None = None) -> dict:
    moment = _memory_record_from_envelope(store, envelope, existing=existing)
    moment["type"] = str(envelope.get("type") or "").strip().lower()
    moment.setdefault("status", str(envelope.get("status") or "active"))
    anchor_ids = envelope.get("anchor_memory_ids") or []
    if anchor_ids:
        moment["anchor_memory_ids"] = list(anchor_ids)
    return moment


def _memory_record_from_envelope(store: UserStore, envelope: dict, *, existing: dict | None = None) -> dict:
    now = core_util._now_iso()
    if "occurred_at" in envelope:
        occurred_at = str(envelope.get("occurred_at") or "")
    elif existing and "occurred_at" in existing:
        occurred_at = str(existing.get("occurred_at") or "")
    else:
        occurred_at = now
    moment = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else f"mom_{uuid.uuid4().hex[:12]}"),
        "occurred_at": occurred_at,
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
    for key in (
        "status",
        "importance",
        "pulse",
        "last_referenced_at",
        "superseded_by",
        "is_sensitive",
        "sensitivity_class",
    ):
        if key in envelope:
            moment[key] = envelope[key]
        elif existing and key in existing:
            moment[key] = existing[key]
    if "supersedes" in envelope:
        moment["supersedes"] = list(envelope.get("supersedes") or [])
    elif existing and "supersedes" in existing:
        moment["supersedes"] = list(existing.get("supersedes") or [])
    return moment


def _memory_apply_v1_metadata(envelope: dict, raw: dict, *, source: str, default_status: str = "active") -> None:
    envelope["status"] = default_status
    envelope["importance"] = _memory_action_float(raw.get("importance"), 0.5)
    envelope["pulse"] = _memory_action_float(raw.get("pulse"), 0.3)
    occurred_at = _memory_action_occurred_at(raw, envelope, default=core_util._now_iso())
    last_referenced_at = _memory_action_text(raw.get("last_referenced_at"), 80)
    if not last_referenced_at and occurred_at:
        last_referenced_at = occurred_at
    if last_referenced_at:
        envelope["last_referenced_at"] = last_referenced_at
    if raw.get("is_sensitive") is not None:
        envelope["is_sensitive"] = bool(raw.get("is_sensitive"))
    if raw.get("sensitivity_class"):
        envelope["sensitivity_class"] = _memory_action_text(raw.get("sensitivity_class"), 80)


def _memory_action_effect(action: str, memory_id: str, fields: list[str] | None = None) -> dict:
    return {
        "type": "memory_updated" if action not in {"memory.add", "memory.add_correction"} else "memory_added",
        "action": action,
        "memory_id": memory_id,
        "fields": fields or [],
    }


def _memory_add_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    if isinstance(action.get("envelope"), dict):
        return _memory_add_envelope_action(store, action)

    raw = action.get("memory") if isinstance(action.get("memory"), dict) else action
    mem_type = str(raw.get("type") or "fact").strip().lower()
    summary = str(raw.get("summary") or raw.get("description") or raw.get("title") or "").strip()[:2000]
    title = _memory_action_text(raw.get("title") or summary, 180)
    description = str(raw.get("description") or summary).strip()[:2000]
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
    envelope["occurred_at"] = _memory_action_occurred_at(raw, default=core_util._now_iso())
    envelope["source"] = _memory_action_text(raw.get("source") or action.get("source") or "model_api_capture", 80)
    _memory_apply_v1_metadata(envelope, raw, source=envelope["source"])
    if anchor_ids:
        envelope["anchor_memory_ids"] = list(anchor_ids)
    moment = _memory_record_from_envelope(store, envelope)
    # Re-read + append + save under one memory_lock hold (the load above was for
    # validation only) so a concurrent same-user write can't lost-update.
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        moments.append(moment)
        memory_service._save_moments(store, moments)
    boot_gates._log_bootstrap_event(store, "memory_action_added_v1", success=True)
    change = memory_service._append_memory_change(store, {
        "action": "insert",
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
        "memory": {"id": moment["id"], "type": mem_type, "occurred_at": moment["occurred_at"], "status": moment["status"]},
        "change": change,
    }, [effect], 201


def _memory_add_envelope_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    envelope = dict(action.get("envelope") or {})
    moments = memory_service._load_moments(store)
    ok, err = _memory_validate_prebuilt_envelope(
        store,
        moments,
        envelope,
        memory_id=str(envelope.get("id") or ""),
    )
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.add"}, [], 400

    moment = _memory_record_from_prebuilt_envelope(store, envelope)
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        moments.append(moment)
        memory_service._save_moments(store, moments)
    boot_gates._log_bootstrap_event(store, "memory_action_added_envelope_v1", success=True)
    anchor_ids = envelope.get("anchor_memory_ids") or []
    change = memory_service._append_memory_change(store, {
        "action": "insert",
        "memory_id": moment["id"],
        "type": moment.get("type", ""),
        "reason": _memory_action_text(action.get("reason") or "Memory added from encrypted tool action.", 500),
        "capture_mode": action.get("capture_mode") or "",
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    return {
        "status": "ok",
        "action": "memory.add",
        "memory": {
            "id": moment["id"],
            "type": moment.get("type", ""),
            "occurred_at": moment.get("occurred_at", ""),
            "status": moment.get("status", "active"),
        },
        "change": change,
    }, [_memory_action_effect("memory.add", moment["id"], ["created"])], 201


def _memory_body_hash(moment: dict | None) -> str:
    """Stable CAS token = sha256 of the stored ciphertext. `to_v1_card` never
    looks inside `body_ct`, so this is invariant across reads until a genuine
    re-encrypt — exactly what a read-modify-write needs to detect concurrent edits."""
    return hashlib.sha256(str((moment or {}).get("body_ct") or "").encode("utf-8")).hexdigest()


def _memory_upgrade_apply(
    store: UserStore,
    *,
    memory_id: str,
    envelope: dict,
    old_body_hash: str,
) -> tuple[dict, list[dict], int]:
    """In-place legacy→v1 upgrade writer (migration plan §3 / §5.5).

    Re-reads the single card fresh INSIDE `memory_lock`, CAS-guards on
    `old_body_hash` (skip if the user edited it during the out-of-lock LLM
    derivation), then writes one row via `db.memory_upsert` — NOT
    `_save_moments`/`memory_replace_all`, so a concurrent new card is never
    clobbered. Preserves id/created_at/occurred_at/source; emits clean v1
    (no `type`). The LLM/encryption happens in the caller, never under the lock."""
    from memory import migration as _migration  # local import avoids load-order cycle
    if not _migration.migration_enabled():
        # FEEDLING_MIGRATE_ENABLE off → reject the write itself. Last gate, so even a
        # hand-issued memory.upgrade can't slip a legacy card to v1 while migration is off.
        return {"status": "ok", "action": "memory.upgrade", "skipped": "migration_disabled", "noop": True}, [], 200
    # The card id is part of the AEAD AAD (owner|v|id), so the body was SEALED with
    # this exact id. We must NOT rewrite envelope["id"] after the fact — that desyncs
    # it from the AAD and writes a card that stores fine but can never be decrypted
    # (regression: 92f6849 did this and broke real-deploy readside). The caller must
    # seal with item_id=memory_id; reject a mismatch up front.
    if str((envelope or {}).get("id") or "") != memory_id:
        return {
            "status": "error",
            "error": "envelope_id_mismatch",
            "action": "memory.upgrade",
            "detail": "envelope.id must equal the target memory_id (id is AEAD-bound; seal with item_id=memory_id).",
        }, [], 400
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        existing = next((m for m in moments if isinstance(m, dict) and m.get("id") == memory_id), None)
        if existing is None:
            # Card deleted while we derived — don't resurrect it.
            return {"status": "ok", "action": "memory.upgrade", "skipped": "not_found", "noop": True}, [], 200
        if existing.get("owner_user_id") != store.user_id:
            return {"status": "error", "error": "not_owned", "action": "memory.upgrade"}, [], 403
        if old_body_hash and _memory_body_hash(existing) != old_body_hash:
            # Changed under us during derivation — leave the user's write intact;
            # the migrator re-detects this card by shape next quiet window.
            return {"status": "ok", "action": "memory.upgrade", "skipped": "stale", "noop": True}, [], 200
        envelope = dict(envelope)
        envelope["occurred_at"] = str(existing.get("occurred_at") or envelope.get("occurred_at") or core_util._now_iso())
        envelope["source"] = str(existing.get("source") or envelope.get("source") or "live_conversation")
        for key in ("status", "importance", "pulse", "last_referenced_at", "is_sensitive", "sensitivity_class"):
            if key not in envelope and key in existing:
                envelope[key] = existing[key]
        updated = _memory_record_from_envelope(store, envelope, existing=existing)
        updated.pop("type", None)  # clean v1 inner carries no `type`
        wrote = db.memory_upsert(store.user_id, memory_id, updated.get("occurred_at") or "", updated)
    if not wrote:
        # DB write didn't commit — do NOT report ok, so a migrator never advances
        # state on a phantom success. The card stays legacy and re-migrates later.
        return {"status": "error", "error": "db_write_failed", "action": "memory.upgrade"}, [], 500
    change = memory_service._append_memory_change(store, {
        "action": "upgrade",
        "memory_id": memory_id,
        "reason": "Legacy memory card upgraded to v1 in place.",
    })
    effect = _memory_action_effect("memory.upgrade", memory_id, ["body_ct", "bucket", "threads", "summary"])
    return {
        "status": "ok",
        "action": "memory.upgrade",
        "memory": {"id": memory_id, "occurred_at": updated.get("occurred_at", "")},
        "change": change,
    }, [effect], 200


def _memory_upgrade_action(store: UserStore, api_key: str | None, action: dict) -> tuple[dict, list[dict], int]:
    """Plaintext upgrade entry: agent supplies the derived v1 fields
    (summary/content/bucket/threads); the server seals them (it already holds
    the shared public keys, same as content_patch) and writes in place."""
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.upgrade"}, [], 400
    v1 = action.get("v1") if isinstance(action.get("v1"), dict) else action
    inner = _memory_inner_from_action(v1)
    if not inner.get("summary"):
        return {"status": "error", "error": "summary_required", "action": "memory.upgrade"}, [], 400
    old_body_hash = _memory_action_text(action.get("old_body_hash"), 80)
    envelope, env_err = _build_memory_envelope_for_store(store, inner, item_id=memory_id)
    if envelope is None:
        return {"status": "error", "error": env_err, "action": "memory.upgrade"}, [], 409
    return _memory_upgrade_apply(store, memory_id=memory_id, envelope=envelope, old_body_hash=old_body_hash)


def _memory_upgrade_envelope_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    """Prebuilt-envelope upgrade entry: the consumer (VPS io_cli, stdlib-only
    crypto per D1) already sealed the v1 plaintext; the server just CAS-guards
    and writes the single row in place."""
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.upgrade"}, [], 400
    envelope = dict(action.get("envelope") or {})
    missing = [f for f in ("body_ct", "nonce", "K_user", "visibility", "owner_user_id") if not envelope.get(f)]
    if missing:
        return {"status": "error", "error": "envelope_missing_fields", "missing": missing, "action": "memory.upgrade"}, [], 400
    if envelope["owner_user_id"] != store.user_id:
        return {"status": "error", "error": "not_owned", "action": "memory.upgrade"}, [], 403
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return {"status": "error", "error": "envelope_shared_requires_K_enclave", "action": "memory.upgrade"}, [], 400
    old_body_hash = _memory_action_text(action.get("old_body_hash"), 80)
    return _memory_upgrade_apply(store, memory_id=memory_id, envelope=envelope, old_body_hash=old_body_hash)


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
    if target.get("owner_user_id") != store.user_id:
        return {"status": "error", "error": "not_owned", "action": "memory.retype"}, [], 403
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
    # Re-read + re-find + replace-by-id + save under one memory_lock hold so a
    # concurrent same-user write can't lost-update.
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        fresh_idx = next(
            (i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == memory_id),
            None,
        )
        if fresh_idx is None:
            return {"status": "error", "error": "not_found", "action": "memory.retype"}, [], 404
        moments[fresh_idx] = target
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


def _memory_supersede_action(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
) -> tuple[dict, list[dict], int]:
    if isinstance(action.get("envelope"), dict):
        return _memory_supersede_envelope_action(store, action)

    raw = action.get("memory") if isinstance(action.get("memory"), dict) else {}
    old_ids = _memory_supersedes_list(action)
    if not old_ids:
        return {"status": "error", "error": "supersedes_required", "action": "memory.supersede"}, [], 400

    mem_type = str(raw.get("type") or "fact").strip().lower()
    summary = str(raw.get("summary") or raw.get("description") or raw.get("title") or "").strip()[:2000]
    title = _memory_action_text(raw.get("title") or summary, 180)
    description = str(raw.get("description") or summary).strip()[:2000]
    if not title:
        return {"status": "error", "error": "title_required", "action": "memory.supersede"}, [], 400
    if not description and mem_type not in {"quote", "event"}:
        return {"status": "error", "error": "description_required", "action": "memory.supersede"}, [], 400

    anchor_ids = raw.get("anchor_memory_ids") or action.get("anchor_memory_ids") or []
    if not isinstance(anchor_ids, list):
        return {"status": "error", "error": "anchor_memory_ids_must_be_list", "action": "memory.supersede"}, [], 400
    moments = memory_service._load_moments(store)
    old_indices: list[int] = []
    missing: list[str] = []
    for old_id in old_ids:
        idx = next((i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == old_id), None)
        if idx is None:
            missing.append(old_id)
        else:
            old_indices.append(idx)
    if missing:
        return {"status": "error", "error": "not_found", "missing": missing, "action": "memory.supersede"}, [], 404
    old_cards = [moments[idx] for idx in old_indices]
    if any(old.get("owner_user_id") != store.user_id for old in old_cards):
        return {"status": "error", "error": "not_owned", "action": "memory.supersede"}, [], 403

    ok, err = _memory_validate_write(store, moments, mem_type=mem_type, anchor_ids=anchor_ids)
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.supersede"}, [], 400

    old_inner, old_inner_err = _memory_plain_from_envelope(
        old_cards[0], api_key, runtime_token=runtime_token)
    if old_inner is None:
        return {
            "status": "error",
            "error": old_inner_err,
            "action": "memory.supersede",
        }, [], 409
    inherited = _memory_inheritable_inner_fields(old_inner)
    raw_for_inner = {**inherited, **raw, "type": mem_type, "title": title, "description": description}
    if "importance" not in raw_for_inner and old_cards[0].get("importance") is not None:
        raw_for_inner["importance"] = old_cards[0].get("importance")
    if "pulse" not in raw_for_inner and old_cards[0].get("pulse") is not None:
        raw_for_inner["pulse"] = old_cards[0].get("pulse")

    inner = _memory_inner_from_action(raw_for_inner)
    envelope, env_err = _build_memory_envelope_for_store(store, inner)
    if envelope is None:
        return {"status": "error", "error": env_err, "action": "memory.supersede"}, [], 409
    envelope["type"] = mem_type
    envelope["occurred_at"] = _memory_action_text(raw.get("occurred_at") or core_util._now_iso(), 80)
    envelope["source"] = _memory_action_text(raw.get("source") or action.get("source") or "hosted_runtime_state", 80)
    _memory_apply_v1_metadata(envelope, raw_for_inner, source=envelope["source"])
    envelope["supersedes"] = list(old_ids)
    if anchor_ids:
        envelope["anchor_memory_ids"] = list(anchor_ids)
    new_moment = _memory_record_from_envelope(store, envelope)

    now = core_util._now_iso()
    retired_docs: list[dict] = []
    # Re-read + re-find-by-id + retire + append + save under one memory_lock hold
    # (any enclave decrypt above stays OUTSIDE the lock). Find by stable id, not
    # the pre-reload index, so a concurrent same-user write can't lost-update.
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        by_id = {m.get("id"): m for m in moments if isinstance(m, dict)}
        for old_id in old_ids:
            retired = by_id.get(old_id)
            if retired is None:
                continue
            retired["status"] = "superseded"
            retired["superseded_by"] = new_moment["id"]
            retired["updated_at"] = now
            retired["is_archived"] = True
            retired["archived_at"] = now
            retired["archive_reason"] = f"superseded_by:{new_moment['id']}"
            retired_docs.append({
                "id": retired["id"],
                "status": "superseded",
                "superseded_by": new_moment["id"],
            })
        moments.append(new_moment)
        memory_service._save_moments(store, moments)
    change = memory_service._append_memory_change(store, {
        "action": "supersede",
        "memory_id": new_moment["id"],
        "supersedes": list(old_ids),
        "type": mem_type,
        "reason": _memory_action_text(action.get("reason") or "Memory superseded from chat.", 500),
        "capture_mode": action.get("capture_mode") or "",
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    effect = {
        "type": "memory_superseded",
        "action": "memory.supersede",
        "memory_id": new_moment["id"],
        "supersedes": old_ids[0] if len(old_ids) == 1 else list(old_ids),
        "superseded_ids": list(old_ids),
        "fields": ["created", "status", "supersedes", "superseded_by"],
    }
    return {
        "status": "ok",
        "action": "memory.supersede",
        "memory": {"id": new_moment["id"], "type": mem_type, "occurred_at": new_moment["occurred_at"], "status": "active"},
        "superseded": retired_docs[0] if len(retired_docs) == 1 else retired_docs,
        "superseded_ids": list(old_ids),
        "change": change,
    }, [effect], 201


def _memory_supersede_envelope_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    old_ids = _memory_supersedes_list(action)
    if not old_ids:
        return {"status": "error", "error": "supersedes_required", "action": "memory.supersede"}, [], 400
    envelope = dict(action.get("envelope") or {})
    moments = memory_service._load_moments(store)
    old_indices: list[int] = []
    missing: list[str] = []
    for old_id in old_ids:
        idx = next((i for i, m in enumerate(moments) if isinstance(m, dict) and m.get("id") == old_id), None)
        if idx is None:
            missing.append(old_id)
        else:
            old_indices.append(idx)
    if missing:
        return {"status": "error", "error": "not_found", "missing": missing, "action": "memory.supersede"}, [], 404
    old_cards = [moments[idx] for idx in old_indices]
    if any(old.get("owner_user_id") != store.user_id for old in old_cards):
        return {"status": "error", "error": "not_owned", "action": "memory.supersede"}, [], 403

    envelope["supersedes"] = list(old_ids)
    ok, err = _memory_validate_prebuilt_envelope(
        store,
        moments,
        envelope,
        memory_id=str(envelope.get("id") or ""),
    )
    if not ok:
        return {"status": "error", **(err or {}), "action": "memory.supersede"}, [], 400

    new_moment = _memory_record_from_prebuilt_envelope(store, envelope)
    now = core_util._now_iso()
    retired_docs: list[dict] = []
    # Re-read + re-find-by-id + retire + append + save under one memory_lock hold
    # (any enclave decrypt above stays OUTSIDE the lock). Find by stable id, not
    # the pre-reload index, so a concurrent same-user write can't lost-update.
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        by_id = {m.get("id"): m for m in moments if isinstance(m, dict)}
        for old_id in old_ids:
            retired = by_id.get(old_id)
            if retired is None:
                continue
            retired["status"] = "superseded"
            retired["superseded_by"] = new_moment["id"]
            retired["updated_at"] = now
            retired["is_archived"] = True
            retired["archived_at"] = now
            retired["archive_reason"] = f"superseded_by:{new_moment['id']}"
            retired_docs.append({
                "id": retired["id"],
                "status": "superseded",
                "superseded_by": new_moment["id"],
            })
        moments.append(new_moment)
        memory_service._save_moments(store, moments)
    anchor_ids = envelope.get("anchor_memory_ids") or []
    change = memory_service._append_memory_change(store, {
        "action": "supersede",
        "memory_id": new_moment["id"],
        "supersedes": list(old_ids),
        "type": new_moment.get("type", ""),
        "reason": _memory_action_text(action.get("reason") or "Memory superseded from encrypted tool action.", 500),
        "capture_mode": action.get("capture_mode") or "",
        "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        "anchor_memory_ids": anchor_ids,
    })
    effect = {
        "type": "memory_superseded",
        "action": "memory.supersede",
        "memory_id": new_moment["id"],
        "supersedes": old_ids[0] if len(old_ids) == 1 else list(old_ids),
        "superseded_ids": list(old_ids),
        "fields": ["created", "status", "supersedes", "superseded_by"],
    }
    return {
        "status": "ok",
        "action": "memory.supersede",
        "memory": {
            "id": new_moment["id"],
            "type": new_moment.get("type", ""),
            "occurred_at": new_moment.get("occurred_at", ""),
            "status": new_moment.get("status", "active"),
        },
        "superseded": retired_docs[0] if len(retired_docs) == 1 else retired_docs,
        "superseded_ids": list(old_ids),
        "change": change,
    }, [effect], 201


def _memory_delete_action(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    memory_id = _memory_action_text(action.get("id") or action.get("memory_id"), 160)
    if not memory_id:
        return {"status": "error", "error": "memory_id_required", "action": "memory.delete"}, [], 400
    with memory_service.mutation_lock(store):
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


def _execute_memory_action(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
) -> tuple[dict, list[dict], int]:
    if not isinstance(action, dict):
        return {"status": "error", "error": "action_must_be_object"}, [], 400
    action_type = str(action.get("type") or action.get("action") or "").strip()
    if action_type in {"memory.create", "memory.add", "memory.add_correction"}:
        normalized = dict(action)
        normalized["type"] = "memory.add"
        return _memory_add_action(store, normalized)
    if action_type in {"memory.patch", "memory.content_patch"}:
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        memory_id = _memory_action_text(
            action.get("memory_id")
            or action.get("id")
            or action.get("target_id")
            or target.get("memory_id")
            or target.get("id"),
            160,
        )
        patch = action.get("patch") if isinstance(action.get("patch"), dict) else action
        memory = action.get("memory") if isinstance(action.get("memory"), dict) else patch
        return _memory_supersede_action(store, api_key, {
            "type": "memory.supersede",
            "supersedes": memory_id,
            "memory": memory,
            "reason": action.get("reason") or "Memory patched by superseding old card.",
            "capture_mode": action.get("capture_mode") or "",
            "source_chat_message_ids": action.get("source_chat_message_ids") or [],
        }, runtime_token=runtime_token)
    if action_type == "memory.retype":
        return _memory_retype_action(store, action)
    if action_type == "memory.supersede":
        return _memory_supersede_action(
            store, api_key, action, runtime_token=runtime_token)
    if action_type == "memory.upgrade":
        # In-place legacy→v1 upgrade (migration). Its OWN branch — must never be
        # rewritten to supersede (that mints a new id; upgrade preserves id).
        if isinstance(action.get("envelope"), dict):
            return _memory_upgrade_envelope_action(store, action)
        return _memory_upgrade_action(store, api_key, action)
    if action_type == "memory.delete":
        return _memory_delete_action(store, action)
    return {
        "status": "error",
        "error": "unsupported_memory_action",
        "action": action_type,
        "supported": ["memory.add", "memory.supersede", "memory.upgrade", "memory.delete", "memory.retype"],
    }, [], 400


def _execute_memory_actions(
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
    for action in actions[:20]:
        result, action_effects, status = _execute_memory_action(
            store, api_key, action, runtime_token=runtime_token)
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
