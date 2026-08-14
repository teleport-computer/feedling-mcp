"""D2 watchdog + hard-timeout（Hosted Runtime V2 PR D，见
``docs/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety.md``
Task 3）。

**为什么要拆成 pure decision + 一个薄 parent loop**：D1（Task 2, ``child_supervisor.py``）
把 turn slots 挪进一个独立可 SIGKILL 的子进程，`ChildSupervisor.poll_liveness()` 能看出
它是否卡死（`last_progress_age_sec` 过旧），但**谁来看、看到了就杀**是另一件事——那件事
需要跑在父进程的事件循环里（有 asyncio.gather 的那个），且必须能被单测在完全不碰真实
子进程/DB 的情况下驱动。所以 `should_kill` 保持纯函数（无 I/O，输入即输出，穷举分支
好测），`_watchdog_loop` 只是拿真实（或测试注入的假）`ChildSupervisor` + 一个
jobs-claimable 判定去喂它，按决策去写 heartbeat / 踢杀——两者的组合方式本身不含任何
需要 mock 的分支逻辑。

**两层 child liveness**：自由跑的 event-loop heartbeat 变旧意味着 loop 本身不能调度，
无需查队列即可强杀；slot progress 只在 claim/idle/turn 工作边界更新。slot 变旧但 loop
仍活、且当前没有 active turn 时，才用 `jobs_claimable` 判断是不是所有 slot 卡在 claim 前。
这个 DB 判定只在它能改变决策时发起、带超时，并且同一时刻最多保留一个 in-flight 查询，
避免 DB 故障反过来耗尽 watchdog 用于 SIGKILL 的线程池。

**capacity=0 尽力先于 kill_and_respawn 落库**：admission（`chat_send_core` 的
`workers_alive`/`live_worker_capacity` 闸）只看 heartbeat 行，不知道子进程正在被
SIGKILL——如果先杀后写，杀到重新 `start()` 之间的窗口里，heartbeat 行还顶着卡死前的旧
capacity，新请求可能被放行到一个即将消失的子进程上排队等死。先写 0 再杀，这个窗口就是
安全的：capacity 从写入那一刻起就诚实地反映"没有可用 slot"，直到新子进程立起来、下一次
`_heartbeat_loop`/watchdog 观测到它健康为止。但该 DB 写有严格超时；数据库挂死时绝不能
挡住物理 SIGKILL，超时后会记录错误并继续恢复 child。
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

    def kill(self) -> slot_protocol.ActiveJobIdentity | None: ...

    def kill_for_recovery(self): ...

    def start(self) -> None: ...


def should_kill(
    liveness: dict,
    *,
    child_liveness_timeout_sec: float,
    jobs_claimable: bool,
    turn_stall_timeout_sec: float | None = None,
    turn_absolute_timeout_sec: float | None = None,
    turn_hard_timeout_sec: float | None = None,
) -> bool:
    """PURE — no I/O, no clock reads (the caller already resolved `liveness` and
    `jobs_claimable` before calling in). Returns True iff any of:

    (a) the child process itself is dead (`liveness["alive"]` is False) — a dead
        process obviously needs a respawn regardless of any queue state;
    (b) the explicit event-loop heartbeat is stale — the child loop itself can
        no longer schedule callbacks, so recovery is unconditional and does not
        wait for queue state;
    (c) real slot progress is stale, no turn has reached its start boundary,
        AND `jobs_claimable` — every slot is wedged in pre-turn/claim work. The
        queue guard stops an idle pool from being killed;
    (d) `current_turn_stall_age_sec` exceeds `turn_stall_timeout_sec`.  The
        child refreshes this clock at real in-turn boundaries (provider round,
        tool batch, Capture batch), so this catches one permanently
        wedged slot even while sibling slots and the child event loop remain
        healthy.  Critically, it does *not* kill a long turn merely because its
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
    # Compatibility for the pre-split pure-function/test surface.  Production
    # always supplies both new budgets; an older caller's one hard timeout keeps
    # its historical absolute-age semantics rather than failing at call time.
    if turn_stall_timeout_sec is None:
        turn_stall_timeout_sec = turn_hard_timeout_sec
    if turn_absolute_timeout_sec is None:
        turn_absolute_timeout_sec = turn_hard_timeout_sec
    if turn_stall_timeout_sec is None or turn_absolute_timeout_sec is None:
        raise TypeError("turn stall and absolute timeout budgets are required")

    if not liveness.get("alive", False):
        return True

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
    turn_stall_timeout_sec: float | None = None,
    turn_absolute_timeout_sec: float | None = None,
    turn_hard_timeout_sec: float | None = None,
    jobs_claimable_timeout_sec: float = 5.0,
    capacity_write_timeout_sec: float = 5.0,
    recovery_timeout_sec: float = 5.0,
    recovery_queue: claim_recovery.ClaimRecoveryQueue | None = None,
    pool: str = "foreground",
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

    On a kill decision: `jobs_store.record_worker_heartbeat(worker_id, capacity=0,
    kind='turn')` is awaited FIRST (best-effort — a failed write is logged and does
    NOT block the kill; the child is wedged regardless of whether the heartbeat row
    could be updated), THEN `supervisor.kill_and_respawn()`. Every per-iteration
    exception (poll_liveness/jobs_claimable_fn/the writes/the kill itself) is caught,
    logged, and the loop continues — the watchdog must never crash the parent
    process (crashing here would also silently stop the heartbeat/reaper/scheduler
    loops it shares an `asyncio.gather` with in `serve_worker._serve`).
    """
    if turn_stall_timeout_sec is None:
        turn_stall_timeout_sec = turn_hard_timeout_sec
    if turn_absolute_timeout_sec is None:
        turn_absolute_timeout_sec = turn_hard_timeout_sec
    if turn_stall_timeout_sec is None or turn_absolute_timeout_sec is None:
        raise TypeError("turn stall and absolute timeout budgets are required")

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
                "(proceeding to kill_and_respawn)", worker_id)
        except Exception:  # noqa: BLE001 — best-effort; the kill must proceed regardless
            log.exception(
                "[v2.watchdog] capacity=0 heartbeat write failed worker=%s "
                "(proceeding to kill_and_respawn anyway)", worker_id)
        if not all(hasattr(supervisor, name) for name in ("snapshot", "kill", "start")):
            # Test-double compatibility while production has fully moved to
            # the split lifecycle. No exact identity exists on this path.
            await asyncio.to_thread(supervisor.kill_and_respawn)
            return
        snapshot = supervisor.snapshot()
        kill_for_recovery = getattr(supervisor, "kill_for_recovery", None)
        if callable(kill_for_recovery):
            outcome = await asyncio.to_thread(kill_for_recovery)
            if not bool(outcome.terminated):
                log.error(
                    "[v2.watchdog] child termination unconfirmed worker=%s; "
                    "retaining DB claim and refusing replacement",
                    worker_id,
                )
                return
            killed_identity = outcome.active_job
        else:
            # Compatibility for narrow test doubles. Production supervisors
            # expose the explicit, confirmed kill outcome above.
            killed_identity = await asyncio.to_thread(supervisor.kill)
        active_job = killed_identity or (
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
                    jobs_claimable=claimable,
                ):
                    await _kill_child(liveness, claimable)
        except Exception:  # noqa: BLE001 — a watchdog tick must survive transient failures
            log.exception("[v2.watchdog] tick failed worker=%s", worker_id)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
