# backend/enclave/routes/chat.py
"""GET /v1/chat/history —— decrypt-and-serve 聊天史 + context_memories。
旧 enclave_app L1404-1598 的 async 重写：auth/拉取在事件循环，
解密批处理 + context_memories 组装整体在 to_thread（spec §4）。
错误串空格拼法（resolve_read_caller 统一处理）。"""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import quote

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from context_memory_selection import (
    select_context_memories,
    select_context_memories_with_trace,
)
from enclave import auth, backend_client, envelope, readside
from enclave.routes._errors import backend_call_or_error, content_sk_or_503
from enclave.routes._json import json_response_offthread

router = APIRouter()


def _decrypt_caption(m, authorized_user_id, content_sk, errors):
    """Decrypt the optional caption envelope (user text sent alongside an
    image/file). Returns the caption string, or "" when absent/failed."""
    cap_ct = m.get("caption_body_ct")
    if not cap_ct:
        return ""
    cap_env = {
        "id": m.get("caption_id") or m.get("id"),
        "v": int(m.get("caption_v", m.get("v", 1)) or m.get("v", 1)),
        "body_ct": cap_ct,
        "nonce": m.get("caption_nonce"),
        "K_enclave": m.get("caption_K_enclave"),
        "owner_user_id": m.get("caption_owner_user_id") or m.get("owner_user_id"),
    }
    try:
        return envelope.decrypt_envelope(
            cap_env, authorized_user_id, content_sk
        ).decode("utf-8", errors="replace")
    except Exception as e:
        errors.append({"id": m.get("id"), "reason": f"caption_decrypt: {e}"})
        return ""


def _decrypt_history_items(messages, authorized_user_id, content_sk):
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
            decrypted.append({
                "id": m["id"],
                "role": m["role"],
                "ts": m["ts"],
                "source": m.get("source"),
                "content": None,
                "content_type": ctype,
                "v": v,
                "visibility": "local_only",
                "decrypt_status": "local_only_agent_cannot_read",
            })
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
                entry["content"] = _decrypt_caption(m, authorized_user_id, content_sk, errors)
                entry["image_omitted"] = True
                entry["image_mime"] = m.get("image_mime") or "image/jpeg"
            elif ctype == "file":
                entry["content"] = _decrypt_caption(m, authorized_user_id, content_sk, errors)
                entry["file_omitted"] = True
                entry["file_mime"] = m.get("file_mime") or "application/octet-stream"
                entry["file_name"] = m.get("file_name") or "file"
            else:
                entry["content"] = None
            qmids = m.get("quoted_memory_ids")
            if isinstance(qmids, str) and qmids.strip():
                entry["quoted_memory_ids"] = qmids.strip()
            decrypted.append(entry)
            continue

        try:
            plaintext = envelope.decrypt_envelope(m, authorized_user_id, content_sk)
            entry: dict = {
                "id": m["id"],
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
                entry["content"] = _decrypt_caption(m, authorized_user_id, content_sk, errors)
                entry["image_b64"] = base64.b64encode(plaintext).decode("ascii")
                entry["image_mime"] = m.get("image_mime") or "image/jpeg"
            elif ctype == "file":
                # File plaintext is the raw file bytes — surface as base64 so the
                # resident consumer can land it on disk / inline it. Caption
                # (user text alongside the file) decrypts into content, mirroring
                # the image branch.
                entry["content"] = _decrypt_caption(m, authorized_user_id, content_sk, errors)
                entry["file_b64"] = base64.b64encode(plaintext).decode("ascii")
                entry["file_mime"] = m.get("file_mime") or "application/octet-stream"
                entry["file_name"] = m.get("file_name") or "file"
            else:
                entry["content"] = plaintext.decode("utf-8", errors="replace")
            decrypted.append(entry)
        except envelope.DecryptFailure as e:
            # Surface the failure per-item so the agent sees partial
            # progress rather than a blanket 500 on one bad blob.
            errors.append({"id": m.get("id"), "reason": e.reason})
            decrypted.append({
                "id": m["id"],
                "role": m["role"],
                "ts": m["ts"],
                "content": None,
                "content_type": ctype,
                "v": v,
                "decrypt_status": f"error: {e.reason}",
            })

    return decrypted, errors


def _attach_quoted_memories(decrypted: list[dict], cards: list[dict]) -> None:
    """Expand user-selected memory ids (Garden「talk in chat」) into decrypted
    cards on their own message, so the resident consumer can inject them into
    the agent's context. Mutates `decrypted` in place; best-effort. The raw id
    list is removed from each entry so it never leaks in the response.
    """
    by_id = {str(c.get("id") or ""): c for c in cards if c.get("id")}
    for entry in decrypted:
        raw = entry.pop("quoted_memory_ids", None)
        if not raw:
            continue
        quoted: list[dict] = []
        for mid in [i.strip() for i in str(raw).split(",") if i.strip()][:8]:
            card = by_id.get(mid)
            if not card:
                continue
            title = str(card.get("title") or "").strip()
            desc = str(card.get("description") or "").strip()
            summary = str(card.get("summary") or "").strip()
            content = str(card.get("content") or "").strip()
            # Prefer title+description; fall back to v1 summary/content, which is
            # where many memories actually keep their text (title/description
            # empty). Mirrors the iOS displayTitle fallback so both ends agree.
            text = "\n".join(part for part in (title, desc) if part) or summary or content
            quoted.append({
                "id": mid,
                "type": str(card.get("type") or "").strip(),
                "title": title or summary or content,
                "text": text,
            })
        if quoted:
            entry["quoted_memories"] = quoted


def _build_context_memories(moments, decrypted, query_args):
    """纯同步 context_memories 选择（在 to_thread 里跑）。函数体 = 旧
    L1554-1585 逐字：latest_user_text 从 decrypted 提取，context_mode/
    want_trace 已由路由层预解析进 query_args dict（不能跨线程读
    request.query_params）。_load_decrypted_moments 的解密部分 →
    readside.moments_to_cards(moments, ...)（拉取已上移到路由层）。
    返回 (context_memories, context_memory_trace | None)。"""
    latest_user_text = ""
    for m in reversed(decrypted):
        if m.get("role") == "user" and m.get("content"):
            latest_user_text = m["content"]
            break

    context_mode = query_args["context_mode"]
    want_trace = query_args["want_trace"]
    use_readside = query_args["use_readside"]

    context_memories: list[dict] = []
    context_memory_trace: dict | None = None

    cards = readside.moments_to_cards(
        moments, query_args["authorized_user_id"], query_args["content_sk"])

    # Expand any user-selected memory references (Garden「talk in chat」) onto
    # their message using the already-decrypted cards. Best-effort side pass;
    # does not affect the context_memories selection below.
    _attach_quoted_memories(decrypted, cards)

    if use_readside:
        context_memories, context_memory_trace = readside.select_context_memories_via_readside(
            cards,
            latest_user_text,
            cap=8,
        )
        if not want_trace:
            context_memory_trace = None
    elif want_trace:
        context_memories, context_memory_trace = select_context_memories_with_trace(
            cards,
            latest_user_text,
            mode=context_mode,
        )
    else:
        context_memories = select_context_memories(cards, latest_user_text, mode=context_mode)

    return context_memories, context_memory_trace


# HEAD 显式声明（同 frames.py）：Flask 自动给 GET 挂 HEAD，FastAPI 不会；
# 体由外层 HeadBodyStripMiddleware 剥掉。
@router.api_route("/v1/chat/history", methods=["GET", "HEAD"])
async def v1_chat_history(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    since = request.query_params.get("since", "0")
    limit = request.query_params.get("limit", "200")
    params = {"since": since, "limit": limit}
    # Forward the body opt-out. Dropping it (the old behaviour) forced every
    # caller to take the image bodies: a window holding a handful of 1.4MB photos
    # serialized to a multi-MB response that the CVM egress truncated mid-body,
    # and the resident then skipped the whole cycle — so the cursor never moved
    # and the next window was guaranteed to contain the same images again.
    include_image_body = request.query_params.get(
        "include_image_body", request.query_params.get("include_image_bodies"))
    if include_image_body is not None:
        params["include_image_body"] = include_image_body
    hist, err_response = await backend_call_or_error(
        backend_client.backend_get(
            "/v1/chat/history", ctx.forward_headers, params=params))
    if err_response is not None:
        return err_response

    # Reconstruct content_sk here — we cached only the pubkey on boot, the
    # privkey is always in-memory under state but we didn't store it.
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    # Attach context_memories — up to 8 plaintext memory cards selected
    # for this conversation moment. Best-effort: if anything fails, return
    # the chat response without them rather than 500-ing (旧 L1548-1587)。
    # /v1/memory/list 拉取不依赖 history 解密结果，在解密进 to_thread 之前
    # 先发起，与解密并行——省掉旧同步实现每请求串行多付的一次 backend RTT。
    context_memories: list = []
    context_memory_trace: dict | None = None
    listing_task: asyncio.Task | None = None
    query_args: dict | None = None
    try:
        context_mode = str(
            request.query_params.get("context_mode")
            or request.query_params.get("contextMode")
            or ""
        ).strip()
        if not context_mode and str(
            request.query_params.get("context_strict") or ""
        ).lower() in {"1", "true", "yes", "on"}:
            context_mode = "strict"
        want_trace = str(
            request.query_params.get("context_trace") or ""
        ).lower() in {"1", "true", "yes", "on"}
        use_readside = (
            context_mode == "model_api"
            and readside.memory_readside_for_model_api_enabled()
        )
        memory_limit = (
            readside.memory_readside_model_api_limit() if use_readside else 200
        )
        query_args = {
            "context_mode": context_mode,
            "want_trace": want_trace,
            "use_readside": use_readside,
            "authorized_user_id": user_id,
            "content_sk": content_sk,
        }
        listing_task = asyncio.create_task(backend_client.backend_get(
            "/v1/memory/list", ctx.forward_headers,
            params={"limit": str(memory_limit)}))
    except Exception as e:
        print(f"[chat/history:{user_id}] context_memories failed: {e}")

    try:
        decrypted, errors = await anyio.to_thread.run_sync(
            _decrypt_history_items, hist.get("messages", []), user_id, content_sk)
    except BaseException:
        if listing_task is not None:
            listing_task.cancel()  # 解密意外失败时不留孤儿任务
        raise

    if listing_task is not None:
        try:
            listing = await listing_task
            moments = listing.get("moments", []) or []
            context_memories, context_memory_trace = await anyio.to_thread.run_sync(
                _build_context_memories, moments, decrypted, query_args)
        except Exception as e:
            print(f"[chat/history:{user_id}] context_memories failed: {e}")
            context_memories, context_memory_trace = [], None

    payload = {
        "user_id": user_id,
        "messages": decrypted,
        "context_memories": context_memories,
        "total": hist.get("total", len(decrypted)),
        "decrypt_errors": errors,
    }
    if context_memory_trace is not None:
        payload["context_memory_trace"] = context_memory_trace
    # 图片聊天史 payload 可达数 MB（image_b64）——json.dumps 离事件循环
    return await json_response_offthread(payload)


@router.api_route("/v1/chat/messages/{message_id}/body", methods=["GET", "HEAD"])
async def v1_chat_message_body(message_id: str, request: Request):
    """Decrypt ONE message body — the bounded counterpart to /v1/chat/history.

    History with include_image_body=false gives the resident the text transcript
    at a few KB; it then pulls pixels through here, one message per request, so a
    single response can never exceed one image (the ingest cap is 2MB). Batching
    the bodies back into the window is what let a wedged transcript grow without
    bound: five stuck images meant a 4.4MB response, the CVM egress cut it off
    mid-body, the resident skipped the cycle, the cursor stalled, and the window
    kept the same images forever. One image per request cannot wedge — a body that
    fails to arrive degrades that one turn (the resident routes its honest
    image-unavailable prompt) while every other message still advances.
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    resp, err_response = await backend_call_or_error(
        backend_client.backend_get(
            f"/v1/chat/messages/{quote(message_id, safe='')}/body",
            ctx.forward_headers),
        not_found_error="message_not_found")
    if err_response is not None:
        return err_response

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    msg = (resp or {}).get("message")
    if not isinstance(msg, dict):
        return JSONResponse({"error": "message_not_found"}, status_code=404)

    # Reuse the batch decryptor on a one-item list: identical per-item semantics
    # (caption handling, image/file branches, per-item DecryptFailure downgrade)
    # with no second copy of the envelope logic to keep in sync.
    decrypted, errors = await anyio.to_thread.run_sync(
        _decrypt_history_items, [msg], user_id, content_sk)

    return await json_response_offthread(
        {"message": decrypted[0], "decrypt_errors": errors})
