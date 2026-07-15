"""P3: identity.replace server-build action — high-risk gating (Codex P1).

Full-card overwrite must be usable ONLY inside a live resident-distill job context, never
as a normal agent action. These tests lock the gate; the replace semantics themselves are
covered by the genesis replace_identity_preserving_anchor tests.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
from identity import actions  # noqa: E402
from conftest import seed_user  # noqa: E402

_IDENTITY = {"agent_name": "Nyx", "self_introduction": "hi", "dimensions": [{"name": "warmth", "value": 60}]}


def _ns(uid):
    return types.SimpleNamespace(user_id=uid)


def _run(store, action):
    return actions._execute_identity_action(store, None, action, runtime_token="")


def _live_resident_job(uid, jid="job_idrep"):
    seed_user(uid)
    db.genesis_create_job(uid, {"job_id": jid, "status": "awaiting_resident"})
    db.genesis_claim_resident_jobs(uid, consumer_id="cons-A")   # -> processing, resident-owned
    return jid


def test_replace_rejected_without_distill_context():
    r, _e, st = _run(_ns("u1"), {"type": "identity.replace", "identity": _IDENTITY})
    assert st == 403 and r["error"] == "identity_replace_requires_resident_distill_context"


def test_replace_rejected_when_payload_carries_envelope():
    r, _e, st = _run(_ns("u2"), {"type": "identity.replace", "envelope": {"body_ct": "x"},
                                 "source": "genesis_resident_distill", "job_id": "j", "reason": "r",
                                 "identity": _IDENTITY})
    assert st == 400 and r["error"] == "envelope_not_allowed"


def test_replace_rejected_when_job_not_a_live_resident_job():
    uid = "usr_idrep_nojob"
    seed_user(uid)
    r, _e, st = _run(_ns(uid), {"type": "identity.replace", "source": "genesis_resident_distill",
                                "job_id": "does_not_exist", "reason": "r", "identity": _IDENTITY})
    assert st == 403 and r["error"] == "not_a_live_resident_distill_job"


def test_replace_valid_context_without_identity_remains_409():
    # Cloud plaintext uploads now create-or-update through the shared service
    # helper. Resident identity.replace remains replace-only and must stop at its
    # action-layer no-card guard before the helper can create an identity.
    uid = "usr_idrep_ok"
    jid = _live_resident_job(uid)
    r, _e, st = _run(_ns(uid), {"type": "identity.replace", "source": "genesis_resident_distill",
                                "job_id": jid, "reason": "redefine persona", "identity": _IDENTITY})
    assert st == 409 and r["error"] == "identity_not_initialized"


def test_replace_in_supported_list_on_unknown_action():
    r, _e, st = _run(_ns("u3"), {"type": "identity.bogus"})
    assert st == 400
    assert "identity.replace" in r["supported"]


def test_replace_rejects_runtime_label_agent_name():
    # Task 5: full-card replace now goes through card_policy.validate_full_identity_card
    # — a runtime-label agent_name must be rejected before it ever reaches
    # genesis_service.replace_identity_preserving_anchor.
    uid = "usr_idrep_runtime_label"
    jid = _live_resident_job(uid)
    bad_identity = {"agent_name": "Claude", "self_introduction": "hi",
                     "dimensions": [{"name": "warmth", "value": 60}]}
    r, _e, st = _run(_ns(uid), {"type": "identity.replace", "source": "genesis_resident_distill",
                                "job_id": jid, "reason": "redefine persona", "identity": bad_identity})
    assert st == 400 and r["error"] == "agent_name_is_runtime_label"


def test_replace_accepts_sparse_two_dimension_card_and_reaches_replace():
    # Contract B: a sparse (2-dimension) card is a legal shape and must NOT be
    # rejected by card_policy. Proven the same way as
    # test_replace_valid_context_without_identity_remains_409: the user has no
    # initialized identity yet, so a card that clears card_policy reaches the
    # resident no-card guard and returns identity_not_initialized (409) — a
    # card_policy rejection would instead surface as 400.
    uid = "usr_idrep_sparse_ok"
    jid = _live_resident_job(uid)
    sparse_identity = {"agent_name": "Nyx", "dimensions": [
        {"name": "warmth", "value": 60}, {"name": "wit", "value": 40}]}
    r, _e, st = _run(_ns(uid), {"type": "identity.replace", "source": "genesis_resident_distill",
                                "job_id": jid, "reason": "redefine persona", "identity": sparse_identity})
    assert st == 409 and r["error"] == "identity_not_initialized"
