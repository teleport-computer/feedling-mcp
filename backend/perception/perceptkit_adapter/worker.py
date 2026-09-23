"""The dispatcher loop that drains PerceptKit's outbox.

**Nothing starts this.** No supervisor entry, no scheduler registration, no
import from a running path. Wiring it up is a separate, deliberate act -- a
dispatcher that begins running the moment its module is imported would start
delivering events on a branch nobody has finished reviewing.

The kit stops at "the event is durable and committed"; delivery is the host's
loop. That split is not tidiness: delivering inside the ingest call would add
the agent runtime's latency to the report endpoint, so a slow runtime makes
the client time out and retransmit, which makes the runtime slower.

## What a round must not do

Run long. The claim carries a lease, and a round that outlives its lease has
its events taken over by another worker while it is still delivering them --
the user hears the same thing twice. `drain`'s limit exists for that reason,
and the lease should comfortably exceed one round's worst case.

Hold one connection across rounds. Every round takes a connection, builds a
storage adapter on it, and gives it back. `transaction()` needs all writes on
one connection, so the adapter deliberately takes a connection rather than a
pool -- but keeping one open between rounds means an idle transaction sitting
on the database for the whole sleep.

Die on one bad event. A single event that makes the runtime raise must not
end the loop; the outbox would stop draining for everybody. It is logged and
the round moves on, and the delivery state machine's retry cap eventually
sends a permanently failing event to dead-letter.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from perceptkit.processing.dispatch import DispatchOutcome, drain

log = logging.getLogger(__name__)

#: Events delivered per round. Bounded so a round finishes well inside its
#: lease.
DEFAULT_BATCH = 50

#: How long a claim is held. Comfortably longer than a round's worst case: if
#: it expires mid-round another worker takes the event over and the user is
#: told twice.
DEFAULT_LEASE_SECONDS = 120.0

#: Sleep between rounds when the outbox came back empty.
DEFAULT_IDLE_SLEEP = 5.0


def run_once(
    *,
    storage_factory: Callable[[], Any],
    wake: Any,
    worker_id: str,
    now: datetime | None = None,
    batch: int = DEFAULT_BATCH,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> DispatchOutcome:
    """One round. Safe to call from a test, a CLI, or a scheduler.

    ``storage_factory`` hands back a fresh storage bound to its own
    connection, and the round is done with it when it returns.
    """
    storage = storage_factory()
    return drain(
        storage=storage, wake=wake, worker_id=worker_id,
        now=now or datetime.now(timezone.utc),
        limit=batch, lease_seconds=lease_seconds,
    )


def run_forever(
    *,
    storage_factory: Callable[[], Any],
    wake: Any,
    worker_id: str,
    batch: int = DEFAULT_BATCH,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    idle_sleep: float = DEFAULT_IDLE_SLEEP,
    should_stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Loop until ``should_stop`` says otherwise.

    ``should_stop`` and ``sleep`` are injected so a test can run a bounded
    number of rounds without waiting in real time.
    """
    while not (should_stop and should_stop()):
        try:
            outcome = run_once(
                storage_factory=storage_factory, wake=wake, worker_id=worker_id,
                batch=batch, lease_seconds=lease_seconds,
            )
        except Exception:                      # noqa: BLE001
            # One failing round must not end the loop -- the outbox would stop
            # draining for every user, and nothing would say why.
            log.exception("perceptkit dispatch round failed; continuing")
            sleep(idle_sleep)
            continue

        moved = (len(outcome.delivered) + len(outcome.retrying)
                 + len(outcome.dead) + len(outcome.suppressed)
                 + len(outcome.rejected))
        if outcome.dead:
            # Dead-letter is the one outcome worth noticing: the runtime never
            # accepted it and never will, so a user is silently missing
            # something we decided was worth saying.
            log.warning("perceptkit: %d event(s) went to dead-letter",
                        len(outcome.dead))
        if not moved:
            sleep(idle_sleep)


__all__ = ["DEFAULT_BATCH", "DEFAULT_LEASE_SECONDS", "DEFAULT_IDLE_SLEEP",
           "run_once", "run_forever"]


# ---------------------------------------------------------------------------
# io 这边的接线（外部审查 F6）
# ---------------------------------------------------------------------------
#
# 上面那两个是框架中立的。下面这两个是 io 的具体接法：从连接池拿一条连接、
# 用 io 的 WakePort，并且**由 asgi 的 lifespan 真的把它跑起来**。
#
# 在这之前只有"这次上报刚产生了新事件"才会顺带把积压的补投一次
# （ingest 的 dispatch=True）。用户不再产生新数据时，卡住的提醒就一直卡着 ——
# 投递失败过一次的事件要等到下一次有人给他发新数据才有机会重投，而
# "他这会儿没在用"恰恰是最常见的情形。

def run_round(*, now: datetime | None = None,
              batch: int = DEFAULT_BATCH) -> DispatchOutcome | None:
    """一轮 drain。没启用或没配唤醒时返回 None（不是错误）。"""
    from .shadow import enabled, wakes_enabled
    if not enabled() or not wakes_enabled():
        return None

    import db

    from .storage import PostgresStorage
    from .wake_port import FeedlingWakePort

    # 每轮拿一条连接、用完还回去。跨轮持有等于一整个 sleep 期间挂着一个
    # 空闲事务；而 transaction() 又要求所有写在同一条连接上，所以是"每轮一条"。
    with db.get_pool().connection() as conn:
        conn.autocommit = True
        return run_once(
            storage_factory=lambda: PostgresStorage(conn),
            wake=FeedlingWakePort(),
            worker_id=_worker_id(),
            now=now, batch=batch,
        )


def _worker_id() -> str:
    """认领租约用的身份。**每个进程一个** —— 两个进程共用一个 id 时，
    另一个的租约看起来像自己的，会把对方正在投的事件抢过来重投一遍。"""
    import os
    import socket
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_loop(stop_event, *, interval: float) -> None:
    """常驻循环，由 asgi lifespan 启动。

    一轮失败不结束循环 —— 发件箱会对**所有人**停止排空，而且没有任何地方
    说得出为什么。
    """
    import asyncio

    while not stop_event.is_set():
        try:
            outcome = await asyncio.to_thread(run_round)
            if outcome and outcome.dead:
                log.warning("perceptkit: %d event(s) went to dead-letter",
                            len(outcome.dead))
        except Exception:                      # noqa: BLE001
            log.exception("perceptkit dispatch round failed; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
