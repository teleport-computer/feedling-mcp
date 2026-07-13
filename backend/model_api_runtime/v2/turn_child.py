"""Turn-child 子进程入口（Hosted Runtime V2 PR D，D1 结构拆分，Task 2）。

`serve_worker._serve`（父进程）不再直接 `asyncio.create_task(v2_worker.run_worker_loop
(...))`——那样 turn slot 跟 reaper/heartbeat/scheduler/Genesis 共用一个事件循环，一个
slot 里卡死的同步调用能拖死全部。现在父进程改用 `child_supervisor.ChildSupervisor` spawn
一个独立子进程，`turn_child.main` 就是那个子进程的入口：在全新的解释器里重新做一遍
`serve_worker.main()` 对"跑 turn 所需"的那部分装配（DB pool 是 lazy 的，spawn 出来的子
进程根本没有继承父进程已经建好的连接池/线程/事件循环，必须重新走一遍），然后只跑
`v2_worker.run_worker_loop`——reaper/heartbeat/scheduler/Genesis 仍然留在父进程
（父进程不受子进程卡死影响，才谈得上"父进程还活着才能 SIGKILL 子进程"）。

**复用 `serve_worker` 而不是重复装配逻辑**：`wire_assembly()`/`build_production_deps()`/
`_configure_provider_thread_limiter()` 都已经在 `serve_worker.py` 里实现好了——那是这个
代码库里"唯一允许同时 import hosted/agent_runtime 和 core/model_api_runtime 的装配层"
（见 serve_worker.py 模块 docstring）。本文件在函数体内部（不是模块顶层）`import
serve_worker` 来复用它们，从而不必自己再 import 一遍 `hosted`/`agent_runtime`——这样
`tests/test_v2_dependency_direction.py`（把 v2/ 下除 serve_worker.py/__init__.py 外的每个
模块都当作 core 模块，禁止直接 `import hosted`/`import agent_runtime`）天然通过：本文件
自己的 AST 里根本没出现这两个词，"只有 serve_worker.py 直接碰 hosted/agent_runtime"这条
不变量并没有被绕过，只是多了一层转发。

反过来，`serve_worker._serve` 对本模块的 import 也刻意放在函数体内部而非模块顶层——
`turn_child` 顶层要 `import serve_worker`，如果 `serve_worker.py` 顶层也 `import
turn_child`，两边互相在对方还没加载完时导入对方，会踩 Python 循环 import 的坑（拿到一个
还没跑完 module body 的半成品模块）。函数体内 import 在 `_serve()` 真正被调用时才执行，
那时 `serve_worker` 模块早已加载完毕，没有这个风险。

**progress pipe（Task 2 更新：slot 驱动而非自由跑计时器）**：早前版本用一个跟
`run_worker_loop` 并发跑在同一个事件循环里的独立协程 `_progress_ticker`，每隔一小段
时间无条件往父进程发一条 `("progress", -1, monotonic)`。那个信号只在事件循环整体被
某个未经 `to_thread` 桥出去的同步阻塞点（死锁、失控的 provider SDK 调用等）卡住时才会
停摆——它抓不住"事件循环本身仍在正常调度、但每个 turn slot 都各自挂在一个永不返回的
`await` 上"这种情况：ticker 协程本身跟 slot 无关，只要 loop 还能调度任务它就按时发心跳，
哪怕全部 slot 一个 job 也claim不到/一个回合也跑不完（Task 3 的 watchdog 需要能分辨这两种
"卡死"）。

现在 progress 由 `worker.run_worker_loop`/`_slot_loop` 的 `progress_cb` 参数驱动
（真实 slot 活动：claim 到 job、`_run_turn` 跑完、每次空转 poll 醒来）——本模块把
`conn.send(("progress", slot_id, time.monotonic(), turn_start))` 包成这个回调传进去，
不再有独立的计时协程。净效果：全部 slot 同时卡在一个永不返回的 `await` 上时（没有
claim、没有 turn 完成、也没有空转唤醒，因为它们根本不是空闲——是卡住了），progress 会
变陈旧，Task 3 的 watchdog 才能据此判定"全部 slot 永久卡死"这个验收场景。

**per-turn 硬超时（曾经是死代码，现已接线）**：这条同一根 pipe 现在还额外携带
`turn_start`——slot claim 到 job、即将进入 `_run_turn` 时的那个 `time.monotonic()`，
turn 跑完/空转时为 `None`。`ChildSupervisor` 用它算出 `current_turn_age_sec`（父进程
挂钟时间减去最后一次收到的 `turn_start`，见 `child_supervisor.py` 的 progress pipe 协议
注释），喂给 `watchdog.should_kill` 的 clause (c)。这解决的是 clause (b)（全池陈旧+有活
可抢）覆盖不到的场景：**单个** slot 卡在 `_run_turn` 里，其它 slot 仍然健康工作——
`last_progress_age_sec` 整体依然新鲜（其它 slot 在正常发 progress），clause (b) 不会
触发，卡住的那一个 slot 只能靠 `current_turn_age_sec`（挂钟时间持续增长，不依赖这个 slot
之后还发不发消息）单独抓出来。
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

    def _progress_cb(slot_id: int, turn_start: float | None = None) -> None:
        try:
            conn.send(("progress", slot_id, time.monotonic(), turn_start))
        except (BrokenPipeError, OSError, ValueError) as e:
            log.info("[v2.turn_child] progress pipe closed, dropping progress signal: %s", e)

    return _progress_cb


async def _run(conn: "Connection", worker_id: str, poll_interval: float) -> None:
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

    provider_threads = serve_worker._configure_provider_thread_limiter(v2_worker.MAX_WORKERS)
    deps = serve_worker.build_production_deps()
    # "v2_jobs" 即时唤醒：跟父进程原来的 _serve 一样，wake_event 必须在 running loop 里
    # 创建/绑定。`wire_assembly()`（在 `main()` 里、进入这个协程之前）只负责注册
    # handler，不依赖 running loop——镜像 serve_worker._serve 里同一段注释的说明。
    wake_event = asyncio.Event()
    v2_worker.set_job_wake_context(loop, wake_event)
    log.info(
        "[v2.turn_child] starting worker=%s max_workers=%s provider_threads=%s pid=%s",
        worker_id, v2_worker.MAX_WORKERS, provider_threads, os.getpid(),
    )

    tasks = [
        asyncio.create_task(v2_worker.run_worker_loop(
            worker_id,
            max_workers=v2_worker.MAX_WORKERS,
            poll_interval=poll_interval,
            stop_event=stop_event,
            deps=deps,
            wake_event=wake_event,
            progress_cb=_make_progress_cb(conn),
        )),
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


def main(conn: "Connection", worker_id: str, poll_interval: float | None = None) -> None:
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
        db.init_schema()
        serve_worker.wire_assembly()
        interval = (
            poll_interval if poll_interval is not None
            else serve_worker._positive_float_env("FEEDLING_V2_POLL_INTERVAL_SEC", "1.0")
        )
        asyncio.run(_run(conn, worker_id, interval))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — best-effort fd cleanup on the way out
            pass
        log.info("[v2.turn_child] child process exiting pid=%s worker_id=%s", os.getpid(), worker_id)
