from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import provider_client  # noqa: E402
from genesis.llm_client import DistillModelTooSlowError, GenesisLLMClient  # noqa: E402


def _runtime():
    return provider_client.ProviderConfig(
        provider="openai",
        model="gpt-test",
        api_key="sk-user-secret",
        base_url="https://api.openai.com/v1",
    )


def test_genesis_llm_client_ignores_plaintext_legacy_cache(monkeypatch):
    from genesis import llm_client

    captured = {}
    monkeypatch.setattr(
        llm_client.db,
        "genesis_upsert_output",
        lambda user_id, job_id, output_type, *, doc, status, ref: captured.update(
            {
                "user_id": user_id,
                "job_id": job_id,
                "output_type": output_type,
                "doc": doc,
                "status": status,
                "ref": ref,
            }
        ),
    )

    def fake_completion(runtime, messages, **_kwargs):
        assert runtime.api_key == "sk-user-secret"
        assert messages[0]["content"] == "hello"
        return {"reply": "fresh text", "usage": {"total_tokens": 4}}

    result = GenesisLLMClient(completion_fn=fake_completion).complete(
        user_id="usr",
        job_id="job",
        task_id="map-1",
        runtime=_runtime(),
        messages=[{"role": "user", "content": "hello"}],
        idempotency_key="job:map:1",
    )

    assert result.cached is False
    assert result.text == "fresh text"
    assert captured["doc"]["plaintext_stored"] is False
    assert "text" not in captured["doc"]


def test_genesis_llm_client_persists_response_metadata_without_plaintext_or_api_key(monkeypatch):
    from genesis import llm_client

    captured = {}
    monkeypatch.setattr(
        llm_client.db,
        "genesis_upsert_output",
        lambda user_id, job_id, output_type, *, doc, status, ref: captured.update(
            {
                "user_id": user_id,
                "job_id": job_id,
                "output_type": output_type,
                "doc": doc,
                "status": status,
                "ref": ref,
            }
        ),
    )

    def fake_completion(runtime, messages, **kwargs):
        assert runtime.api_key == "sk-user-secret"
        assert messages[0]["content"] == "hello"
        assert kwargs["max_tokens"] == 321
        return {
            "reply": "new text",
            "usage": {"total_tokens": 9},
            "stop_reason": "max_tokens",
        }

    result = GenesisLLMClient(completion_fn=fake_completion).complete(
        user_id="usr",
        job_id="job",
        task_id="map-1",
        runtime=_runtime(),
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=321,
        idempotency_key="job:map:1",
    )

    assert result.cached is False
    assert result.text == "new text"
    assert result.stop_reason == "max_tokens"
    assert result.max_tokens == 321
    assert captured["status"] == "done"
    assert captured["doc"]["plaintext_stored"] is False
    assert captured["doc"]["response_sha256"]
    assert captured["doc"]["response_chars"] == len("new text")
    assert captured["doc"]["stop_reason"] == "max_tokens"
    assert "text" not in captured["doc"]
    assert captured["doc"]["usage"] == {"total_tokens": 9}
    assert "api_key" not in json.dumps(captured["doc"])
    assert "sk-user-secret" not in json.dumps(captured["doc"])
    assert "new text" not in json.dumps(captured["doc"])


def test_genesis_llm_client_heartbeats_job_after_each_call(monkeypatch):
    # Every genesis LLM call goes through complete(), so heartbeating the job here
    # bumps updated_at across the WHOLE reducer (map AND reduce AND early-return
    # source families), not just the map loop — closing the stale-reaper gap with
    # no per-call-site wiring to forget.
    from genesis import llm_client

    monkeypatch.setattr(llm_client.db, "genesis_upsert_output", lambda *a, **k: None)
    touched = []
    monkeypatch.setattr(
        llm_client.db, "genesis_touch_job", lambda user_id, job_id: touched.append((user_id, job_id))
    )

    def fake_completion(runtime, messages, **_kwargs):
        return {"reply": "ok", "usage": {}}

    GenesisLLMClient(completion_fn=fake_completion).complete(
        user_id="usr",
        job_id="job",
        task_id="t",
        runtime=_runtime(),
        messages=[{"role": "user", "content": "hi"}],
        idempotency_key="k",
    )

    assert touched == [("usr", "job")]


def test_genesis_canary_uses_real_first_call_once_then_normal_timeout(monkeypatch):
    from genesis import llm_client

    monkeypatch.setenv("FEEDLING_GENESIS_CANARY_TIMEOUT_SEC", "17")
    monkeypatch.setattr(llm_client.db, "genesis_upsert_output", lambda *a, **k: None)
    monkeypatch.setattr(llm_client.db, "genesis_touch_job", lambda *a, **k: None)
    calls = []

    def fake_completion(_runtime, _messages, **kwargs):
        calls.append(kwargs)
        return {"reply": "{}", "usage": {}}

    client = GenesisLLMClient(completion_fn=fake_completion, canary=True)
    for key in ("first", "second"):
        client.complete(
            user_id="usr",
            job_id="job",
            task_id=key,
            runtime=_runtime(),
            messages=[{"role": "user", "content": key}],
            timeout=90,
            idempotency_key=key,
        )

    assert [call["timeout"] for call in calls] == [17.0, 90]


def test_genesis_canary_failure_has_stable_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("relay unavailable")

    with pytest.raises(DistillModelTooSlowError, match="distill_model_too_slow") as exc:
        GenesisLLMClient(completion_fn=fail, persist_output=False, canary=True).complete(
            user_id="usr",
            job_id="job",
            task_id="first-map",
            runtime=_runtime(),
            messages=[{"role": "user", "content": "hello"}],
            idempotency_key="first",
        )
    assert exc.value.feedling_error_class == "provider_config"
