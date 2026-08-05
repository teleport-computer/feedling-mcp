"""Wake-lane processing on the unified `tool_loop.run_tool_loop`.

`_run_wake` mirrors `_run_compaction`'s self-contained shape (own try/except,
silent `mark_failed`, never `_surface_terminal_error`, never a chat bubble on
failure) but on a SUCCESSFUL model-authored reply it DOES write an encrypted
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
a failure — silent `mark_failed`, still no user-visible error chip
(background job, same isolation as maintenance).

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

import conftest
import db
import provider_client
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker


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
        read_summary=lambda uid: (summary, 0.0, 0),
        has_genuine_user_history=has_genuine_user_history,
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


# ------------------------------------------------------------------
# _run_wake direct unit coverage
# ------------------------------------------------------------------

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
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    assert written == {"text": "hey, how did that go?", "user_id": uid}
    assert _job_status(job_id)[0] == "completed"
    system_msg = next(m for m in seen["messages"] if m["role"] == "system")
    assert worker._WAKE_SYSTEM_PROMPT in system_msg["content"]
    assert [
        message["content"]
        for message in seen["messages"]
        if message.get("role") == "user"
    ] == ["hi"]


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
    deps = worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after_ts, limit: [],
        read_summary=lambda uid: ("", 0.0, 0),
        read_messages_after_seq=lambda uid, after_seq: [],
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        send_reply_push=lambda uid, **kw: pushes.append((uid, kw)),
    )

    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

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
    assert not any(message.get("role") == "user" for message in provider_messages[0])


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
            "working_memory": "wake scratch",
        }
    )

    status = asyncio.run(worker._run_wake(
        job_id,
        uid,
        "heartbeat",
        deps,
        _BYOK,
        worker.ENCLAVE_SEMAPHORE,
        claimed_by,
    ))

    assert status == "completed"
    assert loader_calls == ["rt"]
    assert len(provider_calls) == 2
    assert all(
        "wake skill" in str(call["messages"])
        and "wake scratch" not in str(call["messages"])
        and "/memory/WORKING.md" in str(call["messages"])
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
    deps = _wake_deps(tail=[])
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
        worker.ENCLAVE_SEMAPHORE,
        claimed_by,
    ))

    assert status == "failed"
    assert provider_called["value"] is False
    assert surface_called["value"] is False
    assert _job_status(job_id) == (
        "failed",
        "wake_failed:workspace_prompt_unavailable",
    )


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

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"
    assert not any(e["kind"] == "error" for e in _status_events(uid))


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

    deps = _wake_deps(tail=[])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "completed"
    assert provider_calls == []
    assert write_called["n"] == 0
    assert _job_status(job_id)[0] == "completed"


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
    ) or {"trusted_system_blocks": [], "working_memory": ""}

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

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
            worker.ENCLAVE_SEMAPHORE,
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
            worker.ENCLAVE_SEMAPHORE,
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
    _script_provider(monkeypatch, [_text_round("<think>这次不打扰她了</think>")])
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
            worker.ENCLAVE_SEMAPHORE,
            claimed_by,
        )
    )

    assert status == "completed"
    assert _job_status(job_id) == ("completed", None)
    assert writes == []
    schedule = jobs_store.get_wake_schedule(uid)
    assert schedule is None or schedule["proactive_backoff_until"] is None


def test_scheduled_thinking_only_remains_a_must_deliver_failure(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_scheduled_thinking_only"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "scheduled")
    claimed_by = _claim(job_id)
    _script_provider(monkeypatch, [_text_round("<think>提醒必须送达</think>")])
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
            worker.ENCLAVE_SEMAPHORE,
            claimed_by,
        )
    )

    assert status == "failed"
    assert _job_status(job_id) == ("failed", "wake_failed:empty_reply")
    assert writes == []
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
        worker.ENCLAVE_SEMAPHORE,
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
    assert user_text == ""
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
        worker.ENCLAVE_SEMAPHORE,
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
    ] == ["hi"]
    runtime_text = str(runtime_messages[0]["content"])
    assert '"perception_wake"' in runtime_text
    assert "arrived_at_anchor" in runtime_text
    assert "anchor_changed" in runtime_text
    assert "arrived near home" not in runtime_text
    assert "location:home" not in runtime_text
    assert "A recent perception change may be worth responding to." not in str(
        seen["messages"]
    )


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
        read_summary=lambda uid: ("", 0.0, 0),
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
            worker.ENCLAVE_SEMAPHORE,
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
        worker.ENCLAVE_SEMAPHORE,
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

    deps = _wake_deps(tail=[{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}])
    status = asyncio.run(worker._run_wake(
        job_id, uid, "manual_wake", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    assert status == "failed"
    assert write_called["n"] == 0
    assert surface_called["n"] == 0
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1] == "wake_failed:runtimeerror"
    assert not any(e["kind"] == "error" for e in _status_events(uid))
    assert jobs_store.get_wake_schedule(uid) is None


def test_run_wake_unexpected_exception_also_silent_mark_failed(monkeypatch):
    """Any other unexpected exception during the wake turn (e.g. read_tail
    blowing up) must be caught by _run_wake's own try/except, same as
    _run_compaction — never propagate, never surface a user error chip."""
    uid = "u_wake_boom"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "heartbeat")
    claimed_by = _claim(job_id)

    def _boom_read_tail(uid_, after_ts, limit):
        raise RuntimeError("tail read exploded")

    deps = worker.TurnDeps(
        read_messages=lambda uid_: [],
        resolve_provider=lambda uid_: (_BYOK, {}),
        mint_enclave_token=lambda uid_: "rt",
        read_tail=_boom_read_tail,
        read_summary=lambda uid_: ("", 0.0, 0),
    )
    surface_called = {"n": 0}
    monkeypatch.setattr(
        worker, "_surface_terminal_error",
        lambda *a, **k: surface_called.update(n=surface_called["n"] + 1))

    status = asyncio.run(worker._run_wake(
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

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
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

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
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))
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
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

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
        worker.ENCLAVE_SEMAPHORE, claimed_by))

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
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

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
        read_summary=lambda uid_: ("", 0.0, 0),
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
        job_id, uid, "heartbeat", deps, _BYOK, worker.ENCLAVE_SEMAPHORE, claimed_by))

    # The lane must have told the provider that silence is a valid outcome…
    assert seen.get("require_reply") is False, seen
    # …and therefore slept instead of failing.
    assert status == "completed"
    assert _job_status(job_id)[0] == "completed"
    assert write_called["n"] == 0
