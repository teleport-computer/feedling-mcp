"""Hosted turn parsing + background jobs: state actions, memory capture, recap, repair."""

import base64
import re
import threading
import time
from datetime import date, timedelta


import db
from core import enclave as core_enclave
from core import envelope as core_envelope
from chat import service as chat_service
from core.store import UserStore


from accounts import registry
from core import util as core_util
from hosted import image_resize
from identity import service as identity_service
from memory import actions as memory_actions_mod
from memory import service as memory_service
import provider_client
from hosted import config_store as hosted_config_store
from hosted import history_import as hosted_history_import


MODEL_API_PROVIDER_REASONING_MAX_CHARS = chat_service.MODEL_API_PROVIDER_REASONING_MAX_CHARS
# State receipts: one append per chat turn. Read views cap at 100; keep a
# generous tail so the stream can't grow without bound across a user's history.
_sanitize_visible_thinking_summary = chat_service._sanitize_visible_thinking_summary
_sanitize_provider_reasoning_text = chat_service._sanitize_provider_reasoning_text

_STATE_PENDING_BLOB = "model_api_state_pending"
_model_api_recap_active_users: set[str] = set()
_model_api_recap_active_lock = threading.Lock()
# Per-user guard for the running memory-capture job. State actions and recap
# already have one each; capture did not — overlapping capture windows (the
# turn-24 job still running when turn-48 fires) could double-write cards. Mirror
# the recap pattern: add on start, discard in the runner's single exit (finish).


def _state_pending_items(store: UserStore) -> list[dict]:
    data = db.get_blob(store.user_id, _STATE_PENDING_BLOB)
    items = data.get("items") if isinstance(data, dict) else []
    now = time.time()
    clean = [
        item for item in items
        if isinstance(item, dict) and float(item.get("expires_at") or 0) > now
    ]
    if clean != items:
        db.set_blob(store.user_id, _STATE_PENDING_BLOB, {"items": clean})
    return clean


def _load_state_receipts(store: UserStore, limit: int = 30) -> list[dict]:
    entries = db.log_read(store.user_id, "state_receipts", limit=max(1, min(limit, 100)))
    entries.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return entries


def _state_preview_value(value, max_chars: int = 260):
    if isinstance(value, list):
        out = []
        for item in value[:5]:
            text = str(item or "").strip()
            if text:
                out.append(text[:max_chars])
        return out
    if isinstance(value, dict):
        return {
            str(k): _state_preview_value(v, max_chars)
            for k, v in list(value.items())[:8]
            if str(v or "").strip()
        }
    text = str(value or "").strip()
    return text[:max_chars]


def _model_api_latest_recap_job(store: UserStore) -> dict | None:
    jobs = [
        job for job in db.log_read(store.user_id, "memory_capture_jobs", limit=60)
        if isinstance(job, dict) and str(job.get("mode") or "") == "recap"
    ]
    if not jobs:
        return None
    jobs.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
    return jobs[0]


def _model_api_recent_recap_chat(store: UserStore, api_key: str | None, limit: int = 160) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    try:
        rows = db.chat_history_page_strict(
            store.user_id, limit=max(20, min(limit, 200)))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"history_read:{type(exc).__name__}")
        rows = []
    raw_messages: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("content_type") or "text") != "text":
            continue
        try:
            raw = core_envelope.read_envelope_body(
                row, api_key, purpose="model_api_recap_history")
            item = dict(row)
            item["content"] = raw.decode("utf-8")
            raw_messages.append(item)
        except (UnicodeDecodeError, ValueError, RuntimeError) as exc:
            warnings.append(f"history_row_read:{type(exc).__name__}")
    messages: list[dict] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content") or "").strip()
        if not content or hosted_history_import._looks_like_import_artifact(content):
            continue
        role = "user" if msg.get("role") == "user" else "assistant"
        source = str(msg.get("source") or "")
        if source and source != "model_api":
            continue
        messages.append({
            "id": str(msg.get("id") or ""),
            "role": role,
            "content": content[:4000],
            "ts": msg.get("ts"),
            "source": "model_api_live",
        })
    return messages, warnings


def _model_api_plain_memory_cards(store: UserStore, api_key: str | None) -> list[dict]:
    cards: list[dict] = []
    for moment in memory_service._active_memory_moments(memory_service._load_moments(store)):
        if not isinstance(moment, dict):
            continue
        inner, _ = memory_actions_mod._memory_plain_from_envelope(moment, api_key)
        if inner is None:
            continue
        card = {
            "id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or inner.get("type") or "fact"),
            "title": str(inner.get("title") or "")[:180],
            "description": str(inner.get("description") or "")[:2000],
            "occurred_at": str(moment.get("occurred_at") or ""),
            "created_at": str(moment.get("created_at") or ""),
            "source": str(moment.get("source") or ""),
        }
        for key in ("her_quote", "context", "linked_dimension"):
            value = str(inner.get(key) or "").strip()
            if value:
                card[key] = value
        cards.append(card)
    return cards


def _memory_quality_card_issues(
    moment: dict,
    inner: dict,
    *,
    archive_language: str = "",
) -> list[str]:
    # Typed-memory cards use title/description; the v1 plaintext Garden uses
    # summary/content.  Both shapes coexist during migration and quality checks
    # must judge the actual text instead of flagging every v1 card as empty.
    title = str(inner.get("title") or inner.get("summary") or "").strip()
    desc = str(inner.get("description") or inner.get("content") or "").strip()
    context = str(inner.get("context") or "").strip()
    mem_type = str(moment.get("type") or inner.get("type") or "fact")
    issues: list[str] = []
    if hosted_history_import._GENERIC_IMPORT_TITLE_RE.match(title):
        issues.append("generic_import_title")
    if any(hosted_history_import._looks_like_import_artifact(value) for value in (title, desc, context) if value):
        issues.append("raw_import_artifact")
    if hosted_history_import._looks_like_low_value_import_card(title, desc, mem_type):
        issues.append("low_value_content")
    if str(archive_language).lower().startswith("zh") and desc and hosted_history_import._english_only_for_zh(title + "\n" + desc):
        issues.append("language_mismatch")
    return list(dict.fromkeys(issues))


def _model_api_memory_quality_scan(
    store: UserStore,
    *,
    api_key: str | None,
    max_cards: int = 1000,
    fast: bool = False,
) -> dict:
    archive_language = registry._get_user_archive_language(store.user_id) or ""
    moments = memory_service._active_memory_moments(memory_service._load_moments(store))
    scanned: list[dict] = []
    noisy: list[dict] = []
    decrypt_errors = 0
    for moment in moments[:max(1, max_cards)]:
        if not isinstance(moment, dict):
            continue
        inner, err = memory_actions_mod._memory_plain_from_envelope(moment, api_key)
        if inner is None:
            decrypt_errors += 1
            continue
        issues = _memory_quality_card_issues(moment, inner, archive_language=archive_language)
        card = {
            "id": str(moment.get("id") or ""),
            "type": str(moment.get("type") or inner.get("type") or ""),
            "title": str(inner.get("title") or "")[:180],
            "description": str(inner.get("description") or "")[:1200],
            "occurred_at": str(moment.get("occurred_at") or ""),
            "created_at": str(moment.get("created_at") or ""),
            "source": str(moment.get("source") or ""),
            "issues": issues,
        }
        scanned.append(card)
        if issues:
            noisy.append(card)

    duplicate_ids: set[str] = set()
    seen: list[tuple[str, set[str], str]] = []
    for card in scanned:
        desc = str(card.get("description") or "")
        tokens = hosted_history_import._memory_similarity_tokens(desc)
        norm = hosted_history_import._normalize_card_similarity_text(desc)
        if not norm:
            continue
        for prev_norm, prev_tokens, prev_id in seen[-250 if fast else -800:]:
            if norm == prev_norm or norm[:120] == prev_norm[:120] or hosted_history_import._token_jaccard(tokens, prev_tokens) >= 0.72:
                duplicate_ids.add(str(card.get("id") or ""))
                break
        seen.append((norm, tokens, str(card.get("id") or "")))

    by_id = {str(item.get("id") or ""): item for item in noisy}
    for card in scanned:
        cid = str(card.get("id") or "")
        if cid in duplicate_ids and cid not in by_id:
            copy_card = dict(card)
            copy_card["issues"] = list(dict.fromkeys((copy_card.get("issues") or []) + ["duplicate_or_near_duplicate"]))
            by_id[cid] = copy_card
    noisy = list(by_id.values())
    noisy.sort(key=lambda item: (len(item.get("issues") or []), item.get("created_at") or ""), reverse=True)
    issue_count = sum(len(item.get("issues") or []) for item in noisy)
    warning = None
    if noisy:
        warning = "memory_quality_warning"
    return {
        "scanned": len(scanned),
        "total_active": len(moments),
        "decrypt_errors": decrypt_errors,
        "issue_count": issue_count,
        "noisy_count": len(noisy),
        "duplicate_count": len(duplicate_ids),
        "warning": warning,
        "noisy_ids": [str(item.get("id") or "") for item in noisy if item.get("id")],
        "issues": noisy[:40],
    }


def _archive_model_api_memory_cards(
    store: UserStore,
    memory_ids: list[str],
    *,
    reason: str,
    job_id: str,
) -> int:
    target_ids = {str(mid) for mid in memory_ids if str(mid).strip()}
    if not target_ids:
        return 0
    # Hold memory_lock across load→archive→save (this background repair job is a
    # concurrent writer vs foreground memory.add — the exact collision the plain
    # Lock masked under Flask -w1). RLock lets _append_memory_change re-enter.
    archived = 0
    now = core_util._now_iso()
    with memory_service.mutation_lock(store):
        moments = memory_service._load_moments(store)
        for idx, moment in enumerate(moments):
            if not isinstance(moment, dict) or str(moment.get("id") or "") not in target_ids:
                continue
            if memory_service._memory_is_archived(moment):
                continue
            updated = dict(moment)
            updated["is_archived"] = True
            updated["archived_at"] = now
            updated["archive_reason"] = reason[:300]
            updated["archived_by_repair_job"] = job_id
            updated["updated_at"] = now
            moments[idx] = updated
            archived += 1
            memory_service._append_memory_change(store, {
                "action": "archive",
                "memory_id": str(moment.get("id") or ""),
                "type": str(moment.get("type") or ""),
                "reason": reason,
            })
        if archived:
            memory_service._save_moments(store, moments)
    return archived


def _model_api_repair_material_messages(
    store: UserStore,
    api_key: str | None,
    *,
    noisy_ids: set[str],
) -> tuple[list[dict], list[dict], list[str]]:
    messages, warnings = _model_api_recent_recap_chat(store, api_key, limit=200)
    good_cards = [
        card for card in _model_api_plain_memory_cards(store, api_key)
        if str(card.get("id") or "") not in noisy_ids
        and not _memory_quality_card_issues(
            {"type": card.get("type", ""), "source": card.get("source", "")},
            card,
            archive_language=registry._get_user_archive_language(store.user_id) or "",
        )
    ]
    support_messages: list[dict] = []
    for card in good_cards[:80]:
        support_messages.append({
            "role": "user",
            "content": (
                f"Existing readable Memory Garden card ({card.get('type')}): "
                f"{card.get('title')}\n{card.get('description')}"
            ),
            "ts": None,
            "source": "persona_import",
        })
    return support_messages + messages, good_cards, warnings


def _run_model_api_memory_repair_job(
    store: UserStore,
    api_key: str | None,
    runtime: provider_client.ProviderConfig,
    job_id: str,
    *,
    noisy_ids: list[str],
    archive_old: bool,
) -> None:
    warnings: list[str] = []
    try:
        _model_api_patch_recap_job(store, job_id, {"status": "processing", "progress": 8})
        noisy_set = {str(mid) for mid in noisy_ids if str(mid).strip()}
        messages, good_cards, material_warnings = _model_api_repair_material_messages(
            store,
            api_key,
            noisy_ids=noisy_set,
        )
        warnings.extend(material_warnings)
        if len(messages) < 4 and len(good_cards) < 2:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": "not_enough_repair_material",
                "warnings": warnings,
            })
            return

        language = hosted_history_import._import_language_for_store(store, messages)
        user_name, name_warnings = hosted_history_import._resolve_import_user_name(
            store,
            api_key,
            runtime,
            [],
        )
        warnings.extend(name_warnings)
        days = identity_service._relationship_age_days(store)
        relationship_start = date.today() - timedelta(days=max(0, days))
        windows = hosted_history_import._build_transcript_windows(messages, max_chars=14000, max_windows=8)
        target_total = max(6, min(30, len(noisy_set) or 12))
        story_target = max(2, min(10, target_total // 3))
        about_target = max(3, target_total - story_target)

        def progress(done: int, total: int, candidate_count: int) -> None:
            _model_api_patch_recap_job(store, job_id, {
                "status": "processing",
                "progress": 12 + int(50 * done / max(total, 1)),
                "candidate_windows_done": done,
                "candidate_windows_total": total,
                "candidates_extracted": candidate_count,
                "messages_reviewed": len(messages),
            })

        candidates, provider_warnings = hosted_history_import._extract_memory_candidates_with_provider(
            runtime,
            windows,
            relationship_start,
            per_window_target=4,
            language=language,
            user_name=user_name,
            on_progress=progress,
        )
        warnings.extend(provider_warnings)
        cards = hosted_history_import._render_candidates_to_memory_cards(
            candidates,
            relationship_start,
            {
                "story": story_target,
                "about_me": about_target,
                "ta_thinking": 0,
                "total": target_total,
            },
            language=language,
            max_cards=target_total,
            user_name=user_name,
        )
        cards = [
            card for card in cards
            if str(card.get("type") or "") in {"moment", "quote", "fact", "event"}
        ]
        new_cards = hosted_history_import._new_cards_only(good_cards, cards)[:target_total]
        actions = [
            {
                "type": "memory.add",
                "memory": {
                    **card,
                    "source": "model_api_repair",
                    "context": (
                        str(card.get("context") or "").strip()
                        or f"re-distilled during hosted API memory repair job {job_id}"
                    )[:1000],
                },
                "reason": "Memory repair re-distilled readable cards from existing chat/material.",
                "capture_mode": "repair",
            }
            for card in new_cards
        ]
        _model_api_patch_recap_job(store, job_id, {
            "status": "processing",
            "progress": 72,
            "candidate_cluster_count": len(hosted_history_import._merge_import_candidates(candidates)),
            "new_cards_planned": len(actions),
            "memories_planned": len(actions),
            "warnings": warnings,
        })
        if not actions:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": "no_replacement_cards_generated",
                "warnings": warnings,
            })
            return

        body, status = memory_actions_mod._execute_memory_actions(store, api_key, actions)
        effects = body.get("effects") or []
        if status >= 400:
            _model_api_patch_recap_job(store, job_id, {
                "status": "failed",
                "progress": 100,
                "error": body.get("error", "memory_action_failed"),
                "effects": effects,
                "warnings": warnings,
            })
            return
        failed_count = int(body.get("failed_count") or 0)
        applied_count = int(body.get("applied_count") or 0)
        if failed_count:
            warnings.append(
                f"{failed_count} replacement memory action(s) were rejected"
            )
        archived = 0
        # Never archive the old set after a partial replacement batch: doing so
        # would turn isolated item rejection into real memory loss.
        if archive_old and failed_count == 0:
            archived = _archive_model_api_memory_cards(
                store,
                list(noisy_set),
                reason="Archived by hosted API memory repair after replacement cards were written.",
                job_id=job_id,
            )
        _model_api_patch_recap_job(store, job_id, {
            "status": "completed",
            "progress": 100,
            "actions_planned": len(actions),
            "actions_written": applied_count,
            "actions_failed": failed_count,
            "new_cards_created": applied_count,
            "memories_created": applied_count,
            "old_cards_archived": archived,
            "effects": effects,
            "warnings": warnings,
        })
        hosted_config_store._patch_model_api_runtime_profile(store, {
            "last_repair_at": core_util._now_iso(),
            "memory_quality_warning": None if archived else "memory_quality_warning",
        })
    except Exception as e:
        _model_api_patch_recap_job(store, job_id, {
            "status": "failed",
            "progress": 100,
            "error": f"{type(e).__name__}:{str(e)[:300]}",
            "warnings": warnings,
        })


def _model_api_patch_recap_job(store: UserStore, job_id: str, patch: dict, *, only_if_status: str | None = None) -> dict | None:
    merged = dict(patch)
    if patch.get("status") in {"completed", "failed", "skipped"}:
        merged.setdefault("completed_at", core_util._now_iso())
    return db.log_patch_item(store.user_id, "memory_capture_jobs", job_id, merged, only_if_status=only_if_status)


MODEL_API_MAX_IMAGE_BYTES = 2_000_000
# Hard pre-downscale upload ceiling — decode this much before resizing, matching the
# file cap / iOS client cap. Guards against decompression-bomb-sized uploads.
MODEL_API_MAX_IMAGE_UPLOAD_BYTES = 26_214_400  # 25 MiB

MODEL_API_MAX_FILE_BYTES = 26_214_400  # 25 MiB — MUST be >= iOS client cap (ChatEmptyStateView 25*1024*1024)

_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
_DOC_EXTS = {"pdf", "docx", "xlsx"}
_DEFAULT_DOC_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _file_ext(name: str) -> str:
    _, _, ext = name.rpartition(".")
    return ext.lower() if "." in name else ""


def _display_file_name(raw: str) -> str:
    # Keep the user-facing name (incl. CJK); strip path components + control
    # chars only. The fs-safe ASCII clean happens later in the consumer when it
    # builds an on-disk path — do NOT ascii-ize here or 《离职协议.docx》 → ___.docx.
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in ('\n', '\r', '\t'))
    base = base.strip().strip(".") or "file"
    return base[:120]


def _normalize_image_mime(mime: str, ext: str) -> str | None:
    m = mime.lower()
    if m in ("image/jpeg", "image/jpg"):
        return "image/jpeg"
    if m in ("image/png", "image/webp", "image/gif"):
        return m
    by_ext = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
              "webp": "image/webp", "gif": "image/gif"}
    return by_ext.get(ext)  # None for heic and anything unsupported


def _looks_like_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _model_api_file_payload(payload: dict) -> tuple[dict | None, tuple[dict, int] | None]:
    raw = str(payload.get("file_b64") or "").strip()
    if not raw:
        return None, None
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
        raw = raw.strip()
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None, ({"error": "invalid_file", "detail": "file_b64 must be valid base64"}, 400)
    if not data:
        return None, ({"error": "invalid_file", "detail": "file_b64 must not be empty"}, 400)
    if len(data) > MODEL_API_MAX_FILE_BYTES:
        return None, ({"error": "payload_too_large", "detail": "file too large",
                       "max_bytes": MODEL_API_MAX_FILE_BYTES}, 413)
    name = _display_file_name(str(payload.get("file_name") or "").strip())
    mime = str(payload.get("file_mime") or "").strip().lower()
    ext = _file_ext(name)

    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        img_mime = _normalize_image_mime(mime, ext)
        if img_mime is None:
            return None, ({"error": "unsupported_file_type",
                           "detail": f"image type not supported: {mime or ext or 'unknown'}",
                           "hint": "convert to jpeg/png/webp/gif before sending"}, 400)
        # A file-picker image (up to the 25MB file cap) is bounded here so large
        # phone photos are stored/injected small instead of dropped to text at the
        # V2 turn. Small images pass through unchanged.
        data, img_mime = image_resize.downscale_image_if_needed(data, img_mime)
        return {"kind": "image", "bytes": data, "mime": img_mime, "name": name}, None

    if ext in _DOC_EXTS:
        return {"kind": "file", "bytes": data,
                "mime": mime or _DEFAULT_DOC_MIME[ext], "name": name}, None

    if _looks_like_text(data):
        return {"kind": "file", "bytes": data, "mime": mime or "text/plain", "name": name}, None

    return None, ({"error": "unsupported_file_type",
                   "detail": f"file type not supported: {ext or mime or 'binary'}",
                   "hint": "supported: pdf, docx, xlsx, images, or utf-8 text/source files"}, 400)


def _model_api_image_payload(payload: dict) -> tuple[bytes | None, str, str | None]:
    raw = str(payload.get("image_b64") or payload.get("image_base64") or "").strip()
    if not raw:
        return None, "", None
    mime = str(payload.get("image_mime") or "image/jpeg").strip() or "image/jpeg"
    if not re.match(r"^image/(jpeg|jpg|png|webp|gif)$", mime, re.I):
        return None, "", "image_mime must be image/jpeg, image/png, image/webp, or image/gif"
    if raw.startswith("data:"):
        head, _, rest = raw.partition(",")
        raw = rest.strip()
        if head.startswith("data:image/") and ";" in head:
            mime = head.removeprefix("data:").split(";", 1)[0] or mime
    try:
        image_bytes = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "", "image_b64 must be valid base64"
    if not image_bytes:
        return None, "", "image_b64 must not be empty"
    if len(image_bytes) > MODEL_API_MAX_IMAGE_UPLOAD_BYTES:
        return None, "", f"image too large; max {MODEL_API_MAX_IMAGE_UPLOAD_BYTES} bytes"
    # Bound an oversized upload (down to <= MODEL_API_MAX_IMAGE_BYTES) instead of
    # rejecting it; small images are returned unchanged. Only a genuine encode
    # failure on a still-huge image trips the post-check below.
    image_bytes, mime = image_resize.downscale_image_if_needed(image_bytes, mime)
    if len(image_bytes) > MODEL_API_MAX_IMAGE_BYTES:
        return None, "", f"image too large; max {MODEL_API_MAX_IMAGE_BYTES} bytes after downscale"
    return image_bytes, mime, None
