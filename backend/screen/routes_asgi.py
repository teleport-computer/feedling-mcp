"""Native ASGI screen surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/screen/*`` + ``/v1/sources`` routes: each requires an
authenticated user (``Depends(require_auth)`` — the ASGI equivalent of
``auth.require_user()``) and delegates to the framework-neutral
``screen.screen_read_core`` so the response bodies/bytes are identical to Flask's.

Auth/scope: the Flask routes gate on ``auth.require_user()`` only — none call
``runtime_auth.authorize_scope(...)`` — so there is deliberately NO
``require_scope`` here; adding one would diverge from the Flask surface.

E2E boundary: frames are v1 E2E envelopes, never decrypted server-side.
``<filename>`` / ``/envelope`` return the opaque ciphertext envelope verbatim.
``/decrypt`` + ``/image`` proxy to the enclave (which owns decryption): the
caller's credential is forwarded exactly as Flask forwarded it — the raw
``X-Feedling-Runtime-Token`` header (which wins) and ``auth.api_key`` (the same
value the api-key path would extract). No plaintext is produced in this process.

All store / DB / enclave work is blocking, so it runs off the event loop via
``threadpool.run_db`` (plan §5.2). Non-JSON responses (opaque envelope bytes,
proxied image/decrypt bytes) are rendered as raw ``Response`` objects carrying
the exact media type + headers + status the core returns.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from screen import screen_read_core
from screen.screen_read_core import ScreenResult

router = APIRouter()


def _render(result: ScreenResult):
    """Render a neutral ``ScreenResult`` to a FastAPI response, matching Flask."""
    if result.raw_body is not None:
        return Response(
            content=result.raw_body,
            status_code=result.status,
            media_type=result.media_type,
            headers=result.headers or None,
        )
    return JSONResponse(result.json_body, status_code=result.status)


@router.get("/v1/screen/ios")
async def get_ios(request: Request, auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(
        screen_read_core.ios_data, auth.store, request.query_params.get("window_sec"))
    return _render(result)


@router.get("/v1/screen/mac")
async def get_mac(auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(screen_read_core.mac_data, auth.store)
    return _render(result)


@router.get("/v1/screen/summary")
async def get_summary(auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(screen_read_core.summary_data, auth.store)
    return _render(result)


@router.get("/v1/sources")
async def get_sources(auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(screen_read_core.sources_data, auth.store)
    return _render(result)


@router.get("/v1/screen/frames")
async def list_frames(request: Request, auth: AuthResult = Depends(require_auth)):
    """List encrypted frame metadata and content-free screen-share health.

    ``screen_share`` is live when the latest frame is recent, stalled when a
    fresh device broadcast-on report has no frame update for over five minutes,
    and otherwise an empty object. A stalled result includes restart guidance
    and never treats the old frame as current screen content.
    """
    result = await threadpool.run_db(
        screen_read_core.list_frames, auth.store, request.query_params.get("limit"))
    return _render(result)


@router.get("/v1/screen/frames/latest")
async def latest_frame(auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(screen_read_core.latest_frame, auth.store)
    return _render(result)


@router.get("/v1/screen/frames/{filename}")
async def serve_frame(filename: str, auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(screen_read_core.serve_frame, auth.store, filename)
    return _render(result)


@router.get("/v1/screen/frames/{frame_id}/envelope")
async def frame_envelope(frame_id: str, auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(screen_read_core.frame_envelope, auth.store, frame_id)
    return _render(result)


@router.get("/v1/screen/frames/{frame_id}/decrypt")
async def frame_decrypt(frame_id: str, request: Request, auth: AuthResult = Depends(require_auth)):
    runtime_token = auth_core.extract_runtime_token(request.headers)
    result = await threadpool.run_db(
        screen_read_core.frame_decrypt,
        auth.store,
        frame_id,
        include_image=request.query_params.get("include_image", "true"),
        api_key=auth.api_key,
        runtime_token=runtime_token,
    )
    return _render(result)


@router.get("/v1/screen/frames/{frame_id}/image")
async def frame_image(frame_id: str, request: Request, auth: AuthResult = Depends(require_auth)):
    runtime_token = auth_core.extract_runtime_token(request.headers)
    result = await threadpool.run_db(
        screen_read_core.frame_image,
        auth.store,
        frame_id,
        range_header=request.headers.get("Range"),
        api_key=auth.api_key,
        runtime_token=runtime_token,
    )
    return _render(result)


@router.get("/v1/screen/analyze")
async def analyze_screen(request: Request, auth: AuthResult = Depends(require_auth)):
    result = await threadpool.run_db(
        screen_read_core.analyze,
        auth.store,
        request.query_params.get("window_sec"),
        request.query_params.get("min_continuous_min"),
    )
    return _render(result)


def register_asgi(app) -> None:
    app.include_router(router)
