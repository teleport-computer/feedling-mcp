"""iOS 上报 -> kit 信封的翻译层 —— **影子第一次比对真数据时炸出来的那一批**。

这个文件里的每一条，都对应一个「上线了、用户没得到、也没有任何报错」的洞。
它们能藏这么久，是因为翻译层的错分两种，而只有第一种会自己叫：

    名字对不上   每条上报都失败,一眼看见
    **值**对不上  只有那几个值失败 —— 于是躲在 fixture 恰好用了哪个值后面

motion 那条是第二种的教科书例子：manifest 写 `stationary`，iOS 发 `still`。
fixture 用的是 `walking`（两边都有），于是全绿；真人**站着不动**的时候，
也就是最常见的那个状态，每一条观测都被拒。

所以下面每个用例都拿 iOS 契约里**真实的取值**（见
`feedling-mcp-ios/Specs/perception-data-and-reporting.md` §3），不自己编。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.perceptkit_adapter.ios_report import to_envelope  # noqa: E402

AT = "2026-08-31T09:00:00+00:00"


def envelope(key, data):
    payload = {"context_snapshot": [
        {"key": "time", "data": {"timezone": "Asia/Shanghai",
                                 "locale": "zh_CN", "local_time": "x"}},
        {"key": key, "data": data},
    ], "client_ts": 1}
    return {o["signal"]: o for o in to_envelope(payload, occurred_at=AT)["observations"]}


def value(key, data, signal):
    return envelope(key, data).get(signal, {}).get("value")


# --------------------------------------------------------------------------
# 词表：值对不上（最贵的那一类）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ios_state, kit_state", [
    ("still", "stationary"),        # 站着不动 —— 人一天里最常见的状态
    ("in_vehicle", "automotive"),
    ("walking", "walking"),         # 两边同名的那个，正是当初骗过 fixture 的
    ("running", "running"),
    ("cycling", "cycling"),
    ("unknown", "unknown"),
])
def test_every_motion_state_ios_can_send_survives_the_manifest_enum(ios_state, kit_state):
    """iOS 的五个取值必须**一个不落**地落进 manifest 的枚举。

    漏一个不会报错，只会让那个状态的观测全被拒 —— 而被拒的那个，
    很可能就是用户待得最久的状态。
    """
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    assert value("motion_state", json.dumps({"state": ios_state}),
                 "motion_state")["state"] == kit_state
    assert kit_state in MINIMAL_SIGNALS["motion_state"].fields[0].enum


@pytest.mark.parametrize("ios_state, kit_state", [
    ("playing", "playing"), ("paused", "paused"), ("stopped", "stopped"),
    ("interrupted", "paused"),           # 被来电打断，对外看就是暂停
    ("seeking_forward", "playing"),      # 拖进度，音频仍在
    ("seeking_backward", "playing"),
])
def test_playback_states_ios_can_send_land_in_the_manifest_enum(ios_state, kit_state):
    v = value("playback", json.dumps({"playback_state": ios_state,
                                      "title": "t", "artist": "a"}),
              "music_playback")
    assert v["playback_state"] == kit_state


def test_an_unmapped_playback_state_is_left_to_be_rejected_not_guessed():
    """`unknown` 故意不映射。

    manifest 没有对应状态，猜一个等于把编造的答案摆到用户面前；
    被拒会出现在影子报告里，猜错不会。
    """
    v = value("playback", json.dumps({"playback_state": "unknown",
                                      "title": "t", "artist": "a"}),
              "music_playback")
    assert v["playback_state"] == "unknown"     # 原样传下去，由管线拒掉


@pytest.mark.parametrize("ios_type", [
    "bluetooth_a2dp", "bluetooth_hfp", "bluetooth_le",
    "car_audio", "headphones", "builtin", "other",
])
def test_every_audio_route_ios_can_send_is_in_the_manifest_enum(ios_type):
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    v = value("audio_route", json.dumps({"output_type": ios_type}), "audio_route")
    assert v["output_type"] in MINIMAL_SIGNALS["audio_route"].fields[0].enum


# --------------------------------------------------------------------------
# 名字对不上 + 单位对不上
# --------------------------------------------------------------------------

def test_body_fat_is_converted_not_just_renamed():
    """`body_fat_pct` -> `body_fat_ratio` 差 100 倍。

    只改名的话存进去是 18.4 —— manifest 声明的是 0~1 的比率，
    区间检查会拒掉它，然后体脂就**再也没有到过**，一声不吭。
    """
    obs = envelope("health_body", json.dumps({"body_fat_pct": 18.4,
                                              "weight_kg": 68.2}))
    v = obs["health_body_fat"]["value"]
    assert v["body_fat_ratio"] == pytest.approx(0.184)
    assert "body_fat_pct" not in v
    # 体重留在主信号，体脂已经搬走 —— 不许两边各存一份。
    assert obs["health_weight"]["value"] == {"weight_kg": 68.2}


def test_blood_pressure_reaches_the_kit():
    obs = envelope("health_metabolic",
                   json.dumps({"blood_pressure_systolic": 118,
                               "blood_pressure_diastolic": 76}))
    # 收缩压和舒张压是**一次**测量的两半（HKCorrelation），
    # 必须落进同一条观测；拆成两条的话「118/76」就没法再拼回来。
    v = obs["health_blood_pressure"]["value"]
    assert v["blood_pressure_systolic_mmhg"] == 118
    assert v["blood_pressure_diastolic_mmhg"] == 76
    assert "health_glucose" not in obs


def test_workout_duration_reaches_the_kit():
    v = value("health_workout",
              json.dumps({"workout_type": "running", "duration_min": 32,
                          "count_today": 1}), "health_workout")
    assert v["duration_minutes"] == 32


def test_audio_device_name_reaches_the_kit():
    v = value("audio_route", json.dumps({"output_type": "bluetooth_a2dp",
                                         "device_name": "AirPods Pro"}),
              "audio_route")
    assert v["device_label"] == "AirPods Pro"


# --------------------------------------------------------------------------
# 拆信号
# --------------------------------------------------------------------------

def test_step_count_becomes_its_own_signal():
    """iOS 把步数塞在体征里，manifest 给了它独立信号 ——
    因为日累计和「一次读数」的聚合方式根本不同。没人接过去时它是被丢掉的。"""
    obs = envelope("health_vitals",
                   json.dumps({"resting_heart_rate": 58, "step_count": 4211}))
    assert obs["steps"]["value"] == {"step_count": 4211}
    # 不能两边都存：同一个数字被两套聚合规则各写一遍。
    assert "step_count" not in obs["health_resting_hr"]["value"]


def test_no_steps_observation_when_the_device_did_not_report_any():
    """没有步数就是没有，不是「今天零步」。

    补一条 no_data 会让下游读成「设备说了它没走路」—— 那是另一句话。
    """
    obs = envelope("health_vitals", json.dumps({"resting_heart_rate": 58}))
    assert "steps" not in obs


def test_a_report_that_only_carries_split_off_fields_makes_no_main_observation():
    """只测了步数的那趟上报，不该顺带宣称「量了静息心率、结果是空」。

    0.4.0 把体征拆开之后，主信号的字段有可能**一个都不剩**。照旧发一条
    `observed` 空 value 的观测，下游会当成一次真实测量：当前值被一条没有
    数值的记录顶掉、日聚合多算一次，而且不报错。
    """
    obs = envelope("health_vitals", json.dumps({"step_count": 4211}))
    assert "health_resting_hr" not in obs
    assert obs["steps"]["value"] == {"step_count": 4211}   # 拆出来的照发
