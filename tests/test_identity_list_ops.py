"""Pure-unit tests for explicit list-field ops (add/remove/replace) + the
per-user identity mutation lock that closes the lost-update window across
concurrent identity.profile_patch calls.

`apply_list_ops` tests are pure functions (no DB). The concurrency test
drives the real `_identity_profile_patch` path but stays DB-free by
monkeypatching every db.* call site it touches (get_blob/set_blob/log_append)
plus the enclave decrypt / envelope-build boundary — see its docstring for
why each patch point is necessary.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import db
from core import enclave as core_enclave
from core import envelope as core_envelope
from core.store import UserStore
from identity import actions as identity_actions


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
        (user_id, "identity"): {"id": "identity_1", "relationship_started_at": "2026-04-01"},
    }

    def fake_get_blob(uid, kind):
        return blobs.get((uid, kind))

    def fake_set_blob(uid, kind, doc):
        blobs[(uid, kind)] = doc

    def fake_log_append(uid, stream, doc):
        return None

    # Patch db.* BEFORE constructing UserStore: its __init__ eagerly loads/
    # persists tokens/frames_meta/etc against the real db module, and we want
    # this test fully DB-free rather than tripping FK errors against a real
    # (unregistered) user_id.
    monkeypatch.setattr(db, "get_blob", fake_get_blob)
    monkeypatch.setattr(db, "set_blob", fake_set_blob)
    monkeypatch.setattr(db, "log_append", fake_log_append)

    store = UserStore(user_id)

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
