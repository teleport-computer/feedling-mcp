"""Content-free durable counters for rejected public contract values."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import threading
import time
import uuid

from notices import error_contract


HEADER_NAME = "X-Feedling-Contract-Rejections"
MAX_HEADER_BYTES = 8192
MAX_REPORT_ROWS = 32
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:@+-]{1,96}")
_log = logging.getLogger(__name__)


def _safe_token(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:@+-]+", "_", str(value or ""))[:96]
    return cleaned or fallback


@dataclass(frozen=True, slots=True)
class RejectionCounter:
    contract_domain: str
    boundary: str
    fallback: str
    total: int
    first_seen: float
    last_seen: float


class ResidentRejectionReporter:
    """Process-local absolute counters safe to repeat on every HTTP request."""

    def __init__(self, *, writer_id: str, release_sha: str):
        if not _TOKEN_RE.fullmatch(writer_id):
            raise ValueError("invalid rejection writer_id")
        if not _TOKEN_RE.fullmatch(release_sha):
            raise ValueError("invalid rejection release_sha")
        self.writer_id = writer_id
        self.release_sha = release_sha
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str, str], RejectionCounter] = {}

    def record(self, domain: str, boundary: str, fallback: str) -> str:
        _validate_dimensions(domain, boundary, fallback)
        now = time.time()
        key = (domain, boundary, fallback)
        with self._lock:
            previous = self._rows.get(key)
            self._rows[key] = RejectionCounter(
                contract_domain=domain,
                boundary=boundary,
                fallback=fallback,
                total=(previous.total + 1) if previous else 1,
                first_seen=previous.first_seen if previous else now,
                last_seen=now,
            )
            return self._header_value_locked()

    def header_value(self) -> str:
        with self._lock:
            return self._header_value_locked()

    def _header_value_locked(self) -> str:
        rows = sorted(self._rows.values(), key=lambda row: (
            row.contract_domain, row.boundary, row.fallback
        ))
        return json.dumps({
            "writer_id": self.writer_id,
            "release_sha": self.release_sha,
            "rows": [
                {
                    "contract_domain": row.contract_domain,
                    "boundary": row.boundary,
                    "fallback": row.fallback,
                    "total": row.total,
                    "first_seen": row.first_seen,
                    "last_seen": row.last_seen,
                }
                for row in rows
            ],
        }, separators=(",", ":"), sort_keys=True)


def _validate_dimensions(domain: str, boundary: str, fallback: str) -> None:
    error_contract.validate_rejection_dimensions(domain, boundary, fallback)


def parse_resident_header(value: str) -> list[tuple]:
    """Validate the controlled wire report and return DB-upsert tuples."""
    encoded = str(value or "")
    if not encoded:
        return []
    if len(encoded.encode("utf-8")) > MAX_HEADER_BYTES:
        raise ValueError("contract rejection report too large")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("invalid contract rejection report")
    writer_id = str(payload.get("writer_id") or "")
    release_sha = str(payload.get("release_sha") or "")
    if not _TOKEN_RE.fullmatch(writer_id) or not _TOKEN_RE.fullmatch(release_sha):
        raise ValueError("invalid contract rejection report identity")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > MAX_REPORT_ROWS:
        raise ValueError("invalid contract rejection report rows")
    rows: list[tuple] = []
    seen: set[tuple[str, str, str]] = set()
    now = time.time()
    for item in raw_rows:
        if not isinstance(item, dict):
            raise ValueError("invalid contract rejection report row")
        domain = str(item.get("contract_domain") or "")
        boundary = str(item.get("boundary") or "")
        fallback = str(item.get("fallback") or "")
        _validate_dimensions(domain, boundary, fallback)
        key = (domain, boundary, fallback)
        if key in seen:
            raise ValueError("duplicate contract rejection report row")
        seen.add(key)
        total = item.get("total")
        first_seen = item.get("first_seen")
        last_seen = item.get("last_seen")
        if isinstance(total, bool) or not isinstance(total, int) or total < 1:
            raise ValueError("invalid contract rejection total")
        if not isinstance(first_seen, (int, float)) or not isinstance(
            last_seen, (int, float)
        ):
            raise ValueError("invalid contract rejection timestamps")
        first = float(first_seen)
        last = float(last_seen)
        if first <= 0 or last < first or last > now + 300:
            raise ValueError("invalid contract rejection timestamp order")
        rows.append((
            domain, boundary, fallback, release_sha, writer_id,
            total, first, last,
        ))
    return rows


def ingest_resident_header(value: str) -> None:
    """Best-effort durable absorption; malformed reports retain no raw value."""
    try:
        rows = parse_resident_header(value)
        if rows:
            import db
            db.upsert_contract_rejection_stats(rows)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break poll
        _log.warning("contract rejection report rejected: %s", type(exc).__name__)


_HOSTED_REPORTER = ResidentRejectionReporter(
    writer_id=f"backend:{uuid.uuid4().hex}",
    release_sha=_safe_token(
        os.environ.get("FEEDLING_GIT_COMMIT") or "unknown", "unknown"
    ),
)


def record_hosted(domain: str, boundary: str, fallback: str) -> None:
    """Write a hosted rejection immediately using the same absolute contract."""
    report = _HOSTED_REPORTER.record(domain, boundary, fallback)
    ingest_resident_header(report)
