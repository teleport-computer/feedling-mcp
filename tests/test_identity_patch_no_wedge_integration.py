"""Fix A+B integration: the wedge scenario end to end.

A fresh-start V2 user (no identity card) whose model calls identity_patch used to
wedge — the `identity` effect failed with a non-retryable 409
(identity_not_initialized) and the outbox retried it forever
(→ needs_reconciliation). With Fix A the capability now CREATES the card, so the
effect APPLIES on the first pass; the reply is delivered and no reconciliation row
lingers. (If bootstrap ever declined, Fix B would terminal-discard instead — either
way, never a wedge.)
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from model_api_runtime.v2 import effect_id  # noqa: E402
from model_api_runtime.v2 import serve_worker as sw  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as c:
        c.execute("TRUNCATE v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs CASCADE")
    yield


def _fake_envelope(store, plaintext: bytes, item_id=None):
    return {
        "id": item_id or "env_e2e",
        "body_ct": "ct", "nonce": "nonce", "K_user": "k_user",
        "K_enclave": "k_enclave", "visibility": "shared",
        "owner_user_id": store.user_id, "enclave_pk_fpr": "test",
    }, ""


def test_identity_patch_no_wedge_end_to_end(pg_clean, monkeypatch):
    uid = "u_boot_e2e"
    seed_user(uid)
    gen = db.get_runtime_generation(uid)
    with db.get_pool().connection() as c:
        c.execute("UPDATE v2_runtime_state SET hosted_runtime_state='v2' WHERE user_id=%s", (uid,))

    # fresh-start: no identity card → enclave GET has no identity dict.
    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate",
                        lambda *a, **k: ({}, ""))
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope)
    monkeypatch.setattr(sw, "_mint_runtime_token", lambda user_id: "rtok_test")

    assert db.get_blob(uid, "identity") is None

    eid = effect_id.derive(job_id=1, effect_type="identity", ordinal=0)
    db.effect_enqueue(eid, uid, 1, "identity", gen,
                      {"patch": {"self_introduction": "hi, I'm your agent."}})

    res = sw._apply_pending_effects_for_user(uid)

    # Applied, not wedged: card created, effect terminalized as 'applied', nothing
    # left pending, no reconciliation loop.
    assert res == {"applied": 1, "discarded": 0}
    assert db.get_blob(uid, "identity") is not None
    with db.get_pool().connection() as c:
        status, = c.execute(
            "SELECT status FROM v2_effect_outbox WHERE effect_id=%s", (eid,)).fetchone()
        recon = c.execute(
            "SELECT count(*) FROM v2_effect_outbox WHERE user_id=%s AND status='needs_reconciliation'",
            (uid,)).fetchone()[0]
    assert status == "applied"
    assert recon == 0
    assert db.effect_pending(uid) == []
