"""Independent V2 job timeout reaper with user-visible chat failures."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from core import wake_bus
from model_api_runtime.v2 import jobs_store, status_stream

log = logging.getLogger("feedling.runtime_v2.reaper")


def reap_once(*, record_terminal_error: Callable[[str, str], None] | None = None) -> int:
    rows = jobs_store.reap_stuck_job_rows()
    for row in rows:
        if str(row.get("lane") or "") != "chat":
            continue
        user_id = str(row.get("user_id") or "")
        message = str(row.get("last_error") or "runtime_timeout")
        try:
            event = status_stream.redact_status("error")
            jobs_store.append_status_event(
                user_id,
                event["kind"],
                job_id=row.get("id"),
                label=event["label"],
                detail=event["detail"],
            )
        except Exception:  # noqa: BLE001 — terminal state is already durable
            log.exception("failed to emit timeout status for job=%s", row.get("id"))
        if record_terminal_error is not None:
            try:
                record_terminal_error(user_id, message)
            except Exception:  # noqa: BLE001 — preserve the original terminal state
                log.exception("failed to record runtime timeout for job=%s", row.get("id"))
        try:
            wake_bus.notify("chat", user_id)
        except Exception:  # noqa: BLE001 — poll timeout remains the fallback
            log.exception("failed to wake chat poller for job=%s", row.get("id"))
    return len(rows)


async def run_loop(
    stop_event: asyncio.Event,
    *,
    interval: float,
    record_terminal_error: Callable[[str, str], None] | None = None,
) -> None:
    """Run in either the worker or backend process; DB UPDATE is its leader election."""
    while not stop_event.is_set():
        try:
            count = await asyncio.to_thread(
                reap_once, record_terminal_error=record_terminal_error)
            if count:
                log.info("expired %d overdue V2 job(s)", count)
        except Exception:  # noqa: BLE001 — a watchdog must survive transient DB failures
            log.exception("V2 job reaper pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
