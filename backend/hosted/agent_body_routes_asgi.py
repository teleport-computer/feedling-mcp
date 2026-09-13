"""API-key-only synchronous agent body generation with an HTTP wall-clock bound."""
import anyio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import deps, http as asgi_http, threadpool
from hosted import agent_body_core
from chat import consumer as chat_consumer

router = APIRouter()


@router.post("/v1/agent-body/generate")
async def agent_body_generate(request: Request, auth: AuthResult = Depends(deps.require_api_key)):
    generation = agent_body_core.Generation()
    try:
        with anyio.fail_after(max(0.001, generation.remaining())):
            payload = await asgi_http.read_json_silent(request)
            body, status = await threadpool.run_db_bounded(
                agent_body_core.generate, auth.store, payload,
                caller_api_key=auth.api_key, generation=generation,
                timeout_seconds=max(0.001, generation.remaining()),
            )
    except TimeoutError:
        body, status = agent_body_core.timeout_result()
    # trace_event only enqueues (no database/network I/O). Do not rejoin a
    # potentially saturated DB threadpool after the HTTP deadline has fired.
    agent_body_core.record_finished(auth.store, generation, body, status)
    return JSONResponse(body, status_code=status)


@router.post("/v1/internal/agent-body/generate/result")
async def agent_body_result(request: Request, auth: AuthResult = Depends(deps.require_auth)):
    payload = await asgi_http.read_json_silent(request)
    consumer_info = chat_consumer._consumer_headers_from_map(
        request.headers, request.client.host if request.client else "")
    body, status = await threadpool.run_db(
        chat_consumer.complete_agent_body_job, auth.store, payload, consumer_info)
    return JSONResponse(body, status_code=status)


def register_asgi(app):
    app.include_router(router)
