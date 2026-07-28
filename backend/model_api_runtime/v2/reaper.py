"""Independent V2 watchdog and isolated chat-object cleanup loops."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Callable

import db
from model_api_runtime.v2 import jobs_store

log = logging.getLogger("feedling.runtime_v2.reaper")


def reconcile_once(
    *, record_terminal_error: Callable[[str, str], None] | None = None,
) -> dict[str, int]:
    """Replay durable failure visibility independently of deadline selection."""
    return jobs_store.reconcile_terminal_failure_outbox(
        record_terminal_error=record_terminal_error,
    )


def reap_once(*, record_terminal_error: Callable[[str, str], None] | None = None) -> int:
    """Expire overdue jobs and reconcile their durable visibility markers.

    Deliberately contains no object-store I/O.  R2 LIST/DELETE may consume many
    bounded network timeouts in one pass; coupling it to this function would
    delay the deadline watchdog precisely when workers are unhealthy.
    """
    rows = jobs_store.reap_stuck_job_rows()
    # Drain *all* outstanding markers, not just rows terminalized by this pass.
    # That is the crash recovery contract: a prior worker/reaper may have died
    # immediately after the atomic job+marker commit, or any sink may have
    # failed transiently.  The reconciler is independently idempotent.
    reconciled = reconcile_once(record_terminal_error=record_terminal_error)
    if (
        reconciled["status_delivered"]
        or reconciled["runtime_error_delivered"]
        or reconciled["reply_delivered"]
    ):
        log.info(
            "reconciled V2 terminal failures status=%d runtime_error=%d reply=%d",
            reconciled["status_delivered"],
            reconciled["runtime_error_delivered"],
            reconciled["reply_delivered"],
        )
    return len(rows)


def cleanup_once(*, limit: int = 25, inventory_limit: int = 1) -> int:
    """Run one bounded durable R2 cleanup pass outside the job watchdog."""
    bounded_limit = int(limit)
    bounded_inventory_limit = int(inventory_limit)
    if bounded_limit <= 0 or bounded_inventory_limit <= 0:
        raise ValueError("R2 cleanup limits must be positive integers")
    return db.reconcile_chat_r2_cleanup(
        limit=bounded_limit,
        inventory_limit=bounded_inventory_limit,
    )


def cleanup_settings_from_env() -> dict[str, float | int]:
    """Read fail-closed cleanup-loop settings shared by API and worker."""
    raw_interval = os.environ.get(
        "FEEDLING_CHAT_R2_CLEANUP_INTERVAL_SEC", "30"
    ).strip()
    raw_limit = os.environ.get("FEEDLING_CHAT_R2_CLEANUP_BATCH", "25").strip()
    raw_inventory = os.environ.get(
        "FEEDLING_CHAT_R2_INVENTORY_BATCH", "1"
    ).strip()
    try:
        interval = float(raw_interval)
        limit = int(raw_limit)
        inventory_limit = int(raw_inventory)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid chat R2 cleanup loop configuration") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise RuntimeError(
            "FEEDLING_CHAT_R2_CLEANUP_INTERVAL_SEC must be finite and > 0"
        )
    if limit <= 0 or inventory_limit <= 0:
        raise RuntimeError("chat R2 cleanup batch sizes must be positive integers")
    return {
        "interval": interval,
        "limit": limit,
        "inventory_limit": inventory_limit,
    }


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


async def run_cleanup_loop(
    stop_event: asyncio.Event,
    *,
    interval: float,
    limit: int = 25,
    inventory_limit: int = 1,
) -> None:
    """Drain durable chat-object cleanup without ever delaying job expiry."""
    if not math.isfinite(float(interval)) or float(interval) <= 0:
        raise ValueError("cleanup interval must be finite and > 0")
    while not stop_event.is_set():
        try:
            count = await asyncio.to_thread(
                cleanup_once,
                limit=limit,
                inventory_limit=inventory_limit,
            )
            if count:
                log.info("reclaimed %d retired chat R2 object(s)", count)
        except Exception:  # noqa: BLE001 — cleanup must survive transient R2/DB failures
            log.exception("chat R2 cleanup pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval))
        except asyncio.TimeoutError:
            pass
