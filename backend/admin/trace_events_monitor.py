"""Single-leader, read-only health detector for TEE trace partitions."""

from __future__ import annotations

import logging
import os
import threading
import time

import db


log = logging.getLogger("feedling.trace_events")


def _interval() -> float:
    try:
        return max(
            60.0,
            float(os.environ.get("FEEDLING_TRACE_EVENTS_MONITOR_INTERVAL_SEC", "60")),
        )
    except (TypeError, ValueError):
        return 300.0


def _tick(*, now_epoch: float | None = None) -> dict:
    report = db.trace_events_partition_health(now_epoch=now_epoch)
    if report.get("ok"):
        log.info(
            "[trace-events] healthy default_rows=%s future_days=%s "
            "storage_bytes=%s projected_retained_bytes=%s",
            report.get("default_rows"),
            report.get("future_days"),
            report.get("storage_bytes"),
            report.get("projected_retained_bytes"),
        )
    else:
        # DEFAULT non-empty is intentionally red even though the owner command
        # can recover it.  The detector and executor are independent so a
        # failed or never-run maintenance command cannot look healthy.
        log.error(
            "[trace-events] degraded issues=%s default_rows=%s "
            "default_min_ts=%s default_max_ts=%s future_days=%s "
            "storage_bytes=%s projected_retained_bytes=%s budget_bytes=%s "
            "at_risk_events_today=%s at_risk_bytes_today=%s",
            ",".join(report.get("issues") or []),
            report.get("default_rows"),
            report.get("default_min_ts"),
            report.get("default_max_ts"),
            report.get("future_days"),
            report.get("storage_bytes"),
            report.get("projected_retained_bytes"),
            report.get("storage_budget_bytes"),
            report.get("at_risk_events_today"),
            report.get("at_risk_bytes_today"),
        )
    return report


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as exc:  # noqa: BLE001 -- detector must survive a tick
            log.exception("[trace-events] health query failed: %s", exc)
        time.sleep(_interval())


def start() -> None:
    threading.Thread(
        target=_loop,
        daemon=True,
        name="trace-events-monitor",
    ).start()
