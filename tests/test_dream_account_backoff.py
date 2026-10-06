"""Account failures preserve Dream data while backing off into recovery probes."""
import pytest
from contextlib import nullcontext

import db
from admin import lane_rollup_summary
from conftest import seed_user
from core import store as core_store
from memory import capture_failure, dream_failure
from notices import catalog, core as notices_core, error_contract
from proactive import capture_jobs, dream_scheduler
from model_api_runtime.v2 import serve_worker


_REASON = "dream_agent_call_failed:resident_agent_cli_logged_out"
_NOW = 1800000000.0


def _store(uid):
    seed_user(uid)
    return core_store.get_store(uid)


def _failure(store, now, reason=_REASON):
    return dream_scheduler.record_dream_job_status(store, {
        "source": capture_jobs.DREAM_JOB_SOURCE,
        "dream_result": {"reason": reason},
    }, status="failed", now=now)


def _notices(store):
    return {r["dedupe_key"]: r for r in db.log_read_all(store.user_id, notices_core.NOTICES_STREAM)}


def test_logged_out_notice_uses_contract_and_survives_paused_skip():
    store = _store("u_dream_login_notice")
    for n in range(3):
        _failure(store, _NOW + n)
    n = _notices(store)["memory_backoff:dream"]
    assert error_contract.spec_for("resident_agent_cli_logged_out", public_only=False).safe_text_zh in n["user_text"]
    assert "登录已失效" in n["user_text"]
    dream_scheduler.record_dream_job_status(store, {
        "source": capture_jobs.DREAM_JOB_SOURCE,
        "dream_skip_reason": dream_failure.PAUSED_REASON,
    }, status="skipped", now=_NOW + 100)
    assert _notices(store)["memory_backoff:dream"]["resolved"] is False
    dream_scheduler.record_dream_job_status(store, {
        "source": capture_jobs.DREAM_JOB_SOURCE,
    }, status="completed", now=_NOW + 200)
    assert _notices(store)["memory_backoff:dream"]["resolved"] is True
    state = dream_scheduler.load_dream_state(store)
    assert all(state[k] == v for k, v in dream_failure.SUCCESS_RESET.items())


def test_seven_day_account_failures_pause_automatic_retries_without_advancing_ledger(monkeypatch):
    store = _store("u_dream_seven_days")
    day = capture_failure.ACCOUNT_CLOCK_GAP_RESET_SEC
    for n in range(8):
        state = _failure(store, _NOW + n * day)
        assert state["dream_account_paused"] is (n == 7)
    saved_ledger = {k: state[k] for k in dream_scheduler.DREAM_LEDGER_FIELDS}
    monkeypatch.setattr(dream_scheduler, "_dream_enabled", lambda _s: True)
    monkeypatch.setattr(dream_scheduler, "night_only", lambda: False)
    monkeypatch.setattr(dream_scheduler, "min_interval_sec", lambda: day)
    monkeypatch.setattr(dream_scheduler, "_dream_snapshot", lambda _s: {
        "card_count": 20, "seed_card_count": 20, "signature": "garden-unchanged"})
    calls = []
    monkeypatch.setattr(dream_scheduler, "_admission_slot", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("pause must run before admission")))
    # No job/provider call is created during cooldown; this is a control skip.
    out = dream_scheduler._tick_memory_dream(store, now=_NOW + 7 * day + 600,
                                            submit=lambda **kw: calls.append(kw))
    assert out["reason"] == "dream_account_paused" and out["status"] == "skipped"
    assert out["enqueued"] is False and calls == []
    assert {k: dream_scheduler.load_dream_state(store)[k] for k in saved_ledger} == saved_ledger
    assert dream_failure.probe_not_due(state, now=_NOW + 8 * day, interval=day) is False
    # Late recovery probes retain low frequency even if the 24h clock resets.
    state = _failure(store, _NOW + 8 * day + 10)
    assert state["dream_account_paused"] is True
    assert state["dream_account_fail_since"] == _NOW + 8 * day + 10
    assert dream_failure.probe_not_due(state, now=_NOW + 8 * day + 700, interval=day)


def test_account_gap_resets_before_pause_and_non_account_failure_exits_pause():
    store = _store("u_dream_clock_gap")
    _failure(store, _NOW)
    later = _NOW + capture_failure.CAPTURE_ACCOUNT_SKIP_AFTER_SEC + 1
    state = _failure(store, later)
    assert state["dream_account_fail_since"] == later
    assert state["dream_account_paused"] is False
    state["dream_account_paused"] = True
    dream_scheduler.save_dream_state(store, state)
    state = _failure(store, later + 10, "dream_agent_call_failed:turn_timeout")
    assert state["dream_account_error_code"] == ""
    assert state["dream_account_paused"] is False


@pytest.mark.parametrize("force,elapsed", [(True, 600), (False, 86400)])
def test_paused_account_allows_forced_run_or_due_probe(monkeypatch, force, elapsed):
    store = _store(f"u_dream_probe_{force}")
    for n in range(8):
        _failure(store, _NOW + n * 86400)
    monkeypatch.setattr(dream_scheduler, "_dream_enabled", lambda _s: True)
    monkeypatch.setattr(dream_scheduler, "night_only", lambda: False)
    monkeypatch.setattr(dream_scheduler, "min_interval_sec", lambda: 86400)
    monkeypatch.setattr(dream_scheduler, "_dream_snapshot", lambda _s: {
        "card_count": 20, "seed_card_count": 20, "signature": "new-garden"})
    monkeypatch.setattr(dream_scheduler, "_admission_slot", lambda **_kw: nullcontext(None))
    calls = []

    def submit(_store, **kw):
        calls.append(kw)
        return {"job": {"status": "queued", "dream_key": "probe"},
                "enqueued": True, "reason": "enqueued"}

    now = _NOW + 7 * 86400 + elapsed
    out = dream_scheduler._tick_memory_dream(store, now=now, force=force, submit=submit)
    assert out["enqueued"] is True and len(calls) == 1
    assert calls[0]["trigger"] == ("force_dream" if force else "nightly_dream")
    _failure(store, now + 1)
    out = dream_scheduler._tick_memory_dream(store, now=now + 600, submit=submit)
    assert out["reason"] == "dream_account_paused" and len(calls) == 1


def test_paused_skip_is_a_control_outcome_not_an_operational_failure():
    reason = dream_failure.PAUSED_REASON
    assert reason in dream_scheduler.DREAM_SKIP_REASONS
    assert catalog.v1_proactive_outcome_class("skipped", reason) == "control"
    row = dict(user_id="u", day="2026-09-19", lane="dream", route="resident",
               failed=1, completed=0, expired=0, failure_codes={reason: 1},
               operational_failures=0, control_outcomes=1, user_unavailable=0)
    stats = lane_rollup_summary.aggregate_day([row], lane="dream", route="resident", day="2026-09-19")
    assert stats.operational == 0 and stats.control == 1


def test_v2_dream_status_adapter_preserves_account_failure_reason(monkeypatch):
    store = _store("u_dream_v2_account")
    monkeypatch.setattr(dream_scheduler, "_dream_snapshot", lambda _s: {})
    serve_worker._record_extraction_status(store.user_id, "dream", "failed", {
        "reason": "extraction_failed:quota_insufficient"})
    state = dream_scheduler.load_dream_state(store)
    assert state["dream_account_error_code"] == "quota_insufficient"
