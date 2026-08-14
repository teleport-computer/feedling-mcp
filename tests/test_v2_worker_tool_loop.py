"""worker.process_job's chat branch on the unified provider-native tool loop.

Style mirrors tests/test_v2_worker.py: real jobs_store (real DB claim/mark_*/
runtime_state), real core_store (real DB chat/reload), real
model_api_runtime.v2.coalesce/executor/effect_outbox/tool_loop; the two
boundaries stubbed are `cap_registry.run_capability` (capability correctness
has its own test files) and `provider_client.chat_completion_async` (the LLM
wire boundary tool_loop.run_tool_loop calls once per round — scripted here to
drive specific round shapes).
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
import worldbook_match
from admin import data_track as admin_data_track
from provider_types import ToolCall, ToolExchange
from capabilities import registry as cap_registry
from core import envelope as core_envelope
from core import self_thinking
from core import store as core_store
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import effect_id as v2_effect_id
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="needs PG",
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="sk-user-byok",
    base_url="",
)

_FABLE_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-fable-5",
    api_key="sk-user-byok",
    base_url="",
)


class _FakeCapResult:
    def __init__(self, data=None, ok=True):
        self._data = data or {}
        self._ok = ok

    def to_dict(self):
        return {
            "ok": self._ok,
            "data": self._data,
            "error": None,
            "trace": {},
            "warnings": [],
        }


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table(monkeypatch):
    """Mirrors test_v2_worker.py's fixture: claim_next_job() is a global claim,
    not filtered by user_id, so a stray row from another test module would
    otherwise get claimed here instead of this file's own row."""
    # Exact successor-count assertions describe the profile-off contract.
    monkeypatch.setenv("FEEDLING_V2_PROFILE_ENABLED", "0")
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", False)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _patch_real_write(monkeypatch):
    """`worker._write_encrypted_reply`'s real envelope-build path needs a live
    enclave (`core_envelope._build_shared_envelope_for_store` calls
    `enclave._get_enclave_info()`), unavailable in this test process — the
    same reason every DB-backed V2 test (test_v2_worker.py,
    test_v2_p0_exactly_once.py) stubs it. This variant still performs a REAL
    `store.append_chat(..., strict=True)` DB write (a fixed-shape envelope —
    the server stores ciphertext verbatim regardless of shape, see
    `append_chat`'s docstring) so `_bubbles` below reads back genuine
    chat_messages rows, only the encryption step itself is skipped."""

    def _real_write(store, text, *, extra=None):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat(
            "openclaw", "model_api", envelope, strict=True, extra=(extra or None)
        )

    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _patch_tool_effect_encryption(monkeypatch):
    def _fake_build(store, plaintext, *, item_id=None):
        return (
            {
                "id": item_id,
                "owner_user_id": store.user_id,
                "v": 1,
                "body_ct": base64.b64encode(plaintext).decode("ascii"),
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        )

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        _fake_build,
    )


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the `reply` effect_type — mirrors
    `serve_worker._sink_reply`'s real write (`worker._write_encrypted_reply`)
    without pulling in serve_worker's hosted-adjacent wiring."""

    def dispatch(effect_type, payload):
        if effect_type == "reply":
            extra = worker._reply_effect_extra(payload)
            worker._write_encrypted_reply(
                core_store.get_store(user_id),
                str(payload.get("text") or ""),
                **({"extra": extra} if extra else {}),
            )

    return dispatch


def _apply_effects(user_id):
    return v2_effect_outbox.apply_pending_effects(
        user_id, dispatch=_reply_effect_dispatch(user_id)
    )


def _script_provider(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None, **kwargs):
        calls.append({"messages": messages, "tools": tools, **kwargs})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {
        "reply": text,
        "tool_calls": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _tool_result_contents(call) -> set[str]:
    """Tool results reach the next provider round as ToolExchange objects, not
    plain message dicts."""
    out = set()
    for message in call["messages"]:
        for result in getattr(message, "results", ()) or ():
            out.add(str(result.content or ""))
    return out


def _tool_round(*tool_calls, prompt_tokens=1, completion_tokens=1):
    return {
        "reply": "",
        "tool_calls": list(tool_calls),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _tc(call_id, name, **args):
    return {"id": call_id, "name": name, "args": args}


def _deps(*, messages, token="rt-enclave", web_enabled=True, observe_photo=None):
    return worker.TurnDeps(
        # web_search/web_fetch are gated per user now (default OFF); these
        # tests use them as a generic outbound read, so opt in explicitly.
        web_tools_enabled=lambda uid: web_enabled,
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: token,
        observe_photo=observe_photo,
        apply_pending_effects=_apply_effects,
    )


def _bubbles(uid):
    """Real chat_messages rows written for this user's model-authored replies,
    in seq (durable write) order — `role="openclaw"`/`source="model_api"` is
    exactly what `worker._write_encrypted_reply` always writes."""
    store = core_store.get_store(uid)
    store.reload()
    return [
        m
        for m in store.chat_messages
        if m.get("role") == "openclaw" and m.get("source") == "model_api"
    ]


def _turn_metric_row(job_id):
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT model_calls, failed, status FROM v2_turn_metrics WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    return row


def _user_doc(message_id: str, text: str) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "ts": 10.0,
        "source": "model_api",
        "body_ct": f"cipher-{message_id}",
        "nonce": "n",
        "K_user": "k",
        "K_enclave": "e",
        # Test-only plaintext lookup; production's injected reader obtains the
        # same value by decrypting the envelope in the enclave.
        "test_plaintext": text,
    }


def _late_input_deps(uid: str, written: list[str]) -> worker.TurnDeps:
    def read_after_seq(_user_id: str, after_seq: int):
        rows = db.chat_messages_after_seq(uid, after_seq, limit=None)
        return [
            {
                "id": row["id"],
                "seq": row["seq"],
                "ts": row["ts"],
                "role": row.get("role"),
                "content": row.get("test_plaintext", ""),
                "voice_call_id": row.get("voice_call_id", ""),
                "voice_turn_id": row.get("voice_turn_id", ""),
            }
            for row in rows
            if row.get("role") == "user"
        ]

    def apply(user_id: str):
        def dispatch(effect_type, payload):
            if effect_type != "reply":
                return
            written.append(str(payload.get("text") or ""))
            if payload.get("reply_through_seq") is not None:
                db.patch_blob_strict(
                    user_id,
                    "model_api_runtime",
                    {"v2_reply_cursor_seq": int(payload["reply_through_seq"])},
                )

        return v2_effect_outbox.apply_pending_effects(user_id, dispatch=dispatch)

    return worker.TurnDeps(
        # web_search/web_fetch are gated per user now (default OFF); these
        # tests use them as a generic outbound read, so opt in explicitly.
        web_tools_enabled=lambda uid: True,
        read_messages=lambda _user_id: read_after_seq(uid, 0),
        read_messages_after_seq=read_after_seq,
        resolve_provider=lambda _user_id: (_BYOK, {}),
        mint_enclave_token=lambda _user_id: "rt",
        apply_pending_effects=apply,
    )


def test_single_round_plain_text_writes_exactly_one_bubble(monkeypatch):
    """Round 1: no tool_calls, plain text -> that text IS the final reply
    (Global Constraints)."""
    # Legacy include_reasoning path: with self-authored thinking OFF, an explicit
    # include_reasoning request flows through to the provider. (With it ON — the
    # shipped default — native reasoning is suppressed so the model emits a <think>
    # instead; see test_self_thinking_on_suppresses_native_reasoning below.)
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
    uid = "u_toolloop_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("hello from the model")])
    deps = _deps(messages=[{
        "id": "m1",
        "ts": 10.0,
        "role": "user",
        "content": "hi",
        "include_reasoning": True,
    }])

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    assert calls[0]["include_reasoning"] is True
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == "hello from the model"
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[0] == 1  # exactly one model call
    assert row[1] is False  # not failed
    assert row[2] == "ok"
    assert _job_status_row(job_id)[0] == "completed"


def test_chat_worldbook_matches_current_turn_without_rewriting_user_text(
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
    uid = "u_toolloop_worldbook"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-worldbook")
    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("The queen remembers you.")])
    observed: dict = {}

    def read_worldbook(user_id, messages, *, runtime_token):
        observed.update({
            "user_id": user_id,
            "messages": messages,
            "runtime_token": runtime_token,
        })
        return {
            "block": "<world_book>\nLuna is queen of the Moon Court.\n</world_book>",
            "matched_names": ["Moon Court"],
        }

    turn_messages = [{
        "id": "m-worldbook",
        "seq": 1,
        "ts": 10.0,
        "role": "user",
        "content": "Tell me about Luna",
    }]
    deps = _deps(messages=turn_messages)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    monkeypatch.setattr(worker.db, "chat_seqs_after_seq", lambda *_a, **_k: [1])
    deps.read_tail_after_seq = lambda *_a, **_k: list(turn_messages)
    deps.read_worldbook_context = read_worldbook

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert observed == {
        "user_id": uid,
        "messages": [{"role": "user", "content": "Tell me about Luna"}],
        "runtime_token": "rt",
    }
    provider_messages = calls[0]["messages"]
    worldbook_messages = [
        message
        for message in provider_messages
        if isinstance(message, dict)
        and str(message.get("content") or "").startswith(
            v2_context.WORLD_BOOK_CONTEXT_HEADER + "\n"
        )
    ]
    assert len(worldbook_messages) == 1
    assert worldbook_messages[0]["role"] == "user"
    assert "Luna is queen of the Moon Court." in worldbook_messages[0]["content"]
    assert [
        message
        for message in provider_messages
        if isinstance(message, dict)
        and message.get("content") == "Tell me about Luna"
    ] == [{"role": "user", "content": "Tell me about Luna"}]
    assert all(
        "Luna is queen" not in str(message.get("content") or "")
        for message in provider_messages
        if isinstance(message, dict) and message.get("role") == "system"
    )


def test_worldbook_truncation_reaches_provider_and_data_track_without_content(
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
    uid = "u_toolloop_worldbook_truncation"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-worldbook-truncation")
    _patch_real_write(monkeypatch)

    rare_secret = "T047_WORLDBOOK_SECRET_MUST_NOT_REACH_ADMIN"
    raw_worldbook = (
        "<world_book>\n"
        + ("W" * worldbook_match.CONTEXT_CHAR_CAP)
        + rare_secret
        + "\n</world_book>"
    )
    calls = _script_provider(monkeypatch, [_text_round("bounded")])
    traces = []
    turn_messages = [{
        "id": "m-worldbook-truncated",
        "ts": 10.0,
        "role": "user",
        "content": "Tell me about this setting",
    }]
    deps = _deps(messages=turn_messages)
    deps.read_summary = lambda _uid: ("", 0.0, 0)
    deps.read_tail = lambda _uid, _after_ts, _limit: list(turn_messages)
    deps.read_worldbook_context = lambda *_args, **_kwargs: {
        "block": raw_worldbook,
        "matched_names": [],
    }
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "type": event_type, **fields}
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    provider_payload = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert worldbook_match.TRUNCATION_MARKER.strip() in provider_payload
    truncation = next(
        trace for trace in traces if trace["type"] == "context.truncation"
    )
    assert truncation == {
        "user_id": uid,
        "type": "context.truncation",
        "status": "warning",
        "summary": "",
        "explain": "",
        "detail": {
            "counts": {
                "profile_cards_truncated": 0,
                "worldbook_truncated": 1,
            }
        },
    }
    raw_admin_material = json.dumps(
        admin_data_track._debug_event_public_json(truncation),
        ensure_ascii=False,
    )
    assert rare_secret not in provider_payload
    assert rare_secret not in raw_admin_material


def test_self_thinking_on_suppresses_native_reasoning(monkeypatch):
    """Self-authored thinking ON (shipped default): even an explicit
    include_reasoning request is suppressed at the provider, so a reasoning-capable
    model emits its thought in the reply's <think> instead of a raw native CoT. This
    is what aligns V2 with the V1 resident (see worker.py suppress_native_reasoning)."""
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)  # default = ON
    uid = "u_toolloop_selfthink_suppress"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-selfthink")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("hello from the model")])
    deps = _deps(messages=[{
        "id": "m1",
        "ts": 10.0,
        "role": "user",
        "content": "hi",
        "include_reasoning": True,
    }])

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    # The point: native reasoning was NOT requested despite include_reasoning=True.
    assert calls[0].get("include_reasoning") is not True
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[0]["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert self_thinking.INSTRUCTION in system_text


def test_fable_chat_omits_mandatory_self_thinking_prompt(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_toolloop_fable_plain_reply"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-fable-plain")
    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [_text_round("Fable plain reply")])
    deps = _deps(messages=[{
        "id": "m1",
        "ts": 10.0,
        "role": "user",
        "content": "hi",
    }])

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_FABLE_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[0]["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert self_thinking.INSTRUCTION not in system_text
    assert _bubbles(uid)[-1]["body_ct"] == "Fable plain reply"


def test_chat_thinking_only_keeps_existing_required_reply_fallback(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_toolloop_selfthink_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-selfthink-only")
    _stub_envelope_build(monkeypatch)
    _patch_real_write(monkeypatch)
    _script_provider(monkeypatch, [_text_round("<think>只想了但没回答</think>")])

    status = asyncio.run(
        worker.process_job(
            job,
            _deps(messages=[{
                "id": "m1", "ts": 10.0, "role": "user", "content": "在吗"
            }]),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == worker._DEGENERATE_REPLY_FALLBACK
    assert bubbles[0]["turn_failure_error_class"] == "upstream_unavailable"
    assert bubbles[0]["thinking_body_ct"] == "（思考没写完）"
    assert _job_status_row(job_id)[0] == "completed"


def test_degenerate_terminal_reply_becomes_attributed_fallback(monkeypatch):
    uid = "u_toolloop_degenerate_fallback"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-degenerate")
    _patch_real_write(monkeypatch)
    persisted_extra = {}
    real_write = worker._write_encrypted_reply

    def _capture_write(store, text, *, extra=None):
        persisted_extra.update(extra or {})
        return real_write(store, text, extra=extra)

    monkeypatch.setattr(worker, "_write_encrypted_reply", _capture_write)
    _script_provider(monkeypatch, [_text_round("。")])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "晚安呀"}]
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == worker._DEGENERATE_REPLY_FALLBACK
    assert persisted_extra["turn_failure_error_class"] == "upstream_unavailable"
    assert bubbles[0]["turn_failure_error_class"] == "upstream_unavailable"
    assert bubbles[0]["turn_failure_blame"] == "provider_transient"
    assert "模型服务暂时不可用" in bubbles[0]["turn_failure_user_text"]
    assert _job_status_row(job_id)[0] == "completed"


def test_degenerate_intermediate_is_dropped_before_real_final_reply(monkeypatch):
    uid = "u_toolloop_degenerate_intermediate"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-degenerate-mixed")
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda action_type, store, **kwargs: _FakeCapResult({"snippet": "result"}),
    )
    _patch_real_write(monkeypatch)
    _script_provider(
        monkeypatch,
        [
            _tool_round(
                _tc("r1", "reply", text="."),
                _tc("s1", "web_search", query="x"),
            ),
            _text_round("在的，怎么了"),
        ],
    )
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "在吗"}]
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert [bubble["body_ct"] for bubble in _bubbles(uid)] == ["在的，怎么了"]


# ---------------------------------------------------------------------------
# Torn protocol-JSON leak (B3): a stream-cut relay splits one protocol envelope
# across the reasoning/content channels. The head lands in `reasoning`, the tail
# in `reply`. Strong cross-channel evidence must never reach a chat bubble.
# ---------------------------------------------------------------------------

# One envelope torn at the channel boundary.
_TORN_HEAD = '{"messages":[],"actions":[{"type":"pro'
_TORN_TAIL = 'active.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}'
_UPSTREAM_RESPONSE_ENVELOPE = json.dumps(
    {
        "response": {
            "candidates": [{"content": {}}],
            "usageMetadata": {"totalTokenCount": 24515},
            "modelVersion": "gemini-3-flash",
            "responseId": "response-id",
        },
        "traceId": "trace-id",
        "metadata": {},
    }
)


def test_torn_protocol_tail_with_reasoning_head_becomes_fallback(monkeypatch):
    """Chat: reply=tail + reasoning=head is STRONG cross-channel evidence. The
    bubble must be the honest fallback, not the leaked JSON tail, and the torn
    head must NOT surface as a thinking bubble."""
    uid = "u_toolloop_torn_fallback"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-torn")
    _patch_real_write(monkeypatch)
    _script_provider(
        monkeypatch,
        [{
            "reply": _TORN_TAIL,
            "reasoning": _TORN_HEAD,
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }],
    )
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "在吗"}])

    status = asyncio.run(
        worker.process_job(job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt")
    )

    assert status == "completed"
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == worker._DEGENERATE_REPLY_FALLBACK
    assert _TORN_TAIL not in bubbles[0]["body_ct"]
    assert bubbles[0]["turn_failure_error_class"] == "upstream_unavailable"
    # Reasoning head must not ride along as a thinking bubble.
    assert not bubbles[0].get("thinking_body_ct")


def test_foreground_keeps_user_json_talk_without_reasoning(monkeypatch):
    """Chat: a bracket-heavy message with NO reasoning head is WEAK evidence —
    it could be the user discussing code/JSON. Foreground must deliver it, never
    eat a real message on the bracket heuristic alone."""
    uid = "u_toolloop_user_json"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-userjson")
    _patch_real_write(monkeypatch)
    user_json_talk = '删掉多余的 }，把 "port": 8080 改成 8081'
    _script_provider(monkeypatch, [_text_round(user_json_talk)])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "配置怎么改"}])

    status = asyncio.run(
        worker.process_job(job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt")
    )

    assert status == "completed"
    assert [b["body_ct"] for b in _bubbles(uid)] == [user_json_talk]


def test_foreground_real_chain_strips_tool_markup_and_emits_content_free_trace(
    monkeypatch,
):
    """Production chain: provider text -> worker._on_reply -> durable bubble."""
    uid = "u_toolloop_tool_markup_leak"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-tool-markup")
    _patch_real_write(monkeypatch)
    leaked = (
        '<parameter name="tool_name">reply</parameter>\n'
        "好，棋先停着\n你要干嘛去了"
    )
    _script_provider(monkeypatch, [_text_round(leaked)])
    traces = []
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "先停一下"}]
    )
    deps.emit_debug_trace = lambda *args, **kwargs: traces.append((args, kwargs))

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert [bubble["body_ct"] for bubble in _bubbles(uid)] == [
        "好，棋先停着\n你要干嘛去了"
    ]
    sanitized = [item for item in traces if item[0][1] == "agent.reply.sanitized"]
    assert len(sanitized) == 1
    assert sanitized[0][1]["detail"] == {
        "lane": "chat",
        "final": True,
        "error_class": "upstream_unavailable",
        "reason": "tool_markup_leak_sanitized",
    }


def test_foreground_markup_only_reply_uses_existing_fallback(monkeypatch):
    uid = "u_toolloop_tool_markup_only"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-tool-markup-only")
    _patch_real_write(monkeypatch)
    _script_provider(monkeypatch, [_text_round("<tool_call></tool_call>")])

    status = asyncio.run(
        worker.process_job(
            job,
            _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "在吗"}]),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    bubble = _bubbles(uid)[0]
    assert bubble["body_ct"] == worker._DEGENERATE_REPLY_FALLBACK
    assert bubble["turn_failure_error_class"] == "upstream_unavailable"


def test_foreground_wrapped_reply_payload_is_not_lost(monkeypatch):
    uid = "u_toolloop_wrapped_reply_payload"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-wrapped-reply")
    _patch_real_write(monkeypatch)
    _script_provider(
        monkeypatch,
        [
            _text_round(
                '<antml:invoke name="reply"><antml:parameter name="text">'
                "真正的回复内容"
                "</antml:parameter></antml:invoke>"
            )
        ],
    )

    status = asyncio.run(
        worker.process_job(
            job,
            _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "继续"}]),
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert [bubble["body_ct"] for bubble in _bubbles(uid)] == ["真正的回复内容"]


def test_chat_intermediate_reply_tool_tail_with_reasoning_suppressed(monkeypatch):
    """Codex code-review #1: an intermediate `reply` tool call carrying a torn
    tail, with the head in the round's reasoning, must not produce a leaked
    bubble. The reasoning is now passed to the intermediate sink so the chat lane
    sees STRONG evidence; the following real terminal reply still lands."""
    uid = "u_toolloop_torn_intermediate"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-torn-mid")
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda action_type, store, **kwargs: _FakeCapResult({"snippet": "r"}),
    )
    # The round carries reasoning (the torn head), which the worker surfaces via
    # the envelope path — stub it (as the reasoning-surfacing test does) so the
    # sealed bodies are readable and no unwired-assembly error masks the check.
    _stub_envelope_build(monkeypatch)
    # Same structure as test_degenerate_intermediate_is_dropped_before_real_final_
    # reply: a real tool call (web_search) rides alongside the suppressed reply so
    # the loop continues to the terminal reply, isolating the suppression itself.
    _script_provider(
        monkeypatch,
        [
            {
                "reply": "",
                "tool_calls": [
                    _tc("r1", "reply", text=_TORN_TAIL),
                    _tc("s1", "web_search", query="x"),
                ],
                "reasoning": _TORN_HEAD,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            _text_round("在的，怎么了"),
        ],
    )
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "在吗"}])

    status = asyncio.run(
        worker.process_job(job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt")
    )

    assert status == "completed"
    bubbles = [b["body_ct"] for b in _bubbles(uid)]
    # THE invariant: the torn tail never reaches a bubble; the real terminal does.
    assert _TORN_TAIL not in bubbles
    assert bubbles == ["在的，怎么了"]


def test_chat_intermediate_reply_tool_upstream_envelope_is_suppressed(monkeypatch):
    """The final-effect guard also covers a relay wrapper inside reply(text=...)."""
    uid = "u_toolloop_upstream_envelope_intermediate"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-upstream-envelope-mid")
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda action_type, store, **kwargs: _FakeCapResult({"snippet": "r"}),
    )
    _patch_real_write(monkeypatch)
    _script_provider(
        monkeypatch,
        [
            _tool_round(
                _tc("r1", "reply", text=_UPSTREAM_RESPONSE_ENVELOPE),
                _tc("s1", "web_search", query="x"),
            ),
            _text_round("在的，怎么了"),
        ],
    )
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "在吗"}]
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    bubbles = [bubble["body_ct"] for bubble in _bubbles(uid)]
    assert _UPSTREAM_RESPONSE_ENVELOPE not in bubbles
    assert bubbles == ["在的，怎么了"]


def test_torn_protocol_evidence_lane_policy():
    """Pure-unit: the worker's lane-policy helper. Proactive suppresses any leak;
    foreground only strong cross-channel evidence."""
    # Weak orphan tail, no reasoning.
    assert worker._torn_protocol_evidence(_TORN_TAIL, "", lane="proactive")
    assert not worker._torn_protocol_evidence(_TORN_TAIL, "", lane="foreground")
    # Strong: head in reasoning rejoins to a complete envelope.
    assert worker._torn_protocol_evidence(_TORN_TAIL, _TORN_HEAD, lane="proactive")
    assert worker._torn_protocol_evidence(_TORN_TAIL, _TORN_HEAD, lane="foreground")
    # A complete provider transport wrapper is strong without reasoning evidence.
    assert worker._torn_protocol_evidence(
        _UPSTREAM_RESPONSE_ENVELOPE, "", lane="proactive"
    )
    assert worker._torn_protocol_evidence(
        _UPSTREAM_RESPONSE_ENVELOPE, "", lane="foreground"
    )
    # Normal reply: never suppressed.
    assert not worker._torn_protocol_evidence("晚安，做个好梦", "在想她累不累", lane="proactive")
    assert not worker._torn_protocol_evidence("晚安，做个好梦", "", lane="foreground")


def _stub_envelope_build(monkeypatch):
    """Deterministic stand-in for the enclave envelope round-trip so a test can
    read the sealed plaintext straight off the row's ``*_body_ct``. Applies to
    BOTH the reply body and the separately-sealed thinking body."""

    def _fake(store, plaintext, item_id=None):
        return (
            {
                "v": 1,
                "id": item_id or "eid",
                "body_ct": plaintext.decode("utf-8"),
                "nonce": "n",
                "K_user": "k",
            },
            None,
        )

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake)


def test_self_thinking_off_preserves_native_reasoning_bubble(monkeypatch):
    """Feature OFF preserves the legacy provider chain-of-thought contract.

    A final reply whose provider result carried chain-of-thought
    (``result["reasoning"]``) must publish it as the row's separately-sealed
    thinking envelope (``thinking_body_ct`` + ``thinking_kind``).
    """
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
    uid = "u_toolloop_reasoning"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _stub_envelope_build(monkeypatch)
    calls = _script_provider(
        monkeypatch,
        [
            {
                "reply": "the answer",
                "reasoning": "step one\nstep two",
                "tool_calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ],
    )
    traces = []
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "event_type": event_type, **fields}
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    bubble = bubbles[0]
    assert bubble["body_ct"] == "the answer"
    assert bubble.get("thinking_kind") == "provider_reasoning"
    assert bubble.get("thinking_body_ct") == "step one\nstep two"
    thinking_traces = [
        trace for trace in traces if trace["event_type"] == "thinking.surfaced"
    ]
    assert [trace["detail"] for trace in thinking_traces] == [{
        "branch": "native_legacy",
        "chars": len("step one\nstep two"),
        "model": _BYOK.model,
        "lane": "chat",
    }]


def test_self_thinking_on_drops_native_reasoning_without_authored_block(
    monkeypatch,
):
    """Provider-native CoT is never a fallback while self-thinking is ON."""
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_toolloop_selfthink_no_fallback"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-selfthink-no-fallback")

    _stub_envelope_build(monkeypatch)
    _script_provider(
        monkeypatch,
        [{
            "reply": "the answer",
            "reasoning": "private native cot",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }],
    )
    traces = []
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "event_type": event_type, **fields}
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    bubble = _bubbles(uid)[0]
    assert bubble["body_ct"] == "the answer"
    assert "thinking_kind" not in bubble
    assert "thinking_body_ct" not in bubble
    thinking_traces = [
        trace for trace in traces if trace["event_type"] == "thinking.surfaced"
    ]
    assert [trace["detail"] for trace in thinking_traces] == [{
        "branch": "none",
        "chars": 0,
        "model": _BYOK.model,
        "lane": "chat",
    }]


def test_self_thinking_on_prefers_authored_block_over_native_reasoning(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_toolloop_selfthink_authored"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-selfthink-authored")

    _stub_envelope_build(monkeypatch)
    _script_provider(
        monkeypatch,
        [{
            "reply": "<think>我先自己归纳</think>the answer",
            "reasoning": "private native cot",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }],
    )
    traces = []
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "event_type": event_type, **fields}
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    bubble = _bubbles(uid)[0]
    assert bubble["body_ct"] == "the answer"
    assert bubble["thinking_kind"] == "agent_summary"
    assert bubble["thinking_body_ct"] == "我先自己归纳"
    thinking_traces = [
        trace for trace in traces if trace["event_type"] == "thinking.surfaced"
    ]
    assert [trace["detail"] for trace in thinking_traces] == [{
        "branch": "self",
        "chars": len("我先自己归纳"),
        "model": _BYOK.model,
        "lane": "chat",
    }]


def test_reasoning_absent_leaves_no_thinking_fields(monkeypatch):
    """A reply with no provider reasoning must NOT invent thinking metadata."""
    uid = "u_toolloop_no_reasoning"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _stub_envelope_build(monkeypatch)
    _script_provider(monkeypatch, [_text_round("plain answer")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    bubble = _bubbles(uid)[0]
    assert bubble["body_ct"] == "plain answer"
    assert "thinking_kind" not in bubble
    assert "thinking_body_ct" not in bubble


def test_chat_mixed_valid_invalid_workspace_batch_applies_valid_call(
    monkeypatch,
):
    uid = "u_toolloop_mixed_workspace_batch"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-mixed-workspace")
    _patch_real_write(monkeypatch)
    _patch_tool_effect_encryption(monkeypatch)
    captured = {}

    async def direct_loop(**kwargs):
        results = await kwargs["dispatch_tools"](
            [
                ToolCall(
                    id="valid",
                    name="workspace_write",
                    args={
                        "path": "/workspace/valid.md",
                        "content": "kept",
                        "expected_revision": 0,
                    },
                ),
                ToolCall(
                    id="invalid",
                    name="workspace_write",
                    args={},
                    args_ok=False,
                ),
            ]
        )
        captured["results"] = results
        await kwargs["on_reply"]("done", final=True)
        return worker.v2_tool_loop.LoopOutcome(
            final_text="done",
            rounds=1,
            stop_reason="final_text",
            replied_intermediate=False,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    deps = _deps(
        messages=[
            {"id": "m1", "ts": 10.0, "role": "user", "content": "edit"},
        ]
    )
    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert [result.call_id for result in captured["results"]] == [
        "valid",
        "invalid",
    ]
    assert captured["results"][0].content == "ok: workspace_write applied"
    assert captured["results"][1].content.startswith("error: unparseable args")


def test_chat_photo_read_observation_reaches_next_model_round_without_base64(
    monkeypatch,
):
    uid = "u_toolloop_photo_observation"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-photo")
    _patch_real_write(monkeypatch)
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda *_a, **_k: _FakeCapResult({
            "photo_id": "p1",
            "has_image": True,
            "image_media_type": "image/jpeg",
            "image_b64": "cGl4ZWxz",
        }),
    )
    observed = []

    def _observe_photo(user_id, **kwargs):
        observed.append((user_id, kwargs))
        return "a red bicycle beside a wall"

    calls = _script_provider(monkeypatch, [
        _tool_round(_tc(
            "photo-1", "photo_read", photo_id="p1", include_image=True
        )),
        _text_round("I noticed the bicycle."),
    ])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        observe_photo=_observe_photo,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
    ))

    assert status == "completed"
    assert len(observed) == 1
    assert observed[0][0] == uid
    assert observed[0][1]["image_mime"] == "image/jpeg"
    assert observed[0][1]["image_b64"] == "cGl4ZWxz"
    assert observed[0][1]["main_provider_config"] is _BYOK
    results = _tool_result_contents(calls[1])
    assert any("a red bicycle beside a wall" in item for item in results)
    assert all("cGl4ZWxz" not in item and "image_b64" not in item for item in results)


def test_voice_turn_publishes_all_applied_bubbles_once_in_order(monkeypatch):
    uid = "u_toolloop_voice_multi_bubble"
    conftest.seed_user(uid)
    _reset(uid)
    user_doc = _user_doc("voice-user-1", "给我分两条回答")
    user_doc.update(
        voice_call_id="voice-call-1",
        voice_turn_id="voice-turn-1",
    )
    db.chat_append_strict(uid, "voice-user-1", 10.0, user_doc, 5_000)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-voice-multi")
    _patch_tool_effect_encryption(monkeypatch)
    published: list[tuple[str, dict]] = []

    async def direct_loop(**kwargs):
        await kwargs["on_reply"]("这是第一条。", final=False)
        await kwargs["on_reply"]("这是第二条。", final=True)
        return worker.v2_tool_loop.LoopOutcome(
            final_text="这是第二条。",
            rounds=1,
            stop_reason="final_text",
            replied_intermediate=True,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    base_deps = _late_input_deps(uid, [])
    deps = worker.TurnDeps(
        read_messages=base_deps.read_messages,
        read_messages_after_seq=base_deps.read_messages_after_seq,
        resolve_provider=base_deps.resolve_provider,
        mint_enclave_token=base_deps.mint_enclave_token,
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        web_tools_enabled=base_deps.web_tools_enabled,
        publish_voice_reply=lambda user_id, **kwargs: published.append(
            (user_id, kwargs)
        ),
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    bubbles = _bubbles(uid)
    assert [
        base64.b64decode(row["body_ct"]).decode("utf-8") for row in bubbles
    ] == ["这是第一条。", "这是第二条。"]
    assert all(row["voice_call_id"] == "voice-call-1" for row in bubbles)
    assert all(row["voice_turn_id"] == "voice-turn-1" for row in bubbles)
    assert bubbles[-1]["reply_to_message_id"] == "voice-user-1"
    assert len(published) == 1
    published_user, published_turn = published[0]
    assert published_user == uid
    assert published_turn["call_id"] == "voice-call-1"
    assert published_turn["turn_id"] == "voice-turn-1"
    assert published_turn["message_id"]
    assert published_turn["text"] == "这是第一条。\n\n这是第二条。"


def test_chat_workspace_prompt_snapshot_is_loaded_once_across_rounds(
    monkeypatch,
):
    uid = "u_toolloop_workspace_prompt"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-workspace-prompt")

    _patch_real_write(monkeypatch)
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: _FakeCapResult({"items": []}),
    )
    calls = _script_provider(
        monkeypatch,
        [
            _tool_round(_tc("read", "memory_index")),
            _text_round("workspace-aware reply"),
        ],
    )
    loader_calls = []
    deps = _deps(
        messages=[
            {"id": "m1", "ts": 10.0, "role": "user", "content": "hi"},
        ]
    )
    deps.load_workspace_prompt = lambda _store, **kwargs: (
        loader_calls.append(kwargs["runtime_token"])
        or {
            "trusted_system_blocks": (
                "<feedling-skill>trusted skill</feedling-skill>",
            ),
            "working_memory": "DO_NOT_EAGERLY_INJECT",
        }
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert loader_calls == ["rt"]
    assert len(calls) == 2
    for call in calls:
        prompt = str(call["messages"])
        assert "trusted skill" in prompt
        assert "DO_NOT_EAGERLY_INJECT" not in prompt
        assert "/memory/WORKING.md" in prompt
    system = next(
        message for message in calls[0]["messages"] if message["role"] == "system"
    )
    assert "trusted skill" in str(system["content"])
    assert not any(
        worker.context.WORKING_MEMORY_HEADER in str(message.get("content"))
        for message in calls[0]["messages"]
    )
    second_offered = {spec.name for spec in calls[1]["tools"]}
    assert {"web_search", "web_fetch", "task"}.isdisjoint(second_offered)


def test_chat_workspace_prompt_failure_is_visible_before_provider(
    monkeypatch,
):
    uid = "u_toolloop_workspace_prompt_failure"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-workspace-prompt-failure")
    deps = _deps(
        messages=[
            {"id": "m1", "ts": 10.0, "role": "user", "content": "hi"},
        ]
    )
    deps.load_workspace_prompt = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("private workspace plaintext")
    )
    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        lambda *_args, **_kwargs: pytest.fail(
            "provider called after workspace prompt failure"
        ),
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "failed"
    assert _job_status_row(job_id)[:2] == (
        "failed",
        "turn_failed:workspace_prompt_unavailable",
    )


def test_explicit_openrouter_image_rejection_writes_terminal_history_guidance(
    monkeypatch,
):
    """Production-shaped seq-native failure: exact provider rejection reaches
    the durable terminal outbox and becomes a decryptable, parent-linked Chat
    bubble rather than stopping at the status stream."""
    uid = "u_toolloop_openrouter_image_unsupported"
    conftest.seed_user(uid, archive_language="en-US")
    _reset(uid)
    generation = db.get_runtime_generation(uid)
    input_doc = {
        **_user_doc("image-parent", "What is in this image?"),
        "content_type": "image",
        "image_mime": "image/png",
    }
    input_seq, job_id = db.chat_append_and_enqueue(
        uid,
        "image-parent",
        10.0,
        input_doc,
        5000,
        "chat",
        expected_generation=generation,
    )
    job = jobs_store.claim_next_job("w-openrouter-image-unsupported")
    assert job is not None and job["id"] == job_id
    _patch_tool_effect_encryption(monkeypatch)

    def _read_after_seq(_user_id: str, after_seq: int):
        return [
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "ts": float(row.get("ts") or 0),
                "role": row.get("role"),
                "content": row.get("test_plaintext") or "",
                "has_image": row.get("content_type") == "image",
                "image_mime": row.get("image_mime") or "",
            }
            for row in db.chat_messages_after_seq(uid, after_seq, limit=None)
            if row.get("role") == "user"
        ]

    def _read_tail_after_seq(
        _user_id: str,
        after_seq: int,
        limit: int,
        *,
        through_seq: int | None = None,
    ):
        return _read_after_seq(_user_id, after_seq)

    async def _reject_image(_config, _messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError(
            "provider_http_404: No endpoints found that support image input",
            status_code=404,
        )

    monkeypatch.setattr(
        provider_client, "chat_completion_async", _reject_image
    )
    deps = worker.TurnDeps(
        read_messages=lambda _user_id: _read_after_seq(uid, 0),
        read_messages_after_seq=_read_after_seq,
        read_tail_after_seq=_read_tail_after_seq,
        read_images=lambda _user_id, message_ids: {
            message_id: {
                "image_b64": "iVBORw0KGgo=",
                "image_mime": "image/png",
            }
            for message_id in message_ids
        },
        resolve_provider=lambda _user_id: (_BYOK, {}),
        mint_enclave_token=lambda _user_id: "rt",
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "failed"
    failures = [
        row for row in db.chat_load_strict(uid)
        if str(row.get("terminal_failure_job_id") or "") == str(job_id)
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["role"] == "openclaw"
    assert failure["reply_to_message_id"] == "image-parent"
    assert failure["turn_failure_error_class"] == "vision_model_required"
    assert base64.b64decode(failure["body_ct"]).decode() == (
        "Your current model can't process images, so it didn't receive this "
        "picture. Switch models, or add a dedicated vision model in Settings."
    )
    assert v2_cursor.load_seq(core_store.get_store(uid)) == input_seq


def test_chat_native_task_runs_child_then_returns_result_to_parent(
    monkeypatch,
):
    uid = "u_toolloop_native_task"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-native-task")
    _patch_real_write(monkeypatch)
    responses = iter(
        [
            _tool_round(
                _tc(
                    "task-1",
                    "task",
                    prompt="Inspect the report independently.",
                )
            ),
            _text_round("child evidence"),
            _text_round("parent answer using child evidence"),
        ]
    )
    calls = []

    async def provider(config, messages, *, tools=None, **_kwargs):
        calls.append(
            {
                "config": config,
                "messages": messages,
                "tools": tools,
            }
        )
        return next(responses)

    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        provider,
    )
    deps = _deps(
        messages=[
            {
                "id": "m1",
                "ts": 10.0,
                "role": "user",
                "content": "Please inspect the report.",
            },
        ]
    )

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(calls) == 3
    assert calls[0]["config"] is _BYOK
    assert calls[2]["config"] is _BYOK
    child_config = calls[1]["config"]
    assert child_config is not _BYOK
    assert child_config.provider == _BYOK.provider
    assert child_config.model == _BYOK.model
    assert child_config.api_key == _BYOK.api_key
    assert child_config.base_url == _BYOK.base_url
    assert child_config.context_window_tokens == 32_768
    parent_tools = {spec.name for spec in calls[0]["tools"]}
    child_tools = {spec.name for spec in calls[1]["tools"]}
    assert "task" in parent_tools
    assert child_tools == worker._SUBAGENT_ALLOWED_TOOLS
    assert "Inspect the report independently." in str(calls[1]["messages"])
    assert "Please inspect the report." not in str(calls[1]["messages"])
    assert "child evidence" in str(calls[2]["messages"])
    assert _turn_metric_row(job_id)[0] == 3


def test_user_input_during_final_provider_call_is_folded_before_visible_reply(
    monkeypatch,
):
    uid = "u_toolloop_late_final_fold"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind='model_api_runtime'",
            (uid,),
        )
    db.chat_append_strict(uid, "A", 10.0, _user_doc("A", "first A"), 5000)
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    job = jobs_store.claim_next_job("w-late-final")

    # Keep the test focused on the outbox fence rather than enclave crypto: the
    # production builder also returns a dict whose content is encrypted and to
    # which worker adds the same non-sensitive fence metadata.
    monkeypatch.setattr(
        worker,
        "_build_encrypted_reply_effect_payload",
        lambda _store, text, *, effect_id, reply_through_seq=None: {
            "text": text,
            "reply_through_seq": reply_through_seq,
        },
    )
    monkeypatch.setattr(
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({})
    )
    written: list[str] = []
    deps = _late_input_deps(uid, written)
    calls = []

    async def provider(_config, messages, *, tools=None, **_kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            # This is the production send invariant: B and the running job's
            # generation bump commit in the same transaction.
            seq, same_job_id = db.chat_append_and_enqueue(
                uid,
                "B",
                20.0,
                _user_doc("B", "late B"),
                5000,
                "chat",
                expected_generation=generation,
            )
            assert seq > 0 and same_job_id == job_id
            return _text_round("stale A-only final")
        assert any(
            isinstance(message, dict) and message.get("content") == "late B"
            for message in messages
        )
        return _text_round("fresh A+B final")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(calls) == 2
    assert written == ["fresh A+B final"]
    assert _job_status_row(job_id)[0] == "completed"
    with db.get_pool().connection() as conn:
        successors = conn.execute(
            "SELECT COUNT(*) FROM agent_jobs WHERE user_id=%s AND id<>%s",
            (uid, job_id),
        ).fetchone()[0]
        effects = conn.execute(
            "SELECT status,last_error FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s ORDER BY enqueue_seq",
            (uid, v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE),
        ).fetchall()
    assert successors == 0
    assert effects == [
        ("discarded", "input_generation_advanced"),
        ("applied", ""),
    ]
    assert v2_cursor.load_seq(core_store.get_store(uid)) == db.chat_seq_for_msg_id(
        uid, "B"
    )


def test_ordered_chat_replies_settle_each_user_message_separately(monkeypatch):
    uid = "u_toolloop_ordered_replies"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind='model_api_runtime'",
            (uid,),
        )
    db.chat_append_strict(uid, "A", 10.0, _user_doc("A", "first A"), 5000)
    seq_a = db.chat_seq_for_msg_id(uid, "A")
    assert seq_a is not None
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    job = jobs_store.claim_next_job("w-ordered-replies")

    monkeypatch.setattr(
        worker,
        "_build_encrypted_reply_effect_payload",
        lambda _store, text, *, effect_id, reply_through_seq=None: {
            "text": text,
            "reply_through_seq": reply_through_seq,
        },
    )
    monkeypatch.setattr(
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({})
    )
    written: list[str] = []
    deps = _late_input_deps(uid, written)
    deps.ordered_chat_replies = True
    deps.read_tail_after_seq = lambda *_a, **_k: [
        {"id": "A", "seq": seq_a, "ts": 10.0, "role": "user", "content": "first A"}
    ]
    calls = []

    async def provider(_config, messages, *, tools=None, **_kwargs):
        calls.append(list(messages))
        assert worker.context.ORDERED_REPLY_TARGET_POLICY in messages[0]["content"]
        assert any(
            isinstance(message, dict) and message.get("content") == "first A"
            for message in messages
        )
        assert not any(
            isinstance(message, dict) and message.get("content") == "late B"
            for message in messages
        )
        seq, same_job_id = db.chat_append_and_enqueue(
            uid,
            "B",
            20.0,
            _user_doc("B", "late B"),
            5000,
            "chat",
            expected_generation=generation,
        )
        assert seq > 0 and same_job_id == job_id
        return _text_round("answer A")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    assert written == ["answer A"]
    assert v2_cursor.load_seq(core_store.get_store(uid)) == db.chat_seq_for_msg_id(
        uid, "A"
    )
    with db.get_pool().connection() as conn:
        successor = conn.execute(
            "SELECT status,reason FROM agent_jobs "
            "WHERE user_id=%s AND id<>%s ORDER BY id DESC LIMIT 1",
            (uid, job_id),
        ).fetchone()
        reply_payload = conn.execute(
            "SELECT payload FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s ORDER BY enqueue_seq DESC LIMIT 1",
            (uid, v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE),
        ).fetchone()[0]
    assert successor == ("pending", "ordered_followup")
    assert reply_payload["reply_to_message_id"] == "A"


def test_new_turn_after_intermediate_failure_starts_after_failure_cursor(
    monkeypatch,
):
    """An assistant-only prompt snapshot advance cannot poison the final fence.

    The first worker publishes an intermediate reply, then crashes. The retry's
    all-role tail includes that bubble after the newest user row, while the
    compound final reply must still fence and advance only through the user seq.
    """
    uid = "u_toolloop_intermediate_crash_retry_cursor"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs "
            "WHERE user_id=%s AND kind='model_api_runtime'",
            (uid,),
        )

    generation = db.get_runtime_generation(uid)
    user_seq, first_job_id = db.chat_append_and_enqueue(
        uid,
        "user-before-crash",
        10.0,
        _user_doc("user-before-crash", "please investigate"),
        5000,
        "chat",
        expected_generation=generation,
    )
    first_job = jobs_store.claim_next_job("w-intermediate-crash")
    assert first_job is not None and first_job["id"] == first_job_id

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, plaintext, *, item_id=None: (
            {
                "v": 1,
                "id": str(item_id),
                "owner_user_id": store.user_id,
                "visibility": "shared",
                "body_ct": bytes(plaintext).hex(),
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        ),
    )
    monkeypatch.setattr(
        worker,
        "_perception_glance_grounding_results",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=(None, None)),
    )

    def _plaintext(row: dict) -> str:
        if row.get("test_plaintext") is not None:
            return str(row["test_plaintext"])
        try:
            return bytes.fromhex(str(row.get("body_ct") or "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""

    def _render(rows: list[dict], *, user_only: bool) -> list[dict]:
        rendered = []
        for row in rows:
            role = str(row.get("role") or "")
            if user_only and role not in {"user", "human"}:
                continue
            rendered.append(
                {
                    "id": row["id"],
                    "seq": int(row["seq"]),
                    "ts": float(row.get("ts") or 0.0),
                    "role": (
                        "user" if role in {"user", "human"} else "assistant"
                    ),
                    "content": _plaintext(row),
                }
            )
        return rendered

    def _read_after_seq(_user_id: str, after_seq: int):
        return _render(
            db.chat_messages_after_seq(uid, after_seq, limit=None),
            user_only=True,
        )

    def _read_tail_after_seq(
        _user_id: str,
        after_seq: int,
        limit: int,
        *,
        through_seq: int | None = None,
    ):
        return _render(
            db.chat_messages_after_seq(
                uid,
                after_seq,
                limit=limit,
                oldest_first=False,
                through_seq=through_seq,
            ),
            user_only=False,
        )

    deps = worker.TurnDeps(
        # web_search/web_fetch are gated per user now (default OFF); these
        # tests use them as a generic outbound read, so opt in explicitly.
        web_tools_enabled=lambda uid: True,
        read_messages=lambda _user_id: _read_after_seq(uid, 0),
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _user_id: (_BYOK, {}),
        mint_enclave_token=lambda _user_id: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        read_tail_after_seq=_read_tail_after_seq,
    )

    phase = "crash"
    first_attempt_calls = 0
    retry_messages = []

    async def provider(_config, messages, *, tools=None, **_kwargs):
        nonlocal first_attempt_calls
        if phase == "crash":
            first_attempt_calls += 1
            if first_attempt_calls == 1:
                return _tool_round(
                    _tc("checking", "reply", text="I am still checking."),
                )
            raise RuntimeError("injected worker crash after intermediate reply")
        retry_messages.append(list(messages))
        return _text_round("final answer after retry")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    first_status = asyncio.run(
        worker.process_job(
            first_job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )
    assert first_status == "failed"
    assert v2_cursor.load_seq(core_store.get_store(uid)) == user_seq
    with db.get_pool().connection() as conn:
        assistant_rows = conn.execute(
            "SELECT MAX(seq) FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()[0]
        failure_rows = conn.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE user_id=%s AND COALESCE(doc->>'terminal_failure_job_id','')=%s",
            (uid, str(first_job_id)),
        ).fetchone()[0]
    assert int(assistant_rows) > user_seq
    assert failure_rows == 1

    phase = "retry"
    new_user_seq, retry_job_id = db.chat_append_and_enqueue(
        uid,
        "user-after-failure",
        20.0,
        _user_doc("user-after-failure", "please try once more"),
        5000,
        "chat",
        expected_generation=generation,
    )
    retry_job = jobs_store.claim_next_job("w-intermediate-retry")
    assert retry_job is not None and retry_job["id"] == retry_job_id

    retry_status = asyncio.run(
        worker.process_job(
            retry_job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert retry_status == "completed"
    assert len(retry_messages) == 1
    assert "I am still checking." in str(retry_messages[0])
    assert "please try once more" in str(retry_messages[0])
    assert v2_cursor.load_seq(core_store.get_store(uid)) == new_user_seq
    assert _job_status_row(retry_job_id) == ("completed", None)
    with db.get_pool().connection() as conn:
        final_effect = conn.execute(
            "SELECT status,last_error,(payload->>'reply_through_seq')::bigint "
            "FROM v2_effect_outbox "
            "WHERE user_id=%s AND job_id=%s AND effect_type=%s",
            (
                uid,
                retry_job_id,
                v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE,
            ),
        ).fetchone()
        final_seq = conn.execute(
            "SELECT MAX(seq) FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()[0]
    assert final_effect == ("applied", "", new_user_seq)
    assert int(final_seq) > int(assistant_rows)


@pytest.mark.parametrize(
    "failed_post_commit_step", ["done_status", "chat_notify", "metric"]
)
def test_committed_final_reply_survives_post_commit_bookkeeping_failures(
    monkeypatch,
    failed_post_commit_step,
):
    """Auxiliary failures cannot rewrite an atomic reply as a failed child.

    The production reply sink commits the encrypted bubble, reply cursor,
    source-job completion, effect disposition, and PG NOTIFY together.  The
    status stream, redundant process-level wake, and metric upsert happen only
    after that transaction and must therefore be best-effort.
    """
    uid = f"u_toolloop_post_commit_{failed_post_commit_step}"
    conftest.seed_user(uid)
    _reset(uid)
    input_seq, job_id = db.chat_append_and_enqueue(
        uid,
        "A",
        10.0,
        _user_doc("A", "answer me"),
        5000,
        "chat",
        expected_generation=db.get_runtime_generation(uid),
    )
    job = jobs_store.claim_next_job(f"w-{failed_post_commit_step}")
    assert job is not None and job["id"] == job_id

    def read_after_seq(_user_id: str, after_seq: int):
        return [
            {
                "id": row["id"],
                "seq": row["seq"],
                "ts": row["ts"],
                "role": row.get("role"),
                "content": row.get("test_plaintext", ""),
            }
            for row in db.chat_messages_after_seq(uid, after_seq, limit=None)
            if row.get("role") == "user"
        ]

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, plaintext, *, item_id=None: (
            {
                "v": 1,
                "id": str(item_id),
                "owner_user_id": store.user_id,
                "visibility": "shared",
                "body_ct": plaintext.hex(),
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        ),
    )
    monkeypatch.setattr(
        worker,
        "_perception_glance_grounding_results",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=(None, None)),
    )
    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=_text_round("durable final")),
    )
    surfaced: list[str] = []
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda *_args, **_kwargs: surfaced.append(str(_args[-1])),
    )

    if failed_post_commit_step == "done_status":
        original_emit_status = worker._emit_status

        def fail_done_status(user_id, source_job_id, kind):
            if kind == "done":
                raise RuntimeError("injected done status failure")
            return original_emit_status(user_id, source_job_id, kind)

        monkeypatch.setattr(worker, "_emit_status", fail_done_status)
    elif failed_post_commit_step == "chat_notify":
        original_notify = worker.core_wake_bus.notify

        def fail_chat_notify(channel, user_id=""):
            if channel == "chat":
                raise RuntimeError("injected chat notify failure")
            return original_notify(channel, user_id)

        monkeypatch.setattr(worker.core_wake_bus, "notify", fail_chat_notify)
    else:
        monkeypatch.setattr(
            jobs_store,
            "record_whole_turn_metric",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected metric failure")
            ),
        )

    deps = worker.TurnDeps(
        # web_search/web_fetch are gated per user now (default OFF); these
        # tests use them as a generic outbound read, so opt in explicitly.
        web_tools_enabled=lambda uid: True,
        read_messages=lambda _uid: read_after_seq(uid, 0),
        read_messages_after_seq=read_after_seq,
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )
    result = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert result == "completed"
    assert surfaced == []
    assert v2_cursor.load_seq(core_store.get_store(uid)) == input_seq
    assert _job_status_row(job_id) == ("completed", None)
    with db.get_pool().connection() as conn:
        replies = conn.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()[0]
        effect = conn.execute(
            "SELECT status FROM v2_effect_outbox WHERE user_id=%s AND effect_type=%s",
            (uid, v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE),
        ).fetchone()
    assert replies == 1
    assert effect == ("applied",)


def test_pre_commit_status_failure_still_fails_without_reply(monkeypatch):
    """The best-effort boundary starts only after final-reply publication."""
    uid = "u_toolloop_pre_commit_status_failure"
    conftest.seed_user(uid)
    _reset(uid)
    _input_seq, job_id = db.chat_append_and_enqueue(
        uid,
        "A",
        10.0,
        _user_doc("A", "answer me"),
        5000,
        "chat",
        expected_generation=db.get_runtime_generation(uid),
    )
    job = jobs_store.claim_next_job("w-pre-commit-status")
    assert job is not None and job["id"] == job_id
    original_emit_status = worker._emit_status

    def fail_writing_status(user_id, source_job_id, kind):
        if kind == "writing_reply":
            raise RuntimeError("injected pre-commit status failure")
        return original_emit_status(user_id, source_job_id, kind)

    monkeypatch.setattr(worker, "_emit_status", fail_writing_status)
    provider_calls: list[int] = []

    async def provider(*_args, **_kwargs):
        provider_calls.append(1)
        return _text_round("must not publish")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    def read_after_seq(_user_id: str, after_seq: int):
        return [
            {
                "id": row["id"],
                "seq": row["seq"],
                "ts": row["ts"],
                "role": row.get("role"),
                "content": row.get("test_plaintext", ""),
            }
            for row in db.chat_messages_after_seq(uid, after_seq, limit=None)
            if row.get("role") == "user"
        ]

    deps = worker.TurnDeps(
        # web_search/web_fetch are gated per user now (default OFF); these
        # tests use them as a generic outbound read, so opt in explicitly.
        web_tools_enabled=lambda uid: True,
        read_messages=lambda _uid: read_after_seq(uid, 0),
        read_messages_after_seq=read_after_seq,
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )
    result = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert result == "failed"
    assert provider_calls == []
    assert _job_status_row(job_id)[0] == "failed"
    with db.get_pool().connection() as conn:
        replies = conn.execute(
            "SELECT COUNT(*) FROM chat_messages "
            "WHERE user_id=%s AND doc->>'role'='openclaw'",
            (uid,),
        ).fetchone()[0]
    assert replies == 0


def test_sweeper_wins_final_effect_before_producer_drain_and_loop_still_retries(
    monkeypatch,
):
    """The worker must acknowledge the durable row, not the drain return.

    Each wrapper call first runs an independent "sweeper" applier and only then
    returns the producing worker's now-empty drain result. The stale candidate
    must still retry, and the fresh candidate must still count as delivered.
    """
    uid = "u_toolloop_late_final_sweeper_wins"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind='model_api_runtime'",
            (uid,),
        )
    db.chat_append_strict(uid, "A", 10.0, _user_doc("A", "first A"), 5000)
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    job = jobs_store.claim_next_job("w-sweeper-wins")
    monkeypatch.setattr(
        worker,
        "_build_encrypted_reply_effect_payload",
        lambda _store, text, *, effect_id, reply_through_seq=None: {
            "text": text,
            "reply_through_seq": reply_through_seq,
        },
    )
    monkeypatch.setattr(
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({})
    )
    written: list[str] = []
    deps = _late_input_deps(uid, written)
    real_apply = deps.apply_pending_effects
    assert real_apply is not None
    producer_drains = []

    def sweep_before_producer(user_id: str):
        real_apply(user_id)
        producer_result = real_apply(user_id)
        producer_drains.append(producer_result)
        return producer_result

    deps.apply_pending_effects = sweep_before_producer
    calls = []

    async def provider(_config, messages, *, tools=None, **_kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            _seq, same_job_id = db.chat_append_and_enqueue(
                uid,
                "B",
                20.0,
                _user_doc("B", "late B"),
                5000,
                "chat",
                expected_generation=generation,
            )
            assert same_job_id == job_id
            return _text_round("stale A-only final")
        assert any(
            isinstance(message, dict) and message.get("content") == "late B"
            for message in messages
        )
        return _text_round("fresh A+B final")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert len(calls) == 2
    assert written == ["fresh A+B final"]
    # Turn-start recovery plus both final publications all return an empty
    # producer drain because the independent applier got there first.
    assert producer_drains[:3] == [
        {"applied": 0, "discarded": 0},
        {"applied": 0, "discarded": 0},
        {"applied": 0, "discarded": 0},
    ]
    with db.get_pool().connection() as conn:
        effects = conn.execute(
            "SELECT status,last_error FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s ORDER BY enqueue_seq",
            (uid, v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE),
        ).fetchall()
    assert effects == [
        ("discarded", v2_effect_outbox.FINAL_REPLY_INPUT_ADVANCED),
        ("applied", ""),
    ]


def test_last_call_late_input_hands_off_without_reply_or_error_chip(
    monkeypatch,
):
    uid = "u_toolloop_late_final_handoff"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind='model_api_runtime'",
            (uid,),
        )
    db.chat_append_strict(uid, "A", 10.0, _user_doc("A", "first A"), 5000)
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    job = jobs_store.claim_next_job("w-late-handoff")
    written: list[str] = []
    deps = _late_input_deps(uid, written)
    surfaced = []
    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", 1)
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda *args, **kwargs: surfaced.append((args, kwargs)),
    )
    monkeypatch.setattr(
        worker,
        "_build_encrypted_reply_effect_payload",
        lambda _store, text, *, effect_id, reply_through_seq=None: {
            "text": text,
            "reply_through_seq": reply_through_seq,
        },
    )
    monkeypatch.setattr(
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({})
    )

    async def provider(_config, _messages, *, tools=None, **_kwargs):
        _seq, same_job_id = db.chat_append_and_enqueue(
            uid,
            "B",
            20.0,
            _user_doc("B", "late B"),
            5000,
            "chat",
            expected_generation=generation,
        )
        assert same_job_id == job_id
        return _text_round("must never be visible")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "completed"
    assert written == []
    assert surfaced == []
    assert v2_cursor.load_seq(core_store.get_store(uid)) == 0
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,reason,expected_runtime_generation "
            "FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows[0][:2] == (job_id, "completed")
    assert rows[1][1:] == ("pending", "coalesced_followup", generation)


def test_invalid_final_fence_fails_visibly_without_reply_or_retry_loop(monkeypatch):
    uid = "u_toolloop_invalid_final_fence_handoff"
    conftest.seed_user(uid)
    _reset(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_blobs WHERE user_id=%s AND kind='model_api_runtime'",
            (uid,),
        )
    db.chat_append_strict(uid, "A", 10.0, _user_doc("A", "first A"), 5000)
    generation = db.get_runtime_generation(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat", expected_generation=generation)
    job = jobs_store.claim_next_job("w-invalid-fence")
    written: list[str] = []
    deps = _late_input_deps(uid, written)
    real_apply = deps.apply_pending_effects
    assert real_apply is not None
    surfaced = []
    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", 1)
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda *args, **kwargs: surfaced.append((args, kwargs)),
    )
    monkeypatch.setattr(
        worker,
        "_build_encrypted_reply_effect_payload",
        lambda _store, text, *, effect_id, reply_through_seq=None: {
            "text": text,
            "reply_through_seq": reply_through_seq,
        },
    )
    monkeypatch.setattr(
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({})
    )
    corrupted = []

    def corrupt_terminal_before_apply(user_id: str):
        with db.get_pool().connection() as conn:
            changed = conn.execute(
                "UPDATE v2_effect_outbox "
                "SET payload=payload - %s "
                "WHERE user_id=%s AND effect_type=%s "
                "AND status IN ('pending','pending_fenced_v1') "
                "AND payload ? 'reply_through_seq'",
                (
                    v2_effect_outbox.FINAL_REPLY_FENCE_KEY,
                    user_id,
                    v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE,
                ),
            ).rowcount
        if changed:
            corrupted.append(changed)
        return real_apply(user_id)

    deps.apply_pending_effects = corrupt_terminal_before_apply

    async def provider(_config, _messages, *, tools=None, **_kwargs):
        return _text_round("must never be visible")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    status = asyncio.run(
        worker.process_job(
            job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    )

    assert status == "failed"
    assert corrupted == [1]
    assert written == []
    assert len(surfaced) == 1
    assert surfaced[0][0][-1] == "turn_failed:runtimeerror"
    assert v2_cursor.load_seq(core_store.get_store(uid)) == 0
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id,status,reason,expected_runtime_generation "
            "FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
        effect = conn.execute(
            "SELECT status,last_error,attempt_count FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s",
            (uid, v2_effect_outbox.FINAL_REPLY_EFFECT_TYPE),
        ).fetchone()
    # Encrypted capture is independent; provider-backed offline review is
    # fail-closed/off unless the deployment explicitly opts in.
    assert rows == [(job_id, "failed", None, generation)]
    assert effect == ("discarded", v2_effect_outbox.FINAL_REPLY_INVALID_FENCE, 0)


def _job_status_row(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)
        ).fetchone()
    return row


def test_intermediate_reply_then_terminal_text_and_exactly_once_replay(monkeypatch):
    """Two-round script: round 0 the model calls `reply` (intermediate bubble)
    ALONGSIDE a `web_search` read tool call; round 1 the model stops with plain
    terminal text. Both bubbles land via the PR A effect outbox, the
    intermediate one visible BEFORE the terminal one (C6: drained immediately,
    not batched to end-of-turn). Then a re-drive that re-enqueues the SAME
    effect_id (job_id + effect_type + ordinal are what `effect_id.derive`
    hashes — a retry of the same turn reproduces it exactly) must NOT produce a
    duplicate bubble (PR A's ON CONFLICT DO NOTHING + pending-only drain)."""
    uid = "u_toolloop_tworound"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "search result"}),
    )
    _patch_real_write(monkeypatch)
    calls = _script_provider(
        monkeypatch,
        [
            _tool_round(
                _tc("r1", "reply", text="intermediate"),
                _tc("s1", "web_search", query="x"),
            ),
            _text_round("final answer"),
        ],
    )
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert len(calls) == 2
    # Round 1 carries the native assistant call and call-id-matched observation.
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    assert "search result" in " ".join(r.content for r in exchanges[0].results)

    bubbles = _bubbles(uid)
    assert len(bubbles) == 2
    # `_bubbles` reflects chat_messages' `seq` (identity-column) order, i.e. real
    # write order: the intermediate bubble must land before the terminal one.
    assert [b["body_ct"] for b in bubbles] == ["intermediate", "final answer"]

    # Exactly-once replay: reconstruct the FIRST reply effect's deterministic id
    # (ordinal 0 -- the intermediate `reply` tool call was the turn's first
    # enqueue_effect call) and re-drive enqueue+drain exactly as a retried turn
    # would.
    gen = db.get_runtime_generation(uid)
    eid = v2_effect_id.derive(job_id=job_id, effect_type="reply", ordinal=0)
    replay_id = v2_effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="reply",
        ordinal=0,
        expected_generation=gen,
        payload={"text": "intermediate"},
    )
    assert (
        replay_id == eid
    )  # same deterministic id -> ON CONFLICT DO NOTHING, no new row
    result = _apply_effects(uid)
    assert result == {
        "applied": 0,
        "discarded": 0,
    }  # already applied -> not in the pending set

    bubbles_after = _bubbles(uid)
    assert len(bubbles_after) == 2  # NO duplicate bubble


# ------------------------------------------------------------------
# PR C final review, BUG #2 (minor, no-filler): if the unified loop returns a
# LoopOutcome with NO reply produced (final_text empty AND replied_intermediate
# is False), the chat lane must mark the turn FAILED, not silently complete it
# with no bubble. Unlike test_v2_worker.py's existing BUG-4 successor test
# (`test_chat_turn_always_replies_even_when_model_only_calls_tools`), which
# exercises the NORMAL budget-forced-final-round path (last round has
# tools=None and the provider correctly returns plain text), this drives the
# genuinely misbehaving shape: the LAST round (tools=None) has the provider
# ignore that and return a non-reply tool_call anyway. `tool_loop.run_tool_loop`
# still dispatches it (tool_calls are honored regardless of what `tools` was
# passed on the wire), but the `for` loop is then exhausted with no terminal
# `on_reply` call ever having fired -> falls through to the
# `LoopOutcome("", rounds, "budget_exhausted", False)` return.
# ------------------------------------------------------------------


def test_chat_turn_with_no_reply_produced_marks_job_failed_not_completed(monkeypatch):
    uid = "u_toolloop_noreply"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", 1)
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "irrelevant"}),
    )
    # The ONE and only (last) round: tools=None is what the provider is asked
    # for, but it misbehaves and returns a non-reply tool_call anyway.
    calls = _script_provider(
        monkeypatch, [_tool_round(_tc("c1", "web_search", query="x"))]
    )
    write_calls = {"n": 0}
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda store, text: (
            write_calls.update(n=write_calls["n"] + 1) or {"id": "should-not-happen"}
        ),
    )
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "failed"
    assert len(calls) == 1
    assert write_calls["n"] == 0  # no filler bubble, no bubble at all
    assert _bubbles(uid) == []
    status_row = _job_status_row(job_id)
    assert status_row[0] == "failed"
    assert "empty_reply" in (status_row[1] or "")
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[1] is True  # failed=True in the metric row too


def test_required_file_missing_with_empty_provider_text_gets_terminal_reply(
    monkeypatch,
):
    uid = "u_toolloop_required_file_missing"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-required-file-missing")
    _patch_real_write(monkeypatch)

    async def direct_loop(**_kwargs):
        return worker.v2_tool_loop.LoopOutcome(
            final_text="",
            rounds=2,
            stop_reason="required_file_missing",
            replied_intermediate=False,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    deps = _deps(
        messages=[
            {
                "id": "m1",
                "ts": 10.0,
                "role": "user",
                "content": "请生成一份 PDF 报告",
            }
        ]
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert [bubble["body_ct"] for bubble in _bubbles(uid)] == [
        "这次没能生成你要求的可下载文件，请稍后再试。"
    ]
    assert _job_status_row(job_id)[0] == "completed"


# --------------------------------------------------------------- web gate


def _offered(call) -> set[str]:
    return {spec.name for spec in (call["tools"] or ())}


def test_chat_offers_web_tools_when_the_user_enabled_them(monkeypatch):
    uid = "u_web_gate_on"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("ok")])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=True,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert {"web_search", "web_fetch"} <= _offered(calls[0])


def test_chat_hides_web_tools_when_the_user_disabled_them(monkeypatch):
    """Closed state means the tools are ABSENT from the request, not offered
    and then refused: nothing for the model to call, nothing to explain away."""
    uid = "u_web_gate_off"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("ok")])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=False,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert {"web_search", "web_fetch"}.isdisjoint(_offered(calls[0]))
    # the rest of the catalog is untouched — this gate is additive, not a
    # wholesale narrowing of what the model can do
    assert "memory_index" in _offered(calls[0])


def test_chat_hides_web_tools_when_deps_seam_is_absent(monkeypatch):
    """TurnDeps.web_tools_enabled defaults to None (worker never imports
    hosted). That must read as OFF, not as "unconfigured, therefore allow"."""
    uid = "u_web_gate_none"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("ok")])
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [
            {"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}
        ],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt-enclave",
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    assert {"web_search", "web_fetch"}.isdisjoint(_offered(calls[0]))


def test_web_kill_switch_removes_the_tools_even_when_the_user_enabled_them(monkeypatch):
    uid = "u_web_gate_halted"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    monkeypatch.setattr(worker.kill_switch, "web_halted", lambda: (True, False))
    calls = _script_provider(monkeypatch, [_text_round("ok")])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=True,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    offered = _offered(calls[0])
    assert "web_search" not in offered      # halted
    assert "web_fetch" in offered           # independently still allowed


def test_kill_switch_flipped_mid_turn_cancels_the_next_web_batch(monkeypatch):
    """turn_catalog is computed once at the turn entry, so the offer side alone
    cannot stop a batch the model plans AFTER an operator halts web. The
    dispatcher re-checks before executing.

    Boundary being asserted: NEW dispatches stop. Requests already in flight are
    not cancelled — that is stated in the helper's docstring and in the runbook.
    """
    uid = "u_web_halt_midturn"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    # Not halted while the turn opens (so web IS in the offered catalog), halted
    # by the time the model asks for it.
    reads = {"n": 0}

    def _flipping():
        reads["n"] += 1
        return (False, False) if reads["n"] == 1 else (True, True)

    monkeypatch.setattr(worker.kill_switch, "web_halted", _flipping)

    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("s1", "web_search", query="x")),
        _text_round("done"),
    ])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=True,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    # offered at turn entry (first read said "not halted")
    assert "web_search" in {spec.name for spec in (calls[0]["tools"] or ())}
    # but the batch never executed: the model saw the halt error instead
    assert "error: web_tool_halted" in _tool_result_contents(calls[1])


def test_sibling_tools_are_cancelled_with_a_distinct_error(monkeypatch):
    """All-or-nothing: running the siblings while dropping the web call would
    leave half a batch applied. The sibling gets its own error string so the
    model can tell it was policy, not a memory_index failure."""
    uid = "u_web_halt_sibling"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    # Offered at turn entry, halted by the time the batch is dispatched — the
    # whole point of the second boundary.
    reads = {"n": 0}

    def _flipping():
        reads["n"] += 1
        return (False, False) if reads["n"] == 1 else (True, True)

    monkeypatch.setattr(worker.kill_switch, "web_halted", _flipping)

    calls = _script_provider(monkeypatch, [
        _tool_round(
            _tc("s1", "web_search", query="x"),
            _tc("s2", "memory_index"),
        ),
        _text_round("done"),
    ])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=True,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    contents = _tool_result_contents(calls[1])
    assert "error: web_tool_halted" in contents
    assert "error: batch_cancelled_web_halted" in contents


def test_non_web_batches_do_not_pay_for_a_control_read(monkeypatch):
    """The control-table read only happens when the batch actually contains a
    web call — otherwise every tool batch would carry a needless DB round-trip."""
    uid = "u_web_halt_noread"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    reads = {"n": 0}

    def _counting():
        reads["n"] += 1
        return (False, False)

    monkeypatch.setattr(worker.kill_switch, "web_halted", _counting)

    _script_provider(monkeypatch, [
        _tool_round(_tc("s1", "memory_index")),
        _text_round("done"),
    ])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=True,
    )

    asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    # exactly one read: the turn-entry offer decision. The memory_index batch
    # must not have triggered a second one.
    assert reads["n"] == 1


def test_half_open_halt_gives_the_still_working_tool_a_collateral_error(monkeypatch):
    """search halted, fetch fine, one batch holding both.

    Cancelling the whole batch is right, but web_fetch is only collateral — if it
    is told "web_tool_halted" the model concludes fetch is down too and stops
    retrying something that still works.
    """
    uid = "u_web_half_open"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)

    reads = {"n": 0}

    def _flipping():
        reads["n"] += 1
        # open at turn entry so both tools are offered; search halted by dispatch
        return (False, False) if reads["n"] == 1 else (True, False)

    monkeypatch.setattr(worker.kill_switch, "web_halted", _flipping)

    calls = _script_provider(monkeypatch, [
        _tool_round(
            _tc("s1", "web_search", query="x"),
            _tc("s2", "web_fetch", url="https://example.com/"),
        ),
        _text_round("done"),
    ])
    deps = _deps(
        messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}],
        web_enabled=True,
    )

    status = asyncio.run(
        worker.process_job(
            job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"
        )
    )

    assert status == "completed"
    contents = _tool_result_contents(calls[1])
    assert "error: web_tool_halted" in contents           # search: really halted
    assert "error: batch_cancelled_web_halted" in contents  # fetch: collateral
