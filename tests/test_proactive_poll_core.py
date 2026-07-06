"""Parity for the framework-neutral proactive job poll core (plan §7.5).

Locks the pollable-pending selection, the stale-claim reclaim, the limit clamp,
and the response contract that the Flask route and the forthcoming FastAPI async
poll route both go through (``proactive.poll_core``).
"""

from __future__ import annotations

import base64
import sys
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
