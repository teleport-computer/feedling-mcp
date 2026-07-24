import os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from model_api_runtime.v2 import effect_outbox
from conftest import seed_user
pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as c:
        c.execute("TRUNCATE v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs CASCADE")
    yield


def test_enqueue_effect_derives_id_and_inserts(pg_clean):
    seed_user("u_ee1")
    eid = effect_outbox.enqueue_effect(
        job_id=5, user_id="u_ee1", effect_type="reply", ordinal=0,
        expected_generation=1, payload={"text": "hi"})
    assert eid == "job5:reply:0"
    rows = list(db.effect_pending("u_ee1"))
    assert len(rows) == 1 and rows[0]["effect_id"] == eid


def test_enqueue_effect_idempotent_same_id(pg_clean):
    seed_user("u_ee2")
    a = effect_outbox.enqueue_effect(job_id=5, user_id="u_ee2", effect_type="reply",
                                     ordinal=0, expected_generation=1, payload={"text": "x"})
    b = effect_outbox.enqueue_effect(job_id=5, user_id="u_ee2", effect_type="reply",
                                     ordinal=0, expected_generation=1, payload={"text": "y"})
    assert a == b
    assert len(list(db.effect_pending("u_ee2"))) == 1  # second is a no-op
