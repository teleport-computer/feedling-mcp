# backend/enclave/routes/memory.py
"""memory 读侧三路由（旧 enclave_app L1302-1360 + L1601-1697）。
错误串空格拼法；解密批处理经 to_thread（spec §4）。"""

from __future__ import annotations

import json

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, backend_client, envelope, readside, state
from enclave.routes._body import read_json_payload
from enclave.routes._errors import backend_call_or_error, content_sk_or_503

router = APIRouter()


@router.post("/v1/memory/index")
async def v1_memory_index(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response
    payload = await read_json_payload(request)
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return JSONResponse({"error": "moments must be a list"}, status_code=400)
    effective_limit = readside.memory_readside_effective_limit(payload.get("limit"))
    query = str(payload.get("query") or "").strip()

    def _work():
        builder = (
            readside.build_memory_search_item
            if query
            else readside.build_memory_index_item
        )
        # Query matching depends on encrypted content.  Decrypt the bounded
        # candidate window before filtering, then apply the requested result
        # limit.  Pre-slicing here would make a match after the first N cards
        # invisible even though the backend sent it to the enclave.
        decrypt_candidates = moments if query else moments[:effective_limit]
        items, unavailable_ids = readside.decrypt_readside_items(
            decrypt_candidates, user_id or "", content_sk,
            item_builder=builder)
        items = readside.memory_index_filter_items(items, payload)
        if not bool(payload.get("include_sensitive", False)):
            items = [i for i in items if not i.get("is_sensitive")]
        items = items[:effective_limit]
        for item in items:
            item.pop("_search_content", None)
        return items, unavailable_ids

    items, unavailable_ids = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
    })


@router.post("/v1/memory/fetch")
async def v1_memory_fetch(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response
    payload = await read_json_payload(request)
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return JSONResponse({"error": "moments must be a list"}, status_code=400)
    effective_limit = readside.memory_readside_effective_limit(payload.get("limit"))

    def _work():
        items, unavailable_ids = readside.decrypt_readside_items(
            moments[:effective_limit], user_id or "", content_sk,
            item_builder=readside.build_memory_fetch_item)
        blocked_sensitive_ids: list[str] = []
        if not bool(payload.get("include_sensitive", False)):
            allowed = []
            for item in items:
                if item.get("is_sensitive"):
                    blocked_sensitive_ids.append(str(item.get("id") or ""))
                else:
                    allowed.append(item)
            items = allowed
        return items, unavailable_ids, blocked_sensitive_ids

    items, unavailable_ids, blocked_sensitive_ids = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
        "blocked_sensitive_ids": [mid for mid in blocked_sensitive_ids if mid],
    })


# HEAD 显式声明（同 frames.py）：Flask 自动给 GET 挂 HEAD，FastAPI 不会。
@router.api_route("/v1/memory/list", methods=["GET", "HEAD"])
async def v1_memory_list(request: Request):
    """Decrypt-and-serve memory garden for the authenticated user.

    Query params:
      since (ISO string, optional): pass-through to /v1/memory/list
      limit (int, default 50, max 200)
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    limit = request.query_params.get("limit", "50")
    since = request.query_params.get("since", "")
    params = {"limit": limit}
    if since:
        params["since"] = since
    listing, err_response = await backend_call_or_error(
        backend_client.backend_get(
            "/v1/memory/list", ctx.forward_headers, params=params))
    if err_response is not None:
        return err_response

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    def _work():
        decrypted = []
        errors = []
        for m in listing.get("moments", []):
            v = int(m.get("v", 0))
            base = {
                "id": m["id"],
                "occurred_at": m.get("occurred_at"),
                "created_at": m.get("created_at"),
                "source": m.get("source"),
                "v": v,
            }
            if m.get("visibility") == "local_only":
                base.update({
                    "title": None, "description": None, "type": None,
                    "visibility": "local_only",
                    "decrypt_status": "local_only_agent_cannot_read",
                })
                decrypted.append(base)
                continue
            try:
                plaintext = envelope.decrypt_envelope(m, user_id, content_sk)
                inner = json.loads(plaintext.decode("utf-8"))
                base.update({
                    "title": inner.get("title"),
                    "description": inner.get("description"),
                    "type": inner.get("type"),
                    "visibility": m.get("visibility", "shared"),
                    "decrypt_status": "ok",
                })
            except (envelope.DecryptFailure, json.JSONDecodeError) as e:
                reason = e.reason if isinstance(e, envelope.DecryptFailure) else f"json: {e}"
                errors.append({"id": m.get("id"), "reason": reason})
                base.update({
                    "title": None, "description": None, "type": None,
                    "decrypt_status": f"error: {reason}",
                })
            decrypted.append(base)
        return decrypted, errors

    decrypted, errors = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "moments": decrypted,
        "total": listing.get("total", len(decrypted)),
        "decrypt_errors": errors,
    })
