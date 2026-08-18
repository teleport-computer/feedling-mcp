# backend/enclave/routes/frames.py
"""屏幕帧 decrypt-and-serve 三路由（旧 enclave_app L1852-2154）。

/decrypt、/caption 是 Task 10/12 模式的直译：auth 经 resolve_read_caller →
backend_get 拉信封 → content_sk → to_thread 解密。/image 额外手写 Range/ETag
（send_file(conditional=True) 的 Flask 替代，spec §6）：dstack-gateway 对每条
TCP 连接限速 ~1Mbps，客户端靠并行单区间请求分块拉图，因此 HEAD 必须支持
（Starlette 的 @router.get 不会自动挂 HEAD，这里用 api_route 显式声明）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

import anyio.to_thread
import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import provider_client
from core.frame_ids import is_supported_frame_id
from enclave import auth, backend_client, config, envelope, state, visual
from enclave.routes._errors import backend_call_or_error, content_sk_or_503
from enclave.routes._json import json_response_offthread

router = APIRouter()
log = logging.getLogger("feedling.enclave.screen_frames")

def _conditional_image_response(request: Request, image_bytes: bytes,
                                 image_mime: str, frame_id: str):
    """send_file(conditional=True) 的手工替代（spec §6）：单区间 Range、
    ETag/If-None-Match→304、非法区间 416、multipart Range 回退整文件 200。
    dstack-gateway 每 TCP 连接 ~1Mbps 限速下，客户端靠并行单区间请求分块拉图。"""
    total = len(image_bytes)
    ext = visual.IMAGE_EXTENSION_BY_MIME.get(image_mime, "image")
    etag = f'"{hashlib.sha256(image_bytes).hexdigest()[:32]}"'
    base_headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "no-cache",
        "Content-Disposition": f'inline; filename="{frame_id}.{ext}"',
    }

    inm = request.headers.get("If-None-Match", "")
    if inm and etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=base_headers)

    range_header = (request.headers.get("Range") or "").strip()
    if range_header.startswith("bytes=") and "," not in range_header:
        spec_part = range_header[len("bytes="):].strip()
        start_s, _, end_s = spec_part.partition("-")
        start = end = None

        def _is_byte_pos(s: str) -> bool:
            # RFC 7233 byte-pos = 1*DIGIT。不能用裸 int()：它还吃 "+5"/"-3"
            # （partition 把 "5--3" 切成 end="-3"），畸形头必须忽略走 200
            # 全量（Werkzeug conditional=True 同行为），不是 416。
            return bool(s) and s.isascii() and s.isdigit()

        if start_s == "" and _is_byte_pos(end_s):  # 后缀式 bytes=-N
            n = int(end_s)
            if n >= 1:
                start, end = max(0, total - n), total - 1
        elif _is_byte_pos(start_s) and (end_s == "" or _is_byte_pos(end_s)):
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        if start is not None:
            if start >= total or end < start:
                return Response(status_code=416, headers={
                    **base_headers, "Content-Range": f"bytes */{total}"})
            end = min(end, total - 1)
            return Response(
                image_bytes[start:end + 1], status_code=206,
                media_type=image_mime,
                headers={**base_headers,
                         "Content-Range": f"bytes {start}-{end}/{total}"})
    return Response(image_bytes, media_type=image_mime, headers=base_headers)


async def _fetch_frame_envelope(frame_id: str, ctx: auth.AuthContext):
    """GET the frame envelope, mapping httpx errors to the shared error shape.

    Returns (env, None) or (None, JSONResponse) — caller returns the response
    directly when the second element is not None.
    """
    return await backend_call_or_error(
        backend_client.backend_get(
            f"/v1/screen/frames/{frame_id}/envelope", ctx.forward_headers),
        not_found_error="frame not found")


@router.api_route("/v1/screen/frames/{frame_id}/decrypt", methods=["GET", "HEAD"])
async def v1_frame_decrypt(frame_id: str, request: Request):
    """Decrypt a single v1 screen-frame envelope and return its plaintext.

    Query params:
      include_image (bool, default true): omit `image_b64` if false —
        helpful when the caller only wants OCR + metadata and wants to
        avoid pulling ~80-120 KB per frame.
    """
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503)

    if not is_supported_frame_id(frame_id):
        return JSONResponse({"error": "bad frame id"}, status_code=400)

    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    env, err_response = await _fetch_frame_envelope(frame_id, ctx)
    if err_response is not None:
        return err_response

    include_image = request.query_params.get("include_image", "true").lower() != "false"
    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    try:
        # Frames are 100KB+ — decrypt off the event loop (spec §4).
        plaintext = await anyio.to_thread.run_sync(
            envelope.decrypt_envelope, env, user_id, content_sk)
    except envelope.DecryptFailure as e:
        return JSONResponse({"error": f"decrypt_failed: {e.reason}"}, status_code=502)

    try:
        inner = visual.parse_visual_plaintext(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        return JSONResponse({"error": f"plaintext_parse: {e}"}, status_code=502)

    result = {
        "id": frame_id,
        "ts": inner.get("ts") or env.get("ts"),
        "app": inner.get("app"),
        "bundle": inner.get("bundle"),
        "ocr_text": inner.get("ocr_text", ""),
        "urls": inner.get("urls", []),
        "w": inner.get("w", 0),
        "h": inner.get("h", 0),
        "tier_hint": inner.get("tier_hint"),
        "v": int(env.get("v", 1)),
        "owner_user_id": user_id,
        "decrypt_status": "ok",
    }
    if include_image:
        result["image_b64"] = inner.get("image", "")
        result["image_mime"] = inner.get("image_mime") or "image/jpeg"
        if not result["image_b64"]:
            log.warning(
                "screen_frame_image_absent route=decrypt user_id=%s frame_id=%s",
                user_id,
                frame_id,
            )
    else:
        result["image_b64"] = None
        result["image_bytes_omitted"] = True
    # ~470KB image_b64 的 json.dumps 离事件循环（同 chat/history）
    return await json_response_offthread(result)


@router.api_route("/v1/screen/frames/{frame_id}/caption", methods=["GET", "HEAD"])
async def v1_frame_caption(frame_id: str, request: Request):
    """Decrypt a frame IN-ENCLAVE and return a VLM caption — never pixels.

    Query params:
      mode (str, default "caption"): "caption" returns a 1-2 sentence
        description; "full" additionally echoes back the on-device OCR text.
    """
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503)

    if not is_supported_frame_id(frame_id):
        return JSONResponse({"error": "bad frame id"}, status_code=400)

    # Read live so tests / rotated secrets take effect without a restart.
    # VLM key: deliberately NO fallback to the config.SCREEN_VLM_API_KEY module
    # constant (fail-closed: an unset key must read as empty so the route
    # returns screen_caption_unconfigured). model/base_url may fall back to
    # their defaults.
    vlm_key = os.environ.get("FEEDLING_SCREEN_VLM_API_KEY", "")
    if not vlm_key:
        return JSONResponse({"error": "screen_caption_unconfigured"}, status_code=503)

    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    env, err_response = await _fetch_frame_envelope(frame_id, ctx)
    if err_response is not None:
        return err_response

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    try:
        plaintext = await anyio.to_thread.run_sync(
            envelope.decrypt_envelope, env, user_id, content_sk)
        inner = visual.parse_visual_plaintext(plaintext)
    except envelope.DecryptFailure as e:
        return JSONResponse({"error": f"decrypt_failed: {e.reason}"}, status_code=502)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        return JSONResponse({"error": f"plaintext_parse: {e}"}, status_code=502)

    image_b64 = inner.get("image", "")
    image_mime = inner.get("image_mime") or "image/jpeg"
    ocr_text = str(inner.get("ocr_text") or "")
    mode = (request.query_params.get("mode") or "caption").lower()
    full = mode == "full"

    instruction = (
        "Describe what is on this phone screen in one or two sentences: what app "
        "or content it is, and what the user appears to be doing. Be concrete and "
        "neutral."
        + (" Then list the key visible text verbatim." if full else "")
    )
    user_content = [
        {"type": "text", "text": instruction},
        {"type": "image_url",
         "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}},
    ]
    if ocr_text:
        user_content.append(
            {"type": "text", "text": f"On-device OCR text:\n{ocr_text[:2000]}"}
        )

    model = os.environ.get("FEEDLING_SCREEN_VLM_MODEL", config.SCREEN_VLM_MODEL)
    base_url = os.environ.get("FEEDLING_SCREEN_VLM_BASE_URL", config.SCREEN_VLM_BASE_URL)
    try:
        result = await provider_client.chat_completion_async(
            provider_client.ProviderConfig(
                provider="openrouter", model=model, api_key=vlm_key, base_url=base_url,
            ),
            [{"role": "user", "content": user_content}],
            max_tokens=400 if full else 160,
            temperature=0.2,
            timeout=45.0,
        )
    except provider_client.ProviderError as e:
        return JSONResponse({"error": f"screen_caption_failed: {e}"}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"screen_caption_failed: {e}"}, status_code=502)

    out = {
        "frame_id": frame_id,
        "caption": str(result.get("reply") or "").strip(),
        "model": model,
        "decrypt_status": "ok",
    }
    if full:
        out["ocr_text"] = ocr_text[:4000]
    return JSONResponse(out)


@router.api_route("/v1/screen/frames/{frame_id}/image", methods=["GET", "HEAD"])
async def v1_frame_image(frame_id: str, request: Request):
    """Decrypt a v1 screen-frame envelope and return the raw JPEG bytes.

    Binary sibling of /decrypt. Supports HTTP Range requests, which lets a
    client fetch the image in N parallel chunks — dstack-gateway throttles
    each TCP connection to ~1 Mbps, so parallel Range fetches are much
    faster than one stream on a ~175 KB JPEG.
    """
    if not state._state["ready"]:
        return JSONResponse(
            {"error": "not_ready", "detail": state._state["error"]}, status_code=503)

    if not is_supported_frame_id(frame_id):
        return JSONResponse({"error": "bad frame id"}, status_code=400)

    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    env, err_response = await _fetch_frame_envelope(frame_id, ctx)
    if err_response is not None:
        return err_response

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    try:
        plaintext = await anyio.to_thread.run_sync(
            envelope.decrypt_envelope, env, user_id, content_sk)
    except envelope.DecryptFailure as e:
        return JSONResponse({"error": f"decrypt_failed: {e.reason}"}, status_code=502)

    try:
        inner = visual.parse_visual_plaintext(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        return JSONResponse({"error": f"plaintext_parse: {e}"}, status_code=502)

    image_b64 = inner.get("image", "")
    image_mime = inner.get("image_mime") or "image/jpeg"
    if not image_b64:
        log.warning(
            "screen_frame_image_absent route=image user_id=%s frame_id=%s",
            user_id,
            frame_id,
        )
        return JSONResponse({"error": "no image in plaintext"}, status_code=404)
    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception as e:
        return JSONResponse({"error": f"image_b64_decode: {e}"}, status_code=502)

    return _conditional_image_response(request, image_bytes, image_mime, frame_id)
