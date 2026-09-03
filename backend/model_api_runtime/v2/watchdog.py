"""Runtime V2 的 per-slot watchdog 与 hard-timeout。

保留的 progress/lease/write-fence/outbox 安全不变量见
``docs/superpowers/specs/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety-design.md``；
当前 one-process-per-slot 拓扑及其所有权见
``docs/superpowers/specs/2026-08-14-runtime-v2-three-pool-slot-isolation-design.md``。

**当前拓扑**：`pool_supervisor.SlotFleet` 为每个 ``SlotSpec`` 建立一个
`ChildSupervisor` 和一个 `turn_child` 进程。因此一个 child 只拥有一个 slot；不存在一个
child 内的多 slot，也不会把一个 slot 的进度拿来判定 sibling 是否健康。`serve_worker`
在 parent 中为 fleet 的每个 supervisor 运行本模块的 watchdog，并保留 fleet heartbeat、
reaper、scheduler 与 reconcile 的独立职责。

**为什么要拆成 pure decision + 一个薄 parent loop**：`ChildSupervisor.poll_liveness()`
报告单个 slot child 的时钟；谁来观察及执行恢复则属于 parent。`should_kill` 保持纯函数
（无 I/O，输入即输出，穷举分支可测），`_watchdog_loop` 只组合一个 supervisor 与
jobs-claimable 判定。真实 `SlotFleet` 路径的顺序是：先 snapshot 当前 slot，尽力写该
pool/slot 的 ``capacity=0``，确认 SIGKILL，按精确 ``job_id + claimed_by`` 恢复 claim，
最后启动该 slot replacement。DB 写有超时，不能阻挡物理 kill。

**两层 liveness**：event-loop heartbeat 变旧意味着该 slot child 已不能调度，因而无需查
队列即可恢复；slot progress 只在此 slot 的 claim/idle/turn 边界更新。progress 变旧、loop
仍活且没有 active turn 时，`jobs_claimable` 用于区分空闲与 claim 前卡死。该 DB 判定只在
它能改变决策时发起、带超时，并且同一时刻最多保留一个 in-flight 查询，避免 DB 故障反过来
耗尽 watchdog 用于 SIGKILL 的线程池。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Callable, Protocol

from model_api_runtime.v2 import claim_recovery, jobs_store, slot_protocol

log = logging.getLogger("feedling.runtime_v2.watchdog")


class _SupervisorLike(Protocol):
    """Structural — the real `child_supervisor.ChildSupervisor` and any test fake
    both satisfy this without a shared base class."""

    def poll_liveness(self) -> dict: ...

    def snapshot(self): ...

    def kill_for_recovery(self): ...

    def start(self) -> None: ...


def should_kill(
    liveness: dict,
    *,
    child_liveness_timeout_sec: float,
    jobs_claimable: bool,
    turn_stall_timeout_sec: float,
    turn_absolute_timeout_sec: float,
    child_startup_timeout_sec: float | None = None,
) -> bool:
    """PURE — no I/O, no clock reads (the caller already resolved `liveness` and
    `jobs_claimable` before calling in). Returns True iff any of:

    (a) the child process itself is dead (`liveness["alive"]` is False) — a dead
        process obviously needs a respawn regardless of any queue state;
    (b) the explicit event-loop heartbeat is stale — the child loop itself can
        no longer schedule callbacks, so recovery is unconditional and does not
        wait for queue state;
    (c) this slot's real progress is stale, no turn has reached its start
        boundary, AND `jobs_claimable` — this slot is wedged in pre-turn/claim
        work. The queue guard stops an idle slot from being killed;
    (d) `current_turn_stall_age_sec` exceeds `turn_stall_timeout_sec`.  The
        child refreshes this clock at real in-turn boundaries (provider round,
        tool batch, prompt-compaction batch), so this catches this slot's
        permanently wedged turn while the parent fleet and other slots remain
        healthy. Critically, it does *not* kill a long turn merely because its
        absolute age crossed the old 180-second ceiling.
    (e) `current_turn_age_sec` exceeds `turn_absolute_timeout_sec`.  This is a
        separate, deliberately much larger whole-turn budget.  It bounds a
        pathological turn that keeps manufacturing progress forever without
        ever terminating, while allowing the configured prompt catch-up and
        bounded provider rounds to finish normally.

    `last_progress_age_sec` may be `math.inf` (never received a progress message
    yet, or the child is confirmed dead) — the `>` comparison handles that with no
    special-casing.
    """
    if not liveness.get("alive", False):
        return True

    # A spawned interpreter performs synchronous schema/user/runtime bootstrap
    # before its asyncio heartbeat task exists. Production cold starts can take
    # longer than the steady-state heartbeat budget, especially when all pool
    # processes initialize concurrently. Give only generations that have never
    # emitted a valid runtime message a separate bounded startup budget. Once
    # the first heartbeat/progress arrives, all normal liveness rules apply.
    if liveness.get("startup_complete") is False:
        startup_timeout = (
            child_liveness_timeout_sec
            if child_startup_timeout_sec is None
            else float(child_startup_timeout_sec)
        )
        startup_age = float(liveness.get("startup_age_sec", math.inf))
        return startup_age > startup_timeout

    # New supervisors expose the event-loop clock explicitly.  Its heartbeat
    # is unconditional, including while idle, so staleness is a real loop wedge
    # and can short-circuit queue I/O.  Legacy dicts only have the old
    # slot-driven `last_progress_age_sec`; preserve their jobs-claimable guard.
    event_age = liveness.get("event_loop_heartbeat_age_sec")
    if event_age is not None and event_age > child_liveness_timeout_sec:
        return True
    slot_age = liveness.get(
        "last_slot_progress_age_sec",
        liveness.get("last_progress_age_sec", math.inf),
    )
    active_turn_count = int(liveness.get("active_turn_count") or 0)
    if (
        slot_age > child_liveness_timeout_sec
        and active_turn_count == 0
        and jobs_claimable
    ):
        return True
    stall_age = liveness.get("current_turn_stall_age_sec")
    if stall_age is not None and stall_age > turn_stall_timeout_sec:
        return True
    turn_age = liveness.get("current_turn_age_sec")
    if turn_age is not None and turn_age > turn_absolute_timeout_sec:
        return True
    return False


async def _watchdog_loop(
    supervisor: _SupervisorLike,
    worker_id: str,
    stop_event: asyncio.Event,
    *,
    jobs_claimable_fn: Callable[[], bool],
    interval: float,
    child_liveness_timeout_sec: float,
    turn_stall_timeout_sec: float,
    turn_absolute_timeout_sec: float,
    jobs_claimable_timeout_sec: float = 5.0,
    capacity_write_timeout_sec: float = 5.0,
    recovery_timeout_sec: float = 5.0,
    recovery_queue: claim_recovery.ClaimRecoveryQueue | None = None,
    pool: str = "foreground",
    child_startup_timeout_sec: float | None = None,
) -> None:
    """PARENT loop — mirrors `serve_worker._reaper_loop`/`_heartbeat_loop`'s
    interruptible `wait_for(stop_event.wait(), timeout=interval)` shape so
    `stop_event.set()` wakes it immediately instead of waiting out the rest of the
    interval.

    `supervisor.poll_liveness()` is called directly (not bridged through
    `asyncio.to_thread`) — it only touches an in-process lock + `Process.is_alive()`,
    no I/O. `jobs_claimable_fn` is bridged through `to_thread` because the real
    implementation (`jobs_store.pending_job_count() > 0`, wired in
    `serve_worker._serve`) is a blocking DB round trip.

    On a kill decision, the watchdog first snapshots the active identity, then
    advertises the affected pool/slot with `capacity=0` (best effort; DB trouble
    never blocks physical recovery), confirms the physical kill, recovers only
    that `job_id + claimed_by` claim, and finally starts the replacement slot.
    Every per-iteration exception is caught and logged so a bad slot cannot crash
    the parent fleet's heartbeat, reaper, scheduler, or other slot watchdogs.
    """
    claimable_task: asyncio.Task | None = None

    def _observe_claimable_task(task: asyncio.Task) -> None:
        # Retrieve background exceptions even if the watchdog is shutting down
        # before the retained to_thread call completes.
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _kill_child(liveness: dict, claimable: bool) -> None:
        log.warning(
            "[v2.watchdog] kill decision worker=%s liveness=%s claimable=%s",
            worker_id, liveness, claimable)
        snapshot = supervisor.snapshot()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    jobs_store.record_worker_heartbeat,
                    worker_id, capacity=0, kind="turn", pool=pool),
                timeout=capacity_write_timeout_sec,
            )
        except asyncio.TimeoutError:
            log.error(
                "[v2.watchdog] capacity=0 heartbeat write timed out worker=%s "
                "(proceeding to confirmed kill)", worker_id)
        except Exception:  # noqa: BLE001 — best-effort; the kill must proceed regardless
            log.exception(
                "[v2.watchdog] capacity=0 heartbeat write failed worker=%s "
                "(proceeding to confirmed kill anyway)", worker_id)
        outcome = await asyncio.to_thread(supervisor.kill_for_recovery)
        if not bool(outcome.terminated):
            log.error(
                "[v2.watchdog] child termination unconfirmed worker=%s; "
                "retaining DB claim and refusing replacement",
                worker_id,
            )
            return
        active_job = outcome.active_job or (
            None if snapshot is None else snapshot.active_job
        )
        try:
            if active_job is not None:
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            jobs_store.recover_killed_job,
                            job_id=active_job.job_id,
                            claimed_by=active_job.claimed_by,
                            reason="slot_watchdog_timeout",
                        ),
                        timeout=recovery_timeout_sec,
                    )
                    log.warning(
                        "[v2.watchdog] exact recovery pool=%s slot=%s job=%s "
                        "lane=%s owner=%s result=%s",
                        pool,
                        None if snapshot is None else snapshot.slot_id,
                        active_job.job_id,
                        active_job.lane,
                        active_job.claimed_by,
                        result,
                    )
                except Exception:
                    if recovery_queue is not None:
                        recovery_queue.enqueue(
                            job_id=active_job.job_id,
                            claimed_by=active_job.claimed_by,
                            reason="slot_watchdog_timeout",
                        )
                    log.exception(
                        "[v2.watchdog] immediate exact recovery failed job=%s owner=%s",
                        active_job.job_id,
                        active_job.claimed_by,
                    )
        finally:
            await asyncio.to_thread(supervisor.start)

    while not stop_event.is_set():
        try:
            liveness = supervisor.poll_liveness()
            # Dead/per-turn-stalled/absolute-overrun children do not need queue
            # state to justify recovery.  Short-circuit before touching the DB,
            # otherwise a hung pool can prevent the one component capable of
            # physically killing the child from ever reaching its kill path.
            immediate = should_kill(
                liveness,
                turn_stall_timeout_sec=turn_stall_timeout_sec,
                turn_absolute_timeout_sec=turn_absolute_timeout_sec,
                child_liveness_timeout_sec=child_liveness_timeout_sec,
                child_startup_timeout_sec=child_startup_timeout_sec,
                jobs_claimable=False,
            )
            if immediate:
                await _kill_child(liveness, False)
            else:
                slot_age = liveness.get(
                    "last_slot_progress_age_sec",
                    liveness.get("last_progress_age_sec", math.inf),
                )
                active_turn_count = int(liveness.get("active_turn_count") or 0)
                should_check_queue = (
                    slot_age > child_liveness_timeout_sec
                    and active_turn_count == 0
                )
                claimable = False
                try:
                    if should_check_queue:
                        if claimable_task is None:
                            claimable_task = asyncio.create_task(
                                asyncio.to_thread(jobs_claimable_fn))
                            claimable_task.add_done_callback(_observe_claimable_task)
                        done, _pending = await asyncio.wait(
                            {claimable_task}, timeout=jobs_claimable_timeout_sec)
                        if done:
                            completed = claimable_task
                            claimable_task = None
                            claimable = bool(completed.result())
                        else:
                            log.error(
                                "[v2.watchdog] jobs-claimable check timed out worker=%s",
                                worker_id)
                    elif claimable_task is not None and claimable_task.done():
                        # The liveness signal recovered while a prior query was
                        # in flight. Discard its now-stale queue snapshot.
                        claimable_task = None
                except Exception:
                    claimable_task = None
                    raise
                if should_kill(
                    liveness,
                    turn_stall_timeout_sec=turn_stall_timeout_sec,
                    turn_absolute_timeout_sec=turn_absolute_timeout_sec,
                    child_liveness_timeout_sec=child_liveness_timeout_sec,
                    child_startup_timeout_sec=child_startup_timeout_sec,
                    jobs_claimable=claimable,
                ):
                    await _kill_child(liveness, claimable)
        except Exception:  # noqa: BLE001 — a watchdog tick must survive transient failures
            log.exception("[v2.watchdog] tick failed worker=%s", worker_id)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
