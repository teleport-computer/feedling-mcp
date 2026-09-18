"""Stdlib-only closed contract shared by enclave emission and log export.

Export validates values as well as keys. New route templates require an explicit
contract update; an unknown shape is dropped instead of exporting arbitrary text.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import re

from enclave_health_contract import PURPOSE_LABELS

FIELDS = (
    "ts", "pid", "method", "route", "status", "dur_ms", "purpose", "auth_kind",
    "whoami_source", "whoami_ms", "decrypt_queue_ms", "decrypt_ms",
    "user_prefix", "failure_class", "req_bytes", "resp_bytes",
)
FAILURE_CLASSES = frozenset({
    "none", "other", "not_found", "method_not_allowed", "internal_error", "response_incomplete",
    "not_ready", "missing_api_key", "unauthorized", "cannot_resolve_user_id",
    "envelope_required", "decrypt_failed", "backend_error", "backend_unreachable",
    "key_derivation_unavailable", "memory_search_resource_limit",
    "memory_search_protocol_unsupported", "invalid_request",
})
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "OTHER"})
AUTH_KINDS = frozenset({"api_key", "runtime_token", "none"})
WHOAMI_SOURCES = frozenset({"local_token", "backend", "cache", "none"})
ROUTES = frozenset({
    '/attestation',
    '/healthz',
    '/v1/chat/history',
    '/v1/chat/messages/{message_id}/body',
    '/v1/decrypt/selfcheck',
    '/v1/envelope/decrypt',
    '/v1/history/fetch',
    '/v1/history/leaf-hints',
    '/v1/history/scan',
    '/v1/identity/get',
    '/v1/memory/fetch',
    '/v1/memory/index',
    '/v1/memory/list',
    '/v1/screen/frames/{frame_id}/caption',
    '/v1/screen/frames/{frame_id}/decrypt',
    '/v1/screen/frames/{frame_id}/image',
    '/v1/storage/reencrypt-frame',
    '/v1/worldbook/match',
    '<unmatched>',
})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def valid_request_line(raw: bytes) -> bool:
    try:
        row = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            return False
        for key, allowed in (("method", METHODS), ("route", ROUTES),
                             ("purpose", PURPOSE_LABELS), ("auth_kind", AUTH_KINDS),
                             ("whoami_source", WHOAMI_SOURCES), ("failure_class", FAILURE_CLASSES)):
            if not isinstance(row[key], str) or row[key] not in allowed:
                return False
        ts = row["ts"]
        if not isinstance(ts, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ts):
            return False
        datetime.fromisoformat(ts)
        prefix = row["user_prefix"]
        if prefix is not None and (not isinstance(prefix, str) or not re.fullmatch(r"usr_[A-Za-z0-9]{8}", prefix)):
            return False
        for key in ("pid", "req_bytes", "resp_bytes"):
            if type(row[key]) is not int or not 0 <= row[key] <= 2**63 - 1:
                return False
        status = row["status"]
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            return False
        for key in ("dur_ms", "whoami_ms", "decrypt_queue_ms", "decrypt_ms"):
            value = row[key]
            if value is None and key != "dur_ms":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e15:
                return False
        return True
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False
