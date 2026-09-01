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
