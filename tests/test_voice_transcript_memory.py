"""Pure tests for voice transcript memory capture: window rendering matches the
established V2 capture shape, consent is fail-closed with zero model calls, and
the happy path wires the real capture prompt + action converter + apply seam.
(The distill quality itself needs a live call; that half is e2e.)"""

from __future__ import annotations

import json
from types import SimpleNamespace

from proactive import capture_scheduler
from voice import transcript_memory


def test_transcript_window_matches_v2_capture_shape():
    # Byte-exact expected shape: "- {label}: {text}" lines, joined by \n.
    # Server-side there is no plaintext name source, so labels are the same
    # fallbacks the hosted V2 capture window uses: user -> 「对方」, ai -> 「我」
    # (never the literal role — the "user:" prefix incident).
    turns = [
        {"role": "user", "text": "  今天有点累  "},
        {"role": "assistant", "text": "要不要早点休息"},
        {"role": "user", "text": "   "},  # blank text dropped
        "not-a-dict",  # malformed turn dropped
        {"role": "assistant", "text": ""},  # empty dropped
    ]
    assert transcript_memory.render_transcript_window(turns) == (
        "- 对方: 今天有点累\n- 我: 要不要早点休息"
    )


def test_transcript_window_uses_names_when_known():
    turns = [
        {"role": "user", "text": "明天要交报告"},
        {"role": "assistant", "text": "我帮你列个提纲"},
    ]
    rendered = transcript_memory.render_transcript_window(
        turns, user_name="小明", ai_name="小雨"
    )
    assert rendered == "- 小明: 明天要交报告\n- 小雨: 我帮你列个提纲"


def _forbid_model_calls(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("model/provider path must not be reached")

    monkeypatch.setattr(
        transcript_memory.provider_client, "reliable_chat_completion", _boom
    )
    monkeypatch.setattr(
        transcript_memory.hosted_config_store,
        "_load_runtime_provider_config",
        _boom,
    )
    monkeypatch.setattr(transcript_memory.results, "mint_enclave_token", _boom)


def test_consent_off_returns_false_with_zero_model_calls(monkeypatch):
    store = SimpleNamespace(user_id="u_consent_off")
    monkeypatch.setattr(
        capture_scheduler.db,
        "get_blob_strict",
        lambda _uid, _key: {"capture_enabled": False},
    )
    _forbid_model_calls(monkeypatch)
    assert (
        transcript_memory.capture_from_transcript(
            store, [{"role": "user", "text": "hi"}], call_id="vcall_x"
        )
        is False
    )


def test_broken_consent_read_fails_closed(monkeypatch):
    store = SimpleNamespace(user_id="u_consent_err")

    def _broken(_uid, _key):
        raise RuntimeError("settings read failed")

    monkeypatch.setattr(capture_scheduler.db, "get_blob_strict", _broken)
    _forbid_model_calls(monkeypatch)
    assert (
        transcript_memory.capture_from_transcript(
            store, [{"role": "user", "text": "hi"}], call_id="vcall_x"
        )
        is False
    )


def test_empty_window_returns_false_without_model_calls(monkeypatch):
    store = SimpleNamespace(user_id="u_empty")
    monkeypatch.setattr(
        capture_scheduler.db, "get_blob_strict", lambda _uid, _key: None
    )
    _forbid_model_calls(monkeypatch)
    assert (
        transcript_memory.capture_from_transcript(
            store, [{"role": "user", "text": "   "}], call_id="vcall_x"
        )
        is False
    )


def test_happy_path_wires_prompt_context_and_actions(monkeypatch):
    store = SimpleNamespace(user_id="u_happy_path")
    monkeypatch.setattr(
        capture_scheduler.db, "get_blob_strict", lambda _uid, _key: None
    )
    monkeypatch.setattr(
        transcript_memory.results, "mint_enclave_token", lambda _uid: "tok"
    )
    monkeypatch.setattr(
        transcript_memory.memory_core,
        "existing_terms",
        lambda _store, _key, *, post_enclave: (["工作"], ["项目上线"]),
    )
    monkeypatch.setattr(
        transcript_memory.identity_core,
        "get_identity",
        lambda _store: ({"identity": {"summary": "沉稳一点的陪伴者"}}, 200),
    )
    monkeypatch.setattr(
        transcript_memory.hosted_config_store,
        "_load_runtime_provider_config",
        lambda _store, _key, runtime_token: object(),
    )

    seen = {}

    def _fake_completion(_runtime, messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        seen["kwargs"] = kwargs
        return {
            "reply": json.dumps(
                {
                    "cards": [
                        {
                            "action": "add",
                            "type": "event",
                            "summary": "最近在赶一个项目上线，压力比较大",
                            "content": "这周都在加班准备项目上线，说自己有点撑不住了。",
                            "bucket": "工作",
                            "threads": ["项目上线"],
                            "importance": 0.6,
                            "pulse": 0.4,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr(
        transcript_memory.provider_client,
        "reliable_chat_completion",
        _fake_completion,
    )
    monkeypatch.setattr(
        transcript_memory.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _payload, item_id=None: ({"v": 1, "id": item_id}, None),
    )
    applied = []
    monkeypatch.setattr(
        transcript_memory.memory_core,
        "actions",
        lambda _store, _key, payload, *, runtime_token: (
            applied.append((payload, runtime_token))
            or ({"status": "ok", "applied_count": 1, "failed_count": 0}, 200)
        ),
    )

    turns = [
        {"role": "user", "text": "这周都在加班赶项目"},
        {"role": "assistant", "text": "记得照顾好自己"},
    ]
    assert (
        transcript_memory.capture_from_transcript(store, turns, call_id="vcall_hp")
        is True
    )
    # The real capture prompt saw the transcript window and the memory context.
    assert "- 对方: 这周都在加班赶项目" in seen["prompt"]
    assert "- 我: 记得照顾好自己" in seen["prompt"]
    assert "工作" in seen["prompt"] and "项目上线" in seen["prompt"]
    assert seen["kwargs"]["max_tokens"] == 1200
    assert seen["kwargs"]["timeout"] == 60.0
    assert seen["kwargs"]["max_attempts"] == 2
    # One memory.add action with a prebuilt envelope, deterministic voice id,
    # no chat-row provenance (the per-turn rows are deleted at finalize).
    assert len(applied) == 1
    payload, runtime_token = applied[0]
    assert runtime_token == "tok"
    (action,) = payload["actions"]
    assert action["type"] == "memory.add"
    assert action["envelope"]["id"].startswith("mom_voice_")
    assert action["envelope"]["occurred_at"].endswith("Z")
    assert action["source_chat_message_ids"] == []
    assert action["capture_mode"] == "memory_capture"


def test_no_cards_reply_is_a_legal_success(monkeypatch):
    store = SimpleNamespace(user_id="u_no_cards")
    monkeypatch.setattr(
        capture_scheduler.db, "get_blob_strict", lambda _uid, _key: None
    )
    monkeypatch.setattr(
        transcript_memory.results, "mint_enclave_token", lambda _uid: "tok"
    )
    monkeypatch.setattr(
        transcript_memory.memory_core,
        "existing_terms",
        lambda _store, _key, *, post_enclave: ([], []),
    )
    monkeypatch.setattr(
        transcript_memory.identity_core, "get_identity", lambda _store: ({}, 404)
    )
    monkeypatch.setattr(
        transcript_memory.hosted_config_store,
        "_load_runtime_provider_config",
        lambda _store, _key, runtime_token: object(),
    )
    monkeypatch.setattr(
        transcript_memory.provider_client,
        "reliable_chat_completion",
        lambda _runtime, _messages, **_kwargs: {"reply": '{"cards": []}'},
    )

    def _no_apply(*_args, **_kwargs):
        raise AssertionError("no actions must be applied for an empty card set")

    monkeypatch.setattr(transcript_memory.memory_core, "actions", _no_apply)
    assert (
        transcript_memory.capture_from_transcript(
            store, [{"role": "user", "text": "喂喂喂 测试"}], call_id="vcall_nc"
        )
        is True
    )
