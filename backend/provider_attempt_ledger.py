"""Minimal append-only provider attempt ledger.

The resident consumer reports metadata only. Provider prompts, responses,
headers, credentials, and raw errors never enter this stream.

Three writers share this one stream on purpose. V1's resident consumer POSTs its
attempts (``record_attempts_payload``); Runtime V2 and hosted model-API probes
write server-side (``record_runtime_attempt``). Keeping all three in
``provider_attempts`` with the same field names makes provider billing attempts
answerable even when gated debug trace is disabled.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import db


STREAM = "provider_attempts"
# "first"/"stream_cut_retry"/"redelivery" are the resident consumer's own
# vocabulary. The v2_* values are written server-side by Runtime V2; the
# model_api_probe value is written by hosted setup/test endpoints. Runtime and
# lane fields keep the shared stream attributable.
VALID_TRIGGERS = frozenset(
    {
        "first",
        "stream_cut_retry",
        "redelivery",
        "v2_turn",
        "v2_catchup",
        "model_api_probe",
    }
)
VALID_FALLBACK_REASONS = frozenset(
    {
        "tagged_images_rejected",
        "tool_schema_rejected",
        "provider_tool_history_rejected",
    }
)
VALID_PROVIDER_ERROR_CLASSES = frozenset(
    {"provider_config", "transient", "unknown"}
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


def _status_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _fallback_reason(value: Any) -> str:
    reason = str(value or "").strip()
    return reason if reason in VALID_FALLBACK_REASONS else ""


def _provider_error_class(value: Any) -> str:
    error_class = str(value or "").strip()
    return (
        error_class if error_class in VALID_PROVIDER_ERROR_CLASSES else ""
    )


def _duration_ms(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration):
        return None
    return max(0.0, duration)


def summarize_fallbacks(rows: list[dict]) -> list[dict]:
    """Count provider-error fallback/status pairs in a bounded row window.

    This is intentionally not a distribution of every tool-loop degradation:
    it covers failed provider calls with a closed fallback/closure reason.
    """
    counts: dict[tuple[str, int], int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("outcome") != "provider_error":
            continue
        reason = _fallback_reason(row.get("fallback_reason"))
        status = _status_code(row.get("status_code"))
        if not reason or status is None:
            continue
        key = (reason, status)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"fallback_reason": reason, "status_code": status, "count": count}
        for (reason, status), count in sorted(counts.items())
    ]


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
            "total_tokens": _usage_count(usage.get("total_tokens")),
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
    runtime: str = "v2",
    input_tokens: Any = None,
    output_tokens: Any = None,
    total_tokens: Any = None,
    error_class: str = "",
    status_code: Any = None,
    fallback_reason: str = "",
    provider_error_class: str = "",
    dur_ms: Any = None,
    provider_request_id: str = "",
    ts: float | None = None,
) -> bool:
    """Append one server-side provider attempt. Never raises.

    Same stream and same field names as the resident consumer's ledger, plus
    ``provider``/``model``/``lane``/``runtime``/``error_class`` and closed
    fallback metadata — V1 gets route fields from the consumer's own config,
    server-side callers state them explicitly.

    For Runtime V2, the database-assigned ``attempt_n`` counts outer tool-loop
    provider rounds. It does not expose or count retries internal to the
    provider transport client.

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
                "total_tokens": _usage_count(total_tokens),
            },
            "outcome": safe_outcome,
            "ts": _timestamp(ts),
            # Server-side additions. Absent on resident rows, which is itself
            # a useful discriminator when reading the merged stream.
            "runtime": _bounded_text(runtime, 32) or "v2",
            "lane": _bounded_text(lane, 32),
            "provider": _bounded_text(provider, 64),
            "model": _bounded_text(model, 128),
            "error_class": _bounded_text(error_class, 64),
            "status_code": _status_code(status_code),
            "fallback_reason": _fallback_reason(fallback_reason),
            "provider_error_class": _provider_error_class(
                provider_error_class
            ),
            "dur_ms": _duration_ms(dur_ms),
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
