"""worker._run_wake on the unified provider-native tool loop.

Style mirrors tests/test_v2_worker_tool_loop.py (the chat-lane sibling):
real jobs_store (real DB claim/mark_*), real core_store (real DB chat/reload),
real model_api_runtime.v2.coalesce/executor/effect_outbox/tool_loop; the only
boundary stubbed is `provider_client.chat_completion_async` (the LLM wire
`tool_loop.run_tool_loop` calls once per round — scripted here to drive
specific round shapes).

Key wake-specific differences from the chat lane, both asserted here:
- No synthetic user message: explicitly scheduled/manual wakes can run on an
  empty coalesce/fold, while an ordinary heartbeat with no real chat history
  completes without calling the provider.
- An empty wake round is recovered into an explicit reply/stay_silent choice;
  explicit silence still completes with zero bubbles.
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
import debug_trace
import provider_client
from provider_types import ToolCall, ToolExchange
from core import store as core_store
from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("DATABASE_URL"),
    reason="needs PG",
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


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
    monkeypatch.setattr(
        worker.core_envelope,
        "resolve_content_encryption",
        lambda _user_id: "off",
    )
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _patch_real_write(monkeypatch):
    """Same rationale as test_v2_worker_tool_loop.py's identical helper:
    `worker._write_encrypted_reply`'s real envelope-build path needs a live
    enclave, unavailable in this test process. Still performs a REAL
    `store.append_chat(..., strict=True)` DB write so `_bubbles` below reads
    back genuine chat_messages rows."""
    def _real_write(store, text):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _patch_tool_effect_encryption(monkeypatch):
    """Provide a deterministic test envelope without a live enclave.

    The durable payload contains only base64 test ciphertext; the local sink
    adapter below decodes it solely so this integration test can keep asserting
    the authorized memory action that reached the sink.
    """
    def _fake_build(store, plaintext, *, item_id=None):
        return ({
            "id": item_id,
            "owner_user_id": store.user_id,
            "body_ct": base64.b64encode(plaintext).decode("ascii"),
        }, "")

    monkeypatch.setattr(worker.core_envelope, "_build_shared_envelope_for_store", _fake_build)


def _effect_dispatch(user_id, sink_calls):
    """Test-local production-shaped sink for BOTH `reply` (mirrors
    `serve_worker._sink_reply`) and `memory` (records the payload instead of
    performing a real memory-actions write — memory persistence correctness
    has its own test files; this file only cares whether the write tool_call
    made it INTO the outbox authorized, not refused by the provenance gate)."""
    def dispatch(effect_type, payload):
        logical_effect_type = {
            stored: logical
            for logical, stored in worker.ENCRYPTED_TOOL_EFFECT_TYPES.items()
        }.get(effect_type, effect_type)
        if "effect_envelope" in payload:
            envelope = payload["effect_envelope"]
            decoded = json.loads(base64.b64decode(envelope["body_ct"]).decode("utf-8"))
            decoded["effect_id"] = payload["effect_id"]
            payload = decoded
        sink_calls.append((logical_effect_type, payload))
        if logical_effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
    return dispatch


def _apply_effects_factory(sink_calls):
    def _apply(user_id):
        return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_effect_dispatch(user_id, sink_calls))
    return _apply


from wake_look_first_helpers import (  # noqa: E402
    ScriptedCalls as _ScriptedCalls,
    is_look_first_round as _is_look_first_round,
    looked_nothing_needed as _looked_nothing_needed,
)


def _script_provider(monkeypatch, responses):
    """Presence wakes' look-first round (T723) is answered with "looked,
    nothing needed" without consuming a scripted response and is recorded in
    ``calls.look_rounds``."""
    it = iter(responses)
    calls = _ScriptedCalls()

    async def _fake(config, messages, *, tools=None, **_kwargs):
        if _is_look_first_round(tools, messages, _kwargs.get("tool_choice")):
            calls.look_rounds.append({"messages": messages, "tools": tools, **_kwargs})
            return _looked_nothing_needed()
        calls.append({"messages": messages, "tools": tools, **_kwargs})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _wake_reply_round(text, *, prompt_tokens=1, completion_tokens=1):
    return _tool_round(
        _tc(
            "wake-reply-test",
            "reply",
            aside="I want to say this now.",
            text=text,
        ),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _stay_silent_round(reason="没有值得打扰用户的新信息"):
    return _tool_round(_tc("stay-silent-test", "stay_silent", reason=reason))


def _tool_round(*tool_calls, prompt_tokens=1, completion_tokens=1):
    return {"reply": "", "tool_calls": list(tool_calls),
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tc(call_id, name, **args):
    return {"id": call_id, "name": name, "args": args}


def _wake_deps(
    *, tail=None, summary="", sink_calls=None, token="rt-enclave",
    observe_photo=None,
):
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: token,
        observe_photo=observe_photo,
        read_tail=lambda uid, after_ts, limit: list(tail if tail is not None else []),
        has_genuine_user_history=lambda _uid: True,
        apply_pending_effects=_apply_effects_factory(sink_calls if sink_calls is not None else []),
)


class _TrajectoryCapture:
    def __init__(self, events=None, scope=""):
        self.events = [] if events is None else events
        self.scope = scope

    def scoped(self, scope):
        return _TrajectoryCapture(self.events, str(scope))

    async def record(self, event_kind, payload):
        self.events.append((self.scope, event_kind, payload))
        return len(self.events) - 1

    async def record_best_effort(self, event_kind, payload):
        await self.record(event_kind, payload)
        return True


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def _bubbles(uid):
    store = core_store.get_store(uid)
    store.reload()
    return [m for m in store.chat_messages if m.get("role") == "openclaw" and m.get("source") == "model_api"]


def _turn_metric_row(job_id):
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT model_calls, failed, status, visible_reply_count "
            "FROM v2_turn_metrics WHERE job_id=%s",
            (job_id,)).fetchone()
    return row


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


# ------------------------------------------------------------------
# Terminal plain text is not a valid proactive delivery decision.
# ------------------------------------------------------------------

@pytest.mark.parametrize("output_limit", [None, 16384])
def test_wake_terminal_plain_text_fails_without_proactive_bubble(monkeypatch, output_limit):
    if output_limit is not None:
        monkeypatch.setattr(worker, "FILE_OUTPUT_MAX_TOKENS", output_limit)
    uid = "u_wake_toolloop_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [
        _text_round("hey, thinking of you"),
        _text_round("still not a structured choice"),
        _text_round("still not a structured choice"),
    ])
    sink_calls = []
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        sink_calls=sink_calls,
    )
    trajectory = _TrajectoryCapture()

    # Through process_job (not a direct _run_wake call) so a `TurnMetrics`
    # accumulator gets created and flushed, same as production's real
    # dispatch path (`_run_turn` -> `process_job` -> lane dispatch).
    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
        trajectory_recorder=trajectory,
    ))

    assert status == "failed"
    assert len(calls) == 2
    # Direct text gets exactly one structured-choice correction.
    expected_limit = (
        output_limit if output_limit is not None
        else provider_client.CHAT_OUTPUT_MAX_TOKENS
    )
    assert all(call["max_tokens"] == expected_limit for call in calls)
    assert all(call["tool_choice"] == "required" for call in calls[1:])
    assert _bubbles(uid) == []
    assert sink_calls == []
    reply_dispositions = [
        payload
        for _scope, kind, payload in trajectory.events
        if kind == "reply_effect_disposition"
    ]
    assert reply_dispositions == []

    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[0] >= 1           # >=1 model call
    assert row[1] is True
    assert row[2] == "wake_failed:choice_invalid"
    assert row[3] == 0
    assert _job_status(job_id) == ("failed", "wake_failed:choice_invalid")


# De-identified shapes from T658 manual review: A job51093, E job56653.
_A_WAKE_DRAFT = "早安，昨晚说累得要死，今天有课没？"
_E_WAKE_DRAFT = "宝贝还在睡呢，让她多休息会儿吧。"


@pytest.mark.parametrize("outcome,draft", [
    ("reply", _A_WAKE_DRAFT),
    ("silent", _E_WAKE_DRAFT),
    ("invalid", _A_WAKE_DRAFT),
    ("empty", _A_WAKE_DRAFT),
    ("other_tool", _E_WAKE_DRAFT),
])
def test_direct_wake_draft_gets_one_explicit_decision(monkeypatch, outcome, draft):
    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", 8)
    uid = "u_wake_draft_" + outcome
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)
    body = "醒来后慢慢来，记得吃点东西。"
    terminal = {
        "reply": _wake_reply_round(body),
        "silent": _stay_silent_round(),
        "invalid": _text_round("another unapproved draft"),
        "empty": _text_round(""),
        "other_tool": _tool_round(_tc("unexpected", "memory_index")),
    }[outcome]
    # A third usable choice must never rescue an invalid correction.
    calls = _script_provider(monkeypatch, [
        _text_round(draft), terminal, _wake_reply_round("unauthorized third attempt"),
    ])
    sink_calls = []
    deps = _wake_deps(tail=[{"id":"m1", "ts":1.0, "role":"user", "content":"hi"}], sink_calls=sink_calls)
    traces = []
    deps.emit_debug_trace = lambda user_id,event_type,**kw: traces.append((event_type,kw))
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
    ))
    assert len(calls) == 2
    assert calls[1]["tool_choice"] == "required"
    assert {t.name for t in calls[1]["tools"]} == {"reply", "stay_silent"}
    messages = calls[1]["messages"]
    assert any(m.get("role")=="assistant" and m.get("content")==draft for m in messages if isinstance(m,dict))
    assert all(draft not in str(m.get("content", "")) for m in messages if isinstance(m,dict) and m.get("role")=="system")
    assert "unpublished draft" in str(messages)
    bubbles = _bubbles(uid)
    if outcome == "reply":
        assert status == "completed"
        assert len(bubbles) == 1
        replies = [p for kind,p in sink_calls if kind == "reply"]
        assert len(replies) == 1 and replies[0]["text"] == body
    else:
        assert bubbles == []
        assert not any(kind == "reply" for kind,_ in sink_calls)
        if outcome == "silent":
            assert status == "completed"
        else:
            assert status == "failed"
            assert _job_status(job_id) == ("failed", "wake_failed:choice_invalid")
    correction = [debug_trace._safe_detail(kw["detail"]) for name,kw in traces if name == "wake.direct_text_correction"]
    expected = {"reply":"corrected_to_reply", "silent":"corrected_to_silent"}.get(outcome,"still_invalid")
    assert [row["outcome"] for row in correction] == ["direct_text_seen", expected]
    for name in worker.v2_tool_loop._WAKE_DIRECT_TEXT_OUTCOMES:
        assert sum(row[name] for row in correction) == int(name in {"direct_text_seen",expected})
    assert draft not in json.dumps(correction,ensure_ascii=False)
    assert body not in json.dumps(correction,ensure_ascii=False)


@pytest.mark.parametrize("budget", [1, 2, 8])
def test_direct_wake_correction_never_exceeds_remaining_budget(monkeypatch, budget):
    monkeypatch.setattr(worker, "_TURN_MAX_LLM_CALLS", budget)
    uid = "u_wake_draft_budget"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")
    calls = _script_provider(monkeypatch, [_text_round(_E_WAKE_DRAFT)] * 8)
    sink_calls = []
    deps = _wake_deps(tail=[{"id":"m1", "ts":1.0, "role":"user", "content":"hi"}], sink_calls=sink_calls)
    status = asyncio.run(worker.process_job(job,deps,provider_config=_BYOK,api_key=None,runtime_token="rt"))
    assert len(calls) == min(budget,2)
    assert status == "failed"
    assert _job_status(job_id) == ("failed", "wake_failed:choice_invalid")
    assert _bubbles(uid) == [] and sink_calls == []


def test_wake_enqueued_without_sink_is_not_counted_as_visible(monkeypatch):
    uid = "u_wake_toolloop_enqueued_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    _script_provider(monkeypatch, [_wake_reply_round("produced but not applied")])
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
    )
    deps.apply_pending_effects = None

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[3] == 0
    assert _bubbles(uid) == []


# ------------------------------------------------------------------
# Weak wake sleeps after the forced explicit choice, with no visible bubble.
# ------------------------------------------------------------------

def test_wake_empty_terminal_text_completes_with_zero_bubbles(monkeypatch):
    uid = "u_wake_toolloop_silence"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)
    calls = _script_provider(
        monkeypatch, [_text_round(""), _stay_silent_round()]
    )
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    sink_calls = []
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        sink_calls=sink_calls,
    )
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"])))

    assert status == "completed"
    assert calls[1]["tool_choice"] == "required"
    assert _bubbles(uid) == []
    assert sink_calls == []
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))


def _run_protocol_token_choice(
    monkeypatch, response, *, regular=True, provider_config=_BYOK,
):
    """Exercise the real loop and worker trace projection with recorded sinks."""
    loop = worker.v2_tool_loop
    calls = _script_provider(monkeypatch, [response])
    replies, reasons, events, surfaces = [], [], [], []

    async def on_reply(text, **_kwargs):
        replies.append(text)

    async def on_silent(reason):
        reasons.append(reason)

    async def dispatch(_calls):
        pytest.fail("terminal choices must not dispatch platform tools")

    async def fold():
        return []

    async def record(kind, payload):
        events.append((kind, payload))

    def emit(_uid, kind, **kwargs):
        if kind == "mcp.surface.provider":
            projected = kwargs["detail"]
            safe = debug_trace._safe_detail(projected)
            assert set(safe) == set(projected), "trace keys were silently truncated"
            surfaces.append(safe)

    deps = _wake_deps()
    deps.emit_debug_trace = emit
    trace = worker._provider_tool_surface_callback(
        deps, "u_protocol_choice", "heartbeat" if regular else "scheduled"
    )
    outcome = asyncio.run(loop.run_tool_loop(
        provider_config=provider_config,
        build_messages=lambda _transcript: [{"role": "user", "content": "hello"}],
        dispatch_tools=dispatch,
        on_reply=on_reply,
        on_stay_silent=on_silent if regular else None,
        regular_wake_choice_required=regular,
        fold_new_messages=fold,
        add_usage=lambda _usage: None,
        on_trajectory_event=record,
        on_provider_tool_surface=trace,
        require_reply=not regular,
        max_calls=1,
    ))
    return outcome, calls, replies, reasons, events, surfaces


@pytest.mark.parametrize("provider_name", ["anthropic", "openai_compatible"])
@pytest.mark.parametrize("text,token", [
    ("stay_silent", "stay_silent"),
    ("stay silent", "stay_silent"),
    ("Stay_silent.", "stay_silent"),
    ("  ‘STAY   SILENT!’  ", "stay_silent"),
    ("stay-silent", "stay_silent"),
    ("stay.silent", "stay_silent"),
    ("reply", "reply"),
    ("`speak`", "speak"),
    ("stay quiet", "stay_quiet"),
    ("stay_quiet", "stay_quiet"),
    ("stay-quiet", "stay_quiet"),
    ("proactive.sleep", "proactive_sleep"),
    ("proactive_sleep", "proactive_sleep"),
    ("proactive-sleep", "proactive_sleep"),
    ("sleep", "sleep"),
    ("__verify_ack__", "__sentinel__"),
    ("【`__PRIVATE_SENTINEL__`】。", "__sentinel__"),
])
def test_wake_reply_bare_protocol_token_stays_silent_without_retry(
    monkeypatch, text, token, provider_name,
):
    config = provider_client.ProviderConfig(
        provider=provider_name, model="test-model", api_key="test-key", base_url=""
    )
    result = _run_protocol_token_choice(
        monkeypatch, _wake_reply_round(text), provider_config=config,
    )
    outcome, calls, replies, reasons, events, surfaces = result
    assert outcome.stop_reason == "stay_silent"
    assert outcome.final_text == ""
    assert outcome.rounds == 1
    assert len(calls) == 1
    assert replies == []
    assert len(reasons) == 1 and "protocol token" in reasons[0]
    assert [p["choice"] for k, p in events if k == "wake_choice_response"] == [
        "silent_from_protocol_token"
    ]
    assert len([1 for k, _ in events if k == "stay_silent_planned"]) == 1
    assert len(surfaces) == 1
    assert surfaces[0]["protocol_token_reply"] == token
    assert surfaces[0]["wake_kind"] == "heartbeat"
    assert "wake_choice_required" not in surfaces[0]
    assert len(surfaces[0]) == 20
    assert "PRIVATE_SENTINEL" not in json.dumps(surfaces)


@pytest.mark.parametrize("untrusted", ["__PRIVATE_SENTINEL__", "arbitrary text", {}, None])
def test_worker_protocol_token_trace_rejects_noncanonical_values(untrusted):
    source = {"wake_choice_required": True, "call_rejection_reasons": []}
    baseline = worker._provider_tool_surface_trace_detail("heartbeat", source)
    actual = worker._provider_tool_surface_trace_detail(
        "heartbeat", {**source, "protocol_token_reply": untrusted}
    )
    assert actual == baseline
    assert "protocol_token_reply" not in actual
    assert worker._provider_tool_surface_added_detail_keys("heartbeat") == {
        "lane", "wake_kind",
    }


@pytest.mark.parametrize("text", [
    "我先安静一会儿", "我先 stay silent 一会儿", "stay silent for now, love",
    "speak up!", "Please reply.", "sleep well", "`speak` and listen",
    "__verify_ack__ received", "😴sleep", "speaker",
])
def test_wake_reply_natural_language_is_not_a_protocol_token(monkeypatch, text):
    outcome, calls, replies, reasons, events, surfaces = _run_protocol_token_choice(
        monkeypatch, _wake_reply_round(text)
    )
    assert outcome.stop_reason == "final_text"
    assert replies == [text]
    assert isinstance(replies[0], worker.v2_tool_loop.ValidatedWakeReply)
    assert reasons == [] and len(calls) == 1
    assert [p["choice"] for k, p in events if k == "wake_choice_response"] == ["reply"]
    assert len(surfaces) == 1 and len(surfaces[0]) == 20
    assert "protocol_token_reply" not in surfaces[0]
    assert "wake_choice_required" in surfaces[0]
    assert surfaces[0]["wake_kind"] == "heartbeat"


@pytest.mark.parametrize("malformation", [
    "missing_text", "extra_arg", "missing_id", "mixed_batch", "too_long", "media",
])
def test_protocol_token_does_not_make_an_invalid_reply_a_silent_choice(
    monkeypatch, malformation,
):
    response = _wake_reply_round("stay_silent")
    call = response["tool_calls"][0]
    if malformation == "missing_text":
        call["args"].pop("text")
    elif malformation == "extra_arg":
        call["args"]["extra"] = True
    elif malformation == "missing_id":
        call["id"] = ""
    elif malformation == "mixed_batch":
        response["tool_calls"].append(_tc("second", "stay_silent", reason="quiet"))
    elif malformation == "too_long":
        call["args"]["text"] = "__" + "x" * 8192 + "__"
    elif malformation == "media":
        response["media"] = [{"mime_type": "image/png", "data_base64": "AA=="}]
    with pytest.raises(worker.v2_tool_loop.WakeChoiceInvalid):
        _run_protocol_token_choice(monkeypatch, response)


@pytest.mark.parametrize("text", ["stay_silent", "sleep", "__verify_ack__"])
def test_non_choice_terminal_text_keeps_existing_delivery(monkeypatch, text):
    outcome, calls, replies, reasons, _events, surfaces = _run_protocol_token_choice(
        monkeypatch, _text_round(text), regular=False
    )
    assert outcome.stop_reason == "final_text" and replies == [text]
    assert len(calls) == 1 and reasons == []
    assert all("protocol_token_reply" not in surface for surface in surfaces)


@pytest.mark.parametrize("token", ["stay_silent", "`speak`", "__verify_ack__"])
def test_protocol_token_wake_completes_as_sleep_with_no_bubble(monkeypatch, token):
    uid = "u_wake_protocol_token"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [_wake_reply_round(token)])
    sink_calls = []
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        sink_calls=sink_calls,
    )
    trajectory = _TrajectoryCapture()
    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
        trajectory_recorder=trajectory,
    ))
    assert status == "completed" and len(calls) == 1
    assert _bubbles(uid) == [] and sink_calls == []
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT wake_result, wake_result_reason FROM agent_jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row[0] == "sleep" and "protocol token" in row[1]
    assert not any(e["kind"] == "error" for e in _status_events(uid))
    assert any(k == "wake_choice_response" and p["choice"] == "silent_from_protocol_token"
               for _scope, k, p in trajectory.events)


# ------------------------------------------------------------------
# Explicitly scheduled wakes remain valid without real chat history and do not
# manufacture a user request.
# ------------------------------------------------------------------

@pytest.mark.parametrize("lane", ["scheduled", "heartbeat", "manual_wake", "screen_watch"])
@pytest.mark.parametrize("output_limit", [None, 16384])
def test_all_wake_lanes_share_output_budget_on_initial_and_correction_calls(
    monkeypatch, lane, output_limit,
):
    if output_limit is not None:
        monkeypatch.setattr(worker, "FILE_OUTPUT_MAX_TOKENS", output_limit)
    uid = "u_wake_shared_output_budget"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")
    _patch_real_write(monkeypatch)
    final = (
        _text_round("Time for your reminder.") if lane == "scheduled"
        else _wake_reply_round("I wanted to check in.")
    )
    # Scheduled correction requires a terminated empty provider success;
    # a completely missing response is intentionally not recoverable in that lane.
    empty_success = {**_text_round(""), "stop_reason": "end_turn"}
    calls = _script_provider(monkeypatch, [empty_success, final])
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
    )
    status = asyncio.run(worker._run_wake(
        job_id, uid, lane, deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"]),
    ))

    assert status == "completed", _job_status(job_id)
    assert len(calls) == 2
    expected = output_limit or provider_client.CHAT_OUTPUT_MAX_TOKENS
    assert expected != 700
    assert all(call.get("max_tokens") == expected for call in calls)
    assert len(_bubbles(uid)) == 1


def test_wake_empty_tail_still_completes_no_no_user_messages_guard(monkeypatch):
    uid = "u_wake_toolloop_notail"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)
    seen = {}

    async def _fake(config, messages, *, tools=None, **_kwargs):
        seen["messages"] = messages
        assert _kwargs["max_tokens"] == worker.FILE_OUTPUT_MAX_TOKENS
        # 必须返**非空**正文。本用例测的是空 tail 下的 prompt 形状（不触发
        # `no_user_messages` 闸、不造用户角色消息），空回复只是早期图省事的载体；
        # scheduled 道打开 require_reply 之后，空回复本身就会让这一轮判失败，
        # 载体会把被测意图整个盖掉。给了正文，`status == "completed"` 才真正只
        # 由那个闸决定——闸一旦误触发，这里立刻红。
        return _text_round("a scheduled nudge")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    deps = _wake_deps(tail=[])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "scheduled", deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"])))

    assert status == "completed"
    conversation_messages = [
        message
        for message in seen["messages"]
        if message.get("role") != "system"
        and not str(message.get("content") or "").startswith(
            v2_context.RUNTIME_CONTEXT_HEADER
        )
        and not str(message.get("content") or "").startswith(
            v2_context.TEMPORAL_CONTEXT_HEADER
        )
        and not str(message.get("content") or "").startswith(
            v2_context.PROACTIVE_TURN_BOUNDARY_HEADER
        )
    ]
    assert conversation_messages == []
    assert seen["messages"][-1] == {
        "role": "user",
        "content": v2_context.PROACTIVE_TURN_BOUNDARY,
    }


# ------------------------------------------------------------------
# memory_write is AUTHORIZED (turn_authorization=True from wake), applied, and
# acknowledged with its durable disposition — never refused by provenance.
# ------------------------------------------------------------------

def test_wake_memory_write_is_authorized_applied_and_not_refused(monkeypatch):
    uid = "u_wake_toolloop_memwrite"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w")
    # 生产的 `process_job` 在派给 `_run_wake` 之前必定先 `mark_running`。
    # 平台写的 owner 栅栏(T154)和 `start_mcp_mutation_attempt` 一样要求
    # status='running' + 有效租约,所以直接调 `_run_wake` 的测试必须自己补上
    # 这次状态迁移 —— 否则测的是一个生产里不存在的状态。
    assert jobs_store.mark_running(job_id, claimed_by=job["claimed_by"])

    _patch_real_write(monkeypatch)
    _patch_tool_effect_encryption(monkeypatch)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc(
            "w1", "memory_write",
            actions=[{
                "op": "add",
                "summary": "likes tea",
                "content": "likes tea",
            }],
        )),
        _text_round(""),
        _stay_silent_round(),
    ])
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    trajectory = _TrajectoryCapture()

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "manual_wake",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        str(job["claimed_by"]),
        trajectory_recorder=trajectory,
    ))

    assert status == "completed"
    assert len(calls) == 3
    assert calls[2]["tool_choice"] == "required"
    # Round 1 carries the native assistant call and call-id-matched write result.
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    round1_results = " ".join(r.content for r in exchanges[0].results)
    assert "ok: memory_write applied" in round1_results
    assert "refused" not in round1_results
    tool_results = [
        payload
        for _scope, kind, payload in trajectory.events
        if kind == "tool_call_result" and payload.get("call_id") == "w1"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["effect"]["status"] == "applied"

    memory_sinks = [p for (t, p) in sink_calls if t == "memory"]
    assert len(memory_sinks) == 1
    # The raw model action ({"op":"add","summary":...,"content":...}) is translated into the server
    # memory-action shape (worker._memory_tool_actions) — no envelope, nested
    # plaintext memory dict — so the plaintext write path builds the E2E envelope.
    # NOT passed through raw (which memory_core.actions rejects with 400).
    assert len(memory_sinks[0]["actions"]) == 1
    action = memory_sinks[0]["actions"][0]
    assert action["type"] == "memory.add"
    assert action["reason"] == "Written by the agent via the memory_write tool."
    assert action["capture_mode"] == "agent_tool"
    assert action["memory"]["summary"] == "likes tea"
    assert action["memory"]["content"] == "likes tea"
    # The enqueue boundary now pins occurrence time; bucket/thread defaults
    # are normalized later by the memory sink instead of being invented here.
    assert action["memory"]["occurred_at"]
    serve_worker._validate_decrypted_tool_effect(
        "memory", {**memory_sinks[0], "effect_id": "wake-memory-effect"})
    # The durable outbox still contains only the encrypted wrapper; the model's
    # plaintext action is revealed only after sink-side enclave decryption.
    with db.get_pool().connection() as conn:
        stored_payload = conn.execute(
            "SELECT payload::text FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s",
            (uid, worker.ENCRYPTED_TOOL_EFFECT_TYPES["memory"]),
        ).fetchone()[0]
    assert "likes tea" not in stored_payload
    assert "effect_envelope" in stored_payload


@pytest.mark.parametrize(
    "lane", ["heartbeat", "scheduled", "manual_wake", "screen_watch"]
)
def test_wake_lanes_hide_and_reject_memory_delete(monkeypatch, lane):
    """One shared wake path must protect every proactive lane at both gates."""
    uid = f"u_wake_no_memory_delete_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc(
            "delete-1",
            "memory_write",
            actions=[{"op": "delete", "target_id": "memory-1"}],
        )),
        (
            _text_round("wake finished")
            if lane == "scheduled"
            else _wake_reply_round("wake finished")
        ),
    ])
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        lane,
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        str(job["claimed_by"]),
    ))

    assert status == "completed"
    assert len(calls) == 2
    memory_spec = next(
        spec for spec in calls[0]["tools"] if spec.name == "memory_write"
    )
    wake_ops = memory_spec.parameters["properties"]["actions"]["items"][
        "properties"
    ]["op"]["enum"]
    assert wake_ops == ["add", "update"]
    assert "delete" not in memory_spec.description.lower()

    exchanges = [
        message
        for message in calls[1]["messages"]
        if isinstance(message, ToolExchange)
    ]
    assert len(exchanges) == 1
    assert exchanges[0].results[0].content == (
        "error: memory delete refused in background turn"
    )
    assert not [
        payload for effect_type, payload in sink_calls if effect_type == "memory"
    ]
    assert _job_status(job_id)[0] == "completed"


@pytest.mark.parametrize("tool_call", [
    _tc("identity-write", "identity_patch", signature="changed in background"),
    _tc("identity-write", "identity_nudge", dimension="warmth", delta=1),
])
def test_wake_identity_write_is_visibly_refused_and_not_enqueued(
    monkeypatch, tool_call,
):
    uid = "u_wake_toolloop_identity_refused"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w-identity-refused")

    calls = _script_provider(monkeypatch, [
        _tool_round(tool_call),
        _text_round(""),
        _stay_silent_round(),
    ])
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    deps.load_workspace_prompt = lambda *_args, **_kwargs: {
        "identity_card_or_persona": worker.context.render_identity_card({
            "agent_name": "Mira",
            "dimensions": [{"name": "warmth", "value": 70}],
        }),
        "trusted_system_blocks": (),
    }

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "manual_wake",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        str(job["claimed_by"]),
    ))

    assert status == "completed"
    assert len(calls) == 3
    assert calls[2]["tool_choice"] == "required"
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    result = exchanges[0].results[0]
    assert result.call_id == "identity-write"
    assert result.content.startswith("error:")
    assert "identity write refused in background turn" in result.content
    assert sink_calls == []
    assert db.effect_pending(uid) == []
    assert _job_status(job_id)[0] == "completed"


def test_wake_mixed_valid_invalid_workspace_batch_applies_valid_call(
    monkeypatch,
):
    uid = "u_wake_mixed_workspace_batch"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w-mixed-workspace")
    # 生产的 `process_job` 在派给 `_run_wake` 之前必定先 `mark_running`。
    # 平台写的 owner 栅栏(T154)和 `start_mcp_mutation_attempt` 一样要求
    # status='running' + 有效租约,所以直接调 `_run_wake` 的测试必须自己补上
    # 这次状态迁移 —— 否则测的是一个生产里不存在的状态。
    assert jobs_store.mark_running(job_id, claimed_by=job["claimed_by"])
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
        return worker.v2_tool_loop.LoopOutcome(
            final_text="",
            rounds=1,
            stop_reason="final_text",
            replied_intermediate=False,
        )

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    sink_calls = []
    deps = _wake_deps(tail=[], sink_calls=sink_calls)
    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "manual_wake",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            str(job["claimed_by"]),
        )
    )

    assert status == "completed"
    assert [result.call_id for result in captured["results"]] == [
        "valid",
        "invalid",
    ]
    assert captured["results"][0].content == "ok: workspace_write applied"
    assert captured["results"][1].content.startswith("error: unparseable args")
    assert [kind for kind, _payload in sink_calls] == ["workspace_batch"]


def test_wake_photo_read_observation_is_pull_on_demand(monkeypatch):
    uid = "u_wake_photo_observation"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w-photo")
    monkeypatch.setattr(
        worker.cap_registry,
        "run_capability",
        lambda *_a, **_k: type("Result", (), {
            "to_dict": lambda self: {
                "ok": True,
                "data": {
                    "photo_id": "p1",
                    "has_image": True,
                    "image_media_type": "image/jpeg",
                    "image_b64": "cGl4ZWxz",
                },
                "trace": {},
                "warnings": [],
            }
        })(),
    )
    observed = []

    def _observe_photo(user_id, **kwargs):
        observed.append((user_id, kwargs))
        return "a handwritten note on a desk"

    calls = _script_provider(monkeypatch, [
        _tool_round(_tc(
            "photo-1", "photo_read", photo_id="p1", include_image=True
        )),
        _text_round(""),
        _stay_silent_round(),
    ])
    deps = _wake_deps(tail=[], observe_photo=_observe_photo)

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "manual_wake",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        str(job["claimed_by"]),
    ))

    assert status == "completed"
    assert len(observed) == 1
    exchanges = [m for m in calls[1]["messages"] if isinstance(m, ToolExchange)]
    assert len(exchanges) == 1
    content = exchanges[0].results[0].content
    assert "a handwritten note on a desk" in content
    assert "cGl4ZWxz" not in content
    assert "image_b64" not in content


def test_wake_without_photo_read_never_observes_photo(monkeypatch):
    uid = "u_wake_photo_not_pulled"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w-no-photo")

    def _observe_photo(*_a, **_k):
        raise AssertionError("photo wake must stay pull-on-demand")

    _script_provider(
        monkeypatch, [_text_round(""), _stay_silent_round()]
    )
    deps = _wake_deps(tail=[], observe_photo=_observe_photo)

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "manual_wake",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        str(job["claimed_by"]),
    ))

    assert status == "completed"


def test_wake_memory_write_refused_when_process_job_seeds_no_authorization(monkeypatch):
    """Negative control for the above: confirms the assertion strings actually
    distinguish authorized vs refused (guards against a vacuously-true positive
    test) by directly exercising `provenance.write_gate` with
    turn_authorization=False — the same deterministic gate `_run_wake`'s
    dispatcher relies on being True for."""
    from model_api_runtime.v2 import provenance as v2_provenance

    allowed, reason = v2_provenance.write_gate("memory_write", turn_authorization=False)
    assert allowed is False
    assert "refused" in reason

    allowed_wake, _ = v2_provenance.write_gate("memory_write", turn_authorization=True)
    assert allowed_wake is True


# ------------------------------------------------------------------
# Real provider failure -> silent mark_failed (never surfaced, never a bubble).
# ------------------------------------------------------------------

def test_wake_provider_error_silent_mark_failed(monkeypatch):
    uid = "u_wake_toolloop_provider_err"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("boom", status_code=500)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"])))

    assert status == "failed"
    assert _bubbles(uid) == []
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "wake_failed" in (row[1] or "")
    assert not any(e["kind"] == "error" for e in _status_events(uid))


# ------------------------------------------------------------------
# provider_config-kind failures (dead/broke BYOK key) still set payment
# cooldown BEFORE the silent mark_failed (wake-lane contract).
# ------------------------------------------------------------------

def test_wake_provider_config_error_still_sets_payment_cooldown(monkeypatch):
    import time as _time

    uid = "u_wake_toolloop_provider_config"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("out of credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    before = _time.time()
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"])))
    after = _time.time()

    assert status == "failed"
    assert len(cooldown_calls) == 1
    called_uid, kwargs = cooldown_calls[0]
    assert called_uid == uid
    cooldown_at = kwargs["payment_cooldown_until"]
    assert before + worker._WAKE_COOLDOWN_SEC - 5 <= cooldown_at <= after + worker._WAKE_COOLDOWN_SEC + 5

    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is not None
    assert schedule["payment_cooldown_until"] is not None


def test_wake_transient_error_does_not_set_payment_cooldown(monkeypatch):
    uid = "u_wake_toolloop_transient"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("timeout-ish", status_code=503)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"])))

    assert status == "failed"
    assert cooldown_calls == []
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is None or schedule["payment_cooldown_until"] is None


# ------------------------------------------------------------------
# screen_watch lane: its own system prompt + safe screen availability grounding
# flows through `extra_context`; caption text remains an explicit tool read.
# ------------------------------------------------------------------

def test_screen_watch_lane_uses_its_own_prompt_and_screen_context(monkeypatch):
    uid = "u_wake_toolloop_screenwatch"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "screen_watch")
    job = jobs_store.claim_next_job("w")

    async def _fake_cap_data(store, action_type, **kw):
        assert action_type == "screen_recent"
        return {"frames": [{"frame_id": "f1", "caption": "a stack trace"}]}

    monkeypatch.setattr(worker, "_cap_data", _fake_cap_data)
    _patch_real_write(monkeypatch)

    seen = {}

    async def _fake(config, messages, *, tools=None, **_kwargs):
        seen["messages"] = messages
        seen["tools"] = tools
        return _wake_reply_round("你在看这个报错？")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "screen_watch", deps, _BYOK, asyncio.Semaphore(4), str(job["claimed_by"])))

    assert status == "completed"
    system_msg = next(m for m in seen["messages"] if m["role"] == "system")
    assert "watching the screen" in system_msg["content"]
    joined = " ".join(str(m.get("content", "")) for m in seen["messages"])
    assert "a stack trace" not in joined
    assert '"recent_count":1' in joined
    assert "screen_recent" in {spec.name for spec in seen["tools"]}
    assert _bubbles(uid)[0]["body_ct"] == "你在看这个报错？"


# --------------------------------------------------------------- web gate


def _wake_offered(monkeypatch, *, user_enabled: bool, uid: str) -> set[str]:
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_wake_reply_round("hey")])
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}],
        sink_calls=[],
    )
    deps.web_tools_enabled = lambda uid_: user_enabled

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt",
    ))
    assert status == "completed"
    return {spec.name for spec in (calls[0]["tools"] or ())}


def test_wake_offers_web_tools_when_the_user_enabled_them(monkeypatch):
    """The proactive companion follows the SAME switch as chat.

    It could already reach the network before this feature existed, so a
    background carve-out would be a silent capability regression wearing the
    costume of a new setting. One switch, every lane.
    """
    offered = _wake_offered(monkeypatch, user_enabled=True, uid="u_wake_web_on")
    assert {"web_search", "web_fetch"} <= offered
    # the rest of the wake tool surface is unchanged
    assert "memory_index" in offered


def test_wake_withholds_web_tools_when_the_user_turned_them_off(monkeypatch):
    """...and turning the switch off closes the background lane too, which is
    the whole reason it is allowed to be one switch."""
    offered = _wake_offered(monkeypatch, user_enabled=False, uid="u_wake_web_off")
    assert {"web_search", "web_fetch"}.isdisjoint(offered)
    assert "memory_index" in offered
