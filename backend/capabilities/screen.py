"""Screen capabilities — facade over backend/screen/screen_read_core.py."""
from __future__ import annotations

import base64

from screen import screen_read_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _norm(res, *, default_msg: str) -> CapabilityResult:
    if res.status == 200:
        if res.json_body is not None:
            data = res.json_body if isinstance(res.json_body, dict) else {"result": res.json_body}
        else:
            # binary/opaque body (pixels): never inline into planner/status; meta only
            data = {"media_type": res.media_type, "has_binary": res.raw_body is not None}
        return ok(data=errors.cap_data(data))
    return err(errors.code_for_status(res.status),
               errors.message_for_body(res.json_body, default_msg),
               retryable=errors.retryable_for_status(res.status))


def recent(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    res = screen_read_core.list_frames(store, params.get("limit"))
    return _norm(res, default_msg="screen list unavailable")


def read(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    frame_id = params.get("frame_id")
    if not frame_id:
        latest = screen_read_core.latest_frame(store)
        if latest.status != 200 or not isinstance(latest.json_body, dict):
            return _norm(latest, default_msg="no screen frame")
        frame_id = latest.json_body.get("id") or latest.json_body.get("frame_id")
        if not frame_id:
            return err(errors.NOT_FOUND, "no recent screen frame", retryable=False)
    include_image = "true" if params.get("include_image") else "false"
    res = screen_read_core.frame_decrypt(store, frame_id, include_image=include_image,
                                         api_key=api_key, runtime_token=runtime_token)
    if res.status == 200 and params.get("include_image") and res.raw_body is not None:
        return ok(data={
            "frame_id": str(frame_id),
            "media_type": res.media_type,
            "has_image": True,
            "image_b64": base64.b64encode(res.raw_body).decode("ascii"),
        })
    return _norm(res, default_msg="screen read unavailable")
