"""Provider-health state machine and shared proactive-gate coverage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest  # noqa: E402
import db  # noqa: E402
import provider_health  # noqa: E402
from model_api_runtime.v2 import serve_worker  # noqa: E402
from proactive import gate, proactive_core  # noqa: E402


DAY = 24 * 60 * 60


def test_unhealthy_proactive_probe_is_allowed_only_once_per_24h(backend_env):
    uid = "usr_provider_health_daily_probe"
    conftest.seed_user(uid)
    entered_at = 10_000.0
    with db.get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO provider_health (
              user_id, provider_state, last_provider_failure_at,
              last_provider_error_class, last_provider_error_blame, last_probe_at
            )
            VALUES (%s, 'needs_user_action', to_timestamp(%s),
                    'quota_insufficient', 'user_provider', to_timestamp(%s))
            """,
            (uid, entered_at, entered_at),
        )

    before_due = provider_health.proactive_admission(
        uid,
        now=entered_at + DAY - 1,
    )
    due = provider_health.proactive_admission(uid, now=entered_at + DAY)
    duplicate = provider_health.proactive_admission(
        uid,
        now=entered_at + DAY,
    )

    assert before_due.allowed is False
    assert before_due.block_reason == (
        provider_health.PROVIDER_NEEDS_USER_ACTION_REASON
    )
    assert due == provider_health.ProactiveAdmission(allowed=True, probe=True)
    assert duplicate.allowed is False


class _GateStore:
    user_id = "usr_provider_health_gate"

    def __init__(self):
        self.jobs = []
        self.decisions = []

    def load_proactive_settings(self):
        return {
            "first_chat_ok_at": "2026-07-25T00:00:00Z",
            "wake_interval_sec": 900,
        }

    def list_device_events(self, since_epoch=0, limit=100):
        return []

    def append_gate_decision(self, decision):
        self.decisions.append(decision)
        return decision

    def append_proactive_job(self, job):
        self.jobs.append(job)
        return job

    def append_skipped_proactive_job(self, job):
        return job


def _blocked_admission(_user_id: str, *, now: float):
    return provider_health.ProactiveAdmission(
        allowed=False,
        block_reason=provider_health.PROVIDER_NEEDS_USER_ACTION_REASON,
    )


def test_resident_v1_tick_is_blocked_in_shared_gate(monkeypatch):
    store = _GateStore()
    monkeypatch.setattr(
        gate.screen_frames,
        "_recent_frame_meta",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        proactive_core.hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda _store: "",
    )
    monkeypatch.setattr(
        gate.provider_health,
        "proactive_admission",
        _blocked_admission,
    )

    result = proactive_core.proactive_tick(
        store,
        {
            "trigger": "heartbeat_broadcast_on",
            "broadcast_state": "on",
        },
        api_key="key",
    )

    assert result["enqueued"] is False
    assert result["decision"]["should_wake_agent"] is False
    assert (
        result["decision"]["block_reason"]
        == provider_health.PROVIDER_NEEDS_USER_ACTION_REASON
    )


def test_manual_wake_bypasses_provider_health_gate(monkeypatch):
    store = _GateStore()
    monkeypatch.setattr(
        gate.screen_frames,
        "_recent_frame_meta",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        gate.provider_health,
        "proactive_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual wake must not consult automatic health gate")
        ),
    )

    decision = gate._build_proactive_v2_wake_decision(
        store,
        {"manual": True},
        api_key="key",
    )

    assert decision["manual"] is True
    assert decision["should_wake_agent"] is True
    assert decision["block_reason"] == ""


def test_runtime_v2_scheduler_is_blocked_in_shared_gate(monkeypatch):
    store = _GateStore()
    monkeypatch.setattr(
        serve_worker.core_store,
        "get_store",
        lambda _user_id: store,
    )
    monkeypatch.setattr(
        serve_worker.hosted_config_store,
        "hosted_runtime_v2_enabled_strict",
        lambda _store: True,
    )
    monkeypatch.setattr(
        gate.provider_health,
        "proactive_admission",
        _blocked_admission,
    )

    decision = serve_worker._wake_decision_for_user(store.user_id)

    assert decision["should_wake"] is False
    assert (
        decision["block_reason"]
        == provider_health.PROVIDER_NEEDS_USER_ACTION_REASON
    )


def test_stored_latency_average_matches_the_pure_function(backend_env):
    """The UPSERT folds the EWMA in SQL; it must agree with evolve_success.

    Two implementations of one average will drift apart silently, and the SQL
    one is the only path production actually takes.
    """
    uid = "usr_provider_health_latency_ewma"
    conftest.seed_user(uid)

    samples = [293_000.0, 250_000.0, 11_000.0, 11_000.0, 9_000.0]
    for index, sample in enumerate(samples):
        provider_health.record_success(uid, now=1000.0 + index, latency_ms=sample)

    with db.get_pool().connection() as conn:
        stored = conn.execute(
            "SELECT recent_latency_ms FROM provider_health WHERE user_id = %s",
            (uid,),
        ).fetchone()[0]

    expected: dict = {}
    for index, sample in enumerate(samples):
        expected = provider_health.evolve_success(
            expected, now=1000.0 + index, latency_ms=sample
        )

    assert stored == pytest.approx(expected["recent_latency_ms"], rel=1e-9)


def test_a_success_without_a_sample_does_not_erase_the_average(backend_env):
    uid = "usr_provider_health_latency_carry"
    conftest.seed_user(uid)

    provider_health.record_success(uid, now=1000.0, latency_ms=240_000.0)
    provider_health.record_success(uid, now=1001.0)  # no stopwatch available

    with db.get_pool().connection() as conn:
        stored = conn.execute(
            "SELECT recent_latency_ms FROM provider_health WHERE user_id = %s",
            (uid,),
        ).fetchone()[0]

    assert stored == pytest.approx(240_000.0)
    assert provider_health.provider_is_slow({"recent_latency_ms": stored})


def test_a_failure_does_not_erase_the_latency_average(backend_env):
    """Failures describe a different axis and must leave latency intact."""
    uid = "usr_provider_health_latency_vs_failure"
    conftest.seed_user(uid)

    provider_health.record_success(uid, now=1000.0, latency_ms=240_000.0)
    provider_health.record_failure(uid, error_class="transient", now=1001.0)

    with db.get_pool().connection() as conn:
        stored = conn.execute(
            "SELECT recent_latency_ms FROM provider_health WHERE user_id = %s",
            (uid,),
        ).fetchone()[0]

    assert stored == pytest.approx(240_000.0)
