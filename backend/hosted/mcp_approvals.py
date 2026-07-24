"""Shared contract limits for user-approved MCP read-only tools."""

from __future__ import annotations

import re


MAX_READ_ONLY_TOOL_APPROVALS = 64
TOOL_NAME_PATTERN = r"^[^\x00-\x1f\x7f]{1,256}$"
FINGERPRINT_PATTERN = r"^[a-f0-9]{64}$"

_TOOL_NAME_RE = re.compile(TOOL_NAME_PATTERN)
_FINGERPRINT_RE = re.compile(FINGERPRINT_PATTERN)


def valid_tool_name(value: object) -> bool:
    return isinstance(value, str) and _TOOL_NAME_RE.fullmatch(value) is not None


def valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _FINGERPRINT_RE.fullmatch(value) is not None
