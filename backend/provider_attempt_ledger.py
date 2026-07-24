"""Minimal append-only provider attempt ledger.

The resident consumer reports metadata only. Provider prompts, responses,
headers, credentials, and raw errors never enter this stream.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import db


STREAM = "provider_attempts"
VALID_TRIGGERS = frozenset({"first", "stream_cut_retry", "redelivery"})
_SAFE_OUTCOME_RE = re.compile(r"[^a-z0-9_.-]+")
_MAX_BATCH = 64


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _usage_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, count)


def _timestamp(value: Any) -> float:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return time.time()
    return ts if math.isfinite(ts) and ts > 0 else time.time()


def _normalize_attempt(raw: Any) -> tuple[dict | None, str]:
    if not isinstance(raw, dict):
        return None, "invalid_attempt"
    parent_message_id = _bounded_text(raw.get("parent_message_id"), 256)
    if not parent_message_id:
        return None, "parent_message_id_required"
    trigger = _bounded_text(raw.get("trigger"), 32)
    if trigger not in VALID_TRIGGERS:
        return None, "invalid_trigger"
    outcome = _bounded_text(raw.get("outcome"), 64).lower() or "unknown"
    outcome = _SAFE_OUTCOME_RE.sub("_", outcome).strip("_.-") or "unknown"
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    ts = _timestamp(raw.get("ts"))
    return {
        "parent_message_id": parent_message_id,
        "trigger": trigger,
        "provider_request_id": _bounded_text(raw.get("provider_request_id"), 256),
        "usage": {
            "input_tokens": _usage_count(usage.get("input_tokens")),
            "output_tokens": _usage_count(usage.get("output_tokens")),
        },
        "outcome": outcome,
        "ts": ts,
    }, ""


def record_attempts_payload(store, payload: dict) -> tuple[dict, int]:
    raw_attempts = payload.get("provider_attempts")
    if not isinstance(raw_attempts, list):
        single = payload.get("provider_attempt")
        raw_attempts = [single] if isinstance(single, dict) else []
    if not raw_attempts:
        return {"error": "provider_attempts_required"}, 400
    if len(raw_attempts) > _MAX_BATCH:
        return {"error": "too_many_provider_attempts"}, 400

    normalized: list[dict] = []
    for raw in raw_attempts:
        attempt, error = _normalize_attempt(raw)
        if attempt is None:
            return {"error": error}, 400
        normalized.append(attempt)

    recorded: list[dict] = []
    for attempt in normalized:
        stored = db.log_append_numbered(
            store.user_id,
            STREAM,
            attempt,
            number_field="attempt_n",
            ts=attempt["ts"],
            item_key=attempt["parent_message_id"],
        )
        if stored is None:
            return {"error": "provider_attempt_ledger_unavailable"}, 503
        recorded.append(stored)
    return {
        "status": "ok",
        "recorded": len(recorded),
        "attempt_n": [row["attempt_n"] for row in recorded],
    }, 200
