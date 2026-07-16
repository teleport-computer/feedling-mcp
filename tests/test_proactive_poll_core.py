"""Parity for the framework-neutral proactive job poll core (plan §7.5).

Locks the pollable-pending selection, the stale-claim reclaim, the limit clamp,
and the response contract that the Flask route and the forthcoming FastAPI async
poll route both go through (``proactive.poll_core``).
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from proactive import poll_core  # noqa: E402
from proactive import service as proactive_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _chat_envelope(user_id: str, msg_id: str) -> dict:
    return {
        "v": 1,
        "id": msg_id,
        "body_ct": f"ct_{msg_id}",
        "nonce": f"nonce_{msg_id}",
        "K_user": f"k_user_{msg_id}",
        "K_enclave": f"k_enclave_{msg_id}",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "test",
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


# --------------------------------------------------------------------------- #
# limit clamp + response contract
# --------------------------------------------------------------------------- #

def test_clamp_limit_bounds():
    assert poll_core.clamp_limit(0) == poll_core.LIMIT_MIN == 1
    assert poll_core.clamp_limit(9999) == poll_core.LIMIT_MAX == 100
    assert poll_core.clamp_limit(20) == 20
    assert poll_core.LIMIT_DEFAULT == 20


def test_build_response_contract():
    resp = poll_core.build_response(jobs=[{"job_id": "j"}], runtime_profile={"x": 1}, timed_out=False)
    assert set(resp) == {"jobs", "runtime_v2", "timed_out"}
    assert resp["jobs"] == [{"job_id": "j"}]
    assert resp["runtime_v2"] == {"x": 1}
    assert resp["timed_out"] is False


# --------------------------------------------------------------------------- #
# pollable pending selection
# --------------------------------------------------------------------------- #

def test_intro_job_is_pollable_and_carries_runtime_profile(store):
    # An introduction job is ts-watermark exempt: it is returned even with the
    # consumer's `since` watermark ahead of the job ts, and it skips the wake
    # gate. Each returned job carries the runtime profile.
    store.append_proactive_job({
        "job_id": "pj_intro",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "job_kind": "introduction",
        "ts": 1.0,
        "status": "pending",
    })
    profile = {"owner": "resident_runtime_v2"}
    jobs = poll_core.resident_pollable_pending_jobs(store, since=9999.0, limit=20, runtime_profile=profile)
    ids = [j.get("job_id") for j in jobs]
    assert "pj_intro" in ids
    intro = next(j for j in jobs if j.get("job_id") == "pj_intro")
    assert intro["runtime_v2"] == profile


def test_pollable_respects_limit(store):
    for i in range(5):
        store.append_proactive_job({
            "job_id": f"pj_intro_{i}",
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "job_kind": "introduction",
            "ts": 1.0,
            "status": "pending",
        })
    jobs = poll_core.resident_pollable_pending_jobs(store, since=0.0, limit=2, runtime_profile={})
    assert len(jobs) == 2


# --------------------------------------------------------------------------- #
# introduction = self-cancelling deadlock fallback, not the primary greeting
#
# The io-onboarding skill has the resident agent post its OWN Step 6 greeting.
# 976f180/ebe3c52 wired an unconditional backend introduction onto
# chat_loop_verified, which double-greeted every skill-compliant resident (and
# routed that greeting through the proactive→agent→parse path). The job now waits
# out a grace window and retires itself the moment the agent greets.
# --------------------------------------------------------------------------- #

def _append_intro_job(store, *, job_id: str = "pj_intro", ts: float) -> None:
    store.append_proactive_job({
        "job_id": job_id,
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "job_kind": "introduction",
        "ts": ts,
        "status": "pending",
    })


def _poll(store):
    return poll_core.resident_pollable_pending_jobs(
        store, since=0.0, limit=20, runtime_profile={})


def test_intro_deferred_inside_grace_window_so_agent_can_greet_first(store):
    # Fresh job + agent hasn't greeted yet: hold it, don't double-greet.
    _append_intro_job(store, ts=time.time())
    assert [j.get("job_id") for j in _poll(store)] == []


def test_intro_fires_after_grace_when_agent_never_greets(store):
    # The deadlock fallback that 976f180 was protecting must survive.
    _append_intro_job(store, ts=time.time() - poll_core.RESIDENT_INTRO_FALLBACK_GRACE_SEC - 1)
    assert "pj_intro" in [j.get("job_id") for j in _poll(store)]


def test_intro_cancelled_once_agent_posts_its_own_greeting(store):
    _append_intro_job(store, ts=time.time() - poll_core.RESIDENT_INTRO_FALLBACK_GRACE_SEC - 1)
    store.append_chat("agent", "chat", _chat_envelope(store.user_id, "greeting-1"))

    assert [j.get("job_id") for j in _poll(store)] == []
    # durable: never enqueue another introduction for this user
    assert store.introduction_done() is True
    job = next(j for j in store.list_proactive_jobs(limit=100) if j.get("job_id") == "pj_intro")
    assert job["status"] == "skipped"
    assert job["status_reason"] == "agent_greeted"


def test_verify_ping_reply_does_not_count_as_the_agent_greeting(store):
    # Load-bearing: verify_loop's synthetic ping reply must NOT retire the
    # fallback, or a resident whose agent never really greets stays wedged.
    _append_intro_job(store, ts=time.time() - poll_core.RESIDENT_INTRO_FALLBACK_GRACE_SEC - 1)
    store.append_chat("agent", "verify_ping", _chat_envelope(store.user_id, "ping-reply-1"))

    assert "pj_intro" in [j.get("job_id") for j in _poll(store)]
    assert store.introduction_done() is False


def test_grace_window_is_env_tunable(store, monkeypatch):
    monkeypatch.setenv("FEEDLING_RESIDENT_INTRO_FALLBACK_GRACE_SEC", "0")
    _append_intro_job(store, ts=time.time())
    assert "pj_intro" in [j.get("job_id") for j in _poll(store)]


def _append_pending_job(
    store,
    *,
    job_id: str,
    job_kind: str,
    ts: float,
    status: str = "pending",
    **extra,
):
    job = {
        "job_id": job_id,
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "job_kind": job_kind,
        "ts": ts,
        "status": status,
    }
    job.update(extra)
    store.append_proactive_job(job)


def _proactive_jobs_by_id(store) -> dict[str, dict]:
    return {
        str(job.get("job_id") or ""): job
        for job in store.list_proactive_jobs(limit=0)
    }


# --------------------------------------------------------------------------- #
# stale wake expiry
# --------------------------------------------------------------------------- #

def test_stale_wake_expires_before_wake_control(store, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(poll_core.time, "time", lambda: now)
    gate_calls = []
    monkeypatch.setattr(
        poll_core,
        "_resident_wake_control_decision_v2",
        lambda *_args: gate_calls.append(True),
    )
    _append_pending_job(
        store,
        job_id="pj_stale_wake",
        job_kind="heartbeat",
        ts=now - (16 * 60),
    )

    assert _poll(store) == []
    assert gate_calls == []
    job = _proactive_jobs_by_id(store)["pj_stale_wake"]
    assert job["status"] == "expired"
    assert job["status_reason"] == "stale_wake_expired"
    assert job["wake_result"] == "stale_wake_expired"

    updates = []
    monkeypatch.setattr(
        store,
        "update_proactive_job",
        lambda *_args, **_kwargs: updates.append(True),
    )
    assert _poll(store) == []
    assert updates == []


def test_fresh_wake_is_still_pollable(store, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(poll_core.time, "time", lambda: now)
    monkeypatch.setattr(poll_core, "_resident_wake_control_decision_v2", lambda *_args: None)
    _append_pending_job(
        store,
        job_id="pj_fresh_wake",
        job_kind="heartbeat",
        ts=now - (14 * 60),
    )

    assert [job["job_id"] for job in _poll(store)] == ["pj_fresh_wake"]
    assert _proactive_jobs_by_id(store)["pj_fresh_wake"]["status"] == "pending"


def test_wake_max_age_is_env_tunable(store, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(poll_core.time, "time", lambda: now)
    monkeypatch.setenv("FEEDLING_WAKE_JOB_MAX_AGE_SEC", "60")
    _append_pending_job(
        store,
        job_id="pj_env_stale_wake",
        job_kind="heartbeat",
        ts=now - 61,
    )

    assert _poll(store) == []
    assert _proactive_jobs_by_id(store)["pj_env_stale_wake"]["status"] == "expired"


def test_old_watermark_exempt_jobs_do_not_expire(store, monkeypatch):
    now = 10_000.0
    monkeypatch.setattr(poll_core.time, "time", lambda: now)
    old_ts = now - (16 * 60)
    _append_intro_job(store, job_id="pj_old_intro", ts=old_ts)
    for job_id, job_kind in (
        ("pj_old_capture", "memory_capture"),
        ("pj_old_dream", "memory_dream"),
        ("pj_old_migrate", "memory_migrate"),
    ):
        _append_pending_job(store, job_id=job_id, job_kind=job_kind, ts=old_ts)

    assert {job["job_id"] for job in _poll(store)} == {
        "pj_old_intro",
        "pj_old_capture",
        "pj_old_dream",
        "pj_old_migrate",
    }
    assert all(job["status"] == "pending" for job in _proactive_jobs_by_id(store).values())


# --------------------------------------------------------------------------- #
# memory maintenance latest-only selection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("job_kind", ["memory_dream", "memory_migrate"])
def test_dream_and_migrate_only_poll_latest_pending_by_ts(store, job_kind):
    # Append out of timestamp order so selection cannot accidentally rely on seq.
    _append_pending_job(store, job_id=f"{job_kind}_newest", job_kind=job_kind, ts=300.0)
    _append_pending_job(
        store,
        job_id=f"{job_kind}_oldest",
        job_kind=job_kind,
        ts=100.0,
        claimed_at="9999.0",
    )
    _append_pending_job(store, job_id=f"{job_kind}_middle", job_kind=job_kind, ts=200.0)

    assert [job["job_id"] for job in _poll(store)] == [f"{job_kind}_newest"]
    jobs = _proactive_jobs_by_id(store)
    assert jobs[f"{job_kind}_newest"]["status"] == "pending"
    for suffix in ("oldest", "middle"):
        old = jobs[f"{job_kind}_{suffix}"]
        assert old["status"] == "skipped"
        assert old["status_reason"] == "superseded_by_newer"


def test_all_pending_memory_capture_jobs_remain_pollable(store):
    for index in range(3):
        _append_pending_job(
            store,
            job_id=f"memory_capture_{index}",
            job_kind="memory_capture",
            ts=100.0 + index,
        )

    assert [job["job_id"] for job in _poll(store)] == [
        "memory_capture_0",
        "memory_capture_1",
        "memory_capture_2",
    ]
    assert all(job["status"] == "pending" for job in _proactive_jobs_by_id(store).values())


def test_latest_only_selection_is_independent_per_maintenance_kind(store):
    for job_id, job_kind, ts in (
        ("dream_old", "memory_dream", 100.0),
        ("migrate_new", "memory_migrate", 400.0),
        ("capture_fixed", "memory_capture", 150.0),
        ("dream_new", "memory_dream", 300.0),
        ("migrate_old", "memory_migrate", 200.0),
    ):
        _append_pending_job(store, job_id=job_id, job_kind=job_kind, ts=ts)

    assert {job["job_id"] for job in _poll(store)} == {
        "dream_new",
        "migrate_new",
        "capture_fixed",
    }
    jobs = _proactive_jobs_by_id(store)
    assert jobs["dream_old"]["status_reason"] == "superseded_by_newer"
    assert jobs["migrate_old"]["status_reason"] == "superseded_by_newer"


def test_latest_only_selection_does_not_touch_claimed_or_realizing(store):
    _append_pending_job(store, job_id="dream_pending", job_kind="memory_dream", ts=100.0)
    _append_pending_job(
        store,
        job_id="dream_claimed",
        job_kind="memory_dream",
        ts=200.0,
        status="claimed",
    )
    _append_pending_job(
        store,
        job_id="dream_realizing",
        job_kind="memory_dream",
        ts=300.0,
        status="realizing",
    )

    assert [job["job_id"] for job in _poll(store)] == ["dream_pending"]
    jobs = _proactive_jobs_by_id(store)
    assert jobs["dream_claimed"]["status"] == "claimed"
    assert jobs["dream_realizing"]["status"] == "realizing"


# --------------------------------------------------------------------------- #
# stale-claim reclaim
# --------------------------------------------------------------------------- #

def test_reclaim_recovers_stale_resident_claim(store):
    now = 10_000.0
    store.append_proactive_job({
        "job_id": "pj_stale",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1.0,
        "status": "claimed",
        "consumer_id": "resident-a",
        "claimed_at": str(now - poll_core.RESIDENT_WAKE_LEASE_SEC - 1),
    })
    reclaimed = poll_core.reclaim_stale_resident_jobs(store, now=now)
    assert reclaimed == 1
    job = next(j for j in store.list_proactive_jobs(limit=100) if j.get("job_id") == "pj_stale")
    assert job["status"] == "pending"


def test_reclaim_leaves_fresh_and_hosted_claims_alone(store):
    now = 10_000.0
    store.append_proactive_job({
        "job_id": "pj_fresh",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1.0,
        "status": "claimed",
        "consumer_id": "resident-a",
        "claimed_at": str(now - 1),  # well within the lease
    })
    store.append_proactive_job({
        "job_id": "pj_hosted",
        "source": proactive_service.PROACTIVE_JOB_SOURCE,
        "ts": 1.0,
        "status": "claimed",
        "consumer_id": "hosted_runtime",  # hosted consumers manage their own lease
        "claimed_at": str(now - poll_core.RESIDENT_WAKE_LEASE_SEC - 1),
    })
    reclaimed = poll_core.reclaim_stale_resident_jobs(store, now=now)
    assert reclaimed == 0
    statuses = {j.get("job_id"): j.get("status") for j in store.list_proactive_jobs(limit=100)}
    assert statuses["pj_fresh"] == "claimed"
    assert statuses["pj_hosted"] == "claimed"
