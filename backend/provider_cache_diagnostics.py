"""Closed numeric cache evidence and process-local system-prefix comparison.

No content, endpoint, affinity key or content digest leaves this module. Prefix
state uses a process-random HMAC, only in RAM; it is not a provider cache key.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import json
import secrets
import threading
import time

MAX_COUNT = (1 << 63) - 1
NORMALIZED = ("prompt_tokens", "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens")
RAW_PATHS = {
    "anthropic": ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"),
    "bedrock": ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"),
    "gemini": ("promptTokenCount", "cachedContentTokenCount"),
    "openai": ("prompt_tokens", "input_tokens", "prompt_tokens_details.cached_tokens",
               "input_tokens_details.cached_tokens", "prompt_cache_hit_tokens",
               "prompt_tokens_details.cache_write_tokens", "input_tokens_details.cache_write_tokens",
               "cache_write_tokens", "prompt_cache_miss_tokens"),
}
PREFIX_RANGES = frozenset({"openai_chat_leading_system", "openai_responses_instructions",
                           "anthropic_system", "bedrock_system", "gemini_system_instruction"})
PREFIX_STATUSES = frozenset({"baseline", "same", "changed", "unsupported", "unmeasured", "overlap"})


def count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def usage(provider, raw, normalized):
    """Observe only fixed upstream paths; never coerce strings/bools/floats."""
    family = provider if provider in {"anthropic", "bedrock", "gemini"} else "openai"
    fields = {}
    for path in RAW_PATHS[family]:
        value = raw
        present = True
        for part in path.split("."):
            if type(value) is not dict or part not in value:
                present = False
                break
            value = value[part]
        number = count(value) if present else None
        fields[path] = {"present": present, "valid": number is not None if present else None,
                        "value": number}
    return {"version": 1, "family": family, "raw": fields,
            "normalized": {key: count(normalized.get(key)) for key in NORMALIZED}}


def project(value):
    """Ledger boundary: reconstruct every field, ignoring arbitrary additions."""
    if type(value) is not dict or type(value.get("version")) is not int or value["version"] != 1:
        return None
    family = value.get("family")
    if type(family) is not str or family not in RAW_PATHS:
        return None
    raw = value.get("raw")
    normalized = value.get("normalized")
    if type(raw) is not dict or type(normalized) is not dict:
        return None
    out = {"version": 1, "family": family, "raw": {},
           "normalized": {key: count(normalized.get(key)) for key in NORMALIZED}}
    for path in RAW_PATHS[family]:
        row = raw.get(path)
        row = row if type(row) is dict else {}
        present = row.get("present") if type(row.get("present")) is bool else None
        valid = row.get("valid") if present is True and type(row.get("valid")) is bool else None
        number = count(row.get("value")) if valid is True else None
        out["raw"][path] = {"present": present, "valid": valid if number is not None else False if present is True else None,
                              "value": number}
    prefix = value.get("prefix")
    if type(prefix) is dict:
        status, scope = prefix.get("status"), prefix.get("range")
        if type(status) is str and status in PREFIX_STATUSES:
            scope = scope if type(scope) is str and scope in PREFIX_RANGES else None
            changed = status == "changed" if status in {"same", "changed"} and scope else None
            out["prefix"] = {"scope": "user_route_process", "range": scope,
                             "status": status if changed is not None or status not in {"same", "changed"} else "unmeasured",
                             "changed": changed}
    return out


def system_prefix(payload, provider):
    """Select a closed range from the actual final JSON request body."""
    if type(payload) is not dict:
        return None, None
    if provider in {"anthropic", "bedrock"}:
        return f"{provider}_system", payload.get("system") or None
    if provider == "gemini":
        return "gemini_system_instruction", payload.get("systemInstruction") or None
    if "instructions" in payload:
        return "openai_responses_instructions", payload.get("instructions") or None
    messages = payload.get("messages")
    if type(messages) is not list:
        return None, None
    leading = []
    for row in messages:
        if type(row) is not dict or row.get("role") not in {"system", "developer"}:
            break
        if not row.get("content"):
            return None, None
        leading.append(row)
    return "openai_chat_leading_system", leading or None


class PrefixTracker:
    """At most 256 user-route baselines for 30 minutes in this process.

    This host scope spans V2 jobs; it is NOT a durable chat session. Overlapping
    calls invalidate ordering and the baseline. A subsequent call starts afresh.
    """
    def __init__(self, *, capacity=256, ttl=1800, clock=time.monotonic):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._key = secrets.token_bytes(32)
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def begin(self, scope):
        if type(scope) is not str or not scope:
            return None
        with self._lock:
            now = self.clock()
            for key, entry in list(self._entries.items()):
                if not entry["active"] and now - entry["time"] >= self.ttl:
                    del self._entries[key]
            if scope not in self._entries:
                if len(self._entries) >= self.capacity:
                    victim = next((key for key, row in self._entries.items() if not row["active"]), None)
                    if victim is None:
                        return None
                    del self._entries[victim]
                self._entries[scope] = {"active": 0, "overlap": False, "baseline": None, "time": now}
            entry = self._entries[scope]
            if entry["active"]:
                entry["overlap"] = True
                entry["baseline"] = None
            entry["active"] += 1
            self._entries.move_to_end(scope)
            return {"entry": entry, "released": False}

    def finish(self, ticket, payload=None, provider="", model=""):
        result = {"scope": "user_route_process", "range": None, "status": "unmeasured", "changed": None}
        if ticket is None:
            return result
        digest = None
        partition = None
        try:
            scope, prefix = system_prefix(payload, provider)
            result["range"] = scope
            if prefix is not None:
                encoded = json.dumps([provider, model, scope, prefix], ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                if len(encoded) <= 1_048_576:
                    digest = hmac.digest(self._key, encoded, hashlib.sha256)
                    partition = hmac.digest(self._key, json.dumps([provider, model, scope]).encode(), hashlib.sha256)
            if payload is not None and digest is None:
                result["status"] = "unsupported"
        except Exception:
            pass
        finally:
            self._complete(ticket, digest, partition, result)
        return result

    def abandon(self, ticket):
        """Idempotent release if extraction or another diagnostic step failed."""
        if ticket is not None:
            self._complete(ticket, None, None, {})

    def _complete(self, ticket, digest, partition, result):
        with self._lock:
            if ticket["released"]:
                return
            ticket["released"] = True
            entry = ticket["entry"]
            entry["active"] -= 1
            entry["time"] = self.clock()
            if entry["overlap"]:
                result["status"] = "overlap"
                entry["baseline"] = None
                if not entry["active"]:
                    entry["overlap"] = False
            elif digest is not None:
                previous = entry["baseline"]
                if previous is not None and not hmac.compare_digest(previous[0], partition):
                    previous = None
                if previous is None:
                    result["status"] = "baseline"
                else:
                    result["status"] = "same" if hmac.compare_digest(previous[1], digest) else "changed"
                result["changed"] = None if previous is None else result["status"] == "changed"
                entry["baseline"] = (partition, digest)
            else:
                entry["baseline"] = None



prefix_tracker = PrefixTracker()
