"""Durable export queue. All transitions are serialized with account deletion."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from psycopg.rows import dict_row

import db


class AdmissionError(Exception):
    def __init__(self, code: str, status: int, job: dict | None = None):
        self.code, self.status, self.job = code, status, job


def lock_user(conn, user_id: str, *, skip_locked: bool = False) -> bool:
    # Same lock order as db.delete_user: lifecycle fence before users row.
    with conn.cursor() as cur:
        db._lock_chat_user_fence_on_cursor(cur, user_id, exclusive=False)
    tail = " SKIP LOCKED" if skip_locked else ""
    return conn.execute(
        "SELECT 1 FROM users WHERE user_id=%s FOR UPDATE" + tail, (user_id,)
    ).fetchone() is not None


def create(user_id: str, email: str, daily_limit: int) -> dict:
    with db.get_pool().connection() as conn, conn.transaction():
        if not lock_user(conn, user_id):
            raise AdmissionError("export_job_not_found", 404)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM user_export_jobs WHERE user_id=%s "
                        "AND status IN ('queued','processing') LIMIT 1", (user_id,))
            active = cur.fetchone()
            if active:
                raise AdmissionError("export_in_progress", 409, active)
            cur.execute("SELECT count(*) AS n FROM user_export_jobs WHERE user_id=%s "
                        "AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
                        "AT TIME ZONE 'UTC'", (user_id,))
            if cur.fetchone()["n"] >= daily_limit:
                raise AdmissionError("export_daily_limit", 429)
            jid = uuid.uuid4().hex
            # Key is known before upload, so even a crash after PUT is cleanable.
            key = f"user-exports/{user_id}/{jid}.zip"
            cur.execute("INSERT INTO user_export_jobs(job_id,user_id,email,object_key) "
                        "VALUES(%s,%s,%s,%s) RETURNING *", (jid, user_id, email, key))
            return cur.fetchone()


def get(user_id: str, job_id: str | None = None) -> dict | None:
    with db.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if job_id is None:
                cur.execute("SELECT * FROM user_export_jobs WHERE user_id=%s "
                            "ORDER BY created_at DESC,job_id DESC LIMIT 1", (user_id,))
            else:
                cur.execute("SELECT * FROM user_export_jobs WHERE user_id=%s AND job_id=%s",
                            (user_id, job_id))
            row = cur.fetchone()
    return row


def public(job: dict) -> dict:
    status = job["status"]
    expires = job.get("expires_at")
    if status == "completed" and expires is not None and expires <= datetime.now(timezone.utc):
        status = "expired"
    return {"job_id": job["job_id"], "status": status,
            "created_at": job["created_at"].isoformat(),
            "expires_at": expires.isoformat() if expires else None,
            "error_code": job.get("error_code")}
