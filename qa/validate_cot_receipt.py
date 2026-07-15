#!/usr/bin/env python3
"""Validate the private, sanitized receipt emitted by ``cot_delivery_probe``.

The validator deliberately accepts only the probe's flat, metadata-only JSON
contract.  It never returns raw file bytes and hashes a canonical JSON encoding,
so callers can bind later evidence to the validated object without publishing
the private receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


RECEIPT_SCHEMA_VERSION = 2
MAX_RECEIPT_BYTES = 64 * 1024
_EFFORT_VALUES = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "unsupported", "unknown"}
)

RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "request_id",
        "turn_id",
        "trace_id",
        "reply_message_id",
        "status",
        "failure_code",
        "requested_effort",
        "configured_effort",
        "configured_effort_attested",
        "configured_route_matches_manifest",
        "effective_effort",
        "effective_effort_attested",
        "release_qualified",
        "delivery_qualified",
        "final_answer_correct",
        "ack_latency_ms",
        "reply_latency_ms",
        "model_duration_ms",
        "provider_api_duration_ms",
        "trace_dropped",
        "model_call_count",
        "agent_reply_count",
        "chat_response_count",
        "chat_response_match_count",
        "model_thinking_present",
        "model_thinking_len",
        "reasoning_event_count",
        "model_thinking_source",
        "agent_reply_thinking_kind",
        "delivered_thinking_present",
        "delivered_thinking_len",
        "delivered_thinking_kind",
        "delivered_thinking_source",
        "delivered_thinking_model",
        "delivered_thinking_native",
        "metadata_present",
        "user_visible_disclosure_present",
        "token_metadata_status",
        "reasoning_token_count",
        "raw_reply_stored",
        "raw_thinking_stored",
        "raw_trace_stored",
    }
)

_STATUS_FAILURE_CODES = {
    "PASS": frozenset({"NONE"}),
    "FAIL": frozenset(
        {
            "FINAL_ANSWER_WRONG",
            "DOWNSTREAM_PARSE_DROPPED_REASONING",
            "THINKING_ENVELOPE_NOT_DELIVERED",
            "THINKING_ENVELOPE_UNREADABLE",
            "THINKING_METADATA_INVALID",
        }
    ),
    "UNVERIFIED": frozenset(
        {
            "CHAT_TIMEOUT",
            "CHAT_REQUEST_FAILED",
            "MODEL_REASONING_NOT_OBSERVED",
            "TRACE_AMBIGUOUS",
            "TRACE_UNAVAILABLE",
        }
    ),
}
_BOOLEAN_FIELDS = frozenset(
    {
        "release_qualified",
        "delivery_qualified",
        "final_answer_correct",
        "trace_dropped",
        "model_thinking_present",
        "delivered_thinking_present",
        "metadata_present",
        "user_visible_disclosure_present",
        "configured_effort_attested",
        "configured_route_matches_manifest",
        "effective_effort_attested",
        "raw_reply_stored",
        "raw_thinking_stored",
        "raw_trace_stored",
    }
)
_TIMING_FIELDS = (
    "ack_latency_ms",
    "reply_latency_ms",
    "model_duration_ms",
    "provider_api_duration_ms",
)
_COUNT_FIELDS = (
    "model_call_count",
    "agent_reply_count",
    "chat_response_count",
    "chat_response_match_count",
    "model_thinking_len",
    "reasoning_event_count",
    "delivered_thinking_len",
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


class CotReceiptError(RuntimeError):
    """The COT receipt is unsafe, malformed, or internally inconsistent."""


def _read_owned_private_file(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise CotReceiptError("COT receipt path is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CotReceiptError("COT receipt is unavailable") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_RECEIPT_BYTES
        ):
            raise CotReceiptError("COT receipt is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise CotReceiptError("COT receipt changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise CotReceiptError("COT receipt changed while reading")
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise CotReceiptError("COT receipt changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CotReceiptError("COT receipt contains duplicate keys")
        result[key] = value
    return result


def _safe_string(value: object, *, max_length: int, allow_empty: bool) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= max_length
        and (not value or _SAFE_IDENTIFIER_RE.fullmatch(value) is not None)
    )


def _bounded_printable_text(value: object, *, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= max_length
        and all(character.isprintable() for character in value)
    )


def _nonnegative_finite_number_or_none(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_default_delivery(receipt: Mapping[str, Any]) -> None:
    if (
        receipt["delivered_thinking_present"] is not False
        or receipt["delivered_thinking_len"] != 0
        or receipt["delivered_thinking_kind"] != ""
        or receipt["delivered_thinking_source"] != ""
        or receipt["delivered_thinking_model"] != ""
        or receipt["delivered_thinking_native"] is not None
        or receipt["metadata_present"] is not False
        or receipt["user_visible_disclosure_present"] is not False
    ):
        raise CotReceiptError("COT receipt delivery evidence is inconsistent")


def _require_positive_trace(receipt: Mapping[str, Any]) -> None:
    if (
        receipt["trace_dropped"] is not False
        or receipt["model_call_count"] != 1
        or receipt["agent_reply_count"] != 1
        or receipt["chat_response_count"] != 1
        or receipt["chat_response_match_count"] != 1
        or receipt["model_thinking_present"] is not True
        or receipt["model_thinking_len"] <= 0
    ):
        raise CotReceiptError("COT receipt positive trace evidence is inconsistent")


def _require_correlated_reply(receipt: Mapping[str, Any]) -> None:
    if (
        not receipt["request_id"]
        or not receipt["reply_message_id"]
        or receipt["ack_latency_ms"] is None
        or receipt["reply_latency_ms"] is None
    ):
        raise CotReceiptError("COT receipt reply correlation is incomplete")


def _validate_status_evidence(receipt: Mapping[str, Any]) -> None:
    status = receipt["status"]
    code = receipt["failure_code"]
    if code not in _STATUS_FAILURE_CODES[status]:
        raise CotReceiptError("COT receipt status and failure code do not match")
    if receipt["delivery_qualified"] is not (status == "PASS"):
        raise CotReceiptError("COT receipt delivery qualification is inconsistent")

    ambiguous = (
        receipt["trace_dropped"] is True
        or receipt["model_call_count"] != 1
        or receipt["agent_reply_count"] != 1
        or receipt["chat_response_count"] != 1
        or receipt["chat_response_match_count"] != 1
    )
    positive_trace = (
        receipt["model_thinking_present"] is True
        and receipt["model_thinking_len"] > 0
    )

    if code == "CHAT_REQUEST_FAILED":
        request_ack_pair_is_valid = (
            not receipt["request_id"] and receipt["ack_latency_ms"] is None
        ) or (
            bool(receipt["request_id"]) and receipt["ack_latency_ms"] is not None
        )
        if (
            not request_ack_pair_is_valid
            or receipt["reply_message_id"]
            or receipt["reply_latency_ms"] is not None
            or receipt["final_answer_correct"] is not False
            or receipt["trace_dropped"] is not False
            or any(receipt[field] != 0 for field in _COUNT_FIELDS[:-1])
            or receipt["delivered_thinking_len"] != 0
            or receipt["model_thinking_source"]
            or receipt["agent_reply_thinking_kind"]
            or receipt["model_duration_ms"] is not None
            or receipt["provider_api_duration_ms"] is not None
        ):
            raise CotReceiptError("COT request-failure receipt is inconsistent")
        _require_default_delivery(receipt)
        return
    if code == "CHAT_TIMEOUT":
        if (
            not receipt["request_id"]
            or receipt["reply_message_id"]
            or receipt["final_answer_correct"] is not False
            or receipt["ack_latency_ms"] is None
            or receipt["reply_latency_ms"] is not None
            or receipt["trace_dropped"] is not False
            or any(receipt[field] != 0 for field in _COUNT_FIELDS[:-1])
            or receipt["delivered_thinking_len"] != 0
            or receipt["model_thinking_source"]
            or receipt["agent_reply_thinking_kind"]
            or receipt["model_duration_ms"] is not None
            or receipt["provider_api_duration_ms"] is not None
        ):
            raise CotReceiptError("COT timeout receipt is inconsistent")
        _require_default_delivery(receipt)
        return

    _require_correlated_reply(receipt)
    if code == "TRACE_UNAVAILABLE":
        if (
            receipt["trace_dropped"] is not False
            or receipt["model_call_count"] != 0
            or receipt["agent_reply_count"] != 0
            or receipt["chat_response_count"] != 0
            or receipt["chat_response_match_count"] != 0
            or receipt["model_thinking_present"] is not False
            or receipt["model_thinking_len"] != 0
            or receipt["reasoning_event_count"] != 0
            or receipt["model_thinking_source"]
            or receipt["agent_reply_thinking_kind"]
            or receipt["model_duration_ms"] is not None
            or receipt["provider_api_duration_ms"] is not None
        ):
            raise CotReceiptError("COT unavailable trace receipt is inconsistent")
        return
    if code == "TRACE_AMBIGUOUS":
        if not ambiguous:
            raise CotReceiptError("COT ambiguous trace receipt is inconsistent")
        return
    if code == "MODEL_REASONING_NOT_OBSERVED":
        if ambiguous or positive_trace:
            raise CotReceiptError("COT missing-reasoning receipt is inconsistent")
        return

    _require_positive_trace(receipt)
    if code == "DOWNSTREAM_PARSE_DROPPED_REASONING":
        if receipt["agent_reply_thinking_kind"]:
            raise CotReceiptError("COT downstream parse receipt is inconsistent")
        return
    if not receipt["agent_reply_thinking_kind"]:
        raise CotReceiptError("COT agent reply reasoning metadata is missing")
    if code == "THINKING_ENVELOPE_UNREADABLE":
        _require_default_delivery(receipt)
        return
    if code == "THINKING_ENVELOPE_NOT_DELIVERED":
        if receipt["user_visible_disclosure_present"] is not False:
            raise CotReceiptError("COT missing-envelope receipt is inconsistent")
        return
    if code == "THINKING_METADATA_INVALID":
        if (
            receipt["user_visible_disclosure_present"] is not True
            or receipt["metadata_present"] is not False
        ):
            raise CotReceiptError("COT invalid-metadata receipt is inconsistent")
        return
    if code == "FINAL_ANSWER_WRONG":
        if (
            receipt["final_answer_correct"] is not False
            or receipt["user_visible_disclosure_present"] is not True
            or receipt["metadata_present"] is not True
        ):
            raise CotReceiptError("COT final-answer receipt is inconsistent")
        return
    if code == "NONE" and (
        receipt["final_answer_correct"] is not True
        or receipt["user_visible_disclosure_present"] is not True
        or receipt["metadata_present"] is not True
    ):
        raise CotReceiptError("COT passing receipt evidence is incomplete")


def _validate_receipt(receipt: object, expected_profile_id: str) -> dict[str, Any]:
    if not _safe_string(expected_profile_id, max_length=128, allow_empty=False):
        raise CotReceiptError("expected profile ID is invalid")
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise CotReceiptError("COT receipt shape is invalid")
    if (
        not isinstance(receipt.get("schema_version"), int)
        or isinstance(receipt.get("schema_version"), bool)
        or receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
    ):
        raise CotReceiptError("COT receipt schema version is invalid")
    if receipt.get("profile_id") != expected_profile_id:
        raise CotReceiptError("COT receipt profile assignment does not match")
    if not _safe_string(receipt["profile_id"], max_length=128, allow_empty=False):
        raise CotReceiptError("COT receipt profile ID is invalid")
    for field in ("request_id", "turn_id", "trace_id", "reply_message_id"):
        if not _safe_string(receipt[field], max_length=256, allow_empty=True):
            raise CotReceiptError(f"COT receipt {field} is invalid")
    if not (
        receipt["request_id"] == receipt["turn_id"] == receipt["trace_id"]
    ):
        raise CotReceiptError("COT receipt turn correlation is inconsistent")
    if receipt["reply_message_id"] and not receipt["request_id"]:
        raise CotReceiptError("COT receipt reply correlation is inconsistent")

    for field in ("requested_effort", "configured_effort", "effective_effort"):
        if receipt.get(field) not in _EFFORT_VALUES:
            raise CotReceiptError(f"COT receipt {field} is invalid")
    if receipt["requested_effort"] != "medium":
        raise CotReceiptError("COT receipt requested effort is not locked medium")
    if (
        receipt["effective_effort"] != "unknown"
        or receipt.get("effective_effort_attested") is not False
    ):
        raise CotReceiptError("COT receipt invents effective effort evidence")
    configured_attested = receipt.get("configured_effort_attested")
    route_matches = receipt.get("configured_route_matches_manifest")
    if configured_attested is True and receipt["configured_effort"] == "unknown":
        raise CotReceiptError("COT receipt configured effort attestation is inconsistent")
    if configured_attested is False and receipt["configured_effort"] != "unknown":
        raise CotReceiptError("COT receipt configured effort attestation is inconsistent")
    if route_matches is True and configured_attested is not True:
        raise CotReceiptError("COT receipt configured route attestation is inconsistent")

    for field in _BOOLEAN_FIELDS:
        if not isinstance(receipt[field], bool):
            raise CotReceiptError(f"COT receipt {field} is invalid")
    if receipt["release_qualified"] is not False:
        raise CotReceiptError("COT receipt cannot release-qualify a build")
    if any(receipt[field] is not False for field in (
        "raw_reply_stored",
        "raw_thinking_stored",
        "raw_trace_stored",
    )):
        raise CotReceiptError("COT receipt contains raw evidence")

    if receipt.get("status") not in _STATUS_FAILURE_CODES:
        raise CotReceiptError("COT receipt status is invalid")
    if not isinstance(receipt.get("failure_code"), str):
        raise CotReceiptError("COT receipt failure code is invalid")
    for field in _TIMING_FIELDS:
        if not _nonnegative_finite_number_or_none(receipt[field]):
            raise CotReceiptError(f"COT receipt {field} is invalid")
    if (
        receipt["ack_latency_ms"] is not None
        and receipt["reply_latency_ms"] is not None
        and receipt["reply_latency_ms"] < receipt["ack_latency_ms"]
    ):
        raise CotReceiptError("COT receipt latency ordering is inconsistent")
    for field in _COUNT_FIELDS:
        if not _nonnegative_integer(receipt[field]):
            raise CotReceiptError(f"COT receipt {field} is invalid")
    if receipt["chat_response_match_count"] > receipt["chat_response_count"]:
        raise CotReceiptError("COT receipt chat response counts are inconsistent")
    if receipt["model_call_count"] != 1 and (
        receipt["model_thinking_present"]
        or receipt["model_thinking_len"]
        or receipt["model_duration_ms"] is not None
        or receipt["provider_api_duration_ms"] is not None
    ):
        raise CotReceiptError("COT receipt model-call evidence is inconsistent")
    if receipt["reasoning_event_count"] != (
        1 if receipt["model_thinking_present"] else 0
    ):
        raise CotReceiptError("COT receipt reasoning event count is inconsistent")
    if not receipt["model_thinking_present"] and receipt["model_thinking_len"] != 0:
        raise CotReceiptError("COT receipt model reasoning length is inconsistent")

    string_limits = {
        "model_thinking_source": 80,
        "agent_reply_thinking_kind": 64,
        "delivered_thinking_kind": 128,
        "delivered_thinking_source": 128,
    }
    for field, limit in string_limits.items():
        if not _safe_string(receipt[field], max_length=limit, allow_empty=True):
            raise CotReceiptError(f"COT receipt {field} is invalid")
    if not _bounded_printable_text(
        receipt["delivered_thinking_model"], max_length=256
    ):
        raise CotReceiptError("COT receipt delivered_thinking_model is invalid")
    if receipt["delivered_thinking_native"] is not None and not isinstance(
        receipt["delivered_thinking_native"], bool
    ):
        raise CotReceiptError("COT receipt thinking-native value is invalid")
    metadata_present = bool(
        receipt["delivered_thinking_kind"]
        and receipt["delivered_thinking_source"]
        and receipt["delivered_thinking_model"]
        and receipt["delivered_thinking_native"] is True
    )
    if receipt["metadata_present"] is not metadata_present:
        raise CotReceiptError("COT receipt metadata-presence flag is inconsistent")
    if receipt["user_visible_disclosure_present"] and (
        not receipt["delivered_thinking_present"]
        or receipt["delivered_thinking_len"] <= 0
    ):
        raise CotReceiptError("COT receipt disclosure-presence flag is inconsistent")
    if receipt["delivered_thinking_len"] == 0 and receipt[
        "user_visible_disclosure_present"
    ]:
        raise CotReceiptError("COT receipt disclosure length is inconsistent")

    if receipt["token_metadata_status"] not in {"UNVERIFIED", "PRESENT"}:
        raise CotReceiptError("COT receipt token metadata status is invalid")
    if receipt["token_metadata_status"] == "UNVERIFIED":
        if receipt["reasoning_token_count"] is not None:
            raise CotReceiptError(
                "COT receipt reasoning token count must be unavailable"
            )
    elif not _nonnegative_integer(receipt["reasoning_token_count"]):
        raise CotReceiptError("COT receipt reasoning token count is invalid")
    _validate_status_evidence(receipt)
    return receipt


def canonical_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the normalized JSON object, independent of layout."""
    try:
        payload = json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise CotReceiptError("COT receipt cannot be canonicalized") from None
    return hashlib.sha256(payload).hexdigest()


def validate_cot_receipt(
    path: Path, expected_profile_id: str
) -> tuple[dict[str, Any], str]:
    """Securely load, validate, and hash one private COT delivery receipt."""
    raw = _read_owned_private_file(path)
    try:
        decoded = raw.decode("utf-8")
        document = json.loads(decoded, object_pairs_hook=_object_without_duplicate_keys)
    except CotReceiptError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CotReceiptError("COT receipt JSON is invalid") from None
    receipt = _validate_receipt(document, expected_profile_id)
    return receipt, canonical_receipt_sha256(receipt)
