"""Stable per-user reply cursor keyed on ``chat_messages.seq`` (spec A1).

Never ``ts``: two messages appended in the same instant (ordinary under
concurrent workers) can share an identical wall-clock timestamp, which makes a
ts-based cursor non-monotonic — a replay could reprocess or silently skip a
message. ``seq`` is a real monotonic identity-column counter (see
``db.chat_max_seq`` / ``db.chat_messages_after_seq``), so it has no such gap.

Advancing the cursor is itself an outbox ``cursor`` effect (see
``effect_outbox.py``) so it is generation-fenced and idempotent exactly like
every other V2 side effect: the actual durable WRITE of the new cursor value
happens in the ``cursor`` effect's dispatch sink (Task 6 / spec A6), not here.
This module only derives the effect's id/payload and reads the last durably
committed value back off the user's ``model_api_runtime`` profile blob.
"""
from __future__ import annotations

import db
from model_api_runtime.v2 import effect_id

CURSOR_KEY = "v2_reply_cursor_seq"


def advance_effect(*, job_id: int, ordinal: int, generation: int, new_seq: int):
    """Build the (effect_id, payload) pair for a cursor-advance outbox effect.

    ``generation`` is accepted for caller symmetry with the other per-turn
    effect builders (it documents which runtime generation this advance was
    computed under) but does not enter the id: a cursor advance is keyed like
    any other per-job effect, by (job_id, effect_type, ordinal) — see
    ``effect_id.derive``. The generation fence itself is enforced by the
    outbox applier reading ``expected_generation`` at apply time, not by the
    id shape.
    """
    seq = int(new_seq)
    if seq < 0:
        raise ValueError("new_seq must be >= 0")
    eid = effect_id.derive(job_id=job_id, effect_type="cursor", ordinal=ordinal)
    return eid, {"new_seq": seq}


def load_seq(store) -> int:
    """Last durably committed reply-cursor seq for ``store``'s user, or 0 if
    never advanced. ``store`` is duck-typed to anything carrying a
    ``user_id`` (e.g. ``core.store.UserStore``) — this module reads the raw
    ``model_api_runtime`` blob directly via ``db.get_blob_strict`` rather than going
    through ``hosted.config_store``, so it stays import-clean of ``hosted``
    (dependency-direction guard, spec constraints). Invalid stored values and
    database errors propagate: resetting a broken/unreadable correctness cursor
    to zero would replay an arbitrary amount of conversation history."""
    # Correctness cursors use the strict reader: a transient DB failure must
    # fail the turn, never masquerade as "cursor unset" and replay history.
    profile = db.get_blob_strict(store.user_id, "model_api_runtime")
    if profile is None:
        return 0
    if not isinstance(profile, dict):
        raise ValueError("model_api_runtime blob must be an object")
    raw = profile.get(CURSOR_KEY)
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        raise ValueError(f"{CURSOR_KEY} must be a non-negative integer")
    if isinstance(raw, int):
        seq = raw
    elif isinstance(raw, str) and raw.strip().isdecimal():
        seq = int(raw.strip())
    else:
        raise ValueError(f"{CURSOR_KEY} must be a non-negative integer")
    if seq < 0:
        raise ValueError(f"{CURSOR_KEY} must be a non-negative integer")
    return seq
