"""真 Postgres 上的一致性验收 —— 内存实现**永远验不出**的那三类。

conformance 套件自己写着：真正的事务边界、并发下只有一个胜者、崩溃恢复，
这三类在内存实现上永远是绿的（内存天然原子、天然无并发），
**把它们当验过了是那套东西最危险的用法**。这个文件就是补上那一块。

需要一个真库。没有就整份跳过 —— 但跳过要说出来，
「没跑」和「跑过了没问题」不能长得一样。

    docker run -d --name perceptkit-pg -e POSTGRES_PASSWORD=dev \
        -e POSTGRES_DB=perceptkit -p 55432:5432 postgres:16-alpine
    PERCEPTKIT_TEST_PG=postgresql://postgres:dev@127.0.0.1:55432/perceptkit \
        pytest tests/test_perceptkit_postgres.py
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from perceptkit.contracts.records import (  # noqa: E402
    CurrentProjection,
    EventOutboxEntry,
    StoredObservation,
)
from perceptkit.contracts.receipt import WakeReceipt  # noqa: E402

from perception.perceptkit_adapter import schema  # noqa: E402
from perception.perceptkit_adapter.storage import PostgresStorage  # noqa: E402

DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(
    not DSN,
    reason="没有 PERCEPTKIT_TEST_PG，跳过真库验收（这三类内存实现验不出）",
)

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
DAY = T0.date()


def connect():
    return psycopg.connect(DSN, autocommit=True)


@pytest.fixture
def clean():
    with connect() as c:
        c.execute(schema.DDL)
        c.execute(schema.TRUNCATE)
    yield


def store(conn=None) -> PostgresStorage:
    return PostgresStorage(conn or connect())


def obs(oid="o1", **over) -> StoredObservation:
    base = dict(observation_id=oid, subject_id="u1", signal="steps",
                signal_schema_version=1, source="ios", occurred_at=T0,
                received_at=T0, availability="observed", effective_local_date=DAY,
                typed_value={"step_count": 100})
    base.update(over)
    return StoredObservation(**base)


def current(**over) -> CurrentProjection:
    base = dict(subject_id="u1", signal="steps", dimension_key="steps",
                typed_value={"step_count": 100}, availability="observed",
                observed_at=T0, received_at=T0, version=0, content_digest="d1")
    base.update(over)
    return CurrentProjection(**base)


def outbox(eid="e1", **over) -> EventOutboxEntry:
    base = dict(event_id=eid, subject_id="u1", definition_id="d1",
                definition_version=1, event_type="activity.step_goal_reached",
                occurred_at=T0, detected_at=T0, delivery_state="pending",
                fact_snapshot={})
    base.update(over)
    return EventOutboxEntry(**base)


# ---------------------------------------------------------------------------
# 一、真正的事务边界（要从另一条连接观察）
# ---------------------------------------------------------------------------

def test_a_failed_transaction_leaves_nothing_half_written(clean):
    """规则状态写了、发件箱没写（或反过来），就再也说不清"为什么会有这个事件"。

    **必须从另一条连接看** —— 同一条连接看到的是自己未提交的东西，
    那证明不了任何事。
    """
    writer = store()
    observer = store()

    with pytest.raises(RuntimeError):
        with writer.transaction():
            writer.put_rule_state(subject_id="u1", definition_id="d1",
                                  scope_key="2026-08-27", state={"fired": True})
            writer.enqueue_event(outbox())
            raise RuntimeError("在提交之前炸了")

    assert observer.get_rule_state(subject_id="u1", definition_id="d1",
                                   scope_key="2026-08-27") is None
    assert observer.list_pending_events(subject_id="u1") == []


def test_a_committed_transaction_is_visible_to_everyone_at_once(clean):
    writer, observer = store(), store()
    with writer.transaction():
        writer.put_rule_state(subject_id="u1", definition_id="d1",
                              scope_key="2026-08-27", state={"fired": True})
        writer.enqueue_event(outbox())

    assert observer.get_rule_state(subject_id="u1", definition_id="d1",
                                   scope_key="2026-08-27") == {"fired": True}
    assert len(observer.list_pending_events(subject_id="u1")) == 1


def test_nothing_is_visible_from_outside_before_the_commit(clean):
    """写到一半的东西不能被别人读到 —— 否则 worker 会捞到一条
    还没真正存在的事件，投出去之后写它的那个事务却回滚了。"""
    writer, observer = store(), store()
    done = threading.Event()
    seen: list[int] = []

    def hold_open():
        with writer.transaction():
            writer.enqueue_event(outbox())
            seen.append(len(observer.list_pending_events(subject_id="u1")))
            done.set()

    t = threading.Thread(target=hold_open)
    t.start(); done.wait(5); t.join(5)
    assert seen == [0]


# ---------------------------------------------------------------------------
# 二、并发下只有一个胜者（要两条独立连接同时发起）
# ---------------------------------------------------------------------------

def test_two_workers_racing_for_one_event_only_one_wins(clean):
    """两个都拿到的话，用户会被同一件事提醒两次。"""
    store().enqueue_event(outbox())

    got: list[object] = []
    barrier = threading.Barrier(2)

    def grab(worker: str):
        s = store()
        barrier.wait()
        got.append(s.claim_pending_event(worker_id=worker, now=T0,
                                         lease_seconds=60))

    ts = [threading.Thread(target=grab, args=(f"w{i}",)) for i in (1, 2)]
    for t in ts: t.start()
    for t in ts: t.join(10)

    assert sum(1 for g in got if g is not None) == 1


def test_two_reports_with_the_same_id_race_and_only_one_is_accepted(clean):
    accepted: list[str] = []
    barrier = threading.Barrier(2)

    def claim(digest: str):
        s = store()
        barrier.wait()
        accepted.append(s.claim_report(subject_id="u1", producer="ios",
                                       report_id="r1", payload_digest=digest,
                                       received_at=T0).status)

    ts = [threading.Thread(target=claim, args=("same",)) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join(10)

    assert sorted(accepted) == ["accepted", "duplicate"]


def test_two_writers_racing_on_current_do_not_lose_the_newer_value(clean):
    """CAS 的返回值必须认。忽略它的话，两个事务都读到旧版本，
    较新的那个写入失败被静默丢掉 —— 当前值停在旧数据上，没有任何地方报错。
    """
    s0 = store()
    assert s0.compare_and_put_current(current(), expected_version=-1)

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def bump(value: int):
        s = store()
        barrier.wait()
        results.append(s.compare_and_put_current(
            current(typed_value={"step_count": value},
                    observed_at=T0 + timedelta(minutes=value), version=1),
            expected_version=0,
        ))

    ts = [threading.Thread(target=bump, args=(v,)) for v in (1, 2)]
    for t in ts: t.start()
    for t in ts: t.join(10)

    # 恰好一个赢；输的那个**收到 False**，由调用方重读重判，而不是以为自己成功了
    assert sorted(results) == [False, True]
    assert store().get_current(subject_id="u1", signals=["steps"])["steps"][0].version == 1


def test_two_appends_of_one_observation_only_store_it_once(clean):
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def append():
        s = store()
        barrier.wait()
        results.append(s.append_observation(obs()))

    ts = [threading.Thread(target=append) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join(10)

    assert sorted(results) == [False, True]


# ---------------------------------------------------------------------------
# 三、崩溃恢复（模拟"wake 已 accepted、回执还没存下来"就断电）
# ---------------------------------------------------------------------------

def test_an_event_claimed_by_a_dead_worker_is_taken_over_after_the_lease(clean):
    """原持有者可能已经死了。不接管的话，这条事件永远卡在 claimed，
    用户永远收不到 —— 不报错，只是永远不来。"""
    store().enqueue_event(outbox())
    first = store().claim_pending_event(worker_id="w1", now=T0, lease_seconds=60)
    assert first is not None

    # 租约没到期时别人抢不走
    assert store().claim_pending_event(worker_id="w2", now=T0 + timedelta(seconds=10),
                                       lease_seconds=60) is None
    # 到期之后可以
    taken = store().claim_pending_event(worker_id="w2",
                                        now=T0 + timedelta(seconds=120),
                                        lease_seconds=60)
    assert taken is not None and taken.event_id == "e1"


def test_the_dead_worker_cannot_overwrite_the_new_owners_state(clean):
    """栅栏：原持有者醒过来时新持有者已经换了 token，它这一句更新到零行。

    没有栅栏的话，一个"诈尸"的 worker 会把一条别人正在处理的事件改回去。
    """
    store().enqueue_event(outbox())
    dead = store().claim_pending_event(worker_id="w1", now=T0, lease_seconds=60)
    alive = store().claim_pending_event(worker_id="w2",
                                        now=T0 + timedelta(seconds=120),
                                        lease_seconds=60)
    assert dead is not None and alive is not None
    assert dead.claim_token != alive.claim_token

    stale_write = store().record_wake_receipt(
        receipt=WakeReceipt(event_id="e1", attempt_id="att-dead", status="accepted",
                            received_at=T0 + timedelta(seconds=200)),
        next_state="delivered", claim_token=dead.claim_token,
    )
    assert stale_write is False           # 拿旧 token 推不动状态

    fresh_write = store().record_wake_receipt(
        receipt=WakeReceipt(event_id="e1", attempt_id="att-alive", status="accepted",
                            received_at=T0 + timedelta(seconds=200)),
        next_state="delivered", claim_token=alive.claim_token,
    )
    assert fresh_write is True


def test_a_crash_between_accept_and_receipt_does_not_lose_the_event(clean):
    """模拟：runtime 已经 accepted，回执还没写下来就断电。

    事件必须还在发件箱里，能被重新领走 —— 重复投递由 runtime 按
    event_id 幂等挡住（那是 wake conformance 的 W3），
    而这一层要保证的是**不丢**。
    """
    store().enqueue_event(outbox())
    store().claim_pending_event(worker_id="w1", now=T0, lease_seconds=60)
    # ← 这里断电，回执没写

    again = store().claim_pending_event(worker_id="w2",
                                        now=T0 + timedelta(seconds=120),
                                        lease_seconds=60)
    assert again is not None
    assert again.attempt_count == 2       # 第二次尝试，计数往前走了


def test_a_receipt_written_twice_does_not_double_advance(clean):
    store().enqueue_event(outbox())
    claimed = store().claim_pending_event(worker_id="w1", now=T0, lease_seconds=60)
    receipt = WakeReceipt(event_id="e1", attempt_id="att-1", status="accepted",
                          received_at=T0 + timedelta(seconds=5))
    s = store()
    assert s.record_wake_receipt(receipt=receipt, next_state="delivered",
                                 claim_token=claimed.claim_token) is True
    # 重放同一条回执：不该报错，也不该把状态再推一次
    s.record_wake_receipt(receipt=receipt, next_state="delivered",
                          claim_token=claimed.claim_token)
    rows = store()._q("SELECT count(*) FROM perceptkit_wake_receipt "
                      "WHERE event_id=%s", ("e1",))
    assert rows[0][0] == 1
