from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client  # noqa: E402
from genesis import worker  # noqa: E402


def _chunk(seq: int, body: bytes = b"ct") -> dict:
    return {
        "seq": seq,
        "encrypted_body": body + str(seq).encode("ascii"),
        "aad": {
            "envelope_meta": {
                "v": 1,
                "id": f"chunk-{seq}",
                "nonce": "nonce",
                "K_user": "ku",
                "K_enclave": "ke",
                "visibility": "shared",
                "owner_user_id": "usr_1",
            }
        },
    }


class _Resp:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status_code = status
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")


def _install_success_harness(monkeypatch, *, source_kind: str, chunk_texts: list[str], blobs: dict | None = None):
    apply_payloads = []
    minted = []
    blobs = blobs or {}
    chunks = [_chunk(idx) for idx in range(len(chunk_texts))]
    plaintext_by_id = {f"chunk-{idx}": text for idx, text in enumerate(chunk_texts)}

    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no fail")))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{
            "user_id": "usr_1",
            "job_id": "job_1",
            "status": "processing",
            "total_chunks": len(chunks),
            "source_kind": source_kind,
        }],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: [])
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: chunks)
    monkeypatch.setattr(
        worker.db,
        "get_blob",
        lambda _user_id, kind: {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "test_status": "ok",
        } if kind == "model_api" else blobs.get(kind),
    )
    monkeypatch.setattr(worker.httpx, "get", lambda *_args, **_kwargs: _Resp({"api_key_envelope": {"id": "provider-key"}}))

    def fake_post(url, *, headers=None, json=None, **_kwargs):
        assert headers == {"X-Feedling-Runtime-Token": "rtok"}
        if url.endswith("/v1/envelope/decrypt"):
            purpose = json["purpose"]
            if purpose == "model_api_provider_key":
                return _Resp({"plaintext_b64": base64.b64encode(b"sk-user").decode("ascii")})
            if purpose == "genesis_persona":
                return _Resp({"plaintext_b64": base64.b64encode(blobs["persona_plaintext"].encode()).decode("ascii")})
            if purpose == "genesis_voice":
                raw = blobs["voice_plaintext"]
                if isinstance(raw, dict):
                    raw = worker.json.dumps(raw, ensure_ascii=False)
                return _Resp({"plaintext_b64": base64.b64encode(str(raw).encode()).decode("ascii")})
            envelope_id = json["envelope"]["id"]
            return _Resp({"plaintext_b64": base64.b64encode(plaintext_by_id[envelope_id].encode()).decode("ascii")})
        assert url.endswith("/v1/genesis/imports/job_1/outputs")
        apply_payloads.append(json)
        return _Resp({"applied": {"ok": True}})

    monkeypatch.setattr(worker.httpx, "post", fake_post)

    def mint(user_id, scopes):
        minted.append({"user_id": user_id, "scopes": scopes})
        return "rtok"

    return apply_payloads, minted, mint


def test_source_family_accepts_import_suffix_aliases():
    assert worker._source_family("ai_persona_import") == "ai_persona"
    assert worker._source_family("character_import") == "ai_persona"
    assert worker._source_family("user_profile_import") == "user_profile"
    assert worker._source_family("memory_summary_import") == "memory_summary"
    assert worker._source_family("chat_export") == "history"


def test_plaintext_key_prefix_does_not_replace_persisted_job_id(monkeypatch):
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["task_id"] == "fact-write-0"
            return types.SimpleNamespace(
                text=json.dumps({
                    "memories": [],
                    "identity": {"agent_name": "Mira", "dimensions": []},
                }),
                usage={},
                cached=False,
                output_ref=kwargs["task_id"],
            )

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    output = worker.build_reducer_output_from_texts(
        user_id="usr_1",
        job_id="genesis_parent",
        key_prefix="genesis_parent:source_pass:3:memory_summary",
        runtime=types.SimpleNamespace(),
        chunk_texts=["Mira remembers the user."],
        source_kind="memory_summary_import",
    )

    assert output["identity"]["agent_name"] == "Mira"
    assert calls[0]["job_id"] == "genesis_parent"
    assert calls[0]["idempotency_key"] == "genesis_parent:source_pass:3:memory_summary:fact_write:0"


def test_tick_claims_decrypts_all_chunks_and_posts_distilled_output(monkeypatch):
    llm_calls = []
    apply_payloads = []
    minted = []

    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no fail")))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{"user_id": "usr_1", "job_id": "job_1", "status": "processing", "total_chunks": 2}],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: [])
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: [_chunk(0), _chunk(1)])
    monkeypatch.setattr(
        worker.db,
        "get_blob",
        lambda _user_id, kind: {
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "test_status": "ok",
        } if kind == "model_api" else None,
    )
    monkeypatch.setattr(
        worker.httpx,
        "get",
        lambda url, **kwargs: _Resp({"api_key_envelope": {"id": "provider-key"}}),
    )

    def fake_post(url, *, headers=None, json=None, **_kwargs):
        assert headers == {"X-Feedling-Runtime-Token": "rtok"}
        if url.endswith("/v1/envelope/decrypt"):
            purpose = json["purpose"]
            if purpose == "model_api_provider_key":
                return _Resp({"plaintext_b64": base64.b64encode(b"sk-user").decode("ascii")})
            envelope_id = json["envelope"]["id"]
            return _Resp({"plaintext_b64": base64.b64encode(f"text for {envelope_id}".encode()).decode("ascii")})
        assert url.endswith("/v1/genesis/imports/job_1/outputs")
        apply_payloads.append(json)
        return _Resp({"applied": {"ok": True}})

    monkeypatch.setattr(worker.httpx, "post", fake_post)

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("voice-map"):
                text = json.dumps({
                    "behavior_notes_candidates": ["short replies"],
                    "exemplar_candidates": [{"turns": [{"role": "ta", "text": "嗯"}], "axis": ["shape"]}],
                })
            elif task_id.startswith("fact-map"):
                text = json.dumps({"fact_candidates": [{"about": "user", "summary": "User likes tea", "evidence": "tea"}]})
            elif task_id.startswith("voice-reduce"):
                text = json.dumps({
                    "behavior_notes": ["short replies"],
                    "exemplars": [{"turns": [{"role": "ta", "text": "嗯"}], "founding": True}],
                })
            elif task_id.startswith("fact-write"):
                text = json.dumps({
                    "memories": [{"type": "fact", "summary": "User likes tea", "content": "User likes tea."}],
                    "identity": {"agent_name": "", "dimensions": [{"name": "Brief", "value": 70, "description": "Short replies"}]},
                    "days_with_user": 3,
                    "relationship_anchor_evidence": "import",
                })
            elif task_id == "persona-build":
                text = "## 你是谁\n\n你是 TA。\n\n## 你怎么说话\n\n短句。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    def mint(user_id, scopes):
        minted.append({"user_id": user_id, "scopes": scopes})
        return "rtok"

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
        max_jobs=1,
        now=lambda: 10.0,
    )

    assert result["processed"] == 1
    assert minted == [{"user_id": "usr_1", "scopes": ["envelope_decrypt", "genesis"]}]
    voice_inputs = [call["messages"][1]["content"] for call in llm_calls if call["task_id"].startswith("voice-map")]
    assert voice_inputs == ["text for chunk-0", "text for chunk-1"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["persona"]["content"].startswith("## 你是谁")
    assert reducer_output["memories"][0]["summary"] == "User likes tea"
    assert "raw_text" not in reducer_output
    assert "chunks" not in reducer_output


def test_complete_json_repairs_invalid_model_json_once():
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs)
            if kwargs["task_id"] == "voice-map-0":
                text = '{"behavior_notes_candidates":["keeps "quoted" words"],"exemplar_candidates":[]}'
            elif kwargs["task_id"] == "voice-map-0-json-repair":
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["task_id"] == "voice-map-0"
                assert "malformed_json" in payload
                assert kwargs["idempotency_key"] == "job_1:voice_map:0:json_repair"
                assert kwargs["temperature"] == 0.0
                text = json.dumps({
                    "behavior_notes_candidates": ['keeps "quoted" words'],
                    "exemplar_candidates": [],
                })
            else:
                raise AssertionError(kwargs["task_id"])
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=kwargs["task_id"])

    parsed = worker._complete_json(
        FakeLLM(),
        user_id="usr_1",
        job_id="job_1",
        task_id="voice-map-0",
        runtime=types.SimpleNamespace(),
        messages=[{"role": "user", "content": "x"}],
        max_tokens=1000,
        idempotency_key="job_1:voice_map:0",
    )

    assert parsed["behavior_notes_candidates"] == ['keeps "quoted" words']
    assert [call["task_id"] for call in calls] == ["voice-map-0", "voice-map-0-json-repair"]


def test_complete_json_repairs_truncated_model_json_with_larger_budget(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_LLM_MAX_TOKENS_PER_CALL", "8000")
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs)
            if kwargs["task_id"] == "voice-reduce-0":
                text = '{"behavior_notes":["short"],"exemplars":[{"turns":[{"role":"ta","text":"'
                return types.SimpleNamespace(
                    text=text,
                    usage={"output_tokens": 4000},
                    cached=False,
                    output_ref=kwargs["task_id"],
                    stop_reason="max_tokens",
                    max_tokens=4000,
                )
            if kwargs["task_id"] == "voice-reduce-0-json-repair":
                assert kwargs["max_tokens"] == 8000
                text = json.dumps({
                    "behavior_notes": ["short"],
                    "exemplars": [{"turns": [{"role": "ta", "text": "别急,我在。"}]}],
                })
                return types.SimpleNamespace(
                    text=text,
                    usage={"output_tokens": 210},
                    cached=False,
                    output_ref=kwargs["task_id"],
                    stop_reason="end_turn",
                    max_tokens=8000,
                )
            raise AssertionError(kwargs["task_id"])

    parsed = worker._complete_json(
        FakeLLM(),
        user_id="usr_1",
        job_id="job_1",
        task_id="voice-reduce-0",
        runtime=types.SimpleNamespace(),
        messages=[{"role": "user", "content": "x"}],
        max_tokens=4000,
        idempotency_key="job_1:voice_reduce:0:final",
    )

    assert parsed["exemplars"][0]["turns"][0]["text"] == "别急,我在。"
    assert [call["task_id"] for call in calls] == ["voice-reduce-0", "voice-reduce-0-json-repair"]


def test_voice_reduce_uses_high_output_budget():
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["task_id"] == "voice-reduce-0"
            assert kwargs["max_tokens"] == 4000
            text = json.dumps({"behavior_notes": ["short"], "exemplars": []})
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=kwargs["task_id"])

    result = worker._voice_reduce(
        FakeLLM(),
        user_id="usr_1",
        job_id="job_1",
        runtime=types.SimpleNamespace(),
        candidates=[{
            "behavior_notes_candidates": ["short"],
            "exemplar_candidates": [{"turns": [{"role": "ta", "text": "嗯"}]}],
        }],
    )

    assert result == {"behavior_notes": ["short"], "exemplars": []}
    assert len(calls) == 1


def test_ai_persona_source_uses_persona_material_without_voice_or_fact_map(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="ai_persona",
        chunk_texts=["你叫 Mira,保持稳定直接的语气。"],
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            payload = json.loads(kwargs["messages"][1]["content"])
            if task_id.startswith("fact-write"):
                assert payload["fact_digest"] == []
                assert "你叫 Mira" in payload["persona_material"]
                assert payload["memory_summary"] == ""
                text = json.dumps({
                    "memories": [{"summary": "should be dropped"}],
                    "identity": {"agent_name": "Mira", "dimensions": [{"name": "Direct", "value": 70, "description": "Persona says direct."}]},
                    "days_with_user": 5,
                    "relationship_anchor_evidence": "persona",
                })
            elif task_id == "persona-build":
                assert "你叫 Mira" in payload["persona_material"]
                assert payload["behavior_notes"] == []
                assert payload["founding_exemplars"] == []
                text = "## 你是谁\n\n你叫 Mira。\n\n## 你怎么说话\n\n稳定直接。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-write-0", "persona-build"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["source_family"] == "ai_persona"
    assert reducer_output["memories"] == []
    assert reducer_output["identity"]["agent_name"] == "Mira"
    assert reducer_output["persona"]["source_family"] == "ai_persona"
    assert reducer_output["voice"]["exemplar_count"] == 0
    assert "chunk 1" not in json.dumps(reducer_output, ensure_ascii=False)


def test_ai_persona_source_merges_existing_history_voice_workset(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="ai_persona",
        chunk_texts=["你叫 Mira,保持稳定直接的语气。"],
        blobs={
            worker.service.GENESIS_VOICE_BLOB: {"content_envelope": {"id": "voice-env"}},
            "voice_plaintext": {
                "behavior_notes": ["短句接住情绪"],
                "exemplars": [{
                    "turns": [{"role": "ta", "text": "别急,我在。"}],
                    "founding": True,
                    "axis": ["emotion"],
                    "why": "grounded",
                }],
            },
        },
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            payload = json.loads(kwargs["messages"][1]["content"])
            if task_id.startswith("fact-write"):
                text = json.dumps({"identity": {"agent_name": "Mira", "dimensions": []}, "memories": []})
            elif task_id == "persona-build":
                assert payload["persona_material"].startswith("--- chunk 1 ---")
                assert payload["behavior_notes"] == ["短句接住情绪"]
                assert payload["founding_exemplars"][0]["turns"][0]["text"] == "别急,我在。"
                text = "## 你是谁\n\n你叫 Mira。\n\n## 你怎么说话\n\n别急,我在。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-write-0", "persona-build"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["persona"]["source_family"] == "merged"
    assert reducer_output["voice"]["behavior_notes_count"] == 1
    assert reducer_output["voice"]["founding_exemplar_count"] == 1


def test_history_source_merges_existing_ai_persona_with_voice_exemplars(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="chat_export",
        chunk_texts=["user: 你会留下吗\nta: 别急,我在。"],
        blobs={
            worker.service.GENESIS_PERSONA_BLOB: {
                "source_priority": 100,
                "source_family": "ai_persona",
                "content_envelope": {"id": "persona-env"},
            },
            "persona_plaintext": "## 你是谁\n\n你叫 Mira。",
        },
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("voice-map"):
                text = json.dumps({
                    "behavior_notes_candidates": ["短句接住情绪"],
                    "exemplar_candidates": [{"turns": [{"role": "ta", "text": "别急,我在。"}], "axis": ["emotion"]}],
                })
            elif task_id.startswith("fact-map"):
                text = json.dumps({"fact_candidates": []})
            elif task_id.startswith("voice-reduce"):
                text = json.dumps({
                    "behavior_notes": ["短句接住情绪"],
                    "exemplars": [{"turns": [{"role": "ta", "text": "别急,我在。"}], "founding": True}],
                })
            elif task_id.startswith("fact-write"):
                text = json.dumps({"memories": [], "identity": {"agent_name": "", "dimensions": []}})
            elif task_id == "persona-build":
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["persona_material"].startswith("## 你是谁")
                assert payload["behavior_notes"] == ["短句接住情绪"]
                assert payload["founding_exemplars"][0]["turns"][0]["text"] == "别急,我在。"
                text = "## 你是谁\n\n你叫 Mira。\n\n## 你怎么说话\n\n别急,我在。"
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == [
        "voice-map-0", "fact-map-0", "voice-reduce-0", "persona-build",
    ]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["persona"]["source_family"] == "merged"
    assert reducer_output["voice_workset"]["behavior_notes"] == ["短句接住情绪"]
    assert reducer_output["voice_workset"]["exemplars"][0]["founding"] is True


def test_foreground_combined_map_extracts_fact_and_voice_in_one_call(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_COMBINED_MAP", "1")
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs["task_id"])
            assert kwargs["task_id"] == "combined-map-0"
            text = json.dumps({
                "fact_candidates": [{"about": "user", "summary": "用户叫 Z", "evidence": "我叫 Z"}],
                "voice_candidates": {
                    "behavior_notes_candidates": ["短句接住"],
                    "exemplar_candidates": [{"turns": [{"role": "ta", "text": "别急,我在。"}]}],
                },
            })
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=kwargs["task_id"])

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    output = worker.build_foreground_output_from_texts(
        user_id="usr_1",
        job_id="job_1",
        runtime=types.SimpleNamespace(),
        chunk_texts=["user: 我叫 Z\nta: 别急,我在。"],
        source_kind="history",
        write_core=False,
        include_voice_candidates=True,
    )

    assert calls == ["combined-map-0"]
    assert output["all_fact_candidates"] == [{"about": "user", "summary": "用户叫 Z", "evidence": "我叫 Z"}]
    assert output["voice_candidates"] == [{
        "behavior_notes_candidates": ["短句接住"],
        "exemplar_candidates": [{"turns": [{"role": "ta", "text": "别急,我在。"}]}],
    }]


def test_foreground_combined_map_retries_once_when_empty(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_COMBINED_MAP", "1")
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append({"task_id": kwargs["task_id"], "idempotency_key": kwargs["idempotency_key"]})
            if len(calls) == 1:
                text = json.dumps({"fact_candidates": [], "voice_candidates": {}})
            elif len(calls) == 2:
                text = json.dumps({
                    "fact_candidates": [{"about": "user", "summary": "用户叫 Z", "evidence": "我叫 Z"}],
                    "voice_candidates": {"behavior_notes_candidates": ["直说"], "exemplar_candidates": []},
                })
            else:
                raise AssertionError(kwargs["task_id"])
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=kwargs["task_id"])

    output = worker.build_foreground_output_from_texts(
        user_id="usr_1",
        job_id="job_1",
        runtime=types.SimpleNamespace(),
        chunk_texts=["user: 我叫 Z"],
        source_kind="history",
        llm=FakeLLM(),
        write_core=False,
        include_voice_candidates=True,
    )

    assert [call["task_id"] for call in calls] == ["combined-map-0", "combined-map-0-empty-retry-1"]
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]
    assert output["all_fact_candidates"][0]["summary"] == "用户叫 Z"
    assert output["voice_candidates"][0]["behavior_notes_candidates"] == ["直说"]


def test_fact_write_retries_once_when_empty(monkeypatch):
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append({"task_id": kwargs["task_id"], "idempotency_key": kwargs["idempotency_key"]})
            if len(calls) == 1:
                text = json.dumps({"memories": [], "identity": {"agent_name": "", "dimensions": []}})
            elif len(calls) == 2:
                text = json.dumps({
                    "memories": [{"summary": "用户叫 Z", "content": "用户叫 Z"}],
                    "identity": {"agent_name": "乔伊", "dimensions": []},
                })
            else:
                raise AssertionError(kwargs["task_id"])
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=kwargs["task_id"])

    output = worker.build_memory_output_from_fact_candidates(
        user_id="usr_1",
        job_id="job_1",
        runtime=types.SimpleNamespace(),
        fact_candidates=[{"about": "user", "summary": "用户叫 Z", "evidence": "我叫 Z"}],
        llm=FakeLLM(),
    )

    assert [call["task_id"] for call in calls] == ["fact-write-0", "fact-write-0-empty-retry-1"]
    assert calls[0]["idempotency_key"] != calls[1]["idempotency_key"]
    assert output["memories"][0]["summary"] == "用户叫 Z"
    assert output["identity"]["agent_name"] == "乔伊"


def test_fact_write_provider_config_error_does_not_retry(monkeypatch):
    calls = []

    class FakeLLM:
        def complete(self, **kwargs):
            calls.append(kwargs["task_id"])
            raise provider_client.ProviderError("out of credits", status_code=402)

    with pytest.raises(provider_client.ProviderError):
        worker.build_memory_output_from_fact_candidates(
            user_id="usr_1",
            job_id="job_1",
            runtime=types.SimpleNamespace(),
            fact_candidates=[{"about": "user", "summary": "用户叫 Z", "evidence": "我叫 Z"}],
            llm=FakeLLM(),
        )

    assert calls == ["fact-write-0"]


def test_user_profile_source_writes_memory_facts_without_identity_or_persona(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="user_profile",
        chunk_texts=["用户叫 Seven,喜欢直接反馈。"],
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("fact-map"):
                assert "source_kind=user_profile" in kwargs["messages"][1]["content"]
                text = json.dumps({"fact_candidates": [{"about": "user", "summary": "User likes direct feedback", "evidence": "direct"}]})
            elif task_id.startswith("fact-write"):
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["persona_material"] == ""
                assert payload["memory_summary"] == ""
                text = json.dumps({
                    "memories": [{"type": "fact", "summary": "User likes direct feedback", "content": "User likes direct feedback."}],
                    "identity": {"agent_name": "Seven", "dimensions": [{"name": "Wrong", "value": 90, "description": "from user profile"}]},
                    "days_with_user": 9,
                    "relationship_anchor_evidence": "user profile",
                })
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-map-0", "fact-write-0"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["source_family"] == "user_profile"
    assert reducer_output["memories"][0]["summary"] == "User likes direct feedback"
    assert "identity" not in reducer_output
    assert "persona" not in reducer_output


def test_memory_summary_source_feeds_fact_write_material_without_maps(monkeypatch):
    llm_calls = []
    apply_payloads, _minted, mint = _install_success_harness(
        monkeypatch,
        source_kind="memory_summary",
        chunk_texts=["用户在五月反复提到需要稳定陪伴。"],
    )

    class FakeLLM:
        def complete(self, **kwargs):
            task_id = kwargs["task_id"]
            llm_calls.append({"task_id": task_id, "messages": kwargs["messages"]})
            if task_id.startswith("fact-write"):
                payload = json.loads(kwargs["messages"][1]["content"])
                assert payload["fact_digest"] == []
                assert payload["persona_material"] == ""
                assert "稳定陪伴" in payload["memory_summary"]
                text = json.dumps({
                    "memories": [{"type": "fact", "summary": "User needs stable companionship", "content": "User needs stable companionship."}],
                    "identity": {"agent_name": "Mira", "dimensions": [{"name": "Wrong", "value": 90, "description": "from memory summary"}]},
                })
            else:
                raise AssertionError(task_id)
            return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task_id)

    monkeypatch.setattr(worker, "GenesisLLMClient", FakeLLM)

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=mint,
    )

    assert result["processed"] == 1
    assert [call["task_id"] for call in llm_calls] == ["fact-write-0"]
    reducer_output = apply_payloads[0]["reducer_output"]
    assert reducer_output["source_family"] == "memory_summary"
    assert reducer_output["memories"][0]["summary"] == "User needs stable companionship"
    assert reducer_output["identity"] == {"agent_name": "Mira", "dimensions": []}
    assert "persona" not in reducer_output


def test_tick_marks_failed_when_claimed_job_has_missing_chunks(monkeypatch):
    failures = []
    trace_events = []
    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda _store, job_id, error: failures.append((job_id, error)))
    monkeypatch.setattr(
        worker,
        "debug_trace",
        types.SimpleNamespace(trace_event=lambda *_args, **kwargs: trace_events.append(kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{"user_id": "usr_1", "job_id": "job_1", "status": "processing", "total_chunks": 2}],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: [1])
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: (_ for _ in ()).throw(AssertionError("no chunks")))

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=lambda *_args, **_kwargs: "rtok",
    )

    assert result["failed"] == 1
    assert failures[0][0] == "job_1"
    assert "missing_chunks:1" in failures[0][1]
    assert any(event["type"] == "genesis.worker.failed" for event in trace_events)


def test_tick_marks_failed_for_empty_import_without_provider_calls(monkeypatch):
    failures = []
    monkeypatch.setattr(worker, "get_store", lambda user_id: types.SimpleNamespace(user_id=user_id))
    monkeypatch.setattr(worker.service, "write_genesis_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.service, "mark_failed", lambda _store, job_id, error: failures.append((job_id, error)))
    monkeypatch.setattr(
        worker.db,
        "genesis_claim_uploaded_jobs",
        lambda limit=1: [{"user_id": "usr_1", "job_id": "job_1", "status": "processing", "total_chunks": 0}],
    )
    monkeypatch.setattr(worker.db, "genesis_missing_chunk_seqs", lambda *_args: (_ for _ in ()).throw(AssertionError("no missing check")))
    monkeypatch.setattr(worker.db, "genesis_list_chunks", lambda *_args: (_ for _ in ()).throw(AssertionError("no chunks")))
    monkeypatch.setattr(worker.httpx, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no provider")))

    result = worker.tick(
        api_url="http://backend:5001",
        enclave_url="https://enclave:5003",
        mint_runtime_token=lambda *_args, **_kwargs: "rtok",
    )

    assert result["failed"] == 1
    assert failures[0][0] == "job_1"
    assert "empty_import" in failures[0][1]


class _FaultyLLM:
    """Genesis LLM stub that fails specific tasks (by task_id) and returns
    plausible JSON for the rest, so we can exercise per-chunk fault tolerance
    in the map stage of build_reducer_output_from_texts."""

    def __init__(self, fail_tasks):
        self.fail_tasks = set(fail_tasks)
        self.calls: list[str] = []

    def complete(self, **kwargs):
        task = kwargs["task_id"]
        self.calls.append(task)
        if task in self.fail_tasks:
            raise provider_client.ProviderError("provider_http_402", status_code=402)
        if task.startswith("voice-map") or task.startswith("voice-reduce"):
            text = json.dumps({"behavior_notes": [], "exemplars": []})
        elif task.startswith("fact-map"):
            text = json.dumps({"fact_candidates": [{"text": f"fact:{task}"}]})
        elif task.startswith("fact-write"):
            text = json.dumps({"memories": [], "identity": {"agent_name": "Io", "dimensions": []}})
        elif task == "persona-build":
            text = "persona text"
        else:
            text = json.dumps({})
        return types.SimpleNamespace(text=text, usage={}, cached=False, output_ref=task)


def test_build_reducer_tolerates_single_chunk_map_failure(monkeypatch):
    llm = _FaultyLLM(fail_tasks={"fact-map-1"})
    monkeypatch.setattr(worker, "GenesisLLMClient", lambda *a, **k: llm)

    output = worker.build_reducer_output_from_texts(
        user_id="usr_1",
        job_id="job_partial",
        runtime=types.SimpleNamespace(),
        chunk_texts=["c0", "c1", "c2"],
        source_kind="history",
    )

    # One chunk's fact-map blew up, but the import still produced an output.
    assert isinstance(output, dict)
    assert {"fact-map-0", "fact-map-1", "fact-map-2"} <= set(llm.calls)
    # Reduce stage still ran (we didn't abort the whole job on one chunk).
    assert any(t.startswith("fact-write") for t in llm.calls)
    assert output.get("persona")


def test_build_reducer_fails_when_all_fact_maps_fail(monkeypatch):
    llm = _FaultyLLM(fail_tasks={"fact-map-0", "fact-map-1", "fact-map-2"})
    monkeypatch.setattr(worker, "GenesisLLMClient", lambda *a, **k: llm)

    with pytest.raises(worker.GenesisWorkerError):
        worker.build_reducer_output_from_texts(
            user_id="usr_1",
            job_id="job_allfail",
            runtime=types.SimpleNamespace(),
            chunk_texts=["c0", "c1", "c2"],
            source_kind="history",
        )


def test_reap_stale_atomically_fails_then_syncs_blobs(monkeypatch):
    # The DB does the conditional flip atomically (only rows still processing AND
    # still past the cutoff) and returns them; the reaper then syncs each row's
    # genesis_state blob to failed. No list->mark_failed TOCTOU window.
    reaped_rows = [
        {"user_id": "usr_a", "job_id": "job_a"},
        {"user_id": "usr_b", "job_id": "job_b"},
    ]
    captured = {}

    def fake_reap(older_than_sec, *, error, **_kw):
        captured["older_than_sec"] = older_than_sec
        captured["error"] = error
        return list(reaped_rows)

    monkeypatch.setattr(worker.db, "genesis_reap_stale_processing_jobs", fake_reap)
    monkeypatch.setattr(worker, "get_store", lambda uid: types.SimpleNamespace(user_id=uid))
    blobs = []
    monkeypatch.setattr(
        worker.service,
        "write_genesis_state",
        lambda store, job, status: blobs.append((store.user_id, job["job_id"], status)),
    )

    reaped = worker.reap_stale_processing_jobs()

    assert captured["older_than_sec"] >= 300
    assert "stale" in captured["error"]
    assert {(u, j, s) for u, j, s in blobs} == {("usr_a", "job_a", "failed"), ("usr_b", "job_b", "failed")}
    assert {r["job_id"] for r in reaped} == {"job_a", "job_b"}


def test_reap_stale_blob_sync_failure_still_counts_job_reaped(monkeypatch):
    # The DB already flipped both to failed atomically; a blob-sync hiccup on one
    # must not stop the other, and both jobs still count as reaped (they ARE failed).
    reaped_rows = [
        {"user_id": "usr_a", "job_id": "job_a"},
        {"user_id": "usr_b", "job_id": "job_b"},
    ]
    monkeypatch.setattr(worker.db, "genesis_reap_stale_processing_jobs", lambda *a, **k: list(reaped_rows))
    monkeypatch.setattr(worker, "get_store", lambda uid: types.SimpleNamespace(user_id=uid))
    synced = []

    def flaky_blob(store, job, status):
        if job["job_id"] == "job_a":
            raise RuntimeError("blob write blip")
        synced.append(job["job_id"])

    monkeypatch.setattr(worker.service, "write_genesis_state", flaky_blob)

    reaped = worker.reap_stale_processing_jobs()

    assert synced == ["job_b"]
    assert {r["job_id"] for r in reaped} == {"job_a", "job_b"}


class _FakeLLM:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def complete(self, *a, **k):
        item = self.seq[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return type("R", (), {"text": item, "usage": None})()


def test_retry_empty_also_retries_transient_then_succeeds(monkeypatch):
    # 1st call ReadTimeout (transient) -> 2nd call returns usable JSON.
    llm = _FakeLLM([provider_client.ProviderError("provider network error: ReadTimeout"),
                    '{"fact_candidates":[{"about":"user","summary":"x"}]}'])
    out = worker._complete_json_retry_empty(
        llm, user_id="u", job_id="j", task_id="fact-map-0",
        runtime=object(), messages=[{"role": "user", "content": "x"}],
        max_tokens=100, idempotency_key="k", is_empty=worker._combined_map_empty, max_attempts=3)
    assert out["fact_candidates"][0]["summary"] == "x"
    assert llm.calls == 2


def test_retry_empty_does_not_retry_provider_config(monkeypatch):
    # 402 provider_config -> raises immediately, no retry.
    llm = _FakeLLM([provider_client.ProviderError("insufficient credit", status_code=402),
                    '{"fact_candidates":[]}'])
    with pytest.raises(provider_client.ProviderError):
        worker._complete_json_retry_empty(
            llm, user_id="u", job_id="j", task_id="fact-map-0",
            runtime=object(), messages=[{"role": "user", "content": "x"}],
            max_tokens=100, idempotency_key="k", is_empty=worker._combined_map_empty, max_attempts=3)
    assert llm.calls == 1
