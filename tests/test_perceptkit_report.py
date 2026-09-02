"""切换期的观测口。

**账记了、看不见，等于没记。** 差异写在 `perceptkit_shadow_divergence`、
唤醒的下场写在发件箱和回执里 —— 而真正要看它们的时候（test 上跑着、真机在
打数据）没人能连库。这一份是那个缺口的补丁。

这里盯的两件事：

    数对不对           把「被免打扰挡下」算进失败里,会让人去修一个不存在的问题
    干净有没有分母      「零差异」只有配上「比了多少」才是个结论
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

psycopg = pytest.importorskip("psycopg")

from perception.perceptkit_adapter import compare, report, schema  # noqa: E402

DSN = os.environ.get("PERCEPTKIT_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="需要真库")

from datetime import datetime, timezone  # noqa: E402

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(schema.DDL)
        c.execute(schema.TRUNCATE)
        yield c


def test_a_clean_report_comes_with_the_denominator(conn):
    """「零差异」单独看没有意义 —— 一条都没比也是零差异。"""
    out = report.build(conn)
    assert out["perception"]["divergences"] == []
    assert out["coverage"]["fields_compared"] > 0
    assert out["coverage"]["signals_compared"]


def test_divergences_carry_both_values(conn):
    """只报「有 3 个不一致」的报告，等于说有问题然后不说是什么。"""
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "differ", 0.87, 87)],
        now=T0, report_id="r1")
    rows = report.build(conn)["perception"]["divergences"]
    assert rows[0]["signal"] == "battery" and rows[0]["field"] == "level_ratio"
    assert rows[0]["live"] == "0.87" and rows[0]["kit"] == "87"


def test_agreement_is_counted_but_not_listed(conn):
    """一致的不用逐条列 —— 但要有个数，否则看不出报告的分量。"""
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "agree", 1, 1)],
        now=T0, report_id="r1")
    out = report.build(conn)["perception"]
    assert out["divergences"] == [] and out["agreements"] == 1


def _wake(conn, event_id, status):
    from perceptkit.contracts.records import EventOutboxEntry
    from perceptkit.contracts.receipt import WakeReceipt

    from perception.perceptkit_adapter.storage import PostgresStorage
    s = PostgresStorage(conn)
    s.enqueue_event(EventOutboxEntry(
        event_id=event_id, subject_id="u1", definition_id="d", definition_version=1,
        event_type="photo_added", occurred_at=T0, detected_at=T0,
        delivery_state="claimed", attempt_count=1, fact_snapshot={},
        claim_token="t", lease_owner="w", lease_expires_at=T0,
    ))
    s.record_wake_receipt(
        receipt=WakeReceipt(event_id=event_id, attempt_id="a1", status=status,
                            received_at=T0),
        next_state="delivered" if status == "accepted" else "suppressed",
        claim_token="t")


def test_the_host_gate_is_not_counted_as_a_failure(conn):
    """免打扰把它挡下了，是**系统正常工作的样子**。

    混进失败里有两个后果：有人去修一个不存在的问题，而真正该慌的
    `enqueue_failed` 淹在里面看不见。
    """
    _wake(conn, "e1", "accepted")
    _wake(conn, "e2", "conversation_suppressed")
    _wake(conn, "e3", "conversation_suppressed")
    w = report.build(conn)["wakes"]
    assert w["produced"] == 3
    assert w["delivered"] == 1
    assert w["suppressed_by_host"] == 2
    assert w["failed"] == 0


def test_a_real_delivery_failure_is_counted(conn):
    _wake(conn, "e1", "enqueue_failed")
    assert report.build(conn)["wakes"]["failed"] == 1


def test_it_can_be_narrowed_to_one_person(conn):
    """排查「他说没收到提醒」的时候，全局汇总答不了那个问题。"""
    compare.record(conn, "u1", [
        compare.Divergence("battery", "level_ratio", "differ", 1, 2)],
        now=T0, report_id="r1")
    compare.record(conn, "u2", [
        compare.Divergence("weather", "uv_index", "differ", 1, 2)],
        now=T0, report_id="r2")
    only = report.build(conn, subject_id="u1")["perception"]["divergences"]
    assert [r["signal"] for r in only] == ["battery"]


# ---------------------------------------------------------------------------
# 一个 100% 被拒的信号必须是响的
#
# 2026-09-02：外部审查说「iOS adapter 只覆盖部分信号」，我数代码得出「全覆盖」
# 并据此顶了回去 —— 实际上 health_sleep 的每一条上报都被拒（iOS 送四个每日
# 分钟总数，manifest 要「一条观测 = 一个睡眠阶段」，stage 必填）。
#
# 证据当时就在这份报告里：signals_seen 是 22 个而 manifest 有 23 个。
# 但那要求读的人先记得 23 这个数、再去数一遍。三层都不报错：
# 拒收不进任何计数、摄入照样返回成功、快照那侧静默回退到老路。
# ---------------------------------------------------------------------------

def test_a_signal_that_never_arrives_is_named_not_left_to_be_counted_out():
    from perceptkit.manifest.minimal import MINIMAL_SIGNALS
    from perception.perceptkit_adapter import report

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        # (信号, 总行数, observed 行数)
        def fetchall(self): return [("battery", 3, 3), ("weather", 2, 2)]

    class _Conn:
        def cursor(self): return _Cur()

    cov = report._coverage(_Conn(), "u")
    missing = set(cov["signals_never_seen"])
    assert "health_sleep" in missing
    # 名单必须是「声明过的减去见过的」，不是一个手写常量 —— 手写的会漂。
    assert missing == set(MINIMAL_SIGNALS) - {"battery", "weather"}
    assert "battery" not in missing


def test_a_signal_that_only_ever_arrives_absent_is_named_too():
    """**这一条才是真正踩到的那个。**

    health_sleep 的真数据每条都被拒，但「权限关闭」「这轮没读到」那两种
    没有 value、不过字段校验，照常落一行 —— 于是它出现在 signals_seen 里，
    看上去接通了，连 signals_never_seen 都抓不到它。

    「只有缺席、从没有过在场」和「这个信号根本没接」外观一样，
    和「我们把它的真数据全拒了」外观也一样。
    """
    from perception.perceptkit_adapter import report

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def fetchall(self):
            return [("battery", 5, 5),          # 正常
                    ("health_sleep", 9, 0),      # 有行，但一条真数据都没有
                    ("weather", 4, 1)]           # 偶尔读不到，但有过在场

    class _Conn:
        def cursor(self): return _Cur()

    cov = report._coverage(_Conn(), "u")
    assert cov["signals_never_observed"] == ["health_sleep"]
    assert "health_sleep" not in cov["signals_never_seen"], "它有行，不属于'从没见过'"
