"""兜底 500 信封 + RequestValidationError 重塑（spec Phase A / A2）。

用独立子应用测（照 tests/test_asgi_hosted_setup.py 的 _build_asgi_app 模式）：
注册 register_exception_handlers + 两条会出错的路由。
Run:  python -m pytest tests/test_exception_handlers.py -q
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import middleware  # noqa: E402
from core.store_sections import StoreSection, StoreSectionUnavailable  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _build_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom secret detail")

    @app.get("/typed/{n}")
    async def typed(n: int):
        return {"n": n}

    @app.get("/store-section")
    async def store_section():
        try:
            raise RuntimeError("postgresql://private secret detail")
        except RuntimeError as exc:
            raise StoreSectionUnavailable(StoreSection.CHAT) from exc

    return app


def _get(path):
    async def go():
        # raise_app_exceptions=False：让 500 走 handler 而不是直接抛给测试
        transport = httpx.ASGITransport(app=_build_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(path)
    return asyncio.run(go())


def test_uncaught_exception_becomes_internal_error_envelope():
    resp = _get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_error"
    assert re.fullmatch(r"req_[0-9a-f]{8}", body["request_id"])
    # 不泄漏异常内容给客户端——detail 只进服务端日志
    assert "kaboom" not in resp.text
    # FRONTEND_ERROR_CONTRACT.md §三：500 必带 X-Request-Id 且与体内一致
    assert resp.headers.get("x-request-id") == body["request_id"]


def test_validation_error_reshaped_to_invalid_payload():
    resp = _get("/typed/not-a-number")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_payload"
    assert isinstance(body["detail"], list) and body["detail"]
    # detail 条目精简为 {loc, msg} 两键
    assert set(body["detail"][0].keys()) == {"loc", "msg"}


def test_store_section_failure_is_content_free_retryable_503():
    resp = _get("/store-section")

    assert resp.status_code == 503
    assert resp.json() == {
        "error": "store_section_unavailable",
        "section": "chat",
    }
    assert "postgresql://private" not in resp.text
    assert "secret detail" not in resp.text


def test_uncaught_exception_logged_with_same_request_id(caplog):
    import logging
    with caplog.at_level(logging.ERROR):
        resp = _get("/boom")
    rid = resp.json()["request_id"]
    assert any(rid in r.message for r in caplog.records)


def test_unhandled_with_access_log_middleware_same_rid(monkeypatch):
    """主路径：AccessLogMiddleware 设的 rid == 500 体里的 request_id == 响应头。"""
    from dataclasses import replace

    from asgi import context as asgi_context  # noqa: F401
    from asgi.settings import settings

    monkeypatch.setattr(middleware, "settings", replace(settings, access_log=True))
    app = middleware.AccessLogMiddleware(_build_app())

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get("/boom")

    resp = asyncio.run(go())
    body = resp.json()
    rids = resp.headers.get_list("x-request-id")
    assert rids == [body["request_id"]]   # 恰好一个头，且与体内一致
