"""Content envelope ops: swap / rewrap / export, public-key backfill, reset, healthz."""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

import db
from core.store import UserStore
import base64
import shutil

from accounts import auth
from accounts import registry
from content_encryption import build_envelope
from core import config as core_config
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import store as core_store
from identity import service as identity_service
from memory import service as memory_service

bp = Blueprint("content", __name__)

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



@bp.route("/v1/users/public-key", methods=["POST"])
def users_set_public_key():
    """Backfill the authenticated user's content public key.

    This route is intentionally conservative. Once encrypted content exists,
    public_key rotation must go through /v1/content/rewrap-to-current-key so
    stored envelopes are rewrapped before future writes target the new key.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    public_key = (payload.get("public_key") or "").strip()
    if not public_key:
        return jsonify({"error": "public_key required"}), 400
    _, err = core_envelope._decode_content_public_key(public_key)
    if err:
        return jsonify({"error": err}), 400

    existing = registry._get_user_public_key(store.user_id)
    if existing == public_key:
        return jsonify({
            "ok": True,
            "status": "unchanged",
            "user_id": store.user_id,
            "public_key_fpr": core_envelope._content_public_key_fingerprint(public_key),
        })
    counts = _encrypted_content_counts(store)
    if existing and counts["total"] > 0:
        return jsonify({
            "error": "public_key_rotation_requires_rewrap",
            "message": "Existing encrypted content must be rewrapped before changing public_key.",
            "current_public_key_fpr": core_envelope._content_public_key_fingerprint(existing),
            "requested_public_key_fpr": core_envelope._content_public_key_fingerprint(public_key),
            "encrypted_content": counts,
            "recovery_endpoint": "/v1/content/rewrap-to-current-key",
        }), 409

    if not registry._set_user_public_key(store.user_id, public_key):
        return jsonify({"error": "user not found"}), 404

    print(f"[users] updated public_key for {store.user_id} fpr={core_envelope._content_public_key_fingerprint(public_key)}")
    return jsonify({
        "ok": True,
        "status": "updated",
        "user_id": store.user_id,
        "public_key_fpr": core_envelope._content_public_key_fingerprint(public_key),
        "encrypted_content": counts,
    })






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


def _swap_chat(store: "UserStore", msg_id: str, env: dict) -> str:
    with store.chat_lock:
        for msg in store.chat_messages:
            if msg.get("id") != msg_id:
                continue
            msg["v"] = int(env.get("v", 1))
            msg["body_ct"] = env["body_ct"]
            msg["nonce"] = env["nonce"]
            msg["K_user"] = env["K_user"]
            if env.get("K_enclave"):
                msg["K_enclave"] = env["K_enclave"]
            else:
                msg.pop("K_enclave", None)
            msg["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
            msg["visibility"] = env["visibility"]
            msg["owner_user_id"] = env["owner_user_id"]
            # Full-row replace: the K_enclave key may have been removed, which a
            # JSONB shallow-merge can't express, so we overwrite the whole doc.
            db.chat_append(store.user_id, msg_id, msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)
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
        m["visibility"] = env["visibility"]
        m["owner_user_id"] = env["owner_user_id"]
        return "ok"
    return "not_found"


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
    record["visibility"] = env["visibility"]
    record["owner_user_id"] = env["owner_user_id"]


def _build_rewrapped_envelope(
    store: UserStore,
    record: dict,
    *,
    api_key: str | None,
    user_pk: bytes,
    enclave_pk: bytes,
    kind: str,
) -> tuple[dict | None, str, str]:
    item_id = str(record.get("id") or "")
    if not _has_encrypted_content_record(record):
        return None, "skipped_unencrypted", ""
    if str(record.get("visibility") or "shared") != "shared":
        return None, "skipped_local_only", ""
    if not record.get("K_enclave"):
        return None, "skipped_missing_enclave_key", ""
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
        return env, "rewrapped", ""
    except Exception as e:
        return None, "error", f"envelope_build_failed:{type(e).__name__}:{str(e)}"


@bp.route("/v1/content/swap", methods=["POST"])
def content_swap():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    if not items:
        return jsonify({"results": [], "summary": _swap_summary([])})

    results: list[dict] = []
    memory_dirty = False
    moments = None

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
            results.append({"type": "memory", "id": iid, "status": status})

    if memory_dirty and moments is not None:
        memory_service._save_moments(store, moments)

    return jsonify({"results": results, "summary": _swap_summary(results)})


@bp.route("/v1/content/rewrap-to-current-key", methods=["POST"])
def content_rewrap_to_current_key():
    """Rewrap chat/memory/identity envelopes to the caller's current key.

    Recovery path for key drift: the enclave decrypts existing shared envelopes
    via K_enclave, then the backend re-encrypts the same plaintext to the
    public_key supplied by the authenticated iOS client. The user record's
    public_key is updated only after every eligible item has been verified.
    """
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    dry_raw = payload.get("dry_run", False)
    dry_run = dry_raw is True or (isinstance(dry_raw, str) and dry_raw.lower() in {"1", "true", "yes", "on"})
    requested_public_key = (payload.get("public_key") or registry._get_user_public_key(store.user_id) or "").strip()
    user_pk, err = core_envelope._decode_content_public_key(requested_public_key)
    if err or user_pk is None:
        return jsonify({"error": err or "public_key invalid"}), 400

    enclave_pk, enclave_fpr, enclave_err = core_envelope._enclave_content_public_key_material()
    if enclave_err or enclave_pk is None:
        return jsonify({"error": enclave_err or "enclave_info_unavailable"}), 503

    summary = _rewrap_summary()
    results: list[dict] = []
    identity_plan: dict | None = None
    memory_plans: list[tuple[int, dict]] = []
    chat_plans: list[tuple[str, dict]] = []

    identity = identity_service._load_identity(store)
    if identity is not None:
        env, status, reason = _build_rewrapped_envelope(
            store,
            identity,
            api_key=api_key,
            user_pk=user_pk,
            enclave_pk=enclave_pk,
            kind="identity",
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
        )
        item_id = str(moment.get("id") or "")
        results.append(_rewrap_record_result(summary, "memory", item_id, status, reason=reason))
        if env is not None:
            memory_plans.append((idx, env))

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
        )
        item_id = str(msg.get("id") or "")
        results.append(_rewrap_record_result(summary, "chat", item_id, status, reason=reason))
        if env is not None:
            chat_plans.append((item_id, env))

    response = {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "user_id": store.user_id,
        "public_key_fpr": core_envelope._content_public_key_fingerprint(user_pk),
        "enclave_pk_fpr": enclave_fpr,
        "summary": summary,
        "results": results,
    }

    if summary["total_errors"] > 0:
        response["status"] = "failed"
        response["error"] = "rewrap_failed"
        code = 200 if dry_run else 409
        print(f"[content-rewrap:{store.user_id}] failed errors={summary['total_errors']} dry_run={dry_run}")
        return jsonify(response), code

    if dry_run:
        print(f"[content-rewrap:{store.user_id}] dry_run rewrappable={summary['total_rewrapped']} skipped={summary['total_skipped']}")
        return jsonify(response)

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
        for idx, env in memory_plans:
            if 0 <= idx < len(moments) and isinstance(moments[idx], dict):
                _apply_envelope_fields(moments[idx], env)
                moments[idx]["rewrapped_at"] = now
        memory_service._save_moments(store, moments)

    swapped_ids: set[str] = set()
    for item_id, env in chat_plans:
        # _swap_chat persists the swapped envelope fields to the DB itself.
        if _swap_chat(store, item_id, env) == "ok":
            swapped_ids.add(item_id)
    if swapped_ids:
        # Stamp rewrapped_at on the affected in-memory messages and persist the
        # full updated rows (chat is row-per-message in PostgreSQL now).
        with store.chat_lock:
            for msg in store.chat_messages:
                if isinstance(msg, dict) and msg.get("id") in swapped_ids:
                    msg["rewrapped_at"] = now
                    db.chat_append(store.user_id, msg["id"], msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)

    if not registry._set_user_public_key(store.user_id, requested_public_key):
        return jsonify({"error": "user not found"}), 404

    print(f"[content-rewrap:{store.user_id}] ok rewrapped={summary['total_rewrapped']} skipped={summary['total_skipped']} fpr={response['public_key_fpr']}")
    return jsonify(response)


# ---------------------------------------------------------------------------
# Phase B — user-initiated data export + account reset.
#
# These power the "Export my data" + "Delete my data" + "Reset & re-import"
# rows in the new Settings → Privacy page. Both are user-initiated, both
# are auth-gated, and the reset path requires an explicit confirmation
# token in the body to prevent accidental wipes from a buggy client that
# holds the api_key but misbehaves.
# ---------------------------------------------------------------------------


# Cap single-shot export response size. With frames bounded to MAX_FRAMES
# (200) and each body_ct at ~200 KiB, worst-case frame payload is ~40 MiB —
# so the 80 MiB ceiling covers frames + chat + memory + identity with
# headroom. If this ever trips, switch to a streaming multipart response.
_EXPORT_MAX_BYTES = 80 * 1024 * 1024  # 80 MiB


@bp.route("/v1/content/export", methods=["GET"])
def content_export():
    """Return the caller's chat, memory, identity, and frames as one JSON blob.

    Ciphertext is returned verbatim — iOS decrypts client-side using
    the user's content_sk from Keychain. No decryption happens server-
    side, so there is no additional trust boundary crossed by this
    endpoint beyond the existing auth check.

    Frames are included as v1 envelopes (same shape as chat/memory) with
    their stored body_ct inline, so the user can walk away with the full
    screen-recording dataset decryptable only on their devices.
    """
    store = auth.require_user()
    hist = store.chat_messages
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
        return jsonify({
            "error": "export_too_large",
            "detail": "One-shot export exceeds the 80 MiB budget. Streaming"
                      " export is planned (TODO). Contact support / open an issue."
        }), 413

    resp = Response(body, mimetype="application/json")
    # Suggest a filename when clients save to disk.
    safe_name = f"feedling-export-{store.user_id}-{exported_at.replace(':', '').split('.')[0]}.json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


@bp.route("/v1/account/reset", methods=["POST"])
def account_reset():
    """Hard-delete the caller's account: wipe the user dir, revoke the
    api_key, remove the user record.

    Requires an explicit confirmation token in the body to prevent
    accidental wipes from a buggy client that holds the api_key but
    sends the wrong request. Two steps of intent (correct key + correct
    confirmation body) are needed.

    Idempotent in the safe-to-retry sense: a second call with the same
    api_key fails auth (user no longer exists) and returns 401. So
    retries are harmless; spurious wipes require a fresh registration.
    """
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "delete-all-data":
        return jsonify({
            "error": "confirmation_required",
            "detail": "POST body must include {\"confirm\": \"delete-all-data\"}."
                      " This prevents accidental resets from misbehaving clients."
        }), 400

    user_id = store.user_id

    # Remove the user record FIRST so any in-flight requests carrying the old
    # api_key fail auth immediately.
    with registry._users_lock:
        before = len(registry._users)
        registry._users[:] = [u for u in registry._users if u.get("user_id") != user_id]
        removed = before - len(registry._users)
        # Evict all cached (hash → user_id) entries pointing at this user.
        to_evict = [h for h, uid in registry._key_to_user.items() if uid == user_id]
        for h in to_evict:
            registry._key_to_user.pop(h, None)
        db.delete_user(user_id)

    # Then hard-delete all of the user's data rows (chat / memory / frames /
    # logs / blobs) and evict the cached in-memory store.
    db.delete_user_data(user_id)
    with core_store._stores_lock:
        core_store._stores.pop(user_id, None)

    # Best-effort cleanup of any residual on-disk dir (pre-migration leftovers).
    try:
        import shutil
        if (
            store.dir.exists()
            and store.dir != core_config.FEEDLING_DIR
            and store.dir.parent == core_config.FEEDLING_DIR
        ):
            shutil.rmtree(store.dir)
    except Exception as e:
        print(f"[reset:{user_id}] residual dir cleanup failed: {e}")

    print(f"[reset:{user_id}] deleted (user_record={removed})")
    return jsonify({"deleted": True, "user_id": user_id})


@bp.route("/healthz", methods=["GET"])
def healthz():
    """Liveness + readiness probe. Public, no auth — used by Docker/compose."""
    return jsonify({"ok": True, "mode": "multi_tenant"})

