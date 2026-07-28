"""Minimal append-only provider attempt ledger.

The resident consumer reports metadata only. Provider prompts, responses,
headers, credentials, and raw errors never enter this stream.

Two writers share this one stream on purpose. V1's resident consumer POSTs its
attempts (``record_attempts_payload``); Runtime V2 writes its own server-side
(``record_runtime_attempt``). Keeping both in ``provider_attempts`` with the
same field names is what makes "the same relay works under V1 and fails under
V2" a single query instead of a break-glass trajectory decrypt — exactly the
comparison that took a decrypt to answer for usr_90184… on 2026-07-27.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import db


STREAM = "provider_attempts"
# "first"/"stream_cut_retry"/"redelivery" are the resident consumer's own
# vocabulary. The v2_* values are written server-side by Runtime V2 and name
# the lane whose provider call this was, so one stream can still be split by
# runtime when reading.
VALID_TRIGGERS = frozenset(
    {
        "first",
        "stream_cut_retry",
        "redelivery",
        "v2_turn",
        "v2_catchup",
    }
)
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


def record_runtime_attempt(
    user_id: str,
    *,
    parent_key: str,
    trigger: str,
    outcome: str,
    provider: str = "",
    model: str = "",
    lane: str = "",
    input_tokens: Any = None,
    output_tokens: Any = None,
    error_class: str = "",
    provider_request_id: str = "",
    ts: float | None = None,
) -> bool:
    """Append one Runtime V2 provider attempt. Never raises.

    Same stream and same field names as the resident consumer's ledger, plus
    ``provider``/``model``/``lane``/``error_class`` — V1 gets those from the
    consumer's own config, V2 has to state them itself.

    Telemetry must never be able to fail a turn that would otherwise succeed,
    so every failure here is swallowed and reported as ``False``.
    """
    try:
        uid = str(user_id or "").strip()
        key = _bounded_text(parent_key, 256)
        if not uid or not key:
            return False
        safe_trigger = _bounded_text(trigger, 32)
        if safe_trigger not in VALID_TRIGGERS:
            return False
        safe_outcome = _bounded_text(outcome, 64).lower() or "unknown"
        safe_outcome = (
            _SAFE_OUTCOME_RE.sub("_", safe_outcome).strip("_.-") or "unknown"
        )
        doc = {
            "parent_message_id": key,
            "trigger": safe_trigger,
            "provider_request_id": _bounded_text(provider_request_id, 256),
            "usage": {
                "input_tokens": _usage_count(input_tokens),
                "output_tokens": _usage_count(output_tokens),
            },
            "outcome": safe_outcome,
            "ts": _timestamp(ts),
            # V2-only additions. Absent on resident rows, which is itself a
            # useful discriminator when reading the merged stream.
            "runtime": "v2",
            "lane": _bounded_text(lane, 32),
            "provider": _bounded_text(provider, 64),
            "model": _bounded_text(model, 128),
            "error_class": _bounded_text(error_class, 64),
        }
        stored = db.log_append_numbered(
            uid,
            STREAM,
            doc,
            number_field="attempt_n",
            ts=doc["ts"],
            item_key=key,
        )
        return stored is not None
    except Exception:  # noqa: BLE001 - telemetry must not break a turn
        return False
