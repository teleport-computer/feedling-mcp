"""Authoritative content-encryption policy reads for offline TEE jobs."""
from __future__ import annotations

import db


def _pool(pool=None):
    return pool if pool is not None else db.get_pool()


def resolve_content_encryption(user_id: str, *, pool=None) -> str | None:
    """Return ``on``, effective ``off``, or ``None`` for an unknown user."""
    with _pool(pool).connection() as conn:
        row = conn.execute(
            "SELECT doc->>'content_encryption' FROM users WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    if row is None:
        return None
    value = str(row[0] or "").strip().lower()
    return "on" if value == "on" else "off"


def probe_policy_source(*, pool=None) -> dict[str, int]:
    """Verify the users source is readable and return content-free counts."""
    with _pool(pool).connection() as conn:
        row = conn.execute(
            "SELECT count(*), "
            "count(*) FILTER (WHERE lower(trim(coalesce("
            "doc->>'content_encryption', ''))) = 'on'), "
            "count(*) FILTER (WHERE lower(trim(coalesce("
            "doc->>'content_encryption', ''))) = 'off') "
            "FROM users"
        ).fetchone()
    users, explicit_on, explicit_off = (int(value) for value in row)
    return {
        "users": users,
        "explicit_on": explicit_on,
        "explicit_off": explicit_off,
        "default_off": users - explicit_on - explicit_off,
    }
