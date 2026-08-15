"""Wake-lane processing on the unified `tool_loop.run_tool_loop`.

`_run_wake` mirrors `_run_compaction`'s self-contained shape (own try/except).
Weak wake failures remain silent; a scheduled reminder failure is durably
surfaced because that lane has an explicit delivery obligation. On a
SUCCESSFUL model-authored reply it writes an encrypted
chat bubble via the PR A effect outbox (`on_reply` -> `enqueue_effect` ->
`apply_pending_effects`, same mechanism the chat lane uses) — the whole point
of a wake lane is letting the companion reach out proactively.

"Weak wake sleeps": an empty terminal reply (the model chose to stay silent)
is NOT a failure — it's `mark_completed` with zero bubbles. This is the
OPPOSITE of the chat lane's no-filler rule. Proactive application data is
never serialized as a user request. An ordinary heartbeat with no real chat
history completes without calling the provider; explicitly scheduled and
manual wakes remain valid with an empty coalesce/read_tail.

A real provider failure (`provider_client.chat_completion_async` raising) IS
a failure. Scheduled wakes surface it; other wake lanes keep the background
isolation used by maintenance.

Style: real jobs_store/core_store (real DB claim/mark_*/status events) +
stubbed `provider_client.chat_completion_async` (the LLM wire boundary
`tool_loop.run_tool_loop` calls once per round) + stubbed
`worker._write_encrypted_reply` (spy, no real envelope/enclave round-trip in
a unit test) reached through a real PR A effect-outbox drain wired via
`TurnDeps.apply_pending_effects`."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
from nacl.public import PrivateKey

import conftest
import db
import provider_client
from content_encryption import build_envelope
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker
from tools.e2e.client import E2EClient, TEST_API


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """Same rationale as test_v2_worker.py's identical fixture: claim_next_job()
    is a global work-queue claim with no user_id filter, so a pending row left
    behind by another test module would otherwise get claimed here instead of
    this test's own row."""
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


def _job_status(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute("SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
    return row


def _status_events(uid):
    return jobs_store.list_status_events(uid, after_id=0, limit=100)


def _claim(job_id: int) -> str:
    job = jobs_store.claim_next_job("wake-test")
    assert job is not None and job["id"] == job_id
    return str(job["claimed_by"])


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the wake lane's `reply` effect_type
    (mirrors `serve_worker._sink_reply`'s real write, `worker._write_encrypted_
    reply`, without pulling in serve_worker's hosted-adjacent wiring — same
    pattern test_v2_worker.py/test_v2_worker_tool_loop.py use for the chat
    lane)."""
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
    return dispatch


def _apply_effects(user_id):
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))


def _wake_deps(*, summary="", tail=None, has_genuine_user_history=None):
    return worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: list(tail if tail is not None else []),
        has_genuine_user_history=(
            has_genuine_user_history
            if has_genuine_user_history is not None
            else (lambda _uid: bool(tail))
        ),
        apply_pending_effects=_apply_effects,
    )


def _script_provider(monkeypatch, responses):
    """Monkeypatch `provider_client.chat_completion_async` — what
    `tool_loop.run_tool_loop` calls once per round (the wake lane's LLM wire
    boundary since Task 8)."""
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None, **_kwargs):
        calls.append({"messages": messages, "tools": tools})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _patch_user_decryptable_envelopes(monkeypatch, user_id: str) -> E2EClient:
    """Use real reply-envelope crypto and return the user-side decryptor."""
    user_sk = PrivateKey.generate()
    enclave_pk = bytes(PrivateKey.generate().public_key)
    client = E2EClient(
        TEST_API,
        user_id,
        "test-api-key",
        user_sk,
        enclave_pk,
    )

    def _build(store, plaintext, *, item_id=None):
        assert store.user_id == user_id
        return (
            build_envelope(
                plaintext=bytes(plaintext),
                owner_user_id=user_id,
                user_pk_bytes=bytes(user_sk.public_key),
                enclave_pk_bytes=enclave_pk,
                visibility="shared",
                item_id=item_id,
            ),
            "",
        )

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        _build,
    )
    return client


# ------------------------------------------------------------------
# _run_wake direct unit coverage
# ------------------------------------------------------------------

def test_shadow_signals_never_reach_the_wake_provider_prompt(monkeypatch):
    """A′ signals are post-decision observations, never provider inputs.

    This invariant is what permits shadow telemetry to ship without changing
    wake product policy. Exercise the real `_run_wake` prompt assembly so both
    its wake system prompt and runtime application data are covered.
    """
    uid = "u_wake_shadow_prompt_purity"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    claimed_by = _claim(job_id)
    calls = _script_provider(monkeypatch, [_text_round("")])

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "manual_wake",
            _wake_deps(tail=[]),
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    provider_wire = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert "apns_alert_sent" not in provider_wire
    assert "local_hour" not in provider_wire


def test_run_wake_reply_written_and_job_completed(monkeypatch):
    uid = "u_wake_reply"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    seen = {}

    async def _fake(config, messages, *, tools=None, **_kwargs):
        seen["messages"] = messages
        return _text_round("hey, how did that go?")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text, user_id=store.user_id) or {"id": "r1"})

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed"
    assert written == {"text": "hey, how did that go?", "user_id": uid}
    assert _job_status(job_id)[0] == "completed"
    system_msg = next(m for m in seen["messages"] if m["role"] == "system")
    assert worker._WAKE_SYSTEM_PROMPT in system_msg["content"]
    assert worker._OPTIONAL_WAKE_SELF_THINKING_INSTRUCTION in system_msg["content"]
    assert [
        message["content"]
        for message in seen["messages"]
        if message.get("role") == "user"
    ] == [v2_context.PROACTIVE_TURN_BOUNDARY]


def test_wake_self_thinking_on_drops_native_reasoning_fallback(monkeypatch):
    """Wake follows Chat: native CoT is not displayed while self-thinking is ON."""
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_wake_selfthink_no_fallback"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _script_provider(
        monkeypatch,
        [{
            "reply": "hey, how did that go?",
            "reasoning": "private native cot",
            "tool_calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }],
    )
    monkeypatch.setattr(
        worker,
        "_build_thinking_payload",
        lambda *_args, **_kwargs: pytest.fail(
            "native reasoning must not be sealed while self-thinking is on"
        ),
    )
    written = {}
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda store, text: written.update(text=text, user_id=store.user_id)
        or {"id": "wake-no-fallback"},
    )
    traces = []
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "event_type": event_type, **fields}
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert written == {"text": "hey, how did that go?", "user_id": uid}
    thinking_traces = [
        trace for trace in traces if trace["event_type"] == "thinking.surfaced"
    ]
    assert [trace["detail"] for trace in thinking_traces] == [{
        "branch": "none",
        "chars": 0,
        "model": _BYOK.model,
        "lane": "wake",
    }]


@pytest.mark.parametrize(
    "lane", ["heartbeat", "scheduled", "manual_wake", "screen_watch"]
)
def test_wake_full_chain_strips_tool_markup_after_user_decrypt(monkeypatch, lane):
    """The wake outlet must apply the same visible-text guard as chat before
    sealing; the assertion is intentionally after strict user-key decrypt."""
    uid = f"u_wake_tool_markup_user_view_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed_by = _claim(job_id)
    leaked = (
        '<parameter name="tool_name">reply</parameter>\n'
        "好，棋先停着\n你要干嘛去了"
    )
    _script_provider(monkeypatch, [_text_round(leaked)])
    decryptor = _patch_user_decryptable_envelopes(monkeypatch, uid)
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "先停一下"}]
    )
    deps.apply_pending_effects = serve_worker._apply_pending_effects_for_user
    traces = []
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "event_type": event_type, **fields}
    )

    try:
        status = asyncio.run(
            worker._run_wake(
                job_id,
                uid,
                lane,
                deps,
                _BYOK,
                asyncio.Semaphore(4),
                claimed_by,
            )
        )
        store = core_store.get_store(uid)
        store.reload()
        bubble = next(
            row for row in store.chat_messages
            if row.get("role") == "openclaw" and row.get("source") == "model_api"
        )
        plaintext = decryptor.decrypt_reply(bubble)
    finally:
        decryptor._http.close()

    assert status == "completed"
    assert plaintext == "好，棋先停着\n你要干嘛去了"
    sanitized = [
        trace for trace in traces if trace["event_type"] == "agent.reply.sanitized"
    ]
    assert [trace["detail"] for trace in sanitized] == [
        {
            "lane": lane,
            "final": True,
            "error_class": "upstream_unavailable",
            "reason": "tool_markup_leak_sanitized",
        }
    ]


def test_wake_markup_only_reply_fails_silently_without_bubble(monkeypatch):
    """Wake keeps its existing degenerate lifecycle after sanitization."""
    uid = "u_wake_tool_markup_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _script_provider(monkeypatch, [_text_round("<tool_call></tool_call>")])
    writes = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda *_args, **_kwargs: writes.append(True),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "在吗"}]
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "failed"
    assert writes == []
    assert _job_status(job_id) == (
        "failed",
        "wake_failed:degenerate_reply_suppressed",
    )


def test_run_wake_reply_push_carries_is_wake_true_and_manual_wake_lane(monkeypatch):
    """Review Minor #2: before this test, the wake lane's entire push wiring
    (push_slot build + the `finally` `deps.send_reply_push` call in `_run_wake`)
    had zero coverage — a copy-paste bug at the wake lane's `push_slot` build
    (e.g. `is_wake=True` silently becoming `False`, or a missing/wrong `lane`)
    would ship undetected. This runs a real seq-native wake turn through the
    production effect sink (`serve_worker._apply_pending_effects_for_user`,
    the same one `serve_worker.build_production_deps` wires in prod) so
    `push_slot` is built from a genuinely persisted envelope, not a test
    double that bypasses the code path under test — only the enclave envelope
    crypto itself is stubbed (same technique `test_v2_atomic_reply_cursor.py`
    uses for the chat lane's analogous test): a real KMS round-trip needs a
    fully onboarded content key, which is orthogonal to what this test checks.
    """
    uid = "u_wake_push"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    claimed_by = _claim(job_id)
    # The seq-native reply-effect fence (`effect_outbox._lock_active_reply_
    # source_job`) requires the source job to be status=="running", not just
    # "claimed" — production's `process_job` always transitions through
    # `mark_running` before dispatching to `_run_wake`; this test calls
    # `_run_wake` directly (same as every other test in this file), so it must
    # do the same transition explicitly or the reply effect gets discarded as
    # `source_job_not_active` before ever reaching `_on_reply`'s applied branch.
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)

    def _fake_envelope(_store, _text, *, item_id=None):
        return (
            {
                "v": 1,
                "id": str(item_id),
                "owner_user_id": "ignored-by-store",
                "visibility": "shared",
                "body_ct": "ciphertext",
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        )

    monkeypatch.setattr(
        worker.core_envelope, "_build_shared_envelope_for_store", _fake_envelope)

    reply_text = "hey, thinking of you — " + ("x" * 300)
    assert len(reply_text) > 240

    provider_messages = []

    async def _fake(config, messages, *, tools=None, **_kwargs):
        provider_messages.append(messages)
        return _text_round(reply_text)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    pushes = []
    shadow = []

    def _send_push(uid, **kwargs):
        pushes.append((uid, kwargs))
        return True

    deps = worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: [],
        read_messages_after_seq=lambda uid, after_seq: [],
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        send_reply_push=_send_push,
        record_wake_shadow_decision=(
            lambda uid, **kw: shadow.append((uid, kw)) or True
        ),
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed"
    store = core_store.get_store(uid)
    store.reload()
    replies = [m for m in store.chat_messages if m.get("role") == "openclaw"]
    assert replies, "wake reply must actually be persisted"

    assert pushes, "wake lane must push its final reply"
    _, kw = pushes[0]
    assert kw["msg_id"] == replies[0]["id"], (
        "pushed msg_id must be the envelope id of the row that was actually "
        "persisted"
    )
    assert kw["body"] == reply_text[:240]
    assert kw["is_wake"] is True, "wake lane must push with is_wake=True"
    assert kw["lane"] == "manual_wake", (
        "backend derives manual/source from this — a copy-paste bug here "
        "silently breaks the reminders_delivery-off manual-wake bypass"
    )
    runtime_message = next(
        message
        for message in provider_messages[0]
        if message.get("role") == "assistant"
        and str(message.get("content") or "").startswith(
            v2_context.RUNTIME_CONTEXT_HEADER
        )
    )
    runtime_payload = json.loads(runtime_message["content"].split("\n", 1)[1])
    assert runtime_payload["runtime_control"]["manual_wake"] is True
    assert provider_messages[0][-1] == {
        "role": "user",
        "content": v2_context.PROACTIVE_TURN_BOUNDARY,
    }
    assert not any(
        message.get("role") == "user"
        and message.get("content") != v2_context.PROACTIVE_TURN_BOUNDARY
        for message in provider_messages[0]
    )
    assert len(shadow) == 1
    shadow_uid, observed = shadow[0]
    assert shadow_uid == uid
    assert observed["job_id"] == job_id
    assert observed["lane"] == "manual_wake"
    assert observed["decision_allowed"] is True
    assert observed["apns_alert_sent"] is True
    assert isinstance(observed["decided_at"], float)


def test_wake_workspace_prompt_snapshot_is_loaded_once_across_rounds(
    monkeypatch,
):
    uid = "u_wake_workspace_prompt"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    responses = iter([
        {
            "reply": "",
            "tool_calls": [{
                "id": "read",
                "name": "memory_index",
                "args": {},
            }],
            "usage": {},
        },
        _text_round("workspace-aware wake"),
    ])
    provider_calls = []

    async def fake_provider(_config, messages, *, tools=None, **_kwargs):
        provider_calls.append({"messages": messages, "tools": tools})
        return next(responses)

    class _Result:
        def to_dict(self):
            return {"ok": True, "data": {"items": []}}

    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        fake_provider,
    )
    monkeypatch.setattr(
        cap_registry,
        "run_capability",
        lambda *_args, **_kwargs: _Result(),
    )
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, _text: {"id": "wake-reply"},
    )
    loader_calls = []
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.load_workspace_prompt = lambda _store, **kwargs: (
        loader_calls.append(kwargs["runtime_token"])
        or {
            "trusted_system_blocks": (
                "<feedling-skill>wake skill</feedling-skill>",
            ),
        }
    )

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "heartbeat",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        claimed_by,
    ))

    assert status == "completed"
    assert loader_calls == ["rt"]
    assert len(provider_calls) == 2
    assert all(
        "wake skill" in str(call["messages"])
        and "/memory/WORKING.md" not in str(call["messages"])
        for call in provider_calls
    )
    second_offered = {spec.name for spec in provider_calls[1]["tools"]}
    assert {"web_search", "web_fetch", "task"}.isdisjoint(second_offered)


def test_wake_workspace_prompt_failure_is_silent_before_provider(
    monkeypatch,
):
    uid = "u_wake_workspace_prompt_failure"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    shadow = []
    deps = _wake_deps(tail=[], has_genuine_user_history=lambda _uid: True)
    deps.record_wake_shadow_decision = (
        lambda uid, **kw: shadow.append((uid, kw)) or True
    )
    deps.load_workspace_prompt = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("private workspace plaintext"))
    )
    provider_called = {"value": False}

    async def provider(*_args, **_kwargs):
        provider_called["value"] = True
        return _text_round("must not happen")

    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        provider,
    )
    surface_called = {"value": False}
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda *_args, **_kwargs: surface_called.update(value=True),
    )

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "heartbeat",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        claimed_by,
    ))

    assert status == "failed"
    assert provider_called["value"] is False
    assert surface_called["value"] is False
    assert _job_status(job_id) == (
        "failed",
        "wake_failed:workspace_prompt_unavailable",
    )
    assert shadow == []


def test_run_wake_weak_wake_sleeps_no_bubble_no_error(monkeypatch):
    """Model declines to speak (empty terminal text) -> job completes cleanly,
    zero bubbles, and the D3 no-filler-adjacent invariant: no error
    status/callback. Unlike the chat lane, an empty terminal reply is NOT a
    failure for wake — it's a legitimate silence."""
    uid = "u_wake_weak"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    _script_provider(monkeypatch, [_text_round("")])
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    shadow = []
    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    deps.record_wake_shadow_decision = (
        lambda uid, **kw: shadow.append((uid, kw)) or True
    )
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))
    assert len(shadow) == 1
    assert shadow[0][1]["decision_allowed"] is False
    assert shadow[0][1]["apns_alert_sent"] is False


def test_wake_shadow_write_failure_cannot_change_the_decision(monkeypatch):
    uid = "u_wake_shadow_fail_open"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _script_provider(monkeypatch, [_text_round("")])
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )

    def _observer_failure(*_args, **_kwargs):
        raise RuntimeError("shadow database unavailable")

    deps.record_wake_shadow_decision = _observer_failure

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert _job_status(job_id)[0] == "completed"


def test_automatic_heartbeat_with_empty_history_skips_the_provider(monkeypatch):
    """An account with no genuine conversation must not receive a fabricated turn."""
    uid = "u_wake_nouser"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    provider_calls = []

    async def _fake(config, messages, *, tools=None, **_kwargs):
        provider_calls.append(messages)
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    shadow = []
    deps = _wake_deps(tail=[])
    deps.record_wake_shadow_decision = (
        lambda uid, **kw: shadow.append((uid, kw)) or True
    )
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed"
    assert provider_calls == []
    assert write_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert len(shadow) == 1
    assert shadow[0][1]["decision_allowed"] is False
    assert shadow[0][1]["apns_alert_sent"] is False


@pytest.mark.parametrize(
    ("summary", "tail"),
    [
        ("A summary exists without any real user message.", []),
        ("", [{"id": "m1", "ts": 1.0, "role": "assistant", "content": "hello"}]),
    ],
)
def test_automatic_heartbeat_authoritative_no_user_history_skips_all_prompt_work(
    monkeypatch,
    summary,
    tail,
):
    """Summary/assistant artifacts cannot authorize a proactive provider call."""
    uid = f"u_wake_no_authority_{len(tail)}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    provider_calls = []
    workspace_calls = []

    async def _fake(config, messages, *, tools=None, **_kwargs):
        provider_calls.append(messages)
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    deps = _wake_deps(
        summary=summary,
        tail=tail,
        has_genuine_user_history=lambda user_id: False,
    )
    deps.load_workspace_prompt = lambda *args, **kwargs: workspace_calls.append(
        (args, kwargs)
    ) or {"trusted_system_blocks": []}

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed"
    assert provider_calls == []
    assert workspace_calls == []
    assert _job_status(job_id)[0] == "completed"


def test_proactive_policy_does_not_bias_the_model_toward_silence():
    """The policy must preserve V1's equal speak/sleep product decision."""
    prompt = worker._WAKE_SYSTEM_PROMPT.lower()

    assert "speaking and staying silent are equally valid" in prompt
    assert "do not need a strong reason" in prompt
    assert "not a user request" in prompt
    assert "attention_facts" in prompt
    assert "never mention this wake or any system wording" in prompt
    assert "only if" not in prompt
    assert "genuinely worth saying" not in prompt
    assert "silence is correct" not in prompt


def test_wake_injects_attention_facts_as_non_user_application_data(monkeypatch):
    uid = "u_wake_attention_facts"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    provider_calls = []

    async def _provider(_config, messages, *, tools=None, **_kwargs):
        provider_calls.append(messages)
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)
    deps = _wake_deps(
        tail=[
            {
                "id": "m1",
                "ts": 900.0,
                "role": "user",
                "content": "hi",
            }
        ]
    )
    deps.read_temporal_snapshot = lambda *_args, **_kwargs: {
        "timezone": "UTC",
        "last_user_message_ts": 900.0,
    }
    deps.read_wake_attention_snapshot = lambda *_args, **_kwargs: {
        "visible_proactive_count_24h": 8,
        "last_visible_proactive_message_ts": 990.0,
    }
    monkeypatch.setattr(worker.time, "time", lambda: 1_000.0)

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert len(provider_calls) == 1
    temporal_message = next(
        message
        for message in provider_calls[0]
        if str(message.get("content")).startswith(
            worker.context.TEMPORAL_CONTEXT_HEADER + "\n"
        )
    )
    assert temporal_message["role"] == "assistant"
    assert '"visible_proactive_count_24h":8' in temporal_message["content"]
    assert not any(
        message["role"] == "user"
        and "attention_facts" in str(message.get("content"))
        for message in provider_calls[0]
    )


def test_run_wake_degenerate_reply_fails_silently(monkeypatch):
    uid = "u_wake_degenerate_reply"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    _script_provider(monkeypatch, [_text_round("。")])
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda store, text: (
            write_called.update(n=write_called["n"] + 1) or {"id": "never"}
        ),
    )
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda *args, **kwargs: surface_called.update(
            n=surface_called["n"] + 1
        ),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "failed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    assert _job_status(job_id) == (
        "failed",
        "wake_failed:degenerate_reply_suppressed",
    )
    assert not any(event["kind"] == "error" for event in _status_events(uid))


def test_heartbeat_thinking_only_is_successful_silence_without_backoff(
    monkeypatch,
):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_wake_thinking_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    calls = _script_provider(
        monkeypatch, [_text_round("<think>这次不打扰她了</think>")]
    )
    writes = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda *_args, **_kwargs: writes.append(True),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert _job_status(job_id) == ("completed", None)
    assert writes == []
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[0]["messages"]
        if message.get("role") == "system"
    )
    assert worker._OPTIONAL_WAKE_SELF_THINKING_INSTRUCTION in system_text
    assert "nothing after its closing tag" in system_text
    assert "the user receives nothing from that turn" in system_text
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is None or schedule["proactive_backoff_until"] is None


def test_scheduled_thinking_only_remains_a_must_deliver_failure(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_scheduled_thinking_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)
    calls = _script_provider(
        monkeypatch, [_text_round("<think>提醒必须送达</think>")]
    )
    writes = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda *_args, **_kwargs: writes.append(True),
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "scheduled",
            _wake_deps(tail=[]),
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "failed"
    # 2026-08-07:持久码从 empty_reply 拆出来 —— 模型给过完整 <think>、是我们
    # 剥空的,与「provider 什么都没给」必须能在 admin 上分开看(归因仍是 system)。
    assert _job_status(job_id) == ("failed", "wake_failed:thinking_only_no_reply")
    assert writes == []
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[0]["messages"]
        if message.get("role") == "system"
    )
    assert worker._OPTIONAL_WAKE_SELF_THINKING_INSTRUCTION not in system_text
    assert jobs_store.get_wake_schedule(uid)["proactive_backoff_until"] is not None


def test_run_scheduled_wake_prompts_with_the_exact_due_reminders(monkeypatch):
    uid = "u_wake_scheduled_notes"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)
    seen = {}

    async def _fake(config, messages, *, tools=None, **_kwargs):
        seen["messages"] = messages
        return _text_round("该喝水了，也记得拉伸一下。")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, _text: {"id": "scheduled-reply"},
    )
    deps = _wake_deps(tail=[])
    deps.read_scheduled_wake_context = lambda user_id, scheduled_job_id: [
        {
            "note": "提醒我喝水",
            "operation": "scheduled_wake",
            "status": "fired",
            "task_id": "timer-water",
            "next_trigger_at": "2026-07-27T08:00:00",
            "timezone": "Asia/Shanghai",
            "fired_at": 123.0,
        },
        {
            "note": "提醒我拉伸",
            "operation": "scheduled_wake",
            "status": "fired",
            "task_id": "timer-stretch",
            "next_trigger_at": "2026-07-27T08:00:00",
            "timezone": "Asia/Shanghai",
            "fired_at": 123.0,
        },
    ]

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "scheduled",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        claimed_by,
    ))

    assert status == "completed"
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in seen["messages"]
        if message.get("role") == "system"
    )
    assert worker._SCHEDULED_WAKE_SYSTEM_PROMPT in system_text
    user_text = "\n".join(
        str(message.get("content") or "")
        for message in seen["messages"]
        if message.get("role") == "user"
    )
    assert user_text == v2_context.PROACTIVE_TURN_BOUNDARY
    assert "提醒我喝水" not in user_text
    assert "提醒我拉伸" not in user_text
    runtime_text = "\n".join(
        str(message.get("content") or "")
        for message in seen["messages"]
        if message.get("role") == "assistant"
        and str(message.get("content") or "").startswith(
            v2_context.RUNTIME_CONTEXT_HEADER
        )
    )
    assert "提醒我喝水" in runtime_text
    assert "提醒我拉伸" in runtime_text
    runtime_payload = json.loads(runtime_text.split("\n", 1)[1])
    scheduled_wakes = runtime_payload["runtime_data"]["scheduled_wakes"]
    assert scheduled_wakes == [
        {
            "note": "提醒我喝水",
            "schedule_next_trigger_at": "2026-07-27T08:00:00",
            "schedule_operation": "scheduled_wake",
            "schedule_status": "fired",
            "schedule_task_id": "timer-water",
            "schedule_timezone": "Asia/Shanghai",
            "fired_at": 123.0,
        },
        {
            "note": "提醒我拉伸",
            "schedule_next_trigger_at": "2026-07-27T08:00:00",
            "schedule_operation": "scheduled_wake",
            "schedule_status": "fired",
            "schedule_task_id": "timer-stretch",
            "schedule_timezone": "Asia/Shanghai",
            "fired_at": 123.0,
        },
    ]
    with db.get_pool().connection() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_effect_outbox "
            "WHERE user_id=%s AND job_id=%s AND effect_type='reply'",
            (uid, job_id),
        ).fetchone()[0]
    events = payload["activity_events"]
    assert "提醒我喝水" not in json.dumps(payload, ensure_ascii=False)
    assert "提醒我拉伸" not in json.dumps(payload, ensure_ascii=False)
    assert [event["schedule_task_id"] for event in events] == [
        "timer-water",
        "timer-stretch",
    ]
    assert all(event["schedule_status"] == "fired" for event in events)
    assert "提醒我喝水" not in repr(events)
    assert "提醒我拉伸" not in repr(events)


def test_scheduled_wake_does_not_send_claude_48_an_assistant_prefill(monkeypatch):
    """Claude 4.6+ rejects a final assistant message with HTTP 400.

    Keep the reminder in assistant-role application data, but terminate the
    provider request with the fixed non-user transport marker. This reproduces
    the OpenRouter/Opus 4.8 failure seen on Test on 2026-08-14: without the
    boundary this fake provider raises exactly where the real route did.
    """
    uid = "u_wake_claude_48_prefill"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)
    config = provider_client.ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-opus-4.8",
        api_key="sk-user-byok",
        base_url="https://openrouter.ai/api/v1",
    )
    calls = []

    async def _claude_48(_config, messages, *, tools=None, **_kwargs):
        calls.append(messages)
        if messages[-1].get("role") == "assistant":
            raise provider_client.ProviderError(
                "provider_http_400: This model does not support assistant "
                "message prefill. The conversation must end with a user message.",
                status_code=400,
            )
        return _text_round("两分钟到了，该出门啦。")

    monkeypatch.setattr(provider_client, "chat_completion_async", _claude_48)
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, _text: {"id": "scheduled-claude-48-reply"},
    )
    deps = _wake_deps(tail=[])
    deps.read_scheduled_wake_context = lambda _user_id, _job_id: [
        {"note": "提醒我出门", "fired_at": 123.0}
    ]

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "scheduled",
            deps,
            config,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert len(calls) == 1
    assert calls[0][-1] == {
        "role": "user",
        "content": v2_context.PROACTIVE_TURN_BOUNDARY,
    }
    assert any(
        message.get("role") == "assistant"
        and "提醒我出门" in str(message.get("content") or "")
        for message in calls[0]
    )


def test_run_perception_wake_injects_trigger_as_untrusted_runtime_data(monkeypatch):
    uid = "u_wake_perception_context"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    seen = {}

    async def _fake(config, messages, *, tools=None, **_kwargs):
        seen["messages"] = messages
        return _text_round("")

    async def _empty_glance(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    monkeypatch.setattr(
        worker, "_perception_glance_grounding_results", _empty_glance
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.read_perception_wake_context = lambda user_id, wake_job_id: [{
        "wake_id": "wake-1",
        "source": "perception_event",
        "trigger": "arrived_at_anchor",
        "change_digest": "arrived near home",
        "origin_refs": ["location:home"],
        "presence_hints": {"moving": False},
        "created_at": 100.0,
    }]

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "heartbeat",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        claimed_by,
    ))

    assert status == "completed"
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in seen["messages"]
        if message.get("role") == "system"
    )
    assert "arrived near home" not in system_text
    runtime_messages = [
        message
        for message in seen["messages"]
        if message.get("role") == "assistant"
        and str(message.get("content") or "").startswith(
            v2_context.RUNTIME_CONTEXT_HEADER
        )
    ]
    assert len(runtime_messages) == 1
    assert [
        message.get("content")
        for message in seen["messages"]
        if message.get("role") == "user"
    ] == [v2_context.PROACTIVE_TURN_BOUNDARY]
    runtime_text = str(runtime_messages[0]["content"])
    assert '"perception_wake"' in runtime_text
    assert "arrived_at_anchor" in runtime_text
    assert "anchor_changed" in runtime_text
    assert "arrived near home" not in runtime_text
    assert "location:home" not in runtime_text
    assert "A recent perception change may be worth responding to." not in str(
        seen["messages"]
    )


@pytest.mark.parametrize(
    ("trigger", "expected_require_reply", "prompt_fragment"),
    [
        ("broadcast_opened", False, "Speaking and staying silent are equally valid"),
        ("broadcast_closed", False, "Speaking and staying silent are equally valid"),
    ],
)
def test_broadcast_edge_wake_reply_policy(
    monkeypatch, trigger, expected_require_reply, prompt_fragment
):
    uid = f"u_wake_{trigger}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    policy = _seen_lane_policy(monkeypatch)
    calls = _script_provider(monkeypatch, [_text_round("我在，可以开始了。")])

    async def _empty_glance(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(
        worker, "_perception_glance_grounding_results", _empty_glance
    )
    monkeypatch.setattr(worker, "_screen_share_grounding", lambda _uid: {})
    monkeypatch.setattr(
        worker, "_write_encrypted_reply", lambda store, text: {"id": "r"}
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.read_perception_wake_context = lambda user_id, wake_job_id: [{
        "wake_id": f"wake-{trigger}",
        "source": "scene_change",
        "trigger": trigger,
        "change_digest": "broadcast state changed",
        "origin_refs": ["ios_report:broadcast"],
        "created_at": 100.0,
    }]

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert policy["require_reply"] is expected_require_reply
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[0]["messages"]
        if message.get("role") == "system"
    )
    assert prompt_fragment in system_text


def test_lost_heartbeat_lease_does_not_persist_glance_fingerprint(monkeypatch):
    """Catches a candidate write that happens before terminalization wins."""
    uid = "u_glance_lost_lease"
    conftest.seed_user(uid)
    _reset(uid)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply", lambda store, text: {"id": "r"}
    )

    async def fake_provider(*args, **kwargs):
        return _text_round("")

    async def fake_cap_data(store, action_type, **kwargs):
        assert action_type == "perception_glance"
        return {
            "glance": {
                "weather": {"available": True, "notable_change": False}
            }
        }

    upserts = []
    monkeypatch.setattr(provider_client, "chat_completion_async", fake_provider)
    monkeypatch.setattr(worker, "_cap_data", fake_cap_data)
    monkeypatch.setattr(
        jobs_store,
        "finish_wake_job",
        lambda *args, **kwargs: (False, None),
    )
    monkeypatch.setattr(
        jobs_store,
        "upsert_runtime_state",
        lambda *args, **kwargs: upserts.append((args, kwargs)),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.read_perception_wake_context = lambda uid, job_id: []
    jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")
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
    assert upserts == []


def test_ordinary_heartbeat_gives_fingerprint_to_atomic_finish(monkeypatch):
    """The worker must not terminalize and persist in separate store calls."""
    uid = "u_glance_atomic_finish"
    conftest.seed_user(uid)
    _reset(uid)

    async def fake_provider(*args, **kwargs):
        return _text_round("")

    glance = {"weather": {"available": True, "notable_change": False}}
    fingerprint = worker.perception_glance_fingerprint(glance)

    async def fake_cap_data(store, action_type, **kwargs):
        assert action_type == "perception_glance"
        return {"glance": glance}

    finishes = []

    def fake_finish(*args, **kwargs):
        finishes.append((args, kwargs))
        return True, None

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_provider)
    monkeypatch.setattr(worker, "_cap_data", fake_cap_data)
    monkeypatch.setattr(jobs_store, "finish_wake_job", fake_finish)
    monkeypatch.setattr(
        jobs_store,
        "upsert_runtime_state",
        lambda *args, **kwargs: pytest.fail("separate fingerprint upsert"),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.read_perception_wake_context = lambda uid, job_id: []
    jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")

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
    assert len(finishes) == 1
    assert finishes[0][1]["completed_perception_glance_fingerprint"] == fingerprint


def test_ordinary_heartbeat_final_reply_persists_glance_before_finish(
    monkeypatch,
):
    """The final effect owns the marker before the worker resumes completion."""
    uid = "u_glance_atomic_reply_finish"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)

    glance = {"weather": {"available": True, "notable_change": False}}
    fingerprint = worker.perception_glance_fingerprint(glance)

    async def fake_cap_data(store, action_type, **kwargs):
        assert action_type == "perception_glance"
        return {"glance": glance}

    async def fake_provider(*args, **kwargs):
        return _text_round("A quiet proactive reply.")

    def fake_envelope(_store, _text, *, item_id=None):
        return (
            {
                "v": 1,
                "id": str(item_id),
                "owner_user_id": uid,
                "visibility": "shared",
                "body_ct": "ciphertext",
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        )

    real_finish = jobs_store.finish_wake_job
    finish_calls = []

    def assert_effect_committed_first(*args, **kwargs):
        finish_calls.append((args, kwargs))
        assert jobs_store.get_runtime_state(uid) == {
            "last_completed_perception_glance_fingerprint": fingerprint,
            "last_completed_perception_glance_source_job_id": job_id,
        }
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_provider)
    monkeypatch.setattr(worker, "_cap_data", fake_cap_data)
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        fake_envelope,
    )
    monkeypatch.setattr(
        jobs_store,
        "finish_wake_job",
        assert_effect_committed_first,
    )
    deps = worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: [
            {"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}
        ],
        has_genuine_user_history=lambda _uid: True,
        read_messages_after_seq=lambda uid, after_seq: [],
        read_perception_wake_context=lambda uid, job_id: [],
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
    )

    status = asyncio.run(
        worker._run_wake(
            job_id,
            uid,
            "heartbeat",
            deps,
            _BYOK,
            asyncio.Semaphore(4),
            claimed_by,
        )
    )

    assert status == "completed"
    assert len(finish_calls) == 1


def test_heartbeat_without_context_reader_does_not_persist_after_failed_completion(
    monkeypatch,
):
    """Catches optional-reader heartbeats treating failed completion as success."""
    uid = "u_glance_no_context_reader_lost_lease"
    conftest.seed_user(uid)
    _reset(uid)

    async def fake_provider(*args, **kwargs):
        return _text_round("")

    async def fake_cap_data(store, action_type, **kwargs):
        assert action_type == "perception_glance"
        return {
            "glance": {
                "weather": {"available": True, "notable_change": False}
            }
        }

    upserts = []
    monkeypatch.setattr(provider_client, "chat_completion_async", fake_provider)
    monkeypatch.setattr(worker, "_cap_data", fake_cap_data)
    monkeypatch.setattr(jobs_store, "mark_completed", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        jobs_store,
        "upsert_runtime_state",
        lambda *args, **kwargs: upserts.append((args, kwargs)),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    assert deps.read_perception_wake_context is None
    jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")
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
    assert upserts == []


def test_generation_change_before_atomic_heartbeat_completion_fences_fingerprint(
    monkeypatch,
):
    """A stale source cannot complete or write after its runtime generation."""
    uid = "u_glance_generation_fence"
    conftest.seed_user(uid)
    _reset(uid)
    monkeypatch.setattr(
        worker, "_write_encrypted_reply", lambda store, text: {"id": "r"}
    )

    async def fake_provider(*args, **kwargs):
        return _text_round("")

    async def fake_cap_data(store, action_type, **kwargs):
        assert action_type == "perception_glance"
        return {
            "glance": {
                "weather": {"available": True, "notable_change": False}
            }
        }

    real_finish_wake_job = jobs_store.finish_wake_job

    def advance_generation_then_finish(*args, **kwargs):
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_runtime_state "
                "SET runtime_generation=runtime_generation+1 "
                "WHERE user_id=%s",
                (uid,),
            )
        result = real_finish_wake_job(*args, **kwargs)
        assert result == (False, None)
        return result

    monkeypatch.setattr(provider_client, "chat_completion_async", fake_provider)
    monkeypatch.setattr(worker, "_cap_data", fake_cap_data)
    monkeypatch.setattr(
        jobs_store,
        "finish_wake_job",
        advance_generation_then_finish,
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.read_perception_wake_context = lambda uid, job_id: []
    jobs_store.enqueue_job(uid, "heartbeat")
    job = jobs_store.claim_next_job("w")
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
    assert (
        "last_completed_perception_glance_fingerprint"
        not in jobs_store.get_runtime_state(uid)
    )


def test_run_perception_wake_hands_late_context_to_successor(monkeypatch):
    from perception import store as perception_store

    uid = "u_wake_perception_late_context"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, coalesced = jobs_store.enqueue_job_with_context_log(
        uid,
        "heartbeat",
        reason="arrived_at_anchor",
        trace_id="wake-first",
        context_stream=perception_store.V2_WAKE_CONTEXT_STREAM,
        context_doc={
            "wake_id": "wake-first",
            "source": "perception_event",
            "trigger": "arrived_at_anchor",
            "change_digest": "arrived home",
            "origin_refs": ["location:home"],
            "presence_hints": {},
            "created_at": 100.0,
        },
        context_ts=100.0,
    )
    assert coalesced is False
    claimed_by = _claim(job_id)
    assert jobs_store.mark_running(job_id, claimed_by=claimed_by)
    written = []
    monkeypatch.setattr(
        worker,
        "_write_encrypted_reply",
        lambda _store, text: written.append(text) or {"id": "late-reply"},
    )
    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda _store, _text, *, item_id=None: (
            {
                "v": 1,
                "id": str(item_id),
                "owner_user_id": uid,
                "visibility": "shared",
                "body_ct": "ciphertext",
                "nonce": "nonce",
                "K_user": "sealed-user-key",
                "K_enclave": "sealed-enclave-key",
            },
            "",
        ),
    )

    async def _fake(config, messages, *, tools=None, **_kwargs):
        late_job_id, late_coalesced = jobs_store.enqueue_job_with_context_log(
            uid,
            "heartbeat",
            reason="unlock_after_absence",
            trace_id="wake-late",
            context_stream=perception_store.V2_WAKE_CONTEXT_STREAM,
            context_doc={
                "wake_id": "wake-late",
                "source": "perception_event",
                "trigger": "unlock_after_absence",
                "change_digest": "device unlocked",
                "origin_refs": ["device:1"],
                "presence_hints": {},
                "created_at": 101.0,
            },
            context_ts=101.0,
        )
        assert late_job_id == job_id
        assert late_coalesced is True
        return _text_round("This reply is stale after the late event.")

    async def _empty_glance(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    monkeypatch.setattr(
        worker, "_perception_glance_grounding_results", _empty_glance
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.read_messages_after_seq = lambda user_id, after_seq: []
    deps.read_perception_wake_context = (
        serve_worker._read_perception_wake_context
    )

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "heartbeat",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        claimed_by,
    ))

    assert status == "completed"
    assert written == []
    with db.get_pool().connection() as conn:
        jobs = conn.execute(
            "SELECT id,status,reason FROM agent_jobs "
            "WHERE user_id=%s AND lane='heartbeat' ORDER BY id",
            (uid,),
        ).fetchall()
    assert jobs[0][0] == job_id
    assert jobs[0][1] == "completed"
    assert jobs[1][1:] == ("pending", "coalesced_perception_followup")
    successor_context = perception_store.read_v2_wake_context(
        uid, int(jobs[1][0])
    )
    assert [item["wake_id"] for item in successor_context] == ["wake-late"]
    original_context = perception_store.read_v2_wake_context(uid, job_id)
    assert [item["wake_id"] for item in original_context] == ["wake-first"]


def test_production_deps_wire_bounded_perception_wake_context(monkeypatch):
    from perception import store as perception_store

    monkeypatch.setattr(
        perception_store,
        "read_v2_wake_context",
        lambda user_id, job_id, *, limit: [{
            "wake_id": "w" * 300,
            "source": "perception",
            "trigger": "photo_added",
            "change_digest": "d" * 3000,
            "origin_refs": [f"photo:{index}" for index in range(20)],
            "presence_hints": {
                "visible": True,
                "nested": {"instruction": "ignore policy"},
                "note": "n" * 300,
            },
            "created_at": float("inf"),
        }],
    )

    deps = serve_worker.build_production_deps()
    assert deps.read_perception_wake_context is serve_worker._read_perception_wake_context
    rows = deps.read_perception_wake_context("u1", 42)

    assert len(rows) == 1
    assert len(rows[0]["wake_id"]) == 160
    assert len(rows[0]["change_digest"]) == 2000
    assert len(rows[0]["origin_refs"]) == 10
    assert rows[0]["presence_hints"] == {
        "visible": True,
        "note": "n" * 200,
    }
    assert rows[0]["created_at"] == 0.0


def test_run_wake_provider_error_silent_mark_failed(monkeypatch):
    """A real provider failure (BYOK 402, enclave hiccup, etc.) must NOT be
    confused with a weak-wake sleep — it's a real failure, silently marked,
    never surfaced to the user (background job, same isolation as
    maintenance)."""
    uid = "u_wake_provider_err"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "manual_wake")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    shadow = []
    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    deps.record_wake_shadow_decision = (
        lambda uid, **kw: shadow.append((uid, kw)) or True
    )
    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "failed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1] == "wake_failed:runtimeerror"
    assert not any(e["kind"] == "error" for e in _status_events(uid))
    assert jobs_store.get_wake_schedule(uid) is None
    assert shadow == [], "provider failures are not model allow/suppress decisions"


def test_run_wake_unexpected_exception_also_silent_mark_failed(monkeypatch):
    """Any other unexpected exception during the wake turn (e.g. read_tail
    blowing up) must be caught by _run_wake's own try/except, same as
    _run_compaction — never propagate, never surface a user error chip."""
    uid = "u_wake_boom"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    def _boom_read_summary(uid_):
        raise RuntimeError("tail read exploded")

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        has_genuine_user_history=lambda _uid: True,
        read_summary_with_seq=_boom_read_summary,
        read_tail_after_seq=lambda *_args, **_kwargs: [],
    )
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "failed"
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert "wake_failed" in (row[1] or "")


def test_run_wake_tolerates_missing_read_summary_read_tail(monkeypatch):
    """Mirrors _run_compaction's degrade-gracefully contract for deps without
    read_summary/read_tail wired (defaults None): falls back to empty summary/
    tail rather than crashing on a None call."""
    uid = "u_wake_nodeps"
    conftest.seed_user(uid)
    _reset(uid)
    failed_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    failed_owner = _claim(failed_id)
    assert jobs_store.mark_failed(
        failed_id,
        "wake_failed:runtimeerror",
        claimed_by=failed_owner,
        wake_backoff_base_sec=60,
        wake_backoff_cap_sec=3600,
        wake_backoff_now=time.time(),
    )
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    provider_calls = []

    async def _fake(config, messages, *, tools=None, **_kwargs):
        provider_calls.append(messages)
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        # read_tail/read_summary left at their TurnDeps default of None.
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed"
    assert provider_calls == []
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule["proactive_fail_streak"] == 0
    assert schedule["proactive_backoff_until"] is None


# ------------------------------------------------------------------
# D3 Task 7: a "provider_config"-kind failure (dead/broke BYOK key: 402/401/403
# — classified via provider_client.classify_provider_error, Task 8's
# replacement for the old ResponderError.kind mechanism) must write a
# payment_cooldown_until on the wake schedule BEFORE the silent mark_failed,
# so the scheduler's due_heartbeat_users stops re-firing wakes at a key that
# cannot succeed until the user fixes it. A "transient"-kind error must NOT
# set a cooldown — it's just a blip, not a config problem.
# ------------------------------------------------------------------

def test_run_wake_provider_config_error_sets_payment_cooldown(monkeypatch):
    uid = "u_wake_provider_config"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("out of credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    before = time.time()
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))
    after = time.time()

    assert status == "failed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "failed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))

    assert len(cooldown_calls) == 1
    called_uid, kwargs = cooldown_calls[0]
    assert called_uid == uid
    assert "payment_cooldown_until" in kwargs
    cooldown_at = kwargs["payment_cooldown_until"]
    assert before + worker._WAKE_COOLDOWN_SEC - 5 <= cooldown_at <= after + worker._WAKE_COOLDOWN_SEC + 5

    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is not None
    assert schedule["payment_cooldown_until"] is not None


def test_run_wake_rollback_blocks_provider_cooldown_write(monkeypatch):
    uid = "u_wake_provider_rollback"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    # First check (the fence right before the tool loop starts) passes; the
    # SECOND check (inside the payment-cooldown fence, after the provider
    # call fails) is where the rollback lands and must block the write.
    mode_checks = iter([True, False])
    cooldown_calls = []
    monkeypatch.setattr(
        jobs_store,
        "upsert_wake_schedule",
        lambda *a, **k: cooldown_calls.append((a, k)),
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.runtime_mode_enabled = lambda _uid: next(mode_checks)

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "failed"
    assert cooldown_calls == []
    assert jobs_store.get_wake_schedule(uid) is None


def test_run_wake_lost_lease_blocks_provider_cooldown_write(monkeypatch):
    uid = "u_wake_provider_lost_lease"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("credits", status_code=402)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *a, **k: False)
    cooldown_calls = []
    monkeypatch.setattr(
        jobs_store,
        "upsert_wake_schedule",
        lambda *a, **k: cooldown_calls.append((a, k)),
    )

    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK,
        asyncio.Semaphore(4), claimed_by))

    assert status == "failed"
    assert cooldown_calls == []
    assert jobs_store.get_wake_schedule(uid) is None


def test_run_wake_transient_error_does_not_set_payment_cooldown(monkeypatch):
    uid = "u_wake_transient"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    async def _boom(config, messages, *, tools=None, **_kwargs):
        raise provider_client.ProviderError("timed out", status_code=503)

    monkeypatch.setattr(provider_client, "chat_completion_async", _boom)

    cooldown_calls = []
    orig_upsert = jobs_store.upsert_wake_schedule

    def _spy_upsert(user_id_, **kw):
        cooldown_calls.append((user_id_, kw))
        return orig_upsert(user_id_, **kw)

    monkeypatch.setattr(jobs_store, "upsert_wake_schedule", _spy_upsert)

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "failed"
    assert _job_status(job_id)[0] == "failed"
    assert cooldown_calls == []
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule["payment_cooldown_until"] is None
    assert schedule["proactive_fail_streak"] == 1
    assert schedule["proactive_backoff_until"] > time.time()


# ------------------------------------------------------------------
# process_job dispatch: heartbeat/scheduled/manual_wake route to _run_wake,
# NOT the chat coalesce path.
# ------------------------------------------------------------------

@pytest.mark.parametrize("lane", ["heartbeat", "scheduled", "manual_wake"])
def test_process_job_dispatches_wake_lanes_to_run_wake_not_chat_path(monkeypatch, lane):
    uid = f"u_wake_dispatch_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    coalesce_calls = {"n": 0}
    orig_coalesce = v2_coalesce.coalesce_pending

    def _counting_coalesce(*a, **k):
        coalesce_calls["n"] += 1
        return orig_coalesce(*a, **k)

    monkeypatch.setattr(v2_coalesce, "coalesce_pending", _counting_coalesce)

    _script_provider(monkeypatch, [_text_round("a proactive nudge")])
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=lambda uid_, after_ts, limit: [
            {"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}
        ],
        has_genuine_user_history=lambda _uid: True,
        apply_pending_effects=_apply_effects,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert coalesce_calls["n"] == 0
    assert written["text"] == "a proactive nudge"
    assert _job_status(job_id)[0] == "completed"


def test_wake_tells_the_provider_that_an_empty_reply_is_acceptable(monkeypatch):
    """The "weak wake sleeps" contract only holds if the lane ASKS for it.

    Every other wake test stubs `chat_completion_async` to hand back an empty
    reply, which silently assumes provider_client would do that. It does not:
    with the default `require_reply=True`, `_extract_anthropic_reply` (and its
    openai/gemini/bedrock siblings) RAISE
    `ProviderError("provider response had no usable reply text")` on a 2xx
    whose content carries no text — `required = require_reply and not
    tool_calls`. So a model that chooses to stay silent, which is the entire
    point of a wake, is reported as a provider failure.

    Observed on test 2026-07-28: manual_wake failed 3/3 with
    `wake_failed:providererror` while anthropic returned 200 OK on every call.
    Nobody noticed because `_run_wake` fails silently by design (background
    job: no error chip, no bubble) — users just experience a companion that
    never reaches out.

    This stub mirrors provider_client's REAL contract instead of assuming it.
    """
    uid = "u_wake_require_reply"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    seen = {}

    async def _contract_faithful(config, messages, *, tools=None,
                                 require_reply=True, **kwargs):
        seen["require_reply"] = require_reply
        if require_reply:
            # What the real client does with a text-free 2xx body.
            raise provider_client.ProviderError(
                "provider response had no usable reply text")
        return _text_round("")

    monkeypatch.setattr(provider_client, "chat_completion_async", _contract_faithful)
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    # 这一条断言的**主体是 tool_loop**，不是 wake lane 的策略：a0ac5257（V2 空回复
    # 恢复）之后 `run_tool_loop` 对 provider_client 恒传 `require_reply=False`
    # （"V2 owns the lane-specific empty-response policy"），自己在 1156+ 判 lane。
    # 所以它锁的是「V2 不让 provider 解析器替我们决定空回复算不算失败」，**不能**
    # 用来推断 `_run_wake` 传了什么——那一跳由 `test_only_scheduled_wake_demands_
    # a_reply` 的 `_seen_lane_policy` 绑住。两者别混。
    assert seen.get("require_reply") is False, seen
    # 沉默即成功：睡了，没失败，也没写气泡。
    assert status == "completed"
    assert _job_status(job_id)[0] == "completed"
    assert write_called["n"] == 0


def _empty_round(*, stop_reason="end_turn"):
    """A structurally valid provider success with no usable output.

    `stop_reason` 必须非空：`tool_loop._empty_response_shape` 旁边的 `semantic_empty`
    判据要求 reasoning/stop_reason 至少有一个有值，否则纠正重试根本不会 arm
    （直接判死）。造样本时漏了它，测的就不是恢复链而是快速失败路径。
    """
    round_ = _text_round("")
    round_["stop_reason"] = stop_reason
    return round_


def _seen_lane_policy(monkeypatch):
    """记录 `_run_wake` 交给 `run_tool_loop` 的 lane-specific seams。"""
    seen = {}
    orig = worker.v2_tool_loop.run_tool_loop

    async def _spy(*a, **kw):
        seen["require_reply"] = kw.get("require_reply")
        seen["on_provider_tool_surface"] = kw.get("on_provider_tool_surface")
        return await orig(*a, **kw)

    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", _spy)
    return seen


@pytest.mark.parametrize(
    "lane,expected_require_reply",
    [
        ("scheduled", True),
        ("heartbeat", False),
        ("manual_wake", False),
        ("screen_watch", False),
    ],
)
def test_only_scheduled_wake_demands_a_reply(monkeypatch, lane, expected_require_reply):
    """心跳沉默=成功，定时提醒沉默=提醒丢了。两条道必须传不同的策略。

    参数化而不是写两个用例：这个不对称本身就是被测对象，分开写的话有人只改
    一个、另一个照绿。四条 wake lane 全列进来（不只被改的那条）——用例名说
    "only"，就得真的把「其余 lane 零变化」也绑住，否则以后有人顺手把
    manual_wake 也打开，没有任何测试会红（codex 复验 2026-08-10 提出）。
    """
    uid = f"u_wake_policy_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed_by = _claim(job_id)

    seen = _seen_lane_policy(monkeypatch)
    _script_provider(monkeypatch, [_text_round("something to say")])
    monkeypatch.setattr(
        worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.emit_debug_trace = lambda *_args, **_kwargs: None

    asyncio.run(worker._run_wake(
        job_id, uid, lane, deps,
        _BYOK, asyncio.Semaphore(4), claimed_by))
    assert deps is not None

    assert seen["require_reply"] is expected_require_reply, seen
    assert seen["on_provider_tool_surface"] is not None, seen


def test_wake_provider_tool_surface_trace_carries_wake_kind(monkeypatch):
    uid = "u_wake_provider_surface_trace"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)
    traces = []
    _script_provider(monkeypatch, [_text_round("scheduled reply")])
    monkeypatch.setattr(
        worker, "_write_encrypted_reply", lambda store, text: {"id": "r"}
    )
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "type": event_type, **fields}
    )

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "scheduled",
        deps,
        _BYOK,
        asyncio.Semaphore(4),
        claimed_by,
    ))

    assert status == "completed"
    surface = next(
        trace for trace in traces if trace["type"] == "mcp.surface.provider"
    )
    assert surface["detail"]["lane"] == "scheduled"
    assert surface["detail"]["wake_kind"] == "scheduled"
    assert surface["detail"]["sent_tool_count"] > 0


def test_scheduled_wake_empty_reply_fails_instead_of_completing_silently(monkeypatch):
    """usr_4ea3de33c049a676，2026-08-10 prod，一晚上两次。

    用户让 TA「20 秒后发个消息」，模型（deepseek）返空。旧行为：`require_reply=False`
    ⇒ 空回复不被检测 ⇒ 落到 `_run_wake` 的 `if not text: return` ⇒ job 记
    **completed**、无气泡、无失败、lane 指标全绿，用户干等着。同一模型的同一种
    空回复走聊天道时是可见的 `turn_failed:empty_reply`——两条道对同一个事实
    给出相反的结论，而只有静默的那条是用户在踩的。

    这里同时锁两层：不许静默成功，而且最终失败必须进入 durable visibility
    outbox；否则后台虽然红了，手机端仍和旧行为一样什么都看不到。
    """
    uid = "u_wake_sched_empty"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)

    calls = _script_provider(monkeypatch, [_empty_round(), _empty_round()])
    surfaced = []
    monkeypatch.setattr(
        worker,
        "_surface_terminal_error",
        lambda deps, user_id, failed_job_id, code: surfaced.append(
            (user_id, failed_job_id, code)
        ),
    )
    write_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_called.update(n=write_called["n"] + 1) or {"id": "r"})

    traces = []
    deps = _wake_deps(
        tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    )
    deps.emit_debug_trace = lambda user_id, event_type, **fields: traces.append(
        {"user_id": user_id, "type": event_type, **fields}
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "scheduled",
        deps,
        _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status != "completed", "定时提醒返空竟然算成功了——正是本用例要挡的回归"
    st, last_error = _job_status(job_id)
    assert st == "failed", st
    assert "empty_reply" in str(last_error), last_error
    assert write_called["n"] == 0
    # 先重试一轮再判死：判死前必须真的给过第二次机会，否则这就退化成
    # 「把静默成功换成快速失败」，用户拿到的东西一样少。
    assert len(calls) == 2, calls
    correction_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[1]["messages"]
        if message.get("role") == "system"
    )
    assert worker._SCHEDULED_WAKE_EMPTY_RESPONSE_CORRECTION in correction_text
    assert surfaced == [(uid, job_id, "wake_failed:empty_reply")]
    with db.get_pool().connection() as conn:
        marker = conn.execute(
            "SELECT error_code,error_class,reply_frontier_seq,"
            "reply_parent_message_id FROM v2_terminal_failure_outbox "
            "WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert marker == (
        "wake_failed:empty_reply",
        "provider_empty_reply",
        None,
        None,
    )
    diagnostics = [
        trace for trace in traces if trace["type"] == "provider.empty_response"
    ]
    assert len(diagnostics) == 2
    assert all(trace["detail"] == {
        "stop_reason": "end_turn",
        "has_visible_text": False,
        "reasoning_present": False,
        "tool_call_count": 0,
        "completion_tokens": 1,
        "lane": "scheduled",
    } for trace in diagnostics)


def test_scheduled_wake_empty_reply_recovers_on_the_correction_retry(monkeypatch):
    """恢复链的正例：第一轮返空，纠正提示重试后拿到正文 —— 用户照常收到提醒。

    这是本次改动真正的收益。只测失败那条会让人误以为代价是「更多失败」，
    实际上多数抽风到这一步就结束了。
    """
    uid = "u_wake_sched_recover"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)

    calls = _script_provider(
        monkeypatch, [_empty_round(), _text_round("该喝水啦")])
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker._run_wake(
        job_id, uid, "scheduled",
        _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]),
        _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed", _job_status(job_id)
    assert written.get("text") == "该喝水啦"
    assert len(calls) == 2, calls


# ------------------------------------------------------------------
# 世界书:主动开口的四条道也要认得这个世界（Seven 2026-08-10）
#
# 匹配信号按道分。锁死的是「传给匹配器的 messages」——那正是两半语义的分界：
# 空 messages ⇒ `worldbook_match._triggered` 只留 alwaysOn；非空 ⇒ 额外按关键词命中。
# ------------------------------------------------------------------

def _wake_deps_with_worldbook(seen, *, block="〈世界书〉墨白历,一年十四个月。", boom=False,
                              tail=None, **kw):
    def _read(user_id, messages, *, runtime_token):
        seen["messages"] = list(messages or [])
        seen["n"] = seen.get("n", 0) + 1
        if boom:
            raise RuntimeError("worldbook_match_failed")
        return {"block": block, "matched_names": ["历法常识"]}

    deps = _wake_deps(tail=tail if tail is not None else
                      [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}], **kw)
    return deps.__class__(**{**deps.__dict__, "read_worldbook_context": _read})


@pytest.mark.parametrize("lane", ["heartbeat", "manual_wake", "screen_watch"])
def test_wake_lanes_without_fresh_text_ask_for_alwayson_only(monkeypatch, lane):
    """无新鲜文本的三条道:必须**照样去取**世界书,但不给匹配信号。

    两件事一起锁:
    1. 去取了(n==1)——chat 道那句 `if worldbook_match_messages:` 短路若被照抄过来,
       这三条道会一条 alwaysOn 都拿不到,正好抹掉本次改动的主要目的;
    2. messages 为空——空扫描面下 `_triggered` 只放行 alwaysOn。拿几小时前的旧消息
       去撞关键词是噪音,不是接地;screen_watch 更是刻意不用屏幕文本(不可信输入
       不许决定 prompt 内容,见 worker.py 的 screen_recent pull-only 注释)。
    """
    uid = f"u_wb_wake_{lane}"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    claimed_by = _claim(job_id)

    seen = {}
    calls = _script_provider(monkeypatch, [_text_round("在想你呢")])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    status = asyncio.run(worker._run_wake(
        job_id, uid, lane, _wake_deps_with_worldbook(seen),
        _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed", _job_status(job_id)
    assert seen.get("n") == 1, f"{lane} 没有去取世界书: {seen}"
    assert seen["messages"] == [], f"{lane} 不该给匹配信号: {seen['messages']}"
    # 取回来的块要真的进 prompt,而不是取了就扔。
    # ⚠️ 断言必须在**消息正文**里找,不能 json.dumps 之后再找:header 自带真实
    # 换行,dumps 会把它转义成 "\\n" 两个字符,于是永远匹配不上——2026-08-10
    # 我第一版就这么写,红了三条,差点误判成「功能没接上」。
    blocks = [str(m.get("content") or "") for m in calls[0]["messages"]]
    assert any(v2_context.WORLD_BOOK_CONTEXT_HEADER in b for b in blocks), blocks[:3]
    assert any("墨白历" in b for b in blocks), blocks[:3]


def test_scheduled_wake_matches_the_reminder_note(monkeypatch):
    """定时道**有**新鲜文本:提醒正文。它本来就已逐字进 prompt,拿它匹配零新增暴露面。

    用户说「提醒我去青岚学院上课」,到点那一刻理应认得青岚学院——这正是关键词
    触发条目该发挥作用的场景,也是本改动里唯一给匹配信号的道。
    """
    uid = "u_wb_wake_sched"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)

    seen = {}
    _script_provider(monkeypatch, [_text_round("该去上课啦")])
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    deps = _wake_deps_with_worldbook(seen)
    deps = deps.__class__(**{
        **deps.__dict__,
        "read_scheduled_wake_context": lambda uid_, job_: [
            {"note": "提醒他去青岚学院上课", "timer_id": "t1"}
        ],
    })

    status = asyncio.run(worker._run_wake(
        job_id, uid, "scheduled", deps, _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed", _job_status(job_id)
    contents = [m.get("content") for m in seen.get("messages") or []]
    assert any("青岚学院" in str(c) for c in contents), seen.get("messages")


def test_worldbook_failure_does_not_kill_the_wake(monkeypatch):
    """世界书取不到是 best effort(与 chat 道同款):一次主动开口不该因此整个打掉。"""
    uid = "u_wb_wake_boom"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    seen = {}
    calls = _script_provider(monkeypatch, [_text_round("在想你呢")])
    written = {}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", _wake_deps_with_worldbook(seen, boom=True),
        _BYOK, asyncio.Semaphore(4), claimed_by))

    assert status == "completed", _job_status(job_id)
    assert written.get("text") == "在想你呢"
    blocks = [str(m.get("content") or "") for m in calls[0]["messages"]]
    assert not any(v2_context.WORLD_BOOK_CONTEXT_HEADER in b for b in blocks), blocks[:3]
