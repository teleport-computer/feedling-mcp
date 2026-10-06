"""POST /v1/envelope/decrypt —— 解开一个调用者自有的 v1 envelope。

旧 enclave_app L794-871 的 async 重写。安全语义不变：身份每次实时解析
（whoami_live，绝不走缓存）；本地 runtime-token HMAC 校验允许（吊销延迟
以 token TTL 为界）。错误串是下划线拼法（missing_api_key /
cannot_resolve_user_id）——与读路由的空格拼法是并存两套，禁止统一（spec §2）。"""

from __future__ import annotations

import base64

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, envelope, state
from enclave.routes._body import read_json_payload
from enclave.routes import _reqlog
from enclave.routes._errors import backend_call_or_error, content_sk_or_503

router = APIRouter()


@router.post("/v1/envelope/decrypt")
async def v1_envelope_decrypt(request: Request):
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503)

    ctx = auth.extract_auth(request)
    if ctx.missing:
        return JSONResponse({"error": "missing_api_key"}, status_code=401)

    whoami, err_response = await backend_call_or_error(auth.whoami_live(ctx))
    if err_response is not None:
        return err_response

    authorized_user_id = whoami.get("user_id", "")
    if not authorized_user_id:
        return JSONResponse({"error": "cannot_resolve_user_id"}, status_code=401)

    # read_json_payload 复刻旧 Flask get_json(silent=True)：content-type 非 JSON
    # （如 text/plain）、解析失败、或非对象 body 一律归一为 {} → 下方 400
    # （含有意偏差 #2：非对象 body 旧 Flask 500，这里 400）。
    payload = await read_json_payload(request)
    env = payload.get("envelope")
    if not isinstance(env, dict):
        return JSONResponse({"error": "envelope required"}, status_code=400)

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    try:
        plaintext = await _reqlog.decrypt_job(
            request, envelope.decrypt_envelope, env, authorized_user_id, content_sk)
    except envelope.DecryptFailure as e:
        return JSONResponse({"error": f"decrypt_failed: {e.reason}"}, status_code=403)

    return JSONResponse({
        "owner_user_id": authorized_user_id,
        "id": env.get("id", ""),
        "v": int(env.get("v", 1)),
        "plaintext_b64": base64.b64encode(plaintext).decode("ascii"),
    })
