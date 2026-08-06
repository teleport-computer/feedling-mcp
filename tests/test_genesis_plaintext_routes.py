from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import genesis_core, plaintext, service  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

_FAIL_STALE_PLAINTEXT_JOB = plaintext._fail_stale_plaintext_job


def _store(user_id: str = "usr_plaintext"):
    return types.SimpleNamespace(user_id=user_id)


@pytest.fixture(autouse=True)
def _memory_checkpoint(monkeypatch):
    checkpoints = {}

    def load(_store, _api_key, job_id, **_kwargs):
        value = checkpoints.get(job_id)
        return json.loads(json.dumps(value)) if value is not None else None

    def write(_store, job_id, doc):
        checkpoints[job_id] = json.loads(json.dumps(doc))

    monkeypatch.setattr(plaintext.service, "load_genesis_checkpoint", load)
    monkeypatch.setattr(plaintext.service, "write_genesis_checkpoint", write)
    monkeypatch.setattr(plaintext.service, "delete_genesis_checkpoint", lambda *_args: None)
    monkeypatch.setattr(plaintext.db, "genesis_get_job", lambda *_args: None)
    monkeypatch.setattr(plaintext, "_fail_stale_plaintext_job", lambda *_args: None)


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def get_json(self):
        return self._body


class _CoreClient:
    # The Flask genesis blueprint was deleted in the ASGI cutover; this plaintext the
    # one endpoint these tests exercise to genesis_core.plaintext_import, injecting
    # the (monkeypatchable) plaintext-pipeline helpers exactly like genesis.routes_asgi.
    def __init__(self, store):
        self._store = store

    def post(self, path, json=None):
        p = urlparse(path).path
        if p == "/v1/genesis/imports/plaintext":
            body, status = genesis_core.plaintext_import(
                self._store, json or {}, api_key="user_api_key",
                prepare=plaintext._prepare_plaintext_import,
                find_reusable=plaintext._find_reusable_plaintext_job,
                plaintext_mode=plaintext._plaintext_mode,
                job_metadata=plaintext._plaintext_job_metadata,
                start_job=plaintext._start_plaintext_genesis_job,
            )
            return _Resp(body, status)
        if p == "/v1/genesis/imports/plaintext/estimate":
            body, status = genesis_core.plaintext_estimate(
                self._store, json or {}, api_key="user_api_key")
            return _Resp(body, status)
        if p == "/v1/genesis/imports/plaintext/commit":
            body, status = genesis_core.plaintext_commit(
                self._store, json or {}, api_key="user_api_key",
                prepare=plaintext._prepare_plaintext_import,
                find_reusable=plaintext._find_reusable_plaintext_job,
                plaintext_mode=plaintext._plaintext_mode,
                job_metadata=plaintext._plaintext_job_metadata,
                start_job=plaintext._start_plaintext_genesis_job,
            )
            return _Resp(body, status)
        raise AssertionError(f"unrouted path: {p}")


def _client(monkeypatch):
    return _CoreClient(_store())


def test_plaintext_user_name_prefers_encrypted_identity_without_provider(monkeypatch):
    monkeypatch.setattr(
        plaintext,
        "_plaintext_existing_identity_for_update",
        lambda *_args: {"user_preferred_name": "Seven"},
    )
    monkeypatch.setattr(
        plaintext.history_import,
        "_extract_import_user_name_with_provider",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    assert plaintext._resolve_plaintext_user_name(
        _store(),
        "api-key",
        types.SimpleNamespace(),
        [{"source_family": "user_profile", "chunk_texts": ["请叫我小雨"]}],
    ) == "Seven"


def test_plaintext_worker_owner_metadata_is_not_public():
    body = genesis_core._job_response({
        "job_id": "job_owner",
        "metadata": {
            "ingest": "plaintext",
            "plaintext_worker_host": "internal-host",
            "plaintext_worker_pid": 123,
            "plaintext_worker_instance": "internal-instance",
        },
    })

    assert body["job"]["metadata"] == {"ingest": "plaintext"}


def test_plaintext_user_name_extracts_user_profile_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plaintext,
        "_plaintext_existing_identity_for_update",
        lambda *_args: {"agent_name": "Mira"},
    )

    def fake_extract(_runtime, messages):
        calls.append(messages)
        return "小雨"

    monkeypatch.setattr(
        plaintext.history_import,
        "_extract_import_user_name_with_provider",
        fake_extract,
    )

    assert plaintext._resolve_plaintext_user_name(
        _store(),
        "api-key",
        types.SimpleNamespace(),
        [
            {"source_family": "user_profile", "chunk_texts": ["名字：小雨", "请叫我小雨"]},
            {"source_family": "memory_summary", "chunk_texts": ["用户增长是工作主题"]},
        ],
    ) == "小雨"
    assert len(calls) == 1
    assert [message["content"] for message in calls[0]] == ["名字：小雨", "请叫我小雨"]


def test_plaintext_user_name_writeback_preserves_existing_identity(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        plaintext,
        "_plaintext_existing_identity_for_update",
        lambda *_args: {
            "agent_name": "Mira",
            "dimensions": [{"name": "Warm", "value": 80, "description": "Steady."}],
        },
    )
    monkeypatch.setattr(
        plaintext.service,
        "replace_identity_preserving_anchor",
        lambda _store, output, *_a, **_k: captured.update(output) or "updated",
    )

    status = plaintext._write_back_plaintext_user_name(
        _store(), "api-key", "小雨"
    )

    assert status == "updated"
    assert captured["identity"]["agent_name"] == "Mira"
    assert captured["identity"]["user_preferred_name"] == "小雨"


def _stub_update_identity_persona(monkeypatch):
    monkeypatch.setattr(plaintext, "_plaintext_existing_voice_workset_for_update", lambda *_args: {}, raising=False)
    monkeypatch.setattr(
        plaintext.worker,
        "build_persona_output_from_material",
        lambda **_kwargs: {
            "persona": {
                "content": "## 你是谁\n\n测试 persona",
                "prompt_version": "7.B",
                "source_kind": "identity_update",
                "source_family": "ai_persona",
            },
        },
        raising=False,
    )
    monkeypatch.setattr(plaintext.service, "write_persona_artifact", lambda *_args, **_kwargs: ("user_blob:genesis_persona", "sha-persona"))


def test_plaintext_import_missing_material_returns_stable_slug(monkeypatch):
    client = _client(monkeypatch)

    resp = client.post("/v1/genesis/imports/plaintext", json={"format": "auto", "content": ""})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "material_empty"
    assert "fresh_start=true required" in body["detail"]


def test_plaintext_import_normalized_empty_returns_stable_slug(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        plaintext.history_import,
        "_parse_import_history_content",
        lambda *_args: [{"role": "user", "content": "raw", "source": "history_import"}],
    )
    monkeypatch.setattr(plaintext.history_import, "_persona_support_messages", lambda _payload: [])
    monkeypatch.setattr(
        plaintext.history_import,
        "_history_import_profile",
        lambda *_args, **_kwargs: {"tier": "small", "total_windows": 1, "message_count": 1, "support_count": 0},
    )
    monkeypatch.setattr(
        plaintext,
        "_plaintext_source_groups",
        lambda *_args, **_kwargs: [{"source_family": "history", "chunk_texts": ["   "]}],
    )

    resp = client.post("/v1/genesis/imports/plaintext", json={"format": "auto", "content": "raw"})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body == {"error": "material_empty", "detail": "plaintext_import_empty"}


def test_plaintext_import_returns_genesis_job_and_does_not_persist_raw(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "format": "plaintext",
        "content": "User: hello\nAssistant: hi",
        "ai_persona_content": "secret persona text",
        "client_job_id": "ios_job_1",
    }
    captured: dict = {}

    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [])

    def fake_create(
        _store,
        create_payload,
        *,
        initial_status="created",
        trusted_metadata=None,
    ):
        captured["create_payload"] = create_payload
        captured["initial_status"] = initial_status
        captured["trusted_metadata"] = trusted_metadata
        captured["persisted_metadata"] = {
            **create_payload["metadata"],
            **(trusted_metadata or {}),
            "privacy_copy": service.PRIVACY_COPY,
        }
        job = {
            "job_id": "genesis_job_1",
            "status": "created",
            "source_kind": create_payload["source_kind"],
            "metadata": captured["persisted_metadata"],
            "privacy_mode": service.PRIVACY_MODE,
        }
        return job, 201

    monkeypatch.setattr(plaintext.service, "create_import_job", fake_create)
    status_updates = []

    def set_status(_user_id, _job_id, **kwargs):
        status_updates.append(kwargs)
        return {
            "job_id": "genesis_job_1",
            "status": "processing",
            "metadata": captured["persisted_metadata"],
            "source_kind": captured["create_payload"]["source_kind"],
            "privacy_mode": service.PRIVACY_MODE,
            "output": kwargs["output"],
        }

    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        set_status,
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)

    def fake_start(_store, _api_key, job, *, mode="onboarding", chunk_texts, source_kind, source_groups=None, relationship_anchor=None, analysis_messages=None):
        assert status_updates
        captured["started"] = {
            "job_id": job["job_id"],
            "mode": mode,
            "chunk_texts": chunk_texts,
            "source_kind": source_kind,
            "source_groups": source_groups,
            "relationship_anchor": relationship_anchor,
        }
        return True

    monkeypatch.setattr(plaintext, "_start_plaintext_genesis_job", fake_start)

    resp = client.post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 202
    body = resp.get_json()
    assert body["job"]["job_id"] == "genesis_job_1"
    assert body["status"] == "processing"
    assert body["privacy_copy"] == service.PRIVACY_COPY
    assert body["job"]["output"] == {
        "stage": "plaintext_queued",
        "identity_ready": False,
        "materials": [
            {
                "kind": "ai_persona",
                "status": "queued",
                "windows_done": 0,
                "windows_total": 1,
                "cards": 0,
            },
            {
                "kind": "chat_history",
                "status": "queued",
                "windows_done": 0,
                "windows_total": 1,
                "cards": 0,
            },
        ],
    }
    assert captured["started"]["job_id"] == "genesis_job_1"
    assert captured["started"]["mode"] == "onboarding"
    assert len(captured["started"]["chunk_texts"]) <= 8
    assert [group["source_family"] for group in captured["started"]["source_groups"]] == ["ai_persona", "history"]
    assert captured["create_payload"]["total_chunks"] == len(captured["started"]["chunk_texts"])
    metadata_blob = json.dumps(captured["create_payload"]["metadata"], ensure_ascii=False)
    assert "User: hello" not in metadata_blob
    assert "secret persona text" not in metadata_blob
    assert captured["create_payload"]["metadata"]["ingest"] == "plaintext"
    assert captured["create_payload"]["metadata"]["client_job_id"] == "ios_job_1"
    assert captured["create_payload"]["metadata"]["timeline_span_days"] == 0
    assert captured["initial_status"] == "processing"
    assert captured["trusted_metadata"] == plaintext._plaintext_worker_metadata()
    assert captured["persisted_metadata"]["plaintext_worker_instance"] == (
        plaintext._PLAINTEXT_WORKER_INSTANCE
    )


def test_plaintext_import_rejects_second_processing_job_with_active_id(monkeypatch):
    client = _client(monkeypatch)
    active = {
        "job_id": "genesis_active",
        "status": "processing",
        "metadata": {"ingest": "plaintext", "input_hash": "different"},
    }
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [active])
    monkeypatch.setattr(
        plaintext.service,
        "create_import_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )

    resp = client.post(
        "/v1/genesis/imports/plaintext",
        json={"format": "plaintext", "content": "User: second upload"},
    )

    assert resp.status_code == 409
    assert resp.get_json() == {
        "error": "import_job_active",
        "active_job_id": "genesis_active",
    }


def test_plaintext_import_recovers_stale_processing_job_and_resumes(monkeypatch):
    payload = {"format": "plaintext", "content": "User: resume after deploy"}
    input_hash = plaintext.history_import._history_import_payload_hash(payload)
    processing = {
        "job_id": "genesis_stale",
        "status": "processing",
        "metadata": {
            "ingest": "plaintext",
            "input_hash": input_hash,
            "mode": "onboarding",
        },
    }
    failed = {
        **processing,
        "status": "failed",
        "error": "plaintext_stale_timeout:120s",
    }
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [processing])
    monkeypatch.setattr(
        plaintext,
        "_fail_stale_plaintext_job",
        lambda _store, job: failed if job["job_id"] == "genesis_stale" else None,
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_patch_job_metadata",
        lambda *_args, **_kwargs: {
            **failed,
            "metadata": {
                **failed["metadata"],
                **plaintext._plaintext_worker_metadata(),
            },
        },
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_args, **_kwargs: {**failed, "status": "processing"},
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    started = {}
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda _store, _api_key, job, **_kwargs: started.update(job=job) or True,
    )

    resp = _client(monkeypatch).post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 202
    assert resp.get_json()["job"]["job_id"] == "genesis_stale"
    assert started["job"]["status"] == "processing"


def test_plaintext_import_immediately_resumes_dead_same_host_owner(monkeypatch):
    payload = {"format": "plaintext", "content": "User: resume after pkill"}
    processing = {
        "job_id": "genesis_dead_owner",
        "status": "processing",
        "metadata": {
            "ingest": "plaintext",
            "input_hash": plaintext.history_import._history_import_payload_hash(payload),
            "mode": "onboarding",
            "plaintext_worker_host": plaintext._PLAINTEXT_WORKER_HOST,
            "plaintext_worker_pid": 424244,
            "plaintext_worker_instance": "dead-instance",
        },
    }
    failed = {**processing, "status": "failed", "error": "plaintext_worker_restarted"}
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [processing])
    monkeypatch.setattr(
        plaintext.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    fail_calls = []

    def fail_dead_owner(*_args, **kwargs):
        fail_calls.append(kwargs)
        return failed

    monkeypatch.setattr(plaintext.db, "genesis_fail_stale_plaintext_job", fail_dead_owner)
    monkeypatch.setattr(plaintext, "_fail_stale_plaintext_job", _FAIL_STALE_PLAINTEXT_JOB)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_patch_job_metadata",
        lambda *_args, **_kwargs: {
            **failed,
            "metadata": {
                **failed["metadata"],
                **plaintext._plaintext_worker_metadata(),
            },
        },
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_args, **_kwargs: {**failed, "status": "processing"},
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    started = {}
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda _store, _api_key, job, **_kwargs: started.update(job=job) or True,
    )

    resp = _client(monkeypatch).post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 202
    assert resp.get_json()["job"]["job_id"] == "genesis_dead_owner"
    assert started["job"]["status"] == "processing"
    assert fail_calls and all(call["force"] is True for call in fail_calls)


def test_plaintext_import_reuses_failed_job_for_checkpoint_resume(monkeypatch):
    client = _client(monkeypatch)
    payload = {"format": "plaintext", "content": "User: retry me"}
    failed = {
        "job_id": "genesis_failed",
        "status": "failed",
        "metadata": {
            "ingest": "plaintext",
            "input_hash": plaintext.history_import._history_import_payload_hash(payload),
            "mode": "onboarding",
            "distill_model": "old-fast-model",
        },
    }
    list_calls = {"n": 0}

    def list_jobs(*_args, **_kwargs):
        list_calls["n"] += 1
        return [failed]

    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", list_jobs)
    patched = {}
    monkeypatch.setattr(
        plaintext.db,
        "genesis_patch_job_metadata",
        lambda _user_id, _job_id, patch: patched.update(patch) or {
            **failed, "metadata": {**failed["metadata"], **patch}
        },
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_args, **_kwargs: {**failed, "status": "processing"},
    )
    started = {}
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda _store, _api_key, job, **_kwargs: started.update(job=job) or True,
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)

    resp = client.post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 202
    assert resp.get_json()["job"]["job_id"] == "genesis_failed"
    assert started["job"]["status"] == "processing"
    assert patched["distill_model"] is None
    assert patched["plaintext_worker_instance"] == plaintext._PLAINTEXT_WORKER_INSTANCE


def test_plaintext_import_reuses_done_job_without_restart(monkeypatch):
    client = _client(monkeypatch)
    payload = {"format": "plaintext", "content": "User: hello"}
    input_hash = plaintext.history_import._history_import_payload_hash(payload)
    existing = {
        "job_id": "genesis_done",
        "status": "done",
        "memory_action_count": 1,
        "metadata": {"ingest": "plaintext", "input_hash": input_hash},
    }
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [existing])
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not restart done job")),
    )

    resp = client.post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["job"]["job_id"] == "genesis_done"
    assert body["status"] == "done"


def test_update_identity_reuses_done_job_without_existing_identity(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "mode": "update_identity",
        "ai_persona_content": "Name: 乔伊",
        "client_job_id": "identity-idempotent",
    }
    existing = {
        "job_id": "identity_done",
        "status": "done",
        "identity_status": "initialized",
        "metadata": {
            "ingest": "plaintext",
            "client_job_id": "identity-idempotent",
            "mode": "update_identity",
        },
    }
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: None)
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [existing])
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not restart done job")),
    )

    resp = client.post("/v1/genesis/imports/plaintext", json=payload)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "done"
    assert body["job"]["job_id"] == "identity_done"
    assert body["job"]["identity_status"] == "initialized"


def test_prepare_plaintext_import_caps_windows(monkeypatch):
    monkeypatch.setattr(
        plaintext.history_import,
        "_parse_import_history_content",
        lambda *_args: [{"role": "user", "content": "x", "source": "history_import"}],
    )
    monkeypatch.setattr(plaintext.history_import, "_persona_support_messages", lambda _payload: [])
    monkeypatch.setattr(
        plaintext.history_import,
        "_history_import_profile",
        lambda *_args, **_kwargs: {"tier": "small", "total_windows": 2, "message_count": 1, "support_count": 0},
    )
    monkeypatch.setattr(
        plaintext.history_import,
        "_build_transcript_windows",
        lambda *_args, **_kwargs: [{"text": f"window {idx}"} for idx in range(5)],
    )

    prepared = plaintext._prepare_plaintext_import({"content": "x"})

    assert len(prepared["chunk_texts"]) == 2
    assert len(prepared["source_groups"]) == 1
    assert prepared["source_groups"][0]["source_family"] == "history"
    assert prepared["source_kind"] == plaintext.history_import._HISTORY_SOURCE


def test_prepare_plaintext_import_computes_timeline_span_days(monkeypatch):
    base_ts = 1_700_000_000
    messages = [
        {"role": "user", "content": "start", "source": "history_import", "ts": base_ts},
        {
            "role": "assistant",
            "content": "later",
            "source": "history_import",
            "ts": base_ts + 3 * 24 * 60 * 60 + 123,
        },
        {"role": "user", "content": "ignored", "source": "history_import", "ts": "not-a-timestamp"},
    ]
    monkeypatch.setattr(plaintext.history_import, "_parse_import_history_content", lambda *_args: messages)
    monkeypatch.setattr(plaintext.history_import, "_persona_support_messages", lambda _payload: [])
    monkeypatch.setattr(
        plaintext.history_import,
        "_history_import_profile",
        lambda *_args, **_kwargs: {"tier": "small", "total_windows": 1, "message_count": 3, "support_count": 0},
    )
    monkeypatch.setattr(
        plaintext.history_import,
        "_build_transcript_windows",
        lambda *_args, **_kwargs: [{"text": "window"}],
    )

    prepared = plaintext._prepare_plaintext_import({"content": "x"})
    metadata = plaintext._plaintext_job_metadata({}, prepared, client_job_id="", input_hash="input_hash", mode="onboarding")

    assert prepared["timeline_span_days"] == 3
    assert metadata["timeline_span_days"] == 3


def test_plaintext_job_reuse_requires_matching_mode(monkeypatch):
    prepared = {
        "profile": {"tier": "small", "message_count": 1, "support_count": 0},
        "source_stats": {},
        "chunk_texts": ["same material"],
        "timeline_span_days": 0,
        "warnings": [],
        "content_bytes": 13,
    }
    metadata = plaintext._plaintext_job_metadata(
        {},
        prepared,
        client_job_id="same-client-id",
        input_hash="same-input-hash",
        mode="add_memory",
    )
    assert metadata["mode"] == "add_memory"

    add_memory_job = {
        "job_id": "job_add",
        "status": "done",
        "metadata": {
            "ingest": "plaintext",
            "client_job_id": "same-client-id",
            "input_hash": "same-input-hash",
            "mode": "add_memory",
        },
    }
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [add_memory_job])

    assert plaintext._find_reusable_plaintext_job(
        _store(),
        client_job_id="same-client-id",
        input_hash="same-input-hash",
        mode="add_memory",
    ) == add_memory_job
    assert plaintext._find_reusable_plaintext_job(
        _store(),
        client_job_id="same-client-id",
        input_hash="same-input-hash",
        mode="update_identity",
    ) is None


def test_prepare_plaintext_import_builds_ordered_per_source_groups(monkeypatch):
    history_messages = [
        {"role": "user", "content": "hello", "source": plaintext.history_import._HISTORY_SOURCE},
    ]
    support_messages = [
        {
            "role": "user",
            "content": "AI name is Mira",
            "source": plaintext.history_import._AI_PERSONA_SOURCE,
            "source_family": plaintext.history_import._AI_PERSONA_SOURCE,
        },
        {
            "role": "user",
            "content": "User likes direct feedback",
            "source": plaintext.history_import._USER_PROFILE_SOURCE,
            "source_family": plaintext.history_import._USER_PROFILE_SOURCE,
        },
        {
            "role": "user",
            "content": "Long memory says Mira stayed",
            "source": plaintext.history_import._MEMORY_SUMMARY_SOURCE,
            "source_family": plaintext.history_import._MEMORY_SUMMARY_SOURCE,
        },
    ]
    monkeypatch.setattr(plaintext.history_import, "_parse_import_history_content", lambda *_args: history_messages)
    monkeypatch.setattr(plaintext.history_import, "_persona_support_messages", lambda _payload: support_messages)
    monkeypatch.setattr(
        plaintext.history_import,
        "_history_import_profile",
        lambda *_args, **_kwargs: {"tier": "small", "total_windows": 2, "message_count": 1, "support_count": 3},
    )

    def fake_windows(messages, **_kwargs):
        family = plaintext.history_import._import_source_family(str(messages[0].get("source") or ""))
        return [{"text": f"{family}:{len(messages)}"}]

    monkeypatch.setattr(plaintext.history_import, "_build_transcript_windows", fake_windows)

    prepared = plaintext._prepare_plaintext_import({"content": "history"})

    assert [group["source_family"] for group in prepared["source_groups"]] == [
        "ai_persona",
        "user_profile",
        "memory_summary",
        "history",
    ]
    assert prepared["chunk_texts"] == [
        "ai_persona_import:1",
        "user_profile_import:1",
        "memory_summary_import:1",
        "history_import:1",
    ]


def test_plaintext_estimate_stages_without_llm_and_returns_locked_contract(monkeypatch):
    monkeypatch.setattr(
        plaintext.service,
        "create_genesis_staged_payload",
        lambda _store, payload, **_kwargs: "staged_1",
    )
    monkeypatch.setattr(
        plaintext,
        "_recommended_distill_model",
        lambda *_args: "anthropic/claude-haiku-4-5",
    )

    resp = _client(monkeypatch).post("/v1/genesis/imports/plaintext/estimate", json={
        "ai_persona_content": "Name: Mira",
        "personal_profile_content": "User likes direct answers",
        "content": "User: hello\nAssistant: hi",
    })

    assert resp.status_code == 201
    body = resp.get_json()
    assert set(body) == {
        "staged_id", "materials", "est_total_tokens", "recommended_model"
    }
    assert body["staged_id"] == "staged_1"
    assert [item["kind"] for item in body["materials"]] == [
        "ai_persona", "user_profile", "chat_history"
    ]
    assert all(set(item) == {"kind", "windows", "est_tokens"} for item in body["materials"])
    assert body["est_total_tokens"] == sum(item["est_tokens"] for item in body["materials"])
    assert body["recommended_model"] == "anthropic/claude-haiku-4-5"


def test_plaintext_commit_applies_job_model_override_without_consuming_stage(monkeypatch):
    # Commit must NOT consume the staged payload: the job it starts can still fail
    # asynchronously, and consuming now would leave a retry with no materials to
    # re-run (staged_import_consumed 409). Consumption happens on DONE instead.
    # The staged_id is threaded into the job metadata so completion can find it.
    staged_payload = {"format": "plaintext", "content": "User: hello"}
    captured = {}
    monkeypatch.setattr(
        plaintext.service, "load_genesis_staged_payload",
        lambda *_args: staged_payload,
    )
    monkeypatch.setattr(
        plaintext.service, "mark_genesis_staged_consumed",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("commit must not consume the stage")),
    )
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [])

    def fake_create(_store, payload, **_kwargs):
        captured["metadata"] = payload["metadata"]
        return ({"job_id": "job_commit", "status": "processing", "metadata": payload["metadata"]}, 201)

    monkeypatch.setattr(plaintext.service, "create_import_job", fake_create)

    def set_status(*_args, **kwargs):
        captured["queued_output"] = kwargs["output"]
        return {
            "job_id": "job_commit",
            "status": "processing",
            "metadata": captured["metadata"],
            "output": kwargs["output"],
        }

    monkeypatch.setattr(
        plaintext.db, "genesis_set_job_status",
        set_status,
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext, "_start_plaintext_genesis_job", lambda *_args, **_kwargs: True)

    resp = _client(monkeypatch).post("/v1/genesis/imports/plaintext/commit", json={
        "staged_id": "staged_1",
        "distill_model": "anthropic/claude-haiku-4-5",
    })

    assert resp.status_code == 202
    assert captured["metadata"]["distill_model"] == "anthropic/claude-haiku-4-5"
    # staged_id threaded into metadata so consume-on-DONE can release it later.
    assert captured["metadata"]["staged_id"] == "staged_1"
    assert captured["queued_output"]["materials"] == [{
        "kind": "chat_history",
        "status": "queued",
        "windows_done": 0,
        "windows_total": 1,
        "cards": 0,
    }]
    response_materials = resp.get_json()["job"]["output"]["materials"]
    assert response_materials == captured["queued_output"]["materials"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LookupError("staged_import_not_found"), 404),
        (TimeoutError("staged_import_expired"), 410),
        (ValueError("staged_import_consumed"), 409),
    ],
)
def test_plaintext_commit_staged_id_errors_are_distinct(monkeypatch, error, status):
    monkeypatch.setattr(
        plaintext.service,
        "load_genesis_staged_payload",
        lambda *_args: (_ for _ in ()).throw(error),
    )

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext/commit", json={"staged_id": "staged_1"})

    assert resp.status_code == status
    assert resp.get_json()["error"] == str(error)


def test_plaintext_commit_active_job_returns_409_without_consuming_stage(monkeypatch):
    monkeypatch.setattr(
        plaintext.service,
        "load_genesis_staged_payload",
        lambda *_args: {"format": "plaintext", "content": "User: staged"},
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_list_jobs",
        lambda *_args, **_kwargs: [{
            "job_id": "job_active",
            "status": "processing",
            "metadata": {"ingest": "plaintext", "input_hash": "other"},
        }],
    )
    monkeypatch.setattr(
        plaintext.service,
        "mark_genesis_staged_consumed",
        lambda *_args: (_ for _ in ()).throw(AssertionError("409 must keep stage")),
    )

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext/commit", json={"staged_id": "staged_1"})

    assert resp.status_code == 409
    assert resp.get_json() == {
        "error": "import_job_active",
        "active_job_id": "job_active",
    }


def test_consume_staged_for_completed_job_releases_recorded_stage(monkeypatch):
    # On DONE, the staged payload recorded in job metadata is released.
    captured = {}
    monkeypatch.setattr(
        service.db, "genesis_get_job",
        lambda _uid, _jid: {
            "job_id": "job_done", "status": "done",
            "metadata": {"ingest": "plaintext", "staged_id": "staged_42"},
        },
    )
    monkeypatch.setattr(
        service, "mark_genesis_staged_consumed",
        lambda _store, staged_id, job_id: captured.update(consumed=(staged_id, job_id)),
    )
    service.consume_staged_for_completed_job(_store(), "job_done")
    assert captured["consumed"] == ("staged_42", "job_done")


def test_consume_staged_for_completed_job_noop_without_staged_id(monkeypatch):
    # Older jobs (or an already-released stage) carry no staged_id → no-op, no raise.
    monkeypatch.setattr(
        service.db, "genesis_get_job",
        lambda _uid, _jid: {"job_id": "job_x", "metadata": {"ingest": "plaintext"}},
    )
    monkeypatch.setattr(
        service, "mark_genesis_staged_consumed",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not consume")),
    )
    service.consume_staged_for_completed_job(_store(), "job_x")


def test_plaintext_commit_retry_rebinds_staged_id_on_reused_failed_job(monkeypatch):
    # A re-staged retry (stage_B) that reuses the original failed job must re-bind the
    # job's staged_id to stage_B, so consume-on-DONE releases the live stage — not the
    # already-reaped stage_A recorded on the first attempt.
    materials = {"format": "plaintext", "content": "User: retry me"}
    monkeypatch.setattr(plaintext.service, "load_genesis_staged_payload", lambda *_a: materials)
    failed = {
        "job_id": "genesis_failed", "status": "failed",
        "metadata": {
            "ingest": "plaintext",
            "input_hash": plaintext.history_import._history_import_payload_hash(materials),
            "mode": "onboarding", "staged_id": "stage_A",
        },
    }
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_a, **_k: [failed])
    patched = {}
    monkeypatch.setattr(
        plaintext.db, "genesis_patch_job_metadata",
        lambda _u, _j, patch: patched.update(patch) or {**failed, "metadata": {**failed["metadata"], **patch}},
    )
    monkeypatch.setattr(
        plaintext.db, "genesis_set_job_status",
        lambda *_a, **_k: {**failed, "status": "processing"},
    )
    monkeypatch.setattr(plaintext, "_start_plaintext_genesis_job", lambda *_a, **_k: True)
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_a, **_k: None)

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext/commit", json={"staged_id": "stage_B"})

    assert resp.status_code == 202
    assert patched["staged_id"] == "stage_B"


def test_plaintext_commit_reused_done_job_consumes_current_stage(monkeypatch):
    # Identical materials already distilled (DONE): no worker runs, so the current
    # request's stage is released immediately rather than stranded.
    materials = {"format": "plaintext", "content": "User: hello"}
    monkeypatch.setattr(plaintext.service, "load_genesis_staged_payload", lambda *_a: materials)
    done = {
        "job_id": "genesis_done", "status": "done",
        "memory_action_count": 1,
        "metadata": {
            "ingest": "plaintext",
            "input_hash": plaintext.history_import._history_import_payload_hash(materials),
        },
    }
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_a, **_k: [done])
    consumed = {}
    monkeypatch.setattr(
        genesis_core.service, "mark_genesis_staged_consumed",
        lambda _s, staged_id, job_id: consumed.update(v=(staged_id, job_id)),
    )

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext/commit", json={"staged_id": "stage_now"})

    assert resp.status_code == 200
    assert consumed["v"] == ("stage_now", "genesis_done")


def test_plaintext_commit_done_without_artifact_evidence_creates_new_job(monkeypatch):
    materials = {"format": "plaintext", "content": "User: retry poisoned import"}
    input_hash = plaintext.history_import._history_import_payload_hash(materials)
    poisoned = {
        "job_id": "genesis_poisoned",
        "status": "done",
        "memory_action_count": 0,
        "identity_status": "",
        "persona_ref": "",
        "metadata": {
            "ingest": "plaintext",
            "input_hash": input_hash,
            "mode": "onboarding",
        },
    }
    monkeypatch.setattr(plaintext.service, "load_genesis_staged_payload", lambda *_a: materials)
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_a, **_k: [poisoned])

    created = {}

    def fake_create(_store, payload, **_kwargs):
        created["job"] = {
            "job_id": "genesis_retry",
            "status": "processing",
            "metadata": payload["metadata"],
        }
        return created["job"], 201

    monkeypatch.setattr(plaintext.service, "create_import_job", fake_create)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_a, **kwargs: {**created["job"], "output": kwargs.get("output")},
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_a, **_k: None)
    started = {}
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda _store, _key, job, **_kwargs: started.update(job=job) or True,
    )
    monkeypatch.setattr(
        genesis_core.service,
        "mark_genesis_staged_consumed",
        lambda *_a: (_ for _ in ()).throw(AssertionError("poisoned done must not consume retry stage")),
    )

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext/commit", json={"staged_id": "stage_retry"})

    assert resp.status_code == 202
    assert resp.get_json()["job"]["job_id"] == "genesis_retry"
    assert started["job"]["job_id"] == "genesis_retry"
    assert created["job"]["job_id"] != poisoned["job_id"]


def test_plaintext_direct_import_ignores_client_staged_id(monkeypatch):
    # The public /plaintext direct-import path must NOT record a client-supplied
    # staged_id into job metadata (that would let a client tombstone another of their
    # own stages). Only plaintext_commit's trusted arg sets it.
    captured = {}
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_a, **_k: [])

    def fake_create(_store, payload, **_kwargs):
        captured["metadata"] = payload["metadata"]
        return ({"job_id": "job_direct", "status": "processing", "metadata": payload["metadata"]}, 201)

    monkeypatch.setattr(plaintext.service, "create_import_job", fake_create)
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status",
                        lambda *_a, **k: {"job_id": "job_direct", "status": "processing",
                                          "metadata": captured["metadata"], "output": k.get("output")})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_a, **_k: None)
    monkeypatch.setattr(plaintext, "_start_plaintext_genesis_job", lambda *_a, **_k: True)

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext",
        json={"format": "plaintext", "content": "User: hi", "staged_id": "victim_stage"})

    assert resp.status_code == 202
    assert captured["metadata"]["staged_id"] == ""


def test_plaintext_commit_rejects_invalid_distill_model(monkeypatch):
    monkeypatch.setattr(
        plaintext.service,
        "load_genesis_staged_payload",
        lambda *_args: {"format": "plaintext", "content": "User: staged"},
    )

    resp = _client(monkeypatch).post("/v1/genesis/imports/plaintext/commit", json={
        "staged_id": "staged_1",
        "distill_model": "x" * 161,
    })

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_distill_model"


def test_recommended_distill_model_catalog_priority(monkeypatch):
    runtime = plaintext.provider_client.ProviderConfig(
        "openai_compatible", "chat-model", "secret", "https://relay.example/v1")
    monkeypatch.setattr(
        plaintext.hosted_config_store,
        "_load_runtime_provider_config",
        lambda *_args: runtime,
    )
    captured = {}

    def catalog(*_args, **kwargs):
        captured.update(kwargs)
        return {"models": [
            {"id": "vendor/flash-thinking"},
            {"id": "vendor/mini-fast"},
            {"id": "vendor/haiku-fast"},
        ]}

    monkeypatch.setattr(plaintext.provider_client, "list_provider_models", catalog)

    assert plaintext._recommended_distill_model(_store(), "key") == "vendor/haiku-fast"
    assert captured["total_budget_sec"] == 3.0


def test_recommended_distill_model_skips_only_openai_compatible_gemini_flash(monkeypatch):
    runtime = plaintext.provider_client.ProviderConfig(
        "openai_compatible", "chat-model", "secret", "https://relay.example/v1")
    monkeypatch.setattr(
        plaintext.hosted_config_store,
        "_load_runtime_provider_config",
        lambda *_args: runtime,
    )
    monkeypatch.setattr(
        plaintext.provider_client,
        "list_provider_models",
        lambda *_args, **_kwargs: {"models": [
            {"id": "gemini-3-flash-preview"},
            {"id": "gemini-3.1-pro-preview"},
        ]},
    )

    assert plaintext._recommended_distill_model(_store(), "key") == "gemini-3.1-pro-preview"
    assert plaintext._openai_compatible_gemini_flash("gemini-3.5-flash") is True
    assert plaintext._openai_compatible_gemini_flash("gemini-3.1-pro-preview") is False
    assert plaintext._openai_compatible_gemini_flash("deepseek-v4-flash") is False


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", "claude-haiku-4-5"),
        ("deepseek", "deepseek-v4-flash"),
        ("gemini", "gemini-flash-lite-latest"),
        ("openai", "gpt-4o-mini"),
        ("openrouter", "anthropic/claude-haiku-4-5"),
        ("bedrock", None),
    ],
)
def test_recommended_distill_model_provider_defaults(monkeypatch, provider, expected):
    runtime = plaintext.provider_client.ProviderConfig(
        provider, "chat-model", "secret", "https://provider.example/v1")
    monkeypatch.setattr(
        plaintext.hosted_config_store,
        "_load_runtime_provider_config",
        lambda *_args: runtime,
    )
    monkeypatch.setattr(
        plaintext.provider_client,
        "list_provider_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dedicated providers must not probe a relay catalog")),
    )

    assert plaintext._recommended_distill_model(_store(), "key") == expected


def test_plaintext_estimate_recommendation_failure_degrades_to_null(monkeypatch):
    monkeypatch.setattr(
        plaintext.service,
        "create_genesis_staged_payload",
        lambda *_args, **_kwargs: "staged_1",
    )
    monkeypatch.setattr(
        plaintext,
        "_recommended_distill_model",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("config unavailable")),
    )

    resp = _client(monkeypatch).post(
        "/v1/genesis/imports/plaintext/estimate",
        json={"content": "User: hello"},
    )

    assert resp.status_code == 201
    assert resp.get_json()["staged_id"] == "staged_1"
    assert resp.get_json()["recommended_model"] is None


def test_plaintext_status_exposes_identity_materials_and_failure_copy(monkeypatch):
    materials = [{
        "kind": "chat_history",
        "status": "processing",
        "windows_done": 2,
        "windows_total": 4,
        "cards": 7,
    }]
    monkeypatch.setattr(
        genesis_core.db,
        "genesis_get_job",
        lambda *_args: {
            "job_id": "job_status",
            "status": "failed",
            "error": "plaintext_import_failed:ReadTimeout",
            "output": {"identity_ready": True, "materials": materials},
        },
    )
    monkeypatch.setattr(genesis_core.db, "get_blob", lambda *_args: None)

    body, status = genesis_core.get_import_status(
        _store(), "job_status", include_missing_raw=False)

    assert status == 200
    assert body["job_id"] == "job_status"
    assert body["status"] == "failed"
    assert body["identity_ready"] is True
    assert body["materials"] == [{**materials[0], "status": "failed"}]
    assert body["error_class"] == "provider_timeout"
    assert body["friendly_copy"]


def test_plaintext_status_quota_failure_keeps_cause_aware_copy(monkeypatch):
    raw_error = "plaintext_import_failed:ProviderError:provider_http_402"
    monkeypatch.setattr(
        genesis_core.db,
        "genesis_get_job",
        lambda *_args: {
            "job_id": "job_quota",
            "status": "failed",
            "error": raw_error,
            "output": {"stage": "plaintext_reducer", "materials": []},
        },
    )
    monkeypatch.setattr(genesis_core.db, "get_blob", lambda *_args: None)

    body, status = genesis_core.get_import_status(
        _store(), "job_quota", include_missing_raw=False)

    assert status == 200
    assert body["error_class"] == "provider_quota"
    assert service.GENESIS_ERROR_HINTS["provider_quota"] in body["friendly_copy"]
    assert service.GENESIS_ERROR_HINTS["internal"] not in body["friendly_copy"]


def test_plaintext_status_reaps_stale_processing_lease(monkeypatch):
    processing = {
        "job_id": "job_stale",
        "status": "processing",
        "metadata": {"ingest": "plaintext"},
        "output": {"stage": "genesis_v2_background", "materials": []},
    }
    failed = {
        **processing,
        "status": "failed",
        "error": "plaintext_stale_timeout:120s",
    }
    monkeypatch.setattr(genesis_core.db, "genesis_get_job", lambda *_args: processing)
    monkeypatch.setattr(
        plaintext,
        "_fail_stale_plaintext_job",
        lambda *_args: failed,
    )
    monkeypatch.setattr(genesis_core.db, "get_blob", lambda *_args: None)

    body, status = genesis_core.get_import_status(
        _store(), "job_stale", include_missing_raw=False)

    assert status == 200
    assert body["status"] == "failed"
    assert body["error_class"] == "worker_restarted"


def test_plaintext_dead_same_host_owner_is_recovered_immediately(monkeypatch):
    processing = {
        "job_id": "job_dead_owner",
        "status": "processing",
        "metadata": {
            "ingest": "plaintext",
            "plaintext_worker_host": plaintext._PLAINTEXT_WORKER_HOST,
            "plaintext_worker_pid": 424242,
            "plaintext_worker_instance": "old-instance",
        },
    }
    captured = {}
    failed = {**processing, "status": "failed", "error": "plaintext_worker_restarted"}
    monkeypatch.setattr(
        plaintext.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_fail_stale_plaintext_job",
        lambda *_args, **kwargs: captured.update(kwargs) or failed,
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        plaintext,
        "_fail_stale_plaintext_job",
        _FAIL_STALE_PLAINTEXT_JOB,
    )

    recovered = plaintext._fail_stale_plaintext_job(_store(), processing)

    assert recovered == failed
    assert captured["force"] is True
    assert captured["expected_worker_instance"] == "old-instance"
    assert captured["error"] == "plaintext_worker_restarted"


def test_plaintext_live_same_host_owner_keeps_stale_lease_guard(monkeypatch):
    processing = {
        "job_id": "job_live_owner",
        "status": "processing",
        "metadata": {
            "ingest": "plaintext",
            "plaintext_worker_host": plaintext._PLAINTEXT_WORKER_HOST,
            "plaintext_worker_pid": 424243,
            "plaintext_worker_instance": "other-live-instance",
        },
    }
    captured = {}
    monkeypatch.setattr(plaintext.os, "kill", lambda *_args: None)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_fail_stale_plaintext_job",
        lambda *_args, **kwargs: captured.update(kwargs) or None,
    )
    monkeypatch.setattr(
        plaintext,
        "_fail_stale_plaintext_job",
        _FAIL_STALE_PLAINTEXT_JOB,
    )

    assert plaintext._fail_stale_plaintext_job(_store(), processing) is None
    assert captured["force"] is False
    assert captured["expected_worker_instance"] == "other-live-instance"


def test_plaintext_job_heartbeat_renews_processing_lease(monkeypatch):
    waits = []
    touched = []

    class StopAfterOneBeat:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) > 1

    monkeypatch.setattr(plaintext, "_plaintext_heartbeat_sec", lambda: 15)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_touch_plaintext_job",
        lambda user_id, job_id, **kwargs: touched.append(
            (user_id, job_id, kwargs["worker_instance"])
        ),
    )

    plaintext._run_plaintext_job_heartbeat(
        _store(), "job_heartbeat", StopAfterOneBeat())

    assert waits == [15, 15]
    assert touched == [(
        "usr_plaintext",
        "job_heartbeat",
        plaintext._PLAINTEXT_WORKER_INSTANCE,
    )]


def test_plaintext_background_runner_distills_and_applies(monkeypatch):
    store = _store()
    calls: dict = {}
    trace_events: list[dict] = []
    monkeypatch.setattr(
        plaintext,
        "debug_trace",
        type("_Trace", (), {"trace_event": staticmethod(lambda *_args, **kwargs: trace_events.append(kwargs))}),
        raising=False,
    )

    def fake_set_status(_user_id, _job_id, **kwargs):
        calls.setdefault("statuses", []).append(kwargs)
        return {
            "job_id": "genesis_job_1",
            "status": kwargs["status"],
            "source_kind": "history_import",
            "privacy_mode": service.PRIVACY_MODE,
            "metadata": {"distill_model": "fast-genesis-model"},
        }

    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", fake_set_status)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_get_job",
        lambda *_args: {
            "job_id": "genesis_job_1",
            "status": "processing",
            "metadata": {"distill_model": "fast-genesis-model"},
        },
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    chat_runtime = plaintext.provider_client.ProviderConfig(
        "anthropic", "chat-model", "provider-key", "https://api.anthropic.com")
    monkeypatch.setattr(
        plaintext.hosted_config_store,
        "_load_runtime_provider_config",
        lambda *_args: chat_runtime,
    )

    def fake_build(**kwargs):
        calls["build"] = kwargs
        kwargs["on_map_completed"](0, {"fact_candidates": []})
        return {"memories": [], "identity": {}, "voice": {}, "persona": {}}

    monkeypatch.setattr(plaintext.worker, "build_reducer_output_from_texts", fake_build)
    monkeypatch.setattr(
        plaintext.service,
        "apply_reducer_output",
        lambda _store, _api_key, _job_id, output: calls.update({"applied": output}),
    )
    monkeypatch.setattr(
        plaintext.service,
        "mark_failed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not fail")),
    )

    plaintext._run_plaintext_genesis_job(
        store,
        "user_api_key",
        "genesis_job_1",
        chunk_texts=["window 1"],
        source_kind="history_import",
    )

    assert calls["build"]["user_id"] == "usr_plaintext"
    assert calls["build"]["chunk_texts"] == ["window 1"]
    assert calls["build"]["source_kind"] == "history_import"
    assert calls["build"]["runtime"].model == "fast-genesis-model"
    assert chat_runtime.model == "chat-model"
    assert calls["applied"]["memories"] == []
    assert calls["statuses"][-1]["processed_chunks"] == 1
    assert calls["statuses"][-1]["status"] == service.DONE_JOB_STATUS
    assert calls["statuses"][-1]["output"]["identity_ready"] is True
    assert calls["statuses"][-1]["output"]["materials"] == [{
        "kind": "chat_history",
        "status": "done",
        "windows_done": 1,
        "windows_total": 1,
        "cards": 0,
    }]
    material_counts = [
        len(status["output"].get("materials") or [])
        for status in calls["statuses"]
        if status.get("status") == "processing"
    ]
    assert material_counts
    assert material_counts == sorted(material_counts)
    assert material_counts[0] == 1
    assert [
        event["type"]
        for event in trace_events
        if event["type"] in {
            "genesis.plaintext.started",
            "genesis.plaintext.runtime.loaded",
            "genesis.plaintext.reducer_pass.started",
            "genesis.plaintext.reducer_pass.done",
            "genesis.plaintext.apply.started",
            "genesis.plaintext.done",
        }
    ] == [
        "genesis.plaintext.started",
        "genesis.plaintext.runtime.loaded",
        "genesis.plaintext.reducer_pass.started",
        "genesis.plaintext.reducer_pass.done",
        "genesis.plaintext.apply.started",
        "genesis.plaintext.done",
    ]


def test_plaintext_material_cards_come_from_durable_map_outputs(monkeypatch):
    statuses = []
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_args, **kwargs: statuses.append(kwargs) or None,
    )
    progress = plaintext._PlaintextCheckpointProgress(
        _store(),
        "api-key",
        "job-cards",
        [{
            "source_kind": "history_import",
            "source_family": "history",
            "chunk_texts": ["window"],
        }],
    )

    progress.record_map(1, "history", 0, {
        "fact_candidates": [{"summary": "one"}, {"summary": "two"}],
    })

    assert statuses[-1]["output"]["materials"] == [{
        "kind": "chat_history",
        "status": "done",
        "windows_done": 1,
        "windows_total": 1,
        "cards": 2,
    }]


def test_plaintext_map_diagnostics_are_bounded_in_job_output(monkeypatch):
    statuses = []
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_args, **kwargs: statuses.append(kwargs) or None,
    )
    progress = plaintext._PlaintextCheckpointProgress(
        _store(),
        "api-key",
        "job-diagnostics",
        [{
            "source_kind": "memory_summary_import",
            "source_family": "memory_summary",
            "chunk_texts": ["window"],
        }],
    )

    progress.record_map_diagnostics(1, "memory_summary", [{
        "chunk_index": 0,
        "task_id": "fact-map-0",
        "discard_reason": "empty_fact_candidates",
        "raw_output_snippet": "x" * 800,
        "raw_output_chars": 800,
        "raw_output_truncated": True,
    }])

    diagnostic = statuses[-1]["output"]["map_diagnostics"][0]
    assert diagnostic["discard_reason"] == "empty_fact_candidates"
    assert len(diagnostic["raw_output_snippet"]) == 500
    assert diagnostic["raw_output_chars"] == 800
    assert diagnostic["raw_output_truncated"] is True


def test_plaintext_background_runner_routes_sources_and_merges_with_firewall(monkeypatch):
    store = _store()
    calls: dict = {"builds": []}
    source_groups = [
        {
            "source_kind": plaintext.history_import._AI_PERSONA_SOURCE,
            "source_family": "ai_persona",
            "chunk_texts": ["persona window"],
        },
        {
            "source_kind": plaintext.history_import._HISTORY_SOURCE,
            "source_family": "history",
            "chunk_texts": ["history window"],
        },
        {
            "source_kind": plaintext.history_import._MEMORY_SUMMARY_SOURCE,
            "source_family": "memory_summary",
            "chunk_texts": ["memory window"],
        },
        {
            "source_kind": plaintext.history_import._USER_PROFILE_SOURCE,
            "source_family": "user_profile",
            "chunk_texts": ["profile window"],
        },
    ]

    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda _user_id, _job_id, **kwargs: {
            "job_id": "genesis_job_1",
            "status": kwargs["status"],
            "source_kind": "history_import",
            "privacy_mode": service.PRIVACY_MODE,
        },
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")

    def fake_build(**kwargs):
        calls["builds"].append(kwargs)
        family = plaintext.worker._source_family(kwargs["source_kind"])
        if family == "ai_persona":
            return {
                "source_kind": kwargs["source_kind"],
                "source_family": "ai_persona",
                "identity": {
                    "agent_name": "Mira",
                    "dimensions": [{"name": "Steady", "value": 81, "description": "Persona says steady."}],
                },
                "persona": {"content": "persona spine", "prompt_version": "7.B", "source_family": "ai_persona"},
            }
        if family == "history":
            assert kwargs["existing_persona"]["content"] == "persona spine"
            return {
                "source_kind": kwargs["source_kind"],
                "source_family": "history",
                "memories": [{"type": "moment", "summary": "History memory", "content": "History memory."}],
                "identity": {
                    "agent_name": "HistoryName",
                    "dimensions": [{"name": "Playful", "value": 66, "description": "History says playful."}],
                },
                "days_with_user": 11,
                "persona": {"content": "merged persona", "prompt_version": "7.B", "source_family": "merged"},
                "voice_workset": {"behavior_notes": ["short replies"], "exemplars": [{"turns": [{"role": "ta", "text": "I'm here."}], "founding": True}]},
            }
        if family == "memory_summary":
            return {
                "source_kind": kwargs["source_kind"],
                "source_family": "memory_summary",
                "memories": [{
                    "type": "fact",
                    "summary": "Memory summary",
                    "content": "Memory summary.",
                    "tags": ["archive"],
                }],
                "identity": {"agent_name": "MemoryName", "dimensions": [{"name": "ShouldDrop", "description": "drop"}]},
                "days_with_user": 22,
            }
        return {
            "source_kind": kwargs["source_kind"],
            "source_family": "user_profile",
            "memories": [{"type": "fact", "summary": "User profile fact", "content": "User profile fact."}],
            "identity": {
                "agent_name": "WrongUserName",
                "dimensions": [{"name": "Wrong", "value": 99, "description": "from user profile"}],
            },
            "persona": {"content": "bad user persona", "prompt_version": "7.B", "source_family": "user_profile"},
            "voice_workset": {"behavior_notes": ["bad"], "exemplars": []},
        }

    monkeypatch.setattr(plaintext.worker, "build_reducer_output_from_texts", fake_build)
    monkeypatch.setattr(
        plaintext.service,
        "apply_reducer_output",
        lambda _store, _api_key, _job_id, output: calls.update({"applied": output}),
    )
    monkeypatch.setattr(
        plaintext.service,
        "mark_failed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not fail")),
    )

    plaintext._run_plaintext_genesis_job(
        store,
        "user_api_key",
        "genesis_job_1",
        source_groups=source_groups,
        relationship_anchor={"days_with_user": 7, "relationship_anchor_evidence": "timeline span"},
    )

    assert [plaintext.worker._source_family(call["source_kind"]) for call in calls["builds"]] == [
        "ai_persona",
        "history",
        "memory_summary",
        "user_profile",
    ]
    assert [call["job_id"] for call in calls["builds"]] == [
        "genesis_job_1",
        "genesis_job_1",
        "genesis_job_1",
        "genesis_job_1",
    ]
    assert [call["key_prefix"] for call in calls["builds"]] == [
        "genesis_job_1:source_pass:1:ai_persona",
        "genesis_job_1:source_pass:2:history",
        "genesis_job_1:source_pass:3:memory_summary",
        "genesis_job_1:source_pass:4:user_profile",
    ]
    applied = calls["applied"]
    assert applied["identity"]["agent_name"] == "Mira"
    assert applied["identity"]["dimensions"][0]["name"] == "Steady"
    assert applied["days_with_user"] == 7
    assert applied["relationship_anchor_evidence"] == "timeline span"
    assert applied["persona"]["content"] == "merged persona"
    assert applied["voice_workset"]["behavior_notes"] == ["short replies"]
    assert [item["summary"] for item in applied["memories"]] == [
        "History memory",
        "Memory summary",
        "User profile fact",
    ]
    assert applied["memories"][0]["_source_family"] == "history"
    assert applied["memories"][1]["_source_family"] == "memory_summary"
    assert applied["memories"][2]["_source_family"] == "user_profile"
    serialized = json.dumps(applied, ensure_ascii=False)
    assert "WrongUserName" not in serialized
    assert "bad user persona" not in serialized


def test_plaintext_retry_uses_checkpoint_and_skips_completed_maps(monkeypatch):
    store = _store()
    calls = {"maps": 0, "reduces": 0, "failures": []}
    monkeypatch.setattr(plaintext.worker, "genesis_v2_enabled", lambda: True)
    monkeypatch.setattr(plaintext.worker, "genesis_combined_map_enabled", lambda: True)
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext, "_resolve_plaintext_user_name", lambda *_args: "TA")
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda _uid, _jid, **kwargs: {"job_id": "resume_job", "status": kwargs["status"]},
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)

    def foreground_map(**kwargs):
        if 0 not in (kwargs.get("resume_map_outputs") or {}):
            calls["maps"] += 1
            kwargs["on_map_completed"](0, {"fact_candidates": [{"summary": "remembered"}]})
        return {
            "source_family": "history",
            "all_fact_candidates": [{"summary": "remembered"}],
            "core_fact_candidates": [{"summary": "remembered"}],
        }

    monkeypatch.setattr(plaintext.worker, "build_foreground_output_from_texts", foreground_map)

    def reduce_candidates(**_kwargs):
        calls["reduces"] += 1
        if calls["reduces"] == 1:
            raise RuntimeError("forced reduce failure")
        return {"memories": [{"summary": "remembered"}], "identity": {}}

    monkeypatch.setattr(plaintext.worker, "build_memory_output_from_fact_candidates", reduce_candidates)
    monkeypatch.setattr(
        plaintext.worker,
        "build_voice_persona_output_from_candidates",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        plaintext,
        "_plaintext_merge_reducer_outputs",
        lambda *_args, **_kwargs: {"memories": [{"summary": "remembered"}]},
    )
    monkeypatch.setattr(
        plaintext.foreground_identity,
        "derive_foreground_identity",
        lambda **_kwargs: ({"agent_name": "", "dimensions": []}, []),
    )
    monkeypatch.setattr(plaintext.lightweight_identity, "derive_from_support", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(plaintext.lightweight_identity, "has_signal", lambda *_args: False)
    monkeypatch.setattr(plaintext, "_append_plaintext_onboarding_greeting", lambda *_args, **_kwargs: "hi")
    monkeypatch.setattr(plaintext.service, "apply_reducer_output", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, job_id, **_kwargs: {"job_id": job_id, "status": "done"},
    )
    monkeypatch.setattr(
        plaintext.service,
        "mark_failed",
        lambda _store, _job_id, error, **_kwargs: calls["failures"].append(error),
    )

    kwargs = {
        "source_groups": [{
            "source_kind": "history_import",
            "source_family": "history",
            "chunk_texts": ["window one"],
        }],
        "analysis_messages": [{"role": "user", "content": "window one"}],
    }
    plaintext._run_plaintext_genesis_job(store, "api_key", "resume_job", **kwargs)
    plaintext._run_plaintext_genesis_job(store, "api_key", "resume_job", **kwargs)

    assert calls["maps"] == 1
    assert calls["reduces"] == 2
    assert len(calls["failures"]) == 1


def test_plaintext_relationship_anchor_uses_earliest_timestamp_when_no_date():
    # documented priority: no typed date -> earliest message timestamp (NOT blank, which
    # previously fell through to prefer_memory and collapsed 相处天数 to 0).
    from datetime import datetime, timezone

    def _ts(s):
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()

    msgs = [
        {"role": "user", "content": "a", "ts": _ts("2026-01-10T07:50")},
        {"role": "agent", "content": "b", "ts": _ts("2026-05-01T10:00")},
    ]
    anchor = plaintext._plaintext_relationship_anchor({}, messages=msgs)  # empty payload = no typed date
    assert anchor["relationship_started_at"] == "2026-01-10"
    assert anchor["days_with_user"] > 0

    # typed date still wins
    anchor2 = plaintext._plaintext_relationship_anchor({"relationship_started_at": "2024-06-01"}, messages=msgs)
    assert anchor2["relationship_started_at"] == "2024-06-01"

    # no date + no timestamps -> blank (falls back to prefer_memory/today downstream)
    anchor3 = plaintext._plaintext_relationship_anchor({}, messages=[{"role": "user", "content": "x"}])
    assert anchor3["relationship_started_at"] == ""


# ---------------------------------------------------------------------------
# B2 (reverses I7): _plaintext_merge_reducer_outputs must thread the 5
# user-layer identity fields through — and, unlike agent_name/dimensions,
# READ THEM FROM source_family=="user_profile" outputs specifically, since
# those fields describe the user rather than TA.
# ---------------------------------------------------------------------------

def test_plaintext_merge_reducer_outputs_reads_user_layer_fields_from_user_profile():
    outputs = [
        {
            "source_family": "user_profile",
            "identity": {
                "agent_name": "should be ignored",  # TA fields from user_profile don't count
                "dimensions": [{"name": "should be ignored", "value": 1}],
                "user_preferred_name": "Seven",
                "custom_persona_prompt": "始终用第二人称、简短直接。",
                "language_preference": "中文",
                "relationship_anchor": "大学室友",
                "stable_definitions": ["老板=我上司"],
            },
        },
        {
            "source_family": "ai_persona",
            "identity": {"agent_name": "Mira", "dimensions": [{"name": "稳定", "value": 80}]},
        },
    ]

    merged = plaintext._plaintext_merge_reducer_outputs(outputs)

    identity = merged["identity"]
    # TA identity firewall unaffected: agent_name/dimensions still only come
    # from the ai_persona output, never from user_profile.
    assert identity["agent_name"] == "Mira"
    assert identity["dimensions"] == [{"name": "稳定", "value": 80}]
    # user-layer fields DO come from the user_profile output.
    assert identity["user_preferred_name"] == "Seven"
    assert identity["custom_persona_prompt"] == "始终用第二人称、简短直接。"
    assert identity["language_preference"] == "中文"
    assert identity["relationship_anchor"] == "大学室友"
    assert identity["stable_definitions"] == ["老板=我上司"]


def test_plaintext_merge_reducer_outputs_sparse_user_layer_only_still_produces_identity():
    # No agent_name/dimensions anywhere, only a persona directive from a
    # user_profile output — must still surface as `identity` (B2 broadens the
    # "has any signal" gate the same way genesis/worker.py's _identity_only does).
    outputs = [{"source_family": "user_profile",
                "identity": {"custom_persona_prompt": "永远直接回答,不要绕。"}}]

    merged = plaintext._plaintext_merge_reducer_outputs(outputs)

    assert "identity" in merged
    assert merged["identity"]["custom_persona_prompt"] == "永远直接回答,不要绕。"
    assert "agent_name" not in merged["identity"]


def test_plaintext_merge_reducer_outputs_without_user_layer_signal_omits_it():
    outputs = [{"source_family": "ai_persona",
                "identity": {"agent_name": "Mira", "dimensions": [{"name": "稳定", "value": 80}]}}]

    merged = plaintext._plaintext_merge_reducer_outputs(outputs)

    identity = merged["identity"]
    for key in ("user_preferred_name", "custom_persona_prompt", "language_preference",
                "relationship_anchor", "stable_definitions"):
        assert key not in identity, key


def test_add_memory_mode_writes_only_memory(monkeypatch):
    store = _store()
    calls: dict = {}
    monkeypatch.setenv("FEEDLING_GENESIS_COMBINED_MAP", "1")
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_add", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)

    def fake_full_reducer(**kwargs):
        calls.setdefault("full_reducer_calls", []).append(kwargs)
        return {
            "source_kind": kwargs["source_kind"],
            "source_family": "history",
            "memories": [{"type": "fact", "summary": "用户养了一条狗", "content": "用户养了一条狗。"}],
            "identity": {"agent_name": "must_not_write", "dimensions": [{"name": "bad", "description": "bad"}]},
            "persona": {"content": "must not write"},
            "voice_workset": {"behavior_notes": ["bad"], "exemplars": []},
        }

    monkeypatch.setattr(plaintext.worker, "build_reducer_output_from_texts", fake_full_reducer)

    def fake_foreground(**kwargs):
        calls["foreground"] = kwargs
        return {
            "source_kind": kwargs["source_kind"],
            "source_family": "history",
            "all_fact_candidates": [{"summary": "用户养了一条狗"}],
            "core_fact_candidates": [{"summary": "用户养了一条狗"}],
        }

    monkeypatch.setattr(plaintext.worker, "build_foreground_output_from_texts", fake_foreground)

    def fake_fact_write(**kwargs):
        calls["fact_write"] = kwargs
        return {
            "memories": [{"type": "fact", "summary": "用户养了一条狗", "content": "用户养了一条狗。"}],
            "identity": {"agent_name": "must_not_write", "dimensions": [{"name": "bad", "description": "bad"}]},
            "persona": {"content": "must not write"},
            "voice_workset": {"behavior_notes": ["bad"], "exemplars": []},
        }

    monkeypatch.setattr(plaintext.worker, "build_memory_output_from_fact_candidates", fake_fact_write)
    monkeypatch.setattr(
        plaintext.service,
        "apply_memory_outputs",
        lambda _store, _api_key, output, **kwargs: calls.update({
            "memory_output": output,
            "memory_apply_kwargs": kwargs,
        }) or (1, [{"memory": {"id": "m1"}}]),
    )
    monkeypatch.setattr(plaintext.service, "init_identity_if_absent", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("add_memory must not touch identity")))
    monkeypatch.setattr(plaintext.service, "write_persona_artifact", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("add_memory must not write persona")))
    monkeypatch.setattr(plaintext.service, "write_voice_artifact", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("add_memory must not write voice")))
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: calls.update({"completed": kwargs}) or {"job_id": "job_add", "status": "done"},
    )

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_add",
        mode="add_memory",
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["我养了一条狗"]}],
        relationship_anchor={"days_with_user": 9999, "relationship_started_at": "2099-01-01"},
    )

    assert calls.get("full_reducer_calls", []) == []
    assert calls["foreground"]["write_core"] is False
    assert calls["foreground"].get("include_voice_candidates") in (None, False)
    assert calls["fact_write"]["fact_candidates"] == [{"summary": "用户养了一条狗"}]
    assert [item["summary"] for item in calls["memory_output"]["memories"]] == ["用户养了一条狗"]
    assert calls["memory_apply_kwargs"] == {
        "preserve_dates": False,
        "fallback_occurred_at": "2099-01-01",
    }
    assert calls["completed"]["memory_action_count"] == 1
    assert calls["completed"]["identity_status"] == "skipped"


def test_add_memory_keep_all_zero_cards_fails_with_map_diagnostics(monkeypatch):
    store = _store()
    calls: dict = {"statuses": []}
    monkeypatch.setattr(
        plaintext.hosted_config_store,
        "_load_runtime_provider_config",
        lambda *_args: "runtime",
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda *_args, **kwargs: calls["statuses"].append(kwargs) or {
            "job_id": "job_add_empty", "status": kwargs.get("status", "processing")
        },
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        plaintext.worker,
        "build_foreground_output_from_texts",
        lambda **_kwargs: {
            "source_kind": "memory_summary_import",
            "source_family": "memory_summary",
            "all_fact_candidates": [],
            "core_fact_candidates": [],
            "map_diagnostics": [{
                "chunk_index": 0,
                "task_id": "fact-map-0-empty-retry-1",
                "discard_reason": "empty_fact_candidates",
                "raw_output_snippet": '{"fact_candidates":[]}',
                "raw_output_chars": 22,
                "raw_output_truncated": False,
            }],
        },
    )
    monkeypatch.setattr(
        plaintext.worker,
        "build_memory_output_from_fact_candidates",
        lambda **_kwargs: {"memories": [], "identity": {}},
    )
    monkeypatch.setattr(
        plaintext.service,
        "apply_memory_outputs",
        lambda *_args, **_kwargs: (0, []),
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("zero-card keep_all job must not complete")
        ),
    )
    monkeypatch.setattr(
        plaintext.service,
        "mark_failed",
        lambda _store, job_id, error, **_kwargs: calls.update(
            {"failed_job_id": job_id, "error": error}
        ),
    )

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_add_empty",
        mode="add_memory",
        source_groups=[{
            "source_kind": "memory_summary_import",
            "source_family": "memory_summary",
            "chunk_texts": ["用户长期居住在上海，也一直从事产品设计。"],
        }],
    )

    assert calls["failed_job_id"] == "job_add_empty"
    assert "distill_empty_output" in calls["error"]
    assert plaintext.service.classify_genesis_error(calls["error"]) == "distill_empty_output"
    failed_output = next(
        item["output"] for item in reversed(calls["statuses"])
        if (item.get("output") or {}).get("stage") == "plaintext_add_memory_failed"
    )
    assert failed_output["distill_diagnostics"] == {
        "reason": "keep_all_zero_cards",
        "map_candidate_count": 0,
        "raw_memory_count": 0,
    }
    assert failed_output["map_diagnostics"][0]["raw_output_snippet"] == '{"fact_candidates":[]}'


def test_update_identity_mode_replaces_identity_without_writing_memory(monkeypatch):
    store = _store()
    calls: dict = {}
    trace_events: list[dict] = []
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: {"id": "identity_1"})
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_identity", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda _store, _msgs: "zh")
    monkeypatch.setattr(
        plaintext.history_import,
        "_derive_identity_with_provider",
        lambda *_args, **_kwargs: ({"agent_name": "乔伊", "dimensions": [{"name": "活泼", "description": "ENFP"}]}, []),
    )
    _stub_update_identity_persona(monkeypatch)
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, output, *_a, **_k: calls.update({"identity_output": output}) or "updated")
    monkeypatch.setattr(plaintext.service, "apply_memory_outputs", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("update_identity must not write memory")))
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: calls.update({"completed": kwargs}) or {"job_id": "job_identity", "status": "done"},
    )
    monkeypatch.setattr(
        plaintext,
        "_trace_genesis",
        lambda _store, event_type, **kwargs: trace_events.append({"event_type": event_type, **kwargs}),
    )

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_identity",
        mode="update_identity",
        source_groups=[{"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["Name: 乔伊"]}],
        analysis_messages=[{"role": "user", "content": "Name: 乔伊", "source": "ai_persona_import"}],
        relationship_anchor={"days_with_user": 9999, "relationship_started_at": "2099-01-01"},
    )

    assert calls["identity_output"]["identity"]["agent_name"] == "乔伊"
    assert calls["completed"]["memory_action_count"] == 0
    assert calls["completed"]["identity_status"] == "updated"
    done = next(event for event in trace_events if event["event_type"] == "genesis.plaintext.done")
    assert done["detail"] == {"mode": "update_identity", "identity_status": "updated"}


def test_update_identity_mode_initializes_missing_identity(monkeypatch):
    store = _store()
    calls: dict = {}
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: None)
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_identity", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda _store, _msgs: "zh")
    monkeypatch.setattr(
        plaintext.history_import,
        "_derive_identity_with_provider",
        lambda *_args, **_kwargs: ({
            "agent_name": "乔伊",
            "dimensions": [{"name": "活泼", "value": 88, "description": "上传角色卡明确写出。"}],
        }, []),
    )
    _stub_update_identity_persona(monkeypatch)
    monkeypatch.setattr(
        plaintext.service,
        "replace_identity_preserving_anchor",
        lambda _store, output, *_a, **_k: calls.update({"identity_output": output}) or "initialized",
    )
    monkeypatch.setattr(
        plaintext.service,
        "mark_failed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("missing identity must create, not fail")),
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: calls.update({"completed": kwargs}) or {"job_id": "job_identity", "status": "done"},
    )

    trace_events: list[dict] = []
    monkeypatch.setattr(
        plaintext,
        "_trace_genesis",
        lambda _store, event_type, **kwargs: trace_events.append({"event_type": event_type, **kwargs}),
    )

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_identity",
        mode="update_identity",
        source_groups=[{"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["Name: 乔伊"]}],
        analysis_messages=[{"role": "user", "content": "Name: 乔伊", "source": "ai_persona_import"}],
        relationship_anchor={
            "days_with_user": 42,
            "relationship_started_at": "2026-06-02",
            "relationship_anchor_evidence": "uploaded role card date",
        },
    )

    assert calls["identity_output"]["identity"]["agent_name"] == "乔伊"
    assert calls["completed"]["memory_action_count"] == 0
    assert calls["completed"]["identity_status"] == "initialized"
    done = next(event for event in trace_events if event["event_type"] == "genesis.plaintext.done")
    assert done["detail"] == {"mode": "update_identity", "identity_status": "initialized"}


def test_update_identity_rebuilds_persona_from_uploaded_role_card_material(monkeypatch):
    store = _store()
    calls: dict = {}
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: {"id": "identity_1"})
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_identity", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda _store, _msgs: "zh")
    monkeypatch.setattr(
        plaintext.history_import,
        "_derive_identity_with_provider",
        lambda *_args, **_kwargs: ({"agent_name": "乔伊", "dimensions": [{"name": "直爽", "description": "说人话。"}]}, []),
    )
    monkeypatch.setattr(
        plaintext,
        "_plaintext_existing_voice_workset_for_update",
        lambda _store, _api_key: {
            "behavior_notes": ["短句接住, 不绕弯。"],
            "exemplars": [{"founding": True, "turns": [{"speaker": "agent", "text": "我直接说。"}]}],
        },
        raising=False,
    )

    def fake_build_persona(**kwargs):
        calls["persona_kwargs"] = kwargs
        return {
            "persona": {
                "content": "## 你是谁\n\n你叫乔伊, 是一个硬核直爽的 AI 协作者。",
                "prompt_version": "7.B",
                "source_kind": "identity_update",
                "source_family": "ai_persona",
            },
            "voice_workset": kwargs["voice_workset"],
        }

    monkeypatch.setattr(plaintext.worker, "build_persona_output_from_material", fake_build_persona, raising=False)
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, output, *_a, **_k: calls.update({"identity_output": output}) or "updated")
    monkeypatch.setattr(plaintext.service, "write_persona_artifact", lambda _store, _job_id, output: calls.update({"persona_output": output}) or ("user_blob:genesis_persona", "sha-new"))
    monkeypatch.setattr(plaintext.service, "apply_memory_outputs", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("update_identity must not write memory")))
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: calls.update({"completed": kwargs}) or {"job_id": "job_identity", "status": "done"},
    )

    role_card = "名字：乔伊\n性格：硬核、直爽、懂你的全栈 AI 协作者"
    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_identity",
        mode="update_identity",
        source_groups=[{"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": [role_card]}],
        analysis_messages=[{"role": "user", "content": role_card, "source": "ai_persona_import"}],
        relationship_anchor={"days_with_user": 9999, "relationship_started_at": "2099-01-01"},
    )

    assert calls["identity_output"]["identity"]["agent_name"] == "乔伊"
    assert calls["persona_kwargs"]["persona_material"] == role_card
    assert "identity" not in calls["persona_kwargs"]["persona_material"].lower()
    assert calls["persona_kwargs"]["voice_workset"]["behavior_notes"] == ["短句接住, 不绕弯。"]
    assert calls["completed"]["memory_action_count"] == 0
    assert calls["completed"]["identity_status"] == "updated"
    assert calls["completed"]["persona_ref"] == "user_blob:genesis_persona"
    assert calls["completed"]["persona_sha256"] == "sha-new"


def test_update_identity_persona_rebuild_failure_does_not_replace_identity(monkeypatch):
    store = _store()
    calls: dict = {}
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: {"id": "identity_1"})
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_identity", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda _store, _msgs: "zh")
    monkeypatch.setattr(
        plaintext.history_import,
        "_derive_identity_with_provider",
        lambda *_args, **_kwargs: ({"agent_name": "乔伊", "dimensions": [{"name": "直爽", "description": "说人话。"}]}, []),
    )
    monkeypatch.setattr(plaintext, "_plaintext_existing_voice_workset_for_update", lambda *_args: {}, raising=False)
    monkeypatch.setattr(
        plaintext.worker,
        "build_persona_output_from_material",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider timeout")),
        raising=False,
    )
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not replace identity when persona rebuild fails")))
    monkeypatch.setattr(plaintext.db, "genesis_complete_job", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("failed persona rebuild must not complete job")))
    monkeypatch.setattr(plaintext.service, "mark_failed", lambda _store, job_id, error, **_kwargs: calls.update({"job_id": job_id, "error": error}))

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_identity",
        mode="update_identity",
        source_groups=[{"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["名字：乔伊"]}],
        analysis_messages=[{"role": "user", "content": "名字：乔伊", "source": "ai_persona_import"}],
    )

    assert calls["job_id"] == "job_identity"
    assert calls["error"].startswith("persona_rebuild_failed:")


def test_update_identity_mode_allows_nameless_nonempty_identity(monkeypatch):
    store = _store()
    calls: dict = {}
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: {"id": "identity_1"})
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_identity", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda _store, _msgs: "zh")
    monkeypatch.setattr(
        plaintext.history_import,
        "_derive_identity_with_provider",
        lambda *_args, **_kwargs: ({"agent_name": "", "dimensions": [{"name": "直爽", "description": "说人话。"}]}, []),
    )
    _stub_update_identity_persona(monkeypatch)
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, output, *_a, **_k: calls.update({"identity_output": output}) or "updated")
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: calls.update({"completed": kwargs}) or {"job_id": "job_identity", "status": "done"},
    )
    monkeypatch.setattr(plaintext.service, "apply_memory_outputs", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("update_identity must not write memory")))

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_identity",
        mode="update_identity",
        source_groups=[{"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["Role: 直爽的 AI 协作者"]}],
        analysis_messages=[{"role": "user", "content": "Role: 直爽的 AI 协作者", "source": "ai_persona_import"}],
        relationship_anchor={"days_with_user": 9999, "relationship_started_at": "2099-01-01"},
    )

    assert calls["identity_output"]["identity"]["agent_name"] == ""
    assert calls["identity_output"]["identity"]["dimensions"][0]["name"] == "直爽"
    assert calls["completed"]["memory_action_count"] == 0
    assert calls["completed"]["identity_status"] == "updated"


def test_update_identity_mode_fails_on_empty_identity(monkeypatch):
    store = _store()
    calls: dict = {}
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: {"id": "identity_1"})
    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", lambda *_args, **_kwargs: {"job_id": "job_identity", "status": "processing"})
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.history_import, "_import_language_for_store", lambda _store, _msgs: "zh")
    monkeypatch.setattr(
        plaintext.history_import,
        "_derive_identity_with_provider",
        lambda *_args, **_kwargs: ({"agent_name": "", "dimensions": [], "self_introduction": "", "category": "", "signature": []}, []),
    )
    monkeypatch.setattr(
        plaintext.worker,
        "build_persona_output_from_material",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("empty identity must not rebuild persona")),
        raising=False,
    )
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, _output, *_a, **_k: "identity_update_empty")
    monkeypatch.setattr(plaintext.db, "genesis_complete_job", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("empty identity must not complete job")))
    monkeypatch.setattr(plaintext.service, "apply_memory_outputs", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("update_identity must not write memory")))
    monkeypatch.setattr(plaintext.service, "mark_failed", lambda _store, job_id, error, **_kwargs: calls.update({"job_id": job_id, "error": error}))

    plaintext._run_plaintext_genesis_job(
        store,
        "api_key",
        "job_identity",
        mode="update_identity",
        source_groups=[{"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["Role:"]}],
        analysis_messages=[{"role": "user", "content": "Role:", "source": "ai_persona_import"}],
        relationship_anchor={"days_with_user": 9999, "relationship_started_at": "2099-01-01"},
    )

    assert calls == {"job_id": "job_identity", "error": "identity_update_empty"}


def test_update_identity_plaintext_enqueues_without_existing_identity(monkeypatch):
    client = _client(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: None)
    monkeypatch.setattr(plaintext.db, "genesis_list_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        plaintext.service,
        "create_import_job",
        lambda _store, payload, **_kwargs: ({
            "job_id": "identity_create",
            "status": "created",
            "source_kind": payload["source_kind"],
            "metadata": payload["metadata"],
        }, 201),
    )
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda _user_id, _job_id, **_kwargs: {
            "job_id": "identity_create",
            "status": "processing",
            "metadata": {"ingest": "plaintext", "mode": "update_identity"},
        },
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        plaintext,
        "_start_plaintext_genesis_job",
        lambda _store, _api_key, job, **kwargs: captured.update({"job": job, "mode": kwargs.get("mode")}) or True,
    )

    resp = client.post("/v1/genesis/imports/plaintext", json={
        "mode": "update_identity",
        "ai_persona_content": "Name: 乔伊",
        "client_job_id": "identity-test",
    })

    assert resp.status_code == 202
    assert resp.get_json()["status"] == "processing"
    assert captured["job"]["job_id"] == "identity_create"
    assert captured["mode"] == "update_identity"


def test_foreground_history_chunks_capped_support_untouched(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_FG_HISTORY_CAP", "8")
    groups = [
        {"source_family": "history", "chunk_texts": [f"h{i}" for i in range(27)]},
        {"source_family": "ai_persona", "chunk_texts": ["persona-card"]},
    ]
    capped = plaintext._cap_foreground_history_chunks(groups)
    hist = next(g for g in capped if g["source_family"] == "history")
    persona = next(g for g in capped if g["source_family"] == "ai_persona")
    assert len(hist["chunk_texts"]) == 8          # history 采样到 8
    assert hist["_checkpoint_chunk_indices"] == [0, 4, 7, 11, 15, 19, 22, 26]
    assert len(persona["chunk_texts"]) == 1        # 小桶不动
    assert persona["_checkpoint_chunk_indices"] == [0]


def test_plaintext_import_rejection_logs_breadcrumb(monkeypatch, caplog):
    # Observability: when an import 400s because uploaded material normalized to
    # empty, we must leave an always-on breadcrumb (server logs are the only trace
    # for this failure — no job row is created). The high-signal field is
    # material_present=True: the user DID upload something, yet it was dropped.
    client = _client(monkeypatch)
    # An account-export blob (uuid/email, no real content) is legitimately dropped,
    # so this still 400s post-fix — perfect to exercise the breadcrumb.
    blob = json.dumps([{"uuid": "u-1", "email_address": "a@b.com", "full_name": "S"}])
    with caplog.at_level("WARNING", logger="feedling.genesis.plaintext_import"):
        resp = client.post(
            "/v1/genesis/imports/plaintext",
            json={"format": "auto", "content": "", "mode": "add_memory",
                  "memory_summary_content": blob,
                  "memory_summary_filename": "export.json"},
        )
    assert resp.status_code == 400
    recs = [r for r in caplog.records if "genesis.plaintext.rejected" in r.getMessage()]
    assert recs, "expected a genesis.plaintext.rejected breadcrumb"
    msg = recs[0].getMessage()
    assert "material_present=True" in msg          # user uploaded material…
    assert "memory_summary_content" in msg         # …and we record which field + its size
    assert "a@b.com" not in msg                     # but never the content itself (lengths only)


def test_sealed_import_rejection_logs_breadcrumb(monkeypatch, caplog):
    # Resident/sealed lane: an incomplete sealed envelope 400s BEFORE a job row is
    # created (same blind spot the cloud upload 400 had). Assert the always-on
    # genesis.sealed.rejected breadcrumb fires — with structural facts only, no content.
    client = _client(monkeypatch)
    with caplog.at_level("WARNING", logger="feedling.genesis.plaintext_import"):
        resp = client.post(
            "/v1/genesis/imports/plaintext",
            json={"format": "sealed_v1", "mode": "add_memory",
                  "envelope": {"body_ct": "QUJD", "visibility": "shared"}},  # missing nonce/K_user/...
        )
    assert resp.status_code == 400
    assert resp.get_json().get("error") == "sealed_envelope_incomplete"
    recs = [r for r in caplog.records if "genesis.sealed.rejected" in r.getMessage()]
    assert recs, "expected a genesis.sealed.rejected breadcrumb"
    msg = recs[0].getMessage()
    assert "reason=sealed_envelope_incomplete" in msg
    assert "body_ct_bytes" in msg          # structural fact recorded (ciphertext length)
    assert "QUJD" not in msg               # never the (cipher)payload itself
