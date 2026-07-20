"""Single-leader scheduler for immutable completed-day DAU snapshots."""

from __future__ import annotations

import logging
import os
import threading
import time

import db


log = logging.getLogger("feedling.dau_snapshot")


def _interval() -> float:
    try:
        return max(60.0, float(os.environ.get("FEEDLING_DAU_SNAPSHOT_INTERVAL_SEC", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


def _tick(*, now_epoch: float | None = None) -> list[str]:
    inserted = db.freeze_completed_dau_days(now_epoch=now_epoch, tz="Asia/Shanghai")
    if inserted:
        log.info("[dau-snapshot] froze completed Beijing days: %s", ",".join(inserted))
    # Growth + retention ride the same single-leader tick (same cadence, same
    # deletion-proofing rationale). A failure in one must not starve the others.
    try:
        grown = db.freeze_completed_growth_days(now_epoch=now_epoch, tz="Asia/Shanghai")
        if grown:
            log.info("[growth-snapshot] froze completed Beijing days: %s", ",".join(grown))
    except Exception as e:  # noqa: BLE001
        log.warning("[growth-snapshot] tick failed: %s", e)
    try:
        cells = db.freeze_completed_retention_cohorts(now_epoch=now_epoch, tz="Asia/Shanghai")
        if cells:
            log.info("[retention-snapshot] froze cohort cells: %s", ",".join(cells))
    except Exception as e:  # noqa: BLE001
        log.warning("[retention-snapshot] tick failed: %s", e)
    return inserted


def _loop() -> None:
    # Run immediately after winning leadership, then poll cheaply. The DB
    # function is write-once and usually becomes a MIN(day) + existence check.
    while True:
        try:
            _tick()
        except Exception as e:  # noqa: BLE001 -- a scheduler must survive a bad tick
            log.warning("[dau-snapshot] tick failed: %s", e)
        time.sleep(_interval())


def start() -> None:
    """Spawn the loop after ``core.leader`` grants singleton leadership."""
    threading.Thread(target=_loop, daemon=True, name="dau-snapshot").start()
