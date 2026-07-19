"""Shared JSON response + fixed error-body helpers for the ASGI routers.

The error bodies mirror the Flask ``errorhandler`` bodies verbatim (plan §3.1)
so a client sees the same shape regardless of which backend served it — a
hard parity requirement under the no-fallback cutover.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

# Verbatim parity with app.py's errorhandler(401/403/503) bodies (plan §3.1).
ERROR_BODIES: dict[int, dict] = {
    401: {"error": "unauthorized"},
    403: {"error": "forbidden"},
    503: {"error": "service_unavailable", "detail": "admin token is not configured"},
}


def json_error(status_code: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


VALID_BLAME = ("user_provider", "provider_transient", "user_environment", "system")


def api_error(status: int, slug: str, *, blame: str = "", detail=None,
              request_id: str = "") -> JSONResponse:
    """统一错误信封（spec 2026-07-07-unified-error-surfacing Phase A）。

    ``slug`` 是稳定契约面（docs/API_ERRORS.md）；blame/detail/request_id 为
    增量可选字段，缺省不出现在 body——老客户端零感知。新代码与被触碰的路由
    渐进采用；存量 ``{"error": ...}`` 返回不强制迁移。"""
    if blame and blame not in VALID_BLAME:
        raise ValueError(f"invalid blame: {blame!r}")
    body: dict = {"error": slug}
    if blame:
        body["blame"] = blame
    if detail is not None:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status)
