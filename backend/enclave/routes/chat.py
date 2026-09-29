# backend/enclave/routes/chat.py
"""GET /v1/chat/history —— decrypt-and-serve 聊天史 + context_memories。
旧 enclave_app L1404-1598 的 async 重写：auth/拉取在事件循环，
解密批处理 + context_memories 组装整体在 to_thread（spec §4）。
错误串空格拼法（resolve_read_caller 统一处理）。"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from memory import recall_select
from core import history_view
from enclave import auth, backend_client, envelope, readside, recall_hybrid
from enclave.routes._errors import backend_call_or_error, content_sk_or_503
from enclave.routes._json import json_response_offthread

router = APIRouter()
_QUOTED_MEMORY_MAX = 8


def _decrypt_history_items(messages, authorized_user_id, content_sk):
    """纯同步批解密（在 to_thread 里跑）：在 enclave 里用本用户的 content_sk 读每一行，
    组视图交给 core.history_view（T779 第 2a 步抽出，逻辑不变）。返回 (decrypted, errors)。"""
    return history_view.history_items(
        messages,
        lambda env: envelope.read_envelope(env, authorized_user_id, content_sk),
        envelope.DecryptFailure,
    )


def _quoted_memory_ids(messages: list[dict]) -> list[str]:
    """Collect unique Garden references without inspecting message bodies."""
    wanted: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        for memory_id in str(message.get("quoted_memory_ids") or "").split(",")[
            :_QUOTED_MEMORY_MAX
        ]:
            memory_id = memory_id.strip()
            if memory_id and memory_id not in wanted:
                wanted.append(memory_id)
    return wanted


def _attach_quoted_memories(decrypted: list[dict], cards: list[dict]) -> None:
    """Expand user-selected memory ids (Garden「talk in chat」) into decrypted
    cards on their own message, so the resident consumer can inject them into
    the agent's context. Mutates `decrypted` in place; best-effort. The raw id
    list is removed from each entry so it never leaks in the response.  Every
    referenced message also receives a content-free status shape.  Consumers
    use it to show the model a generic "unavailable" note rather than silently
    pretending that a deleted, replaced, unreadable, or failed-to-fetch card was
    never selected.
    """
    by_id = {str(c.get("id") or ""): c for c in cards if c.get("id")}
    for entry in decrypted:
        raw = entry.pop("quoted_memory_ids", None)
        if not raw:
            continue
        requested = [
            i.strip() for i in str(raw).split(",") if i.strip()
        ][:_QUOTED_MEMORY_MAX]
        quoted: list[dict] = []
        for mid in requested:
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
        entry["quoted_memory_status"] = {
            "requested": len(requested),
            "resolved": len(quoted),
            "unavailable": max(0, len(requested) - len(quoted)),
        }


def _build_context_memories(moments, decrypted, query_args):
    """纯同步 context_memories 选择（在 to_thread 里跑）：在 enclave 里读卡，
    挑卡交给 memory.recall_select（T779 第 1 步抽出，逻辑不变）。
    返回 (context_memories, context_memory_trace, context_memory_log)。"""
    inner: dict | None = None
    if query_args.get("hybrid") is None:
        cards = readside.moments_to_cards(
            moments, query_args["authorized_user_id"], query_args["content_sk"])
    else:
        inner = {}
        cards = readside.moments_to_cards(
            moments, query_args["authorized_user_id"], query_args["content_sk"],
            inner_out=inner)
    return recall_select.select_context_memories(
        cards, decrypted, query_args, inner=inner, encoder=recall_hybrid)


# HEAD 显式声明（同 frames.py）：Flask 自动给 GET 挂 HEAD，FastAPI 不会；
# 体由外层 HeadBodyStripMiddleware 剥掉。
@router.api_route("/v1/chat/history", methods=["GET", "HEAD"])
async def v1_chat_history(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    limit = request.query_params.get("limit", "200")
    params = {"limit": limit}
    # Sequence cursors are the lossless pagination contract: unlike timestamps,
    # they cannot skip siblings appended with the same client clock value. Keep
    # forwarding the timestamp cursors for older app builds.
    for cursor_name in ("since", "before", "after_seq", "before_seq"):
        cursor_value = request.query_params.get(cursor_name)
        if cursor_value is not None:
            params[cursor_name] = cursor_value
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
    quoted_fetch_task: asyncio.Task | None = None
    # Hybrid recall (T523) is off unless FEEDLING_MEMORY_RECALL_HYBRID is set;
    # while off this stays None and the path below is unchanged.
    hybrid_state: dict | None = None
    query_args: dict | None = None
    context_setup_error: Exception | None = None
    # Decrypt-health probes (the resident consumer fires one every
    # DECRYPT_HEALTH_REFRESH_SEC) only need the decrypt round-trip above to have
    # succeeded — they read the HTTP status, never the body. Skip the context
    # fan-out for them: on a normal read it costs an extra configured
    # memory/list page plus _build_context_memories on the enclave's single busy
    # path, tripling the
    # probe's weight for nothing. iOS / model_api callers never set probe, so
    # their context is unchanged.
    probe = str(request.query_params.get("probe") or "").lower() in {
        "1", "true", "yes", "on"}
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
        # context_mode/context_strict 只做 wire 兼容解析，不改变挑法。候选池仍可
        # 通过 MEMORY_READSIDE_MODEL_API_LIMIT 调整；enclave 原样转发正整数，
        # backend 对超出 memory/list 支持范围的值显式报错。
        memory_limit = readside.memory_readside_model_api_limit()
        query_args = {
            "context_mode": context_mode,
            "context_recent": str(request.query_params.get("context_recent") or "").lower()
                              in {"1", "true", "yes", "on"},
            "want_trace": want_trace,
            "authorized_user_id": user_id,
            "content_sk": content_sk,
        }
        if not probe:
            listing_task = asyncio.create_task(backend_client.backend_get(
                "/v1/memory/list", ctx.forward_headers,
                params={"limit": str(memory_limit)}))
            if recall_hybrid.enabled():
                hybrid_state = recall_hybrid.begin(recall_select.unified_recall_enabled())
            quoted_ids = _quoted_memory_ids(hist.get("messages", []))
            if quoted_ids:
                # User-selected cards are an exact lookup, not part of the
                # ranked auto-recall candidate pool.  Both lifecycle flags are
                # intentional: an explicit Garden click authorizes the model to
                # see archived and superseded cards.
                quoted_fetch_task = asyncio.create_task(backend_client.backend_post(
                    "/v1/memory/fetch",
                    ctx.forward_headers,
                    {
                        "ids": quoted_ids,
                        "limit": len(quoted_ids),
                        "include_archived": True,
                        "include_superseded": True,
                    },
                ))
    except Exception as e:
        context_setup_error = e
        print(f"[chat/history:{user_id}] context_memories failed: {e}")

    try:
        decrypted, errors = await anyio.to_thread.run_sync(
            _decrypt_history_items, hist.get("messages", []), user_id, content_sk)
    except BaseException:
        if listing_task is not None:
            listing_task.cancel()  # 解密意外失败时不留孤儿任务
        if quoted_fetch_task is not None:
            quoted_fetch_task.cancel()
        raise

    quoted_cards: list[dict] = []
    if quoted_fetch_task is not None:
        try:
            quoted_response = await quoted_fetch_task
            quoted_cards = (
                quoted_response.get("items", [])
                if isinstance(quoted_response, dict)
                and isinstance(quoted_response.get("items"), list)
                else []
            )
        except Exception as e:
            # The chat message still goes through.  _attach_quoted_memories
            # turns the empty result into a generic, content-free unavailable
            # marker so the model does not guess what the selected card said.
            print(f"[chat/history:{user_id}] quoted memory fetch failed: {e}")
    _attach_quoted_memories(decrypted, quoted_cards)

    context_memory_log: dict | None = None
    if context_setup_error is not None:
        context_memory_log = {
            "mode": "failed",
            "error": type(context_setup_error).__name__,
            "counts": {"candidate_pool": 0, "injected": 0},
        }
    elif listing_task is not None:
        moments: list = []
        try:
            listing = await listing_task
            moments = listing.get("moments", []) or []
            if hybrid_state is not None:
                if hybrid_state["model_id"] and not hybrid_state["fallback_reason"]:
                    # Only this turn's plaintext candidates: the backend answers
                    # the intersection with the caller's eligible cards.
                    ids = recall_hybrid.plaintext_candidate_ids(moments, user_id)
                    if ids:
                        await recall_hybrid.fetch_vectors(ctx.forward_headers, hybrid_state, ids)
                    else:
                        hybrid_state["stored"] = {}
                query_args["hybrid"] = hybrid_state
            context_memories, context_memory_trace, context_memory_log = await anyio.to_thread.run_sync(
                _build_context_memories, moments, decrypted, query_args)
        except Exception as e:
            print(f"[chat/history:{user_id}] context_memories failed: {e}")
            context_memories, context_memory_trace = [], None
            # 失败也要留痕 —— 否则「这轮一张都没注入」和「挑卡整个崩了」
            # 在日志里长得一模一样，排查时分不开。
            context_memory_log = {"mode": "failed", "error": type(e).__name__,
                                  "counts": {"candidate_pool": len(moments), "injected": 0}}

    payload = {
        "user_id": user_id,
        "messages": decrypted,
        "context_memories": context_memories,
        # 内容无关的注入记录，由调用方落库（enclave 自己没有数据库）。
        "context_memory_log": context_memory_log,
        "total": hist.get("total", len(decrypted)),
        "decrypt_errors": errors,
    }
    # Sequence cursors are the lossless pagination contract. Keep them outside
    # the encrypted body but preserve them through this decrypting boundary;
    # timestamp cursors alone can skip rows that share the same timestamp.
    for key in (
        "oldest_ts",
        "latest_ts",
        "oldest_seq",
        "latest_seq",
        "has_more_older",
        "has_more_newer",
        "bodies_omitted",
        "image_bodies_omitted",
        "body_omit_inline_max",
    ):
        if key in hist:
            payload[key] = hist[key]
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
