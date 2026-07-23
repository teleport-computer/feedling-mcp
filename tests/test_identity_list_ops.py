"""Pure-unit tests for explicit list-field ops (add/remove/replace) + the
per-user identity mutation lock / cross-worker CAS that close the
lost-update window across concurrent identity.profile_patch /
identity.dimension_nudge calls.

Fully DB-free: no real `core.store.UserStore` is ever constructed (Codex
review I8 — a real UserStore's __init__ eagerly touches db.chat_load /
frame_list_meta / world_book_load / etc, which is exactly the kind of DB
dependency a file whitelisted as pure-unit (tests/conftest.py's
_PURE_UNIT) must never have). Instead every db.* call site the code under
test touches (get_blob/set_blob/set_blob_if_unchanged/log_append) is
monkeypatched onto an in-memory dict, plus the enclave decrypt /
envelope-build boundary — see `_FakeStore` and `_fake_db_blob_layer` below.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from identity import actions as identity_actions  # noqa: E402
from identity import service as identity_service  # noqa: E402


class _FakeStore:
    """Minimal test double exposing only `user_id` — the sole attribute the
    profile_patch/dimension_nudge code path touches (identity_mutation_lock
    is keyed by the plain string user_id, not the store object; every real
    `store.xxx` call in that path is `store.user_id`). See module docstring
    for why this exists instead of a real `core.store.UserStore`."""

    def __init__(self, user_id: str):
        self.user_id = user_id


def _fake_db_blob_layer(monkeypatch, blobs: dict[tuple[str, str], dict]) -> None:
    """Patch the exact db.* functions the profile_patch/dimension_nudge path
    touches onto an in-memory `blobs` dict — including a CAS-faithful
    `set_blob_if_unchanged` (plain equality compare on the stored doc,
    mirroring the real `UPDATE ... WHERE doc = expected` semantics)."""

    def fake_get_blob(uid, kind):
        return blobs.get((uid, kind))

    def fake_set_blob(uid, kind, doc):
        blobs[(uid, kind)] = doc

    def fake_set_blob_if_unchanged(uid, kind, expected_doc, new_doc, *, insert_if_missing=False):
        if blobs.get((uid, kind)) != expected_doc:
            return False
        blobs[(uid, kind)] = new_doc
        return True

    def fake_log_append(uid, stream, doc):
        return None

    monkeypatch.setattr(db, "get_blob", fake_get_blob)
    monkeypatch.setattr(db, "set_blob", fake_set_blob)
    monkeypatch.setattr(db, "set_blob_if_unchanged", fake_set_blob_if_unchanged)
    monkeypatch.setattr(db, "log_append", fake_log_append)


# ---------------------------------------------------------------------------
# apply_list_ops — pure function
# ---------------------------------------------------------------------------

def test_add_signature_appends():
    merged = identity_actions.apply_list_ops(
        {"signature": ["得嘞", "交给我"]},
        {"add_signature": ["包我身上"]})
    assert merged["signature"] == ["得嘞", "交给我", "包我身上"]


def test_remove_signature_exact():
    merged = identity_actions.apply_list_ops(
        {"signature": ["得嘞", "交给我"]}, {"remove_signature": ["得嘞"]})
    assert merged["signature"] == ["交给我"]


def test_conflicting_ops_rejected():
    with pytest.raises(identity_actions.ListOpConflict):
        identity_actions.apply_list_ops(
            {"signature": []},
            {"add_signature": ["a"], "replace_signatures": ["b"]})


def test_blank_only_add_rejected():
    with pytest.raises(identity_actions.ListOpBlank):
        identity_actions.apply_list_ops({"signature": []}, {"add_signature": ["  "]})


def test_add_signature_dedupes_against_existing():
    # "add 去重追加": items already present in the existing list must not be
    # duplicated by an add op.
    merged = identity_actions.apply_list_ops(
        {"signature": ["得嘞", "交给我"]},
        {"add_signature": ["得嘞", "新签名"]})
    assert merged["signature"] == ["得嘞", "交给我", "新签名"]


def test_replace_signatures_swaps_whole_list():
    merged = identity_actions.apply_list_ops(
        {"signature": ["旧1", "旧2"]}, {"replace_signatures": ["新1"]})
    assert merged["signature"] == ["新1"]


def test_legacy_signature_key_still_replaces():
    # Backward compat: passing the plain field name directly (no add_/remove_/
    # replace_ op key) keeps the pre-existing "whole-list replace" behavior.
    merged = identity_actions.apply_list_ops(
        {"signature": ["旧的"]}, {"signature": ["新的签名一", "新的签名二"]})
    assert merged["signature"] == ["新的签名一", "新的签名二"]


def test_add_signature_strips_blank_items():
    # Blank items are stripped, but a mix of blank + real items is NOT the
    # all-blank case — no ListOpBlank here, just the blanks silently dropped.
    merged = identity_actions.apply_list_ops(
        {"signature": ["得嘞"]}, {"add_signature": ["  ", "新签名", "\t"]})
    assert merged["signature"] == ["得嘞", "新签名"]


def test_other_list_fields_use_same_op_shape():
    # boundaries/do_not_say/stable_definitions must be driven by the same
    # _LIST_OP_FIELDS table, not copy-pasted per field.
    merged = identity_actions.apply_list_ops(
        {"boundaries": ["别聊工作"]}, {"add_boundaries": ["别催更"]})
    assert merged["boundaries"] == ["别聊工作", "别催更"]

    merged = identity_actions.apply_list_ops(
        {"do_not_say": ["废话"]}, {"remove_do_not_say": ["废话"]})
    assert merged["do_not_say"] == []

    merged = identity_actions.apply_list_ops(
        {"stable_definitions": ["A=旧"]}, {"replace_stable_definitions": ["A=新"]})
    assert merged["stable_definitions"] == ["A=新"]


def test_untouched_field_is_omitted_from_result():
    merged = identity_actions.apply_list_ops(
        {"signature": ["s"], "boundaries": ["b"]}, {"add_signature": ["s2"]})
    assert "boundaries" not in merged


# ---------------------------------------------------------------------------
# I7: add_* must not silently blow past the 12-item cap (12 existing + 12
# added = 24 stored was the bug; legacy/replace already truncate via
# _clean_list_items's raw[:12], but add_* didn't check the MERGED total).
#
# NOTE ON FILE PLACEMENT: these were originally requested in
# tests/test_identity_nudge_cap.py, but that file already exists for an
# unrelated, pre-existing feature (the dimension-NUDGE delta *sum* cap,
# |Δsum| <= 10 — see identity/card_policy.validate_nudge_sum). Overwriting it
# would have destroyed that coverage, so these live here instead, alongside
# the rest of apply_list_ops' pure-function tests. See task report.
# ---------------------------------------------------------------------------

def test_add_signature_rejects_when_merged_exceeds_cap():
    # 12 existing (full) + 1 genuinely new addition = 13 -> rejected, not
    # silently truncated back down to 12.
    existing = {"signature": [f"s{i}" for i in range(12)]}
    with pytest.raises(identity_actions.ListOpTooManyItems):
        identity_actions.apply_list_ops(existing, {"add_signature": ["one-more"]})


def test_add_signature_dedup_landing_exactly_at_cap_passes():
    # 11 existing + ["s0" (dup, no net growth), "s12" (new)] -> 12 total,
    # exactly at the cap. Must NOT be rejected — the cap is on the MERGED
    # result, and a dedup hit contributes zero net growth.
    existing = {"signature": [f"s{i}" for i in range(11)]}
    merged = identity_actions.apply_list_ops(existing, {"add_signature": ["s0", "s12"]})
    assert len(merged["signature"]) == 12
    assert merged["signature"][-1] == "s12"


def test_add_signature_13th_item_rejected():
    # 11 existing + 2 genuinely new (no dedup hits) = 13 -> rejected.
    existing = {"signature": [f"s{i}" for i in range(11)]}
    with pytest.raises(identity_actions.ListOpTooManyItems):
        identity_actions.apply_list_ops(existing, {"add_signature": ["new1", "new2"]})


def test_add_boundaries_also_enforces_cap():
    # Same _LIST_OP_FIELDS-driven code path — not a signature-only special case.
    existing = {"boundaries": [f"b{i}" for i in range(12)]}
    with pytest.raises(identity_actions.ListOpTooManyItems):
        identity_actions.apply_list_ops(existing, {"add_boundaries": ["one-more"]})


# ---------------------------------------------------------------------------
# I4 (review): the ORIGINAL reported repro — empty list + add of 13 distinct
# values. Pre-fix, `_clean_list_items`'s raw[:12] truncated the 13 additions
# down to 12 BEFORE the merged-length check ever ran, so this silently
# produced a 12-item list instead of raising ListOpTooManyItems.
# ---------------------------------------------------------------------------

def test_add_13_distinct_to_empty_list_rejected_not_truncated():
    with pytest.raises(identity_actions.ListOpTooManyItems):
        identity_actions.apply_list_ops(
            {"signature": []}, {"add_signature": [f"new{i}" for i in range(13)]})


def test_add_exactly_12_distinct_to_empty_list_ok():
    merged = identity_actions.apply_list_ops(
        {"signature": []}, {"add_signature": [f"new{i}" for i in range(12)]})
    assert len(merged["signature"]) == 12
    assert merged["signature"] == [f"new{i}" for i in range(12)]


def test_legacy_direct_assign_still_silently_truncates_not_reject():
    # Unchanged pre-existing behavior for the GENUINE legacy path only (a bare
    # `signature` key, i.e. direct-list-assign): back-compat, no new
    # ListOpTooManyItems here — _clean_list_items truncates via raw[:12].
    oversized = [f"x{i}" for i in range(20)]
    merged = identity_actions.apply_list_ops({"signature": []}, {"signature": oversized})
    assert len(merged["signature"]) == 12


def test_replace_signatures_13_items_rejected_not_truncated():
    # I4 fix: replace_* is a brand-new op key (not the legacy path) and must
    # now REJECT an oversized request instead of silently truncating it —
    # the pre-fix behavior truncated via _clean_list_items's raw[:12] before
    # any length check ever ran.
    oversized = [f"x{i}" for i in range(13)]
    with pytest.raises(identity_actions.ListOpTooManyItems):
        identity_actions.apply_list_ops({"signature": []}, {"replace_signatures": oversized})


# ---------------------------------------------------------------------------
# Step 4: concurrent add_signature must not lost-update each other.
# ---------------------------------------------------------------------------

def test_concurrent_add_signature_no_lost_update(monkeypatch):
    user_id = "u_concurrent_list_ops_test"

    # The FAKE plaintext "card" the enclave would decrypt to, and the FAKE
    # ciphertext blob table. `state_lock` only protects the test double's own
    # bookkeeping — it is NOT a stand-in for the lock under test.
    state = {"card": {
        "agent_name": "bro",
        "self_introduction": "keeping it real",
        "signature": ["seed"],
    }}
    state_lock = threading.Lock()
    blobs: dict[tuple[str, str], dict] = {
        (user_id, "identity"): {
            "id": "identity_1", "relationship_started_at": "2026-04-01",
            "relationship_anchor_source": "test",
        },
    }
    _fake_db_blob_layer(monkeypatch, blobs)

    store = _FakeStore(user_id)

    def fake_enclave_get(path, key, params=None, runtime_token=""):
        if path != "/v1/identity/get":
            return {}, ""
        with state_lock:
            snapshot = dict(state["card"])
        # Widen the race window: sleeping here releases the GIL mid-read, so
        # an implementation WITHOUT the per-user lock would let both threads
        # read the same stale signature list and lost-update each other.
        time.sleep(0.05)
        return {"identity": {**snapshot, "decrypt_status": "ok"}}, ""

    def fake_build_envelope(store_arg, plaintext, item_id=None):
        payload = json.loads(plaintext.decode("utf-8"))
        with state_lock:
            state["card"] = payload
        return {
            "id": item_id or "identity_1",
            "body_ct": "ct", "nonce": "n", "K_user": "k",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": user_id, "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_get)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", fake_build_envelope)

    errors: list[Exception] = []

    def add(sig):
        try:
            result, _effects, status = identity_actions._identity_profile_patch(
                store, "test-key",
                {"type": "identity.profile_patch", "patch": {"add_signature": [sig]}},
            )
            assert status == 200, result
        except Exception as exc:  # noqa: BLE001 — surface on the main thread
            errors.append(exc)

    t1 = threading.Thread(target=add, args=("sig-A",))
    t2 = threading.Thread(target=add, args=("sig-B",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, errors
    final_signature = state["card"]["signature"]
    assert set(final_signature) == {"seed", "sig-A", "sig-B"}
    assert len(final_signature) == 3


def test_concurrent_profile_patch_and_dimension_nudge_no_lost_update(monkeypatch):
    # Reviewer-flagged gap: _identity_dimension_nudge is a read->merge->save
    # writer on the SAME identity card as _identity_profile_patch, but wasn't
    # under identity_mutation_lock. A concurrent add_signature (profile_patch)
    # and a dimension nudge could each read the pre-mutation card and
    # lost-update each other's change on save.
    user_id = "u_concurrent_cross_action_test"

    state = {"card": {
        "agent_name": "bro",
        "self_introduction": "keeping it real",
        "signature": ["seed"],
        "dimensions": [{"name": "锐利", "value": 50, "description": "x"}],
    }}
    state_lock = threading.Lock()
    blobs: dict[tuple[str, str], dict] = {
        (user_id, "identity"): {
            "id": "identity_1", "relationship_started_at": "2026-04-01",
            "relationship_anchor_source": "test",
        },
    }
    _fake_db_blob_layer(monkeypatch, blobs)

    store = _FakeStore(user_id)

    def fake_enclave_get(path, key, params=None, runtime_token=""):
        if path != "/v1/identity/get":
            return {}, ""
        with state_lock:
            snapshot = json.loads(json.dumps(state["card"]))
        time.sleep(0.05)  # widen the race window, see test above
        return {"identity": {**snapshot, "decrypt_status": "ok"}}, ""

    def fake_build_envelope(store_arg, plaintext, item_id=None):
        payload = json.loads(plaintext.decode("utf-8"))
        with state_lock:
            state["card"] = payload
        return {
            "id": item_id or "identity_1",
            "body_ct": "ct", "nonce": "n", "K_user": "k",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": user_id, "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_get)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", fake_build_envelope)

    errors: list[Exception] = []

    def do_profile_patch():
        try:
            result, _effects, status = identity_actions._identity_profile_patch(
                store, "test-key",
                {"type": "identity.profile_patch", "patch": {"add_signature": ["sig-A"]}},
            )
            assert status == 200, result
        except Exception as exc:  # noqa: BLE001 — surface on the main thread
            errors.append(exc)

    def do_dimension_nudge():
        try:
            result, _effects, status = identity_actions._identity_dimension_nudge(
                store, "test-key",
                {"type": "identity.dimension_nudge", "dimension": "锐利", "delta": 10},
            )
            assert status == 200, result
        except Exception as exc:  # noqa: BLE001 — surface on the main thread
            errors.append(exc)

    t1 = threading.Thread(target=do_profile_patch)
    t2 = threading.Thread(target=do_dimension_nudge)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, errors
    final_card = state["card"]
    assert set(final_card["signature"]) == {"seed", "sig-A"}
    dims_by_name = {d["name"]: d["value"] for d in final_card["dimensions"]}
    assert dims_by_name["锐利"] == 60


# ---------------------------------------------------------------------------
# C1: per-process lock is not enough across gunicorn worker PROCESSES — the
# DB-level CAS (identity_service._save_identity_cas / db.set_blob_if_unchanged)
# is what actually survives that race, via bounded retry in
# identity_actions._with_identity_mutation_lock_and_retry.
# ---------------------------------------------------------------------------

def test_cross_worker_cas_retry_survives_concurrent_write(monkeypatch):
    """Simulates 2 independent gunicorn worker PROCESSES writing the SAME
    user's identity card. identity_mutation_lock is a plain process-local
    threading.Lock (see identity/service.py) — in real deployment, two
    workers each have their OWN independent lock instance, so worker A's
    lock provides ZERO protection against worker B's write. This test does
    NOT rely on the lock to serialize the two writes.

    Injection point deliberately targets the window this file's C1 follow-up
    fix closed: worker B's complete, independent write lands between A's raw
    -blob snapshot (`_load_identity_snapshot_for_write`, taken BEFORE the
    enclave call) and A's enclave plaintext read — i.e. DURING what would be
    the enclave round trip in production. Before that fix, the raw-blob
    snapshot used for the CAS `expected` value was taken AFTER the enclave
    round trip (inside `_save_identity_action_payload`), so a write landing
    in exactly this window would have made `expected` equal the ALREADY-
    current (post-B) row while the merge was still computed from pre-B
    plaintext — the CAS would have wrongly matched and silently clobbered
    B's write instead of catching it. Reordering the snapshot to before the
    enclave call means this window is now covered too."""
    user_id = "u_cross_worker_cas_test"
    blobs: dict[tuple[str, str], dict] = {
        (user_id, "identity"): {
            "id": "identity_1", "relationship_started_at": "2026-04-01",
            "relationship_anchor_source": "test",
        },
    }
    _fake_db_blob_layer(monkeypatch, blobs)

    store_a = _FakeStore(user_id)  # "worker process A" — the one under test
    store_b = _FakeStore(user_id)  # "worker process B" — same user, a totally
    # independent process in reality; here it just means "a write not driven
    # through A's call, so A's identity_mutation_lock never touches it."

    plain_card = {
        "agent_name": "bro", "self_introduction": "keeping it real",
        "signature": ["seed"],
    }
    enclave_reads = {"n": 0}
    build_calls = {"n": 0}

    def fake_enclave_get(path, key, params=None, runtime_token=""):
        if path != "/v1/identity/get":
            return {}, ""
        enclave_reads["n"] += 1
        if enclave_reads["n"] == 1:
            # Worker B's independent write lands HERE — after A's raw-blob
            # snapshot (already taken by this point, via
            # _load_identity_snapshot_for_write) but before A's OWN read of
            # this enclave endpoint returns. THIS is the window the C1
            # follow-up fix closed (see docstring above) — it did not exist
            # in the pre-fix ordering, where the raw-blob snapshot was taken
            # only after this call returned.
            ok = identity_service._save_identity_cas(
                store_b,
                blobs[(user_id, "identity")],
                {
                    **blobs[(user_id, "identity")],
                    "body_ct": "ct_from_worker_b", "nonce": "n_b", "K_user": "k_b",
                    "visibility": "shared", "owner_user_id": user_id,
                    "updated_at": "2026-04-01T00:00:01",
                },
            )
            assert ok, "worker B's independent write should land cleanly (nothing raced it)"
            plain_card["signature"] = ["seed", "sig-from-B"]
        return {"identity": {**plain_card, "decrypt_status": "ok"}}, ""

    def fake_build_envelope(store_arg, plaintext, item_id=None):
        build_calls["n"] += 1
        if build_calls["n"] > 1:
            # Only mirror an attempt that's actually going to WIN the CAS
            # (attempt 1 always loses here — its expected snapshot predates
            # worker B's write above) — a losing write must not change what
            # the enclave subsequently reports, same as it wouldn't in
            # reality (nothing was actually persisted).
            payload = json.loads(plaintext.decode("utf-8"))
            plain_card.clear()
            plain_card.update(payload)

        return {
            "id": item_id or "identity_1",
            "body_ct": f"ct_from_worker_a_{build_calls['n']}", "nonce": "n_a", "K_user": "k_a",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": user_id, "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_get)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", fake_build_envelope)

    result, effects, status = identity_actions._identity_profile_patch(
        store_a, "test-key",
        {"type": "identity.profile_patch", "patch": {"add_signature": ["sig-from-A"]}},
    )

    assert status == 200, result
    # One enclave read + one envelope build per attempt: attempt 1's raw-blob
    # snapshot predates worker B's write (injected during attempt 1's own
    # enclave read), so attempt 1 loses the CAS; attempt 2 re-snapshots
    # (now sees B's committed row) and wins. Exactly 2 of each confirms the
    # retry path actually ran, not that it coincidentally succeeded first try.
    assert enclave_reads["n"] == 2, "expected exactly one retry after the CAS conflict"
    assert build_calls["n"] == 2
    assert set(plain_card["signature"]) == {"seed", "sig-from-B", "sig-from-A"}
    assert blobs[(user_id, "identity")]["body_ct"] == "ct_from_worker_a_2"
