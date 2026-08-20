"""Terminal ciphertext preservation safety contracts."""

from __future__ import annotations

import os
import re
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

import db
from tee_replicator import terminal_preservation as preservation
from tee_shadow import mirror


def test_preserved_marker_round_trips_original_reason():
    """Dropping the original reason would make guarded revert impossible."""
    reason = "decrypt_failed:enclave_http_403"
    encoded = preservation.encode_preserved_reason("a" * 64, reason)

    assert preservation.parse_preserved_reason(encoded) == ("a" * 64, reason)
    assert preservation.is_terminal_reason(encoded) is False


@pytest.mark.parametrize(
    "reason",
    ["decrypt_failed:old-key", "pdm:local-only", "visibility_local_only"],
)
def test_only_unpreserved_terminal_reasons_are_eligible(reason):
    """A missing terminal prefix would strand an eligible historical row."""
    assert preservation.is_terminal_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "requeue",
        "requeue:source_updated",
        "preserved_ciphertext:v2:bad:bad",
        "preserved_ciphertext:v1:not-a-digest:bad",
        "",
    ],
)
def test_nonterminal_or_malformed_reasons_are_not_eligible(reason):
    """Treating backlog or malformed audit state as terminal would waive work."""
    assert preservation.is_terminal_reason(reason) is False
    assert preservation.parse_preserved_reason(reason) is None


def test_canonical_digest_is_stable_across_json_key_order():
    """Equivalent JSON objects must not invalidate an operator plan digest."""
    first = preservation.canonical_row_sha256(
        "chat_messages", ("u", "m", 1.0, {"b": 2, "a": 1}, 7, 0)
    )
    second = preservation.canonical_row_sha256(
        "chat_messages", ("u", "m", 1.0, {"a": 1, "b": 2}, 7, 0)
    )

    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_contracts_are_exactly_the_four_approved_families():
    """Adding an unreviewed table must not silently widen destructive scope."""
    assert set(preservation.CONTRACTS) == {
        "chat_messages",
        "memory_moments",
        "identity",
        "frame_envelopes",
    }


@pytest.fixture(autouse=True)
def _clean_preservation_rows(backend_env):
    """Keep whole-database planning deterministic in the shared test DBs."""
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        tee.execute("DELETE FROM tee_pending_device_migration")
        tee.execute("DELETE FROM users WHERE user_id LIKE 'usr_preserve_%'")
    yield
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        tee.execute("DELETE FROM tee_pending_device_migration")
        tee.execute("DELETE FROM users WHERE user_id LIKE 'usr_preserve_%'")
    with db.get_pool().connection() as source:
        source.execute("DELETE FROM users WHERE user_id LIKE 'usr_preserve_%'")


def _seed_four_terminal_rows() -> tuple[str, dict[str, str]]:
    uid = f"usr_preserve_{uuid.uuid4().hex[:12]}"
    ids = {
        "chat_messages": f"msg_{uuid.uuid4().hex[:10]}",
        "memory_moments": f"mem_{uuid.uuid4().hex[:10]}",
        "identity": uid,
        "frame_envelopes": f"frm_{uuid.uuid4().hex[:10]}",
    }
    user_doc = {"user_id": uid, "api_key_hash": "preservation-test"}
    with db.get_pool().connection() as source:
        source.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,'',%s)",
            (uid, Jsonb(user_doc)),
        )
        source.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
            "VALUES (%s,%s,1,%s)",
            (uid, ids["chat_messages"], Jsonb({"id": ids["chat_messages"], "body_ct": "chat-ct"})),
        )
        source.execute(
            "INSERT INTO memory_moments (user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,%s,'2026-08-20T00:00:00Z',%s)",
            (uid, ids["memory_moments"], Jsonb({"id": ids["memory_moments"], "body_ct": "memory-ct"})),
        )
        source.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,'identity',%s)",
            (uid, Jsonb({"id": uid, "body_ct": "identity-ct"})),
        )
        source.execute(
            "INSERT INTO frame_envelopes (user_id,frame_id,ts,doc) "
            "VALUES (%s,%s,2,%s)",
            (uid, ids["frame_envelopes"], Jsonb({"id": ids["frame_envelopes"], "body_ct": "frame-ct"})),
        )

    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        tee.execute(
            "INSERT INTO users (user_id,created_at,doc) VALUES (%s,'',%s)",
            (uid, Jsonb(user_doc)),
        )
        with tee.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO tee_pending_device_migration "
                "(user_id,table_name,item_id,reason) VALUES (%s,%s,%s,%s)",
                [
                    (uid, table, item_id, "decrypt_failed:historical-test")
                    for table, item_id in ids.items()
                ],
            )
    return uid, ids


def test_build_plan_is_read_only_and_stable(backend_env):
    """A dry-run that mutates either database defeats the approval gate."""
    uid, _ids = _seed_four_terminal_rows()

    with db.get_pool().connection() as source, psycopg.connect(
        os.environ["TEE_DATABASE_URL"], autocommit=True
    ) as destination:
        before_user = source.execute(
            "SELECT doc FROM users WHERE user_id=%s", (uid,)
        ).fetchone()[0]
        plan = preservation.build_plan(source, destination)

        assert plan.counts == {
            "chat_messages": 1,
            "frame_envelopes": 1,
            "identity": 1,
            "memory_moments": 1,
        }
        assert len(plan.rows) == 4
        assert re.fullmatch(r"[0-9a-f]{64}", plan.sha256)
        assert plan.blockers == ()
        assert destination.execute(
            "SELECT count(*) FROM tee_pending_device_migration"
        ).fetchone()[0] == 4
        assert destination.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,)
        ).fetchone()[0] == 0
        assert source.execute(
            "SELECT doc FROM users WHERE user_id=%s", (uid,)
        ).fetchone()[0] == before_user


@pytest.mark.parametrize(
    ("expected_count", "expected_digest"),
    [(3, "a" * 64), (4, "b" * 64)],
)
def test_apply_rejects_stale_compare_and_apply_guard(
    backend_env, expected_count, expected_digest
):
    """A stale approval must never partially copy rows or rewrite markers."""
    uid, _ids = _seed_four_terminal_rows()

    with db.get_pool().connection() as source, psycopg.connect(
        os.environ["TEE_DATABASE_URL"], autocommit=True
    ) as destination:
        plan = preservation.build_plan(source, destination)
        before = (
            destination.execute(
                "SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,)
            ).fetchone()[0],
            destination.execute(
                "SELECT table_name,item_id,reason FROM tee_pending_device_migration "
                "WHERE user_id=%s ORDER BY table_name,item_id",
                (uid,),
            ).fetchall(),
        )

        with pytest.raises(preservation.PreservationRefused):
            preservation.apply_plan(
                source,
                destination,
                plan,
                expected_count=expected_count,
                expected_plan_sha256=expected_digest,
            )

        after = (
            destination.execute(
                "SELECT count(*) FROM chat_messages WHERE user_id=%s", (uid,)
            ).fetchone()[0],
            destination.execute(
                "SELECT table_name,item_id,reason FROM tee_pending_device_migration "
                "WHERE user_id=%s ORDER BY table_name,item_id",
                (uid,),
            ).fetchall(),
        )
        assert after == before


def test_apply_copies_all_four_shapes_and_marks_them_atomically(backend_env, caplog):
    """Skipping any family or marker update would make the Phase 4 waiver unsafe."""
    uid, ids = _seed_four_terminal_rows()

    with db.get_pool().connection() as source, psycopg.connect(
        os.environ["TEE_DATABASE_URL"], autocommit=True
    ) as destination:
        user_before = source.execute(
            "SELECT doc FROM users WHERE user_id=%s", (uid,)
        ).fetchone()[0]
        plan = preservation.build_plan(source, destination)
        report = preservation.apply_plan(
            source,
            destination,
            plan,
            expected_count=4,
            expected_plan_sha256=plan.sha256,
        )

        assert report == {
            "ok": True,
            "preserved": 4,
            "inserted": 4,
            "already_exact": 0,
            "counts": {
                "chat_messages": 1,
                "frame_envelopes": 1,
                "identity": 1,
                "memory_moments": 1,
            },
            "plan_sha256": plan.sha256,
        }
        for row in plan.rows:
            contract = preservation.CONTRACTS[row.table]
            actual = destination.execute(
                contract.destination_fetch_sql,
                contract.args(row.user_id, row.item_id),
            ).fetchone()
            assert tuple(actual) == row.source_row
            marker = destination.execute(
                "SELECT reason FROM tee_pending_device_migration "
                "WHERE user_id=%s AND table_name=%s AND item_id=%s",
                (row.user_id, row.table, row.item_id),
            ).fetchone()[0]
            assert preservation.parse_preserved_reason(marker) == (
                row.row_sha256,
                row.original_reason,
            )
        assert source.execute(
            "SELECT doc FROM users WHERE user_id=%s", (uid,)
        ).fetchone()[0] == user_before
        assert uid not in caplog.text
        assert all(item_id not in caplog.text for item_id in ids.values())


def test_apply_is_idempotent_after_exact_preservation(backend_env):
    """Retrying an acknowledged operation must not insert or lose audit markers."""
    _uid, _ids = _seed_four_terminal_rows()

    with db.get_pool().connection() as source, psycopg.connect(
        os.environ["TEE_DATABASE_URL"], autocommit=True
    ) as destination:
        first_plan = preservation.build_plan(source, destination)
        first = preservation.apply_plan(
            source,
            destination,
            first_plan,
            expected_count=4,
            expected_plan_sha256=first_plan.sha256,
        )
        retry_plan = preservation.build_plan(source, destination)
        second = preservation.apply_plan(
            source,
            destination,
            retry_plan,
            expected_count=4,
            expected_plan_sha256=first_plan.sha256,
        )

        assert retry_plan.sha256 == first_plan.sha256
        assert second["preserved"] == 4
        assert second["inserted"] == 0
        assert second["already_exact"] == 4
        assert second["plan_sha256"] == first["plan_sha256"]


def test_missing_parent_blocks_without_exposing_identifiers(backend_env):
    """An orphan must block while reports remain safe to paste into CI logs."""
    uid = f"usr_preserve_{uuid.uuid4().hex[:12]}"
    item_id = f"msg_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        tee.execute(
            "INSERT INTO tee_pending_device_migration "
            "(user_id,table_name,item_id,reason) "
            "VALUES (%s,'chat_messages',%s,'decrypt_failed:orphan')",
            (uid, item_id),
        )
        with db.get_pool().connection() as source:
            plan = preservation.build_plan(source, tee)

    assert plan.rows == ()
    assert plan.blockers == ("missing_parent:chat_messages:1",)
    assert uid not in repr(plan.blockers)
    assert item_id not in repr(plan.blockers)


def test_destination_conflict_blocks_instead_of_overwriting_plaintext(backend_env):
    """Preservation must never replace an existing plaintext projection."""
    uid, ids = _seed_four_terminal_rows()
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        tee.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,1,%s)",
            (uid, ids["chat_messages"], Jsonb({"id": ids["chat_messages"], "body": "plaintext"})),
        )
        with db.get_pool().connection() as source:
            plan = preservation.build_plan(source, tee)

    assert "destination_conflict:chat_messages:1" in plan.blockers
    assert len(plan.rows) == 3


def test_unmarked_exact_destination_blocks_to_keep_revert_ownership(backend_env):
    """Revert must never delete an exact row that preservation did not create."""
    uid, ids = _seed_four_terminal_rows()
    contract = preservation.CONTRACTS["chat_messages"]
    with db.get_pool().connection() as source, psycopg.connect(
        os.environ["TEE_DATABASE_URL"], autocommit=True
    ) as destination:
        args = contract.args(uid, ids["chat_messages"])
        source_row = tuple(source.execute(contract.source_fetch_sql, args).fetchone())
        destination.execute(contract.insert_sql, contract.insert_args(source_row))
        plan = preservation.build_plan(source, destination)

    assert "unowned_exact_destination:chat_messages:1" in plan.blockers
    assert len(plan.rows) == 3


def test_later_source_mutation_requeues_a_preserved_row(backend_env, monkeypatch):
    """A preserved marker must not hide a later in-place source rewrite."""
    uid, ids = _seed_four_terminal_rows()
    with db.get_pool().connection() as source, psycopg.connect(
        os.environ["TEE_DATABASE_URL"], autocommit=True
    ) as destination:
        plan = preservation.build_plan(source, destination)
        preservation.apply_plan(
            source,
            destination,
            plan,
            expected_count=4,
            expected_plan_sha256=plan.sha256,
        )

    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    mirror.mark_pending(uid, "chat_messages", ids["chat_messages"], "requeue")

    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as tee:
        reason = tee.execute(
            "SELECT reason FROM tee_pending_device_migration "
            "WHERE user_id=%s AND table_name='chat_messages' AND item_id=%s",
            (uid, ids["chat_messages"]),
        ).fetchone()[0]
    assert reason == "requeue"
