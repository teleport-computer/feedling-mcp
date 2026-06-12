"""Beta tracking events: /v1/track/event (content-refusing sanitizer)."""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

import db
from core.store import UserStore

from accounts import auth
from core import store as core_store
from core import util as core_util

bp = Blueprint("tracking", __name__)

_TRACK_EVENT_TYPE_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_TRACK_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|private|body_ct|k_user|k_enclave|"
    r"nonce|cipher|content|clipboard|prompt|transcript|persona|history|"
    r"filename|file_name|file|raw|text|title|url|email|phone|lat|lng|"
    r"latitude|longitude)",
    re.IGNORECASE,
)


def _safe_track_scalar(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, str):
        return value.strip()[:200]
    return None


def _sanitize_track_payload(payload, depth: int = 0) -> dict:
    """Keep beta tracking metadata useful while refusing content-like fields."""
    if not isinstance(payload, dict) or depth > 2:
        return {}
    clean: dict = {}
    for raw_key, value in payload.items():
        key = str(raw_key or "").strip()[:80]
        if not key or _TRACK_SENSITIVE_KEY_RE.search(key):
            continue
        if isinstance(value, dict):
            nested = _sanitize_track_payload(value, depth + 1)
            if nested:
                clean[key] = nested
            continue
        if isinstance(value, list):
            vals = []
            for item in value[:20]:
                if isinstance(item, dict):
                    nested = _sanitize_track_payload(item, depth + 1)
                    if nested:
                        vals.append(nested)
                else:
                    scalar = _safe_track_scalar(item)
                    if scalar is not None:
                        vals.append(scalar)
            if vals:
                clean[key] = vals
            continue
        scalar = _safe_track_scalar(value)
        if scalar is not None:
            clean[key] = scalar
    return clean


def _make_tracking_event(store: UserStore, event_type: str, payload: dict | None = None) -> dict:
    raw_type = str(event_type or "unknown").strip()[:120]
    normalized = _TRACK_EVENT_TYPE_RE.sub("_", raw_type).strip("_.:-").lower()
    if not normalized:
        normalized = "unknown"
    return {
        "event_id": core_util._new_public_id("trk"),
        "user_id": store.user_id,
        "type": normalized[:120],
        "ts": time.time(),
        "created_at": datetime.now().isoformat(),
        "source": str((payload or {}).get("source") or "ios")[:40],
        "payload": _sanitize_track_payload((payload or {}).get("payload") if isinstance(payload, dict) else {}),
        "app_version": str((payload or {}).get("app_version") or "")[:40],
        "build": str((payload or {}).get("build") or "")[:40],
        "platform": str((payload or {}).get("platform") or "ios")[:40],
        "route": str((payload or {}).get("route") or "")[:80],
    }


@bp.route("/v1/track/event", methods=["POST"])
def track_event():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("event_type") or payload.get("type") or "unknown")
    event = _make_tracking_event(store, event_type, payload)
    store.append_tracking_event(event)
    return jsonify({"status": "ok", "event_id": event["event_id"]})



