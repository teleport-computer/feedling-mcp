"""V2 replan/invalidation 安全点状态机（spec §8）。Pure-unit（invalidate() 测调用契约）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import invalidation as v2_inval  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def _u(mid, ts, content="hi"):
    return {"id": mid, "role": "user", "ts": ts, "content": content}


def test_new_visible_message_after_cursor_triggers_replan():
    # plan built after folding messages up to ts=102; a new user msg at 105 arrives.
    messages = [_u("m1", 101.0), _u("m2", 102.0), _u("m3", 105.0)]
    assert v2_inval.new_visible_message_since(messages, cursor_ts=102.0) is True
    decision = v2_inval.evaluate(messages, safe_point="after_reads", coalesced_cursor_ts=102.0)
    assert decision == v2_inval.REPLAN


def test_no_new_message_continues():
    messages = [_u("m1", 101.0), _u("m2", 102.0)]
    decision = v2_inval.evaluate(messages, safe_point="before_write", coalesced_cursor_ts=102.0)
    assert decision == v2_inval.CONTINUE


def test_committed_final_response_finishes_not_replans():
    messages = [_u("m1", 101.0), _u("m3", 105.0)]  # new msg exists
    decision = v2_inval.evaluate(
        messages, safe_point="before_final_response",
        coalesced_cursor_ts=102.0, final_response_committed=True)
    assert decision == v2_inval.FINISH   # default: never abort a useful in-flight reply


def test_unknown_safe_point_raises():
    # "mid_action" / any non-safe-point name must raise — replan is only ever
    # evaluated at the three defined safe points, never mid-action / mid-mutation.
    try:
        v2_inval.evaluate([], safe_point="not_a_point", coalesced_cursor_ts=0.0)
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        v2_inval.evaluate([], safe_point="mid_action", coalesced_cursor_ts=0.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_invalidate_delegates_to_jobs_store(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        jobs_store, "invalidate_pending_actions",
        lambda job_id, *, by_job_id: calls.update(job_id=job_id, by=by_job_id) or 3)
    n = v2_inval.invalidate(42, replan_job_id=42)
    assert n == 3
    assert calls == {"job_id": 42, "by": 42}


def test_replan_budget_allows_replan_under_budget():
    messages = [_u("m1", 101.0), _u("m3", 105.0)]
    decision = v2_inval.evaluate(
        messages, safe_point="after_reads", coalesced_cursor_ts=102.0,
        replan_count=1, replan_budget=2)
    assert decision == v2_inval.REPLAN


def test_replan_budget_caps_loop_at_limit():
    # Message flood: a new message is always present, but the job has already
    # replanned up to its budget — must stop replanning (CONTINUE) rather than
    # replan forever.
    messages = [_u("m1", 101.0), _u("m3", 105.0)]
    decision = v2_inval.evaluate(
        messages, safe_point="after_reads", coalesced_cursor_ts=102.0,
        replan_count=2, replan_budget=2)
    assert decision == v2_inval.CONTINUE


def test_replan_budget_default_matches_two():
    assert v2_inval.DEFAULT_REPLAN_BUDGET == 2
    messages = [_u("m1", 101.0), _u("m3", 105.0)]
    # replan_count omitted -> defaults to 0, well under DEFAULT_REPLAN_BUDGET -> REPLAN
    decision = v2_inval.evaluate(messages, safe_point="after_reads", coalesced_cursor_ts=102.0)
    assert decision == v2_inval.REPLAN


def test_multiple_replans_stay_within_same_job_no_competing_job(monkeypatch):
    """Within-job replan: invalidate() must always be called with replan_job_id ==
    job_id itself (the SAME job absorbs replans). single-flight's partial unique
    index forbids a second active job for the same user/lane, so a correct
    invalidation loop never introduces a different job id."""
    seen_calls = []
    monkeypatch.setattr(
        jobs_store, "invalidate_pending_actions",
        lambda job_id, *, by_job_id: seen_calls.append((job_id, by_job_id)) or 1)

    job_id = 7
    for _ in range(3):  # simulate 3 replan iterations within one running job
        v2_inval.invalidate(job_id, replan_job_id=job_id)

    assert seen_calls == [(7, 7), (7, 7), (7, 7)]
    # every by_job_id equals the job's own id -> no competing/other job was ever named
    assert all(job == by for job, by in seen_calls)


def test_safe_points_tuple_excludes_mid_action_points():
    assert v2_inval.SAFE_POINTS == ("after_reads", "before_write", "before_final_response")
    assert "mid_action" not in v2_inval.SAFE_POINTS
    assert "mid_mutation" not in v2_inval.SAFE_POINTS
