"""Native ASGI hosted-setup surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``hosted.setup_routes`` blueprint: each route requires an
authenticated user (``Depends(require_auth)`` — the ASGI equivalent of
``auth.require_user()``) and delegates to the framework-neutral
``hosted.setup_core`` so the response bodies are byte-identical to Flask's.

Auth/scope: the Flask routes gate on ``auth.require_user()`` only — none call
``runtime_auth.authorize_scope(...)`` — so there is deliberately NO
``require_scope`` here; adding one would diverge from the Flask surface.

Credentials: the Flask routes that need the caller's provider credential read it
via ``auth._extract_api_key()``; the ASGI equivalent is
``auth_core.extract_api_key(headers, query_params)`` (X-API-Key / Bearer / legacy
``?key=``), forwarded to the neutral core exactly as Flask forwards it. The core
never touches ``flask.request``.

E2E boundary: ``/v1/model_api/key_envelope`` returns the caller's OWN
``api_key_envelope`` ciphertext — never decrypted server-side. ``setup`` seals a
provider key via the same envelope/enclave functions Flask calls; no server-side
decrypt is added here.

All store / enclave / provider work is blocking, so it runs off the event loop
via ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth
from core import provider_usage
from hosted import config_store
from hosted import setup_core
from chat import consumer as chat_consumer
from hosted import usage_core
from hosted import image_generator
from hosted import vision_observer

router = APIRouter()


@router.post("/v1/model_api/setup")
async def model_api_setup(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_setup, auth.store, payload, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.get("/v1/model_api/get")
async def model_api_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/driver")
async def model_api_set_hosting(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_set_hosting, auth.store)
    return JSONResponse(body, status_code=status)


@router.get("/v1/model_api/key_envelope")
async def model_api_key_envelope(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_key_envelope, auth.store)
    return JSONResponse(body, status_code=status)


@router.get("/v1/vision/config")
async def vision_config_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.vision_config_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/vision/main/test")
async def vision_main_test(request: Request, auth: AuthResult = Depends(require_auth)):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.vision_main_test,
        auth.store,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/internal/vision/main/test/result")
async def vision_main_test_result(
    request: Request,
    auth: AuthResult = Depends(require_auth),
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    consumer_info = chat_consumer._consumer_headers_from_map(
        request.headers,
        request.client.host if request.client else "",
    )
    body, status = await threadpool.run_db(
        setup_core.vision_main_test_result,
        auth.store,
        payload,
        consumer_info=consumer_info,
    )
    return JSONResponse(body, status_code=status)


@router.put("/v1/vision/config")
async def vision_config_set(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.vision_config_set,
        auth.store,
        payload,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/vision/config")
async def vision_route_configure(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.vision_route_configure,
        auth.store,
        payload,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/vision/routes/{route_id}/test")
async def vision_route_test(route_id: str, request: Request,
                            auth: AuthResult = Depends(require_auth)):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.vision_route_test,
        auth.store,
        route_id,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/vision/observe")
async def vision_observe(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    # Forward only a token already verified by require_auth. When runtime-token
    # auth is disabled, auth_core deliberately ignores the raw header; do not
    # resurrect an unverified credential for the enclave hop here.
    caller_runtime_token = (
        auth_core.extract_runtime_token(request.headers) or ""
        if auth.runtime_token_claims is not None
        else ""
    )
    body, status = await threadpool.run_db(
        vision_observer.observe_pinned_message,
        auth.store,
        payload,
        caller_api_key=caller_api_key,
        caller_runtime_token=caller_runtime_token,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/image-generation/config")
async def image_generation_config_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.image_generation_config_get,
        auth.store,
    )
    return JSONResponse(body, status_code=status)


@router.put("/v1/image-generation/config")
async def image_generation_config_set(
    request: Request,
    auth: AuthResult = Depends(require_auth),
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.image_generation_config_set,
        auth.store,
        payload,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/image-generation/config")
async def image_generation_route_configure(
    request: Request,
    auth: AuthResult = Depends(require_auth),
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.image_generation_route_configure,
        auth.store,
        payload,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/image-generation/main/test")
async def image_generation_main_test(
    request: Request,
    auth: AuthResult = Depends(require_auth),
):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.image_generation_main_test,
        auth.store,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/image-generation/routes/{route_id}/test")
async def image_generation_route_test(
    route_id: str,
    request: Request,
    auth: AuthResult = Depends(require_auth),
):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.image_generation_route_test,
        auth.store,
        route_id,
        caller_api_key=caller_api_key,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/image-generation/generate")
async def image_generation_generate(
    request: Request,
    auth: AuthResult = Depends(require_auth),
):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    caller_runtime_token = (
        auth_core.extract_runtime_token(request.headers) or ""
        if auth.runtime_token_claims is not None
        else ""
    )
    body, status = await threadpool.run_db(
        image_generator.generate_with_pinned_route,
        auth.store,
        payload,
        caller_api_key=caller_api_key,
        caller_runtime_token=caller_runtime_token,
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/test")
async def model_api_test(request: Request, auth: AuthResult = Depends(require_auth)):
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(setup_core.model_api_test, auth.store, api_key=api_key)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/model_api/delete")
async def model_api_delete(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_delete, auth.store)
    return JSONResponse(body, status_code=status)


@router.get("/v1/model_api/runtime")
async def model_api_runtime_status(request: Request, auth: AuthResult = Depends(require_auth)):
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_runtime_status, auth.store, api_key=api_key)
    return JSONResponse(body, status_code=status)


@router.get("/v1/model_api/usage")
async def model_api_usage(request: Request, auth: AuthResult = Depends(require_auth)):
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    result = await threadpool.run_db(
        usage_core.resolve_usage_config, auth.store, api_key=api_key)
    if isinstance(result, tuple):
        return JSONResponse(result[1], status_code=400)
    payload = await provider_usage.query_usage_async(result)
    return JSONResponse(payload, status_code=200)


@router.post("/v1/model_api/runtime_error")
async def model_api_runtime_error(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        config_store.record_runtime_error,
        auth.store,
        error=str(payload.get("error") or ""),
        error_class=str(payload.get("error_class") or ""),
        provider_result=str(payload.get("provider_result") or ""),
    )
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/memory/repair")
async def model_api_memory_repair(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    # No Flask app config under ASGI; production Flask has TESTING=False, so the
    # sync path is driven solely by the payload's synchronous/sync flags — matching
    # production Flask (only the Flask test harness flips config TESTING on).
    body, status = await threadpool.run_db(
        setup_core.model_api_memory_repair, auth.store, payload, api_key=api_key, testing=False)
    return JSONResponse(body, status_code=status)


@router.get("/v1/model_api/routes")
async def model_api_routes_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_routes_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/routes")
async def model_api_route_create(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_route_create, auth.store, payload, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/models")
async def model_api_models(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_models, auth.store, payload, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/routes/{route_id}/activate")
async def model_api_route_activate(route_id: str, request: Request,
                                   auth: AuthResult = Depends(require_auth)):
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_route_activate, auth.store, route_id, caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.post("/v1/model_api/routes/{route_id}/test")
async def model_api_route_test(route_id: str, request: Request,
                               auth: AuthResult = Depends(require_auth)):
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_route_test, auth.store, route_id, api_key=api_key)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/model_api/routes/{route_id}")
async def model_api_route_remove(route_id: str, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.model_api_route_remove, auth.store, route_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/model_api/credentials")
async def model_api_credentials_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(setup_core.model_api_credentials_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.patch("/v1/model_api/credentials/{credential_id}")
async def model_api_credential_patch(credential_id: str, request: Request,
                                     auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    caller_api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        setup_core.model_api_credential_patch, auth.store, credential_id, payload,
        caller_api_key=caller_api_key)
    return JSONResponse(body, status_code=status)


@router.delete("/v1/model_api/credentials/{credential_id}")
async def model_api_credential_remove(credential_id: str,
                                      auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.model_api_credential_remove, auth.store, credential_id)
    return JSONResponse(body, status_code=status)


@router.get("/v1/state/receipts")
async def state_receipts(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.state_receipts, auth.store, request.query_params.get("limit", 30))
    return JSONResponse(body, status_code=status)


@router.get("/v1/memory/capture_jobs")
async def memory_capture_jobs(request: Request, auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        setup_core.memory_capture_jobs, auth.store, request.query_params.get("limit", 30))
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)
