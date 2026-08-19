"""Single-leader scheduler for immutable per-user lane-outcome rollup cells.

Same shape as dau_snapshot_scheduler (advisory-lock singleton, cheap poll):
db.freeze_completed_lane_days is write-once per (user, day, route, lane,
enqueue_source) cell and bounds its own catch-up work per tick, so the first
run backfills agent_jobs history across a few ticks and steady state is one
day per Beijing midnight. See migration 0091 for the table rationale.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import db


log = logging.getLogger("feedling.lane_rollup")


def _interval() -> float:
    try:
        return max(60.0, float(os.environ.get("FEEDLING_LANE_ROLLUP_INTERVAL_SEC", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


def _tick(*, now_epoch: float | None = None) -> list[str]:
    frozen = db.freeze_completed_lane_days(now_epoch=now_epoch, tz="Asia/Shanghai")
    if frozen:
        log.info("[lane-rollup] froze completed Beijing days: %s", ",".join(frozen))
    # resident (user_logs) source rides the same single-leader tick; a failure
    # in one source must not starve the other (same pattern as dau_snapshot's
    # growth/retention riders).
    try:
        resident = db.freeze_completed_resident_lane_days(
            now_epoch=now_epoch, tz="Asia/Shanghai")
        if resident:
            log.info("[lane-rollup] froze resident Beijing days: %s",
                     ",".join(resident))
    except Exception as e:  # noqa: BLE001
        log.warning("[lane-rollup] resident tick failed: %s", e)
    # chat cells (期3b) ride the same leader tick, in their own failure domain.
    # Without this the data-track endpoint reads empty history forever — the
    # freeze is the ONLY thing that fills it, so an unwired scheduler looks
    # exactly like "these users never talked".
    try:
        chat = db.freeze_completed_chat_days(
            now_epoch=now_epoch, tz="Asia/Shanghai")
        if chat:
            log.info("[lane-rollup] froze chat Beijing days: %s", ",".join(chat))
    except Exception as e:  # noqa: BLE001
        log.warning("[lane-rollup] chat tick failed: %s", e)
    return frozen


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as e:  # noqa: BLE001 -- a scheduler must survive a bad tick
            log.warning("[lane-rollup] tick failed: %s", e)
        time.sleep(_interval())


def start() -> None:
    """Spawn the loop after ``core.leader`` grants singleton leadership."""
    threading.Thread(target=_loop, daemon=True, name="lane-rollup").start()
