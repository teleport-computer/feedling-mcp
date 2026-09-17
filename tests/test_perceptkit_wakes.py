"""kit 接管唤醒 —— 「什么时候主动找你」这半。

感知切过来之后，用户还差最后一件事没换：**什么时候被打扰**。这个文件盯的
就是那件事，重点全在「换实现的时候有没有顺带换行为」：

    同一件事叫两遍     老路和 kit 同时投递 —— 这是切换最容易出的事故
    该叫的没叫         kit 的规则漏了老路会叫的某种情况
    回执说谎           io 的免打扰把它挡下了,回执却记「投递成功」
    撞闸无限重试       把「不该叫」当成「没叫成功」,会一直重试到闸放行

最后一条最隐蔽：它不报错、不丢数据，只是**绕过了用户设的免打扰**。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.perceptkit_adapter import wake_rules  # noqa: E402
from perception.perceptkit_adapter.wake_port import FeedlingWakePort  # noqa: E402

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def event(event_type="photo_added", event_id="e1", definition="io.perception.photo_added"):
    from perceptkit.contracts.event import PerceptionEvent
    return PerceptionEvent(
        event_id=event_id, definition_id=definition, definition_version=1,
        subject_id="u1", type=event_type, signal="photo_library_added",
        occurred_at=T0, received_at=T0, condition="occurrence",
        field_name=None, previous=None, current=None, context={},
    )


# --------------------------------------------------------------------------
# 规则：照着老路抄，不是新设计
# --------------------------------------------------------------------------

def test_every_wake_the_live_path_fires_has_a_rule():
    """老路会叫醒的五种情况，kit 一个都不能少。

    少一种 = 用户从此收不到那类提醒，而且**不会报错** —— 只是再也没响过。
    """
    from perception import differ_v2
    live_triggers = {
        "arrived_at_anchor", "unlock_after_absence",
        "broadcast_opened", "broadcast_closed", "scene_change", "photo_added",
    }
    kit_triggers = {d.event_type for d in wake_rules.wake_definitions()}
    assert live_triggers == kit_triggers
    # 老路真的还认得这些信号（防止它那边改了名而这里没跟上）
    assert "photo_added" in differ_v2._DURABLE_WAKE_SIGNALS


def test_the_debounces_are_the_ones_the_live_path_used():
    """防抖直接决定用户被打扰的频率。从老路的目录抄，不重新拍。"""
    by_id = {d.definition_id: d for d in wake_rules.wake_definitions()}
    assert by_id["io.perception.anchor_changed"].lifecycle.cooldown_seconds == 60.0
    assert by_id["io.perception.broadcast_opened"].lifecycle.cooldown_seconds == 60.0
    # 照片不防抖：连拍成簇在照片入口那层按 30 秒去过重了，
    # 这里再来一道会把两次真实拍照吃掉一次。
    assert by_id["io.perception.photo_added"].lifecycle.cooldown_seconds == 0.0


def test_the_anchor_rule_only_counts_connected_reports():
    """前置条件在规则上，不在调用方那里 —— 所以钉在定义本身。

    版本号跟着加一：加前置条件是行为变了，状态键带版本，新版本从干净状态开始。
    """
    by_id = {d.definition_id: d for d in wake_rules.wake_definitions()}
    anchor = by_id["io.perception.anchor_changed"]
    assert dict(anchor.when) == {"is_connected": True}
    assert anchor.version == 2
    # 其余规则没有前置条件
    assert all(not d.when for d in by_id.values() if d is not anchor)


# --------------------------------------------------------------------------
# 真实唤醒次数：整条 kit 管线 + 真 Postgres 发件箱 + io 的 WakePort
#
# 上面那些只看定义字段。perceptkit 0.5.0 的定义字段完全正确，
# 用户照样「周二起到公司不再被叫醒」—— 缺陷在事件 id 和发件箱去重合起来的
# 地方，只有数真实唤醒才看得见。
# --------------------------------------------------------------------------

import os  # noqa: E402
from datetime import timedelta  # noqa: E402

_PG = os.environ.get("PERCEPTKIT_TEST_PG")
_needs_pg = pytest.mark.skipif(
    not _PG, reason="没有 PERCEPTKIT_TEST_PG，跳过数真实唤醒的几条（发件箱去重只有真库验得出）")


@pytest.fixture
def kit_world(monkeypatch):
    """io 真实的 kit 装配（shadow._kit），只把最后一跳的排队换成记录。"""
    psycopg = pytest.importorskip("psycopg")
    from perception.perceptkit_adapter import schema, shadow
    from perception.perceptkit_adapter import wake_port as _wake_port
    from perception.perceptkit_adapter.storage import PostgresStorage

    with psycopg.connect(_PG, autocommit=True) as c:
        c.execute(schema.DDL)
        c.execute(schema.TRUNCATE)

    delivered: list = []
    real_port = _wake_port.FeedlingWakePort
    monkeypatch.setattr(
        _wake_port, "FeedlingWakePort",
        lambda: real_port(submit=lambda ev: delivered.append(ev) or True))
    monkeypatch.setattr(shadow, "wakes_enabled", lambda: True)

    conn = psycopg.connect(_PG, autocommit=True)
    storage = PostgresStorage(conn)
    kit = shadow._kit(storage)

    def ingest(envelope, at):
        from perceptkit.contracts import IngestContext
        return kit.ingest(envelope, context=IngestContext("u1", at), dispatch=True)

    def outbox(event_type):
        return storage.list_events(subject_id="u1", event_type=event_type, limit=100)

    try:
        yield type("World", (), {"ingest": staticmethod(ingest),
                                 "outbox": staticmethod(outbox),
                                 "delivered": delivered})
    finally:
        conn.close()


def _anchor(anchor_id, at):
    """io 自己的 producer 产出的锚点上报（和 /location 入口同一份代码）。"""
    from perception.perceptkit_adapter.events import location_envelope
    return location_envelope({"wifi_anchor_id": anchor_id, "wifi_label": anchor_id},
                             occurred_at=at, timezone_id="Asia/Shanghai")


def _broadcast(active, at):
    """io 自己的 iOS 快照转换产出的屏幕采集上报。"""
    from perception.perceptkit_adapter.ios_report import to_envelope
    return to_envelope({"context_snapshot": [{"key": "broadcast", "data": {"active": active}}],
                        "client_ts": at.isoformat()},
                       occurred_at=at.isoformat())


def _wakes(world, trigger):
    return [e for e in world.delivered if e.trigger == trigger]


@_needs_pg
def test_the_same_commute_wakes_every_day_not_only_the_first(kit_world):
    """之前：周一 家→公司 叫醒，周二起 家→公司 永远不再叫（同一个 id 被发件箱当重复吞掉）。

    A→B→A→B 跨天：第一条是基线不算，后面三次真实到达各叫一次。
    """
    trips = [("home", T0), ("office", T0 + timedelta(hours=1)),
             ("home", T0 + timedelta(hours=10)),
             ("office", T0 + timedelta(days=1, hours=1))]
    for where, at in trips:
        kit_world.ingest(_anchor(where, at), at)

    assert len(kit_world.outbox("arrived_at_anchor")) == 3
    assert len(_wakes(kit_world, "arrived_at_anchor")) == 3


@_needs_pg
def test_a_resent_report_does_not_wake_again(kit_world):
    """修了「第二次跳变被吞」，不能反过来让客户端重传也叫一次。"""
    home, office = T0, T0 + timedelta(hours=1)
    kit_world.ingest(_anchor("home", home), home)
    kit_world.ingest(_anchor("office", office), office)
    kit_world.ingest(_anchor("office", office), office + timedelta(minutes=2))

    assert len(kit_world.outbox("arrived_at_anchor")) == 1
    assert len(_wakes(kit_world, "arrived_at_anchor")) == 1


@_needs_pg
def test_every_broadcast_toggle_wakes_once_the_cooldown_has_passed(kit_world):
    """之前：录屏只在第一次开、第一次关时叫醒。

    关→开→关→开→关→开，间隔都超过 60 秒冷却：3 次开 + 2 次关。
    """
    for i, active in enumerate([False, True, False, True, False, True]):
        at = T0 + timedelta(minutes=5 * i)
        kit_world.ingest(_broadcast(active, at), at)

    assert len(kit_world.outbox("broadcast_opened")) == 3
    assert len(kit_world.outbox("broadcast_closed")) == 2
    assert len(_wakes(kit_world, "broadcast_opened")) == 3
    assert len(_wakes(kit_world, "broadcast_closed")) == 2


@_needs_pg
def test_a_late_disconnect_neither_wakes_nor_swallows_the_real_arrival(kit_world):
    """迟到的「家里 Wi-Fi 断开」既不能讲成「到家了」，也不能把前值推成 home。

    家(连)→公司(连)→家(断开，迟到)→没有到达→家(连，稍后)→一次到达。
    推成 home 的话，真正到家那一次会被当成「没变」吞掉。

    io 的 producer 目前只在连着时才发锚点（is_connected 写死 True），所以
    断开这条手工构造 —— 前置条件防的是 producer 以后开始发断开状态。
    """
    from perception.perceptkit_adapter.events import location_envelope

    t_home, t_office = T0, T0 + timedelta(hours=1)
    t_late, t_back = T0 + timedelta(hours=9), T0 + timedelta(hours=10)
    kit_world.ingest(_anchor("home", t_home), t_home)
    kit_world.ingest(_anchor("office", t_office), t_office)
    assert len(_wakes(kit_world, "arrived_at_anchor")) == 1

    late = location_envelope({"wifi_anchor_id": "home"}, occurred_at=t_late)
    late["report_id"] = "late-disconnect"
    for o in late["observations"]:
        if o["signal"] == "proximity_anchor":
            o["value"]["is_connected"] = False
    kit_world.ingest(late, t_late)
    assert len(_wakes(kit_world, "arrived_at_anchor")) == 1, "断开被讲成了到达"

    kit_world.ingest(_anchor("home", t_back), t_back)
    assert len(kit_world.outbox("arrived_at_anchor")) == 2
    assert len(_wakes(kit_world, "arrived_at_anchor")) == 2, "真正到家那一次被吞了"


# --------------------------------------------------------------------------
# 投递
# --------------------------------------------------------------------------

def test_a_wake_that_is_delivered_is_reported_accepted():
    got = []
    port = FeedlingWakePort(submit=lambda ev: got.append(ev) or True)
    receipt = port.wake(event(), None)
    assert receipt.status == "accepted"
    assert got[0].trigger == "photo_added"
    assert got[0].user_id == "u1"


def test_the_host_gate_blocking_it_is_not_a_delivery_failure():
    """免打扰时段里不叫人，是**正常应答**，不是投递失败。

    记成失败的话，投递会一直重试到闸放行为止 —— 那正好绕过了用户设的闸。
    契约里表达这件事的词是 `conversation_suppressed`。
    """
    port = FeedlingWakePort(submit=lambda ev: False)
    receipt = port.wake(event(), None)
    assert receipt.status == "conversation_suppressed"
    assert receipt.reason == "host_gate"

    from perceptkit.contracts import receipt as _r
    assert receipt.status in _r.WAKE_STATUSES


def test_the_same_event_delivered_twice_only_reaches_the_queue_once():
    """崩溃重投是常态：投出去了、回执没落库，进程挂了，重启一定再投一次。"""
    got = []
    port = FeedlingWakePort(submit=lambda ev: got.append(ev) or True)
    first = port.wake(event(event_id="same"), None)
    second = port.wake(event(event_id="same"), None)
    assert (first.status, second.status) == ("accepted", "duplicate")
    assert len(got) == 1


def test_a_real_failure_is_raised_not_reported_as_a_refusal():
    """连不上、序列化炸了 —— 那些要重试。

    把它们说成「用户设了静音」，会让一次网络抖动看起来像用户的选择。
    """
    def boom(ev):
        raise RuntimeError("队列连不上")
    port = FeedlingWakePort(submit=boom)
    with pytest.raises(RuntimeError):
        port.wake(event(), None)


def test_screen_events_keep_the_source_the_throttles_are_configured_on():
    """下游的频率闸是**按 source 配的**。换个 source 就等于换了一套闸。"""
    got = []
    port = FeedlingWakePort(submit=lambda ev: got.append(ev) or True)
    port.wake(event(event_type="scene_change", event_id="s1"), None)
    port.wake(event(event_type="photo_added", event_id="p1"), None)
    assert [e.source for e in got] == ["scene_change", "perception_event"]


# --------------------------------------------------------------------------
# 两条路不能同时投
# --------------------------------------------------------------------------

def test_the_live_path_stops_delivering_when_the_kit_owns_wakes(monkeypatch):
    """这是切换最容易出的事故：同一次「回到家」排两条 job，用户被提醒两遍。"""
    from perception import service
    monkeypatch.setenv("FEEDLING_PERCEPTKIT_WAKES", "1")
    assert service._perceptkit_owns_wakes() is True

    delivered = []
    monkeypatch.setattr(service, "_settings_v2_for_user",
                        lambda uid: delivered.append("reached") or None)
    service._submit_wake_event_v2_compat(
        type("E", (), {"trigger": "photo_added", "user_id": "u1"})())
    assert delivered == [], "kit 接管时老路不该再投"


def test_turning_the_kit_off_gives_the_live_path_back(monkeypatch):
    """回滚闸：一个环境变量退回切换之前。"""
    from perception import service
    monkeypatch.setenv("FEEDLING_PERCEPTKIT_WAKES", "0")
    assert service._perceptkit_owns_wakes() is False


# --------------------------------------------------------------------------
# 唤醒 job 的时间戳 = 这一次投进 io 队列的时刻（2026-09-17）
#
# 照片带上拍摄时间以后，事件的 occurred_at 可以是几小时前；发件箱重投时，
# 事件的 received_at 是第一次收到的时刻。job 时间戳要是跟着其中任何一个走：
# V1 consumer 只读 ts > 游标 的 job → 这条落在游标后面被永远跳过；
# V2 兼容投递按它算能力防抖 → now - last 变负数。
# --------------------------------------------------------------------------

CAPTURED = T0 - timedelta(hours=3)


def _late_photo_event():
    """拍摄于 T0-3h、第一次收到于 T0（过去）、现在才投递 —— 即一次延迟重投。"""
    from perceptkit.contracts.event import PerceptionEvent
    return PerceptionEvent(
        event_id="late", definition_id="io.perception.photo_added", definition_version=1,
        subject_id="u1", type="photo_added", signal="photo_library_added",
        occurred_at=CAPTURED, received_at=T0, condition="occurrence",
        field_name=None, previous=None, current=None, context={},
    )


def _between_now(fn):
    import time
    before = time.time()
    out = fn()
    return before, out, time.time()


def test_the_wake_is_stamped_with_the_delivery_time():
    got = []
    before, _, after = _between_now(lambda: FeedlingWakePort(
        submit=lambda ev: got.append(ev) or True).wake(_late_photo_event(), None))
    assert before <= got[0].created_at <= after
    assert got[0].created_at not in (CAPTURED.timestamp(), T0.timestamp())


def _drive_real_enqueue(monkeypatch, runtime_mode):
    """kit 事件 → io WakePort → service._fire_wake_event_v2 → 真实的 job 形状。

    只把最底下的存储换成记录，中间的字段传递全是生产代码。
    """
    import types
    from core import store as core_store
    from core import wake_bus as core_wake_bus
    from hosted import config_store as hosted_config_store
    from model_api_runtime.v2 import jobs_store
    from perception import service

    legacy_jobs, v2_jobs = [], []
    user_store = types.SimpleNamespace(
        proactive_activation_ready=lambda: True,
        append_proactive_job=lambda job: legacy_jobs.append(job),
    )
    monkeypatch.setattr(core_store, "get_store", lambda _uid: user_store)
    monkeypatch.setattr(core_store, "get_store_per_load_mode",
                        lambda _uid, **_kw: user_store)
    monkeypatch.setattr(hosted_config_store, "get_hosted_runtime_mode_strict",
                        lambda _store: runtime_mode)
    monkeypatch.setattr(jobs_store, "enqueue_job_with_context_log",
                        lambda uid, lane, **kw: (v2_jobs.append(kw), (1, False))[1])
    monkeypatch.setattr(core_wake_bus, "notify", lambda *_a: None)
    monkeypatch.setattr(service.store, "trim_v2_wake_context", lambda uid: None)

    port = FeedlingWakePort(
        submit=lambda ev: service._fire_wake_event_v2(ev) or True)
    before, _, after = _between_now(lambda: port.wake(_late_photo_event(), None))
    return legacy_jobs, v2_jobs, before, after


def test_v1_job_is_visible_past_a_cursor_that_moved_on_after_first_receipt(monkeypatch):
    """V1 consumer 只读 ts > 游标。事件 T0 第一次收到、投递失败；游标被别的
    job 推到 T0 之后；现在重投 —— job 必须排在游标后面，不然永远读不到。"""
    from hosted import config_store as hosted_config_store
    cursor = T0.timestamp() + 60
    legacy, v2, before, after = _drive_real_enqueue(
        monkeypatch, hosted_config_store.HOSTED_RUNTIME_MODE_RESIDENT)
    assert v2 == []
    assert len(legacy) == 1
    assert before <= legacy[0]["ts"] <= after
    assert legacy[0]["ts"] > cursor


def test_v2_job_timestamp_is_the_delivery_time(monkeypatch):
    from hosted import config_store as hosted_config_store
    legacy, v2, before, after = _drive_real_enqueue(
        monkeypatch, hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2)
    assert legacy == []
    assert len(v2) == 1
    assert before <= v2[0]["context_ts"] <= after
    assert v2[0]["context_doc"]["created_at"] == v2[0]["context_ts"]


@_needs_pg
def test_a_late_photo_keeps_its_capture_time_in_the_outbox_but_queues_at_delivery(kit_world):
    from perception.perceptkit_adapter.events import photo_envelope
    before, _, after = _between_now(lambda: kit_world.ingest(
        photo_envelope("late-1", occurred_at=CAPTURED, timezone_id="Asia/Shanghai"), T0))
    rows = kit_world.outbox("photo_added")
    assert len(rows) == 1
    assert rows[0].occurred_at == CAPTURED
    wakes = _wakes(kit_world, "photo_added")
    assert len(wakes) == 1
    assert before <= wakes[0].created_at <= after


@_needs_pg
def test_carrying_the_absence_duration_does_not_change_how_often_it_wakes(kit_world):
    """修 bug A 只改观测里的值，不改唤醒次数：每次回来仍然叫一次、重传不叫。"""
    from perception.perceptkit_adapter.events import device_event_envelope
    from proactive import service as proactive_service

    def unlock(event_id, at, **payload):
        stored = proactive_service._make_device_event(
            "ios", "unlock_after_absence", {"wake_trigger": "unlock_after_absence", **payload})
        stored["event_id"] = event_id
        return device_event_envelope(stored, occurred_at=at)

    kit_world.ingest(unlock("u-old", T0), T0)
    t1 = T0 + timedelta(hours=2)
    with_fields = unlock("u-new", t1, idle_sec=2400, presence_evidence="app_became_active")
    kit_world.ingest(with_fields, t1)
    kit_world.ingest(with_fields, t1 + timedelta(seconds=5))      # 重传

    assert len(kit_world.outbox("unlock_after_absence")) == 2
    assert len(_wakes(kit_world, "unlock_after_absence")) == 2
