"""v2_turn_metrics whole-turn metric (Hosted Runtime V2 PR B / spec B5):
`jobs_store.record_whole_turn_metric` upserts ONE idempotent row per job_id —
a re-drive of the same job REPLACES the row (latest wins), and a failed turn
is recorded just as faithfully as a successful one."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from model_api_runtime.v2 import jobs_store
from conftest import seed_user
import os
import pytest
pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


@pytest.fixture()
def pg_clean_metrics():
    """Truncate the tables this module's tests touch so a leftover row from an
    earlier module/run can't be picked up by `claim_next_job`'s global,
    unfiltered claim or pollute a `WHERE job_id=...` assertion here."""
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE v2_turn_metrics, agent_jobs, v2_runtime_state CASCADE")
    yield


def _seed_job(uid):
    seed_user(uid)
    jid, _ = jobs_store.enqueue_job(uid, "chat", reason="t")
    return jid


def test_upsert_is_idempotent_by_job(pg_clean_metrics):
    uid = "u_wtm1"
    jid = _seed_job(uid)
    jobs_store.record_whole_turn_metric(jid, uid, "chat", prompt_tokens=10,
        completion_tokens=5, latency_ms=100, model_calls=2, retries=0, failed=False, status="ok")
    jobs_store.record_whole_turn_metric(jid, uid, "chat", prompt_tokens=30,
        completion_tokens=9, latency_ms=200, model_calls=3, retries=1, failed=False, status="ok")
    with db.get_pool().connection() as c:
        rows = c.execute("SELECT prompt_tokens, model_calls FROM v2_turn_metrics WHERE job_id=%s",
                         (jid,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == 30 and rows[0][1] == 3   # latest wins, one row


def test_failed_turn_is_recorded(pg_clean_metrics):
    uid = "u_wtm2"
    jid = _seed_job(uid)
    jobs_store.record_whole_turn_metric(jid, uid, "chat", prompt_tokens=7,
        completion_tokens=0, latency_ms=50, model_calls=1, retries=0, failed=True, status="provider_error")
    with db.get_pool().connection() as c:
        row = c.execute("SELECT failed, status FROM v2_turn_metrics WHERE job_id=%s", (jid,)).fetchone()
    assert row[0] is True and row[1] == "provider_error"


def test_cache_telemetry_and_provider_identity_are_persisted(pg_clean_metrics):
    uid = "u_wtm_cache"
    jid = _seed_job(uid)
    jobs_store.record_whole_turn_metric(
        jid, uid, "chat",
        prompt_tokens=100,
        completion_tokens=12,
        cache_read_tokens=70,
        cache_write_tokens=10,
        cache_miss_tokens=30,
        usage_reported_calls=2,
        cache_reported_calls=2,
        provider="anthropic",
        model="claude-test",
        cache_route_fingerprint="feedling-v2-route-test",
        latency_ms=50,
        model_calls=2,
        retries=0,
        failed=False,
        status="ok",
    )
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT cache_read_tokens, cache_write_tokens, cache_miss_tokens, "
            "usage_reported_calls, cache_reported_calls, provider, model, "
            "cache_route_fingerprint "
            "FROM v2_turn_metrics WHERE job_id=%s",
            (jid,),
        ).fetchone()
    assert row == (
        70, 10, 30, 2, 2, "anthropic", "claude-test", "feedling-v2-route-test"
    )


def test_token_metrics_accept_values_above_legacy_integer_range(pg_clean_metrics):
    uid = "u_wtm_bigint"
    jid = _seed_job(uid)
    prompt_tokens = (1 << 31) + 7

    jobs_store.record_whole_turn_metric(
        jid, uid, "chat",
        prompt_tokens=prompt_tokens,
        completion_tokens=1,
        latency_ms=50,
        model_calls=1,
        retries=0,
        failed=False,
        status="ok",
    )

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT prompt_tokens, completion_tokens FROM v2_turn_metrics "
            "WHERE job_id=%s",
            (jid,),
        ).fetchone()
    assert row == (prompt_tokens, 1)
