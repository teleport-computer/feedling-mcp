"""The history "decrypt view": stored chat rows -> the items callers read.

Moved from enclave/routes/chat.py (T779 step 2a) so plaintext accounts can
build the same view next to the data. Nothing here decrypts or checks keys:
``read_envelope(row) -> bytes`` does that and raises ``failure_exc`` (whose
``reason`` is reported) when a row cannot be read. The enclave passes its
authenticated decrypt; a plaintext reader only validates owner and shape.
"""
from __future__ import annotations

import base64

from core import chat_images
from core import envelope as core_envelope


def attach_chat_metadata(source: dict, target: dict) -> None:
    """Carry bounded reply and voice metadata into the decrypt view."""
    for key, limit in (
        ("voice_call_id", 96),
        ("voice_turn_id", 128),
        ("voice_logical_turn_id", 128),
        ("voice_turn_status", 24),
        ("voice_superseded_by", 160),
        ("reply_to_message_id", 128),
    ):
        value = source.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= limit:
                target[key] = value
    for key in ("voice_turn_count", "voice_duration_sec"):
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[key] = value


def caption_text(m, read_envelope, errors):
    """Decrypt the optional caption envelope (user text sent alongside an
    image/file). Returns the caption string, or "" when absent/failed."""
    cap_env = core_envelope.caption_envelope_from_row(m)
    if cap_env is None:
        return ""
    try:
        return core_envelope.read_caption_envelope_text(
            cap_env,
            read_envelope,
        )
    except Exception as e:
        errors.append({"id": m.get("id"), "reason": f"caption_decrypt: {e}"})
        return ""


def history_items(messages, read_envelope, failure_exc):
    """纯同步批解密（在 to_thread 里跑）。函数体 = 旧 L1471-1546 逐字，
    唯一改动：_decrypt_envelope → envelope.decrypt_envelope、
    DecryptFailure → envelope.DecryptFailure。返回 (decrypted, errors)。"""
    decrypted = []
    errors = []
    for m in messages:
        v = int(m.get("v", 0))
        # Default to "text" for legacy messages stored before the
        # content_type field was added.
        ctype = m.get("content_type", "text")
        # v1+ envelope (v0 plaintext paths were stripped post-migration).
        if m.get("visibility") == "local_only":
            entry = {
                "id": m["id"],
                "seq": m.get("seq"),
                "role": m["role"],
                "ts": m["ts"],
                "source": m.get("source"),
                "content": None,
                "content_type": ctype,
                "v": v,
                "visibility": "local_only",
                "decrypt_status": "local_only_agent_cannot_read",
            }
            attach_chat_metadata(m, entry)
            decrypted.append(entry)
            continue

        if m.get("body_omitted"):
            # The caller asked for the transcript without the heavy bodies
            # (include_image_body=false). There is no body_ct to decrypt, so this
            # is an opt-out, NOT a decrypt failure — it must never land in
            # decrypt_errors. The caption envelope survives body omission, so the
            # user's actual question is still readable; the pixels are fetched one
            # message at a time via GET /v1/chat/messages/<id>/body.
            entry = {
                "id": m["id"],
                "seq": m.get("seq"),
                "role": m["role"],
                "ts": m["ts"],
                "source": m.get("source"),
                "content_type": ctype,
                "v": v,
                "visibility": m.get("visibility", "shared"),
                "decrypt_status": "ok",
                "body_omitted": True,
            }
            reason = m.get("body_omitted_reason")
            if reason:
                entry["body_omitted_reason"] = reason
            if ctype == "image":
                entry["content"] = caption_text(m, read_envelope, errors)
                if m.get("image_bundle_version"):
                    mimes = m.get("image_mimes") or []
                    entry["images"] = [
                        {"image_omitted": True, "image_mime": str(mime)}
                        for mime in mimes
                    ]
                    entry["image_count"] = len(entry["images"])
                else:
                    entry["image_omitted"] = True
                    entry["image_mime"] = m.get("image_mime") or "image/jpeg"
                if m.get("vision_route_id"):
                    entry["vision_route_id"] = str(m["vision_route_id"])
            elif ctype == "file":
                entry["content"] = caption_text(m, read_envelope, errors)
                entry["file_omitted"] = True
                entry["file_mime"] = m.get("file_mime") or "application/octet-stream"
                entry["file_name"] = m.get("file_name") or "file"
                if m.get("file_display_title"):
                    entry["file_display_title"] = m["file_display_title"]
                if m.get("file_display_subtitle"):
                    entry["file_display_subtitle"] = m["file_display_subtitle"]
            else:
                entry["content"] = None
            qmids = m.get("quoted_memory_ids")
            if isinstance(qmids, str) and qmids.strip():
                entry["quoted_memory_ids"] = qmids.strip()
            attach_chat_metadata(m, entry)
            decrypted.append(entry)
            continue

        try:
            plaintext = read_envelope(m)
            entry: dict = {
                "id": m["id"],
                "seq": m.get("seq"),
                "role": m["role"],
                "ts": m["ts"],
                "source": m.get("source"),
                "content_type": ctype,
                "v": v,
                "visibility": m.get("visibility", "shared"),
                "decrypt_status": "ok",
            }
            # Carry user-selected memory references (Garden「talk in chat」)
            # forward; expanded into decrypted cards in _build_context_memories.
            qmids = m.get("quoted_memory_ids")
            if isinstance(qmids, str) and qmids.strip():
                entry["quoted_memory_ids"] = qmids.strip()
            if ctype == "image":
                # Image plaintext is raw image bytes (JPEG/PNG/WebP) — surface
                # as base64 so JSON callers (vision-capable agents, iOS clients
                # with local copies) can decode and render.
                # If a caption envelope is present (user sent text alongside the
                # image), decrypt it and fill content so the agent sees the
                # user's actual question rather than an empty string.
                entry["content"] = caption_text(m, read_envelope, errors)
                if m.get("image_bundle_version"):
                    unpacked = chat_images.decode_image_bundle(plaintext)
                    entry["images"] = [
                        {
                            "image_b64": base64.b64encode(body).decode("ascii"),
                            "image_mime": mime,
                        }
                        for body, mime in unpacked
                    ]
                    entry["image_count"] = len(entry["images"])
                else:
                    entry["image_b64"] = base64.b64encode(plaintext).decode("ascii")
                    entry["image_mime"] = m.get("image_mime") or "image/jpeg"
                if m.get("vision_route_id"):
                    entry["vision_route_id"] = str(m["vision_route_id"])
            elif ctype == "file":
                # File plaintext is the raw file bytes — surface as base64 so the
                # resident consumer can land it on disk / inline it. Caption
                # (user text alongside the file) decrypts into content, mirroring
                # the image branch.
                entry["content"] = caption_text(m, read_envelope, errors)
                entry["file_b64"] = base64.b64encode(plaintext).decode("ascii")
                entry["file_mime"] = m.get("file_mime") or "application/octet-stream"
                entry["file_name"] = m.get("file_name") or "file"
                if m.get("file_display_title"):
                    entry["file_display_title"] = m["file_display_title"]
                if m.get("file_display_subtitle"):
                    entry["file_display_subtitle"] = m["file_display_subtitle"]
            else:
                entry["content"] = plaintext.decode("utf-8", errors="replace")
            attach_chat_metadata(m, entry)
            decrypted.append(entry)
        except failure_exc as e:
            # Surface the failure per-item so the agent sees partial
            # progress rather than a blanket 500 on one bad blob.
            errors.append({"id": m.get("id"), "reason": e.reason})
            entry = {
                "id": m["id"],
                "seq": m.get("seq"),
                "role": m["role"],
                "ts": m["ts"],
                "content": None,
                "content_type": ctype,
                "v": v,
                "decrypt_status": f"error: {e.reason}",
            }
            attach_chat_metadata(m, entry)
            decrypted.append(entry)

    return decrypted, errors
