"""Migration 0033: seq backfill, effect ordering, and sink claim state."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


BACKEND = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import db  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

from conftest import seed_user  # noqa: E402


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed V2 migration tests require the PostgreSQL test fixture",
)


def _migration_module():
    path = BACKEND / "alembic" / "versions" / "0033_v2_seq_cursor_and_effect_order.py"
    spec = importlib.util.spec_from_file_location("migration_0033", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repair_migration_module():
    path = BACKEND / "alembic" / "versions" / "0034_v2_legacy_sink_reconciliation.py"
    spec = importlib.util.spec_from_file_location("migration_0034", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM user_blobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def test_effect_outbox_has_identity_order_and_pending_index():
    with db.get_pool().connection() as conn:
        column = conn.execute(
            "SELECT is_identity FROM information_schema.columns "
            "WHERE table_name='v2_effect_outbox' AND column_name='enqueue_seq'"
        ).fetchone()
        indexdef = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='v2_effect_outbox' AND indexname='v2_effect_outbox_pending'"
        ).fetchone()
        unique_order_index = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='v2_effect_outbox' "
            "AND indexname='v2_effect_outbox_enqueue_seq_unique'"
        ).fetchone()
        retry_columns = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='v2_effect_outbox' "
            "AND column_name IN ('last_attempt_at','next_attempt_at')"
        ).fetchall()
    assert column == ("YES",)
    assert indexdef is not None
    assert "(user_id, enqueue_seq)" in indexdef[0]
    assert "status = 'pending'" in indexdef[0]
    assert unique_order_index is not None
    assert "UNIQUE INDEX" in unique_order_index[0]
    assert {row[0] for row in retry_columns} == {
        "last_attempt_at", "next_attempt_at",
    }


def test_effect_sink_ledger_distinguishes_claimed_from_completed():
    with db.get_pool().connection() as conn:
        columns = conn.execute(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name='v2_effect_sink_applied' "
            "AND column_name IN ('claim_state', 'completed_at')"
        ).fetchall()
        constraint = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname='v2_effect_sink_applied_claim_state_check'"
        ).fetchone()
    by_name = {row[0]: row[1] for row in columns}
    assert set(by_name) == {"claim_state", "completed_at"}
    assert "claimed" in str(by_name["claim_state"])
    assert constraint is not None
    assert "'claimed'" in constraint[0]
    assert "'completed'" in constraint[0]


def test_legacy_pending_sink_claim_requires_reconciliation_and_scrubs_payload():
    uid = "u_migration_0033_legacy_sink_claim"
    effect_id = "migration-legacy-ambiguous-claim"
    seed_user(uid)
    _clean(uid)
    generation = db.get_runtime_generation(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_effect_sink_applied WHERE effect_id=%s",
            (effect_id,),
        )
        conn.execute(
            "INSERT INTO v2_effect_outbox "
            "(effect_id,user_id,job_id,effect_type,expected_generation,payload,status) "
            "VALUES (%s,%s,903,'identity',%s,%s,'pending')",
            (effect_id, uid, generation, db.Jsonb({"signature": "sensitive"})),
        )
        # Shape left by the earlier unsafe backfill: completed_at was copied
        # exactly from the old marker's applied_at even though the outbox row
        # never reached a terminal status.
        conn.execute(
            "INSERT INTO v2_effect_sink_applied "
            "(effect_id,applied_at,claim_state,completed_at) "
            "VALUES (%s,'2026-07-13T00:00:00Z','completed','2026-07-13T00:00:00Z')",
            (effect_id,),
        )
        conn.execute(_repair_migration_module()._UP)
        sink = conn.execute(
            "SELECT claim_state,completed_at FROM v2_effect_sink_applied "
            "WHERE effect_id=%s",
            (effect_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT status,attempt_count,last_error,payload "
            "FROM v2_effect_outbox WHERE effect_id=%s",
            (effect_id,),
        ).fetchone()

    assert sink == ("claimed", None)
    assert effect[0] == "needs_reconciliation"
    assert effect[1] >= 1
    assert "delivery uncertain" in effect[2]
    assert effect[3] == {"legacy_payload_scrubbed": True}


def test_schema_downgrade_never_turns_unresolved_claim_into_success():
    uid = "u_migration_0033_claimed_downgrade"
    effect_id = "migration-claimed-before-downgrade"
    seed_user(uid)
    _clean(uid)
    generation = db.get_runtime_generation(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_effect_sink_applied WHERE effect_id=%s",
            (effect_id,),
        )
        conn.execute(
            "INSERT INTO v2_effect_outbox "
            "(effect_id,user_id,job_id,effect_type,expected_generation,payload,status) "
            "VALUES (%s,%s,904,'memory',%s,%s,'pending')",
            (effect_id, uid, generation, db.Jsonb({"actions": [{"secret": "x"}]})),
        )
        conn.execute(
            "INSERT INTO v2_effect_sink_applied (effect_id,claim_state) "
            "VALUES (%s,'claimed')",
            (effect_id,),
        )
        conn.execute(_migration_module()._DOWN)
        effect = conn.execute(
            "SELECT status,last_error,payload FROM v2_effect_outbox WHERE effect_id=%s",
            (effect_id,),
        ).fetchone()
        # Restore the shared test schema before leaving the test.
        conn.execute(_migration_module()._UP)
        conn.execute(_repair_migration_module()._UP)

    assert effect[0] == "needs_reconciliation"
    assert "schema rollback" in effect[1]
    assert effect[2] == {"legacy_payload_scrubbed": True}


def test_pending_effects_follow_commit_order_not_timestamp():
    uid = "u_migration_0033_effect_order"
    seed_user(uid)
    _clean(uid)
    db.get_runtime_generation(uid)

    for ordinal in (2, 0, 1):
        assert db.effect_enqueue(
            f"migration-order-{ordinal}", uid, 100 + ordinal, "status", 1,
            {"ordinal": ordinal},
        )

    assert [row["payload"]["ordinal"] for row in db.effect_pending(uid)] == [2, 0, 1]


def test_migration_scrubs_terminal_legacy_tool_payloads_only():
    uid = "u_migration_0033_legacy_payload_scrub"
    seed_user(uid)
    _clean(uid)
    generation = db.get_runtime_generation(uid)
    rows = (
        ("terminal-memory", "memory", "applied", {"actions": [{"secret": "old"}]}),
        ("pending-identity", "identity", "pending", {"signature": "needed-for-retry"}),
        ("terminal-reply", "reply", "applied", {"text": "legacy reply"}),
    )
    with db.get_pool().connection() as conn:
        for effect_id_value, effect_type, status, payload in rows:
            conn.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id,user_id,job_id,effect_type,expected_generation,payload,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    effect_id_value,
                    uid,
                    901,
                    effect_type,
                    generation,
                    db.Jsonb(payload),
                    status,
                ),
            )
        conn.execute(_migration_module()._UP)
        persisted = dict(conn.execute(
            "SELECT effect_id,payload FROM v2_effect_outbox WHERE user_id=%s",
            (uid,),
        ).fetchall())

    assert persisted["terminal-memory"] == {"legacy_payload_scrubbed": True}
    assert persisted["pending-identity"] == {"signature": "needed-for-retry"}
    assert persisted["terminal-reply"] == {"text": "legacy reply"}


def test_backfill_is_conservative_at_same_timestamp_and_monotonic():
    uid = "u_migration_0033_cursor_backfill"
    seed_user(uid)
    _clean(uid)

    for mid, ts in (("before", 1.0), ("tie-a", 2.0), ("tie-b", 2.0), ("after", 3.0)):
        db.chat_append(
            uid, mid, ts,
            {"id": mid, "role": "user", "content": mid},
            1000,
        )
    seq_before = db.chat_seq_for_msg_id(uid, "before")
    seq_tie_b = db.chat_seq_for_msg_id(uid, "tie-b")
    seq_after = db.chat_seq_for_msg_id(uid, "after")
    assert seq_before is not None and seq_tie_b is not None and seq_after is not None

    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "legacy"},
        watermark_ts=3.0,
        watermark_seq=0,
        expected_version=0,
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO runtime_state (user_id, state_json) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET state_json=EXCLUDED.state_json",
            (uid, '{"last_replied_ts": 2.0}'),
        )
        conn.execute(_migration_module()._UP)

    summary = jobs_store.get_summary_row(uid)
    assert summary["watermark_seq"] == seq_tie_b
    assert db.get_blob(uid, "model_api_runtime")["v2_reply_cursor_seq"] == seq_before

    # Re-running the migration cannot regress a cursor already advanced by a
    # live worker, even if the legacy timestamp would map to an older seq.
    db.patch_blob(uid, "model_api_runtime", {"v2_reply_cursor_seq": seq_after})
    with db.get_pool().connection() as conn:
        conn.execute(_migration_module()._UP)
    assert db.get_blob(uid, "model_api_runtime")["v2_reply_cursor_seq"] == seq_after


def test_0034_repairs_old_0033_order_and_reseeds_identity():
    """Exercise the exact already-stamped 0033 shape: enqueue_seq exists and
    is unique, but its identity/heap order disagrees with semantic creation
    order. 0034 must repair it without relying on a fresh 0033 run."""
    uid = "u_migration_0034_old_0033_order"
    seed_user(uid)
    _clean(uid)
    generation = db.get_runtime_generation(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_effect_sink_applied WHERE effect_id LIKE 'old-0033-%'"
        )
        # Insert newest first so identity order is the opposite of created_at.
        for effect_id_value, created_at in (
            ("old-0033-new", "2026-07-13T02:00:00Z"),
            ("old-0033-old", "2026-07-13T01:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id,user_id,job_id,effect_type,expected_generation,payload,created_at) "
                "VALUES (%s,%s,934,'status',%s,'{}'::jsonb,%s)",
                (effect_id_value, uid, generation, created_at),
            )
        before = conn.execute(
            "SELECT effect_id FROM v2_effect_outbox WHERE user_id=%s "
            "ORDER BY enqueue_seq",
            (uid,),
        ).fetchall()
        assert before == [("old-0033-new",), ("old-0033-old",)]

        conn.execute(_repair_migration_module()._UP)
        repaired = conn.execute(
            "SELECT effect_id,enqueue_seq FROM v2_effect_outbox WHERE user_id=%s "
            "ORDER BY enqueue_seq",
            (uid,),
        ).fetchall()
        repaired_max = conn.execute(
            "SELECT MAX(enqueue_seq) FROM v2_effect_outbox"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO v2_effect_outbox "
            "(effect_id,user_id,job_id,effect_type,expected_generation,payload) "
            "VALUES ('old-0033-next',%s,934,'status',%s,'{}'::jsonb)",
            (uid, generation),
        )
        next_seq = conn.execute(
            "SELECT enqueue_seq FROM v2_effect_outbox "
            "WHERE effect_id='old-0033-next'"
        ).fetchone()[0]

    assert [row[0] for row in repaired] == ["old-0033-old", "old-0033-new"]
    assert repaired[1][1] == repaired[0][1] + 1
    assert next_seq == repaired_max + 1


def test_0034_reconciles_preexisting_v2_blob_without_invalidating_work():
    uid = "u_migration_0034_runtime_state_reconcile"
    seed_user(uid)
    _clean(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (uid,))
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) "
            "VALUES (%s,'model_api_runtime',%s)",
            (uid, db.Jsonb({"hosted_runtime_mode": "db_action_v2"})),
        )
        conn.execute(
            "INSERT INTO v2_runtime_state "
            "(user_id,hosted_runtime_state,runtime_generation) "
            "VALUES (%s,'resident',7)",
            (uid,),
        )
        conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,reason,expected_runtime_generation) "
            "VALUES (%s,'chat','pending','pre-0034',7)",
            (uid,),
        )

        conn.execute(_repair_migration_module()._UP)
        control = conn.execute(
            "SELECT hosted_runtime_state,runtime_generation "
            "FROM v2_runtime_state WHERE user_id=%s",
            (uid,),
        ).fetchone()
        job = conn.execute(
            "SELECT status,expected_runtime_generation FROM agent_jobs "
            "WHERE user_id=%s AND reason='pre-0034'",
            (uid,),
        ).fetchone()

    assert control == ("v2", 7)
    assert job == ("pending", 7)
