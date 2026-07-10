"""enclave FastAPI 组装（assembly-only，逻辑在各路由模块）。"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI

from enclave import backend_client, config
from enclave.routes.gzip import ContentTypeGZipMiddleware
from enclave.routes.head import HeadBodyStripMiddleware

# 每个路由任务落地时把模块名加进来（Task 9-13）。
_ROUTE_MODULES = ("health", "envelope", "memory", "worldbook", "chat", "identity",
                  "frames", "storage")


@asynccontextmanager
async def lifespan(app):
    # 解密线程池容量（spec §4）：anyio 默认全局 40 tokens；这里的池保护的是
    # 解密批处理 + 少量 dstack 阻塞调用，与主 backend 的 FEEDLING_ASGI_DB_THREADS
    # 无关，用 FEEDLING_ENCLAVE_THREADS（默认 32，env 名沿用免动 compose）。
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = config.ENCLAVE_THREADS
    yield
    import provider_client
    await backend_client.aclose()
    await provider_client.aclose_async_http_client()


def build_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # decrypt-with-image ~470KB JSON 是主要受益者；500B 阈值对齐 flask-compress 默认。
    # 用内容类型限定版而非 Starlette 自带 GZipMiddleware：后者不看
    # content-type/status，会把 /image（image/jpeg）和其 206 Range 分片也压缩，
    # 而 206 的 Content-Range 是按解压后字节算的 —— 压缩后即畸形，破坏并行分块
    # 下载（spec §6）。ContentTypeGZipMiddleware 复刻旧 flask-compress 语义：只压
    # 200 + text/JSON allowlist。
    app.add_middleware(ContentTypeGZipMiddleware, minimum_size=500)
    # 最外层：HEAD 请求剥掉响应体（保留全部头，含 gzip 后的 Content-Length）。app 层
    # 显式剥离，不再把"HEAD 不带 body"押在 uvicorn 协议层行为上（见 head.py）。
    app.add_middleware(HeadBodyStripMiddleware)
    for name in _ROUTE_MODULES:
        module = importlib.import_module(f"enclave.routes.{name}")
        app.include_router(module.router)
    return app
