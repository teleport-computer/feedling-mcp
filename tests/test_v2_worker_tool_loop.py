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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from provider_types import ToolExchange
from capabilities import registry as cap_registry
from core import store as core_store
from model_api_runtime.v2 import cursor as v2_cursor
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
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-user-byok", base_url="")


class _FakeCapResult:
    def __init__(self, data=None, ok=True):
        self._data = data or {}
        self._ok = ok

    def to_dict(self):
        return {"ok": self._ok, "data": self._data, "error": None, "trace": {}, "warnings": []}


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    """Mirrors test_v2_worker.py's fixture: claim_next_job() is a global claim,
    not filtered by user_id, so a stray row from another test module would
    otherwise get claimed here instead of this file's own row."""
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
    def _real_write(store, text):
        envelope = {"v": 1, "body_ct": text, "nonce": "n", "K_user": "k_test"}
        return store.append_chat("openclaw", "model_api", envelope, strict=True)

    monkeypatch.setattr(worker, "_write_encrypted_reply", _real_write)


def _reply_effect_dispatch(user_id):
    """Test-local production-shaped sink for the `reply` effect_type — mirrors
    `serve_worker._sink_reply`'s real write (`worker._write_encrypted_reply`)
    without pulling in serve_worker's hosted-adjacent wiring."""
    def dispatch(effect_type, payload):
        if effect_type == "reply":
            worker._write_encrypted_reply(core_store.get_store(user_id), str(payload.get("text") or ""))
    return dispatch


def _apply_effects(user_id):
    return v2_effect_outbox.apply_pending_effects(user_id, dispatch=_reply_effect_dispatch(user_id))


def _script_provider(monkeypatch, responses):
    it = iter(responses)
    calls = []

    async def _fake(config, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return next(it)

    monkeypatch.setattr(provider_client, "chat_completion_async", _fake)
    return calls


def _text_round(text, *, prompt_tokens=1, completion_tokens=1):
    return {"reply": text, "tool_calls": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tool_round(*tool_calls, prompt_tokens=1, completion_tokens=1):
    return {"reply": "", "tool_calls": list(tool_calls),
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def _tc(call_id, name, **args):
    return {"id": call_id, "name": name, "args": args}


def _deps(*, messages, token="rt-enclave"):
    return worker.TurnDeps(
        read_messages=lambda uid: list(messages),
        resolve_provider=lambda uid: (_BYOK, {}),
        mint_enclave_token=lambda uid: token,
        apply_pending_effects=_apply_effects,
    )


def _bubbles(uid):
    """Real chat_messages rows written for this user's model-authored replies,
    in seq (durable write) order — `role="openclaw"`/`source="model_api"` is
    exactly what `worker._write_encrypted_reply` always writes."""
    store = core_store.get_store(uid)
    store.reload()
    return [m for m in store.chat_messages if m.get("role") == "openclaw" and m.get("source") == "model_api"]


def _turn_metric_row(job_id):
    with db.get_pool().connection() as c:
        row = c.execute(
            "SELECT model_calls, failed, status FROM v2_turn_metrics WHERE job_id=%s",
            (job_id,)).fetchone()
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

        return v2_effect_outbox.apply_pending_effects(
            user_id, dispatch=dispatch)

    return worker.TurnDeps(
        read_messages=lambda _user_id: read_after_seq(uid, 0),
        read_messages_after_seq=read_after_seq,
        resolve_provider=lambda _user_id: (_BYOK, {}),
        mint_enclave_token=lambda _user_id: "rt",
        apply_pending_effects=apply,
    )


def test_single_round_plain_text_writes_exactly_one_bubble(monkeypatch):
    """Round 1: no tool_calls, plain text -> that text IS the final reply
    (Global Constraints)."""
    uid = "u_toolloop_happy"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_real_write(monkeypatch)

    calls = _script_provider(monkeypatch, [_text_round("hello from the model")])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert len(calls) == 1
    bubbles = _bubbles(uid)
    assert len(bubbles) == 1
    assert bubbles[0]["body_ct"] == "hello from the model"
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[0] == 1          # exactly one model call
    assert row[1] is False      # not failed
    assert row[2] == "ok"
    assert _job_status_row(job_id)[0] == "completed"


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
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("read", "memory_index")),
        _text_round("workspace-aware reply"),
    ])
    loader_calls = []
    deps = _deps(messages=[
        {"id": "m1", "ts": 10.0, "role": "user", "content": "hi"},
    ])
    deps.load_workspace_prompt = lambda _store, **kwargs: (
        loader_calls.append(kwargs["runtime_token"])
        or {
            "trusted_system_blocks": (
                "<feedling-skill>trusted skill</feedling-skill>",
            ),
            "working_memory": "editable working state",
        }
    )

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert loader_calls == ["rt"]
    assert len(calls) == 2
    for call in calls:
        prompt = str(call["messages"])
        assert "trusted skill" in prompt
        assert "editable working state" in prompt
    system = next(
        message for message in calls[0]["messages"]
        if message["role"] == "system"
    )
    assert "trusted skill" in str(system["content"])
    working = next(
        message for message in calls[0]["messages"]
        if worker.context.WORKING_MEMORY_HEADER
        in str(message.get("content"))
    )
    assert working["role"] == "user"


def test_chat_workspace_prompt_failure_is_visible_before_provider(
    monkeypatch,
):
    uid = "u_toolloop_workspace_prompt_failure"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-workspace-prompt-failure")
    deps = _deps(messages=[
        {"id": "m1", "ts": 10.0, "role": "user", "content": "hi"},
    ])
    deps.load_workspace_prompt = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("private workspace plaintext"))
    )
    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        lambda *_args, **_kwargs: pytest.fail(
            "provider called after workspace prompt failure"
        ),
    )

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "failed"
    assert _job_status_row(job_id)[:2] == (
        "failed",
        "turn_failed:workspace_prompt_unavailable",
    )


def test_chat_native_task_runs_child_then_returns_result_to_parent(
    monkeypatch,
):
    uid = "u_toolloop_native_task"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-native-task")
    _patch_real_write(monkeypatch)
    responses = iter([
        _tool_round(_tc(
            "task-1",
            "task",
            prompt="Inspect the report independently.",
        )),
        _text_round("child evidence"),
        _text_round("parent answer using child evidence"),
    ])
    calls = []

    async def provider(config, messages, *, tools=None):
        calls.append({
            "config": config,
            "messages": messages,
            "tools": tools,
        })
        return next(responses)

    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        provider,
    )
    deps = _deps(messages=[
        {
            "id": "m1",
            "ts": 10.0,
            "role": "user",
            "content": "Please inspect the report.",
        },
    ])

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

    assert status == "completed"
    assert len(calls) == 3
    assert all(call["config"] is _BYOK for call in calls)
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
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
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
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({}))
    written: list[str] = []
    deps = _late_input_deps(uid, written)
    calls = []

    async def provider(_config, messages, *, tools=None):
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

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

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
    assert v2_cursor.load_seq(core_store.get_store(uid)) == db.chat_seq_for_msg_id(uid, "B")


@pytest.mark.parametrize("failed_post_commit_step", ["done_status", "chat_notify", "metric"])
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
        "_perception_grounding_results",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(
        provider_client,
        "chat_completion_async",
        lambda *_args, **_kwargs: asyncio.sleep(
            0, result=_text_round("durable final")
        ),
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
            "SELECT status FROM v2_effect_outbox "
            "WHERE user_id=%s AND effect_type=%s",
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
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
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
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({}))
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

    async def provider(_config, messages, *, tools=None):
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

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

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
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
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
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({}))

    async def provider(_config, _messages, *, tools=None):
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

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

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
    assert rows[1][1:] == (
        "pending", "coalesced_followup", generation)


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
    job_id, _ = jobs_store.enqueue_job(
        uid, "chat", expected_generation=generation)
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
        cap_registry, "run_capability", lambda *args, **kwargs: _FakeCapResult({}))
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

    async def provider(_config, _messages, *, tools=None):
        return _text_round("must never be visible")

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    status = asyncio.run(worker.process_job(
        job,
        deps,
        provider_config=_BYOK,
        api_key=None,
        runtime_token="rt",
    ))

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
    assert rows == [(job_id, "failed", None, generation)]
    assert effect == (
        "discarded", v2_effect_outbox.FINAL_REPLY_INVALID_FENCE, 0)


def _job_status_row(job_id):
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()
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
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "search result"}))
    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [
        _tool_round(_tc("r1", "reply", text="intermediate"), _tc("s1", "web_search", query="x")),
        _text_round("final answer"),
    ])
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

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
        job_id=job_id, user_id=uid, effect_type="reply", ordinal=0,
        expected_generation=gen, payload={"text": "intermediate"})
    assert replay_id == eid  # same deterministic id -> ON CONFLICT DO NOTHING, no new row
    result = _apply_effects(uid)
    assert result == {"applied": 0, "discarded": 0}  # already applied -> not in the pending set

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
        cap_registry, "run_capability",
        lambda action_type, store, **k: _FakeCapResult({"snippet": "irrelevant"}))
    # The ONE and only (last) round: tools=None is what the provider is asked
    # for, but it misbehaves and returns a non-reply tool_call anyway.
    calls = _script_provider(monkeypatch, [_tool_round(_tc("c1", "web_search", query="x"))])
    write_calls = {"n": 0}
    monkeypatch.setattr(
        worker, "_write_encrypted_reply",
        lambda store, text: write_calls.update(n=write_calls["n"] + 1) or {"id": "should-not-happen"})
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert len(calls) == 1
    assert write_calls["n"] == 0          # no filler bubble, no bubble at all
    assert _bubbles(uid) == []
    status_row = _job_status_row(job_id)
    assert status_row[0] == "failed"
    assert "empty_reply" in (status_row[1] or "")
    row = _turn_metric_row(job_id)
    assert row is not None
    assert row[1] is True                 # failed=True in the metric row too
