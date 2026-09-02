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


def test_anchors_are_deduped_per_anchor_not_globally():
    """从家到公司再回家，是两件事。按 anchor_id 各自去重。"""
    by_id = {d.definition_id: d for d in wake_rules.wake_definitions()}
    assert by_id["io.perception.anchor_changed"].dedupe_field == "anchor_id"


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
