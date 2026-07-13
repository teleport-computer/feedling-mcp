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

**`jobs_claimable` 这道闸存在的理由**：一个健康、当前没活干的子进程同样会让
`last_progress_age_sec` 慢慢变老（D1 的 progress 是 slot-driven 的：claim/回合完成/
idle-poll 唤醒时才发一条，参见 `child_supervisor.py` 的 progress pipe 协议注释）。没有
这道闸，watchdog 会在每一个空闲期后误杀一个好端端没事干的 worker。只有"过旧 **且**
有活等着被抢"才是"整个池子都卡死了"的信号——任何一个还活着/空闲的 slot 都会通过 claim/
idle-poll 刷新 `last_progress_age_sec`，所以"最新一次进度"仍然停留在阈值以上，恰好就是
"全员卡死"这件事本身的证据，不需要逐 slot 粒度。

**capacity=0 必须先于 kill_and_respawn 落库**：admission（`chat_send_core` 的
`workers_alive`/`live_worker_capacity` 闸）只看 heartbeat 行，不知道子进程正在被
SIGKILL——如果先杀后写，杀到重新 `start()` 之间的窗口里，heartbeat 行还顶着卡死前的旧
capacity，新请求可能被放行到一个即将消失的子进程上排队等死。先写 0 再杀，这个窗口就是
安全的：capacity 从写入那一刻起就诚实地反映"没有可用 slot"，直到新子进程立起来、下一次
`_heartbeat_loop`/watchdog 观测到它健康为止。
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Callable, Protocol

from model_api_runtime.v2 import jobs_store

log = logging.getLogger("feedling.runtime_v2.watchdog")


class _SupervisorLike(Protocol):
    """Structural — the real `child_supervisor.ChildSupervisor` and any test fake
    both satisfy this without a shared base class."""

    def poll_liveness(self) -> dict: ...

    def kill_and_respawn(self) -> None: ...


def should_kill(
    liveness: dict,
    *,
    turn_hard_timeout_sec: float,
    child_liveness_timeout_sec: float,
    jobs_claimable: bool,
) -> bool:
    """PURE — no I/O, no clock reads (the caller already resolved `liveness` and
    `jobs_claimable` before calling in). Returns True iff any of:

    (a) the child process itself is dead (`liveness["alive"]` is False) — a dead
        process obviously needs a respawn regardless of any queue state;
    (b) `liveness["last_progress_age_sec"] > child_liveness_timeout_sec` AND
        `jobs_claimable` — all slots stuck while work waits. The `jobs_claimable`
        guard is what stops this from firing on a healthy-but-idle child (see
        module docstring);
    (c) `liveness.get("current_turn_age_sec")` is present and exceeds
        `turn_hard_timeout_sec` — a finer-grained per-turn signal. The real
        `ChildSupervisor.poll_liveness()` always reports this field now (hard-timeout
        fix: `worker._slot_loop` threads a per-slot `turn_start` through
        `progress_cb`, and `ChildSupervisor` tracks the oldest still-active one — see
        `child_supervisor.py`'s module docstring), so this clause is live in
        production: a SINGLE slot wedged inside `_run_turn` while every other slot
        keeps working never makes `last_progress_age_sec` go stale (clause (b) can't
        see it — the other slots' claim/completion/idle-poll activity keeps the
        coarse clock fresh), so clause (c) is the only thing that catches it.
        `.get(...)` (not `[...]`) is kept for a supervisor implementation/test double
        that genuinely omits the key — absence still means "skip this clause", not
        "treat as 0 > timeout" (which would trivially always be False anyway).

    `last_progress_age_sec` may be `math.inf` (never received a progress message
    yet, or the child is confirmed dead) — the `>` comparison handles that with no
    special-casing.
    """
    if not liveness.get("alive", False):
        return True
    age = liveness.get("last_progress_age_sec", math.inf)
    if age > child_liveness_timeout_sec and jobs_claimable:
        return True
    turn_age = liveness.get("current_turn_age_sec")
    if turn_age is not None and turn_age > turn_hard_timeout_sec:
        return True
    return False


async def _watchdog_loop(
    supervisor: _SupervisorLike,
    worker_id: str,
    stop_event: asyncio.Event,
    *,
    jobs_claimable_fn: Callable[[], bool],
    interval: float,
    turn_hard_timeout_sec: float,
    child_liveness_timeout_sec: float,
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
    while not stop_event.is_set():
        try:
            liveness = supervisor.poll_liveness()
            claimable = await asyncio.to_thread(jobs_claimable_fn)
            if should_kill(
                liveness,
                turn_hard_timeout_sec=turn_hard_timeout_sec,
                child_liveness_timeout_sec=child_liveness_timeout_sec,
                jobs_claimable=claimable,
            ):
                log.warning(
                    "[v2.watchdog] kill decision worker=%s liveness=%s claimable=%s",
                    worker_id, liveness, claimable)
                try:
                    await asyncio.to_thread(
                        jobs_store.record_worker_heartbeat,
                        worker_id, capacity=0, kind="turn")
                except Exception:  # noqa: BLE001 — best-effort; the kill must proceed regardless
                    log.exception(
                        "[v2.watchdog] capacity=0 heartbeat write failed worker=%s "
                        "(proceeding to kill_and_respawn anyway)", worker_id)
                await asyncio.to_thread(supervisor.kill_and_respawn)
        except Exception:  # noqa: BLE001 — a watchdog tick must survive transient failures
            log.exception("[v2.watchdog] tick failed worker=%s", worker_id)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
