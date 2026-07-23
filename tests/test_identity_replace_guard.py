"""Regression guard: identity.replace context gate (403) must not regress.

Locks the gate: without resident-distill context (source + job_id + reason),
identity.replace returns 403. With distill context fully specified, it passes
the context guard and reaches downstream checks (which may fail with 400/409,
but 403 means the gate itself broke).

The gate is HIGH-RISK (Codex P1): identity.replace must NEVER be a normal agent
action — it must only run inside a live resident-distill job context.
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


class TestIdentityReplaceContextGuard:
    """Tests that lock the 403 gate on identity.replace without resident-distill context."""

    def test_context_guard_missing_source(self):
        """Without source, returns 403 context-guard error."""
        r, _e, st = _run(_ns("u_guard_nosrc"), {
            "type": "identity.replace",
            "job_id": "j1",
            "reason": "test",
            "identity": _IDENTITY
        })
        assert st == 403
        assert r["error"] == "identity_replace_requires_resident_distill_context"

    def test_context_guard_wrong_source(self):
        """With non-resident source, returns 403 context-guard error."""
        r, _e, st = _run(_ns("u_guard_wrongsrc"), {
            "type": "identity.replace",
            "source": "cloud_api",
            "job_id": "j1",
            "reason": "test",
            "identity": _IDENTITY
        })
        assert st == 403
        assert r["error"] == "identity_replace_requires_resident_distill_context"

    def test_context_guard_missing_job_id(self):
        """Without job_id, returns 403 context-guard error."""
        r, _e, st = _run(_ns("u_guard_nojob"), {
            "type": "identity.replace",
            "source": "genesis_resident_distill",
            "reason": "test",
            "identity": _IDENTITY
        })
        assert st == 403
        assert r["error"] == "identity_replace_requires_resident_distill_context"

    def test_context_guard_empty_job_id(self):
        """With empty/whitespace job_id, returns 403 context-guard error."""
        r, _e, st = _run(_ns("u_guard_emptyjob"), {
            "type": "identity.replace",
            "source": "genesis_resident_distill",
            "job_id": "   ",
            "reason": "test",
            "identity": _IDENTITY
        })
        assert st == 403
        assert r["error"] == "identity_replace_requires_resident_distill_context"

    def test_context_guard_missing_reason(self):
        """Without reason, returns 403 context-guard error."""
        r, _e, st = _run(_ns("u_guard_noreason"), {
            "type": "identity.replace",
            "source": "genesis_resident_distill",
            "job_id": "j1",
            "identity": _IDENTITY
        })
        assert st == 403
        assert r["error"] == "identity_replace_requires_resident_distill_context"

    def test_context_guard_empty_reason(self):
        """With empty/whitespace reason, returns 403 context-guard error."""
        r, _e, st = _run(_ns("u_guard_emptyreason"), {
            "type": "identity.replace",
            "source": "genesis_resident_distill",
            "job_id": "j1",
            "reason": "   ",
            "identity": _IDENTITY
        })
        assert st == 403
        assert r["error"] == "identity_replace_requires_resident_distill_context"

    def test_context_guard_passes_with_all_required_fields(self):
        """With all context fields correctly set and live job, passes context guard.

        May fail at downstream checks (e.g. identity_not_initialized, 409),
        but NOT at the context guard itself (403).
        """
        uid = "u_guard_passctx"
        jid = _live_resident_job(uid)
        r, _e, st = _run(_ns(uid), {
            "type": "identity.replace",
            "source": "genesis_resident_distill",
            "job_id": jid,
            "reason": "test distill",
            "identity": _IDENTITY
        })
        # Must NOT be 403 (context guard); may be 409 (identity_not_initialized) or other.
        assert st != 403, f"Context guard should pass but got {st}: {r}"
