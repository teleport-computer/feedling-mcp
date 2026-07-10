"""POST /v1/storage/reencrypt-frame —— 存储层重加密（D4）。

TEE 复制器把一条帧的 v1 信封（含 body_ct）交给这里：enclave 复用现有 decrypt
路径解出明文，在 enclave 内算明文的 sha256/size，再用 KMS 派生的存储对称钥
AES-256-GCM 重加密，回吐存储密文 + 版本号 + 校验字段。**明文不出 enclave**——
只有存储密文离开。

auth 与 /v1/envelope/decrypt 同型：身份每次实时解析（whoami_live，绝不走缓存），
本地 runtime-token HMAC 校验允许（复制器就用它）。错误串沿用 envelope 路由的
下划线拼法（missing_api_key / cannot_resolve_user_id / envelope required）。"""

from __future__ import annotations

import base64
import hashlib

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, envelope, keys, state, storage_crypto
from enclave.routes._body import read_json_payload
from enclave.routes._errors import backend_call_or_error, content_sk_or_503

router = APIRouter()

# 已知存储钥版本 allowlist：未知版本直接 400，而不是替调用方悄悄派生一枚
# 新钥（钥版本是复制器与 TEE 行之间的契约字段，滚版本须两侧一起动）。
_KEY_VERSIONS = {"v1"}


@router.post("/v1/storage/reencrypt-frame")
async def v1_reencrypt_frame(request: Request):
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

    payload = await read_json_payload(request)
    env = payload.get("envelope")
    if not isinstance(env, dict):
        return JSONResponse({"error": "envelope required"}, status_code=400)
    key_version = str(payload.get("key_version") or "v1")
    if key_version not in _KEY_VERSIONS:
        return JSONResponse(
            {"error": f"unsupported key_version: {key_version} "
                      f"(supported: {', '.join(sorted(_KEY_VERSIONS))})"},
            status_code=400)

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    try:
        # Frames are 100KB+ — decrypt off the event loop (spec §4).
        plaintext = await anyio.to_thread.run_sync(
            envelope.decrypt_envelope, env, authorized_user_id, content_sk)
    except envelope.DecryptFailure as e:
        return JSONResponse({"error": f"decrypt_failed: {e.reason}"}, status_code=403)

    try:
        storage_key = await keys.get_storage_key(key_version)
    except Exception as e:  # noqa: BLE001 — dstack socket jitter → retryable 503
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)

    # sha256/size are of the PLAINTEXT, computed inside the enclave; the plaintext
    # is sealed here and only the storage ciphertext is returned.
    storage_ct = await anyio.to_thread.run_sync(
        storage_crypto.seal, storage_key, plaintext)
    return JSONResponse({
        "body_ct_storage": base64.b64encode(storage_ct).decode("ascii"),
        "key_version": key_version,
        "sha256": hashlib.sha256(plaintext).hexdigest(),
        "size": len(plaintext),
    })
