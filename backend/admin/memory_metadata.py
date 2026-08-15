"""Content-free admin reads for memory cards and Runtime V2 dream jobs.

This module is intentionally a projection boundary, not a generic row serializer.
Neither query selects an encrypted body/prompt/reply column, and the result builders
name every returned field.  Adding a column to either source table therefore cannot
silently expand the admin response.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

import db

_CARD_LIMIT_DEFAULT = 100
_JOB_LIMIT_DEFAULT = 100
_LIMIT_MAX = 500
_OFFSET_MAX = 1_000_000

CARD_FIELDS = frozenset(
    {
        "id",
        "occurred_at",
        "created_at",
        "supersedes",
        "superseded_by",
        "source",
        "archived",
    }
)
DREAM_JOB_FIELDS = frozenset(
    {
        "job_id",
        "user_id",
        "lane",
        "status",
        "failure_code",
        "duration_ms",
        "provider",
        "model",
        "memory_card_count_now",
        "created_at",
        "finished_at",
    }
)

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,95}$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_:-]{1,120}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def pagination(raw_limit: object, raw_offset: object, *, jobs: bool = False) -> tuple[int, int]:
    """Return bounded pagination without creating a new public error contract."""
    default = _JOB_LIMIT_DEFAULT if jobs else _CARD_LIMIT_DEFAULT
    try:
        limit = int(str(raw_limit)) if raw_limit not in (None, "") else default
    except (TypeError, ValueError, OverflowError):
        limit = default
    try:
        offset = int(str(raw_offset)) if raw_offset not in (None, "") else 0
    except (TypeError, ValueError, OverflowError):
        offset = 0
    return max(1, min(limit, _LIMIT_MAX)), max(0, min(offset, _OFFSET_MAX))


def _safe_id(value: object) -> str:
    text = str(value or "").strip()
    return text if _ID_RE.fullmatch(text) else ""


def _safe_label(value: object) -> str:
    text = str(value or "").strip()
    return text if _LABEL_RE.fullmatch(text) else "unknown"


def _safe_failure_code(value: object) -> str:
    text = str(value or "").strip()
    return text if _ERROR_CODE_RE.fullmatch(text) else "runtime_failed"


def _safe_timestamp(value: object) -> str:
    """Accept only real ISO dates/timestamps; metadata fields cannot smuggle prose."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = value.isoformat()
    elif isinstance(value, date):
        text = value.isoformat()
    else:
        text = str(value).strip()
    if _DATE_RE.fullmatch(text):
        try:
            date.fromisoformat(text)
        except ValueError:
            return ""
        return text
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    normalized = parsed.isoformat()
    return normalized[:-6] + "Z" if normalized.endswith("+00:00") else normalized


def _safe_supersedes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:100]:
        safe = _safe_id(item)
        if safe and safe not in result:
            result.append(safe)
    return result


def card_metadata_from_row(row: Mapping[str, Any]) -> dict:
    """Fixed card projection; unexpected row/doc keys are unrepresentable."""
    result = {
        "id": _safe_id(row.get("id")),
        "occurred_at": _safe_timestamp(row.get("occurred_at")),
        "created_at": _safe_timestamp(row.get("created_at")),
        "supersedes": _safe_supersedes(row.get("supersedes")),
        "superseded_by": _safe_id(row.get("superseded_by")),
        "source": _safe_label(row.get("source")),
        "archived": bool(row.get("archived")),
    }
    assert result.keys() == CARD_FIELDS
    return result


def dream_job_metadata_from_row(row: Mapping[str, Any]) -> dict:
    """Fixed job projection; last_error is reduced to a stable short code.

    ``duration_ms`` is processing wall time: ``finished_at`` minus ``started_at``;
    old/incomplete rows fall back to ``claimed_at`` and finally ``created_at``.
    ``memory_card_count_now`` is a query-time snapshot, never the job's input size.
    """
    duration = row.get("duration_ms")
    try:
        safe_duration = max(0, int(duration)) if duration is not None else None
    except (TypeError, ValueError, OverflowError):
        safe_duration = None
    result = {
        "job_id": int(row.get("job_id") or 0),
        "user_id": _safe_id(row.get("user_id")),
        "lane": "dream",
        "status": _safe_label(row.get("status")),
        "failure_code": (
            _safe_failure_code(row.get("failure_code"))
            if row.get("failure_code")
            else ""
        ),
        "duration_ms": safe_duration,
        "provider": _safe_label(row.get("provider")),
        "model": _safe_label(row.get("model")),
        "memory_card_count_now": max(
            0, int(row.get("memory_card_count_now") or 0)
        ),
        "created_at": _safe_timestamp(row.get("created_at")),
        "finished_at": _safe_timestamp(row.get("finished_at")),
    }
    assert result.keys() == DREAM_JOB_FIELDS
    return result


def _pagination_payload(*, limit: int, offset: int, total: int, returned: int) -> dict:
    return {
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + returned < total,
    }


def list_card_metadata(user_id: str, *, limit: int, offset: int) -> dict:
    """List one user's card lifecycle metadata without selecting ``doc`` itself."""
    safe_user_id = str(user_id or "")[:200]
    with db.get_pool().connection() as conn:
        total = int(
            conn.execute(
                "SELECT count(*) FROM memory_moments WHERE user_id=%s",
                (safe_user_id,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT moment_id AS id,
                   occurred_at,
                   doc->>'created_at' AS created_at,
                   CASE WHEN jsonb_typeof(doc->'supersedes')='array'
                        THEN doc->'supersedes' ELSE '[]'::jsonb END AS supersedes,
                   doc->>'superseded_by' AS superseded_by,
                   COALESCE(NULLIF(doc->>'source',''),
                            NULLIF(doc->>'capture_mode',''), 'unknown') AS source,
                   ((doc->>'is_archived')='true'
                     OR COALESCE(doc->>'archived_at','')<>''
                     OR COALESCE(doc->>'archive_reason','')<>'') AS archived
            FROM memory_moments
            WHERE user_id=%s
            ORDER BY occurred_at DESC, moment_id DESC
            LIMIT %s OFFSET %s
            """,
            (safe_user_id, limit, offset),
        ).fetchall()
    cards = [
        card_metadata_from_row(
            {
                "id": row[0],
                "occurred_at": row[1],
                "created_at": row[2],
                "supersedes": row[3],
                "superseded_by": row[4],
                "source": row[5],
                "archived": row[6],
            }
        )
        for row in rows
    ]
    return {
        "user_id": _safe_id(safe_user_id),
        "cards": cards,
        "pagination": _pagination_payload(
            limit=limit, offset=offset, total=total, returned=len(cards)
        ),
    }


def list_dream_job_metadata(
    *,
    limit: int,
    offset: int,
    user_id: str = "",
    status: str = "",
) -> dict:
    """List content-free dream diagnostics, optionally narrowed by user/status."""
    safe_user_id = str(user_id or "")[:200]
    safe_status = str(status or "")[:80]
    clauses = ["j.lane='dream'"]
    params: list[object] = []
    if safe_user_id:
        clauses.append("j.user_id=%s")
        params.append(safe_user_id)
    if safe_status:
        clauses.append("j.status=%s")
        params.append(safe_status)
    where = " AND ".join(clauses)
    with db.get_pool().connection() as conn:
        total = int(
            conn.execute(
                f"SELECT count(*) FROM agent_jobs j WHERE {where}",
                tuple(params),
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT j.id AS job_id,
                   j.user_id,
                   j.status,
                   CASE WHEN j.last_error ~ '^[a-z0-9_:-]{{1,120}}$'
                        THEN j.last_error
                        WHEN j.last_error IS NULL OR j.last_error=''
                        THEN '' ELSE 'runtime_failed' END AS failure_code,
                   CASE WHEN j.finished_at IS NOT NULL THEN
                     GREATEST(0, ROUND(EXTRACT(EPOCH FROM
                       (j.finished_at-COALESCE(j.started_at,j.claimed_at,j.created_at))
                     )*1000))::bigint ELSE NULL END AS duration_ms,
                   COALESCE(metric.provider, 'unknown') AS provider,
                   COALESCE(metric.model, 'unknown') AS model,
                   (SELECT count(*) FROM memory_moments mm
                    WHERE mm.user_id=j.user_id)::bigint AS memory_card_count_now,
                   j.created_at,
                   j.finished_at
            FROM agent_jobs j
            LEFT JOIN LATERAL (
              SELECT m.provider, m.model
              FROM v2_turn_metrics m
              WHERE m.job_id=j.id
              ORDER BY m.created_at DESC, m.id DESC
              LIMIT 1
            ) metric ON TRUE
            WHERE {where}
            ORDER BY j.created_at DESC, j.id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [limit, offset]),
        ).fetchall()
    jobs = [
        dream_job_metadata_from_row(
            {
                "job_id": row[0],
                "user_id": row[1],
                "status": row[2],
                "failure_code": row[3],
                "duration_ms": row[4],
                "provider": row[5],
                "model": row[6],
                "memory_card_count_now": row[7],
                "created_at": row[8],
                "finished_at": row[9],
            }
        )
        for row in rows
    ]
    return {
        "jobs": jobs,
        "filters": {"user_id": _safe_id(safe_user_id), "status": safe_status},
        "pagination": _pagination_payload(
            limit=limit, offset=offset, total=total, returned=len(jobs)
        ),
    }
