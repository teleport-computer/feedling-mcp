from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import service  # noqa: E402


def _store(user_id: str = "usr_genesis"):
    return types.SimpleNamespace(user_id=user_id)


def _chunk_meta(user_id: str = "usr_genesis", *, body: bytes = b"ciphertext") -> dict:
    return {
        "v": 1,
        "id": "genesis_chunk_job_1_0",
        "body_ct": base64.b64encode(body).decode("ascii"),
        "nonce": "nonce_b64",
        "K_user": "ku_b64",
        "K_enclave": "ke_b64",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "fpr",
    }


def test_genesis_state_maps_active_job_to_processing_gate_status(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda user_id, kind, doc: captured.update({"user_id": user_id, "kind": kind, "doc": doc}),
    )

    state = service.write_genesis_state(_store(), {"job_id": "job_1", "status": "uploading"})

    assert state["status"] == "processing"
    assert state["job_status"] == "uploading"
    assert captured["kind"] == service.GENESIS_STATE_BLOB
    assert captured["doc"]["status"] == "processing"


def test_genesis_state_preserves_uploaded_done_failed_gate_status(monkeypatch):
    states = []
    monkeypatch.setattr(service.db, "set_blob", lambda _user_id, _kind, doc: states.append(doc))

    service.write_genesis_state(_store(), {"job_id": "job_1", "status": "uploaded"}, status="uploaded")
    service.write_genesis_state(_store(), {"job_id": "job_1", "status": "done"})
    service.write_genesis_state(_store(), {"job_id": "job_1", "status": "failed", "error": "boom"})

    assert [state["status"] for state in states] == ["uploaded", "done", "failed"]
    assert states[-1]["error"] == "boom"


def test_create_import_job_writes_state_only_after_real_upload_start(monkeypatch):
    captured = {}

    def fake_create(user_id, job):
        assert user_id == "usr_genesis"
        assert job["metadata"]["privacy_copy"] == service.PRIVACY_COPY
        return {
            "job_id": job["job_id"],
            "status": "created",
            "privacy_mode": job["privacy_mode"],
            "memory_action_count": 0,
        }

    monkeypatch.setattr(service.db, "genesis_create_job", fake_create)
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda user_id, kind, doc: captured.update({"user_id": user_id, "kind": kind, "doc": doc}),
    )

    job, status = service.create_import_job(
        _store(),
        {"job_id": "job_1", "source_kind": "chat_export", "total_chunks": 2},
    )

    assert status == 201
    assert job["job_id"] == "job_1"
    assert captured["kind"] == service.GENESIS_STATE_BLOB
    assert captured["doc"]["status"] == "processing"


def test_create_import_job_drops_plaintext_metadata(monkeypatch):
    saved = {}

    def fake_create(_user_id, job):
        saved.update(job)
        return {"job_id": job["job_id"], "status": "created", "privacy_mode": job["privacy_mode"]}

    monkeypatch.setattr(service.db, "genesis_create_job", fake_create)
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    service.create_import_job(_store(), {
        "job_id": "job_1",
        "metadata": {
            "transcript": "raw chat should not persist",
            "ai_persona": "raw persona should not persist",
            "file_manifest_hash": "abc123",
            "file_count": 2,
            "timeline_span_days": 7,
            "distill_model": "claude-haiku-4-5",
        },
    })

    metadata = saved["metadata"]
    assert metadata["file_manifest_hash"] == "abc123"
    assert metadata["file_count"] == 2
    assert metadata["timeline_span_days"] == 7
    assert metadata["distill_model"] == "claude-haiku-4-5"
    assert metadata["privacy_copy"] == service.PRIVACY_COPY
    assert "transcript" not in metadata
    assert "ai_persona" not in metadata


def test_create_import_job_does_not_trust_payload_status(monkeypatch):
    saved = {}

    def fake_create(_user_id, job):
        saved.update(job)
        return {"job_id": job["job_id"], "status": job["status"]}

    monkeypatch.setattr(service.db, "genesis_create_job", fake_create)
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    service.create_import_job(_store(), {"job_id": "job_1", "status": "processing"})
    assert saved["status"] == "created"

    saved.clear()
    service.create_import_job(
        _store(),
        {"job_id": "job_2"},
        initial_status="processing",
    )
    assert saved["status"] == "processing"


def test_create_import_job_is_idempotent_for_existing_job(monkeypatch):
    monkeypatch.setattr(service.db, "genesis_create_job", lambda *_args: None)
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading"},
    )
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    job, status = service.create_import_job(_store(), {"job_id": "job_1"})

    assert status == 200
    assert job == {"job_id": "job_1", "status": "uploading"}


def test_finalize_upload_blocks_gate_when_chunks_missing(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 3},
    )
    monkeypatch.setattr(service.db, "genesis_missing_chunk_seqs", lambda *_args: [1])
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda _user_id, _kind, doc: captured.update(doc),
    )

    _job, missing = service.finalize_upload(_store(), "job_1")

    assert missing == [1]
    assert captured["status"] == "processing"
    assert captured["job_status"] == "uploading"


def test_finalize_upload_sets_uploaded_gate_status_when_complete(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 2},
    )
    monkeypatch.setattr(service.db, "genesis_missing_chunk_seqs", lambda *_args: [])
    monkeypatch.setattr(
        service.db,
        "genesis_mark_finalized",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploaded", "total_chunks": 2},
    )
    monkeypatch.setattr(service.db, "set_blob", lambda _user_id, _kind, doc: captured.update(doc))

    job, missing = service.finalize_upload(_store(), "job_1")

    assert missing == []
    assert job["status"] == "uploaded"
    assert captured["status"] == "uploaded"
    assert captured["job_status"] == "uploaded"


def test_put_chunk_requires_v1_envelope_meta(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 1},
    )

    try:
        service.put_chunk(
            _store(),
            "job_1",
            seq=0,
            encrypted_body=b"ciphertext",
            byte_start=0,
            byte_end=10,
        )
    except ValueError as e:
        assert str(e) == "chunk_envelope_required"
    else:
        raise AssertionError("expected missing envelope metadata to be rejected")


def test_put_chunk_stores_envelope_meta_without_body_ct(monkeypatch):
    captured = {}
    body = b"ciphertext"
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 1},
    )

    def fake_put(_user_id, _job_id, **kwargs):
        captured.update(kwargs)
        return {"seq": kwargs["seq"], "aad": kwargs["aad"]}

    monkeypatch.setattr(service.db, "genesis_put_chunk", fake_put)
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: None)

    chunk = service.put_chunk(
        _store(),
        "job_1",
        seq=0,
        encrypted_body=body,
        byte_start=0,
        byte_end=len(body),
        envelope_meta=_chunk_meta(body=body),
    )

    meta = captured["aad"]["envelope_meta"]
    assert chunk["seq"] == 0
    assert meta["owner_user_id"] == "usr_genesis"
    assert meta["K_enclave"] == "ke_b64"
    assert "body_ct" not in meta


def test_put_chunk_rejects_cross_user_envelope_meta(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploading", "total_chunks": 1},
    )

    try:
        service.put_chunk(
            _store("usr_genesis"),
            "job_1",
            seq=0,
            encrypted_body=b"ciphertext",
            byte_start=0,
            byte_end=10,
            envelope_meta=_chunk_meta("usr_other"),
        )
    except ValueError as e:
        assert str(e) == "chunk_envelope_owner_mismatch"
    else:
        raise AssertionError("expected cross-user chunk envelope to be rejected")


def test_chunk_envelope_from_row_reconstructs_worker_decrypt_payload():
    body = b"ciphertext"
    meta = dict(_chunk_meta(body=body))
    meta.pop("body_ct")
    envelope = service.chunk_envelope_from_row({
        "encrypted_body": body,
        "aad": {"envelope_meta": meta},
    })

    assert envelope["body_ct"] == base64.b64encode(body).decode("ascii")
    assert envelope["owner_user_id"] == "usr_genesis"
    assert envelope["id"] == "genesis_chunk_job_1_0"


def test_apply_reducer_output_writes_persona_and_done_state(monkeypatch):
    blobs = []
    outputs = []

    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploaded", "total_chunks": 1},
    )
    monkeypatch.setattr(
        service.db,
        "genesis_set_job_status",
        lambda _user_id, _job_id, **_kwargs: {"job_id": "job_1", "status": "processing"},
    )
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda _user_id, kind, doc: blobs.append({"kind": kind, "doc": doc}),
    )
    monkeypatch.setattr(service.db, "get_blob", lambda *_args: None)
    monkeypatch.setattr(
        service.db,
        "genesis_upsert_output",
        lambda _user_id, _job_id, output_type, **kwargs: outputs.append({"type": output_type, **kwargs}),
    )
    monkeypatch.setattr(
        service.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: {
            "job_id": "job_1",
            "status": "done",
            **kwargs,
        },
    )
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *_args: (2, [{"memory": {"id": "m1"}}]))
    monkeypatch.setattr(service, "init_identity_if_absent", lambda *_args: "initialized")
    monkeypatch.setattr(
        service.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _plaintext, item_id=None: ({
            "id": item_id,
            "body_ct": "encrypted_persona",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
        }, ""),
    )

    result = service.apply_reducer_output(
        _store(),
        "api_key",
        "job_1",
        {"persona": {"content": "You remember the user's voice.", "prompt_version": "7.B"}},
    )

    assert result["memory_action_count"] == 2
    assert result["identity_status"] == "initialized"
    persona_blob = next(blob for blob in blobs if blob["kind"] == service.GENESIS_PERSONA_BLOB)
    assert persona_blob["doc"]["encrypted"] is True
    assert persona_blob["doc"]["content_envelope"]["body_ct"] == "encrypted_persona"
    assert "content" not in persona_blob["doc"]
    state_blob = [blob for blob in blobs if blob["kind"] == service.GENESIS_STATE_BLOB][-1]
    assert state_blob["doc"]["status"] == "done"
    reducer_doc = next(output["doc"] for output in outputs if output["type"] == "reducer")
    reducer_json = json.dumps(reducer_doc, ensure_ascii=False)
    assert reducer_doc["plaintext_stored"] is False
    assert reducer_doc["persona_provided"] is True
    assert "You remember the user's voice." not in reducer_json
    assert any(output["type"] == "apply" for output in outputs)


def test_apply_reducer_output_can_defer_job_completion(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda *_args: {"job_id": "job_1", "status": "processing"},
    )
    monkeypatch.setattr(service.db, "genesis_set_job_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.db, "genesis_upsert_output", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service.db,
        "genesis_complete_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("foreground must not complete the job")),
    )
    monkeypatch.setattr(service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.notices, "resolve", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *_args: (2, []))
    monkeypatch.setattr(service, "init_identity_if_absent", lambda *_args: "initialized")
    monkeypatch.setattr(service, "write_persona_artifact", lambda *_args: ("persona-ref", "persona-sha"))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *_args: ("voice-ref", "voice-sha"))

    result = service.apply_reducer_output(
        _store(),
        "api-key",
        "job_1",
        {"memories": [{"summary": "foreground"}]},
        complete_job=False,
    )

    assert result["memory_action_count"] == 2
    assert result["identity_status"] == "initialized"
    assert result["persona_ref"] == "persona-ref"


def test_write_persona_artifact_keeps_existing_higher_priority_persona(monkeypatch):
    writes = []

    monkeypatch.setattr(
        service.db,
        "get_blob",
        lambda _user_id, kind: {"source_priority": 100, "sha256": "existing_sha"} if kind == service.GENESIS_PERSONA_BLOB else None,
    )
    monkeypatch.setattr(service.db, "set_blob", lambda *_args: writes.append(_args))
    monkeypatch.setattr(
        service.core_envelope,
        "_build_shared_envelope_for_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no overwrite")),
    )

    ref, digest = service.write_persona_artifact(
        _store(),
        "job_history",
        {
            "source_kind": "chat_export",
            "source_family": "history",
            "persona": {"content": "history-derived persona", "prompt_version": "7.B"},
        },
    )

    assert ref == service.GENESIS_PERSONA_REF
    assert digest == "existing_sha"
    assert writes == []


def test_write_voice_artifact_encrypts_workset_without_plaintext(monkeypatch):
    blobs = []
    captured_plaintext = {}

    def fake_envelope(_store, plaintext, item_id=None):
        captured_plaintext["raw"] = plaintext
        return ({
            "id": item_id,
            "body_ct": "encrypted_voice",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
        }, "")

    monkeypatch.setattr(service.db, "set_blob", lambda _user_id, kind, doc: blobs.append({"kind": kind, "doc": doc}))
    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)

    ref, digest = service.write_voice_artifact(
        _store(),
        "job_history",
        {
            "source_kind": "chat_export",
            "source_family": "history",
            "voice_workset": {
                "behavior_notes": ["短句接住情绪"],
                "exemplars": [{
                    "turns": [{"role": "ta", "text": "别急,我在。"}],
                    "founding": True,
                    "axis": ["emotion"],
                }],
            },
        },
    )

    assert ref == service.GENESIS_VOICE_REF
    assert digest
    assert "别急".encode("utf-8") in captured_plaintext["raw"]
    voice_blob = blobs[0]
    assert voice_blob["kind"] == service.GENESIS_VOICE_BLOB
    assert voice_blob["doc"]["encrypted"] is True
    assert voice_blob["doc"]["content_envelope"]["body_ct"] == "encrypted_voice"
    assert voice_blob["doc"]["behavior_note_count"] == 1
    assert voice_blob["doc"]["founding_exemplar_count"] == 1
    assert "别急" not in json.dumps(voice_blob["doc"], ensure_ascii=False)


def test_apply_reducer_output_rejects_raw_transcript_fields(monkeypatch):
    monkeypatch.setattr(
        service.db,
        "genesis_get_job",
        lambda _user_id, _job_id: {"job_id": "job_1", "status": "uploaded", "total_chunks": 1},
    )

    try:
        service.apply_reducer_output(_store(), "api_key", "job_1", {"raw_text": "do not send raw text"})
    except ValueError as e:
        assert str(e) == "raw_reducer_field_not_allowed:raw_text"
    else:
        raise AssertionError("expected raw reducer output to be rejected")


def test_identity_payload_from_output_leaves_intro_and_signature_for_respawn():
    payload = service._identity_payload_from_output(
        {
            "identity": {
                "agent_name": "Assistant",
                "self_introduction": "I should not be written by genesis.",
                "signature": ["not yet"],
                "dimensions": [
                    {"name": "Direct", "value": 82, "description": "TA often gives blunt feedback."},
                    {"name": "Warmth", "value": 60},
                ],
            }
        }
    )

    assert payload == {
        "agent_name": "",
        "self_introduction": "",
        "category": "Direct",
        "dimensions": [
            {"name": "Direct", "value": 82, "description": "TA often gives blunt feedback."}
        ],
    }


def test_identity_payload_from_output_passes_category_through():
    payload = service._identity_payload_from_output(
        {
            "identity": {
                "agent_name": "Mira",
                "category": "  细心 · 稳定。 ",
                "dimensions": [
                    {"name": "细心驱动", "value": 90, "description": "Always checks details."},
                    {"name": "稳定型", "value": 40, "description": "Keeps the user steady."},
                ],
            }
        }
    )

    assert payload is not None
    assert payload["category"] == "细心 · 稳定"


def test_identity_payload_from_output_derives_category_from_dimensions():
    payload = service._identity_payload_from_output(
        {
            "identity": {
                "agent_name": "Mira",
                "dimensions": [
                    {"name": "稳定型", "value": 35, "description": "Keeps the room quiet."},
                    {"name": "好奇驱动", "value": 91, "description": "Asks sharp questions."},
                    {"name": "观察性", "value": 70, "description": "Notices small shifts."},
                ],
            }
        }
    )

    assert payload is not None
    assert payload["category"] == "好奇 · 稳定"


def test_identity_payload_from_output_without_dimensions_leaves_category_empty():
    payload = service._identity_payload_from_output(
        {"identity": {"agent_name": "Mira", "category": "", "dimensions": []}}
    )

    assert payload is not None
    assert "category" not in payload


def test_identity_payload_from_output_ignores_empty_identity():
    assert service._identity_payload_from_output({"identity": {"agent_name": "", "dimensions": []}}) is None


def test_identity_payload_from_output_preserves_valid_user_preferred_name():
    payload = service._identity_payload_from_output({
        "identity": {
            "agent_name": "Mira",
            "dimensions": [],
            "user_preferred_name": " Seven ",
        }
    })

    assert payload is not None
    assert payload["user_preferred_name"] == "Seven"


# ---------------------------------------------------------------------------
# B2 (reverses I7): the 4 remaining user-layer fields — GROUNDED, so present-
# in-material -> kept, absent -> just missing from the payload (never invented,
# never a crash).
# ---------------------------------------------------------------------------

def test_identity_payload_from_output_carries_the_4_remaining_user_layer_fields():
    payload = service._identity_payload_from_output({
        "identity": {
            "agent_name": "Mira",
            "dimensions": [],
            "custom_persona_prompt": "永远用简短的第二人称回复我。",
            "language_preference": "中文",
            "relationship_anchor": "大学室友",
            "stable_definitions": ["老板=我上司", "  ", "deadline 一律北京时间"],
        }
    })

    assert payload is not None
    assert payload["custom_persona_prompt"] == "永远用简短的第二人称回复我。"
    assert payload["language_preference"] == "中文"
    assert payload["relationship_anchor"] == "大学室友"
    # blank list entries are dropped, not preserved as empty strings
    assert payload["stable_definitions"] == ["老板=我上司", "deadline 一律北京时间"]


def test_identity_payload_from_output_caps_long_user_layer_strings():
    long_text = "长" * 2000
    payload = service._identity_payload_from_output({
        "identity": {
            "agent_name": "Mira", "dimensions": [],
            "custom_persona_prompt": long_text,
            "relationship_anchor": long_text,
            "language_preference": long_text,
        }
    })

    assert payload is not None
    assert len(payload["custom_persona_prompt"]) == 1200
    assert len(payload["relationship_anchor"]) == 1200
    assert len(payload["language_preference"]) == 240


def test_identity_payload_from_output_material_without_user_layer_signal_omits_them():
    # Grounding: no signal in the material -> the payload simply doesn't carry
    # the key at all (never an invented empty string / never a crash).
    payload = service._identity_payload_from_output({
        "identity": {"agent_name": "Mira", "dimensions": [
            {"name": "细心", "value": 80, "description": "d"},
        ]}
    })

    assert payload is not None
    for key in ("user_preferred_name", "custom_persona_prompt", "language_preference",
                "relationship_anchor", "stable_definitions"):
        assert key not in payload, key


def test_identity_payload_from_output_sparse_material_with_only_persona_directive():
    # B2 broadens the "has any signal" gate: a persona directive alone (no
    # agent_name, no dimensions) must not make this return None outright —
    # a new card can start from just that.
    payload = service._identity_payload_from_output({
        "identity": {"agent_name": "", "dimensions": [],
                      "custom_persona_prompt": "永远直接回答,不要绕。"}
    })

    assert payload is not None
    assert payload["custom_persona_prompt"] == "永远直接回答,不要绕。"


def test_init_identity_threads_the_5_user_layer_fields_on_a_new_card(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: None)

    def fake_envelope(_store, plaintext, item_id=None):
        captured["plaintext"] = json.loads(plaintext.decode("utf-8"))
        return ({
            "id": item_id or "identity_new",
            "body_ct": "encrypted_identity", "nonce": "nonce", "K_user": "ku",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": "usr_genesis", "enclave_pk_fpr": "fpr",
        }, "")

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(service.identity_service, "_save_identity",
                         lambda _store, doc: captured.update({"saved": doc}))
    monkeypatch.setattr(service.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)
    monkeypatch.setattr(service.identity_service, "_append_identity_change", lambda *_a, **_k: None)

    status = service.init_identity_if_absent(
        _store(),
        {
            "identity": {
                "agent_name": "Mira",
                "dimensions": [{"name": "Steady", "value": 84, "description": "Persona says steady."}],
                "user_preferred_name": "Seven",
                "custom_persona_prompt": "始终用第二人称、简短直接。",
                "language_preference": "中文",
                "relationship_anchor": "大学室友",
                "stable_definitions": ["老板=我上司"],
            },
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_evidence": "persona card named Mira",
        },
        None,
        "runtime_token_1",
    )

    assert status == "initialized"
    plaintext = captured["plaintext"]
    assert plaintext["user_preferred_name"] == "Seven"
    assert plaintext["custom_persona_prompt"] == "始终用第二人称、简短直接。"
    assert plaintext["language_preference"] == "中文"
    assert plaintext["relationship_anchor"] == "大学室友"
    assert plaintext["stable_definitions"] == ["老板=我上司"]


def test_init_identity_without_user_layer_fields_leaves_new_card_empty_no_crash(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: None)

    def fake_envelope(_store, plaintext, item_id=None):
        captured["plaintext"] = json.loads(plaintext.decode("utf-8"))
        return ({
            "id": item_id or "identity_new",
            "body_ct": "encrypted_identity", "nonce": "nonce", "K_user": "ku",
            "K_enclave": "ke", "visibility": "shared",
            "owner_user_id": "usr_genesis", "enclave_pk_fpr": "fpr",
        }, "")

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(service.identity_service, "_save_identity",
                         lambda _store, doc: captured.update({"saved": doc}))
    monkeypatch.setattr(service.boot_gates, "_log_bootstrap_event", lambda *_a, **_k: None)
    monkeypatch.setattr(service.identity_service, "_append_identity_change", lambda *_a, **_k: None)

    status = service.init_identity_if_absent(
        _store(),
        {"identity": {"agent_name": "Mira", "dimensions": []},
         "relationship_started_at": "2026-06-01",
         "relationship_anchor_evidence": "persona card named Mira"},
        None,
        "runtime_token_1",
    )

    assert status == "initialized"
    plaintext = captured["plaintext"]
    for key in ("user_preferred_name", "custom_persona_prompt", "language_preference",
                "relationship_anchor", "stable_definitions"):
        assert key not in plaintext, key


def test_init_identity_upserts_genesis_fields_and_preserves_agent_profile(monkeypatch):
    captured: dict = {}
    existing = {
        "id": "identity_1",
        "created_at": "2026-06-01T00:00:00",
        "relationship_anchor_source": service.GENESIS_SOURCE,
    }

    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: existing)
    monkeypatch.setattr(
        service.core_enclave,
        "_enclave_get_json_for_gate",
        lambda _path, _api_key, **kwargs: (
            captured.update({"runtime_token": kwargs.get("runtime_token")})
            or ({
                "identity": {
                    "agent_name": "Old",
                    "self_introduction": "I wrote this after respawn.",
                    "signature": ["Still here", "Receipts first"],
                    "dimensions": [{"name": "OldDim", "value": 10, "description": "old"}],
                }
            }, "")
        ),
    )

    def fake_envelope(_store, plaintext, item_id=None):
        captured["plaintext"] = json.loads(plaintext.decode("utf-8"))
        captured["item_id"] = item_id
        return ({
            "id": item_id,
            "body_ct": "encrypted_identity",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
            "enclave_pk_fpr": "fpr",
        }, "")

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(
        service.identity_service,
        "_save_identity",
        lambda _store, doc: captured.update({"saved": doc}),
    )
    monkeypatch.setattr(service.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.identity_service, "_append_identity_change", lambda *_args, **_kwargs: None)

    status = service.init_identity_if_absent(
        _store(),
        {
            "identity": {
                "agent_name": "Mira",
                "category": "细心 · 稳定",
                "dimensions": [{"name": "Steady", "value": 84, "description": "Persona says steady."}],
            },
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_evidence": "persona card named Mira",
        },
        None,
        "runtime_token_1",
    )

    assert status == "updated"
    assert captured["runtime_token"] == "runtime_token_1"
    assert captured["item_id"] == "identity_1"
    assert captured["plaintext"]["agent_name"] == "Mira"
    assert captured["plaintext"]["category"] == "细心 · 稳定"
    assert captured["plaintext"]["dimensions"][0]["name"] == "Steady"
    assert captured["plaintext"]["self_introduction"] == "I wrote this after respawn."
    assert captured["plaintext"]["signature"] == ["Still here", "Receipts first"]
    assert captured["saved"]["id"] == "identity_1"
    assert captured["saved"]["created_at"] == "2026-06-01T00:00:00"
    assert captured["saved"]["relationship_started_at"] == "2026-06-01"
    assert captured["saved"]["identity_agent_name_present"] is True
    assert captured["saved"]["identity_dimension_count"] == 1


def test_replace_identity_preserving_anchor_replaces_body_only(monkeypatch):
    captured: dict = {}
    existing = {
        "id": "identity_existing",
        "created_at": "2026-05-01T00:00:00",
        "relationship_started_at": "2025-01-02",
        "relationship_anchor_source": "user_confirmed",
        "relationship_anchor_evidence": "typed date",
        "identity_agent_name_present": True,
        "identity_dimension_count": 2,
    }

    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: existing)
    monkeypatch.setattr(
        service.core_enclave,
        "_enclave_get_json_for_gate",
        lambda _path, _api_key, **_kwargs: (
            {"identity": {
                "agent_name": "旧名",
                "self_introduction": "旧介绍",
                "signature": ["旧签名"],
                "decrypt_status": "ok",
            }},
            "",
        ),
    )

    def fake_envelope(_store, plaintext, item_id=None):
        captured["plaintext"] = json.loads(plaintext.decode("utf-8"))
        captured["item_id"] = item_id
        return ({
            "id": item_id,
            "body_ct": "encrypted_new_identity",
            "nonce": "nonce_new",
            "K_user": "ku_new",
            "K_enclave": "ke_new",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
            "enclave_pk_fpr": "fpr_new",
        }, "")

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(
        service.identity_service,
        "_save_identity_cas",
        lambda _store, _expected, doc: captured.update({"saved": doc}) or True,
    )
    monkeypatch.setattr(service.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.identity_service, "_append_identity_change", lambda *_args, **_kwargs: None)

    status = service.replace_identity_preserving_anchor(
        _store(),
        {
            "identity": {
                "agent_name": "乔伊",
                "category": "创意 · 活泼",
                "self_introduction": "我是乔伊。",
                "dimensions": [{"name": "创造力", "value": 91, "description": "广告设计师和自媒体创作者。"}],
            },
            "relationship_started_at": "2099-12-31",
            "days_with_user": 9999,
            "relationship_anchor_evidence": "must not overwrite",
        },
        "test-api-key",
    )

    assert status == "updated"
    assert captured["item_id"] == "identity_existing"
    assert captured["plaintext"]["agent_name"] == "乔伊"
    assert captured["plaintext"]["category"] == "创意 · 活泼"
    assert captured["plaintext"]["dimensions"][0]["name"] == "创造力"
    # T12: fields the distilled output didn't address (e.g. signature) survive
    # the replace — key-level overlay onto the LATEST decrypted card, not a
    # blind full-body overwrite.
    assert captured["plaintext"]["signature"] == ["旧签名"]
    assert captured["saved"]["id"] == "identity_existing"
    assert captured["saved"]["created_at"] == "2026-05-01T00:00:00"
    assert captured["saved"]["relationship_started_at"] == "2025-01-02"
    assert captured["saved"]["relationship_anchor_source"] == "user_confirmed"
    assert captured["saved"]["relationship_anchor_evidence"] == "typed date"
    assert captured["saved"]["body_ct"] == "encrypted_new_identity"


def test_replace_identity_preserving_anchor_initializes_missing_identity(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: None)

    def fake_envelope(_store, plaintext, item_id=None):
        captured["plaintext"] = json.loads(plaintext.decode("utf-8"))
        captured["item_id"] = item_id
        return ({
            "id": "identity_created",
            "body_ct": "encrypted_identity",
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
            "enclave_pk_fpr": "fpr",
        }, "")

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(
        service.identity_service,
        "_save_identity",
        lambda _store, doc: captured.update({"saved": doc}),
    )
    monkeypatch.setattr(service.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.identity_service, "_append_identity_change", lambda *_args, **_kwargs: None)

    status = service.replace_identity_preserving_anchor(
        _store(),
        {
            "identity": {
                "agent_name": "乔伊",
                "dimensions": [{"name": "直爽", "value": 90, "description": "说人话。"}],
            },
            "relationship_anchor": {
                "relationship_started_at": "2025-01-02",
                "days_with_user": 558,
                "relationship_anchor_evidence": "uploaded role card date",
            },
        },
    )

    assert status == "initialized"
    assert captured["item_id"] is None
    assert captured["plaintext"]["agent_name"] == "乔伊"
    assert captured["saved"]["id"] == "identity_created"
    assert captured["saved"]["relationship_started_at"] == "2025-01-02"
    assert captured["saved"]["relationship_anchor_evidence"] == "uploaded role card date"
    assert captured["saved"]["identity_agent_name_present"] is True
    assert captured["saved"]["identity_dimension_count"] == 1


def test_replace_identity_preserving_anchor_allows_nameless_nonempty_update(monkeypatch):
    captured: dict = {}
    existing = {
        "id": "identity_existing",
        "created_at": "2026-05-01T00:00:00",
        "relationship_started_at": "2025-01-02",
        "identity_agent_name_present": True,
        "identity_dimension_count": 1,
    }
    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: existing)
    monkeypatch.setattr(
        service.core_enclave,
        "_enclave_get_json_for_gate",
        lambda _path, _api_key, **_kwargs: (
            {"identity": {"agent_name": "", "decrypt_status": "ok"}}, "",
        ),
    )

    def fake_envelope(_store, plaintext, item_id=None):
        captured["plaintext"] = json.loads(plaintext.decode("utf-8"))
        captured["item_id"] = item_id
        return ({
            "id": item_id,
            "body_ct": "encrypted_nameless_identity",
            "nonce": "nonce_new",
            "K_user": "ku_new",
            "K_enclave": "ke_new",
            "visibility": "shared",
            "owner_user_id": "usr_genesis",
            "enclave_pk_fpr": "fpr_new",
        }, "")

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", fake_envelope)
    monkeypatch.setattr(
        service.identity_service,
        "_save_identity_cas",
        lambda _store, _expected, doc: captured.update({"saved": doc}) or True,
    )
    monkeypatch.setattr(service.boot_gates, "_log_bootstrap_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.identity_service, "_append_identity_change", lambda *_args, **_kwargs: None)

    status = service.replace_identity_preserving_anchor(
        _store(),
        {
            "identity": {
                "agent_name": "",
                "category": "硬核 · 直爽",
                "self_introduction": "我是懂你的全栈 AI 协作者。",
                "dimensions": [{"name": "直爽", "value": 90, "description": "说人话，不绕弯。"}],
            },
        },
        "test-api-key",
    )

    assert status == "updated"
    assert captured["item_id"] == "identity_existing"
    assert captured["plaintext"]["agent_name"] == ""
    assert captured["plaintext"]["category"] == "硬核 · 直爽"
    assert captured["plaintext"]["self_introduction"] == "我是懂你的全栈 AI 协作者。"
    assert captured["plaintext"]["dimensions"][0]["name"] == "直爽"
    assert captured["saved"]["id"] == "identity_existing"
    assert captured["saved"]["body_ct"] == "encrypted_nameless_identity"


def test_replace_identity_preserving_anchor_rejects_empty_update(monkeypatch):
    existing = {
        "id": "identity_existing",
        "created_at": "2026-05-01T00:00:00",
        "relationship_started_at": "2025-01-02",
        "identity_agent_name_present": True,
        "identity_dimension_count": 1,
    }
    monkeypatch.setattr(service.identity_service, "_load_identity", lambda _store: existing)
    monkeypatch.setattr(
        service.core_envelope,
        "_build_shared_envelope_for_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not overwrite identity")),
    )
    monkeypatch.setattr(
        service.identity_service,
        "_save_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not save identity")),
    )

    status = service.replace_identity_preserving_anchor(
        _store(),
        {"identity": {"agent_name": "", "dimensions": [], "self_introduction": "", "category": "", "signature": []}},
    )

    assert status == "identity_update_empty"


def test_apply_memory_outputs_batches_memory_actions(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {
            "status": "ok",
            "results": [{"memory": {"id": f"m{len(calls)}_{idx}"}} for idx, _action in enumerate(actions)],
        }, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    memories = [
        {
            "type": "fact",
            "summary": f"Fact {idx}",
            "content": f"Memory: Fact {idx}",
            "bucket": "Imported",
        }
        for idx in range(25)
    ]

    count, results = service.apply_memory_outputs(_store(), "api_key", {"memories": memories})

    assert count == 25
    assert len(results) == 25
    assert [len(call) for call in calls] == [20, 5]
    assert calls[0][0]["memory"]["occurred_at"] == ""


def test_apply_memory_outputs_preserves_memory_summary_dates_tags_and_fallback(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {
            "status": "ok",
            "results": [{"memory": {"id": f"m{idx}"}} for idx, _action in enumerate(actions)],
        }, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    output = {
        "source_family": "memory_summary",
        "relationship_started_at": "2023-01-02",
        "memories": [
            {
                "type": "fact",
                "summary": "Spring archive",
                "content": "Spring archive memory.",
                "threads": ["relationship", "stable"],
                "tags": ["stable", "archive"],
                "date": "2024-05-09",
            },
            {
                "type": "moment",
                "summary": "Undated archive",
                "content": "Undated archive memory.",
                "tags": "comfort, archive",
            },
        ],
    }

    count, results = service.apply_memory_outputs(_store(), "api_key", output)

    assert count == 2
    assert len(results) == 2
    assert calls[0][0]["memory"]["occurred_at"] == "2024-05-09"
    assert calls[0][0]["memory"]["threads"] == ["relationship", "stable", "archive"]
    assert calls[0][1]["memory"]["occurred_at"] == "2023-01-02"
    assert calls[0][1]["memory"]["threads"] == ["comfort", "archive"]


def test_apply_memory_outputs_does_not_preserve_source_date_tags_by_default(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {"status": "ok", "results": [{"memory": {"id": "m1"}}]}, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    output = {
        "memories": [{
            "type": "fact",
            "summary": "Regular card",
            "content": "Regular card content.",
            "date": "2024-05-09",
            "tags": ["should_not_seed"],
        }]
    }

    count, results = service.apply_memory_outputs(_store(), "api_key", output)

    assert count == 1
    assert results == [{"memory": {"id": "m1"}}]
    assert calls[0][0]["memory"]["occurred_at"] == ""
    assert calls[0][0]["memory"]["threads"] == []


def test_apply_memory_outputs_preserve_dates_flag_enables_source_metadata(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {"status": "ok", "results": [{"memory": {"id": "m1"}}]}, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    output = {
        "memories": [{
            "type": "fact",
            "summary": "Explicit preserve",
            "content": "Explicit preserve content.",
            "tags": ["archive"],
        }]
    }

    count, _results = service.apply_memory_outputs(
        _store(),
        "api_key",
        output,
        preserve_dates=True,
        fallback_occurred_at="2022-12-31",
    )

    assert count == 1
    assert calls[0][0]["memory"]["occurred_at"] == "2022-12-31"
    assert calls[0][0]["memory"]["threads"] == ["archive"]


def test_apply_memory_outputs_preserves_per_item_memory_summary_in_merged_output(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {
            "status": "ok",
            "results": [{"memory": {"id": f"m{idx}"}} for idx, _action in enumerate(actions)],
        }, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    output = {
        "source_family": "merged",
        "relationship_started_at": "2023-01-02",
        "memories": [
            {
                "_source_family": "history",
                "type": "fact",
                "summary": "History card",
                "content": "History card content.",
                "date": "2024-05-09",
                "tags": ["history_tag"],
            },
            {
                "_source_family": "memory_summary",
                "type": "fact",
                "summary": "Archive card",
                "content": "Archive card content.",
                "tags": ["archive_tag"],
            },
        ],
    }

    count, _results = service.apply_memory_outputs(_store(), "api_key", output)

    assert count == 2
    assert calls[0][0]["memory"]["occurred_at"] == ""
    assert calls[0][0]["memory"]["threads"] == []
    assert calls[0][1]["memory"]["occurred_at"] == "2023-01-02"
    assert calls[0][1]["memory"]["threads"] == ["archive_tag"]


def test_apply_memory_outputs_coerces_unknown_memory_type_to_fact(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {"status": "ok", "results": [{"memory": {"id": "m1"}}]}, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    output = {
        "memories": [{
            "type": "habit",
            "summary": "User asks for direct feedback",
            "content": "User asks for direct feedback during planning.",
        }]
    }

    count, results = service.apply_memory_outputs(_store(), "api_key", output)
    reducer_doc = service._safe_reducer_doc("job_1", output)

    assert count == 1
    assert results == [{"memory": {"id": "m1"}}]
    assert calls[0][0]["memory"]["type"] == "fact"
    assert reducer_doc["memory_type_counts"] == {"fact": 1}


def test_apply_memory_outputs_skips_incomplete_memory_items(monkeypatch):
    calls = []

    def fake_execute(_store, _api_key, actions):
        calls.append(actions)
        return {"status": "ok", "results": [{"memory": {"id": "m1"}}]}, 200

    monkeypatch.setattr(service.memory_actions, "_execute_memory_actions", fake_execute)
    output = {
        "memories": [
            {"type": "fact", "content": "Missing summary should be skipped."},
            {
                "type": "fact",
                "summary": "User likes direct feedback",
                "content": "User likes direct feedback during planning.",
            },
        ]
    }

    count, results = service.apply_memory_outputs(_store(), "api_key", output)

    assert count == 1
    assert results == [{"memory": {"id": "m1"}}]
    assert len(calls) == 1
    assert calls[0][0]["memory"]["summary"] == "User likes direct feedback"


def test_public_stage_maps_plaintext_reducer_to_friendly_phases():
    # plaintext_reducer / _done are set before the v2 gate (routes.py), so they can leak
    # to the client at job start; map them so iOS never shows the raw stage name.
    assert service.public_stage("plaintext_reducer") == "chat_history_importing"
    assert service.public_stage("plaintext_reducer_done") == "background_importing"
    assert service.public_stage("genesis_v2_foreground") == "chat_history_importing"  # unchanged
    assert service.public_stage("genesis_v2_foreground_ready") == "background_importing"
    assert service.public_stage("unknown_stage") == "unknown_stage"  # passthrough


def test_genesis_checkpoint_persists_only_encrypted_content(monkeypatch):
    stored = {}
    raw_seen = {}

    def build_envelope(_store, raw, *, item_id):
        raw_seen["raw"] = raw
        return {"body_ct": "ciphertext", "id": item_id}, ""

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", build_envelope)
    monkeypatch.setattr(
        service.db,
        "set_blob",
        lambda user_id, kind, doc: stored.update(user_id=user_id, kind=kind, doc=doc),
    )
    monkeypatch.setattr(service.db, "get_blob_strict", lambda *_args: stored["doc"])

    checkpoint_doc = {
        "tasks": {"fact-map::0": {"status": "done"}},
        "map_outputs": {"fact-map::0": {"fact_candidates": [{"summary": "secret"}]}},
    }
    service.write_genesis_checkpoint(_store(), "job_1", checkpoint_doc)

    assert json.loads(raw_seen["raw"]) == checkpoint_doc
    assert stored["kind"] == "genesis_checkpoint:job_1"
    assert "secret" not in json.dumps(stored["doc"])
    assert stored["doc"]["encrypted"] is True


def test_genesis_checkpoint_load_verifies_and_decrypts(monkeypatch):
    checkpoint_doc = {"v": 1, "tasks": {}, "map_outputs": {}}
    raw = json.dumps(
        checkpoint_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    blob = {
        "content_envelope": {"body_ct": "ciphertext"},
        "sha256": service._sha256_hex(raw),
    }
    monkeypatch.setattr(service.db, "get_blob_strict", lambda *_args: blob)
    monkeypatch.setattr(
        service.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda envelope, api_key, **kwargs: raw,
    )

    assert service.load_genesis_checkpoint(_store(), "api-key", "job_1") == checkpoint_doc


def test_genesis_staged_payload_is_encrypted_and_consumed_as_tombstone(monkeypatch):
    stored = {}
    deleted = []

    def build_envelope(_store, raw, *, item_id):
        stored["raw"] = raw
        return {"body_ct": "ciphertext", "id": item_id}, ""

    monkeypatch.setattr(service.core_envelope, "_build_shared_envelope_for_store", build_envelope)
    monkeypatch.setattr(
        service.db,
        "list_blobs",
        lambda *_args: [{"staged_id": "staged_previous", "consumed": True}],
    )
    monkeypatch.setattr(
        service.db,
        "delete_blob",
        lambda user_id, kind: deleted.append((user_id, kind)) or True,
    )
    monkeypatch.setattr(
        service.db,
        "set_blob_strict_mirrored",
        lambda user_id, kind, doc: stored.update(user_id=user_id, kind=kind, doc=doc),
    )
    monkeypatch.setattr(service.db, "get_blob_strict", lambda *_args: stored["doc"])
    monkeypatch.setattr(
        service.core_enclave,
        "_decrypt_envelope_via_enclave",
        lambda *_args, **_kwargs: stored["raw"],
    )

    staged_id = service.create_genesis_staged_payload(
        _store(), {"content": "secret history"}, ttl_sec=600)
    assert deleted == [("usr_genesis", "genesis_staged:staged_previous")]
    assert stored["kind"] == f"genesis_staged:{staged_id}"
    assert "secret history" not in json.dumps(stored["doc"])
    assert service.load_genesis_staged_payload(
        _store(), "api-key", staged_id) == {"content": "secret history"}

    service.mark_genesis_staged_consumed(_store(), staged_id, "job_1")
    assert stored["doc"]["consumed"] is True
    assert "content_envelope" not in stored["doc"]


def test_expired_genesis_stage_is_deleted_before_410(monkeypatch):
    deleted = []
    monkeypatch.setattr(
        service.db,
        "get_blob_strict",
        lambda *_args: {
            "staged_id": "staged_expired",
            "expires_at": 1,
            "consumed": False,
            "content_envelope": {"body_ct": "ciphertext"},
        },
    )
    monkeypatch.setattr(
        service.db,
        "delete_blob",
        lambda user_id, kind: deleted.append((user_id, kind)) or True,
    )

    with pytest.raises(TimeoutError, match="staged_import_expired"):
        service.load_genesis_staged_payload(_store(), "api-key", "staged_expired")

    assert deleted == [("usr_genesis", "genesis_staged:staged_expired")]
