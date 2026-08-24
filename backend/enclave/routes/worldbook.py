# backend/enclave/routes/worldbook.py
"""POST /v1/worldbook/match（旧 enclave_app L1363-1401 直译）。
错误串空格拼法；解密批处理经 to_thread（spec §4）。"""

from __future__ import annotations

import json
from collections import OrderedDict
import hashlib
import os
import threading
import time

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

import worldbook_readside_core
from enclave import auth, envelope
from enclave.routes._body import read_json_payload
from enclave.routes._errors import content_sk_or_503

router = APIRouter()

_CACHE_TTL_SEC = max(
    0, int(os.environ.get("FEEDLING_WORLDBOOK_CACHE_TTL_SEC", "300"))
)
_CACHE_MAX_USERS = max(
    0, int(os.environ.get("FEEDLING_WORLDBOOK_CACHE_MAX_USERS", "128"))
)
_worldbook_cache: OrderedDict[
    str, tuple[float, list[dict], list[str]]
] = OrderedDict()
_worldbook_cache_lock = threading.Lock()


def _cache_key(user_id: str, envelopes: list) -> str:
    canonical = json.dumps(
        envelopes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(
        str(user_id or "").encode("utf-8") + b"\0" + canonical
    ).hexdigest()


def _decrypt_entries(
    user_id: str, envelopes: list, content_sk
) -> tuple[list[dict], list[str]]:
    key = _cache_key(user_id, envelopes)
    now = time.monotonic()
    if _CACHE_TTL_SEC > 0 and _CACHE_MAX_USERS > 0:
        with _worldbook_cache_lock:
            cached = _worldbook_cache.get(key)
            if cached is not None and now - cached[0] <= _CACHE_TTL_SEC:
                _worldbook_cache.move_to_end(key)
                return [dict(item) for item in cached[1]], list(cached[2])
            if cached is not None:
                _worldbook_cache.pop(key, None)

    entries: list[dict] = []
    unavailable_ids: list[str] = []
    for env in envelopes:
        if not isinstance(env, dict):
            continue
        entry_id = str(env.get("id") or "")
        if env.get("visibility") == "local_only" or not env.get("K_enclave"):
            if entry_id:
                unavailable_ids.append(entry_id)
            continue
        try:
            plaintext = envelope.decrypt_envelope(env, user_id or "", content_sk)
            inner = json.loads(plaintext.decode("utf-8"))
            if not isinstance(inner, dict):
                raise ValueError("world book plaintext is not an object")
        except (envelope.DecryptFailure, json.JSONDecodeError, ValueError):
            if entry_id:
                unavailable_ids.append(entry_id)
            continue
        entries.append(inner)

    if _CACHE_TTL_SEC > 0 and _CACHE_MAX_USERS > 0:
        with _worldbook_cache_lock:
            _worldbook_cache[key] = (
                now,
                [dict(item) for item in entries],
                list(unavailable_ids),
            )
            _worldbook_cache.move_to_end(key)
            while len(_worldbook_cache) > _CACHE_MAX_USERS:
                _worldbook_cache.popitem(last=False)
    return entries, unavailable_ids


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
        entries, unavailable_ids = _decrypt_entries(
            user_id or "", envelopes, content_sk
        )

        response = worldbook_readside_core.build_block(entries, messages)
        response["user_id"] = user_id
        response["unavailable_ids"] = unavailable_ids
        return response

    response = await anyio.to_thread.run_sync(_work)
    return JSONResponse(response)
