"""D7 GC/retention gate: the chat-history trim in ``db._chat_insert_on_cursor``
must never delete a row the conversation summary hasn't covered yet.

Today's summary row (migration 0016) only stores a ``watermark_ts`` (a
wall-clock float), NOT a seq — that column (``watermark_seq``) is a LATER
task. So the gate this test proves is the CONSERVATIVE ts-based one: a row is
only eligible for the count-based trim if its ``ts`` is STRICTLY LESS than
the user's ``v2_conversation_summary.watermark_ts`` (same-ts boundary rows
may not have made it into the compacted batch — never delete those). No
summary row at all (user never compacted), or a watermark_ts of 0/NULL, must
disable trimming entirely — fail-safe: never delete history nothing has
proven is covered.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 GC coverage-gate tests require the PostgreSQL test fixture",
)


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute(
            "TRUNCATE v2_conversation_summary, chat_messages CASCADE"
        )
    yield


def _rows(uid: str) -> list[tuple[int, float]]:
    """Raw (seq, ts) pairs for the user, seq-ordered — bypasses
    ``chat_messages_after_seq`` (which merges seq into ``doc`` but drops the
    column-level ``ts``) so assertions can check the trim's ts boundary
    directly against the real stored rows."""
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT seq, ts FROM chat_messages WHERE user_id = %s ORDER BY seq ASC",
            (uid,),
        ).fetchall()
    return [(int(r[0]), float(r[1])) for r in rows]


def _seed_messages(uid: str, n: int, *, base_ts: float, max_messages: int) -> None:
    for i in range(n):
        db.chat_append_strict(uid, f"m{i:03d}", base_ts + i, {"role": "user", "n": i}, max_messages)


def test_uncovered_rows_survive_trim_even_beyond_max_messages(pg_clean):
    """max_messages=5, 10 messages ts=1000..1009, watermark_ts=1003 (covers
    ts<1003 => i=0,1,2). The naive count-only cutoff would keep only the
    newest 5 (i=5..9) and delete i=0..4. The gated trim must additionally
    protect i=3,4 (ts>=watermark_ts, NOT yet covered) even though they are
    below the count cutoff — so the surviving row count (7) exceeds
    max_messages (5) until compaction advances the watermark. Only the
    covered-and-below-cutoff rows (i=0,1,2) may actually be deleted."""
    uid = "u_gc_partial"
    seed_user(uid)
    base_ts = 1000.0
    watermark_ts = 1003.0
    ok = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={}, watermark_ts=watermark_ts, expected_version=0,
    )
    assert ok

    _seed_messages(uid, 10, base_ts=base_ts, max_messages=5)

    rows = _rows(uid)
    ts_present = {ts for _, ts in rows}

    # Every row with ts >= watermark_ts (not yet covered: i=3..9) must
    # survive, regardless of the count cutoff.
    for i in range(3, 10):
        assert (base_ts + i) in ts_present, f"uncovered row i={i} was deleted"
    assert len(rows) == 7  # 10 - the 3 covered-and-below-cutoff rows (i=0,1,2)

    # Covered rows beyond the count cutoff (i=0,1,2; ts < watermark_ts) ARE
    # trimmed normally.
    for i in range(0, 3):
        assert (base_ts + i) not in ts_present, f"covered row i={i} was NOT trimmed"


def test_no_summary_row_disables_trim_entirely(pg_clean):
    """A user who has never been compacted has no v2_conversation_summary
    row at all. The trim must be a fail-safe no-op: every row survives even
    though the count far exceeds max_messages."""
    uid = "u_gc_nosummary"
    seed_user(uid)

    _seed_messages(uid, 10, base_ts=2000.0, max_messages=5)

    rows = _rows(uid)
    assert len(rows) == 10  # nothing trimmed — no proof of coverage exists


def test_zero_watermark_disables_trim(pg_clean):
    """A summary row that exists but with watermark_ts=0 (the column
    default — compaction has never actually advanced it) must ALSO disable
    trimming, same as no row at all: 0 means nothing is proven covered."""
    uid = "u_gc_zerowm"
    seed_user(uid)
    ok = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={}, watermark_ts=0.0, expected_version=0,
    )
    assert ok

    _seed_messages(uid, 10, base_ts=3000.0, max_messages=5)

    rows = _rows(uid)
    assert len(rows) == 10  # watermark_ts=0 covers nothing (ts is never < 0)


def test_legacy_path_plain_trims_even_with_no_summary_row(pg_clean):
    """Regression guard: ``db.chat_append`` (the LEGACY/pre-V2 best-effort
    write path, shared by ``core/store.py`` and ``content/content_core.py``)
    must NOT be gated on summary coverage — that path never writes a
    ``v2_conversation_summary`` row, so gating it would silently disable
    trimming forever (unbounded ``chat_messages`` growth). With no summary
    row at all, ``chat_append`` must still plain-trim to the newest
    max_messages, exactly like the pre-D7 behavior."""
    uid = "u_gc_legacy_nosummary"
    seed_user(uid)
    base_ts = 5000.0
    for i in range(10):
        db.chat_append(uid, f"m{i:03d}", base_ts + i, {"role": "user", "n": i}, max_messages=5)

    rows = _rows(uid)
    assert len(rows) == 5  # plain ring-buffer trim, unaffected by (absent) coverage
    ts_present = {ts for _, ts in rows}
    for i in range(5, 10):
        assert (base_ts + i) in ts_present
    for i in range(0, 5):
        assert (base_ts + i) not in ts_present


def test_watermark_above_all_messages_behaves_like_today(pg_clean):
    """Sanity: once the watermark covers everything (compaction is fully
    caught up), the gate is a no-op and the trim behaves exactly like the
    pre-existing count-only trim — keeps only the newest max_messages."""
    uid = "u_gc_full_coverage"
    seed_user(uid)
    base_ts = 4000.0
    ok = jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={}, watermark_ts=base_ts + 1000, expected_version=0,
    )
    assert ok

    _seed_messages(uid, 10, base_ts=base_ts, max_messages=5)

    rows = _rows(uid)
    assert len(rows) == 5
    ts_present = {ts for _, ts in rows}
    # newest 5 (i=5..9) survive; oldest 5 (i=0..4) are gone.
    for i in range(5, 10):
        assert (base_ts + i) in ts_present
    for i in range(0, 5):
        assert (base_ts + i) not in ts_present
