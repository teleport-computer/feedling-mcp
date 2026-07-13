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

**per-turn 硬超时**（曾经是死代码——D2 `watchdog.should_kill` 的 clause (c) 一直在读
`current_turn_age_sec`，但直到这次改动之前从没有任何东西往 `poll_liveness()` 的返回值
里填过这个字段）：`poll_liveness()` 现在额外算出 `current_turn_age_sec` = 当前挂钟时间
减去 `_turn_starts` 里最老的那个 turn_start（没有任何 slot 在跑 turn 时为 `0.0`）。
CRITICAL 的性质：一个卡在 `_run_turn` 里的 slot 会彻底停止发送任何消息（既不会再
claim，也不会跑完 `_run_turn`，也不会空转唤醒）——它留在 `_turn_starts` 里的那个
turn_start 就此定住不再更新，但 `poll_liveness()` 每次调用都用**当下**的挂钟时间去减
它，所以 `current_turn_age_sec` 会随真实流逝的挂钟时间持续增长，直到超过
`turn_hard_timeout_sec`，即使这个 slot 一条新消息都不再发。这正是它跟粗粒度的
`last_progress_age_sec` 互补的地方：其它 slot 仍在正常工作时，`last_progress_age_sec`
整体依然新鲜（clause (b) 不会触发），只有 `current_turn_age_sec` 能把这一个卡住的
turn 单独抓出来。

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
from typing import Any, Callable

log = logging.getLogger("feedling.runtime_v2.child_supervisor")

# `multiprocessing.connection.Connection.poll()` 的轮询粒度：够短以便 stop()/reader 线程
# 能及时响应，又不至于空转烧 CPU。
_POLL_TIMEOUT_SEC = 0.5


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
    ) -> None:
        self._spawn_target = spawn_target
        self.liveness_timeout_sec = float(liveness_timeout_sec)
        self._spawn_args = tuple(spawn_args)
        self._ctx = mp.get_context("spawn")

        self._lock = threading.Lock()
        self._proc: mp.process.BaseProcess | None = None
        self._read_conn = None
        self._last_progress_at: float | None = None
        # hard-timeout fix: per-slot turn_start, keyed by slot_id. Populated/cleared
        # by _handle_message on every ("progress", slot_id, ts, turn_start) message;
        # read by poll_liveness() to compute current_turn_age_sec. See module
        # docstring's "progress pipe 协议" / "per-turn 硬超时" sections.
        self._turn_starts: dict[Any, float] = {}

        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """spawn 一个新子进程 + 起后台 reader 线程drain progress pipe。

        可重复调用（`kill_and_respawn` 内部就是 kill 完再调一次这个）——每次都是全新
        的 Pipe + 全新的 Process，不复用上一代子进程的任何状态。
        """
        read_conn, write_conn = mp.Pipe(duplex=False)
        proc = self._ctx.Process(
            target=self._spawn_target,
            args=(write_conn, *self._spawn_args),
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
            # Fresh generation of child == fresh turn-tracking state. A prior
            # generation's slot_ids/turn_starts must never leak into this one
            # (kill_and_respawn() calls start() again after killing the old proc).
            self._turn_starts = {}

        self._reader_stop = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._drain_loop, name="v2-child-supervisor-reader", daemon=True)
        self._reader_thread.start()
        log.info("[v2.child_supervisor] spawned child pid=%s target=%s",
                 proc.pid, getattr(self._spawn_target, "__qualname__", self._spawn_target))

    def poll_liveness(self) -> dict:
        """`{"alive": bool, "last_progress_age_sec": float, "current_turn_age_sec": float}`.

        `alive` 只反映"OS 进程还在不在"（`proc.is_alive()`），不代表它没卡死——一个
        在死锁里转不动的进程同样 `is_alive() == True`。粗粒度的卡死信号是
        `last_progress_age_sec`：多久没收到过**任意** slot 的一条 progress 消息了。从未
        收到过/尚未 `start()` 过时返回 `math.inf`，好让调用方（D2 watchdog）的阈值比较
        `age > liveness_timeout_sec` 自然成立，不需要单独判 None。

        `current_turn_age_sec`（hard-timeout fix，喂给 `watchdog.should_kill` 的
        clause (c)）：当下挂钟时间减去 `_turn_starts` 里最老的那个 turn_start——也就是
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
            turn_starts = list(self._turn_starts.values())
        alive = bool(proc is not None and proc.is_alive())
        age = math.inf if last is None else max(0.0, time.monotonic() - last)
        now = time.monotonic()
        current_turn_age_sec = max((now - ts for ts in turn_starts), default=0.0)
        current_turn_age_sec = max(0.0, current_turn_age_sec)
        return {
            "alive": alive,
            "last_progress_age_sec": age,
            "current_turn_age_sec": current_turn_age_sec,
        }

    def kill_and_respawn(self, *, join_timeout: float = 5.0) -> None:
        """SIGKILL 当前子进程（不可catch，就是要硬杀掉卡死的那个），join，然后重新
        `start()` 一个全新子进程。是否 SIGKILL 由调用方（D2 watchdog）的
        `should_kill` 判定驱动——本方法本身不判断，只执行。"""
        self._stop_reader()
        proc = self._proc
        if proc is not None:
            try:
                if proc.is_alive():
                    log.warning("[v2.child_supervisor] SIGKILL wedged child pid=%s", proc.pid)
                    proc.kill()
                proc.join(join_timeout)
                if proc.is_alive():
                    log.error(
                        "[v2.child_supervisor] child pid=%s still alive %.1fs after "
                        "SIGKILL+join — orphaned; proceeding to respawn anyway",
                        proc.pid, join_timeout)
            except Exception as e:  # noqa: BLE001 — 杀不掉也不能拖垮父进程的 watchdog 循环
                log.warning("[v2.child_supervisor] kill_and_respawn cleanup failed: %s", e)
        self._close_read_conn()
        self.start()

    def stop(self, *, drain_timeout: float = 10.0, kill_timeout: float = 2.0) -> None:
        """优雅停止：SIGTERM（子进程的信号处理器据此 drain 手上的回合再退出）+ 限时
        join；超时仍活着才升级成 SIGKILL。父进程 `_serve` 的 finally/stop_event 路径调
        这个，不是 `kill_and_respawn`——那个是 watchdog 专用的"不管三七二十一先杀死"。
        """
        self._stop_reader()
        proc = self._proc
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
            except Exception as e:  # noqa: BLE001 — shutdown 必须有界，不能被这里的异常卡住
                log.warning("[v2.child_supervisor] stop() cleanup failed: %s", e)
        self._close_read_conn()
        with self._lock:
            self._proc = None
        log.info("[v2.child_supervisor] stopped")

    # -- progress pipe reader -------------------------------------------------

    def _drain_loop(self) -> None:
        conn = self._read_conn
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

    def _handle_message(self, msg: Any) -> None:
        try:
            kind = msg[0]
        except (TypeError, IndexError):
            return
        if kind != "progress":
            return
        # msg shape: ("progress", slot_id, monotonic_ts, turn_start). The 4th
        # element is optional at the parse level (defensive against an older/
        # narrower fake target in a test that only sends a 3-tuple, e.g.
        # tests/test_v2_child_supervisor.py's pre-existing fakes) — absent, this
        # message carries no turn-start information and only refreshes the
        # coarse last_progress_at clock below.
        slot_id = msg[1] if len(msg) > 1 else None
        turn_start = msg[3] if len(msg) > 3 else None
        with self._lock:
            self._last_progress_at = time.monotonic()
            if len(msg) > 3:
                if turn_start is None:
                    self._turn_starts.pop(slot_id, None)
                else:
                    self._turn_starts[slot_id] = turn_start

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
