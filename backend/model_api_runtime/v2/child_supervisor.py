"""``ChildSupervisor`` — 通用的「spawn 一个子进程 + 用 Pipe 侦测它是否还活着/还在动」
工具类（Hosted Runtime V2 PR D，D1 结构拆分，见
``docs/superpowers/plans/2026-07-13-hosted-runtime-v2-PR-D-pool-history-safety.md``
Task 2）。

**为什么是子进程而不是子协程**：拆分前，turn slot 是 ``serve_worker._serve`` 里
``asyncio.gather`` 的一个协程任务（``run_worker_loop``），跟 reaper/heartbeat/scheduler
共用同一个进程和事件循环——任何一个 slot 里卡死的同步调用（未经 ``to_thread`` 桥出去的
阻塞点、死锁、失控的 provider SDK）都会连带拖死整条事件循环，heartbeat/reaper 也跟着停,
而 heartbeat 一旦停摆，jobs_store 反而看不出这个 worker 已经失能（心跳行还在，只是
`capacity` 数字不会再更新）。把 turn slots 挪进一个独立进程后，父进程只要还能拿到
SIGKILL 权限，就能在子进程卡死时无视它内部状态直接强杀重启——这是这个类存在的唯一
理由，其余全是达成这个目的所需的管子。

**本文件刻意保持零 backend 依赖**（不 import ``db``/``worker``/``hosted``/
``agent_runtime`` 任何东西）：它只认识"一个可 pickle 的模块级 callable + 一根
``multiprocessing.Pipe``"，对目标进程具体在干什么一无所知。`tests/test_v2_dependency_
direction.py` 把这当作 v2/ 下的一个 core 模块检查（未在 EXEMPT 名单里），零依赖本来就是
这条防线要求的；但更根本的原因是可测试性——单测要在完全不碰 DB/enclave 的情况下验证
kill/respawn 语义（见 `tests/test_v2_child_supervisor.py`），子进程目标必须能替换成一个
纯 Python 的 fake。真正的生产目标（``turn_child.main``）由调用方（``serve_worker._serve``）
显式传入，本类不内置默认值、不 import ``turn_child``。

**progress pipe 协议**：子进程侧持有 Pipe 的写端（``start()`` 里作为 spawn target 的第
一个位置参数传入），每隔一小段时间发送 ``("progress", slot_id, monotonic_ts,
turn_start)``——``slot_id`` 是真实的 per-slot 下标（``worker._slot_loop`` 的
``slot_id``，见 ``turn_child._make_progress_cb``）。父进程侧的后台线程把收到的每条消息
落成两样东西：(1) 一个单调时钟时间戳 `_last_progress_at`——用本地 `time.monotonic()`
（不是子进程发来的那个数值）记录"收到时刻"，避免依赖跨进程单调时钟是否严格同源这种
细节；`poll_liveness()` 只是拿它跟当下的 `time.monotonic()` 作差，得到粗粒度的
`last_progress_age_sec`（整个子进程"最新一次有任何 slot 活动"是多久之前）。(2) 一个
per-slot 的 `_turn_starts` 字典——``turn_start is None``（slot 空闲）时清掉该 slot 的
条目，否则记下子进程发来的那个 `turn_start` 原样（这个数值必须跟子进程自己后续用
`time.monotonic()` 算出来的挂钟差保持同源，所以这里不能替换成父进程收到时刻——参见
`poll_liveness()` 对它的用法）。

**四只独立时钟**：`last_progress_age_sec`/`event_loop_heartbeat_age_sec` 只看 child event
loop 是否还能调度；`last_slot_progress_age_sec` 看有没有 slot 穿过 claim/idle/turn 边界；
active slot 另外有 `current_turn_stall_age_sec`（距本 turn 最近真实 provider/tool/Capture
边界）和 `current_turn_age_sec`（距 turn 开始）。watchdog 用 stall age 抓永久卡住的 await，
用更大的 absolute age 防止一个不断制造进度却永不终止的回合。这样合法的多轮/600s 历史
catch-up 不会再因为绝对年龄超过旧 180s 就被误杀，而 event-loop heartbeat 也不会替某个
卡住的 turn 刷新 stall clock。

用法（`serve_worker._serve`）::

    supervisor = ChildSupervisor(turn_child.main, liveness_timeout_sec=45.0,
                                  spawn_args=(worker_id, poll_interval))
    supervisor.start()
    ...
    supervisor.stop()  # 优雅：SIGTERM + join，超时才 SIGKILL

D2（Task 3）的 watchdog 循环会另外调 `poll_liveness()` + `kill_and_respawn()`——本任务
（D1/Task 2）只负责把子进程立起来、能被父进程干净地 stop，不做自动踢杀。
"""
from __future__ import annotations

import logging
import math
import multiprocessing as mp
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from model_api_runtime.v2 import enclave_broker, slot_protocol

log = logging.getLogger("feedling.runtime_v2.child_supervisor")

# `multiprocessing.connection.Connection.poll()` 的轮询粒度：够短以便 stop()/reader 线程
# 能及时响应，又不至于空转烧 CPU。
_POLL_TIMEOUT_SEC = 0.5


@dataclass(frozen=True)
class KillOutcome:
    """Result of a physical kill attempt used before DB claim recovery."""

    active_job: slot_protocol.ActiveJobIdentity | None
    terminated: bool


class ChildSupervisor:
    """spawn 一个子进程跑 `spawn_target`，用 progress pipe 侦测它是否卡死。

    `spawn_target` 必须是一个模块级（可 pickle）的 callable——`multiprocessing.
    get_context("spawn")` 用 pickle 把目标传给子解释器，bound method / 闭包 / lambda
    都不行。签名约定为 `spawn_target(conn_write_end, *spawn_args)`。
    """

    def __init__(
        self,
        spawn_target: Callable[..., None],
        *,
        liveness_timeout_sec: float,
        spawn_args: tuple[Any, ...] = (),
        broker: enclave_broker.EnclaveBroker | None = None,
        pool: str = "",
        slot_id: str = "",
    ) -> None:
        self._spawn_target = spawn_target
        self.liveness_timeout_sec = float(liveness_timeout_sec)
        self._spawn_args = tuple(spawn_args)
        self._ctx = mp.get_context("spawn")
        self._broker = broker
        self._pool = str(pool)
        self._slot_id = str(slot_id)

        # Serialize the whole per-slot process lifecycle.  `_lock` protects
        # snapshots and pipe state for short reads/writes; it cannot protect a
        # compare -> kill -> cleanup -> start transaction because reader
        # shutdown may itself need `_lock`.  A separate re-entrant lock keeps
        # cancellation, reconciliation, watchdog recovery, and shutdown from
        # spawning overlapping generations.
        self._lifecycle_lock = threading.RLock()
        self._lock = threading.Lock()
        self._proc: mp.process.BaseProcess | None = None
        self._read_conn = None
        # Process/event-loop liveness.  Kept under the historical attribute
        # name because a few narrow tests inspect the startup seed directly.
        self._last_progress_at: float | None = None
        # Real slot activity (claim/idle/turn boundaries), deliberately not
        # refreshed by the free-running event-loop heartbeat.
        self._last_slot_progress_at: float | None = None
        # Absolute-budget clock: per-slot turn_start, keyed by slot_id. Populated/cleared
        # by _handle_message on every ("progress", slot_id, ts, turn_start) message;
        # read by poll_liveness() to compute current_turn_age_sec. See module
        # docstring's "progress pipe 协议" / split-budget sections.
        self._slot_generation = ""
        # Generations invalidated while their OS child could not yet be
        # confirmed dead.  Keep their broker permits reserved until a later
        # kill/join proves the tracked process is gone.
        self._retired_generations: set[str] = set()
        self._snapshot: slot_protocol.SlotProgress | None = None
        self._turn_progress_at: float | None = None

        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """spawn 一个新子进程 + 起后台 reader 线程drain progress pipe。

        在上一代已 kill/stop 后可重复调用，每次都是全新的 Pipe + Process。若上一代仍
        存活则幂等返回，防止任何误用覆盖 `_proc` 并制造无法追踪的孤儿子进程。
        """
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """Start one generation while `_lifecycle_lock` is held."""
        with self._lock:
            existing = self._proc
        if existing is not None and existing.is_alive():
            log.warning(
                "[v2.child_supervisor] ignored duplicate start for live child pid=%s",
                existing.pid,
            )
            return
        if existing is not None:
            # Reap a process that exited between liveness polling and start;
            # do not overwrite its pipe/reader handles.
            self._kill_locked(join_timeout=0.0)

        read_conn, write_conn = mp.Pipe(duplex=True)
        slot_generation = uuid.uuid4().hex
        proc = self._ctx.Process(
            target=self._spawn_target,
            args=(write_conn, *self._spawn_args, slot_generation),
            daemon=True,
        )
        proc.start()
        # 父进程自己这份写端句柄没有用——留着不关的话，子进程异常退出时 OS 管道的写端
        # 不会真正关闭（父进程手里还攥着一份），reader 线程的 `conn.poll()`/`recv()`
        # 就侦测不到 EOF，只会一直阻塞在等消息上。关掉它，让"子进程活着"与"写端是否
        # 关闭"精确对应。
        write_conn.close()

        with self._lock:
            self._proc = proc
            self._read_conn = read_conn
            # 立即打一个时间戳，而不是等第一条 progress 消息——否则 spawn 刚完成、子
            # 进程还没来得及发第一条心跳的这一小段窗口里，poll_liveness() 会读到一个
            # `last_progress_at is None` → age=inf 的假阳性"卡死"读数。
            self._last_progress_at = time.monotonic()
            self._last_slot_progress_at = self._last_progress_at
            # Fresh generation of child == fresh turn-tracking state. A prior
            # generation's slot_ids/turn_starts must never leak into this one
            # (kill_and_respawn() calls start() again after killing the old proc).
            self._slot_generation = slot_generation
            self._snapshot = None
            self._turn_progress_at = None

        self._reader_stop = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._drain_loop, name="v2-child-supervisor-reader", daemon=True)
        self._reader_thread.start()
        log.info("[v2.child_supervisor] spawned child pid=%s target=%s",
                 proc.pid, getattr(self._spawn_target, "__qualname__", self._spawn_target))

    def poll_liveness(self) -> dict:
        """Return process, slot, per-turn stall, and absolute-age liveness clocks.

        `alive` 只反映"OS 进程还在不在"（`proc.is_alive()`），不代表它没卡死——一个
        在死锁里转不动的进程同样 `is_alive() == True`。`last_progress_age_sec`（兼容名）/
        `event_loop_heartbeat_age_sec` 是 event-loop 调度心跳年龄；
        `last_slot_progress_age_sec` 才是任意 slot 最近真实活动的年龄。从未 start 时均为
        `math.inf`，让 watchdog 的阈值比较自然成立。

        `current_turn_age_sec`：当下挂钟时间减去 `_turn_starts` 里最老的那个 turn_start——也就是
        "跑得最久、仍然没完成的那个 turn 已经跑了多久"。没有任何 slot 当前在跑 turn（
        `_turn_starts` 为空）时是 `0.0`，不是 `inf`——一个空闲子进程不该被 clause (c)
        误判为"有个 turn 卡了 inf 秒"。这个字段跟 `last_progress_age_sec` 互补：一个
        slot 卡死在 `_run_turn` 内部时，它最后一次发来的 turn_start 就定死在
        `_turn_starts` 里不再更新，但这里每次都用**当前**挂钟时间去减它，所以即使这个
        slot 从此不再发送任何消息，`current_turn_age_sec` 依然会随真实流逝的时间持续
        增长——不像 `last_progress_age_sec`，它不需要新消息到达就能反映"这个卡住的 turn
        又多卡了几秒"。其它 slot 仍在正常工作时 `last_progress_age_sec` 整体依然新鲜，
        只有这个字段能把这一个卡住的 turn 单独抓出来（见 clause (b) 的
        `jobs_claimable` 闸门为什么抓不住这个场景：其它 slot 仍在正常抢/跑活）。
        """
        with self._lock:
            proc = self._proc
            last = self._last_progress_at
            last_slot = self._last_slot_progress_at
            snapshot = self._snapshot
            turn_progress_at = self._turn_progress_at
        alive = bool(proc is not None and proc.is_alive())
        age = math.inf if last is None else max(0.0, time.monotonic() - last)
        slot_age = (
            math.inf if last_slot is None
            else max(0.0, time.monotonic() - last_slot)
        )
        now = time.monotonic()
        current_turn_age_sec = (
            0.0
            if snapshot is None or snapshot.turn_start is None
            else now - snapshot.turn_start
        )
        current_turn_age_sec = max(0.0, current_turn_age_sec)
        current_turn_stall_age_sec = (
            0.0 if turn_progress_at is None else now - turn_progress_at
        )
        current_turn_stall_age_sec = max(0.0, current_turn_stall_age_sec)
        return {
            "alive": alive,
            # Historical name now means event-loop/process progress; explicit
            # alias makes the split unambiguous for the watchdog.
            "last_progress_age_sec": age,
            "event_loop_heartbeat_age_sec": age,
            "last_slot_progress_age_sec": slot_age,
            "active_turn_count": int(
                snapshot is not None and snapshot.active_job is not None
            ),
            "current_turn_age_sec": current_turn_age_sec,
            "current_turn_stall_age_sec": current_turn_stall_age_sec,
        }

    def snapshot(self) -> slot_protocol.SlotProgress | None:
        """Return the immutable latest snapshot for this one-slot child."""
        with self._lock:
            return self._snapshot

    def kill(
        self, *, join_timeout: float = 5.0
    ) -> slot_protocol.ActiveJobIdentity | None:
        """Snapshot the exact owner, kill this slot generation, and stop."""
        with self._lifecycle_lock:
            outcome = self._kill_locked(join_timeout=join_timeout)
            return outcome.active_job if outcome.terminated else None

    def kill_for_recovery(self, *, join_timeout: float = 5.0) -> KillOutcome:
        """Kill and explicitly report whether DB claim recovery is now safe."""
        with self._lifecycle_lock:
            return self._kill_locked(join_timeout=join_timeout)

    def _kill_locked(
        self, *, join_timeout: float = 5.0
    ) -> KillOutcome:
        """Kill the current generation while `_lifecycle_lock` is held."""
        with self._lock:
            snapshot = self._snapshot
            active_job = None if snapshot is None else snapshot.active_job
            dead_generation = self._slot_generation
        self._stop_reader()
        proc = self._proc
        terminated = proc is None
        if proc is not None:
            try:
                if proc.is_alive():
                    log.warning("[v2.child_supervisor] SIGKILL wedged child pid=%s", proc.pid)
                    proc.kill()
                proc.join(join_timeout)
                terminated = not proc.is_alive()
                if not terminated:
                    log.error(
                        "[v2.child_supervisor] child pid=%s still alive %.1fs after "
                        "SIGKILL+join — keeping it tracked and refusing respawn",
                        proc.pid, join_timeout)
            except Exception as e:  # noqa: BLE001 — 杀不掉也不能拖垮父进程的 watchdog 循环
                log.warning("[v2.child_supervisor] kill cleanup failed: %s", e)
                terminated = False
        self._close_read_conn()
        if terminated:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                generations_to_drop = set(self._retired_generations)
                self._retired_generations.clear()
                if dead_generation:
                    generations_to_drop.add(dead_generation)
                self._slot_generation = uuid.uuid4().hex
                self._snapshot = None
                self._turn_progress_at = None
            self._drop_broker_generations(generations_to_drop)
        return KillOutcome(active_job=active_job, terminated=terminated)

    def kill_and_respawn(self, *, join_timeout: float = 5.0) -> None:
        """Compatibility wrapper; new recovery paths call kill/recover/start."""
        with self._lifecycle_lock:
            outcome = self._kill_locked(join_timeout=join_timeout)
            if outcome.terminated:
                self._start_locked()

    def kill_if_snapshot(
        self,
        expected: slot_protocol.SlotProgress,
        *,
        join_timeout: float = 5.0,
    ) -> bool:
        """SIGKILL only while the exact generation/job snapshot still matches.

        This is the parent-side compare-and-kill fence used by periodic claim
        reconciliation.  Invalidate the generation while holding the snapshot
        lock, so a late pipe message from the killed process cannot repopulate
        state after the decision.
        """
        with self._lifecycle_lock:
            return self._kill_if_snapshot_locked(
                expected, join_timeout=join_timeout
            )

    def _kill_if_snapshot_locked(
        self,
        expected: slot_protocol.SlotProgress,
        *,
        join_timeout: float = 5.0,
    ) -> bool:
        """Fenced kill while `_lifecycle_lock` is held."""
        with self._lock:
            if self._snapshot != expected:
                return False
            proc = self._proc
            dead_generation = self._slot_generation
            try:
                if proc is not None and proc.is_alive():
                    log.warning(
                        "[v2.child_supervisor] SIGKILL invalid exact claim pid=%s",
                        proc.pid,
                    )
                    proc.kill()
            except Exception as exc:  # noqa: BLE001
                log.warning("[v2.child_supervisor] fenced kill failed: %s", exc)
                return False
            self._slot_generation = uuid.uuid4().hex
            self._snapshot = None
            self._turn_progress_at = None
        self._stop_reader()
        terminated = proc is None
        if proc is not None:
            try:
                proc.join(join_timeout)
                terminated = not proc.is_alive()
            except Exception as exc:  # noqa: BLE001
                log.warning("[v2.child_supervisor] fenced join failed: %s", exc)
                terminated = False
        self._close_read_conn()
        if not terminated:
            with self._lock:
                if dead_generation:
                    self._retired_generations.add(dead_generation)
            log.error(
                "[v2.child_supervisor] fenced child pid=%s still alive after "
                "SIGKILL+join — keeping it tracked and refusing replacement",
                None if proc is None else proc.pid,
            )
            return False
        with self._lock:
            if self._proc is proc:
                self._proc = None
            generations_to_drop = set(self._retired_generations)
            self._retired_generations.clear()
            if dead_generation:
                generations_to_drop.add(dead_generation)
        self._drop_broker_generations(generations_to_drop)
        return True

    def restart_if_snapshot(
        self,
        expected: slot_protocol.SlotProgress,
        *,
        join_timeout: float = 5.0,
    ) -> bool:
        """Atomically replace only the exact slot generation in `expected`.

        Snapshot comparison, SIGKILL, pipe cleanup, and replacement spawn are
        one lifecycle transaction.  Concurrent owners of the same stale
        snapshot therefore cannot each create a replacement child.
        """
        with self._lifecycle_lock:
            if not self._kill_if_snapshot_locked(
                expected, join_timeout=join_timeout
            ):
                return False
            self._start_locked()
            return True

    def stop(self, *, drain_timeout: float = 10.0, kill_timeout: float = 2.0) -> None:
        """优雅停止：SIGTERM（子进程的信号处理器据此 drain 手上的回合再退出）+ 限时
        join；超时仍活着才升级成 SIGKILL。父进程 `_serve` 的 finally/stop_event 路径调
        这个，不是 `kill_and_respawn`——那个是 watchdog 专用的"不管三七二十一先杀死"。
        """
        with self._lifecycle_lock:
            self._stop_locked(
                drain_timeout=drain_timeout, kill_timeout=kill_timeout
            )

    def _stop_locked(
        self, *, drain_timeout: float = 10.0, kill_timeout: float = 2.0
    ) -> None:
        """Gracefully stop the current generation with lifecycle ownership."""
        with self._lock:
            dead_generation = self._slot_generation
        self._stop_reader()
        proc = self._proc
        terminated = proc is None
        if proc is not None:
            try:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(drain_timeout)
                if proc.is_alive():
                    log.warning(
                        "[v2.child_supervisor] child pid=%s did not drain within %.1fs "
                        "after SIGTERM — SIGKILL", proc.pid, drain_timeout)
                    proc.kill()
                    proc.join(kill_timeout)
                terminated = not proc.is_alive()
            except Exception as e:  # noqa: BLE001 — shutdown 必须有界，不能被这里的异常卡住
                log.warning("[v2.child_supervisor] stop() cleanup failed: %s", e)
                terminated = False
        self._close_read_conn()
        with self._lock:
            if terminated and self._proc is proc:
                self._proc = None
            if terminated:
                generations_to_drop = set(self._retired_generations)
                self._retired_generations.clear()
                if dead_generation:
                    generations_to_drop.add(dead_generation)
            else:
                generations_to_drop = set()
                if dead_generation:
                    self._retired_generations.add(dead_generation)
            # Shutdown is a lifecycle fence even when OS termination fails.
            # Invalidate the old snapshot/generation so a cancellation queued
            # before unwatch cannot resurrect this slot after stop returns.
            self._slot_generation = uuid.uuid4().hex
            self._snapshot = None
            self._turn_progress_at = None
        self._drop_broker_generations(generations_to_drop)
        log.info("[v2.child_supervisor] stopped")

    def _drop_broker_generations(self, generations: set[str]) -> None:
        if self._broker is None:
            return
        for generation in generations:
            if generation:
                self._broker.drop_generation(generation)

    def grant_enclave(self, request: enclave_broker.EnclaveRequest) -> None:
        """Deliver a broker grant only to the matching live slot generation."""
        with self._lock:
            if (
                request.slot_generation != self._slot_generation
                or request.pool != self._pool
                or request.slot_id != self._slot_id
                or self._read_conn is None
            ):
                return
            try:
                self._read_conn.send(
                    enclave_broker.grant_message(
                        request.request_id, request.slot_generation
                    )
                )
            except (BrokenPipeError, EOFError, OSError, ValueError):
                return

    # -- progress pipe reader -------------------------------------------------

    def _drain_loop(self) -> None:
        conn = self._read_conn
        try:
            while not self._reader_stop.is_set():
                try:
                    ready = conn.poll(_POLL_TIMEOUT_SEC)
                except (OSError, EOFError, ValueError, BrokenPipeError):
                    return
                if not ready:
                    continue
                try:
                    msg = conn.recv()
                except (EOFError, OSError, BrokenPipeError):
                    # 子进程退出/写端关闭——没有更多消息可读了，安静退出这个线程；
                    # poll_liveness() 的 alive 字段会随 proc.is_alive() 自然翻false，
                    # age 字段则从这一刻起只会单调增长，两者共同反映出"子进程没了"。
                    return
                except Exception as e:  # noqa: BLE001 — 单条畸形消息不能打死整个 reader 线程
                    log.warning("[v2.child_supervisor] malformed progress message: %s", e)
                    continue
                self._handle_message(msg)
        finally:
            with self._lock:
                proc = self._proc
                generation = self._slot_generation
            if (
                self._broker is not None
                and generation
                and proc is not None
                and not proc.is_alive()
            ):
                self._broker.drop_generation(generation)

    def _handle_message(self, msg: Any) -> None:
        broker_message = enclave_broker.decode_child_message(msg)
        if broker_message is not None:
            if self._broker is None:
                return
            action, payload = broker_message
            if action == "acquire":
                request = payload
                with self._lock:
                    valid = (
                        request.slot_generation == self._slot_generation
                        and request.pool == self._pool
                        and request.slot_id == self._slot_id
                    )
                if valid:
                    self._broker.request(request)
                return
            request_id, generation = payload
            with self._lock:
                valid = generation == self._slot_generation
            if not valid:
                return
            if action == "release":
                self._broker.release(request_id, generation)
            else:
                self._broker.cancel(request_id, generation)
            return
        try:
            decoded = slot_protocol.decode_message(msg)
        except (TypeError, ValueError):
            return
        with self._lock:
            if decoded.slot_generation != self._slot_generation:
                return
            if isinstance(decoded, slot_protocol.LoopHeartbeat):
                # Event-loop heartbeat is deliberately process-level only.  It
                # proves the child's loop can still schedule work, but must never
                # refresh a particular turn's stall clock: an await that never
                # returns leaves that slot's ``_turn_progress_at`` untouched and
                # is still killed by the per-turn stall watchdog.
                self._last_progress_at = time.monotonic()
                return
            received_at = time.monotonic()
            self._last_progress_at = received_at
            self._last_slot_progress_at = received_at
            self._snapshot = decoded
            self._turn_progress_at = (
                None if decoded.active_job is None else received_at
            )

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        thread = self._reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_POLL_TIMEOUT_SEC * 3)
        self._reader_thread = None

    def _close_read_conn(self) -> None:
        conn = self._read_conn
        self._read_conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — best-effort fd cleanup
                pass
