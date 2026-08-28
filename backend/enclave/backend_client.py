"""enclave→backend 回环的进程级 async HTTP 客户端。

旧 enclave_app._http_client（同步 keep-alive 池）的 ASGI 版：同一份连接池
参数，换成 httpx.AsyncClient。单事件循环内 get_async_client 的检查-创建
无竞态（无 await 切换点）；lifespan 退出时 aclose()。

auth 转发语义（旧 _forward_auth_headers，逐字保留）：runtime token 优先，
其次 api_key，两者皆无 → 空 headers。"""

from __future__ import annotations

import httpx

from enclave import config

_client: httpx.AsyncClient | None = None


def get_async_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            # pool=None：池满时排队等空位而不是 15s 后抛 PoolTimeout。旧
            # gthread 模型 32 线程既是并发上限也是隐式准入闸（多余请求排队
            # 不报错）；ASGI 后准入闸是 limit_concurrency=2048，若池获取带
            # 超时，>100 并发的突发（history import fan-out 等）会整批 502
            # backend_unreachable。排队上限由 limit_concurrency 兜底。
            timeout=httpx.Timeout(15, pool=None),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=90.0,
            ),
        )
    return _client


async def aclose() -> None:
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()


def forward_auth_headers(api_key: str, runtime_token: str) -> dict:
    if runtime_token:
        return {"X-Feedling-Runtime-Token": runtime_token}
    if api_key:
        return {"X-API-Key": api_key}
    return {}


async def backend_get(path: str, headers: dict, params: dict | None = None) -> dict:
    """GET backend 并返回 JSON。调用方负责 httpx 异常→错误码映射
    （HTTPStatusError 401→401、其余→502；HTTPError→502，逐字沿用旧路由）。"""
    r = await get_async_client().get(
        f"{config.FLASK_URL}{path}", params=params, headers=headers
    )
    r.raise_for_status()
    return r.json()


async def backend_post(path: str, headers: dict, payload: dict) -> dict:
    """POST JSON to the backend and return its decoded response.

    Error mapping deliberately stays with the route, matching ``backend_get``.
    """
    r = await get_async_client().post(
        f"{config.FLASK_URL}{path}", json=payload, headers=headers
    )
    r.raise_for_status()
    return r.json()
