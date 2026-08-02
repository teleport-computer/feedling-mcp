"""Photo capabilities — facade over backend/perception/perception_read_core.py."""
from __future__ import annotations

import base64

from perception import perception_read_core
from screen import screen_read_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(body, status, *, default_msg) -> CapabilityResult:
    if status == 200:
        data = body if isinstance(body, dict) else {"result": body}
        return ok(data=errors.cap_data(data))
    return err(errors.code_for_status(status),
               errors.message_for_body(body, default_msg),
               retryable=errors.retryable_for_status(status))


def recent(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    body, status = perception_read_core.photos_recent(store, params.get("limit"))
    return _norm(body, status, default_msg="photos unavailable")


def read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    photo_id = params.get("photo_id") or params.get("id")
    if not photo_id:
        return err(errors.INVALID, "photo read needs id", retryable=False)
    body, status = perception_read_core.photo_content(store, photo_id)
    result = _norm(body, status, default_msg="photo unavailable")
    if not result.ok or not params.get("include_image"):
        return result
    frame_id = body.get("frame_id") if isinstance(body, dict) else None
    if not frame_id:
        return result
    img = screen_read_core.frame_decrypt(store, frame_id, include_image="true",
                                         api_key=api_key, runtime_token=runtime_token)
    if img.status == 200:
        result.data = {**result.data, "image_media_type": img.media_type,
                       "has_image": img.raw_body is not None}
        if img.raw_body is not None:
            result.data["image_b64"] = base64.b64encode(img.raw_body).decode("ascii")
    return result
