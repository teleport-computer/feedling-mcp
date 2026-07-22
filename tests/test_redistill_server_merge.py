"""T12: server-side key-level merge for the redistill lane's identity.replace
landing point (genesis.service.replace_identity_preserving_anchor).

Fully DB-free, same pattern as tests/test_identity_list_ops.py's _FakeStore /
_fake_db_blob_layer (Task 2): every db.* call site touched by the code under
test is monkeypatched onto an in-memory dict, plus the enclave decrypt /
envelope-build boundary. Whitelisted in tests/conftest.py's _PURE_UNIT so it
still collects on a no-Postgres machine.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from genesis import service as genesis_service  # noqa: E402
from identity import actions as identity_actions  # noqa: E402
from identity import service as identity_service  # noqa: E402


class _FakeStore:
    """Minimal test double exposing only `user_id` — see
    test_identity_list_ops.py's _FakeStore for the full rationale (a real
    core.store.UserStore eagerly touches DB in __init__)."""

    def __init__(self, user_id: str):
        self.user_id = user_id


def _fake_db_blob_layer(monkeypatch, blobs: dict[tuple[str, str], dict]) -> None:
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


def _seed(user_id: str, blobs: dict, card: dict) -> None:
    blobs[(user_id, "identity")] = {
        "id": "identity_1",
        "relationship_started_at": "2026-04-01",
        "relationship_anchor_source": "test",
    }
    card.clear()
    card.update({
        "agent_name": "旧名",
        "self_introduction": "旧介绍",
        "tone_style": "旧语气",
        "custom_persona_prompt": "用户亲手写的 persona,不可丢",
    })


def _wire_enclave_and_envelope(monkeypatch, card: dict, state_lock: threading.Lock | None = None,
                                *, read_delay: float = 0.0):
    def fake_enclave_get(path, key, params=None, runtime_token=""):
        if path != "/v1/identity/get":
            return {}, ""
        if state_lock is not None:
            with state_lock:
                snapshot = dict(card)
        else:
            snapshot = dict(card)
        if read_delay:
            time.sleep(read_delay)
        return {"identity": {**snapshot, "decrypt_status": "ok"}}, ""

    def fake_build_envelope(store_arg, plaintext, item_id=None):
        payload = json.loads(plaintext.decode("utf-8"))
        if state_lock is not None:
            with state_lock:
                card.clear()
                card.update(payload)
        else:
            card.clear()
            card.update(payload)
        return {
            "id": item_id or "identity_1",
            "body_ct": "ct", "nonce": "n", "K_user": "k",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": "u", "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_get)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", fake_build_envelope)


# ---------------------------------------------------------------------------
# Core field-preservation: a distilled output that never mentions
# custom_persona_prompt must not wipe it (没提的字段永不丢失).
# ---------------------------------------------------------------------------

def test_redistill_merge_preserves_unaddressed_custom_persona_prompt(monkeypatch):
    user_id = "u_redistill_merge_core"
    blobs: dict[tuple[str, str], dict] = {}
    card: dict = {}
    _seed(user_id, blobs, card)
    _fake_db_blob_layer(monkeypatch, blobs)
    _wire_enclave_and_envelope(monkeypatch, card)

    store = _FakeStore(user_id)

    # The distill lane only addressed agent_name/tone_style — it never even
    # looked at custom_persona_prompt (distill_prompt_v1.RESIDENT_IDENTITY_FIELDS
    # doesn't include it).
    status = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": {"agent_name": "新名", "tone_style": "新语气", "dimensions": [
            {"name": "直接", "value": 90, "description": "从材料里看出来的。"},
        ]}},
        "test-api-key",
    )

    assert status == "updated"
    assert card["agent_name"] == "新名"
    assert card["tone_style"] == "新语气"
    # The field the distill lane never addressed survives untouched.
    assert card["custom_persona_prompt"] == "用户亲手写的 persona,不可丢"
    # Unaddressed self_introduction also survives (not blanked to "").
    assert card["self_introduction"] == "旧介绍"


def test_redistill_merge_blank_distilled_field_keeps_existing(monkeypatch):
    """A distilled field present but EMPTY (the model found nothing to derive,
    e.g. agent_name per distill_prompt_v1's _FIELDS_SPEC) must not blank an
    existing value — same "not addressed" semantics as an absent key."""
    user_id = "u_redistill_merge_blank"
    blobs: dict[tuple[str, str], dict] = {}
    card: dict = {}
    _seed(user_id, blobs, card)
    _fake_db_blob_layer(monkeypatch, blobs)
    _wire_enclave_and_envelope(monkeypatch, card)

    store = _FakeStore(user_id)

    status = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": {"agent_name": "", "tone_style": "新语气",
                       "dimensions": [{"name": "直接", "value": 90, "description": "x"}]}},
        "test-api-key",
    )

    assert status == "updated"
    assert card["agent_name"] == "旧名"  # not blanked
    assert card["tone_style"] == "新语气"
    assert card["custom_persona_prompt"] == "用户亲手写的 persona,不可丢"


# ---------------------------------------------------------------------------
# Post-review fix: _identity_replace_payload_has_content used to only count
# agent_name/dimensions/self_introduction/category/signature as "content" —
# a redistill whose only change was a non-core PROFILE_FIELDS entry (the
# EXACT user-authored fields this task exists to protect) was rejected as
# identity_update_empty (fails closed, but silently drops a legitimate
# update). Locks that a tone_style-only or custom_persona_prompt-only
# redistill now lands.
# ---------------------------------------------------------------------------

def test_redistill_tone_style_only_change_is_not_rejected_as_empty(monkeypatch):
    user_id = "u_redistill_has_content_tone_style"
    blobs: dict[tuple[str, str], dict] = {}
    card: dict = {}
    _seed(user_id, blobs, card)
    _fake_db_blob_layer(monkeypatch, blobs)
    _wire_enclave_and_envelope(monkeypatch, card)

    store = _FakeStore(user_id)

    status = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": {"tone_style": "更活泼的新语气"}},
        "test-api-key",
    )

    assert status == "updated"
    assert card["tone_style"] == "更活泼的新语气"
    # Everything else this redistill didn't address survives.
    assert card["agent_name"] == "旧名"
    assert card["custom_persona_prompt"] == "用户亲手写的 persona,不可丢"


def test_redistill_custom_persona_prompt_only_change_is_not_rejected_as_empty(monkeypatch):
    user_id = "u_redistill_has_content_persona_prompt"
    blobs: dict[tuple[str, str], dict] = {}
    card: dict = {}
    _seed(user_id, blobs, card)
    _fake_db_blob_layer(monkeypatch, blobs)
    _wire_enclave_and_envelope(monkeypatch, card)

    store = _FakeStore(user_id)

    status = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": {"custom_persona_prompt": "用户更新后的 persona 指令"}},
        "test-api-key",
    )

    assert status == "updated"
    assert card["custom_persona_prompt"] == "用户更新后的 persona 指令"
    assert card["agent_name"] == "旧名"
    assert card["tone_style"] == "旧语气"


# ---------------------------------------------------------------------------
# Concurrency: a card mutated BETWEEN the distill snapshot and the merge
# landing must not be clobbered — the merge reads the LATEST card at write
# time (not a stale pre-job snapshot), and the CAS retries on a genuine race.
# ---------------------------------------------------------------------------

def test_redistill_merge_survives_patch_inserted_after_snapshot(monkeypatch):
    """Sequence: distill derives its output from OLD material (conceptually
    snapshotted earlier) -> a profile_patch lands and changes
    custom_persona_prompt -> the redistill's replace call finally lands.
    Both edits must be present afterward — the redistill must not resurrect
    the pre-patch custom_persona_prompt."""
    user_id = "u_redistill_merge_sequenced"
    blobs: dict[tuple[str, str], dict] = {}
    card: dict = {}
    _seed(user_id, blobs, card)
    _fake_db_blob_layer(monkeypatch, blobs)
    _wire_enclave_and_envelope(monkeypatch, card)

    store = _FakeStore(user_id)

    # A concurrent edit (e.g. the user, via chat, editing their persona
    # override) lands AFTER the distill material was captured but BEFORE the
    # redistill job's replace call reaches the server.
    result, effects, status_code = identity_actions._identity_profile_patch(
        store, "test-api-key",
        {"type": "identity.profile_patch",
         "patch": {"custom_persona_prompt": "并发编辑:新的 persona 指令"}},
    )
    assert status_code == 200, result

    status = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": {"agent_name": "新名", "tone_style": "新语气",
                       "dimensions": [{"name": "直接", "value": 90, "description": "x"}]}},
        "test-api-key",
    )

    assert status == "updated"
    assert card["agent_name"] == "新名"
    assert card["tone_style"] == "新语气"
    # The concurrent edit is NOT clobbered by the redistill's full-card-shaped
    # (but server-merged) write.
    assert card["custom_persona_prompt"] == "并发编辑:新的 persona 指令"


def test_redistill_merge_retries_on_cross_worker_cas_conflict(monkeypatch):
    """A second, fully independent write (e.g. a different gunicorn worker
    handling a profile_patch for the same user) lands DURING the redistill's
    own enclave round trip — exactly the window identity/actions.py's C1
    follow-up fix closed for profile_patch/dimension_nudge. The redistill's
    CAS must fail on attempt 1 and retry from a fresh read, landing on top of
    (not instead of) the concurrent writer's change."""
    user_id = "u_redistill_merge_cas_retry"
    blobs: dict[tuple[str, str], dict] = {}
    card: dict = {}
    _seed(user_id, blobs, card)
    _fake_db_blob_layer(monkeypatch, blobs)

    store = _FakeStore(user_id)
    enclave_reads = {"n": 0}

    def fake_enclave_get(path, key, params=None, runtime_token=""):
        if path != "/v1/identity/get":
            return {}, ""
        enclave_reads["n"] += 1
        if enclave_reads["n"] == 1:
            # Independent worker's write: lands after this function's
            # raw-blob snapshot (already taken) but before its own enclave
            # read returns.
            ok = identity_service._save_identity_cas(
                store,
                blobs[(user_id, "identity")],
                {**blobs[(user_id, "identity")], "updated_at": "concurrent"},
            )
            assert ok, "the independent write should land cleanly"
            card["custom_persona_prompt"] = "并发 worker 写入的新值"
        return {"identity": {**card, "decrypt_status": "ok"}}, ""

    build_calls = {"n": 0}

    def fake_build_envelope(store_arg, plaintext, item_id=None):
        build_calls["n"] += 1
        if build_calls["n"] > 1:
            # Only mirror an attempt that's actually going to WIN the CAS —
            # attempt 1 always loses here (its `snapshot` predates the
            # concurrent worker's write) — a losing write must not change
            # what the enclave subsequently reports, same as it wouldn't in
            # reality (nothing was actually persisted).
            payload = json.loads(plaintext.decode("utf-8"))
            card.clear()
            card.update(payload)
        return {
            "id": item_id or "identity_1",
            "body_ct": f"ct_{build_calls['n']}", "nonce": "n", "K_user": "k",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": user_id, "enclave_pk_fpr": "test",
        }, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_get)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", fake_build_envelope)

    status = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": {"agent_name": "新名",
                       "dimensions": [{"name": "直接", "value": 90, "description": "x"}]}},
        "test-api-key",
    )

    assert status == "updated"
    assert enclave_reads["n"] == 2  # attempt 1 (loses CAS) + attempt 2 (wins)
    assert card["agent_name"] == "新名"
    # The concurrent worker's write is preserved, not clobbered by attempt 1's
    # stale-relative-to-it merge (which never won the CAS).
    assert card["custom_persona_prompt"] == "并发 worker 写入的新值"
