"""Fix B1: a sink raising EffectTerminalError terminal-discards the effect (no
infinite retry/wedge); a plain RuntimeError still retries."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from model_api_runtime.v2 import effect_outbox, effect_id
from conftest import seed_user

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


def _set_v2_owner(uid):
    with db.get_pool().connection() as c:
        c.execute("UPDATE v2_runtime_state SET hosted_runtime_state='v2' WHERE user_id=%s", (uid,))


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as c:
        c.execute("TRUNCATE v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs CASCADE")
    yield


def test_terminal_error_discards_effect_no_retry(pg_clean):
    uid = "u_terminal"
    seed_user(uid)
    db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=7, effect_type="identity_encrypted_v1", ordinal=0)
    db.effect_enqueue(eid, uid, 7, "identity_encrypted_v1", 1, {"patch": {"self_introduction": "x"}})

    def _dispatch(_t, _p):
        raise db.EffectTerminalError("identity_patch_failed")

    # must NOT raise — a terminal discard is a handled outcome
    res = effect_outbox.apply_pending_effects(uid, dispatch=_dispatch)
    assert res == {"applied": 0, "discarded": 1}

    with db.get_pool().connection() as c:
        status, attempt, last_error = c.execute(
            "SELECT status, attempt_count, last_error FROM v2_effect_outbox WHERE effect_id=%s",
            (eid,)).fetchone()
    assert status == "discarded"
    assert attempt == 0                            # not retried
    assert "EffectTerminalError" in last_error     # sanitized to the exception class
    assert db.effect_pending(uid) == []            # gone from the work-list, no wedge


def test_plain_runtime_error_still_retries(pg_clean):
    uid = "u_retry"
    seed_user(uid)
    db.get_runtime_generation(uid)
    _set_v2_owner(uid)
    eid = effect_id.derive(job_id=8, effect_type="identity_encrypted_v1", ordinal=0)
    db.effect_enqueue(eid, uid, 8, "identity_encrypted_v1", 1, {"patch": {}})

    def _dispatch(_t, _p):
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError, match="transient"):
        effect_outbox.apply_pending_effects(uid, dispatch=_dispatch)

    with db.get_pool().connection() as c:
        status, attempt = c.execute(
            "SELECT status, attempt_count FROM v2_effect_outbox WHERE effect_id=%s", (eid,)).fetchone()
    assert status == "pending" and attempt == 1   # retryable path unchanged
