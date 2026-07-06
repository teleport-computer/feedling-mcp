from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from genesis import genesis_core, plaintext, service  # noqa: E402
from urllib.parse import urlparse  # noqa: E402


def _store(user_id: str = "usr_plaintext"):
    return types.SimpleNamespace(user_id=user_id)


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
        raise AssertionError(f"unrouted path: {p}")


def _client(monkeypatch):
    return _CoreClient(_store())


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

    def fake_create(_store, create_payload):
        captured["create_payload"] = create_payload
        job = {
            "job_id": "genesis_job_1",
            "status": "created",
            "source_kind": create_payload["source_kind"],
            "metadata": {
                **create_payload["metadata"],
                "privacy_copy": service.PRIVACY_COPY,
            },
            "privacy_mode": service.PRIVACY_MODE,
        }
        return job, 201

    monkeypatch.setattr(plaintext.service, "create_import_job", fake_create)
    monkeypatch.setattr(
        plaintext.db,
        "genesis_set_job_status",
        lambda _user_id, _job_id, **_kwargs: {
            "job_id": "genesis_job_1",
            "status": "processing",
            "metadata": captured["create_payload"]["metadata"],
            "source_kind": captured["create_payload"]["source_kind"],
            "privacy_mode": service.PRIVACY_MODE,
        },
    )
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)

    def fake_start(_store, _api_key, job, *, mode="onboarding", chunk_texts, source_kind, source_groups=None, relationship_anchor=None, analysis_messages=None):
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


def test_plaintext_import_reuses_done_job_without_restart(monkeypatch):
    client = _client(monkeypatch)
    payload = {"format": "plaintext", "content": "User: hello"}
    input_hash = plaintext.history_import._history_import_payload_hash(payload)
    existing = {
        "job_id": "genesis_done",
        "status": "done",
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
        "history",
        "memory_summary",
        "user_profile",
    ]
    assert prepared["chunk_texts"] == [
        "ai_persona_import:1",
        "history_import:1",
        "memory_summary_import:1",
        "user_profile_import:1",
    ]


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
        }

    monkeypatch.setattr(plaintext.db, "genesis_set_job_status", fake_set_status)
    monkeypatch.setattr(plaintext.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(plaintext.hosted_config_store, "_load_runtime_provider_config", lambda *_args: "runtime")

    def fake_build(**kwargs):
        calls["build"] = kwargs
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
    assert calls["applied"]["memories"] == []
    assert calls["statuses"][-1]["processed_chunks"] == 1
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
                "memories": [{"type": "fact", "summary": "Memory summary", "content": "Memory summary."}],
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
    serialized = json.dumps(applied, ensure_ascii=False)
    assert "WrongUserName" not in serialized
    assert "bad user persona" not in serialized


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
        lambda _store, _api_key, output: calls.update({"memory_output": output}) or (1, [{"memory": {"id": "m1"}}]),
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
    assert calls["completed"]["memory_action_count"] == 1
    assert calls["completed"]["identity_status"] == "skipped"


def test_update_identity_mode_replaces_identity_without_writing_memory(monkeypatch):
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
        lambda *_args, **_kwargs: ({"agent_name": "乔伊", "dimensions": [{"name": "活泼", "description": "ENFP"}]}, []),
    )
    _stub_update_identity_persona(monkeypatch)
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, output: calls.update({"identity_output": output}) or "updated")
    monkeypatch.setattr(plaintext.service, "apply_memory_outputs", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("update_identity must not write memory")))
    monkeypatch.setattr(
        plaintext.db,
        "genesis_complete_job",
        lambda _user_id, _job_id, **kwargs: calls.update({"completed": kwargs}) or {"job_id": "job_identity", "status": "done"},
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
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, output: calls.update({"identity_output": output}) or "updated")
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
    monkeypatch.setattr(plaintext.service, "mark_failed", lambda _store, job_id, error: calls.update({"job_id": job_id, "error": error}))

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
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, output: calls.update({"identity_output": output}) or "updated")
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
    monkeypatch.setattr(plaintext.service, "replace_identity_preserving_anchor", lambda _store, _output: "identity_update_empty")
    monkeypatch.setattr(plaintext.db, "genesis_complete_job", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("empty identity must not complete job")))
    monkeypatch.setattr(plaintext.service, "apply_memory_outputs", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("update_identity must not write memory")))
    monkeypatch.setattr(plaintext.service, "mark_failed", lambda _store, job_id, error: calls.update({"job_id": job_id, "error": error}))

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


def test_update_identity_plaintext_requires_existing_identity(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(plaintext.identity_service, "_load_identity", lambda _store: None)
    monkeypatch.setattr(
        plaintext.service,
        "create_import_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create job")),
    )

    resp = client.post("/v1/genesis/imports/plaintext", json={
        "mode": "update_identity",
        "ai_persona_content": "Name: 乔伊",
        "client_job_id": "identity-test",
    })

    assert resp.status_code == 409
    assert resp.get_json()["error"] == "identity_not_initialized"


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
    assert len(persona["chunk_texts"]) == 1        # 小桶不动
