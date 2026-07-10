"""Framework-neutral content-envelope operations (ASGI-migration plan §5.3).

A pure relocation of the Flask route bodies for ``/v1/users/public-key``,
``/v1/content/swap``, ``/v1/content/rewrap-to-current-key``, ``/v1/content/export``
and ``/v1/account/reset`` so both the Flask adapter (``content.routes``) and the
native FastAPI router (``content.routes_asgi``) share one implementation and
return byte-identical responses. No ``flask.request`` here — every function takes
already-parsed params + the resolved store (and, for rewrap, the caller's api
key) as explicit arguments.

E2E boundary (unchanged): chat / memory / identity / frame ``content`` fields are
v1 E2E envelopes. The server NEVER decrypts them.

  - ``set_public_key`` / ``swap`` / ``export_data`` are plain store/DB reads or
    envelope-field relocations — no decryption, no enclave, no new plaintext.
    ``export_data`` returns the stored ciphertext verbatim.
  - ``rewrap_to_current_key`` forwards the caller's **api key** to the enclave
    (``core_enclave._decrypt_envelope_via_enclave``) exactly as the Flask route
    did — same function, same positional/keyword args, same order. Decryption
    happens INSIDE the enclave; the backend only re-wraps the returned plaintext
    to the caller-supplied content public key. Mirrors Flask precisely (Flask
    forwarded only ``auth._extract_api_key()``; it never passed a runtime token
    to this enclave call, so neither do we).

``account_reset`` is DESTRUCTIVE (per-user CASCADE delete). Its ordering and side
effects are preserved byte-for-byte from the Flask route; the plaintext-archive
purge is injected (``purge_archives``) so the Flask adapter keeps owning the
retry constants that its tests monkeypatch.

All of these are blocking (sync DB / enclave / R2 / filesystem), so ASGI callers
must run them on the threadpool (``threadpool.run_db``), never the event loop.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import db
from accounts import registry
from content_encryption import build_envelope
from core import config as core_config
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import store as core_store
from core import wake_bus
from core.store import UserStore
from identity import service as identity_service
from memory import service as memory_service
from onboarding_archive import storage as onboarding_archive_storage


# --------------------------------------------------------------------------- #
# Plaintext-archive purge (used by account_reset). Lives here (framework-neutral)
# since the Flask content.routes adapter was deleted in the ASGI cutover; both
# the retry constants and the helper are injected into ``account_reset`` and are
# monkeypatched by test_onboarding_archive_reset.py at this module path.
# --------------------------------------------------------------------------- #

_ARCHIVE_DELETE_ATTEMPTS = 3
_ARCHIVE_DELETE_BASE_DELAY = 0.3


def _purge_onboarding_archives_with_retry(user_id: str) -> Exception | None:
    """Delete the user's plaintext onboarding archives from R2 with bounded retry.

    Returns None on success (or when archive storage is disabled), else the last
    exception. A non-None return MUST abort the reset BEFORE the account is deleted:
    deleting the account first removes the authenticated retry path / DB ownership,
    so a failed purge would orphan undiscoverable plaintext originals on R2 while we
    report success. ``onboarding_archive.storage.delete_user_archives`` raises for
    exactly this reason."""
    if not onboarding_archive_storage.enabled():
        return None
    import time
    last: Exception | None = None
    for attempt in range(_ARCHIVE_DELETE_ATTEMPTS):
        try:
            onboarding_archive_storage.delete_user_archives(user_id)
            return None
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < _ARCHIVE_DELETE_ATTEMPTS - 1 and _ARCHIVE_DELETE_BASE_DELAY > 0:
                time.sleep(_ARCHIVE_DELETE_BASE_DELAY * (attempt + 1))
    return last


# --------------------------------------------------------------------------- #
# Encrypted-content counters
# --------------------------------------------------------------------------- #

def _has_encrypted_content_record(item: dict | None) -> bool:
    return bool(
        isinstance(item, dict)
        and item.get("body_ct")
        and item.get("nonce")
        and item.get("K_user")
    )


def _encrypted_content_counts(store: UserStore) -> dict:
    identity = identity_service._load_identity(store)
    moments = memory_service._load_moments(store)
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    counts = {
        "identity": 1 if _has_encrypted_content_record(identity) else 0,
        "memory": sum(1 for m in moments if _has_encrypted_content_record(m)),
        "chat": sum(1 for m in chat_msgs if _has_encrypted_content_record(m)),
    }
    counts["total"] = counts["identity"] + counts["memory"] + counts["chat"]
    return counts


# --------------------------------------------------------------------------- #
# Envelope swap helpers
# --------------------------------------------------------------------------- #

def _swap_envelope_missing(env) -> list:
    if not isinstance(env, dict):
        return ["envelope"]
    return [f for f in ("body_ct", "nonce", "K_user", "visibility", "owner_user_id") if not env.get(f)]


def _swap_summary(results: list) -> dict:
    summary = {"ok": 0, "not_found": 0, "error": 0, "total": len(results)}
    for r in results:
        status = r.get("status", "")
        if status == "ok":
            summary["ok"] += 1
        elif status == "not_found":
            summary["not_found"] += 1
        else:
            summary["error"] += 1
    return summary


def _propagate_swap_to_tee(user_id: str, table_name: str, item_id: str,
                           visibility: str, tee_delete_sql: str) -> None:
    """Plaintext-safe propagation of a /v1/content/swap (or rewrap) in-place
    envelope replace to the TEE shadow.

    chat/memory rows are ciphertext→plaintext REPLICATED (not dual-written), and
    a swap keeps the same PK, so the append-only cursor never revisits them:
      - → local_only: the item is now private; DELETE the TEE plaintext row and
        drop a terminal ``visibility_local_only`` pending marker (keeps verify's
        ``rds == tee + pending`` balanced — the RDS row still exists).
      - → shared: enqueue the requeue lane so the next worker pass re-derives the
        TEE plaintext from the new ciphertext.
    Best-effort (shadow phase): mirror swallows failures. ``table_name`` matches
    the tee_replicator.worker._TABLES key so the requeue consume step picks it up.
    """
    from tee_shadow import mirror
    if visibility == "local_only":
        mirror.execute(tee_delete_sql, (user_id, item_id))
        mirror.mark_pending(user_id, table_name, item_id, "visibility_local_only")
    else:  # "shared" — validated by the caller
        mirror.mark_pending(user_id, table_name, item_id, "requeue_visibility_shared")


def _swap_chat(
    store: "UserStore",
    msg_id: str,
    env: dict | None,
    sub_envs: dict[str, dict] | None = None,
) -> str:
    """``env=None`` 表示主信封已是当前钥,只写回 ``sub_envs`` 里的子信封。"""
    with store.chat_lock:
        for msg in store.chat_messages:
            if msg.get("id") != msg_id:
                continue
            if env is not None:
                msg["v"] = int(env.get("v", 1))
                msg["body_ct"] = env["body_ct"]
                msg["nonce"] = env["nonce"]
                msg["K_user"] = env["K_user"]
                if env.get("K_enclave"):
                    msg["K_enclave"] = env["K_enclave"]
                else:
                    msg.pop("K_enclave", None)
                msg["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
                if env.get("content_pk_fpr"):
                    msg["content_pk_fpr"] = env["content_pk_fpr"]
                else:
                    msg.pop("content_pk_fpr", None)
                msg["visibility"] = env["visibility"]
                msg["owner_user_id"] = env["owner_user_id"]
            for prefix, sub_env in (sub_envs or {}).items():
                _apply_sub_envelope_fields(msg, prefix, sub_env)
            # Full-row replace: the K_enclave key may have been removed, which a
            # JSONB shallow-merge can't express, so we overwrite the whole doc.
            db.chat_append(store.user_id, msg_id, msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)
            # env=None（仅子信封 rewrap）时 visibility 不变，取行内现值——行的
            # 密文仍被原地改写了，TEE 明文照样需要传播（游标不会回头）。
            _propagate_swap_to_tee(
                store.user_id, "chat_messages", msg_id,
                env["visibility"] if env is not None else str(msg.get("visibility") or "shared"),
                "DELETE FROM chat_messages WHERE user_id = %s AND msg_id = %s")
            return "ok"
    return "not_found"


def _swap_memory_inplace(moments: list, mom_id: str, env: dict) -> str:
    for m in moments:
        if m.get("id") != mom_id:
            continue
        m["v"] = int(env.get("v", 1))
        m["body_ct"] = env["body_ct"]
        m["nonce"] = env["nonce"]
        m["K_user"] = env["K_user"]
        if env.get("K_enclave"):
            m["K_enclave"] = env["K_enclave"]
        else:
            m.pop("K_enclave", None)
        m["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
        m.pop("content_pk_fpr", None)  # swap is not a rewrap; drop any stale server stamp
        m["visibility"] = env["visibility"]
        m["owner_user_id"] = env["owner_user_id"]
        return "ok"
    return "not_found"


# --------------------------------------------------------------------------- #
# Rewrap helpers
# --------------------------------------------------------------------------- #

def _rewrap_bucket() -> dict:
    return {"checked": 0, "rewrapped": 0, "skipped": 0, "errors": 0}


def _rewrap_summary() -> dict:
    return {
        "identity": _rewrap_bucket(),
        "memory": _rewrap_bucket(),
        "chat": _rewrap_bucket(),
        "total_checked": 0,
        "total_rewrapped": 0,
        "total_skipped": 0,
        "total_errors": 0,
    }


def _rewrap_record_result(
    summary: dict,
    kind: str,
    item_id: str,
    status: str,
    *,
    reason: str = "",
) -> dict:
    bucket = summary[kind]
    bucket["checked"] += 1
    summary["total_checked"] += 1
    if status == "rewrapped":
        bucket["rewrapped"] += 1
        summary["total_rewrapped"] += 1
    elif status == "error":
        bucket["errors"] += 1
        summary["total_errors"] += 1
    else:
        bucket["skipped"] += 1
        summary["total_skipped"] += 1
    result = {"type": kind, "id": item_id, "status": status}
    if reason:
        result["reason"] = reason[:240]
    return result


def _apply_envelope_fields(record: dict, env: dict) -> None:
    if not record.get("id") and env.get("id"):
        record["id"] = env["id"]
    record["v"] = int(env.get("v", 1))
    record["body_ct"] = env["body_ct"]
    record["nonce"] = env["nonce"]
    record["K_user"] = env["K_user"]
    if env.get("K_enclave"):
        record["K_enclave"] = env["K_enclave"]
    else:
        record.pop("K_enclave", None)
    record["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
    if env.get("content_pk_fpr"):
        record["content_pk_fpr"] = env["content_pk_fpr"]
    else:
        record.pop("content_pk_fpr", None)
    record["visibility"] = env["visibility"]
    record["owner_user_id"] = env["owner_user_id"]


# 一条 chat 记录可以挂两个独立信封:agent 的思维链、图片的说明文字。它们各自带
# id/v/owner_user_id,iOS 正是用这三元组重算 AAD(ChatMessage.swift,`thinking_id ?? id`),
# 所以重封时必须原样保留,否则设备再也算不出匹配的 AAD。
_SUB_ENVELOPE_PREFIXES = ("thinking", "caption")


def _extract_sub_envelope(record: dict, prefix: str) -> dict | None:
    """把 ``<prefix>_*`` 字段还原成一个标准信封,好走与主信封同一套重封逻辑。"""
    if not record.get(f"{prefix}_K_user") or not record.get(f"{prefix}_body_ct"):
        return None
    return {
        "v": int(record.get(f"{prefix}_v") or 1),
        # iOS 在 <prefix>_id 缺失时回落到记录自身的 id;AAD 要跟着一起回落。
        "id": str(record.get(f"{prefix}_id") or record.get("id") or ""),
        "body_ct": record.get(f"{prefix}_body_ct"),
        "nonce": record.get(f"{prefix}_nonce"),
        "K_user": record.get(f"{prefix}_K_user"),
        "K_enclave": record.get(f"{prefix}_K_enclave"),
        "visibility": record.get(f"{prefix}_visibility") or "shared",
        "owner_user_id": record.get(f"{prefix}_owner_user_id") or record.get("owner_user_id"),
        "content_pk_fpr": record.get(f"{prefix}_content_pk_fpr"),
    }


def _apply_sub_envelope_fields(record: dict, prefix: str, env: dict) -> None:
    """写回重封后的子信封。只碰自己的前缀字段,不像主信封那样 pop 缺失的键。"""
    record[f"{prefix}_v"] = int(env.get("v", 1))
    record[f"{prefix}_id"] = env["id"]
    record[f"{prefix}_body_ct"] = env["body_ct"]
    record[f"{prefix}_nonce"] = env["nonce"]
    record[f"{prefix}_K_user"] = env["K_user"]
    if env.get("K_enclave"):
        record[f"{prefix}_K_enclave"] = env["K_enclave"]
    record[f"{prefix}_visibility"] = env["visibility"]
    record[f"{prefix}_owner_user_id"] = env["owner_user_id"]
    record[f"{prefix}_content_pk_fpr"] = env.get("content_pk_fpr", "")


def _build_rewrapped_sub_envelopes(
    store: UserStore,
    record: dict,
    *,
    api_key: str | None,
    user_pk: bytes,
    enclave_pk: bytes,
    kind: str,
    current_fpr: str = "",
) -> list[tuple[str, dict | None, str, str]]:
    """重封该记录下的每个子信封,逐个返回 ``(prefix, env, status, reason)``。

    每个子信封独立计入 summary:主信封重封成功、思维链失败时,前者仍是真实进展
    (``_swap_chat`` 已经把它落库了),后者只是一条待重试的 error。把两者压成一个
    status 会让 ``made_progress`` 误判成 False,于是 public_key 永远推不上去。

    local_only / 无 K_enclave 的子信封 enclave 打不开,原样留下(而不是丢弃)。
    """
    out: list[tuple[str, dict | None, str, str]] = []
    for prefix in _SUB_ENVELOPE_PREFIXES:
        sub = _extract_sub_envelope(record, prefix)
        if sub is None:
            continue
        env, status, reason = _build_rewrapped_envelope(
            store,
            sub,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind=f"{kind}_{prefix}",
            current_fpr=current_fpr,
        )
        out.append((prefix, env, status, reason))
    return out


def _build_rewrapped_envelope(
    store: UserStore,
    record: dict,
    *,
    api_key: str | None,
    user_pk: bytes,
    enclave_pk: bytes,
    kind: str,
    current_fpr: str = "",
) -> tuple[dict | None, str, str]:
    item_id = str(record.get("id") or "")
    if not _has_encrypted_content_record(record):
        return None, "skipped_unencrypted", ""
    if str(record.get("visibility") or "shared") != "shared":
        return None, "skipped_local_only", ""
    if not record.get("K_enclave"):
        return None, "skipped_missing_enclave_key", ""
    # 已是当前钥 → 跳过,不进 enclave。仅 rewrap 会盖 content_pk_fpr,故字段与
    # K_user 始终由同一 env 原子写入、二者一致可信。
    if current_fpr and record.get("content_pk_fpr") == current_fpr:
        return None, "skipped_already_current", ""
    try:
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            record,
            api_key,
            purpose=f"content_rewrap:{kind}:{item_id or 'unknown'}",
        )
    except Exception as e:
        return None, "error", f"decrypt_failed:{type(e).__name__}:{str(e)}"
    try:
        env = build_envelope(
            plaintext=plaintext,
            owner_user_id=store.user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enclave_pk,
            visibility="shared",
            item_id=item_id or None,
        )
        env["content_pk_fpr"] = current_fpr
        return env, "rewrapped", ""
    except Exception as e:
        return None, "error", f"envelope_build_failed:{type(e).__name__}:{str(e)}"


# --------------------------------------------------------------------------- #
# POST /v1/users/public-key
# --------------------------------------------------------------------------- #

def set_public_key(store: UserStore, payload: dict) -> tuple[dict, int]:
    """Backfill the authenticated user's content public key.

    Conservative: once encrypted content exists, rotation must go through
    ``rewrap_to_current_key`` so stored envelopes are rewrapped first.
    """
    public_key = (payload.get("public_key") or "").strip()
    if not public_key:
        return {"error": "public_key required"}, 400
    _, err = core_envelope._decode_content_public_key(public_key)
    if err:
        return {"error": err}, 400

    existing = registry._get_user_public_key(store.user_id)
    if existing == public_key:
        return {
            "ok": True,
            "status": "unchanged",
            "user_id": store.user_id,
            "public_key_fpr": core_envelope._content_public_key_fingerprint(public_key),
        }, 200
    counts = _encrypted_content_counts(store)
    if existing and counts["total"] > 0:
        return {
            "error": "public_key_rotation_requires_rewrap",
            "message": "Existing encrypted content must be rewrapped before changing public_key.",
            "current_public_key_fpr": core_envelope._content_public_key_fingerprint(existing),
            "requested_public_key_fpr": core_envelope._content_public_key_fingerprint(public_key),
            "encrypted_content": counts,
            "recovery_endpoint": "/v1/content/rewrap-to-current-key",
        }, 409

    if not registry._set_user_public_key(store.user_id, public_key):
        return {"error": "user not found"}, 404

    print(f"[users] updated public_key for {store.user_id} fpr={core_envelope._content_public_key_fingerprint(public_key)}")
    return {
        "ok": True,
        "status": "updated",
        "user_id": store.user_id,
        "public_key_fpr": core_envelope._content_public_key_fingerprint(public_key),
        "encrypted_content": counts,
    }, 200


# --------------------------------------------------------------------------- #
# POST /v1/content/swap
# --------------------------------------------------------------------------- #

def swap(store: UserStore, payload: dict) -> tuple[dict, int]:
    items = payload.get("items")
    if not isinstance(items, list):
        return {"error": "items must be a list"}, 400
    if not items:
        return {"results": [], "summary": _swap_summary([])}, 200

    results: list[dict] = []
    memory_dirty = False
    moments = None
    memory_swaps: list[tuple[str, dict]] = []

    for item in items:
        if not isinstance(item, dict):
            results.append({"type": None, "id": None, "status": "error: item must be a dict"})
            continue
        itype = item.get("type")
        iid = (item.get("id") or "").strip()
        env = item.get("envelope")
        if itype not in ("chat", "memory"):
            results.append({"type": itype, "id": iid, "status": "error: unsupported type (chat, memory only)"})
            continue
        if not iid:
            results.append({"type": itype, "id": None, "status": "error: id required"})
            continue
        missing = _swap_envelope_missing(env)
        if missing:
            results.append({"type": itype, "id": iid, "status": f"error: envelope missing {missing}"})
            continue
        if env["visibility"] not in ("shared", "local_only"):
            results.append({"type": itype, "id": iid, "status": "error: envelope.visibility must be 'shared' or 'local_only'"})
            continue
        if env["visibility"] == "shared" and not env.get("K_enclave"):
            results.append({"type": itype, "id": iid, "status": "error: shared visibility requires K_enclave"})
            continue
        if env["owner_user_id"] != store.user_id:
            results.append({"type": itype, "id": iid, "status": "error: owner_user_id does not match caller"})
            continue

        env.pop("content_pk_fpr", None)  # content_pk_fpr is a server-only stamp; never trust the client

        if itype == "chat":
            # _swap_chat persists the matched message to the DB itself.
            status = _swap_chat(store, iid, env)
            results.append({"type": "chat", "id": iid, "status": status})
        else:
            if moments is None:
                moments = memory_service._load_moments(store)
            status = _swap_memory_inplace(moments, iid, env)
            if status == "ok":
                memory_dirty = True
                memory_swaps.append((iid, env))
            results.append({"type": "memory", "id": iid, "status": status})

    if memory_dirty:
        # Re-apply the accepted envelope swaps onto a fresh snapshot under
        # memory_lock so a concurrent same-user memory write isn't lost-updated
        # by this save (the plain full-list replace reconciles deletes).
        with memory_service.mutation_lock(store):
            fresh = memory_service._load_moments(store)
            for swap_id, swap_env in memory_swaps:
                _swap_memory_inplace(fresh, swap_id, swap_env)
            memory_service._save_moments(store, fresh)
        # Propagate each accepted memory swap to the TEE shadow AFTER the persist.
        # _save_moments → db.memory_replace_all already requeues all survivors, but
        # a → local_only swap additionally needs the TEE plaintext row DELETED +
        # a terminal marker, which requeue-all can't express — so wire it here
        # explicitly (last-writer-wins on the pending reason). See
        # _propagate_swap_to_tee.
        for swap_id, swap_env in memory_swaps:
            _propagate_swap_to_tee(
                store.user_id, "memory_moments", swap_id, swap_env["visibility"],
                "DELETE FROM memory_moments WHERE user_id = %s AND moment_id = %s")

    return {"results": results, "summary": _swap_summary(results)}, 200


# --------------------------------------------------------------------------- #
# POST /v1/content/rewrap-to-current-key
# --------------------------------------------------------------------------- #

def rewrap_to_current_key(
    store: UserStore, payload: dict, *, api_key: str | None
) -> tuple[dict, int]:
    """Rewrap chat/memory/identity envelopes to the caller's current key.

    Recovery path for key drift: the enclave decrypts existing shared envelopes
    via K_enclave (forwarding ``api_key`` exactly as the Flask route did), then
    the backend re-encrypts the same plaintext to the public_key supplied by the
    authenticated iOS client. The user record's public_key is updated only after
    every eligible item has been verified.
    """
    dry_raw = payload.get("dry_run", False)
    dry_run = dry_raw is True or (isinstance(dry_raw, str) and dry_raw.lower() in {"1", "true", "yes", "on"})
    requested_public_key = (payload.get("public_key") or registry._get_user_public_key(store.user_id) or "").strip()
    user_pk, err = core_envelope._decode_content_public_key(requested_public_key)
    if err or user_pk is None:
        return {"error": err or "public_key invalid"}, 400

    enclave_pk, enclave_fpr, enclave_err = core_envelope._enclave_content_public_key_material()
    if enclave_err or enclave_pk is None:
        return {"error": enclave_err or "enclave_info_unavailable"}, 503
    current_fpr = core_envelope._content_public_key_fingerprint(user_pk)

    summary = _rewrap_summary()
    results: list[dict] = []
    identity_plan: dict | None = None
    memory_plans: list[tuple[int, dict]] = []
    chat_plans: list[tuple[str, dict | None, dict[str, dict]]] = []

    identity = identity_service._load_identity(store)
    if identity is not None:
        env, status, reason = _build_rewrapped_envelope(
            store,
            identity,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="identity",
            current_fpr=current_fpr,
        )
        item_id = str(identity.get("id") or "identity")
        results.append(_rewrap_record_result(summary, "identity", item_id, status, reason=reason))
        if env is not None:
            identity_plan = env

    moments = memory_service._load_moments(store)
    for idx, moment in enumerate(moments):
        if not isinstance(moment, dict):
            continue
        env, status, reason = _build_rewrapped_envelope(
            store,
            moment,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="memory",
            current_fpr=current_fpr,
        )
        item_id = str(moment.get("id") or "")
        results.append(_rewrap_record_result(summary, "memory", item_id, status, reason=reason))
        if env is not None:
            memory_plans.append((item_id, env))

    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    for msg in chat_msgs:
        if not isinstance(msg, dict):
            continue
        env, status, reason = _build_rewrapped_envelope(
            store,
            msg,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="chat",
            current_fpr=current_fpr,
        )
        item_id = str(msg.get("id") or "")
        results.append(_rewrap_record_result(summary, "chat", item_id, status, reason=reason))
        # 子信封独立于主信封:主体可能早已重封到当前钥,思维链却还封在旧钥上。
        # 因此既不能因为主信封 skipped_already_current 就整条跳过,也不能让子信封
        # 的失败盖掉主信封的成功 —— 各记各的账。
        sub_envs: dict[str, dict] = {}
        for prefix, sub_env, sub_status, sub_reason in _build_rewrapped_sub_envelopes(
            store,
            msg,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="chat",
            current_fpr=current_fpr,
        ):
            results.append(
                _rewrap_record_result(
                    summary, "chat", f"{item_id}#{prefix}", sub_status, reason=sub_reason
                )
            )
            if sub_env is not None:
                sub_envs[prefix] = sub_env
        if env is not None or sub_envs:
            chat_plans.append((item_id, env, sub_envs))

    response = {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "user_id": store.user_id,
        "public_key_fpr": core_envelope._content_public_key_fingerprint(user_pk),
        "enclave_pk_fpr": enclave_fpr,
        "summary": summary,
        "results": results,
    }
    # pending = 本轮 error 的条目;客户端只需重试这些即可收敛。
    pending = [
        {"type": r["type"], "id": r["id"], "reason": r.get("reason", "")}
        for r in results if r["status"] == "error"
    ]
    response["pending"] = pending

    if dry_run:
        response["status"] = "dry_run"
        print(f"[content-rewrap:{store.user_id}] dry_run rewrappable={summary['total_rewrapped']} skipped={summary['total_skipped']} errors={summary['total_errors']}")
        return response, 200

    # 无条件落盘所有已成功 rewrap 的条目。部分进度是安全的:仍停在旧钥的条目
    # 本就不可解(设备已丢旧 SK,这正是调 rewrap 的前提),故落盘成功项只增不减
    # 可解内容。收敛来自客户端重试 pending。
    now = datetime.now().isoformat()
    if identity is not None and identity_plan is not None:
        new_identity = dict(identity)
        _apply_envelope_fields(new_identity, identity_plan)
        new_identity["rewrapped_at"] = now
        identity_service._save_identity(store, new_identity)
        identity_service._append_identity_change(store, {
            "action": "rewrap",
            "reason": "Identity envelope rewrapped to the current iOS content key.",
        })

    if memory_plans:
        # Re-read + apply-by-id + save under one memory_lock hold (find by stable
        # id, not the pre-reload index) so a concurrent same-user memory write
        # isn't lost-updated by this rewrap save.
        with memory_service.mutation_lock(store):
            fresh = memory_service._load_moments(store)
            by_id = {m.get("id"): m for m in fresh if isinstance(m, dict)}
            for plan_id, env in memory_plans:
                target = by_id.get(plan_id)
                if target is not None:
                    _apply_envelope_fields(target, env)
                    target["rewrapped_at"] = now
            memory_service._save_moments(store, fresh)

    swapped_ids: set[str] = set()
    for item_id, env, sub_envs in chat_plans:
        # _swap_chat 自行把交换后的信封字段写回 DB。
        if _swap_chat(store, item_id, env, sub_envs) == "ok":
            swapped_ids.add(item_id)
    if swapped_ids:
        with store.chat_lock:
            for msg in store.chat_messages:
                if isinstance(msg, dict) and msg.get("id") in swapped_ids:
                    msg["rewrapped_at"] = now
                    db.chat_append(store.user_id, msg["id"], msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)

    # 有进展(至少一条 rewrap)或完全无错 → 推进注册钥,使新内容 wrap 到当前设备钥、
    # 下一轮工作集单调收缩。零进展且有错 → 不动钥,返回 409 让客户端退避重试。
    made_progress = summary["total_rewrapped"] > 0
    clean = summary["total_errors"] == 0
    if clean or made_progress:
        if not registry._set_user_public_key(store.user_id, requested_public_key):
            return {"error": "user not found"}, 404

    if clean:
        response["status"] = "ok"
        code = 200
    elif made_progress:
        response["status"] = "partial"
        code = 200
    else:
        response["status"] = "failed"
        response["error"] = "rewrap_failed_no_progress"
        code = 409

    print(f"[content-rewrap:{store.user_id}] {response['status']} rewrapped={summary['total_rewrapped']} skipped={summary['total_skipped']} errors={summary['total_errors']} pending={len(pending)} fpr={response['public_key_fpr']}")
    return response, code


# --------------------------------------------------------------------------- #
# GET /v1/content/export
# --------------------------------------------------------------------------- #

# Cap single-shot export response size. With frames bounded to MAX_FRAMES
# (200) and each body_ct at ~200 KiB, worst-case frame payload is ~40 MiB —
# so the 80 MiB ceiling covers frames + chat + memory + identity with
# headroom. If this ever trips, switch to a streaming multipart response.
_EXPORT_MAX_BYTES = 80 * 1024 * 1024  # 80 MiB


@dataclass
class ExportResult:
    """Framework-neutral description of the export response.

    ``raw_body`` set → an opaque body rendered as a raw Response with
    ``media_type`` + ``headers`` (the attachment JSON blob). ``json_body`` set →
    a jsonify / JSONResponse (the 413 error). Mutually exclusive.
    """

    status: int
    json_body: Optional[Any] = None
    raw_body: Optional[str] = None
    media_type: Optional[str] = None
    headers: dict = field(default_factory=dict)


def export_data(store: UserStore) -> ExportResult:
    """Return the caller's chat, memory, identity, and frames as one JSON blob.

    Ciphertext is returned verbatim — iOS decrypts client-side using the user's
    content_sk from Keychain. No decryption happens server-side.
    """
    # Synthetic verify-loop liveness rows are internal probes, not the user's
    # conversation — exclude them from the data export (belt-and-suspenders with
    # the visible-history scrub).
    hist = [m for m in store.chat_messages if m.get("source") != "verify_ping"]
    moments = memory_service._load_moments(store)
    identity = identity_service._load_identity(store)

    # Inline each frame's stored envelope. frames_meta is the index; the
    # ciphertext lives in its frame_envelopes row. A missing row just means the
    # frame was evicted mid-read — skip it rather than 500.
    frames_out: list[dict] = []
    with store.frames_lock:
        frame_index = [f.copy() for f in store.frames_meta]
    for meta in frame_index:
        fid = meta.get("id")
        envelope = db.frame_get(store.user_id, fid) if fid else None
        if not isinstance(envelope, dict):
            continue
        frames_out.append({
            "id": fid,
            "ts": meta.get("ts"),
            "w": meta.get("w", 0),
            "h": meta.get("h", 0),
            "envelope": envelope,
        })

    exported_at = datetime.now().isoformat()
    enclave_info = core_enclave._get_enclave_info() or {}

    export = {
        "schema_version": 2,
        "user_id": store.user_id,
        "exported_at": exported_at,
        "attestation_snapshot": {
            "enclave_content_public_key_hex": enclave_info.get("content_pk_hex", ""),
            "compose_hash": enclave_info.get("compose_hash", ""),
        },
        "chat": hist,
        "memory": moments,
        "identity": identity,
        "frames": frames_out,
        "notes": (
            "Ciphertext included verbatim; decrypt client-side using your"
            " content private key (iCloud Keychain). The attestation_snapshot"
            " records which enclave version was live at export time so you"
            " can verify origin later. Frames are v1 envelopes — their JPEG"
            " + OCR live inside body_ct."
        ),
    }

    body = json.dumps(export, ensure_ascii=False, indent=2)
    if len(body.encode("utf-8")) > _EXPORT_MAX_BYTES:
        return ExportResult(413, json_body={
            "error": "export_too_large",
            "detail": "One-shot export exceeds the 80 MiB budget. Streaming"
                      " export is planned (TODO). Contact support / open an issue."
        })

    # Suggest a filename when clients save to disk.
    safe_name = f"feedling-export-{store.user_id}-{exported_at.replace(':', '').split('.')[0]}.json"
    return ExportResult(
        200,
        raw_body=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# --------------------------------------------------------------------------- #
# POST /v1/account/reset  (DESTRUCTIVE — ordering + side effects preserved)
# --------------------------------------------------------------------------- #

def account_reset(
    store: UserStore,
    payload: dict,
    *,
    purge_archives: Callable[[str], Optional[Exception]],
) -> tuple[dict, int]:
    """Hard-delete the caller's account: purge plaintext R2 archives, drop the
    users row (CASCADE), evict caches, best-effort clean residuals.

    ``purge_archives`` is injected (the Flask adapter owns the retry constants its
    tests monkeypatch); it must return ``None`` on success (or when archive
    storage is disabled) else the last exception, in which case the reset aborts
    BEFORE the account is deleted — matching Flask exactly.
    """
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "delete-all-data":
        return {
            "error": "confirmation_required",
            "detail": "POST body must include {\"confirm\": \"delete-all-data\"}."
                      " This prevents accidental resets from misbehaving clients."
        }, 400

    user_id = store.user_id

    # 隐私关键第一步：删账号前先清掉该用户的 R2 明文 onboarding 归档。若先删账号，
    # 就没了带鉴权的重试路径，一次瞬时 R2 失败会在"报告成功"的同时留下无从发现的
    # 明文孤儿(无 reaper 兜底)。有界重试抹平抖动;持续失败则中止(此刻尚未删任何东西，
    # 状态一致、客户端可安全重试;iOS 会把非 200/401 当错误提示，不会误以为已删)。
    archive_err = purge_archives(user_id)
    if archive_err is not None:
        print(f"[reset:{user_id}] onboarding archive R2 cleanup failed after retries, aborting: {archive_err}")
        return {
            "error": "archive_cleanup_failed",
            "detail": "Could not purge onboarding archives; reset aborted, safe to retry.",
        }, 503

    # DB 权威、原子：删 users 行即 CASCADE 清净所有 per-user 数据(0011)。
    with registry._users_lock:
        before = len(registry._users)
        registry._users[:] = [u for u in registry._users if u.get("user_id") != user_id]
        removed = before - len(registry._users)
        # Evict all cached (hash → user_id) entries pointing at this user.
        to_evict = [h for h, uid in registry._key_to_user.items() if uid == user_id]
        for h in to_evict:
            registry._key_to_user.pop(h, None)
        db.delete_user(user_id)                 # CASCADE 原子清净
        # Cross-worker: other workers still hold this user in their _users /
        # _key_to_user and would keep auth'ing the deleted account's key until
        # they reload. db.delete_user already removed the row, so their reload
        # drops the user.
        registry.notify_users_changed()

    # 跨 worker evict 缓存 store(丢掉脏 token)——本 worker 下面再 pop。
    wake_bus.notify("blob", user_id)

    # 以下 best-effort：DB 已原子删净(CASCADE)，剩余 R2 frames / DB 兜底清理失败只
    # 记日志，绝不 abort、绝不改 200——它们不是明文、且已被 CASCADE 覆盖。明文 onboarding
    # 归档不在此列：它在删账号之前就已清理并在失败时 abort(见上)。
    for label, fn in (
        ("frames-r2", lambda: db.delete_user_frames(user_id)),
        ("chat-files-r2", lambda: db.delete_user_chat_files(user_id)),
        ("db-belt", lambda: db.delete_user_data(user_id)),
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[reset:{user_id}] {label} cleanup failed (non-fatal): {e}")

    with core_store._stores_lock:
        core_store._stores.pop(user_id, None)

    # Best-effort cleanup of any residual on-disk dir (pre-migration leftovers).
    try:
        if (
            store.dir.exists()
            and store.dir != core_config.FEEDLING_DIR
            and store.dir.parent == core_config.FEEDLING_DIR
        ):
            shutil.rmtree(store.dir)
    except Exception as e:
        print(f"[reset:{user_id}] residual dir cleanup failed: {e}")

    print(f"[reset:{user_id}] deleted (user_record={removed})")
    return {"deleted": True, "user_id": user_id}, 200
