# backend/enclave/routes/worldbook.py
"""POST /v1/worldbook/match（旧 enclave_app L1363-1401 直译）。
错误串空格拼法；解密批处理经 to_thread（spec §4）。"""

from __future__ import annotations

import json

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

import worldbook_readside_core
from enclave import auth, envelope
from enclave.routes._body import read_json_payload
from enclave.routes._errors import content_sk_or_503

router = APIRouter()


@router.post("/v1/worldbook/match")
async def v1_worldbook_match(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    payload = await read_json_payload(request)
    envelopes = payload.get("world_books")
    if not isinstance(envelopes, list):
        return JSONResponse({"error": "world_books must be a list"}, status_code=400)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return JSONResponse({"error": "messages must be a list"}, status_code=400)

    def _work():
        entries: list[dict] = []
        unavailable_ids: list[str] = []
        for env in envelopes:
            if not isinstance(env, dict):
                continue
            entry_id = str(env.get("id") or "")
            if env.get("visibility") == "local_only" or (
                not env.get("K_enclave")
                and env.get("body") is None
                and env.get("body_b64") is None
            ):
                if entry_id:
                    unavailable_ids.append(entry_id)
                continue
            try:
                plaintext = envelope.read_envelope(env, user_id or "", content_sk)
                inner = json.loads(plaintext.decode("utf-8"))
                if not isinstance(inner, dict):
                    raise ValueError("world book plaintext is not an object")
            except (envelope.DecryptFailure, json.JSONDecodeError, ValueError):
                if entry_id:
                    unavailable_ids.append(entry_id)
                continue
            entries.append(inner)

        response = worldbook_readside_core.build_block(entries, messages)
        response["user_id"] = user_id
        response["unavailable_ids"] = unavailable_ids
        return response

    response = await anyio.to_thread.run_sync(_work)
    return JSONResponse(response)
