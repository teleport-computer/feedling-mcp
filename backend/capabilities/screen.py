"""Screen capabilities — facade over backend/screen/screen_read_core.py."""
from __future__ import annotations

import base64
import json

from screen import screen_read_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _decode_json_proxy_body(res) -> None:
    """Expose an enclave JSON relay as JSON to the capability facade.

    ``frame_decrypt`` deliberately relays enclave bytes unchanged for the HTTP
    routes. Capability callers, however, need the decoded fields (especially
    ``image_b64``), not a base64 encoding of the entire JSON document.
    """
    if res.json_body is not None or res.raw_body is None:
        return
    if "json" not in str(res.media_type or "").lower():
        return
    try:
        res.json_body = json.loads(res.raw_body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return
    res.raw_body = None


def _image_result(data: dict) -> CapabilityResult:
    """Cap metadata while preserving pixels for the native vision bridge."""
    image_b64 = str(data.get("image_b64") or "")
    metadata = errors.cap_data(
        {k: value for k, value in data.items() if k != "image_b64"}
    )
    return ok(data={**metadata, "has_image": True, "image_b64": image_b64})


def _norm(
    res,
    *,
    default_msg: str,
    image_requested: bool | None = None,
) -> CapabilityResult:
    if res.status == 200:
        if res.json_body is not None:
            data = res.json_body if isinstance(res.json_body, dict) else {"result": res.json_body}
        else:
            # binary/opaque body (pixels): never inline into planner/status; meta only
            data = {"media_type": res.media_type, "has_binary": res.raw_body is not None}
        if image_requested is not None:
            has_image = bool(data.get("image_b64")) or bool(data.get("has_binary"))
            if not has_image:
                data = {
                    **data,
                    "image_omitted_reason": (
                        "absent_in_plaintext" if image_requested else "not_requested"
                    ),
                }
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
    if "include_image" in params:
        include_pixels = bool(params.get("include_image"))
    else:
        user_id = str(getattr(store, "user_id", "") or "")
        include_pixels = bool(
            user_id and screen_read_core.screen_share_grounding(user_id)
        )
    include_image = "true" if include_pixels else "false"
    res = screen_read_core.frame_decrypt(store, frame_id, include_image=include_image,
                                         api_key=api_key, runtime_token=runtime_token)
    _decode_json_proxy_body(res)
    if (
        res.status == 200
        and include_pixels
        and isinstance(res.json_body, dict)
        and res.json_body.get("image_b64")
    ):
        return _image_result(res.json_body)
    if res.status == 200 and include_pixels and res.raw_body is not None:
        return ok(data={
            "frame_id": str(frame_id),
            "media_type": res.media_type,
            "has_image": True,
            "image_b64": base64.b64encode(res.raw_body).decode("ascii"),
        })
    return _norm(
        res,
        default_msg="screen read unavailable",
        image_requested=include_pixels,
    )
