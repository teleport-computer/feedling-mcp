"""GET /v1/decrypt/selfcheck —— 不绑用户的解密自检探针（runner-shared
decrypt-health 阶段二）。

runner-shared decrypt-health 的选主 consumer 用它替代「借某个用户身份打
/v1/chat/history?probe=1」：

  1) 解密自检：优先还原开启动时以 content_pk seal 的固定明文
     （state.SELFCHECK_PLAINTEXT），用 content_sk 走真实解密引擎断言相等——比只看
     HTTP 200 更强，且能发现启动后的钥漂移（同一把钥 seal 的东西现在解不开就是钥
     变了）。启动 seal 是 best-effort，若缺失（state 里为 None）就**当场用
     content_sk 派生的公钥重新 seal 再解**，这样一次被容忍的启动 seal 失败不会
     误报成解密故障（代价只是丢掉「相对启动的钥漂移」这一项，仍验证 content_sk +
     解密引擎的真实往返）。
  2) 回环 ping：打 backend 的公开 /healthz，验证 enclave→backend 反向可达
     （正是那条 reentrant 瓶颈链路），不碰任何用户数据。**任何 HTTP 应答**（含
     backend 繁忙时的 503）都算回环可达——回环探的是「能否到达 backend」，拿到应答
     本身就证明到达了；只有传输层失败（连不上/超时）才算 loopback fail，避免
     backend 的 DB 池 2s 超时之类的负载抖动被误判成解密不可达。

鉴权：**要求一个本地可验证的 runtime token**（纯 HMAC 校验，不回后端、不绑 user），
从而真正挡掉公网匿名放大——仅带任意 X-API-Key 字符串不再放行。只持 api_key 的
keyed self-host consumer 会拿到 401，其探针侧据此回退到 /v1/chat/history?probe=1
（api_key 能服务），是软降级而非故障。设计见
docs/proposals/shared-decrypt-health-probe.md 阶段二。
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

import content_encryption
from enclave import auth, backend_client, envelope, state
from enclave.routes._errors import content_sk_or_503

router = APIRouter()


@router.get("/v1/decrypt/selfcheck")
async def decrypt_selfcheck(request: Request):
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503
        )
    # 要求一个本地可验证的 runtime token（HMAC，纯计算、不回后端、不绑 user）。
    # 只验存在会被任意 X-API-Key 字符串绕过——那是个廉价的公网放大面。
    ctx = auth.extract_auth(request)
    if not (ctx.runtime_token and auth.local_user_id_from_token(ctx.runtime_token)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    content_sk, err = await content_sk_or_503()
    if err is not None:
        return err

    # 1) 解密自检：优先还原启动时 seal 的固定明文；缺失则当场 seal-then-open。
    decrypt_ok = False
    sealed = state._state.get("decrypt_selfcheck_sealed")
    if not sealed:
        # 启动 seal 被容忍地缺失（state.py bootstrap best-effort）——当场用
        # content_sk 的公钥重新封一份，避免「没 staged」被误报成「解密坏了」。
        try:
            sealed = content_encryption.box_seal(
                state.SELFCHECK_PLAINTEXT, bytes(content_sk.public_key)
            )
        except Exception:
            sealed = None
    if sealed:
        try:
            restored = envelope.box_seal_open_hkdf(sealed, bytes(content_sk))
            decrypt_ok = restored == state.SELFCHECK_PLAINTEXT
        except Exception:
            decrypt_ok = False

    # 2) 回环 ping：enclave→backend 的公开 /healthz（无鉴权、不碰用户数据）。
    #    拿到任何 HTTP 应答（含 503 繁忙）即证明到达 backend → loopback ok；
    #    只有传输失败才算 fail。
    loopback_ok = False
    try:
        await backend_client.backend_get("/healthz", headers={})
        loopback_ok = True
    except httpx.HTTPStatusError:
        loopback_ok = True          # got a response (e.g. 503 busy) → reachable
    except Exception:
        loopback_ok = False         # transport failure → genuinely unreachable

    # 200 = 「自检确实跑完了」，结论在 body 里（decrypt/loopback 各自 ok|fail）。
    # 非 200 只留给「跑不了」的情形（not_ready 503 / unauthorized 401 /
    # key 不可用 503），这样调用方能区分「结果是坏」与「压根没测成」。
    return JSONResponse(
        {"decrypt": "ok" if decrypt_ok else "fail",
         "loopback": "ok" if loopback_ok else "fail"},
        status_code=200,
    )
