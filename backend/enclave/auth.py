# backend/enclave/auth.py
"""调用方身份解析（ASGI 版）。

旧 enclave_app 的 _extract_api_key/_caller_runtime_token/_local_user_id_from_token/
_whoami_cached 的 async 重写。语义保持（spec §2/§4）：
  - runtime token 本地 HMAC 校验是快路径（纯计算，事件循环内联）；
  - whoami 缓存 TTL 30s，仅供只读 decrypt-and-serve 路由；
  - singleflight：同 key 并发冷 miss 收敛为一次回环（asyncio.Future 版）；
  - /v1/envelope/decrypt 走 whoami_live（绝不走缓存）。
AuthContext 显式携带凭证（spec §4 硬约束）：token 不再藏在 request 全局，
所有 backend 转发都显式传 ctx.forward_headers。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

import httpx
from starlette.requests import Request

from core import runtime_token as rt_token
from enclave import backend_client, config


@dataclass(frozen=True)
class AuthContext:
    api_key: str
    runtime_token: str

    @property
    def forward_headers(self) -> dict:
        return backend_client.forward_auth_headers(self.api_key, self.runtime_token)

    @property
    def missing(self) -> bool:
        return not self.api_key and not self.runtime_token


def extract_auth(request: Request) -> AuthContext:
    """X-API-Key / Bearer / ?key=（legacy，泄漏进日志，仅兼容）+ runtime token。"""
    api_key = (request.headers.get("X-API-Key") or "").strip()
    if not api_key:
        authz = (request.headers.get("Authorization") or "").strip()
        if authz.lower().startswith("bearer "):
            api_key = authz[7:].strip()
    if not api_key:
        api_key = (request.query_params.get("key") or "").strip()
    runtime_token = (request.headers.get("X-Feedling-Runtime-Token") or "").strip()
    return AuthContext(api_key=api_key, runtime_token=runtime_token)


def local_user_id_from_token(runtime_token: str) -> str | None:
    """本地 HMAC 校验 runtime token（旧 _local_user_id_from_token 逐字语义）。
    secret 未配置 / token 无效 → None，调用方回退 backend 解析，绝不硬失败。"""
    if not (runtime_token and config.RUNTIME_TOKEN_SECRET):
        return None
    try:
        claims = rt_token.verify(config.RUNTIME_TOKEN_SECRET, runtime_token)
    except rt_token.TokenError:
        return None
    return claims.get("user_id") or None


WHOAMI_CACHE_TTL = 30.0
_whoami_cache: dict[str, tuple[float, dict]] = {}
_whoami_inflight: dict[str, asyncio.Future] = {}
# 领跑者失败时喂给等待者的哨兵：等待者见到它就回到循环头各自重试，
# 而不是收到领跑者的异常（旧线程版：leader 失败不连坐 waiter）。
_FLIGHT_FAILED = object()


def reset_cache() -> None:
    _whoami_cache.clear()
    _whoami_inflight.clear()


def _prune_whoami_cache(now: float) -> None:
    for h in [h for h, (ts, _) in _whoami_cache.items()
              if now - ts >= WHOAMI_CACHE_TTL]:
        _whoami_cache.pop(h, None)


async def whoami_live(ctx: AuthContext) -> dict:
    """每次实时解析（/v1/envelope/decrypt 专用——缓存会把刚吊销的 key 多放行
    最多 TTL 秒）。本地 token 校验允许：吊销延迟以 token TTL（≤15min）为界，
    与旧实现一致。"""
    local_uid = local_user_id_from_token(ctx.runtime_token)
    if local_uid:
        return {"user_id": local_uid}
    return await backend_client.backend_get("/v1/users/whoami", ctx.forward_headers)


async def whoami_cached(ctx: AuthContext) -> dict:
    local_uid = local_user_id_from_token(ctx.runtime_token)
    if local_uid:
        return {"user_id": local_uid}
    cred = ("rt:" + ctx.runtime_token) if ctx.runtime_token else ("ak:" + ctx.api_key)
    h = hashlib.sha256(cred.encode("utf-8")).hexdigest()

    hit = _whoami_cache.get(h)
    if hit is not None and time.monotonic() - hit[0] < WHOAMI_CACHE_TTL:
        return hit[1]

    # inflight 注册表按 (当前事件循环, h) 隔离。asyncio.Future 绑定创建它的 loop，
    # 跨 loop `await` 会抛 RuntimeError（got Future attached to a different loop）。
    # 生产是多 worker 进程（prod FEEDLING_ENCLAVE_WORKERS=4），但 _whoami_cache /
    # _whoami_inflight 都是进程本地的，每个 worker 进程内只有一个事件循环 → key 里的
    # loop 在进程内恒定，singleflight 照常把同凭证并发冷 miss 收敛为一次（代价只是
    # 每进程各付一次冷 miss，无跨进程共享）；多 loop 嵌入（如线程各自 asyncio.run）时各 loop 各自
    # 独立飞，正确性优先（跨 loop 本就无法共享同一个 Future）。缓存字典
    # _whoami_cache 是纯数据、loop 无关，仍按 h 共享。entry 在 finally 弹出，
    # 不会把 loop 引用留过一次 flight。
    loop = asyncio.get_running_loop()
    key = (loop, h)
    while True:
        inflight = _whoami_inflight.get(key)
        if inflight is None:
            break  # 无人在飞 → 竞选为领跑者
        # shield：等待者被取消不连坐领跑者的 flight。领跑者失败时 future 携带
        # 哨兵而非异常，所以这里的 CancelledError 只可能是等待者自身被取消。
        outcome = await asyncio.shield(inflight)
        if outcome is not _FLIGHT_FAILED:
            return outcome
        # 领跑者倒下 → 重查缓存后各自重试（旧线程版 waiter 在 per-key 锁上
        # 排队醒来的语义：串行地一个个重试，首个成功者回填缓存供其余复用）
        hit = _whoami_cache.get(h)
        if hit is not None and time.monotonic() - hit[0] < WHOAMI_CACHE_TTL:
            return hit[1]

    fut: asyncio.Future = loop.create_future()
    _whoami_inflight[key] = fut
    try:
        whoami = await backend_client.backend_get(
            "/v1/users/whoami", ctx.forward_headers)
        if isinstance(whoami, dict) and whoami.get("user_id"):
            now = time.monotonic()
            _whoami_cache[h] = (now, whoami)
            _prune_whoami_cache(now)
        fut.set_result(whoami)
        return whoami
    except BaseException:  # 含 CancelledError：领跑者倒下要放行等待者
        # 异常只留给领跑者自己；等待者收哨兵后各自独立重试。直接广播异常会把
        # 一次瞬时错误整批扇出（history import 同凭证并发全 502），且
        # CancelledError 会逃出路由层只捕 httpx 的 except 变成 500。
        fut.set_result(_FLIGHT_FAILED)
        raise
    finally:
        if _whoami_inflight.get(key) is fut:
            _whoami_inflight.pop(key, None)


async def resolve_read_caller(ctx: AuthContext):
    """decrypt-and-serve 读路由共享的 auth 前置（旧 _memory_readside_auth_context
    的 auth 部分）。返回 (user_id, None) 或 (None, (错误体, 状态码))。
    错误字符串为空格拼法（spec §2）——与 /v1/envelope/decrypt 的下划线拼法
    是历史上并存的两套，禁止统一。"""
    from enclave import state  # 延迟 import 避免环
    if not state._state["ready"]:
        return None, ({"error": "not_ready", "detail": state._state["error"]}, 503)
    if ctx.missing:
        return None, ({"error": "missing api_key"}, 401)
    try:
        whoami = await whoami_cached(ctx)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return None, ({"error": "unauthorized"}, 401)
        return None, ({"error": f"backend_error: {e}"}, 502)
    except httpx.HTTPError as e:
        return None, ({"error": f"backend_unreachable: {e}"}, 502)
    user_id = whoami.get("user_id", "")
    if not user_id:
        return None, ({"error": "cannot resolve user_id"}, 401)
    return user_id, None
