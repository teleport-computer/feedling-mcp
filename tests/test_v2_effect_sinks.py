"""Effect dispatch sinks (Hosted Runtime V2 PR A / spec A6).

`serve_worker.build_effect_dispatch(deps)` is the pure router: it maps each of
the 8 outbox effect_types (reply/status/cursor/job/memory/identity/schedule/workspace)
to its injected sink callable and raises on anything else. Production sinks
(`serve_worker._sink_*` / `build_production_effect_dispatch`) wrap generic
writes in the two-phase sink ledger: claim, durable write, complete. A replay
of a completed effect no-ops; an interrupted claim fails visibly as delivery-
uncertain instead of silently skipping or blindly duplicating the write.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import effect_outbox
from model_api_runtime.v2 import serve_worker

from conftest import seed_user, set_v2_runtime_owner

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 effect sink tests require the PostgreSQL test fixture",
)

_EFFECT_TYPES = (
    "reply", "status", "cursor", "job", "memory", "identity", "schedule", "workspace",
)


@pytest.fixture
def pg_clean():
    """Truncate the tables this module's tests touch so rows from one test
    (or another module sharing the session-scoped DB) never leak into the
    next — mirrors tests/test_v2_effect_outbox.py's pg_clean rationale."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_effect_sink_applied, v2_effect_outbox, v2_runtime_state, "
            "agent_jobs, user_blobs CASCADE"
        )
    yield


def _fake_deps(calls: dict) -> serve_worker.EffectSinkDeps:
    def make_sink(name):
        def _sink(payload):
            calls.setdefault(name, []).append(payload)
        return _sink

    return serve_worker.EffectSinkDeps(**{t: make_sink(t) for t in _EFFECT_TYPES})


def test_build_effect_dispatch_routes_each_effect_type_to_its_own_sink():
    calls: dict = {}
    dispatch = serve_worker.build_effect_dispatch(_fake_deps(calls))
    for t in _EFFECT_TYPES:
        dispatch(t, {"effect_id": f"eid-{t}", "marker": t})
    # Each sink saw exactly its own effect_type's payload, exactly once — no
    # cross-talk between routes.
    for t in _EFFECT_TYPES:
        assert calls[t] == [{"effect_id": f"eid-{t}", "marker": t}]
    assert sum(len(v) for v in calls.values()) == len(_EFFECT_TYPES)


def test_build_effect_dispatch_unknown_effect_type_raises():
    dispatch = serve_worker.build_effect_dispatch(_fake_deps({}))
    with pytest.raises(ValueError):
        dispatch("bogus_effect_type", {"effect_id": "x"})


def test_replay_same_effect_id_performs_underlying_write_once(pg_clean):
    # A production-shaped sink: guard the real write with db.effect_sink_claim,
    # exactly the pattern every _sink_* in serve_worker.py follows.
    writes: list[dict] = []

    def claimed_sink(payload):
        if not db.effect_sink_claim(payload["effect_id"]):
            return
        writes.append(payload)
        db.effect_sink_complete(payload["effect_id"])

    deps = serve_worker.EffectSinkDeps(
        reply=claimed_sink,
        status=lambda p: None, cursor=lambda p: None, job=lambda p: None,
        memory=lambda p: None, identity=lambda p: None, schedule=lambda p: None,
        workspace=lambda p: None,
    )
    dispatch = serve_worker.build_effect_dispatch(deps)
    payload = {"effect_id": "job1:reply:0", "text": "hi"}
    dispatch("reply", dict(payload))
    dispatch("reply", dict(payload))  # replay after a crash before status=applied
    assert len(writes) == 1
    assert writes[0]["text"] == "hi"


def test_late_release_cannot_erase_completed_sink_marker(pg_clean):
    eid = "job1:status:completed-release-guard"
    assert db.effect_sink_claim(eid) is True
    db.effect_sink_complete(eid)

    db.effect_sink_release(eid)

    assert _sink_claim_state(eid) == "completed"
    assert db.effect_sink_claim(eid) is False


def test_job_sink_fence_skips_enqueue_when_generation_advanced(pg_clean, monkeypatch):
    seed_user("u_sink_job")
    db.get_runtime_generation("u_sink_job")  # lazily inits generation at 1
    db.advance_runtime_state("u_sink_job", from_state="resident", to_state="draining")
    db.advance_runtime_state("u_sink_job", from_state="draining", to_state="v2")  # generation -> 3

    calls = []
    monkeypatch.setattr(
        jobs_store, "enqueue_job", lambda *a, **k: calls.append((a, k)))

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_job")
    dispatch("job", {
        "effect_id": "job9:job:0", "lane": "chat", "expected_generation": 1,
    })
    assert calls == []
    assert _sink_claim_state("job9:job:0") == "completed"


def test_job_sink_enqueues_when_generation_still_current(pg_clean, monkeypatch):
    seed_user("u_sink_job2")
    gen = db.get_runtime_generation("u_sink_job2")  # 1, no cutover yet

    calls = []
    monkeypatch.setattr(
        jobs_store, "enqueue_job", lambda *a, **k: calls.append((a, k)))

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_job2")
    dispatch("job", {
        "effect_id": "job10:job:0", "lane": "chat", "reason": "follow_up",
        "expected_generation": gen,
    })
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ("u_sink_job2", "chat")
    assert kwargs.get("reason") == "follow_up"
    assert kwargs.get("expected_generation") == gen
    assert _sink_claim_state("job10:job:0") == "completed"


def test_job_effect_dispatch_does_not_relock_outer_runtime_row(pg_clean):
    """apply_pending_effects holds runtime_state FOR UPDATE across dispatch.
    The job sink must pass that validated generation into enqueue_job instead
    of asking a second connection to lock the same row."""
    uid = "u_sink_job_outer_lock"
    seed_user(uid)
    set_v2_runtime_owner(uid)
    generation = db.get_runtime_generation(uid)
    assert db.effect_enqueue(
        "job-outer-lock:job:0",
        uid,
        1010,
        "job",
        generation,
        {
            "lane": "maintenance",
            "reason": "outer_lock_regression",
            "expected_generation": generation,
        },
    )
    dispatch = serve_worker.build_production_effect_dispatch(uid)

    result = effect_outbox.apply_pending_effects(uid, dispatch=dispatch)

    assert result == {"applied": 1, "discarded": 0}
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT lane,status,expected_runtime_generation FROM agent_jobs "
            "WHERE user_id=%s",
            (uid,),
        ).fetchone()
    assert row == ("maintenance", "pending", generation)


def test_cursor_sink_is_monotonic_across_out_of_order_effects(pg_clean):
    uid = "u_sink_cursor_monotonic"
    seed_user(uid)
    db.set_blob_strict(
        uid, "model_api_runtime", {"hosted_runtime_mode": "db_action_v2"},
    )
    dispatch = serve_worker.build_production_effect_dispatch(uid)

    dispatch("cursor", {"effect_id": "job20:cursor:0", "new_seq": 30})
    dispatch("cursor", {"effect_id": "job19:cursor:0", "new_seq": 10})

    profile = db.get_blob_strict(uid, "model_api_runtime")
    assert profile[serve_worker.v2_cursor.CURSOR_KEY] == 30
    assert profile["hosted_runtime_mode"] == "db_action_v2"


def test_production_deps_apply_pending_effects_wired_and_safe_with_empty_outbox(pg_clean):
    seed_user("u_sink_wired")
    deps = serve_worker.build_production_deps()
    assert deps.apply_pending_effects is not None
    assert deps.apply_pending_effects("u_sink_wired") == {"applied": 0, "discarded": 0}


def _sink_applied_row_exists(effect_id: str) -> bool:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM v2_effect_sink_applied WHERE effect_id=%s", (effect_id,)
        ).fetchone()
    return row is not None


def _sink_claim_state(effect_id: str) -> str | None:
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT claim_state FROM v2_effect_sink_applied WHERE effect_id=%s",
            (effect_id,),
        ).fetchone()
    return str(row[0]) if row else None


# ------------------------------------------------------------------
# BUG-1: reply-sink must not silently swallow an envelope-build failure
# (v2_worker._write_encrypted_reply returns None, does not raise, on
# failure — see its docstring). Restores the deleted
# test_reply_envelope_failure_is_terminal_not_success invariant on the
# new PR A effect-sink path.
# ------------------------------------------------------------------

def test_sink_reply_raises_and_releases_claim_when_envelope_build_fails(pg_clean, monkeypatch):
    seed_user("u_sink_reply_fail")
    monkeypatch.setattr(serve_worker.v2_worker, "_write_encrypted_reply", lambda store, text: None)

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_reply_fail")
    eid = "job_reply_fail:reply:0"
    with pytest.raises(RuntimeError, match="reply envelope build failed"):
        dispatch("reply", {"effect_id": eid, "text": "hello"})

    # The claim must be released -- no surviving row -- so a replay (the outbox
    # applier re-driving this pending effect) can re-attempt the write instead
    # of a permanently-orphaned claim silently no-oping forever.
    assert not _sink_applied_row_exists(eid)


def test_sink_reply_succeeds_and_keeps_claim_when_write_succeeds(pg_clean, monkeypatch):
    seed_user("u_sink_reply_ok")
    monkeypatch.setattr(
        serve_worker.v2_worker, "_write_encrypted_reply", lambda store, text: {"id": "r1"})

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_reply_ok")
    eid = "job_reply_ok:reply:0"
    dispatch("reply", {"effect_id": eid, "text": "hello"})  # must not raise

    assert _sink_applied_row_exists(eid)
    assert _sink_claim_state(eid) == "completed"


@pytest.mark.parametrize(
    ("effect_type", "payload", "failure_code"),
    [
        (
            "identity",
            {"patch": {"signature": "new"}},
            "identity_patch_failed",
        ),
        (
            "schedule",
            {"op": "schedule_wake", "at": "2026-07-15T09:00:00Z"},
            "schedule_wake_failed",
        ),
    ],
)
def test_capability_sink_result_failure_releases_claim_and_raises_stable_code(
    pg_clean, monkeypatch, effect_type, payload, failure_code
):
    uid = f"u_sink_cap_failure_{effect_type}"
    seed_user(uid)
    eid = f"job_cap_failure:{effect_type}:0"

    def failed_capability(*_args, **_kwargs):
        from capabilities.types import err
        return err("upstream", "secret provider response", retryable=True)

    monkeypatch.setattr(
        serve_worker.cap_registry, "run_capability", failed_capability
    )
    dispatch = serve_worker.build_production_effect_dispatch(
        uid, runtime_token_provider=lambda: "rt-test")

    with pytest.raises(RuntimeError, match=f"^{failure_code}$") as caught:
        dispatch(effect_type, {"effect_id": eid, **payload})

    assert "secret provider response" not in str(caught.value)
    assert _sink_claim_state(eid) is None


def test_identity_sink_forwards_enclave_runtime_token(pg_clean, monkeypatch):
    uid = "u_sink_identity_runtime_token"
    seed_user(uid)
    seen = []

    def fake_run_capability(
        action_type, store, *, api_key=None, runtime_token=None, params=None
    ):
        from capabilities.types import ok

        seen.append((action_type, store.user_id, api_key, runtime_token, params))
        return ok(data={})

    monkeypatch.setattr(
        serve_worker.cap_registry, "run_capability", fake_run_capability
    )
    dispatch = serve_worker.build_production_effect_dispatch(
        uid,
        runtime_token_provider=lambda: "rt-envelope-decrypt",
    )
    dispatch("identity", {
        "effect_id": "job_identity:identity:0",
        "patch": {"signature": "new"},
    })

    assert seen == [(
        "identity_patch",
        uid,
        None,
        "rt-envelope-decrypt",
        {"patch": {"signature": "new"}},
    )]
    assert _sink_claim_state("job_identity:identity:0") == "completed"


# ------------------------------------------------------------------
# Codex C1: one `identity` effect_type, two ops. A trusted `op` (missing =>
# legacy identity_patch; "identity_nudge" => nudge) selects the capability;
# op and effect_id are stripped from the forwarded params; an unknown op is
# fail-closed (terminal discard), never silently applied as a patch.
# ------------------------------------------------------------------

def _record_run_capability(seen):
    def fake_run_capability(action_type, store, *, api_key=None, runtime_token=None, params=None):
        from capabilities.types import ok
        seen.append((action_type, store.user_id, params))
        return ok(data={})
    return fake_run_capability


def test_identity_sink_routes_nudge_op_to_identity_nudge_capability(pg_clean, monkeypatch):
    seed_user("u_sink_id_nudge")
    seen = []
    monkeypatch.setattr(serve_worker.cap_registry, "run_capability", _record_run_capability(seen))
    dispatch = serve_worker.build_production_effect_dispatch(
        "u_sink_id_nudge", runtime_token_provider=lambda: "rt")
    dispatch("identity", {
        "effect_id": "job_idn:identity:0",
        "op": "identity_nudge", "dimension": "trust", "delta": 3, "reason": "kept a promise",
    })
    assert seen == [(
        "identity_nudge", "u_sink_id_nudge",
        {"dimension": "trust", "delta": 3, "reason": "kept a promise"},
    )]
    assert _sink_claim_state("job_idn:identity:0") == "completed"


def test_identity_sink_without_op_routes_to_identity_patch(pg_clean, monkeypatch):
    """Legacy/in-flight identity effects carry NO op and must keep routing to
    identity_patch — the byte-for-byte shape from before the op key existed."""
    seed_user("u_sink_id_legacy")
    seen = []
    monkeypatch.setattr(serve_worker.cap_registry, "run_capability", _record_run_capability(seen))
    dispatch = serve_worker.build_production_effect_dispatch(
        "u_sink_id_legacy", runtime_token_provider=lambda: "rt")
    dispatch("identity", {"effect_id": "job_idl:identity:0", "patch": {"signature": "kind"}})
    assert seen == [("identity_patch", "u_sink_id_legacy", {"patch": {"signature": "kind"}})]
    assert _sink_claim_state("job_idl:identity:0") == "completed"


def test_identity_sink_unknown_op_terminal_discards_not_patch(pg_clean, monkeypatch):
    seed_user("u_sink_id_bad")
    monkeypatch.setattr(
        serve_worker.cap_registry, "run_capability",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unknown op must not run a capability")))
    dispatch = serve_worker.build_production_effect_dispatch(
        "u_sink_id_bad", runtime_token_provider=lambda: "rt")
    with pytest.raises(db.EffectTerminalError, match="identity_operation_invalid"):
        dispatch("identity", {"effect_id": "job_idb:identity:0", "op": "identity_wipe"})
    # Claim released (not completed) so the terminal-discard bookkeeping runs.
    assert _sink_claim_state("job_idb:identity:0") is None


def test_memory_sink_forwards_enclave_runtime_token(pg_clean, monkeypatch):
    uid = "u_sink_memory_runtime_token"
    seed_user(uid)
    seen = []

    def fake_actions(store, api_key, payload, *, runtime_token=""):
        seen.append((store.user_id, api_key, payload, runtime_token))
        return {"results": [{"status": "ok"}]}, 200

    monkeypatch.setattr(serve_worker.memory_core, "actions", fake_actions)
    dispatch = serve_worker.build_production_effect_dispatch(
        uid,
        runtime_token_provider=lambda: "rt-envelope-decrypt",
    )
    actions = [{
        "type": "memory.add",
        "memory": {"type": "fact", "title": "tea", "description": "likes tea"},
    }]
    dispatch("memory", {
        "effect_id": "job_memory:memory:0",
        "actions": actions,
    })

    assert seen == [(
        uid,
        None,
        {"actions": actions},
        "rt-envelope-decrypt",
    )]
    assert _sink_claim_state("job_memory:memory:0") == "completed"


# ------------------------------------------------------------------
# BUG-3: the `schedule` effect_type has two producers sharing one sink.
# schedule_wake/cancel_wake write-tool-calls carry capability params
# (at/tz/reason/wake_id) under payload["op"] and must route through
# cap_registry.run_capability -- NOT jobs_store.upsert_wake_schedule,
# whose kwargs (_SCHEDULE_PAYLOAD_KEYS) are an unrelated PR A/D
# wake-timing-table shape that silently drops every capability param.
# ------------------------------------------------------------------

def test_sink_schedule_routes_schedule_wake_op_through_run_capability(pg_clean, monkeypatch):
    seed_user("u_sink_sched_wake")
    calls = []

    def fake_run_capability(action_type, store, *, api_key=None, runtime_token=None, params=None):
        calls.append((action_type, store.user_id, api_key, params))
        from capabilities.types import ok
        return ok(data={"results": [{
            "type": "schedule_wake_result",
            "status": "scheduled",
            "timer_id": "sched_real_1",
            "next_trigger_at": "2026-07-11T17:00:00",
            "timezone": "Asia/Shanghai",
        }]})

    monkeypatch.setattr(serve_worker.cap_registry, "run_capability", fake_run_capability)
    monkeypatch.setattr(
        serve_worker.jobs_store, "upsert_wake_schedule",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch wake-timing table")))

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_sched_wake")
    applied = dispatch("schedule", {
        "effect_id": "job_sw:schedule:0", "op": "schedule_wake",
        "at": "2026-07-11T09:00:00Z", "tz": "Asia/Shanghai", "reason": "remind me",
    })

    assert len(calls) == 1
    action_type, user_id, api_key, params = calls[0]
    assert action_type == "schedule_wake"
    assert user_id == "u_sink_sched_wake"
    assert api_key is None
    assert params == {
        "at": "2026-07-11T09:00:00Z", "tz": "Asia/Shanghai", "reason": "remind me",
    }
    assert applied.result == {
        "kind": "schedule_v1",
        "operation": "schedule_wake",
        "status": "scheduled",
        "task_id": "sched_real_1",
        "next_trigger_at": "2026-07-11T17:00:00",
        "timezone": "Asia/Shanghai",
    }


def test_sink_schedule_routes_cancel_wake_op_through_run_capability(pg_clean, monkeypatch):
    seed_user("u_sink_sched_cancel")
    calls = []

    def fake_run_capability(action_type, store, *, api_key=None, runtime_token=None, params=None):
        calls.append((action_type, params))
        from capabilities.types import ok
        return ok(data={})

    monkeypatch.setattr(serve_worker.cap_registry, "run_capability", fake_run_capability)

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_sched_cancel")
    dispatch("schedule", {
        "effect_id": "job_cw:schedule:0", "op": "cancel_wake", "wake_id": "w1",
    })

    assert calls == [("cancel_wake", {"wake_id": "w1"})]


def test_sink_schedule_without_op_still_upserts_wake_schedule(pg_clean, monkeypatch):
    """PR A/D producers (_fire_scheduled_for_user/_tick_screen_watch_for_user) never
    set payload["op"] -- their _SCHEDULE_PAYLOAD_KEYS-filtered upsert path must be
    unchanged by the BUG-3 fix."""
    seed_user("u_sink_sched_timing")
    calls = []
    monkeypatch.setattr(
        serve_worker.jobs_store, "upsert_wake_schedule",
        lambda user_id, **kwargs: calls.append((user_id, kwargs)))
    monkeypatch.setattr(
        serve_worker.cap_registry, "run_capability",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call run_capability")))

    dispatch = serve_worker.build_production_effect_dispatch("u_sink_sched_timing")
    dispatch("schedule", {
        "effect_id": "job_timing:schedule:0", "next_heartbeat_at": 123.0,
    })

    assert calls == [("u_sink_sched_timing", {"next_heartbeat_at": 123.0})]
