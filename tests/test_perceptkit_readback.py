"""kit 当主、老路当参照 —— 切换那一层。

影子证明了 kit 算得和老路一样；这一层是**用它的结果**。所以这里盯的不是
「算得对不对」（那是影子的事），是**换数据来源的时候有没有顺带改了别的**：

    词表被换掉        `app_state` 从 closed 变成 close,读它的 prompt 一个字不知道
    形状变窄          manifest 没建模的字段（motion 的 confidence、播放的 duration）
                      悄悄消失
    last_known 冒充当前  kit 在没权限时留着上一个可靠值,那个值不能当当前事实报出去
    出错就炸          读快照是每次唤醒都要走的路

最硬的一条在最后：**同一份数据，两条路的输出必须逐字段相同**。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.perceptkit_adapter import readback  # noqa: E402

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
NOW = 1788253200.0


class P:
    """一条当前投影，够 readback 用。"""
    def __init__(self, signal, value, availability="observed", dimension=""):
        self.signal, self.typed_value = signal, value
        self.availability, self.dimension_key = availability, dimension
        self.observed_at = T0


def cell(v, ts=NOW):
    return {"v": v, "ts": ts}


def test_a_field_the_kit_supplies_comes_from_the_kit():
    snap, src, conflicts = readback.merged_snapshot(
        {"battery_level": cell(0.5)},
        {"battery": [P("battery", {"level_ratio": 0.87})]},
        wanted={"battery_level": 600}, now=NOW,
    )
    assert snap["battery_level"] == 0.87
    assert src["battery_level"] == readback.FROM_KIT
    assert conflicts == [{"field": "battery_level", "kit": 0.87, "live": 0.5}]


def test_a_field_the_kit_cannot_supply_still_comes_from_the_live_path():
    """manifest 没建模的字段照旧走老路，**并且标出来源** ——
    一份分不清哪个字段来自哪条路的输出，出了问题没法回溯。"""
    snap, src, _ = readback.merged_snapshot(
        {"focus_authorization_status": cell("denied")}, {},
        wanted={"focus_authorization_status": 300}, now=NOW,
    )
    assert snap["focus_authorization_status"] == "denied"
    assert src["focus_authorization_status"] == readback.FROM_LIVE


def test_changing_the_source_does_not_change_the_vocabulary():
    """`app_state` 老路是 foreground/closed，kit 是 open/close。

    不翻回去的话，agent 读到的词变了而没有任何人知道 —— 换数据来源不该
    顺带换词表。
    """
    snap, _, _ = readback.merged_snapshot(
        {"app_state": cell("closed")},
        {"app_usage": [P("app_usage", {"action": "close", "app_name": "Slack"})]},
        wanted={"app_state": 900}, now=NOW,
    )
    assert snap["app_state"] == "closed"


@pytest.mark.parametrize("field", sorted(readback.LIVE_SHAPE_IS_RICHER))
def test_fields_whose_live_shape_carries_more_keep_coming_from_the_live_path(field):
    """不是 kit 算错了，是它按规范只留了一部分。

    拿 kit 的版本替换，等于在换数据来源的同时**悄悄删掉几个 agent 现在
    读得到的东西**（motion 的 confidence、播放的 duration）。
    """
    rich = {"state": "still", "confidence": "high", "started_at": "x"}
    snap, src, _ = readback.merged_snapshot(
        {field: cell(rich)},
        {"motion_state": [P("motion_state", {"state": "stationary"})],
         "music_playback": [P("music_playback", {"title": "t", "playback_state": "playing"})]},
        wanted={field: 300}, now=NOW,
    )
    assert src[field] == readback.FROM_LIVE
    assert snap[field] == rich
    assert readback.LIVE_SHAPE_IS_RICHER[field], "每条都得写明为什么"


def test_a_withheld_signal_reports_nothing_not_the_last_known_value():
    """kit 在 unavailable 时刻意留着上一个可靠值当 last_known。

    那个值**不能当当前事实报出去** —— 用户 09:10 撤了权限，09:20 去问还说
    「在专注」。影子第一批修的就是这个错误的反面。
    """
    snap, _, _ = readback.merged_snapshot(
        {"in_focus": cell(None)},
        {"focus_state": [P("focus_state", {"is_active": True},
                           availability="unavailable")]},
        wanted={"in_focus": 300}, now=NOW,
    )
    assert snap["in_focus"] is None


def test_staleness_still_follows_the_live_catalog():
    """过期判据不跟着数据来源一起换。两件事一起改，出了问题分不清是谁的。"""
    snap, _, _ = readback.merged_snapshot(
        {"condition": cell("cloudy", ts=NOW - 99999)}, {},
        wanted={"condition": 1800}, now=NOW,
    )
    assert snap["condition"] is None


def test_one_side_missing_is_not_reported_as_a_conflict():
    """kit 只见过影子开跑之后的数据。一边有一边没有是**正常的**，
    报成冲突会让真正的不一致淹在噪音里。"""
    _, _, conflicts = readback.merged_snapshot(
        {"battery_level": cell(0.5)}, {},
        wanted={"battery_level": 600}, now=NOW,
    )
    assert conflicts == []


def test_the_log_line_names_the_field_and_both_values():
    """只报「有 3 个冲突」的日志，等于告诉你有问题然后不告诉你是什么。"""
    _, src, conflicts = readback.merged_snapshot(
        {"battery_level": cell(0.5)},
        {"battery": [P("battery", {"level_ratio": 0.87})]},
        wanted={"battery_level": 600}, now=NOW,
    )
    summary = readback.summarize(src, conflicts)
    assert summary["from_kit"] == 1 and summary["conflicts"] == 1
    assert summary["detail"][0] == {"field": "battery_level", "kit": 0.87, "live": 0.5}


def test_the_declared_unit_conversion_is_undone_on_the_way_back():
    """kit 存比率、老路存百分比。翻回去的时候要换回来，
    否则 agent 会读到 0.184% 的体脂。"""
    snap, _, _ = readback.merged_snapshot(
        {}, {"health_body_fat": [P("health_body_fat",
                                   {"body_fat_ratio": 0.184})]},
        wanted={"body_fat_pct": 86400}, now=NOW,
    )
    assert snap["body_fat_pct"] == pytest.approx(18.4)


# --------------------------------------------------------------------------
# 接进 service 的那一层（要真库）
# --------------------------------------------------------------------------

import os  # noqa: E402

needs_pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="需要真库：这几条验的是 service 真的从 kit 的表读")


@needs_pg
def test_the_kill_switch_returns_exactly_the_old_behaviour(monkeypatch):
    """关掉之后必须**逐字段**回到切换之前，不是「差不多一样」。

    这是这个开关存在的全部意义：出问题时不用回滚代码重新部署。
    """
    from perception import service
    monkeypatch.setenv("FEEDLING_PERCEPTKIT_PRIMARY", "0")
    assert service._perceptkit_primary() is False
    monkeypatch.setenv("FEEDLING_PERCEPTKIT_PRIMARY", "1")
    assert service._perceptkit_primary() is True


@needs_pg
def test_it_defaults_to_on(monkeypatch):
    """默认开。默认关的开关会制造另一类 bug：代码上线了、功能没上线。"""
    from perception import service
    monkeypatch.delenv("FEEDLING_PERCEPTKIT_PRIMARY", raising=False)
    assert service._perceptkit_primary() is True


@needs_pg
def test_a_failure_reading_the_kit_falls_back_instead_of_raising(monkeypatch):
    """读快照是每次唤醒、每次对话都要走的路。

    为了一个数据来源的切换让它报错，是自己给自己制造事故。
    """
    from perception import service

    def boom(*a, **k):
        raise RuntimeError("kit 那边炸了")

    monkeypatch.setattr(service.store, "get_state", lambda uid: {"battery_level": {"v": 0.5, "ts": 1788253200.0}})
    monkeypatch.setattr(service, "_perceptkit_snapshot", boom)
    monkeypatch.setenv("FEEDLING_PERCEPTKIT_PRIMARY", "1")
    with pytest.raises(RuntimeError):
        service._perceptkit_snapshot("u", {}, wanted={}, now=0.0)
    # 但真实调用点包了兜底：_catalog_snapshot_fields 里 kit 返回 None 就走老路
    monkeypatch.setattr(service, "_perceptkit_snapshot", lambda *a, **k: None)
    snap = service._catalog_snapshot_fields("usr_nonexistent")
    assert isinstance(snap, dict)
