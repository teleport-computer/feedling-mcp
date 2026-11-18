"""User-initiated exports with explicit logging or Cloudflare email delivery."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from psycopg.rows import dict_row

import db
import object_storage
from content import export_archive, export_store, export_mailer
from core import runtime_token

log = logging.getLogger(__name__)


class ExportMailer(Protocol):
    def send(self, *, email: str, download_url: str, job_id: str) -> None:
        """job_id correlates attempts; transport-specific deduplication is not guaranteed."""


class LogExportMailer:
    def send(self, *, email: str, download_url: str, job_id: str) -> None:
        # Never record arbitrary recipient domains/addresses or signed URLs.
        log.info("export_mail_placeholder job=%s link_generated=%s",
                 job_id, bool(download_url))


def configured() -> bool:
    return (os.environ.get("FEEDLING_EXPORT_ENABLED") == "1"
            and (os.environ.get("FEEDLING_EXPORT_MAILER", "log") == "log"
                 or (os.environ.get("FEEDLING_EXPORT_MAILER") == "cloudflare"
                     and export_mailer.configured()))
            and bool(os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip())
            and object_storage.exports_enabled())


def submit(user_id: str, payload: object) -> tuple[dict, int]:
    email = payload.get("email") if isinstance(payload, dict) else None
    if (not isinstance(email, str) or len(email) > 254
            or not re.fullmatch(r"[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+",
                                email)):
        return {"error": "invalid_email", "error_code": "invalid_email"}, 400
    if not configured():
        return {"error": "export_unavailable", "error_code": "export_unavailable"}, 503
    limit = max(1, int(os.environ.get("FEEDLING_EXPORT_DAILY_LIMIT", "3")))
    try:
        job = export_store.create(user_id, email, limit)
    except export_store.AdmissionError as exc:
        body = {"error": exc.code, "error_code": exc.code}
        if exc.job:
            body.update(export_store.public(exc.job))
            body["error_code"] = exc.code
        return body, exc.status
    return export_store.public(job), 202


def status(user_id: str, job_id: str | None = None) -> tuple[dict, int]:
    job = export_store.get(user_id, job_id)
    if job is None:
        return {"error": "export_job_not_found", "error_code": "export_job_not_found"}, 404
    return export_store.public(job), 200


def _token(user_id: str) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode()
    if not secret:
        raise RuntimeError("export_token_unavailable")
    return runtime_token.mint(secret, user_id=user_id, runtime_instance_id="user-export",
                              scope=["envelope_decrypt"], ttl=1800)


def cleanup_expired() -> int:
    count = 0
    with db.get_pool().connection() as conn, conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT job_id,object_key,status FROM user_export_jobs "
                        "WHERE expires_at<=now() AND status IN ('completed','failed') "
                        "ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT 20")
            for job in cur.fetchall():
                object_storage.delete_export(job["object_key"])
                cur.execute("UPDATE user_export_jobs SET status=%s,email=NULL,expires_at=NULL "
                            "WHERE job_id=%s",
                            ("expired" if job["status"] == "completed" else "failed",
                             job["job_id"]))
                count += 1
    return count


def tick(mailer: ExportMailer | None = None) -> bool:
    if object_storage.exports_enabled():
        cleanup_expired()
    if not configured():
        return False
    # A transaction-scoped advisory claim survives network work while a separate
    # short transaction publishes processing state. On process death PostgreSQL
    # releases the claim; another worker resumes the SAME durable job/key.
    with db.get_pool().connection() as conn:
        candidates = conn.execute(
            "SELECT job_id,user_id FROM user_export_jobs WHERE status IN ('queued','processing') "
            "AND available_at<=now() ORDER BY created_at LIMIT 20").fetchall()
    for jid, uid in candidates:
        with db.get_pool().connection() as conn, conn.transaction():
            if not conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('user-export:' || %s,837))",
                (jid,),
            ).fetchone()[0]:
                continue
            with db.get_pool().connection() as claim, claim.transaction():
                with claim.cursor(row_factory=dict_row) as cur:
                    cur.execute("UPDATE user_export_jobs SET status='processing',"
                                "attempts=attempts+1 WHERE job_id=%s AND "
                                "status IN ('queued','processing') AND available_at<=now() "
                                "RETURNING *", (jid,))
                    job = cur.fetchone()
            if job is None:
                continue
            with conn.cursor(row_factory=dict_row) as cur:
                try:
                    if job["attempts"] > 3:
                        raise RuntimeError("export_attempts_exhausted")
                    delivery = mailer
                    if delivery is None:
                        if os.environ.get("FEEDLING_EXPORT_MAILER", "log") == "cloudflare":
                            delivery = export_mailer.CloudflareExportMailer()
                            delivery.check_recipient(job["email"])  # Before build/upload.
                        else:
                            delivery = LogExportMailer()
                    with tempfile.TemporaryDirectory(prefix="io-export-") as tmp:
                        path = Path(tmp) / "export.zip"
                        export_archive.build(uid, _token(uid), path)
                        # Building may take minutes. Account deletion must be
                        # able to cancel it without waiting (or locking whoami).
                        # Only publication takes the lifecycle fence, followed
                        # by a fresh authoritative user/job check.
                        db._lock_chat_user_fence_on_cursor(cur, uid, exclusive=False)
                        cur.execute("SELECT 1 FROM users u JOIN user_export_jobs j "
                                    "ON j.user_id=u.user_id WHERE u.user_id=%s "
                                    "AND j.job_id=%s AND j.status='processing'", (uid,jid))
                        if cur.fetchone() is None:
                            return True  # Deleted during build: discard the result.
                        object_storage.upload_export(job["object_key"], str(path))
                    # The shared lifecycle fence excludes account deletion until
                    # PUT/mail/DB completion finish; no user-row lock delays status
                    # queries or admission. Cloudflare network work has an 8s total deadline.
                    expires = datetime.now(timezone.utc) + timedelta(days=7)
                    url = object_storage.export_download_url(job["object_key"])
                    delivery.send(
                        email=job["email"], download_url=url, job_id=jid)
                    cur.execute("UPDATE user_export_jobs SET status='completed',email=NULL,"
                                "expires_at=%s,error_code=NULL WHERE job_id=%s", (expires, jid))
                except Exception as exc:  # No user content/URLs/addresses in diagnostics.
                    log.warning("export_job_failed job=%s type=%s", jid, type(exc).__name__)
                    # If upload succeeded but mail failed, delete before retry.
                    # The known key remains recorded even if deletion fails.
                    try:
                        object_storage.delete_export(job["object_key"])
                    except Exception:
                        log.warning("export_cleanup_pending job=%s", jid)
                    mail_error = isinstance(exc, export_mailer.MailDeliveryError)
                    terminal = job["attempts"] >= 3 or (mail_error and not exc.retryable)
                    code = exc.code if mail_error else "export_failed"
                    cur.execute("UPDATE user_export_jobs SET status=%s,email=CASE WHEN %s "
                                "THEN NULL ELSE email END,error_code=%s,"
                                "available_at=now()+interval '1 minute',"
                                "expires_at=CASE WHEN %s THEN now() ELSE NULL END WHERE job_id=%s",
                                ("failed" if terminal else "queued", terminal, code, terminal, jid))
                return True
    return False


async def run(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception as exc:
            log.warning("export_worker_unavailable type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
