"""快照之外的那几条入口 —— **影子先前的覆盖窟窿**。

影子最早只挂在 `/v1/perception/report` 上，因为感知的大头在那儿。
但不是全部：照片走 `/photo/evaluate`，锁屏/解锁走设备事件，app 开关走两个
iOS 快捷指令，城市和 Wi-Fi 锚点是从 `location_signal` 里解出来的。
manifest 二十三个信号里有六个**只**出现在这些路上。

漏掉它们不像个窟窿，像「一致」：没比过，就不会不一致，报告干干净净，
而原因和 kit 算得对不对毫无关系。

这个文件盯三件事：

    每条入口真的产出观测        不然就是「悄悄没接」的翻版
    **精确信息一个字节都不过界**  坐标、BSSID 只在设备上,后端从来没有
    重传落在同一个 report_id    否则 kit 会把同一件事记两遍
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perceptkit.manifest.minimal import MINIMAL_SIGNALS  # noqa: E402

from perception.perceptkit_adapter import compare, events  # noqa: E402

AT = 1788166800.0          # 2026-08-31T09:00:00Z


def only(envelope):
    assert envelope is not None
    return envelope["observations"]


def signals(envelope):
    return {o["signal"] for o in only(envelope)}


def value(envelope, signal):
    return next(o["value"] for o in only(envelope) if o["signal"] == signal)


# --------------------------------------------------------------------------
# 覆盖面：这才是这批的目的
# --------------------------------------------------------------------------

def test_no_signal_is_left_unshadowed_any_more():
    """`NOT_SHADOWED` 必须是空的。

    非空就意味着有信号从来没被喂进 kit，而报告照样是干净的 ——
    「没比过」和「比过都一样」长得一模一样，这条就是不让它们再长得一样。
    """
    assert compare.NOT_SHADOWED == {}
    cov = compare.coverage(MINIMAL_SIGNALS)
    assert len(cov["signals_compared"]) + len(cov["signals_observed_only"]) \
        + len(cov["shape_differs"]) == cov["signals_total"]


def test_signals_without_a_live_counterpart_say_so():
    """照片/锁屏/解锁这三个 kit 收得到，但老路没有对应的状态字段可比 ——
    这和「压根没接」是两回事，必须能分开。"""
    for key in ("photo_library_added", "screen_change", "presence_recovery"):
        assert key in compare.NO_LIVE_COUNTERPART
    assert all(compare.NO_LIVE_COUNTERPART.values())


# --------------------------------------------------------------------------
# 隐私：这批里最重要的一条
# --------------------------------------------------------------------------

def test_precise_location_never_crosses_the_boundary():
    """iOS 在设备上把坐标解成城市、把 SSID 解成锚点，原件后端从来没有。

    manifest 把 `coordinate` 和 `raw_identifier` 标成 restricted、kit 会在
    写入边界丢掉它们 —— 但**靠那道闸等于把篱笆修在里侧**。这里根本不填。
    """
    payload = {
        "locality": "上海", "country": "CN", "wifi_label": "家里",
        "wifi_anchor_id": "a1b2c3d4e5f6a7b8",
        # 载荷里确实有这些精确字段（活路径解密后拿到的就是整包）
        "signal": {"latitude": 31.23, "longitude": 121.47},
        "placemark": {"iso_country_code": "CN", "thoroughfare": "某某路 123 号"},
        "bssid": "aa:bb:cc:dd:ee:ff",
    }
    env = events.location_envelope(payload, occurred_at=AT)
    assert signals(env) == {"location_city", "proximity_anchor"}
    city = value(env, "location_city")
    anchor = value(env, "proximity_anchor")
    assert city["coordinate"] is None if "coordinate" in city else True
    assert "coordinate" not in city or city["coordinate"] is None
    assert "raw_identifier" not in anchor or anchor["raw_identifier"] is None
    # 整个信封里不许出现任何精确值
    blob = repr(env)
    for leaked in ("31.23", "121.47", "aa:bb:cc:dd:ee:ff", "某某路"):
        assert leaked not in blob, f"精确信息泄漏了: {leaked}"


def test_photo_sends_only_the_count_and_the_time():
    """照片元数据里最能说明问题的部分（场景、人脸数、是不是截图）不往外送。

    manifest 没声明它们，送过去只会被当未声明字段丢掉 —— 而丢之前会先在
    日志里出现一行。
    """
    env = events.photo_envelope("ph_1", occurred_at=AT)
    assert value(env, "photo_library_added") == {
        "count": 1, "added_at": "2026-08-31T09:00:00+00:00"}


# --------------------------------------------------------------------------
# 各条入口
# --------------------------------------------------------------------------

def test_app_open_and_close_become_the_two_actions():
    o = events.app_event_envelope("Instagram", "social", action="open", occurred_at=AT)
    c = events.app_event_envelope("Instagram", "social", action="close", occurred_at=AT)
    assert value(o, "app_usage")["action"] == "open"
    assert value(c, "app_usage")["action"] == "close"
    # 快捷指令那条路上没有 bundle id，两边就是同一个字符串，不编造
    assert value(o, "app_usage")["app_id"] == value(o, "app_usage")["app_name"]


def test_unlock_after_absence_does_not_invent_a_duration():
    """iOS 报的是「一段离开结束了」，不是「离开了多久」。

    编一个时长出来，会让「我们不知道多久」看起来和「我们量到 40 分钟」
    一模一样。`absence_quality` 就是用来区分这两句话的。
    """
    env = events.device_event_envelope(
        {"event_id": "e1", "type": "unlock_after_absence", "payload": {}},
        occurred_at=AT)
    v = value(env, "presence_recovery")
    assert v["absence_seconds"] is None
    assert v["absence_quality"] == "estimated"

    env = events.device_event_envelope(
        {"event_id": "e2", "type": "unlock_after_absence",
         "payload": {"absence_seconds": 2400}}, occurred_at=AT)
    v = value(env, "presence_recovery")
    assert (v["absence_seconds"], v["absence_quality"]) == (2400.0, "measured")


def test_screen_change_carries_the_flag_not_the_screen():
    """manifest 记的是「屏幕变了没有」，不是屏幕上是什么。
    感知哈希是从用户屏幕算出来的，它留在老路上。"""
    env = events.device_event_envelope(
        {"event_id": "e3", "payload": {"screen_phash": "ff00ff00",
                                       "broadcast_state": "on"}},
        occurred_at=AT)
    assert value(env, "screen_change") == {"changed": True}
    assert "ff00ff00" not in repr(env)


def test_a_device_event_the_manifest_does_not_model_produces_nothing():
    """端点收的事件种类比 manifest 建模的多。产出一个空信封会白白占一条
    上报回执。"""
    assert events.device_event_envelope(
        {"event_id": "e4", "type": "battery_low", "payload": {}},
        occurred_at=AT) is None


def test_location_without_any_coarse_label_produces_nothing():
    assert events.location_envelope({}, occurred_at=AT) is None


# --------------------------------------------------------------------------
# 重传
# --------------------------------------------------------------------------

def test_the_same_event_replayed_lands_on_the_same_report_id():
    """重传是正常的（网络抖动、App 被挂起）。report_id 里掺进时钟或随机数，
    每次重传都会变成一条新上报，幂等就不成立了。"""
    a = events.photo_envelope("ph_1", occurred_at=AT)
    b = events.photo_envelope("ph_1", occurred_at=AT)
    assert a["report_id"] == b["report_id"]
    assert events.photo_envelope("ph_2", occurred_at=AT)["report_id"] != a["report_id"]

    x = events.app_event_envelope("Slack", None, action="open", occurred_at=AT)
    y = events.app_event_envelope("Slack", None, action="open", occurred_at=AT)
    assert x["report_id"] == y["report_id"]
    # 开和关不能撞成同一条
    z = events.app_event_envelope("Slack", None, action="close", occurred_at=AT)
    assert z["report_id"] != x["report_id"]


# --------------------------------------------------------------------------
# 日历 / 提醒：走来源镜像，不走信号路
# --------------------------------------------------------------------------

def test_calendar_rows_key_on_the_source_id():
    """一个事件被挪晚一小时，还是同一个事件 —— 时间线表达不了这件事，
    镜像靠上游自己的 id 才能用修订替换掉旧行，而不是追加第二份真相。"""
    rows = events.calendar_rows({"events": [
        {"event_id": "ev1", "title": "站会", "start_time": "2026-08-31T10:00:00+08:00",
         "revision": "r2"},
        {"title": "没有 id 的"},          # 分不清是修订还是新事件 → 丢掉
    ]})
    assert [r["source_event_id"] for r in rows] == ["ev1"]
    assert rows[0]["source_revision"] == "r2"


def test_reminder_rows_key_on_the_source_id():
    rows = events.reminder_rows({"reminders": [
        {"reminder_id": "rm1", "title": "买牛奶", "is_completed": False},
        {"title": "没有 id 的"},
    ]})
    assert [r["source_reminder_id"] for r in rows] == ["rm1"]


def test_a_single_next_event_payload_is_accepted_too():
    """老契约里 calendar 只发「下一个事件」，不是列表。"""
    rows = events.calendar_rows(
        {"calendar_next_event": {"event_id": "ev9", "title": "牙医"}})
    assert [r["source_event_id"] for r in rows] == ["ev9"]
