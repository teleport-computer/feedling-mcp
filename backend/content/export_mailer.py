"""Cloudflare Email Sending transport; no recipient/content/provider-body logging."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)
SEND_DEADLINE_SECONDS = 8
MAX_RESPONSE_BYTES = 65536
_EMAIL = re.compile(r"[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+")


class MailDeliveryError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _allowlist() -> set[str]:
    value = os.environ.get("FEEDLING_EXPORT_EMAIL_ALLOWLIST", "").strip()
    if value == "*":
        return {"*"}
    entries = {part.strip().casefold() for part in value.split(",")}
    if not entries or any(not _EMAIL.fullmatch(part) for part in entries):
        return set()
    return entries


def configured() -> bool:
    token = os.environ.get("CLOUDFLARE_EMAIL_TOKEN", "").strip()
    account = os.environ.get("CLOUDFLARE_EMAIL_ACCOUNT_ID", "").strip()
    sender = os.environ.get("FEEDLING_EXPORT_EMAIL_FROM", "").strip()
    return bool(token and not re.search(r"\s", token)
                and re.fullmatch(r"[a-fA-F0-9]{32}", account)
                and _EMAIL.fullmatch(sender) and _allowlist())


class CloudflareExportMailer:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    def check_recipient(self, email: str) -> None:
        if not configured():
            raise MailDeliveryError("export_email_unconfigured")
        allowed = _allowlist()
        if "*" not in allowed and email.casefold() not in allowed:
            log.info("export_email domain=%s success=false status=0 code=recipient_not_allowed",
                     email.rpartition("@")[2])
            raise MailDeliveryError("export_recipient_not_allowed")

    def send(self, *, email: str, download_url: str, job_id: str) -> None:
        self.check_recipient(email)  # Recheck policy immediately before HTTP.
        asyncio.run(self._send(email, download_url, job_id))

    async def _send(self, email: str, url: str, job_id: str) -> None:
        # No token stored on this object, included in exception messages or copied to logs.
        account = os.environ["CLOUDFLARE_EMAIL_ACCOUNT_ID"].strip()
        endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account}/email/sending/send"
        payload = {
            "from": os.environ["FEEDLING_EXPORT_EMAIL_FROM"].strip(),
            "to": email,
            "subject": "IO 数据导出 / Your IO data export",
            "text": ("您的 IO 数据导出已准备好。下载链接 7 天内有效，请勿转发。\n"
                     "Your IO data export is ready. This private download link is valid "
                     "for 7 days. Do not forward it.\n\n" + url),
            # Correlation only: Cloudflare documents no idempotency guarantee for this header.
            "headers": {"X-IO-Export-Job": job_id},
        }
        status, provider_code = 0, 0
        domain = email.rpartition("@")[2]
        try:
            # Total deadline includes connect, headers and the bounded response stream.
            async with asyncio.timeout(SEND_DEADLINE_SECONDS):
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=httpx.Timeout(5, connect=3),
                    trust_env=False, follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST", endpoint, json=payload,
                        headers={"Authorization": "Bearer " +
                                 os.environ["CLOUDFLARE_EMAIL_TOKEN"].strip()},
                    ) as response:
                        status = response.status_code
                        raw = bytearray()
                        oversized = False
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_RESPONSE_BYTES:
                                oversized = True
                                break
                        try:
                            result = {} if oversized else json.loads(raw)
                        except (ValueError, UnicodeError):
                            result = {}
                        errors = result.get("errors") if isinstance(result, dict) else None
                        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                            value = errors[0].get("code")
                            provider_code = value if type(value) is int else 0
                        if status == 429 or status >= 500:
                            raise MailDeliveryError("export_email_transient", retryable=True)
                        if not 200 <= status < 300:
                            raise MailDeliveryError("export_email_rejected")
                        if oversized or not isinstance(result, dict) or not result:
                            raise MailDeliveryError("export_email_invalid_response")
                        delivery = result.get("result")
                        if result.get("success") is not True or not isinstance(delivery, dict):
                            raise MailDeliveryError("export_email_rejected")
                        recipient = email.casefold()
                        def contains(field: str) -> bool:
                            values = delivery.get(field)
                            return isinstance(values, list) and any(
                                isinstance(v, str) and v.casefold() == recipient for v in values)
                        if (contains("permanent_bounces") or contains("suppressed_recipients")
                                or not (contains("delivered") or contains("queued"))
                                or not isinstance(delivery.get("message_id"), str)
                                or not delivery["message_id"]):
                            raise MailDeliveryError("export_email_not_accepted")
        except (TimeoutError, httpx.TimeoutException):
            # A stalled diagnostic body must not turn a known permanent rejection
            # into a retry. Before headers, or for 429/5xx/2xx ambiguity, retry.
            permanent = 300 <= status < 500 and status != 429
            code = "export_email_rejected" if permanent else "export_email_timeout"
            log.warning("export_email domain=%s success=false status=%s code=%s provider_code=0",
                        domain, status, "rejected" if permanent else "timeout")
            raise MailDeliveryError(code, retryable=not permanent) from None
        except httpx.RequestError:
            permanent = 300 <= status < 500 and status != 429
            code = "export_email_rejected" if permanent else "export_email_transient"
            log.warning("export_email domain=%s success=false status=%s code=%s provider_code=0",
                        domain, status, "rejected" if permanent else "network")
            raise MailDeliveryError(code, retryable=not permanent) from None
        except MailDeliveryError as exc:
            log.warning("export_email domain=%s success=false status=%s code=%s provider_code=%s",
                        domain, status, exc.code, provider_code)
            raise
        log.info("export_email domain=%s success=true status=%s code=accepted", domain, status)
