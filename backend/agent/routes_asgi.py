"""Native ASGI agent perception routes (ASGI-migration plan §5.3).

The FastAPI counterpart of ``agent.routes``. Each route resolves the caller via
the shared ``require_auth`` dependency, extracts the raw query params, and runs
the framework-neutral ``agent.perception_core`` builder on the bounded threadpool
(the builders call sync perception store/service functions that touch the DB, a
red-line if run on the event loop). The typed ``AgentRouteError`` a builder may
raise is mapped to the identical Flask 4xx body via ``JSONResponse``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from agent import perception_core
from asgi import threadpool
from asgi.deps import require_auth

router = APIRouter()


def _error_response(err: perception_core.AgentRouteError) -> JSONResponse:
    return JSONResponse(err.body, status_code=err.status_code)


@router.get("/v1/agent/perception")
async def agent_perception(request: Request, auth: AuthResult = Depends(require_auth)):
    try:
        return await threadpool.run_db(
            perception_core.agent_perception_payload,
            auth.store,
            signals_raw=request.query_params.get("signals"),
        )
    except perception_core.AgentRouteError as err:
        return _error_response(err)


@router.get("/v1/agent/perception/recent_apps")
async def agent_perception_recent_apps(request: Request, auth: AuthResult = Depends(require_auth)):
    try:
        return await threadpool.run_db(
            perception_core.recent_apps_payload,
            auth.store,
            limit_raw=request.query_params.get("limit"),
            hours_raw=request.query_params.get("hours"),
        )
    except perception_core.AgentRouteError as err:
        return _error_response(err)


@router.get("/v1/agent/perception/trend")
async def agent_perception_trend(request: Request, auth: AuthResult = Depends(require_auth)):
    try:
        return await threadpool.run_db(
            perception_core.perception_trend_payload,
            auth.store,
            signal_raw=request.query_params.get("signal"),
            field_raw=request.query_params.get("field"),
            days_raw=request.query_params.get("days"),
        )
    except perception_core.AgentRouteError as err:
        return _error_response(err)


@router.get("/v1/agent/perception/history")
async def agent_perception_history(request: Request, auth: AuthResult = Depends(require_auth)):
    try:
        return await threadpool.run_db(
            perception_core.perception_history_payload,
            auth.store,
            signal_raw=request.query_params.get("signal"),
            days_raw=request.query_params.get("days"),
        )
    except perception_core.AgentRouteError as err:
        return _error_response(err)


@router.get("/v1/agent/perception/digest")
async def agent_perception_digest(request: Request, auth: AuthResult = Depends(require_auth)):
    try:
        return await threadpool.run_db(
            perception_core.perception_digest_payload,
            auth.store,
            days_raw=request.query_params.get("days"),
        )
    except perception_core.AgentRouteError as err:
        return _error_response(err)


def register_asgi(app) -> None:
    app.include_router(router)
