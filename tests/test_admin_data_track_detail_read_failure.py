"""T367 验收量具:detail 页面必须能看见「这次没读出来」。

注入注在**真实故障发生的那一层**,不是 monkeypatch 边界:
`admin_data_track_snapshot` 借出的那条 admin 连接自己会
`SET statement_timeout`(backend/db.py `_admin_data_track_connection`)。我们从
另一条真 psycopg 连接对 `frame_envelopes` 加 ACCESS EXCLUSIVE 锁,PostgreSQL 就
真的把 snapshot 的那条 SQL 取消掉(sqlstate 57014),真的走进 db.py 的 except。

为什么锁 `frame_envelopes` 而不是 `user_logs`:
  * `frame_envelopes` 只被 snapshot 读(`_screen_frames_into`,db.py:1504,在
    snapshot 内于 db.py:1682 被调用 —— 早于 app_usage 与 responder_runtime 两段
    查询,所以这两块都吃到这次失败)。
  * `user_logs` 还被 `admin_data_track_user_daily_usage` 读；那条路径现在也有
    statement_timeout。锁它会让 snapshot 和 daily_usage 一起失败，破坏本测试
    需要的「只有 snapshot 失败」对照。锁错表 = 复现不出生产那个形状。

健康态 daily_usage 仍然读得到,故障态 app_usage 归零 —— 这就是生产上
「全时段计数 < 自己的 14 天切片」那个内部矛盾的本地复刻。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from tests.test_data_track import (  # noqa: F401
    _admin_headers,
    _register,
    client,
)

import db  # noqa: E402


LOCKED_TABLE = "frame_envelopes"
INJECTED_TIMEOUT_MS = 300


class RealReadFailure:
    """真锁 + 真 statement_timeout ⇒ 真 57014。不是 monkeypatch 边界。"""

    def __init__(self, table: str = LOCKED_TABLE, hold_sec: float = 8.0):
        self.table = table
        self.hold_sec = hold_sec
        self._locked = threading.Event()
        self._release = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            with conn.cursor() as cur:
                cur.execute(f"LOCK TABLE {self.table} IN ACCESS EXCLUSIVE MODE")
                self._locked.set()
                self._release.wait(self.hold_sec)
            conn.rollback()

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._locked.wait(5), "locker never acquired the lock"
        return self

    def __exit__(self, *_a):
        self._release.set()
        if self._thread:
            self._thread.join(timeout=10)
        return False


def _seed_sessions(user_id: str) -> int:
    """今天两条 app_session_end;返回期望的 foreground_sec。"""
    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).date()
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=zone).timestamp()
    durations = (80, 90)
    for index, duration in enumerate(durations):
        db.log_append(
            user_id,
            "tracking_events",
            {
                "event_id": f"t367_{index}",
                "type": "app_session_end",
                "ts": midnight + index,
                "payload": {"duration_sec": duration},
            },
            ts=midnight + index,
        )
    return sum(durations)


def _seed_live_lease(user_id: str, *, owner: str = "sup-t367", ttl_sec: int = 600) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
            INSERT INTO agent_runtime_instances
                (user_id, driver, status, lease_owner, lease_expires_at, runtime_home)
            VALUES (%s, 'claude', 'running', %s, now() + make_interval(secs => %s), %s)
            ON CONFLICT (user_id) DO UPDATE SET
                lease_owner = EXCLUDED.lease_owner,
                lease_expires_at = EXCLUDED.lease_expires_at,
                status = EXCLUDED.status
            """,
            (user_id, owner, ttl_sec, f"/tmp/t367/{user_id}"),
        )
        conn.commit()


def _detail(client, user_id: str) -> dict:
    res = client.get(
        f"/v1/admin/data-track/users/{user_id}?days=3",
        headers=_admin_headers(),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["user"]


def _daily_sum(row: dict) -> int:
    return sum(r["foreground_sec"] for r in row.get("daily_usage") or [])


# --------------------------------------------------------------------------
# 桶 A:真的有人取件。夹具自证走到了 lease 分支。
# --------------------------------------------------------------------------
def test_live_lease_reads_hosted_v1(client):
    user_id, _ = _register(client)
    _seed_live_lease(user_id)

    responder = _detail(client, user_id)["responder"]

    assert responder["effective_responder"] == "hosted_v1"
    # basis 只可能由 v1_lease_active 产生 ⇒ 夹具确实走到了目标分支
    assert responder["basis"] == "live_agent_runtime_lease"


# --------------------------------------------------------------------------
# 桶 B:真的没人取件(镜像反例)。修复后这一格必须**仍然**是 none,
# 否则就是把所有 none 一律改成 unknown 的假修复。
# --------------------------------------------------------------------------
def test_idle_user_still_reads_none_when_read_succeeds(client):
    user_id, _ = _register(client)

    responder = _detail(client, user_id)["responder"]

    assert responder["effective_responder"] == "none"
    assert responder["basis"] == "no_current_or_recent_evidence"


# --------------------------------------------------------------------------
# 桶 C:取数失败。今天它和桶 B 逐字节相同 —— 这就是 T367 要修的谎。
#
# ①②③ 各自独立成一条测试:堆在一条里的话只有最先那条断言会报红,
# 另外两条结构上到不了,等于没有覆盖。
# --------------------------------------------------------------------------
def _healthy_then_broken(client, monkeypatch) -> tuple[dict, dict, int]:
    user_id, _ = _register(client)
    expected = _seed_sessions(user_id)
    _seed_live_lease(user_id)

    # 先证明量具在健康态下读得对,否则后面的 0 / none 说明不了任何事
    healthy = _detail(client, user_id)
    assert healthy["app_usage"]["foreground_sec"] == expected
    assert _daily_sum(healthy) == expected
    assert healthy["responder"]["effective_responder"] == "hosted_v1"

    monkeypatch.setattr(db, "_ADMIN_DATA_TRACK_READ_TIMEOUT_MS", INJECTED_TIMEOUT_MS)
    with RealReadFailure():
        broken = _detail(client, user_id)

    # 自证:注入只打中了读 frame_envelopes 的 snapshot；daily_usage 只读 user_logs。
    assert _daily_sum(broken) == expected, (
        "daily_usage 也挂了 ⇒ 锁选错表,复现的不是生产那个形状"
    )
    return healthy, broken, expected


def test_c2_detail_surfaces_snapshot_read_status(client, monkeypatch):
    """② operator 诊断单个用户的那一页,必须看得见「这次没读出来」。"""
    _, broken, _ = _healthy_then_broken(client, monkeypatch)

    status = broken.get("snapshot_read_status") or {}
    assert status.get("level") in {"timeout", "read_error"}, (
        f"detail 页看不见读失败,snapshot_read_status={broken.get('snapshot_read_status')!r}"
    )


def test_c1_responder_degrades_to_unknown_not_none(client, monkeypatch):
    """① 失效只能退向 unknown,不能退向一个具体结论。"""
    _, broken, _ = _healthy_then_broken(client, monkeypatch)

    responder = broken["responder"]
    assert responder["effective_responder"] == "unknown", (
        f"读失败被渲染成确定结论 {responder['effective_responder']!r};"
        " 它与桶 B「真的没人取件」不可区分"
    )
    assert responder["basis"] != "no_current_or_recent_evidence"


def test_c1b_app_usage_marked_failed_with_the_same_snapshot(client, monkeypatch):
    """① 后半:app_usage 和 responder 是**同一次** snapshot 的产物,要一起标失败。"""
    _, broken, _ = _healthy_then_broken(client, monkeypatch)

    app_usage = broken["app_usage"]
    assert app_usage.get("fields_status") != "ok", (
        f"app_usage 读失败却仍自称 ok,把 0 当成确定读数呈现:{app_usage!r}"
    )


def test_c3_status_labels_are_derived_not_unconditional(client, monkeypatch):
    """③ 把这次读取改成失败,标签要跟着变;不变 ⇒ 它是假的。"""
    healthy, broken, _ = _healthy_then_broken(client, monkeypatch)

    unchanged = []
    for path in (("chat", "counts_status"), ("app_usage", "fields_status")):
        section, key = path
        healthy_value = (healthy.get(section) or {}).get(key)
        broken_value = (broken.get(section) or {}).get(key)
        if healthy_value is None and broken_value is None:
            continue
        if healthy_value == broken_value:
            unchanged.append(f"{section}.{key}={broken_value!r}")
    assert not unchanged, (
        "这些标签在读失败前后逐字节相同 ⇒ 无条件写死的真值标签:" + ", ".join(unchanged)
    )
