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
    CalendarEventMirror,
    CurrentProjection,
    EventOutboxEntry,
    ReminderItemMirror,
    StoredObservation,
)
from perceptkit.contracts.receipt import WakeReceipt  # noqa: E402

from perceptkit.processing.source_sync import DeletedItem  # noqa: E402
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


# ---------------------------------------------------------------------------
# 整套 conformance，跑在真 adapter 上
# ---------------------------------------------------------------------------

def test_the_real_adapter_passes_the_whole_conformance_suite(clean):
    """**这条本该一开始就有。**

    包里那套 conformance 存在的全部意义，就是让宿主自己写的 adapter
    对着它跑一遍。io 写了一个真 Postgres 实现，却从来没有一条测试真的跑过
    这套东西 —— 于是「提醒镜像整条不通」（写读都抛，因为读了
    `ReminderItemMirror` 上根本不存在的字段）在库里躺着，而这个文件全绿。

    ⑤ 那条内存实现证不出来（内存天然原子），但**这里跑的就是真库**，
    所以不跳过：真库上它是有意义的。
    """
    from perceptkit.conformance import run_storage_conformance

    def factory():
        with connect() as c:
            c.execute(schema.TRUNCATE)
        return PostgresStorage(connect())

    problems = run_storage_conformance(factory)
    assert problems == [], "真 adapter 没过 conformance：\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# 镜像查询的 SQL 边界 —— 内存实现碰不到的那几种
#
# conformance 第 ⑫ 条验的是"下推有没有生效"。这里补的是 **io 自己那份 SQL**：
# JSONB 里的时间戳不是合法日期时会不会把整条查询炸掉、时间未知的条目会不会
# 被窗口悄悄藏起来。两个都是"一条坏数据让整个用户看不到日历"，不是少一条。
# ---------------------------------------------------------------------------

def _mirror_fixtures(s, T):
    s.upsert_calendar_events(subject_id="u", events=[
        CalendarEventMirror(
            subject_id="u", source="ios", source_account_id="a", source_calendar_id="c",
            source_event_id="good",
            event_fields={"title": "站会", "start_at": T.isoformat()}),
        # 时间字段里是垃圾 —— 直接 ::timestamptz 会让**整条查询**抛异常。
        CalendarEventMirror(
            subject_id="u", source="ios", source_account_id="a", source_calendar_id="c",
            source_event_id="garbage",
            event_fields={"title": "坏数据", "start_at": "不是时间"}),
        # 压根没有时间 —— 按纪律必须保留，不能被窗口藏起来。
        CalendarEventMirror(
            subject_id="u", source="ios", source_account_id="a", source_calendar_id="c",
            source_event_id="notime", event_fields={"title": "没写时间"}),
    ])


def test_one_unparseable_timestamp_does_not_take_the_whole_query_down(clean):
    s = store()
    T = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    _mirror_fixtures(s, T)
    got = {e.source_event_id for e in s.list_calendar_events(
        subject_id="u", start=T - timedelta(days=1), end=T + timedelta(days=1),
        limit=50)}
    assert got == {"good", "garbage", "notime"}


def test_items_with_no_known_time_are_not_hidden_by_the_window(clean):
    """证明不了它在范围外，就不能替用户把它藏起来 —— 和删除那边同一条纪律。"""
    s = store()
    T = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    _mirror_fixtures(s, T)
    outside = {e.source_event_id for e in s.list_calendar_events(
        subject_id="u", start=T + timedelta(days=30), end=T + timedelta(days=31),
        limit=50)}
    assert outside == {"garbage", "notime"}, "时间明确且在窗外的才该被排掉"


def test_a_reminder_without_is_completed_counts_as_open(clean):
    """缺字段不等于已完成。SQL 里少个 COALESCE，这条提醒就整条消失。"""
    s = store()
    s.upsert_reminders(subject_id="u", items=[
        ReminderItemMirror(subject_id="u", source="ios", source_account_id="a",
                           source_list_id="l", source_reminder_id="done",
                           reminder_fields={"title": "完成的", "is_completed": True}),
        ReminderItemMirror(subject_id="u", source="ios", source_account_id="a",
                           source_list_id="l", source_reminder_id="open",
                           reminder_fields={"title": "没完成", "is_completed": False}),
        ReminderItemMirror(subject_id="u", source="ios", source_account_id="a",
                           source_list_id="l", source_reminder_id="unset",
                           reminder_fields={"title": "没这个字段"}),
    ])
    assert {r.source_reminder_id for r in s.list_reminders(
        subject_id="u", limit=50)} == {"open", "unset"}
    assert {r.source_reminder_id for r in s.list_reminders(
        subject_id="u", include_completed=True, limit=50)} == {
            "open", "unset", "done"}


# ---------------------------------------------------------------------------
# 来源隔离 —— 审查者在 kit 上复现的那个场景，这里用真 Postgres 再跑一遍
#
# 内存实现验不出这一条会不会真的落到 SQL 的 WHERE 里。而它的失败长这样：
# 用户发现自己另一个日历账户的日程凭空消失了，不可逆，且没有任何地方报错。
# ---------------------------------------------------------------------------

def _cal(source: str, eid: str, *, sync: str, at=None):
    return CalendarEventMirror(
        subject_id="u1", source=source, source_account_id=f"{source}-acct",
        source_calendar_id="c1", source_event_id=eid,
        event_fields={"title": f"{source}/{eid}", "start_at": at or T0},
        last_seen_sync_id=sync,
    )


def test_a_full_sync_for_one_source_does_not_delete_another_source(clean):
    s = store()
    s.upsert_calendar_events(subject_id="u1", events=[
        _cal("google", "g1", sync="google-round-1"),
        _cal("ios", "i1", sync="ios-round-0"),
    ])
    # ios 的一轮全量：范围盖住两条，但只有 ios 那条属于它。
    n = s.apply_source_snapshot(
        subject_id="u1", source="ios", collection_kind="calendar",
        sync_id="ios-round-1", coverage_start=T0 - timedelta(days=1),
        coverage_end=T0 + timedelta(days=1), snapshot_kind="full")
    left = {(e.source, e.source_event_id)
            for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert ("google", "g1") in left, "ios 的全量同步删掉了 Google 的日程"
    assert ("ios", "i1") not in left and n == 1


def test_two_sources_sharing_an_event_id_do_not_overwrite_each_other(clean):
    s = store()
    s.upsert_calendar_events(subject_id="u1", events=[
        _cal("ios", "同一个 id", sync="a"),
        _cal("google", "同一个 id", sync="a"),
    ])
    titles = {e.event_fields["title"]
              for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert titles == {"ios/同一个 id", "google/同一个 id"}, titles


def test_an_explicit_tombstone_deletes_only_that_source(clean):
    """增量同步执行来源明确的删除；它不能越界到另一个来源。"""
    s = store()
    s.upsert_calendar_events(subject_id="u1", events=[
        _cal("ios", "同一个 id", sync="a"),
        _cal("google", "同一个 id", sync="a"),
    ])
    n = s.delete_source_items(subject_id="u1", source="ios",
                              collection_kind="calendar",
                              deleted_items=[
                                  DeletedItem("ios-acct", "c1", "同一个 id")])
    left = {e.source for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert n == 1 and left == {"google"}, f"删越界了，剩下 {left}"


def test_a_tombstone_stays_inside_its_own_account_and_calendar(clean):
    """范围是五段。真实存储要自己写 SQL，**少一个 AND 就是删过头**，
    而这在内存实现上很容易"看起来对" —— 只放一条数据是发现不了的。
    """
    s = store()
    s.upsert_calendar_events(subject_id="u1", events=[
        CalendarEventMirror(
            subject_id="u1", source="ios", source_account_id=acct,
            source_calendar_id=cal, source_event_id="撞 id",
            event_fields={"title": f"{acct}/{cal}", "start_at": T0},
            last_seen_sync_id="a")
        for acct, cal in (("工作", "日历1"), ("工作", "日历2"), ("私人", "日历1"))
    ])
    n = s.delete_source_items(
        subject_id="u1", source="ios", collection_kind="calendar",
        deleted_items=[DeletedItem("工作", "日历1", "撞 id")])
    left = {(e.source_account_id, e.source_calendar_id)
            for e in s.list_calendar_events(subject_id="u1", limit=50)}
    assert n == 1, f"该删 1 条，删了 {n} 条"
    assert left == {("工作", "日历2"), ("私人", "日历1")}, f"剩下 {left}"


def test_a_tombstone_for_an_unknown_id_deletes_nothing(clean):
    s = store()
    s.upsert_calendar_events(subject_id="u1", events=[_cal("ios", "i1", sync="a")])
    assert s.delete_source_items(subject_id="u1", source="ios",
                                 collection_kind="calendar",
                                 deleted_items=[DeletedItem("ios-acct", "c1", "从来没有过的 id")]) == 0
    assert len(s.list_calendar_events(subject_id="u1", limit=50)) == 1


def test_a_tombstone_stays_inside_one_subject(clean):
    """跨用户的一条 DELETE 少写一个 WHERE 就会删掉别人的数据。"""
    s = store()
    for who in ("u1", "u2"):
        s.upsert_calendar_events(subject_id=who, events=[CalendarEventMirror(
            subject_id=who, source="ios", source_account_id="a",
            source_calendar_id="c", source_event_id="同一个 id",
            event_fields={"start_at": T0})])
    s.delete_source_items(subject_id="u1", source="ios",
                          collection_kind="calendar",
                          deleted_items=[DeletedItem("ios-acct", "c1", "同一个 id")])
    assert len(s.list_calendar_events(subject_id="u2", limit=10)) == 1


# ---------------------------------------------------------------------------
# 同步状态的往返
#
# 这一类 bug 在这个适配器上出现过两次，都是同一个形状：**写的时候用了记录上
# 不存在的字段名，而没有任何调用方，所以一直没暴露**。
#   · 提醒镜像读 source_created_at —— CalendarEventMirror 有、ReminderItemMirror 没有
#   · 同步状态读 last_sync_id —— 记录上叫 sync_cursor
# 两次都是"整条路从来没通过，而测试全绿"。往返测试是唯一能拦住它的东西。
# ---------------------------------------------------------------------------

def test_sync_state_round_trips_every_field(clean):
    from perceptkit.contracts.records import SourceSyncState
    s = store()
    want = SourceSyncState(
        subject_id="u1", source="ios", collection_kind="calendar",
        sync_cursor="page-7", coverage_start=T0 - timedelta(days=1),
        coverage_end=T0 + timedelta(days=1), snapshot_kind="full",
        last_attempted_at=T0, last_successful_sync_at=T0,
        last_error_code=None,
    )
    s.put_sync_state(want)
    got = s.get_sync_state(subject_id="u1", source="ios",
                           collection_kind="calendar")
    assert got == want, f"往返之后变了：{got}"


def test_a_failed_sync_is_distinguishable_from_one_that_never_ran(clean):
    """没有 last_error_code / last_attempted_at 这两列的话，
    「日历同步挂了三天」这个问题根本答不出来 —— 失败和从没跑过长得一样。"""
    from perceptkit.contracts.records import SourceSyncState
    s = store()
    s.put_sync_state(SourceSyncState(
        subject_id="u1", source="ios", collection_kind="calendar",
        sync_cursor="page-7", last_attempted_at=T0 + timedelta(days=3),
        last_successful_sync_at=T0, last_error_code="auth_revoked",
    ))
    got = s.get_sync_state(subject_id="u1", source="ios",
                           collection_kind="calendar")
    assert got.last_error_code == "auth_revoked"
    assert got.last_attempted_at > got.last_successful_sync_at
    assert got.sync_cursor == "page-7", "失败不该动游标"


def test_sync_state_is_scoped_by_source_and_collection(clean):
    from perceptkit.contracts.records import SourceSyncState
    s = store()
    for src in ("ios", "google"):
        for kind in ("calendar", "reminders"):
            s.put_sync_state(SourceSyncState(
                subject_id="u1", source=src, collection_kind=kind,
                sync_cursor=f"{src}-{kind}"))
    for src in ("ios", "google"):
        for kind in ("calendar", "reminders"):
            got = s.get_sync_state(subject_id="u1", source=src,
                                   collection_kind=kind)
            assert got.sync_cursor == f"{src}-{kind}"
