"""Effect dispatch sinks (Hosted Runtime V2 PR A / spec A6).

`serve_worker.build_effect_dispatch(deps)` is the pure router: it maps each of
the 7 outbox effect_types (reply/status/cursor/job/memory/identity/schedule)
to its injected sink callable and raises on anything else. Production sinks
(`serve_worker._sink_*` / `build_production_effect_dispatch`) each wrap the
real write with `db.effect_sink_claim` — the universal exactly-once guard
table (spec A6) — so a replayed dispatch (same effect_id, e.g. after a crash
between the sink write and the outbox row flipping to 'applied') performs the
underlying write exactly once.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 effect sink tests require the PostgreSQL test fixture",
)

_EFFECT_TYPES = ("reply", "status", "cursor", "job", "memory", "identity", "schedule")


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

    deps = serve_worker.EffectSinkDeps(
        reply=claimed_sink,
        status=lambda p: None, cursor=lambda p: None, job=lambda p: None,
        memory=lambda p: None, identity=lambda p: None, schedule=lambda p: None,
    )
    dispatch = serve_worker.build_effect_dispatch(deps)
    payload = {"effect_id": "job1:reply:0", "text": "hi"}
    dispatch("reply", dict(payload))
    dispatch("reply", dict(payload))  # replay after a crash before status=applied
    assert len(writes) == 1
    assert writes[0]["text"] == "hi"


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


def test_production_deps_apply_pending_effects_wired_and_safe_with_empty_outbox(pg_clean):
    seed_user("u_sink_wired")
    deps = serve_worker.build_production_deps()
    assert deps.apply_pending_effects is not None
    assert deps.apply_pending_effects("u_sink_wired") == {"applied": 0, "discarded": 0}
