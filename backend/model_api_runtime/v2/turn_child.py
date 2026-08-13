"""Turn-child 子进程入口（Hosted Runtime V2 PR D，D1 结构拆分，Task 2）。

`serve_worker._serve`（父进程）不再直接 `asyncio.create_task(v2_worker.run_worker_loop
(...))`——那样 turn slot 跟 reaper/heartbeat/scheduler/Genesis 共用一个事件循环，一个
slot 里卡死的同步调用能拖死全部。现在父进程改用 `child_supervisor.ChildSupervisor` spawn
一个独立子进程，`turn_child.main` 就是那个子进程的入口：在全新的解释器里重新做一遍
`serve_worker.main()` 对"跑 turn 所需"的那部分装配（DB pool 是 lazy 的，spawn 出来的子
进程根本没有继承父进程已经建好的连接池/线程/事件循环，必须重新走一遍），然后只跑
`v2_worker.run_worker_loop`——reaper/heartbeat/scheduler/Genesis 仍然留在父进程
（父进程不受子进程卡死影响，才谈得上"父进程还活着才能 SIGKILL 子进程"）。

**复用 `serve_worker` 而不是重复装配逻辑**：`wire_assembly()`/
`build_production_deps()` 都已经在 `serve_worker.py` 里实现好了——那是这个
代码库里"唯一允许同时 import hosted/agent_runtime 和 core/model_api_runtime 的装配层"
（见 serve_worker.py 模块 docstring）。本文件只 `import serve_worker` 这个装配层来复用
它们，从而不必自己再 import 一遍 `hosted`/`agent_runtime`——这样
`tests/test_v2_dependency_direction.py`（把 v2/ 下除 serve_worker.py/__init__.py 外的每个
模块都当作 core 模块，禁止直接 `import hosted`/`import agent_runtime`）天然通过：本文件
自己的 AST 里根本没出现这两个词，"只有 serve_worker.py 直接碰 hosted/agent_runtime"这条
不变量并没有被绕过，只是多了一层转发。

反过来，`serve_worker._serve` 对本模块的 import 刻意放在函数体内部而非模块顶层——如果
`serve_worker.py` 顶层也 `import turn_child`，两边会在对方还没加载完时互相导入，踩到
Python 循环 import。函数体内 import 在 `_serve()` 真正被调用时才执行，那时
`serve_worker` 模块早已加载完毕，没有这个风险。

**progress pipe 有两类信号**：`loop_heartbeat` 每 5s 证明 event loop 还能调度；slot
`progress` 则只在 claim/idle/turn 内真实 provider、tool、compaction 边界发送，并携带固定
`turn_start`。父进程分别维护 loop age、slot age、active-turn stall age 与 absolute age。
所以同步阻塞会让 loop heartbeat 变旧；卡在一个永不返回的 async await 会让该 slot 的 stall
age 变旧（即使 loop heartbeat 仍新鲜）；合法的 60/90s provider 调用、多轮工具循环和 600s
prompt catch-up 会在边界持续刷新 stall age，却不会重置 absolute start。这个分离同时避免
旧 45s pool-wide / 180s absolute 两种误杀，也仍能物理 SIGKILL 真正卡住的 child。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

# 跟 serve_worker.py 一样的 sys.path 兜底：spawn context 在全新解释器里只 unpickle 出
# "模块名 + 函数名"再重新 import，若这个模块曾经是以脚本方式被发现的（不太可能，因为
# turn_child 永远是被 serve_worker 当库 import 的），保险起见镜像同一份兜底逻辑。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import db  # noqa: E402
from model_api_runtime.v2 import serve_worker  # noqa: E402 — 见模块 docstring：复用装配层
from model_api_runtime.v2 import slot_protocol  # noqa: E402
from model_api_runtime.v2 import worker as v2_worker  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover — 类型检查专用，运行期不需要真的 import
    from multiprocessing.connection import Connection

log = logging.getLogger("feedling.runtime_v2.turn_child")


def _make_progress_cb(conn: "Connection") -> "callable":
    """构造喂给 `worker.run_worker_loop(progress_cb=...)` 的回调：每次真实 slot 活动
    （claim 到 job / turn 跑完 / 空转 poll 醒来——见 `worker._slot_loop` 的三个调用点）
    往 `conn`（progress pipe 写端）发一条 `("progress", slot_id, monotonic, turn_start)`。

    `turn_start`（hard-timeout fix）：原样转发 `worker._slot_loop` 传来的第二个参数——
    这个 slot 当前正在跑的 turn 的开始时刻（claim 之后、`_run_turn` 之前），或者
    `None`（turn 跑完/空转，slot 当前空闲）。父进程侧的 `ChildSupervisor` 用它算
    `current_turn_age_sec`（见该模块的 progress pipe 协议注释）——一个 slot 卡死在
    `_run_turn` 内部永不返回时，它发出的最后一条消息里的 `turn_start` 就是父进程能拿到
    的最新数据，父进程用挂钟时间减去它，年龄跟着挂钟时间持续增长，不依赖这个 slot 之后
    还发不发消息。

    `progress_cb` 本身是同步函数（`_slot_loop` 直接调用，不 await），必须便宜——`conn.send`
    是一次本地 pipe 写，不是网络往返。send 失败（父进程已经把读端关掉，例如父进程自己
    正在退出）必须原地吞掉、绝不能把异常炸回 `_slot_loop`——`_slot_loop` 自己也用
    try/except 包了这次调用（双保险），这里再兜一层是因为 `conn.send` 在 BrokenPipe 之外
    还可能抛 `OSError`/`ValueError`（管道已 close 后再 send）。"""

    def _progress_cb(progress: slot_protocol.SlotProgress) -> None:
        try:
            conn.send(slot_protocol.encode_message(progress))
        except (BrokenPipeError, OSError, ValueError) as e:
            log.info("[v2.turn_child] progress pipe closed, dropping progress signal: %s", e)

    return _progress_cb


async def _event_loop_heartbeat(
    conn: "Connection",
    stop_event: asyncio.Event,
    *,
    slot_generation: str,
    interval: float = 5.0,
) -> None:
    """Prove that the child event loop itself can still schedule callbacks.

    This signal refreshes only the supervisor's process-level liveness clock;
    it deliberately does not refresh any active turn's stall clock.  The two
    signals cover different failure modes:

    * a synchronous event-loop block stops this heartbeat and trips the short
      pool-wide liveness watchdog;
    * an ``await`` that never returns leaves the loop heartbeat healthy, but
      its slot stops crossing provider/tool/compaction boundaries and trips
      the per-turn stall watchdog.

    Keeping them separate also prevents a normal 60-second async provider wait
    from looking like a dead 45-second child merely because all slots happen to
    be busy while another job is queued.
    """
    while not stop_event.is_set():
        try:
            conn.send(
                slot_protocol.encode_message(
                    slot_protocol.LoopHeartbeat(
                        slot_generation=slot_generation,
                        monotonic_at=time.monotonic(),
                    )
                )
            )
        except (BrokenPipeError, OSError, ValueError) as e:
            log.info("[v2.turn_child] heartbeat pipe closed: %s", e)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _run(
    conn: "Connection",
    worker_id: str,
    poll_interval: float,
    slot_generation: str,
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # 极少数平台/事件循环组合不支持 add_signal_handler——退化为"收不到干净 drain
            # 信号"，但进程仍然会被裸 SIGTERM 杀掉（只是跳过 drain），父进程的 SIGKILL
            # 路径（watchdog kill_and_respawn）完全不受影响，那条本来就是不可 catch 的。
            log.warning("[v2.turn_child] add_signal_handler unsupported for %s", sig)

    deps = serve_worker.build_production_deps()
    # "v2_jobs" 即时唤醒：跟父进程原来的 _serve 一样，wake_event 必须在 running loop 里
    # 创建/绑定。`wire_assembly()`（在 `main()` 里、进入这个协程之前）只负责注册
    # handler，不依赖 running loop——镜像 serve_worker._serve 里同一段注释的说明。
    wake_event = asyncio.Event()
    v2_worker.set_job_wake_context(loop, wake_event)
    log.info(
        "[v2.turn_child] starting worker=%s max_workers=%s pid=%s",
        worker_id, v2_worker.MAX_WORKERS, os.getpid(),
    )

    tasks = [
        asyncio.create_task(v2_worker.run_worker_loop(
            worker_id,
            max_workers=1,
            poll_interval=poll_interval,
            stop_event=stop_event,
            deps=deps,
            wake_event=wake_event,
            progress_cb=_make_progress_cb(conn),
            slot_generation=slot_generation,
            slot_ids=["foreground-0"],
        )),
        asyncio.create_task(
            _event_loop_heartbeat(
                conn, stop_event, slot_generation=slot_generation
            )
        ),
    ]
    try:
        # run_worker_loop 已经把所有可恢复的 per-slot 故障吞在内部；能逃出来的异常代表
        # slot 不变量被打破了——镜像父进程原 _serve 的处理方式：不吞，往上冒，让子进程
        # 退出（`main()` 的 try/finally 兜底关 conn），父进程的 watchdog/reaper 会看到
        # `alive=False` 并重新 spawn 一个干净的子进程，好过悄悄少算 slot 数却仍然广播
        # 满容量心跳。
        await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("[v2.turn_child] drained; exiting worker=%s pid=%s", worker_id, os.getpid())


def main(
    conn: "Connection",
    worker_id: str,
    poll_interval: float | None = None,
    slot_generation: str = "g0",
) -> None:
    """`child_supervisor.ChildSupervisor` 的 spawn target——签名约定
    `spawn_target(conn_write_end, *spawn_args)`，这里的 `spawn_args` 就是
    `(worker_id, poll_interval)`（由 `serve_worker._serve` 传入，沿用跟父进程心跳/
    reaper 同一个 `worker_id`，只是这份 slot 现在跑在另一个进程里）。

    在**全新的**解释器里重新做一遍 `serve_worker.main()` 里"跑 turn 所需"的那部分启动
    步骤：`db.init_schema()` 是幂等的（alembic upgrade head，见 db.py docstring），可以
    放心在每个子进程里重跑；`wire_assembly()` 同样幂等（load_users/register_handler/
    start_listener 都自身幂等）。DB 连接池是模块级懒加载单例，spawn 出来的子进程根本没
    继承父进程已经建好的那份（`spawn` start method 是全新解释器，不像 `fork` 会拷贝父
    进程的内存/fd 状态）——子进程第一次真正用到 DB 时会在自己的地址空间里另起一份。

    只重新做"跑 turn 所需"的那部分：不起 Genesis 线程、不跑 reaper/heartbeat/scheduler
    循环——那些留在父进程（`serve_worker._serve` 未改动的部分）。
    """
    logging.basicConfig(level=logging.INFO)
    log.info("[v2.turn_child] child process starting pid=%s worker_id=%s", os.getpid(), worker_id)
    try:
        # Validate before db.init_schema() creates this fresh process's lazy
        # pool.  The parent normally exports the computed ceiling before spawn;
        # the child repeats the check so direct invocation cannot bypass it.
        serve_worker._configure_db_pool_capacity(v2_worker.MAX_WORKERS)
        db.init_schema()
        serve_worker.wire_assembly()
        # 周期性全量自愈——和 serve_worker.main / backend lifespan 对称。turn_child
        # 是长命进程且经 wire_assembly 注册了 users handler + envelope 公钥 getter，
        # 但走自己的入口（不经 serve_worker.main）。少了这行,一条丢失的 users notify
        # 在本子进程永不自愈：用户轮换内容公钥后,本进程会一直用陈旧公钥封装托管回复
        # → decrypt-failed。wire_assembly 本身刻意不起它（被测试调用,见其归属说明）。
        serve_worker.accounts_registry.start_periodic_full_reload()
        interval = (
            poll_interval if poll_interval is not None
            else serve_worker._positive_float_env("FEEDLING_V2_POLL_INTERVAL_SEC", "1.0")
        )
        asyncio.run(_run(conn, worker_id, interval, slot_generation))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — best-effort fd cleanup on the way out
            pass
        log.info("[v2.turn_child] child process exiting pid=%s worker_id=%s", os.getpid(), worker_id)
