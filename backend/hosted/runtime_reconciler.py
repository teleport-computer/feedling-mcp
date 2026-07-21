"""Dual-runtime canary reconciler.

Drives per-user fence (resident/draining/v2 + generation) toward the desired
runtime recorded in ``v2_user_allowlist``. Leader-elected via
``core.leader.run_singleton`` (same primitive as ``tee_sync_scheduler`` /
``dau_snapshot_scheduler``) so only one backend worker runs the loop. The send
hot path never reads the allowlist table — the fence is the routing truth —
so this loop being down only pauses *transitions*, never delivery.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import db
from hosted import config_store

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_SEC = float(os.environ.get("FEEDLING_RECONCILE_INTERVAL_SEC", "15"))
_DEFAULT_DESIRED_ENV = "FEEDLING_RUNTIME_DEFAULT_DESIRED"
_BACKOFF_BASE_SEC = 60.0
_BACKOFF_MAX_SEC = 3600.0

# user_id -> (fail_count, not_before_ts)；进程内即可——重启清零只是提早重试
_failures: dict[str, tuple[int, float]] = {}


def desired_for(user_id: str, allow_map: dict[str, str]) -> str:
    if user_id in allow_map:
        return allow_map[user_id]
    default = os.environ.get(_DEFAULT_DESIRED_ENV, "resident").strip().lower()
    return default if default in ("resident", "v2") else "resident"


def _flip_user(user_id: str, desired: str) -> None:
    """One fenced transition. Reuses admin_core.set_runtime_mode so the V2
    direction keeps its wake-schedule seeding (seed-before-persist order)."""
    from admin import admin_core  # noqa: PLC0415 — avoid import cycle at module load
    mode = (config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
            if desired == "v2" else config_store.HOSTED_RUNTIME_MODE_RESIDENT)
    body, status = admin_core.set_runtime_mode(user_id, mode)
    if status != 200:
        raise RuntimeError(f"set_runtime_mode({user_id}, {mode}) -> {status}: {body}")


def _current_actual(user_id: str) -> str | None:
    """'v2' | 'resident' | None(转换中/异常，本轮跳过)."""
    from core import store as core_store  # noqa: PLC0415
    try:
        mode, state, _gen = config_store.get_hosted_runtime_control_strict(
            core_store.get_store(user_id))
    except Exception:
        return None
    if mode == config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2 and state == "v2":
        return "v2"
    if mode == config_store.HOSTED_RUNTIME_MODE_RESIDENT and state == "resident":
        return "resident"
    return None  # draining 或不一致 tuple：等它收敛或人工介入，本轮不动


def reconcile_once() -> dict:
    stats = {"checked": 0, "flipped": 0, "failed": 0, "skipped_backoff": 0}
    allow_map = db.get_runtime_allowlist_map()
    # 范围 = 名单里的用户 + （默认为 v2 时）所有还在 resident 的托管用户。
    # P4/P5（默认 resident）阶段名单就是全部工作集；P6 翻默认后由
    # list_agent_runtime_enabled_users 提供存量 resident 用户集。
    user_ids = set(allow_map)
    if os.environ.get(_DEFAULT_DESIRED_ENV, "resident").strip().lower() == "v2":
        user_ids.update(r["user_id"] for r in db.list_agent_runtime_enabled_users())
    now = time.time()
    for uid in sorted(user_ids):
        stats["checked"] += 1
        fail_count, not_before = _failures.get(uid, (0, 0.0))
        if now < not_before:
            stats["skipped_backoff"] += 1
            continue
        desired = desired_for(uid, allow_map)
        actual = _current_actual(uid)
        if actual == desired:
            _failures.pop(uid, None)
            continue
        if actual is None:
            continue  # draining/不一致 tuple：转换中，下轮再看
        try:
            _flip_user(uid, desired)
            stats["flipped"] += 1
            _failures.pop(uid, None)
            log.info("[reconciler] flipped %s -> %s", uid, desired)
        except Exception as e:  # noqa: BLE001 — 单用户失败不挡环
            stats["failed"] += 1
            backoff = min(_BACKOFF_BASE_SEC * (2 ** fail_count), _BACKOFF_MAX_SEC)
            _failures[uid] = (fail_count + 1, now + backoff)
            log.warning("[reconciler] flip %s -> %s failed (retry in %.0fs): %s",
                        uid, desired, backoff, e)
    return stats


def _loop() -> None:
    """Runs forever on the elected leader worker, exactly like
    ``tee_sync_scheduler._loop`` / ``dau_snapshot_scheduler._loop``: a plain
    ``while True`` daemon-thread loop with no external stop signal (those
    siblings don't have one either — the thread is killed with the process).
    Each tick is wrapped so a single exception never kills the loop."""
    while True:
        try:
            stats = reconcile_once()
            if stats["flipped"] or stats["failed"]:
                log.info("[reconciler] tick %s", stats)
        except Exception as e:  # noqa: BLE001 — a scheduler must survive a bad tick
            log.warning("[reconciler] tick failed: %s", e)
        time.sleep(RECONCILE_INTERVAL_SEC)


def start() -> None:
    """Spawn the reconcile loop after ``core.leader`` grants singleton
    leadership. Call via ``core.leader.run_singleton("runtime-reconciler",
    runtime_reconciler.start)`` from the asgi assembly layer — mirrors
    ``tee_sync_scheduler.start`` / ``dau_snapshot_scheduler.start``.

    NOTE on the leader API actually in this repo (``backend/core/leader.py``):
    it is NOT a context manager (``leader.try_leadership(...) as is_leader``,
    as an earlier draft of this module guessed) — it is
    ``run_singleton(name, start_fn)``: it elects exactly one worker via a
    held ``pg_try_advisory_lock`` session and then calls ``start_fn()`` once;
    ``start_fn`` is expected to spawn its own daemon thread and return
    immediately, same as every other singleton background loop in this repo.
    There is no ``stop_event``/graceful-shutdown parameter anywhere in that
    API or in any of its current callers — the daemon thread simply dies with
    the process, so this module follows the same shape rather than inventing
    a stop mechanism its siblings don't have.
    """
    threading.Thread(target=_loop, daemon=True, name="runtime-reconciler").start()
