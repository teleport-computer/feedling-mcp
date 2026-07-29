"""Crash-recovery barriers for chat turns that may already have mutated state."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
from capabilities import registry as cap_registry
from capabilities import tool_schema as cap_tool_schema
from core import store as core_store
from model_api_runtime.v2 import context as v2_context
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import effect_outbox
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker
from provider_types import ToolCall, ToolResult, ToolSpec


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs PG"
)

_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-x",
    api_key="sk-user-byok",
    base_url="",
    context_window_tokens=128_000,
)

_MCP_MUTATION = ToolSpec(
    name="mcp__calendar__create",
    description="create",
    parameters={"type": "object", "properties": {}},
)
_MCP_READ = ToolSpec(
    name="mcp__calendar__list",
    description="list",
    parameters={"type": "object", "properties": {}},
)


@pytest.fixture(autouse=True)
def _clean_jobs():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_effect_outbox WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM user_blobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
    conftest.set_v2_runtime_owner(uid)


def _user_doc(message_id: str, text: str) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "source": "model_api",
        "body_ct": f"cipher-{message_id}",
        "nonce": "n",
        "K_user": "k",
        "K_enclave": "e",
        "test_plaintext": text,
    }


def _read_after(uid: str, after_seq: int) -> list[dict]:
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


class _PolicyMcpTurn:
    tool_specs = (_MCP_MUTATION, _MCP_READ)
    mutating_tool_names = frozenset({_MCP_MUTATION.name})

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def handles(self, name: str) -> bool:
        return name in {spec.name for spec in self.tool_specs}

    async def dispatch(self, call) -> ToolResult:
        self.dispatched.append(call.name)
        return ToolResult(call_id=call.id, content="ok")


async def _load_mcp(turn, *_args, **_kwargs):
    return turn


def _fake_envelope(store, plaintext: bytes, *, item_id=None):
    return (
        {
            "v": 1,
            "id": item_id,
            "owner_user_id": store.user_id,
            "visibility": "shared",
            "body_ct": plaintext.hex(),
            "nonce": "nonce",
            "K_user": "sealed-user-key",
            "K_enclave": "sealed-enclave-key",
        },
        "",
    )


def test_known_mcp_and_applied_platform_write_remain_barriers_until_cursor():
    uid = "u_mutation_barrier_query"
    conftest.seed_user(uid)
    _reset(uid)
    seq, job_id = db.chat_append_and_enqueue(
        uid, "first", 1.0, _user_doc("first", "do the write"), 5000, "chat"
    )
    claimed = jobs_store.claim_next_job("old-owner")
    assert claimed is not None and claimed["id"] == job_id
    assert jobs_store.mark_running(job_id, claimed_by="old-owner")
    assert jobs_store.start_mcp_mutation_attempt(
        job_id,
        user_id=uid,
        claimed_by="old-owner",
        call_id="call-1",
        tool_name=_MCP_MUTATION.name,
        input_frontier_seq=seq,
    )
    assert jobs_store.finish_mcp_mutation_attempt(
        job_id, call_id="call-1", outcome="known"
    )
    effect_id = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=worker.ENCRYPTED_TOOL_EFFECT_TYPES["memory"],
        ordinal=0,
        expected_generation=db.get_runtime_generation(uid),
        payload={"effect_envelope": {"ciphertext": "opaque"}},
        input_frontier_seq=seq,
    )
    db.effect_mark(effect_id, "applied")
    overlap_effect_id = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=worker.ENCRYPTED_TOOL_EFFECT_TYPES["identity"],
        ordinal=1,
        expected_generation=db.get_runtime_generation(uid),
        payload={"effect_envelope": {"ciphertext": "old-worker-opaque"}},
    )
    with db.get_pool().connection() as conn:
        stored_payload, stored_frontier = conn.execute(
            "SELECT payload,input_frontier_seq FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (effect_id,),
        ).fetchone()
        overlap_frontier = conn.execute(
            "SELECT input_frontier_seq FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (overlap_effect_id,),
        ).fetchone()[0]
    assert stored_payload == {"effect_envelope": {"ciphertext": "opaque"}}
    assert stored_frontier == seq
    # A pre-0041 worker overlapping the deploy cannot create an unguarded row:
    # the DB trigger supplies a conservative frontier in the INSERT itself.
    assert overlap_frontier == seq

    assert jobs_store.get_chat_mutation_recovery_barrier(
        uid, after_seq=0
    ) == {
        "through_seq": seq,
        "has_mcp": True,
        "has_platform": True,
    }
    assert jobs_store.get_chat_mutation_recovery_barrier(
        uid, after_seq=0, exclude_job_id=job_id
    ) is None
    assert jobs_store.get_chat_mutation_recovery_barrier(
        uid, after_seq=seq
    ) is None


def test_all_discarded_workspace_batch_does_not_leave_mutation_barrier():
    uid = "u_discarded_workspace_batch_barrier"
    conftest.seed_user(uid)
    _reset(uid)
    seq, job_id = db.chat_append_and_enqueue(
        uid, "first", 1.0, _user_doc("first", "create a file"), 5000, "chat"
    )
    effect_id = effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="workspace_batch_encrypted_v1",
        ordinal=0,
        expected_generation=db.get_runtime_generation(uid),
        payload={"effect_envelope": {"ciphertext": "opaque"}},
        input_frontier_seq=seq,
    )
    result = {
        "kind": effect_outbox.WORKSPACE_BATCH_RESULT_KIND,
        "items": [{
            "effect_id": f"{effect_id}:item:0",
            "status": "discarded",
            "error": "workspace_revision_conflict",
        }],
    }
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_effect_outbox SET status=%s,payload=%s WHERE effect_id=%s",
            (
                effect_outbox.APPLIED_WITH_RESULTS_STATUS,
                db.Jsonb({effect_outbox.APPLIED_RESULT_PAYLOAD_KEY: result}),
                effect_id,
            ),
        )

    assert jobs_store.get_chat_mutation_recovery_barrier(uid, after_seq=0) is None

    result["items"].append({
        "effect_id": f"{effect_id}:item:1",
        "status": "applied",
        "revision": 1,
    })
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE v2_effect_outbox SET payload=%s WHERE effect_id=%s",
            (db.Jsonb({effect_outbox.APPLIED_RESULT_PAYLOAD_KEY: result}), effect_id),
        )
    assert jobs_store.get_chat_mutation_recovery_barrier(
        uid, after_seq=0
    ) == {
        "through_seq": seq,
        "has_mcp": False,
        "has_platform": True,
    }


def test_new_user_send_after_crashed_mutation_runs_one_text_only_recovery_turn(
    monkeypatch,
):
    """Old pending platform work may apply, but the successor cannot repeat it."""
    uid = "u_mutation_barrier_followup"
    conftest.seed_user(uid)
    _reset(uid)
    first_seq, old_job_id = db.chat_append_and_enqueue(
        uid,
        "first",
        1.0,
        _user_doc("first", "create the event and remember it"),
        5000,
        "chat",
    )
    old_job = jobs_store.claim_next_job("crashed-owner")
    assert old_job is not None and old_job["id"] == old_job_id
    assert jobs_store.mark_running(old_job_id, claimed_by="crashed-owner")
    assert jobs_store.start_mcp_mutation_attempt(
        old_job_id,
        user_id=uid,
        claimed_by="crashed-owner",
        call_id="mcp-known-success",
        tool_name=_MCP_MUTATION.name,
        input_frontier_seq=first_seq,
    )
    assert jobs_store.finish_mcp_mutation_attempt(
        old_job_id, call_id="mcp-known-success", outcome="known"
    )
    old_platform_effect = effect_outbox.enqueue_effect(
        job_id=old_job_id,
        user_id=uid,
        effect_type=worker.ENCRYPTED_TOOL_EFFECT_TYPES["memory"],
        ordinal=0,
        expected_generation=db.get_runtime_generation(uid),
        payload={"effect_envelope": {"ciphertext": "opaque"}},
        input_frontier_seq=first_seq,
    )
    assert jobs_store.mark_failed(
        old_job_id, "simulated_child_crash", claimed_by="crashed-owner"
    )

    # This is the ordinary iOS follow-up path, not the periodic reconciler.
    second_seq, recovery_job_id = db.chat_append_and_enqueue(
        uid,
        "second",
        2.0,
        _user_doc("second", "please generate a PDF"),
        5000,
        "chat",
    )
    recovery_job = jobs_store.claim_next_job("recovery-owner")
    assert recovery_job is not None and recovery_job["id"] == recovery_job_id

    platform_applies: list[str] = []

    def apply_pending(user_id: str):
        def dispatch(effect_type: str, payload: dict) -> None:
            if effect_type == worker.ENCRYPTED_TOOL_EFFECT_TYPES["memory"]:
                platform_applies.append(str(payload["effect_id"]))
                return
            raise AssertionError(f"unexpected ordinary dispatch: {effect_type}")

        return effect_outbox.apply_pending_effects(
            user_id,
            dispatch=dispatch,
            dispatch_reply_in_transaction=(
                lambda _effect_type, payload, connection:
                serve_worker._sink_reply_in_transaction(
                    user_id, payload, connection
                )
            ),
        )

    mcp_turn = _PolicyMcpTurn()
    provider_calls: list[dict] = []

    async def provider(_config, messages, *, tools=None, **_kwargs):
        names = {spec.name for spec in (tools or [])}
        provider_calls.append({"messages": list(messages), "names": names})
        assert any(
            isinstance(message, dict)
            and message.get("role") == "system"
            and "RECOVERY SAFETY RULE" in str(message.get("content") or "")
            for message in messages
        )
        runtime_blocks = [
            message
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "user"
            and str(message.get("content") or "").startswith(
                v2_context.RUNTIME_CONTEXT_HEADER + "\n"
            )
        ]
        assert len(runtime_blocks) == 1
        runtime_payload = json.loads(
            str(runtime_blocks[0]["content"]).split("\n", 1)[1]
        )
        assert runtime_payload["runtime_control"] == {
            "mutation_recovery_active": True,
        }
        if len(provider_calls) == 1:
            assert names.isdisjoint(cap_registry.WRITE_ACTIONS)
            assert _MCP_MUTATION.name not in names
            assert _MCP_READ.name in names
            assert "reply" in names
            assert cap_tool_schema.FILE_REPLY_TOOL not in names
            # Simulate a broken relay inventing a call for the omitted schema.
            # The loop rejects it before dispatch; the worker also has an
            # independent fail-closed dispatcher gate.
            return {
                "reply": "",
                "tool_calls": [{
                    "id": "invented-mutation",
                    "name": _MCP_MUTATION.name,
                    "args": {},
                }],
                "usage": {},
            }
        assert tools is None
        return {"reply": "I couldn't confirm the earlier write. Want me to check?",
                "tool_calls": [], "usage": {}}

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        _fake_envelope,
    )
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: _read_after(uid, 0),
        read_messages_after_seq=lambda _uid, after: _read_after(uid, after),
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=apply_pending,
        load_workspace_file=lambda *_args, **_kwargs: {
            "path": "/workspace/existing.pdf",
            "content": "unused",
            "mime_type": "application/pdf",
            "revision": 1,
        },
        load_mcp_turn=lambda *args, **kwargs: _load_mcp(
            mcp_turn, *args, **kwargs
        ),
    )

    assert asyncio.run(
        worker.process_job(
            recovery_job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"

    assert len(provider_calls) == 2
    assert mcp_turn.dispatched == []
    assert platform_applies == [old_platform_effect]
    with db.get_pool().connection() as conn:
        old_effect_status = conn.execute(
            "SELECT status FROM v2_effect_outbox WHERE effect_id=%s",
            (old_platform_effect,),
        ).fetchone()[0]
        attempt_count = conn.execute(
            "SELECT COUNT(*) FROM v2_mcp_mutation_attempts WHERE user_id=%s",
            (uid,),
        ).fetchone()[0]
    assert old_effect_status == "applied"
    assert attempt_count == 1  # only the old job's pre-crash attempt
    assert v2_cursor.load_seq(core_store.get_store(uid)) >= second_seq
    assert jobs_store.get_chat_mutation_recovery_barrier(
        uid, after_seq=v2_cursor.load_seq(core_store.get_store(uid))
    ) is None


def test_reconciler_uses_cursor_after_intermediate_reply_and_mutation_crash():
    uid = "u_mutation_barrier_reconcile"
    conftest.seed_user(uid)
    _reset(uid)
    # Periodic reconciliation requires the profile's explicit V2 assignment.
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id,kind,doc) "
            "VALUES (%s,'model_api_runtime',%s) "
            "ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc",
            (uid, db.Jsonb({"hosted_runtime_mode": "db_action_v2"})),
        )
    db.chat_append_strict(
        uid, "orphan", 1.0, _user_doc("orphan", "do it"), 5000
    )
    seq = db.chat_seq_for_msg_id(uid, "orphan")
    assert seq is not None
    db.chat_append_strict(
        uid,
        "intermediate",
        2.0,
        {
            "id": "intermediate",
            "role": "openclaw",
            "source": "model_api",
            "body_ct": "cipher-intermediate",
        },
        5000,
    )
    assert db.chat_max_seq(uid) > seq
    with db.get_pool().connection() as conn:
        old_job_id = conn.execute(
            "INSERT INTO agent_jobs "
            "(user_id,lane,status,reason,trace_id,finished_at,last_error) "
            "VALUES (%s,'chat','failed','reconcile','orphan',now(),'child_crash') "
            "RETURNING id",
            (uid,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO v2_mcp_mutation_attempts "
            "(job_id,user_id,input_frontier_seq,call_key,tool_fingerprint,outcome,resolved_at) "
            "VALUES (%s,%s,%s,%s,%s,'known',now())",
            (old_job_id, uid, seq, "a" * 64, "b" * 64),
        )

    # The sweeper is fleet-wide; other test-created V2 users may also be
    # eligible in a combined CI run.  Assert this user received exactly the
    # intended recovery row below instead of coupling to the global count.
    assert db.reconcile_unenqueued_v2_messages() >= 1
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT reason,status FROM agent_jobs WHERE user_id=%s ORDER BY id",
            (uid,),
        ).fetchall()
    assert rows == [
        ("reconcile", "failed"),
        ("mutation_recovery", "pending"),
    ]
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status='failed',finished_at=now() "
            "WHERE user_id=%s AND reason='mutation_recovery'",
            (uid,),
        )
    # Recovery itself is one-shot for this exact input; a provider outage must
    # not turn the safety mechanism into an infinite billed retry loop.
    assert db.reconcile_unenqueued_v2_messages() == 0


def test_recovery_dispatcher_blocks_direct_mutating_mcp_bypass(monkeypatch):
    """Defense in depth does not rely only on provider schema enforcement."""
    uid = "u_mutation_barrier_direct_dispatch"
    conftest.seed_user(uid)
    _reset(uid)
    first_seq, old_job_id = db.chat_append_and_enqueue(
        uid,
        "first-direct",
        1.0,
        _user_doc("first-direct", "create it"),
        5000,
        "chat",
    )
    old_job = jobs_store.claim_next_job("old-direct-owner")
    assert old_job is not None and old_job["id"] == old_job_id
    assert jobs_store.mark_running(old_job_id, claimed_by="old-direct-owner")
    assert jobs_store.start_mcp_mutation_attempt(
        old_job_id,
        user_id=uid,
        claimed_by="old-direct-owner",
        call_id="old-direct-call",
        tool_name=_MCP_MUTATION.name,
        input_frontier_seq=first_seq,
    )
    assert jobs_store.finish_mcp_mutation_attempt(
        old_job_id, call_id="old-direct-call", outcome="known"
    )
    assert jobs_store.mark_failed(
        old_job_id, "simulated_child_crash", claimed_by="old-direct-owner"
    )
    _, recovery_job_id = db.chat_append_and_enqueue(
        uid,
        "second-direct",
        2.0,
        _user_doc("second-direct", "hello?"),
        5000,
        "chat",
    )
    recovery_job = jobs_store.claim_next_job("new-direct-owner")
    assert recovery_job is not None and recovery_job["id"] == recovery_job_id
    mcp_turn = _PolicyMcpTurn()

    def apply_pending(user_id: str):
        return effect_outbox.apply_pending_effects(
            user_id,
            dispatch=lambda effect_type, _payload: pytest.fail(
                f"unexpected ordinary effect {effect_type}"
            ),
            dispatch_reply_in_transaction=(
                lambda _effect_type, payload, connection:
                serve_worker._sink_reply_in_transaction(
                    user_id, payload, connection
                )
            ),
        )

    async def direct_loop(**kwargs):
        assert _MCP_MUTATION.name not in {
            spec.name for spec in kwargs["extra_tool_specs"]
        }
        assert _MCP_MUTATION.name in kwargs["disabled_tool_names"]
        (blocked,) = await kwargs["dispatch_tools"]([
            ToolCall(
                id="invented-direct",
                name=_MCP_MUTATION.name,
                args={},
            )
        ])
        assert blocked.content == worker._MUTATION_RECOVERY_BLOCKED_ERROR
        await kwargs["on_reply"]("safe recovery", final=True)
        return worker.v2_tool_loop.LoopOutcome(
            final_text="safe recovery",
            rounds=1,
            stop_reason="final_text",
            replied_intermediate=False,
        )

    monkeypatch.setattr(
        worker.core_envelope,
        "_build_shared_envelope_for_store",
        _fake_envelope,
    )
    monkeypatch.setattr(worker.v2_tool_loop, "run_tool_loop", direct_loop)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: _read_after(uid, 0),
        read_messages_after_seq=lambda _uid, after: _read_after(uid, after),
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=apply_pending,
        load_mcp_turn=lambda *args, **kwargs: _load_mcp(
            mcp_turn, *args, **kwargs
        ),
    )

    assert asyncio.run(
        worker.process_job(
            recovery_job,
            deps,
            provider_config=_BYOK,
            api_key=None,
            runtime_token="rt",
        )
    ) == "completed"
    assert mcp_turn.dispatched == []
    with db.get_pool().connection() as conn:
        attempts = conn.execute(
            "SELECT COUNT(*) FROM v2_mcp_mutation_attempts WHERE user_id=%s",
            (uid,),
        ).fetchone()[0]
    assert attempts == 1
