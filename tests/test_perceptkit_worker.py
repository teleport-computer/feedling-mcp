"""投递 worker —— 它现在不接任何东西，测试要钉住的第一件事就是「不接」。

这个 worker 一旦接上就会开始给真实用户投递事件。所以最重要的两条测试不是
「它能投」，是「没人叫它它不会自己跑」和「一条坏事件不会让整个循环停摆」。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from perceptkit.contracts.receipt import WakeReceipt  # noqa: E402
from perceptkit.contracts.records import EventOutboxEntry  # noqa: E402

from perception.perceptkit_adapter import schema, worker  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402

DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(
    not DSN, reason="没有 PERCEPTKIT_TEST_PG，跳过投递 worker 的真库验收")

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


@pytest.fixture
def db():
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(schema.DDL)
    conn.execute(schema.TRUNCATE)
    yield conn
    conn.close()


def factory():
    return PostgresStorage(psycopg.connect(DSN, autocommit=True))


def entry(eid: str) -> EventOutboxEntry:
    return EventOutboxEntry(
        event_id=eid, subject_id="u1", definition_id="d1", definition_version=1,
        event_type="activity.step_goal_reached", occurred_at=T0, detected_at=T0,
        delivery_state="pending", fact_snapshot={},
    )


class Runtime:
    """最小的 WakePort 实现，按 event_id 幂等。"""

    def __init__(self, behaviour="accepted"):
        self.behaviour = behaviour
        self.seen: list[str] = []

    def wake(self, event, attempt):
        if self.behaviour == "raise":
            raise RuntimeError("runtime 挂了")
        status = "duplicate" if event.event_id in self.seen else self.behaviour
        self.seen.append(event.event_id)
        return WakeReceipt(event_id=event.event_id, attempt_id=attempt.attempt_id,
                           status=status, received_at=T0)


# ---------------------------------------------------------------------------
# 最重要的一条：它现在不接任何东西
# ---------------------------------------------------------------------------

def test_importing_the_module_starts_nothing(db):
    """import 一下就开始给真实用户投递事件，是这类模块最坏的形态。"""
    import importlib
    store = factory()
    store.enqueue_event(entry("e1"))
    importlib.reload(worker)
    assert store.list_pending_events(subject_id="u1")     # 还躺在发件箱里


def test_no_scheduler_or_supervisor_references_this_worker():
    """接线是单独一步、要人来做。这条测试会在有人接上时红 ——
    那时请连同「接在哪、默认起不起」一起想清楚，再改这条测试。
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "backend"
    hits = []
    for p in root.rglob("*.py"):
        if "perceptkit_adapter" in str(p):
            continue
        if re.search(r"perceptkit_adapter[.\s]*(import\s+)?worker|from .*worker import run_forever",
                     p.read_text(encoding="utf-8")):
            hits.append(str(p.relative_to(root)))
    assert not hits, "有人把 worker 接上了：" + ", ".join(hits)


# ---------------------------------------------------------------------------
# 一轮能干活
# ---------------------------------------------------------------------------

def test_one_round_drains_what_is_there(db):
    store = factory()
    for i in range(3):
        store.enqueue_event(entry(f"e{i}"))

    rt = Runtime()
    out = worker.run_once(storage_factory=factory, wake=rt, worker_id="w1", now=T0)
    assert len(out.delivered) == 3
    assert store.list_pending_events(subject_id="u1") == []


def test_a_round_never_exceeds_its_batch(db):
    """一轮跑太久，前面已经领的事件会被别人接管 —— 用户被提醒两次。"""
    store = factory()
    for i in range(6):
        store.enqueue_event(entry(f"e{i}"))

    out = worker.run_once(storage_factory=factory, wake=Runtime(),
                          worker_id="w1", now=T0, batch=2)
    assert len(out.delivered) == 2
    assert len(store.list_pending_events(subject_id="u1")) == 4


def test_an_empty_outbox_is_not_an_error(db):
    out = worker.run_once(storage_factory=factory, wake=Runtime(),
                          worker_id="w1", now=T0)
    assert not out.delivered and not out.dead


# ---------------------------------------------------------------------------
# 一条坏事件不能让整个循环停摆
# ---------------------------------------------------------------------------

def test_a_failing_round_does_not_end_the_loop(db):
    """循环停了，发件箱对所有用户都不再排空，而且没有任何地方说为什么。

    ⚠️ 失败必须发生在 **worker 这一层**（比如连不上库）。
    runtime 自己抛异常是 kit 接住的（当成 enqueue_failed 重试），
    拿那个来测，测的是 kit 不是 worker —— 这条先前就是那么写的，测了个寂寞。
    """
    store = factory()
    store.enqueue_event(entry("e1"))

    rounds = {"n": 0}

    def exploding_factory():
        if rounds["n"] <= 2:
            raise ConnectionError("连不上库")
        return factory()

    def should_stop():
        rounds["n"] += 1
        return rounds["n"] > 4

    worker.run_forever(
        storage_factory=exploding_factory, wake=Runtime(), worker_id="w1",
        should_stop=should_stop, sleep=lambda _: None,
    )
    assert rounds["n"] > 4                    # 连着两轮炸掉之后仍然继续转
    assert store.list_pending_events(subject_id="u1") == []   # 后面几轮把它投出去了


def test_a_runtime_that_raises_is_the_kits_problem_not_the_loops(db):
    """runtime 抛异常算投递失败、进重试 —— 不是 worker 该处理的异常。"""
    store = factory()
    store.enqueue_event(entry("e1"))
    out = worker.run_once(storage_factory=factory, wake=Runtime("raise"),
                          worker_id="w1", now=T0)
    assert out.retrying == ["e1"] and not out.delivered


def test_the_loop_sleeps_instead_of_spinning_on_an_empty_outbox(db):
    slept: list[float] = []
    rounds = {"n": 0}

    def should_stop():
        rounds["n"] += 1
        return rounds["n"] > 2

    worker.run_forever(
        storage_factory=factory, wake=Runtime(), worker_id="w1",
        should_stop=should_stop, sleep=slept.append,
    )
    assert slept and all(s > 0 for s in slept)


def test_a_suppressed_event_is_not_retried_forever(db):
    """「现在不想被打扰」是正常应答，不是失败。"""
    store = factory()
    store.enqueue_event(entry("e1"))

    out = worker.run_once(storage_factory=factory,
                          wake=Runtime("conversation_suppressed"),
                          worker_id="w1", now=T0)
    assert out.suppressed and not out.retrying
    assert store.list_pending_events(subject_id="u1") == []
