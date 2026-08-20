"""Audited preservation for terminal ciphertext during TEE promotion.

This module never decrypts content.  It identifies the small, explicit set of
source/target row projections whose original ciphertext may be retained when a
terminal replication marker proves the plaintext projection cannot be built.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass


PRESERVED_PREFIX = "preserved_ciphertext:v1:"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Contract:
    source_fetch_sql: str
    destination_fetch_sql: str
    by_user_only: bool = False

    def args(self, user_id: str, item_id: str) -> tuple[str, ...]:
        return (user_id,) if self.by_user_only else (user_id, item_id)


CONTRACTS: dict[str, _Contract] = {
    "chat_messages": _Contract(
        "SELECT user_id,msg_id,ts,doc,seq,storage_generation "
        "FROM chat_messages WHERE user_id=%s AND msg_id=%s",
        "SELECT user_id,msg_id,ts,doc,seq,storage_generation "
        "FROM chat_messages WHERE user_id=%s AND msg_id=%s",
    ),
    "memory_moments": _Contract(
        "SELECT user_id,moment_id,occurred_at,doc FROM memory_moments "
        "WHERE user_id=%s AND moment_id=%s",
        "SELECT user_id,moment_id,occurred_at,doc FROM memory_moments "
        "WHERE user_id=%s AND moment_id=%s",
    ),
    "identity": _Contract(
        "SELECT user_id,kind,doc FROM user_blobs "
        "WHERE user_id=%s AND kind='identity'",
        "SELECT user_id,kind,doc FROM user_blobs "
        "WHERE user_id=%s AND kind='identity'",
        by_user_only=True,
    ),
    "frame_envelopes": _Contract(
        "SELECT user_id,frame_id,ts,doc,env_meta,body_key "
        "FROM frame_envelopes WHERE user_id=%s AND frame_id=%s",
        "SELECT user_id,frame_id,ts,doc,env_meta,body_key "
        "FROM frame_envelopes WHERE user_id=%s AND frame_id=%s",
    ),
}


def is_terminal_reason(reason: str) -> bool:
    """Whether ``reason`` is eligible for one-time ciphertext preservation."""
    value = str(reason or "")
    return (
        value.startswith("decrypt_failed:")
        or value.startswith("pdm:")
        or value == "visibility_local_only"
    )


def encode_preserved_reason(row_sha256: str, original_reason: str) -> str:
    """Build a versioned, reversible audit marker for one preserved row."""
    if not _DIGEST_RE.fullmatch(row_sha256) or not is_terminal_reason(original_reason):
        raise ValueError("invalid_preserved_marker_input")
    encoded = base64.urlsafe_b64encode(original_reason.encode()).decode().rstrip("=")
    return f"{PRESERVED_PREFIX}{row_sha256}:{encoded}"


def parse_preserved_reason(reason: str) -> tuple[str, str] | None:
    """Return ``(row digest, original reason)`` for a valid v1 marker."""
    value = str(reason or "")
    if not value.startswith(PRESERVED_PREFIX):
        return None
    digest, separator, encoded = value[len(PRESERVED_PREFIX):].partition(":")
    if not separator or not _DIGEST_RE.fullmatch(digest) or not encoded:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True
        )
        original_reason = raw.decode("utf-8", "strict")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if not is_terminal_reason(original_reason):
        return None
    return digest, original_reason


def canonical_row_sha256(table: str, row: tuple) -> str:
    """Hash one complete source/target projection without exposing its bytes."""
    payload = json.dumps(
        [table, *row],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
