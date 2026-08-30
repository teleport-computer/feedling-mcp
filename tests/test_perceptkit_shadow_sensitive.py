"""影子在【敏感信号】上的行为 —— 感知数据的大头都在这半边。

位置、播放、健康这些走加密信封，活路径解密之后才进 `storage_items`，
影子接的就是那一步之后。所以这里用仓库自己那条注入解密的缝
（`ingest_snapshot_v2(decrypt_envelope=...)`），和 test_perception_ingress_v2
用的是同一个，**enclave 解密本身不是这个文件要验的东西**。

跑在真库上：影子写的是真表，内存实现验不出主键、CAS 和跨用户隔离。
"""
from __future__ import annotations

import json
import os
import time
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(_BACKEND))

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("PERCEPTKIT_TEST_PG") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="需要真库：设 PERCEPTKIT_TEST_PG 或 DATABASE_URL")

SENSITIVE = {
    "loc-1": {"place_label": "home", "wifi_label": "家里", "country": "CN",
              "locality": "上海", "wifi_anchor_id": "a1b2c3d4e5f6a7b8"},
    "mot-1": {"state": "walking"},
    "pb-1": {"playback_state": "playing", "title": "夜空中最亮的星",
             "artist": "逃跑计划"},
    "vit-1": {"resting_heart_rate": 58, "hrv_sdnn_ms": 42.5},
}


def _decrypt(envelope, api_key, *, purpose):
    return json.dumps(SENSITIVE[envelope["id"]]).encode("utf-8")


def _snapshot():
    return [
        {"key": "time", "message": "",
         "data": {"local_time": "2026-08-30T17:00:00+08:00",
                  "timezone": "Asia/Shanghai", "locale": "zh_CN"}},
        {"key": "location_signal", "changed": True, "message": "",
         "envelope": {"id": "loc-1", "body_ct": "Y3Q="}},
        {"key": "motion_state", "changed": True, "message": "",
         "envelope": {"id": "mot-1", "body_ct": "Y3Q="}},
        {"key": "playback", "changed": True, "message": "",
         "envelope": {"id": "pb-1", "body_ct": "Y3Q="}},
        {"key": "health_vitals", "changed": True, "message": "",
         "envelope": {"id": "vit-1", "body_ct": "Y3Q="}},
    ]


def _user(uid: str) -> str:
    """建一个真用户行。

    不建的话 `users` 的外键会挡住 per-user 写入，活路径一行都写不进去，
    结果每个信号都报 stale_ignored —— 看起来像上报过期，实际是用户不存在。
    仓库自带 seed_user 就是为这个准备的。
    """
    from conftest import seed_user
    seed_user(uid)
    return uid


@pytest.fixture
def clean_kit_tables():
    import db
    from perception.perceptkit_adapter import schema
    with db.get_pool().connection() as c:
        c.execute(schema.DDL)
        c.execute(schema.TRUNCATE)
    yield


def _current(uid: str) -> dict[str, dict]:
    """走后端自己的连接池读。

    **不要用环境变量里的 DSN 另开连接** —— conftest 会把 DATABASE_URL 换成
    它自己起的测试库，另开连接就读到了另一个库：影子明明写进去了，
    这里却读出空的，看起来像影子没跑。
    """
    import db
    with db.get_pool().connection() as c:
        return {
            r[0]: r[1] for r in c.execute(
                "select signal, typed_value from perceptkit_current "
                "where subject_id=%s", (uid,))
        }


def test_decrypted_sensitive_signals_reach_the_kit(clean_kit_tables):
    """感知的大头在加密那半 —— 影子必须看得到它们，否则等于只验了零头。"""
    from perception import service
    uid = _user("usr_shadow_sensitive")
    results = service.ingest_snapshot_v2(
        uid, _snapshot(), client_ts=str(int(time.time())), decrypt_envelope=_decrypt)
    assert results.get("playback") in ("changed", "accepted", "unchanged"), results

    got = _current(uid)
    assert "music_playback" in got, f"播放没进 kit：{sorted(got)}"
    assert got["music_playback"]["title"] == "夜空中最亮的星"
    assert "motion_state" in got and got["motion_state"]["state"] == "walking"
    assert "health_vitals" in got
    assert got["health_vitals"]["resting_heart_rate"] == 58


def test_location_is_deliberately_not_a_pass_through_signal(clean_kit_tables):
    """位置在端上就解析成城市/锚点了，坐标不出设备，所以它不是一条直通观测。

    写成测试是为了让"它没进 kit"是有意的，而不是下一个人以为漏了。
    """
    from perception import service
    uid = _user("usr_shadow_location")
    service.ingest_snapshot_v2(uid, _snapshot(), client_ts=str(int(time.time())),
                               decrypt_envelope=_decrypt)
    assert "location_city" not in _current(uid)
    assert "proximity_anchor" not in _current(uid)


def test_the_shadow_never_breaks_the_report(clean_kit_tables, monkeypatch):
    """影子炸了，上报照样要成功 —— 为一个诊断把用户的上报变成 500，
    是自己给自己制造事故。"""
    from perception import service
    from perception.perceptkit_adapter import shadow

    def boom(*a, **k):
        raise RuntimeError("影子炸了")

    monkeypatch.setattr(shadow, "observe", boom)
    results = service.ingest_snapshot_v2(
        _user("usr_shadow_boom"), _snapshot(), client_ts=str(int(time.time())), decrypt_envelope=_decrypt)
    assert results.get("time") == "accepted"


def test_the_kill_switch_stops_it_writing_anything(clean_kit_tables, monkeypatch):
    from perception import service
    from perception.perceptkit_adapter import shadow

    monkeypatch.setenv(shadow.ENV_FLAG, "0")
    uid = _user("usr_shadow_off")
    results = service.ingest_snapshot_v2(
        uid, _snapshot(), client_ts=str(int(time.time())), decrypt_envelope=_decrypt)
    assert results.get("time") == "accepted"
    assert _current(uid) == {}


def test_two_users_snapshots_do_not_mix(clean_kit_tables):
    """影子写的是真表，主键里带 subject —— 换个人不该看到别人的值。"""
    from perception import service
    a = dict(SENSITIVE)
    service.ingest_snapshot_v2(_user("usr_a"), _snapshot(), client_ts=str(int(time.time())),
                               decrypt_envelope=_decrypt)
    SENSITIVE["pb-1"] = {"playback_state": "playing", "title": "别的歌",
                         "artist": "别人"}
    try:
        service.ingest_snapshot_v2(_user("usr_b"), _snapshot(), client_ts=str(int(time.time()) + 1),
                                   decrypt_envelope=_decrypt)
        assert _current("usr_a")["music_playback"]["title"] == "夜空中最亮的星"
        assert _current("usr_b")["music_playback"]["title"] == "别的歌"
    finally:
        SENSITIVE.update(a)
