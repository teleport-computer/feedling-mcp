"""Codex C2: fresh-user identity bootstrap must be an ATOMIC create-if-absent.

Two gunicorn worker processes both seeing "no card" on a fresh user previously
each ran the plain INSERT ... ON CONFLICT DO UPDATE and the second clobbered the
first — a lost update where BOTH reported success. `_save_identity` also masked
DB failures as a false 200 (`db.set_blob` swallows the exception). The fix routes
the create through `db.set_blob_if_unchanged(expected_doc={}, insert_if_missing
=True)` (INSERT ... ON CONFLICT DO NOTHING RETURNING): exactly one caller wins,
the loser raises IdentityWriteConflict and the mutation retry loop re-reads the
now-existing card and merges via the CAS UPDATE path.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core import store as core_store  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from identity import actions as identity_actions  # noqa: E402
from identity import service as identity_service  # noqa: E402

from conftest import seed_user  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed atomic-bootstrap tests require the PostgreSQL test fixture",
)


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE user_blobs, users CASCADE")
    yield


def _card(marker: str) -> dict:
    return {
        "v": 1, "id": f"identity_{marker}", "body_ct": f"ct_{marker}",
        "nonce": "n", "K_user": "k", "visibility": "shared",
        "owner_user_id": "u", "created_at": "t", "updated_at": "t",
        "replaced_at": "t", "relationship_started_at": "2026-01-01",
        "relationship_anchor_source": "agent_bootstrap",
    }


def test_create_if_absent_single_winner_sequential(pg_clean):
    seed_user("u_boot_seq")
    store = core_store.get_store("u_boot_seq")

    assert identity_service._save_identity_create_if_absent(store, _card("first")) is True
    # Second create loses: the row already exists, ON CONFLICT DO NOTHING.
    assert identity_service._save_identity_create_if_absent(store, _card("second")) is False

    saved = db.get_blob("u_boot_seq", "identity")
    assert saved["id"] == "identity_first"  # winner's card is intact, not clobbered


def test_create_if_absent_single_winner_under_real_concurrency(pg_clean):
    """Two real threads / pool connections race the first create. Exactly one
    wins; the loser sees False (never a silent double-success)."""
    seed_user("u_boot_race")
    store = core_store.get_store("u_boot_race")

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def _attempt(marker: str) -> None:
        barrier.wait()
        results[marker] = identity_service._save_identity_create_if_absent(store, _card(marker))

    threads = [threading.Thread(target=_attempt, args=(m,)) for m in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [False, True]  # exactly one winner
    winner = next(m for m, won in results.items() if won)
    assert db.get_blob("u_boot_race", "identity")["id"] == f"identity_{winner}"


def test_create_identity_action_payload_raises_conflict_when_card_exists(pg_clean, monkeypatch):
    """If a card already exists (a concurrent worker bootstrapped it first), the
    create path raises IdentityWriteConflict so the caller retries down the
    UPDATE path — it does NOT overwrite the existing card or report success."""
    seed_user("u_boot_conflict")
    store = core_store.get_store("u_boot_conflict")
    db.set_blob("u_boot_conflict", "identity", _card("existing"))

    def _fake_envelope(store, plaintext, *, item_id=None):
        return {
            "id": "env", "body_ct": "ct", "nonce": "n", "K_user": "k",
            "visibility": "shared", "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope)

    with pytest.raises(identity_service.IdentityWriteConflict):
        identity_actions._create_identity_action_payload(
            store,
            {"agent_name": "Ori", "self_introduction": "hi", "dimensions": []},
            audit={"action": "profile_patch"},
            event_type="identity_action_bootstrap",
        )
    # Existing card untouched.
    assert db.get_blob("u_boot_conflict", "identity")["id"] == "identity_existing"


def test_bootstrap_db_failure_does_not_return_200(pg_clean, monkeypatch):
    """A genuine DB failure during the fresh-user create must NOT surface as a
    success. set_blob_if_unchanged returns False on error; the create raises
    IdentityWriteConflict, the retry loop exhausts, and the action returns 409
    — never a false 200 with nothing persisted."""
    seed_user("u_boot_dbfail")
    store = core_store.get_store("u_boot_dbfail")

    # No card exists -> profile_patch takes the bootstrap branch.
    monkeypatch.setattr(identity_service, "_load_identity", lambda _store: None)
    # Simulate an enclave read that reports "not initialized" (fresh user).
    from core import enclave as core_enclave
    monkeypatch.setattr(
        core_enclave, "_enclave_get_json_for_gate",
        lambda path, key, **kw: ({}, ""),
    )

    def _fake_envelope(store, plaintext, *, item_id=None):
        return {
            "id": "env", "body_ct": "ct", "nonce": "n", "K_user": "k",
            "visibility": "shared", "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope)
    # Simulate the create write failing at the DB layer (returns False).
    monkeypatch.setattr(
        db, "set_blob_if_unchanged", lambda *a, **k: False
    )

    body, _effects, status = identity_actions._identity_profile_patch(
        store, None,
        {"type": "identity.profile_patch",
         "patch": {"agent_name": "Ori", "self_introduction": "hi"}},
        runtime_token="",
    )

    assert status == 409, body
    assert body["error"] == "identity_write_conflict"
    assert db.get_blob("u_boot_dbfail", "identity") is None  # nothing persisted
