"""proactive_tick 在 db_action_v2 模式下的 MANUAL wake 桥接（D3 Task 9）。

db_action_v2 用户没有常驻 consumer（D0 排他性 guard 已把他们从发现名单里摘掉），
所以 MANUAL wake（"talk to me now"）不能再走常驻 proactive_job（永远不会被认领）；
必须落成 V2 的 manual_wake agent_job。非 manual（heartbeat）tick 归 V2 scheduler
自己管，这里不建任何常驻 job。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from psycopg.rows import dict_row

import db
from core import store as core_store
from core import wake_bus as core_wake_bus
from hosted import config_store as hosted_config_store
from model_api_runtime.v2 import jobs_store
from proactive import gate, proactive_core

from conftest import seed_user, configure_model_api_route


def _seed_v2(uid):
    seed_user(uid)
    configure_model_api_route(
        uid, provider="anthropic", model="m", test_status="ok",
        envelope={"body_ct": "x", "nonce": "n", "K_user": "k"})


@pytest.fixture(autouse=True)
def _clean_agent_jobs():
    yield
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM agent_jobs WHERE user_id IN "
            "('u_manual_v2', 'u_heartbeat_v2', 'u_manual_resident')"
        )


def _manual_wake_jobs(uid):
    with db.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, lane, status, reason FROM agent_jobs WHERE user_id=%s AND lane='manual_wake'",
                (uid,),
            )
            rows = cur.fetchall()
    return rows


def test_db_action_v2_manual_tick_enqueues_manual_wake_job(monkeypatch):
    _seed_v2("u_manual_v2")
    store = core_store.get_store("u_manual_v2")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    gate_called = {"n": 0}
    monkeypatch.setattr(
        gate, "_build_proactive_v2_wake_decision",
        lambda *a, **k: gate_called.update(n=gate_called["n"] + 1) or {},
    )
    resident_job_called = {"n": 0}
    monkeypatch.setattr(
        store, "append_proactive_job",
        lambda *a, **k: resident_job_called.update(n=resident_job_called["n"] + 1) or {},
    )
    notified = {}
    monkeypatch.setattr(
        core_wake_bus, "notify",
        lambda channel, user_id="": notified.update(channel=channel, user_id=user_id),
    )

    result = proactive_core.proactive_tick(store, {"manual": True}, api_key="key")

    assert result["v2"] is True
    assert result["enqueued"] is True
    assert result["job"]["lane"] == "manual_wake"
    assert gate_called["n"] == 0
    assert resident_job_called["n"] == 0
    assert notified["channel"] == "v2_jobs" and notified["user_id"] == "u_manual_v2"

    rows = _manual_wake_jobs("u_manual_v2")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_db_action_v2_manual_via_force_flag_also_enqueues(monkeypatch):
    _seed_v2("u_manual_v2")
    store = core_store.get_store("u_manual_v2")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    result = proactive_core.proactive_tick(store, {"force": True}, api_key="key")

    assert result["v2"] is True
    assert result["enqueued"] is True
    rows = _manual_wake_jobs("u_manual_v2")
    assert len(rows) == 1


def test_db_action_v2_non_manual_tick_creates_no_job(monkeypatch):
    _seed_v2("u_heartbeat_v2")
    store = core_store.get_store("u_heartbeat_v2")
    hosted_config_store.set_hosted_runtime_mode(store, "db_action_v2")

    gate_called = {"n": 0}
    monkeypatch.setattr(
        gate, "_build_proactive_v2_wake_decision",
        lambda *a, **k: gate_called.update(n=gate_called["n"] + 1) or {},
    )
    resident_job_called = {"n": 0}
    monkeypatch.setattr(
        store, "append_proactive_job",
        lambda *a, **k: resident_job_called.update(n=resident_job_called["n"] + 1) or {},
    )

    result = proactive_core.proactive_tick(store, {}, api_key="key")

    assert result["v2"] is True
    assert result["enqueued"] is False
    assert result["job"] is None
    assert gate_called["n"] == 0
    assert resident_job_called["n"] == 0
    assert _manual_wake_jobs("u_heartbeat_v2") == []


def test_resident_cli_manual_tick_still_uses_resident_path(monkeypatch):
    """Regression: a hosted user with no hosted_runtime_mode set (default
    resident_cli) must hit the exact same resident gate-decision path as
    before Task 9 — the v2 bridge must be a no-op for them."""
    _seed_v2("u_manual_resident")
    store = core_store.get_store("u_manual_resident")
    # Full cutover 2026-07-11: db_action_v2 is the default, so the resident path is
    # reached only by an explicit resident_cli opt-out.
    hosted_config_store.set_hosted_runtime_mode(store, "resident_cli")
    assert hosted_config_store.get_hosted_runtime_mode(store) == "resident_cli"

    fake_decision = {"should_wake_agent": True, "decision_id": "d1"}
    gate_called = {"n": 0}
    monkeypatch.setattr(
        gate, "_build_proactive_v2_wake_decision",
        lambda *a, **k: gate_called.update(n=gate_called["n"] + 1) or fake_decision,
    )
    monkeypatch.setattr(gate, "_proactive_job_from_decision", lambda decision: {"job_id": "j1"})

    result = proactive_core.proactive_tick(store, {"manual": True}, api_key="key")

    assert gate_called["n"] == 1
    assert "v2" not in result
    assert result["enqueued"] is True
    assert result["decision"] == fake_decision
    assert _manual_wake_jobs("u_manual_resident") == []
