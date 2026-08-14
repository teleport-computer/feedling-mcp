"""Pure input, raw-scan, and authenticated cursor kernel for V2 history search."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone


QUERY_MAX_CHARS = 128
CURSOR_MAX_CHARS = 1024
# Version 1 carried summary watermark and multi-phase leaf-hint state. Raw-only
# search deliberately rejects those short-lived cursors and restarts page one.
CURSOR_VERSION = 2
DEFAULT_CURSOR_TTL_SECONDS = 15 * 60


class HistorySearchInputError(ValueError):
    """Stable-slug rejection of one user-supplied search parameter."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code)


class CursorInvalid(ValueError):
    """An untrusted, expired, stale, or cross-user cursor."""

    code = "cursor_invalid"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail)
        super().__init__(self.code)


class CursorMismatch(ValueError):
    """An authentic cursor paired with conflicting request parameters."""

    code = "cursor_mismatch"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail)
        super().__init__(self.code)


def normalize_for_match(raw: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(raw)).casefold().split())


def normalize_query(raw: str) -> str:
    text = str(raw)
    if len(text) > QUERY_MAX_CHARS:
        raise HistorySearchInputError("query_too_long")
    normalized = normalize_for_match(text)
    if not normalized:
        raise HistorySearchInputError("query_empty")
    if len(normalized) > QUERY_MAX_CHARS:
        raise HistorySearchInputError("query_too_long")
    return normalized


def parse_rfc3339_utc(raw: str) -> float:
    text = str(raw).strip()
    if not text:
        raise HistorySearchInputError("invalid_time")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise HistorySearchInputError("invalid_time", text) from None
    if parsed.tzinfo is None:
        raise HistorySearchInputError("invalid_time", "missing_utc_offset")
    return parsed.astimezone(timezone.utc).timestamp()


def normalize_time_range(
    start: str | None,
    end: str | None,
) -> tuple[float | None, float | None]:
    start_ts = parse_rfc3339_utc(start) if start is not None else None
    end_ts = parse_rfc3339_utc(end) if end is not None else None
    if start_ts is not None and end_ts is not None and start_ts >= end_ts:
        raise HistorySearchInputError(
            "invalid_time_range",
            "start_must_precede_end",
        )
    return start_ts, end_ts


@dataclass(frozen=True)
class ScanState:
    """Inclusive newest sequence still eligible for the raw descending scan."""

    resume_seq: int

    def __post_init__(self) -> None:
        if int(self.resume_seq) < 0:
            raise ValueError("scan resume_seq must be >= 0")
        object.__setattr__(self, "resume_seq", int(self.resume_seq))


@dataclass(frozen=True)
class ScanBatch:
    min_seq: int
    max_seq: int
    limit: int


def initial_scan_state(*, snapshot_through_seq: int) -> ScanState:
    snapshot = int(snapshot_through_seq)
    if snapshot < 0:
        raise ValueError("snapshot_through_seq must be >= 0")
    return ScanState(snapshot)


def scan_complete(state: ScanState) -> bool:
    return int(state.resume_seq) <= 0


def next_batch(state: ScanState, *, batch_limit: int) -> ScanBatch | None:
    limit = int(batch_limit)
    if limit <= 0:
        raise ValueError("batch_limit must be positive")
    if scan_complete(state):
        return None
    return ScanBatch(min_seq=1, max_seq=int(state.resume_seq), limit=limit)


def advance_scan_state(
    state: ScanState,
    batch: ScanBatch,
    *,
    last_checked_seq: int | None = None,
    exhausted: bool = False,
) -> ScanState:
    if int(batch.max_seq) != int(state.resume_seq):
        raise ValueError("batch does not belong to current scan state")
    if exhausted:
        checked_floor = int(batch.min_seq)
    else:
        if last_checked_seq is None:
            raise ValueError("last_checked_seq required unless exhausted")
        checked_floor = int(last_checked_seq)
        if not int(batch.min_seq) <= checked_floor <= int(batch.max_seq):
            raise ValueError("last_checked_seq outside the batch window")
    return ScanState(max(0, checked_floor - 1))


@dataclass(frozen=True)
class HistoryCursor:
    """Authenticated resume state for one frozen raw-Chat snapshot."""

    user_id: str
    snapshot_through_seq: int
    runtime_generation: int
    query: str
    start_ts: float | None
    end_ts: float | None
    resume_seq: int
    expires_at: float
    version: int = CURSOR_VERSION

    def __post_init__(self) -> None:
        if not str(self.user_id):
            raise ValueError("cursor user_id required")
        for field in (
            "snapshot_through_seq",
            "runtime_generation",
            "resume_seq",
        ):
            if int(getattr(self, field)) < 0:
                raise ValueError(f"cursor {field} must be >= 0")
        if len(str(self.query)) > QUERY_MAX_CHARS:
            raise ValueError("cursor query too long")
        if not self.query and self.start_ts is None and self.end_ts is None:
            raise ValueError("cursor needs a query or a time range")

    def scan_state(self) -> ScanState:
        return ScanState(self.resume_seq)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _require_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise ValueError("cursor HMAC key must be >= 16 bytes")
    return bytes(key)


def encode_cursor(cursor: HistoryCursor, *, key: bytes) -> str:
    signing_key = _require_key(key)
    payload = {
        "v": int(cursor.version),
        "exp": float(cursor.expires_at),
        "u": str(cursor.user_id),
        "ss": int(cursor.snapshot_through_seq),
        "rg": int(cursor.runtime_generation),
        "q": str(cursor.query),
        "t0": cursor.start_ts,
        "t1": cursor.end_ts,
        "rs": int(cursor.resume_seq),
    }
    raw = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    compressed = zlib.compress(raw, 9)
    body = b"z" + compressed if len(compressed) < len(raw) else b"r" + raw
    segment = _b64url(body)
    signature = _b64url(
        hmac.new(signing_key, segment.encode("ascii"), hashlib.sha256).digest()
    )
    token = f"{segment}.{signature}"
    if len(token) > CURSOR_MAX_CHARS:
        raise HistorySearchInputError("cursor_overflow")
    return token


def decode_cursor(token: str, *, key: bytes, now: float) -> HistoryCursor:
    signing_key = _require_key(key)
    text = str(token or "")
    if not text or len(text) > CURSOR_MAX_CHARS:
        raise CursorInvalid("bad_length")
    segment, _, signature = text.partition(".")
    if not segment or not signature:
        raise CursorInvalid("bad_format")
    expected = _b64url(
        hmac.new(signing_key, segment.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected, signature):
        raise CursorInvalid("bad_signature")
    try:
        body = _b64url_decode(segment)
        if body[:1] == b"z":
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(body[1:], 64 * 1024)
            if decompressor.unconsumed_tail:
                raise CursorInvalid("payload_too_large")
        elif body[:1] == b"r":
            raw = body[1:]
        else:
            raise CursorInvalid("bad_payload_tag")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise CursorInvalid("bad_payload")
        if int(payload.get("v", 0)) != CURSOR_VERSION:
            raise CursorInvalid("unsupported_version")
        cursor = HistoryCursor(
            user_id=str(payload["u"]),
            snapshot_through_seq=int(payload["ss"]),
            runtime_generation=int(payload["rg"]),
            query=str(payload.get("q", "")),
            start_ts=(
                None if payload.get("t0") is None else float(payload["t0"])
            ),
            end_ts=(
                None if payload.get("t1") is None else float(payload["t1"])
            ),
            resume_seq=int(payload["rs"]),
            expires_at=float(payload["exp"]),
        )
    except CursorInvalid:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        zlib.error,
        UnicodeDecodeError,
    ):
        raise CursorInvalid("bad_payload") from None
    if not cursor.expires_at > float(now):
        raise CursorInvalid("expired")
    return cursor


def verify_cursor_binding(
    cursor: HistoryCursor,
    *,
    user_id: str,
    runtime_generation: int,
) -> None:
    if not hmac.compare_digest(str(cursor.user_id), str(user_id)):
        raise CursorInvalid("user_binding")
    if int(cursor.runtime_generation) != int(runtime_generation):
        raise CursorInvalid("generation_changed")


def verify_cursor_request(
    cursor: HistoryCursor,
    *,
    query: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> None:
    if limit is not None:
        raise CursorMismatch("limit")
    if query is not None and normalize_query(query) != cursor.query:
        raise CursorMismatch("query")
    if start is not None and parse_rfc3339_utc(start) != cursor.start_ts:
        raise CursorMismatch("start")
    if end is not None and parse_rfc3339_utc(end) != cursor.end_ts:
        raise CursorMismatch("end")
