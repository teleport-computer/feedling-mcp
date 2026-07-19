"""Runtime V2 workspace batching: ordering, fencing, and real sink overlap."""
from __future__ import annotations

import asyncio
import itertools
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import effect_id
from model_api_runtime.v2 import effect_outbox
from model_api_runtime.v2 import executor
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker
from provider_types import ToolCall, ToolResult
import db
from conftest import seed_user, set_v2_runtime_owner


def _operation(parent: str, index: int, path: str) -> dict:
    return {
        "op": "workspace_write",
        "path": path,
        "content": f"content:{path}",
        "expected_revision": 0,
        "sub_effect_id": effect_id.derive_batch_item(
            parent_effect_id=parent,
            ordinal=index,
        ),
    }


def _install_sink_seams(monkeypatch):
    claim_states: dict[str, str] = {}
    claim_lock = threading.Lock()

    def claim(effect: str) -> bool:
        with claim_lock:
            state = claim_states.get(effect)
            if state == "completed":
                return False
            if state == "claimed":
                raise AssertionError("unexpected concurrent duplicate child claim")
            claim_states[effect] = "claimed"
            return True

    def complete(effect: str) -> None:
        with claim_lock:
            assert claim_states.get(effect) == "claimed"
            claim_states[effect] = "completed"

    def release(effect: str) -> None:
        with claim_lock:
            if claim_states.get(effect) == "claimed":
                claim_states.pop(effect)

    monkeypatch.setattr(serve_worker.db, "effect_sink_claim", claim)
    monkeypatch.setattr(serve_worker.db, "effect_sink_complete", complete)
    monkeypatch.setattr(serve_worker.db, "effect_sink_release", release)
    monkeypatch.setattr(
        serve_worker.core_store,
        "get_store",
        lambda user_id: SimpleNamespace(user_id=user_id),
    )
    monkeypatch.setattr(
        serve_worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, plaintext, item_id=None: (
            {
                "id": item_id,
                "owner_user_id": store.user_id,
                "body_ct": "ciphertext",
            },
            "",
        ),
    )
    return claim_states


def _workspace_row(path, kind, mime_type, source_ref, expected_revision):
    return {
        "path": path,
        "kind": kind,
        "mime_type": mime_type,
        "source_ref": source_ref,
        "revision": expected_revision + 1,
    }


def test_disjoint_workspace_writes_overlap_at_durable_backend_boundary(
    monkeypatch,
):
    """Both calls must reach jobs_store's CAS seam before either may return.

    Concurrent preparation/encryption alone cannot pass this barrier: a serial
    durable sink deadlocks until the barrier times out and fails the test.
    """
    _install_sink_seams(monkeypatch)
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    barrier = threading.Barrier(2, timeout=2)
    intervals: dict[str, list[float]] = {}
    interval_lock = threading.Lock()

    def put_workspace_entry_cas(
        user_id,
        path,
        *,
        kind,
        content_envelope,
        mime_type,
        source_ref,
        expected_revision,
    ):
        with interval_lock:
            intervals[path] = [time.monotonic()]
        barrier.wait()
        time.sleep(0.05)
        with interval_lock:
            intervals[path].append(time.monotonic())
        return _workspace_row(
            path, kind, mime_type, source_ref, expected_revision
        )

    monkeypatch.setattr(
        serve_worker.jobs_store,
        "put_workspace_entry_cas",
        put_workspace_entry_cas,
    )
    parent = "job7:workspace_batch_encrypted_v1:0"
    serve_worker._sink_workspace_batch(
        "u_parallel",
        {
            "effect_id": parent,
            "operations": [
                _operation(parent, 0, "/workspace/alpha.md"),
                _operation(parent, 1, "/workspace/beta.md"),
            ],
        },
        runtime_token="token",
    )

    alpha = intervals["/workspace/alpha.md"]
    beta = intervals["/workspace/beta.md"]
    assert max(alpha[0], beta[0]) < min(alpha[1], beta[1])


def test_ancestor_paths_are_ordered_while_later_disjoint_path_can_overlap(
    monkeypatch,
):
    _install_sink_seams(monkeypatch)
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "3")
    intervals: dict[str, list[float]] = {}
    interval_lock = threading.Lock()

    def put_workspace_entry_cas(
        user_id,
        path,
        *,
        kind,
        content_envelope,
        mime_type,
        source_ref,
        expected_revision,
    ):
        with interval_lock:
            intervals[path] = [time.monotonic()]
        time.sleep(0.06)
        with interval_lock:
            intervals[path].append(time.monotonic())
        return _workspace_row(
            path, kind, mime_type, source_ref, expected_revision
        )

    monkeypatch.setattr(
        serve_worker.jobs_store,
        "put_workspace_entry_cas",
        put_workspace_entry_cas,
    )
    parent = "job8:workspace_batch_encrypted_v1:0"
    serve_worker._sink_workspace_batch(
        "u_ordered",
        {
            "effect_id": parent,
            "operations": [
                _operation(parent, 0, "/workspace/project"),
                _operation(parent, 1, "/workspace/project/child.md"),
                _operation(parent, 2, "/workspace/independent.md"),
            ],
        },
        runtime_token="token",
    )

    parent_interval = intervals["/workspace/project"]
    child_interval = intervals["/workspace/project/child.md"]
    independent = intervals["/workspace/independent.md"]
    assert parent_interval[1] <= child_interval[0]
    assert max(child_interval[0], independent[0]) < min(
        child_interval[1], independent[1]
    )


def test_partial_batch_retry_skips_completed_children(monkeypatch):
    states = _install_sink_seams(monkeypatch)
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    attempts = Counter()

    def put_workspace_entry_cas(
        user_id,
        path,
        *,
        kind,
        content_envelope,
        mime_type,
        source_ref,
        expected_revision,
    ):
        attempts[path] += 1
        if path.endswith("retry.md") and attempts[path] == 1:
            raise OSError("transient database failure")
        return _workspace_row(
            path, kind, mime_type, source_ref, expected_revision
        )

    monkeypatch.setattr(
        serve_worker.jobs_store,
        "put_workspace_entry_cas",
        put_workspace_entry_cas,
    )
    parent = "job9:workspace_batch_encrypted_v1:0"
    payload = {
        "effect_id": parent,
        "operations": [
            _operation(parent, 0, "/workspace/complete.md"),
            _operation(parent, 1, "/workspace/retry.md"),
        ],
    }

    with pytest.raises(RuntimeError):
        serve_worker._sink_workspace_batch(
            "u_retry", payload, runtime_token="token"
        )
    serve_worker._sink_workspace_batch(
        "u_retry", payload, runtime_token="token"
    )

    assert attempts == {
        "/workspace/complete.md": 1,
        "/workspace/retry.md": 2,
    }
    assert all(state == "completed" for state in states.values())


def test_terminal_child_preserves_successful_sibling_in_ordered_parent_result(
    monkeypatch,
):
    states = _install_sink_seams(monkeypatch)
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    parent = "job9:workspace_batch_encrypted_v1:terminal"
    operations = [
        _operation(parent, 0, "/workspace/applied.md"),
        _operation(parent, 1, "/workspace/conflict.md"),
    ]

    def run_capability(op, _store, *, api_key, runtime_token, params):
        assert op == "workspace_write"
        assert api_key is None
        assert runtime_token == "token"
        if params["path"].endswith("conflict.md"):
            return SimpleNamespace(
                ok=False,
                error={"retryable": False},
            )
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr(
        serve_worker.cap_registry,
        "run_capability",
        run_capability,
    )

    result = serve_worker._sink_workspace_batch(
        "u_terminal",
        {"effect_id": parent, "operations": operations},
        runtime_token="token",
    )

    assert result == effect_outbox.WorkspaceBatchAppliedResult(
        {
            "kind": effect_outbox.WORKSPACE_BATCH_RESULT_KIND,
            "items": [
                {
                    "effect_id": operations[0]["sub_effect_id"],
                    "status": "applied",
                },
                {
                    "effect_id": operations[1]["sub_effect_id"],
                    "status": "discarded",
                    "error": "workspace_write_failed",
                },
            ],
        },
        status=effect_outbox.APPLIED_WITH_RESULTS_STATUS,
    )
    assert states == {operations[0]["sub_effect_id"]: "completed"}
    calls = [
        ToolCall(
            id="applied",
            name="workspace_write",
            args={
                "path": "/workspace/applied.md",
                "content": "applied",
                "expected_revision": 0,
            },
        ),
        ToolCall(
            id="conflict",
            name="workspace_write",
            args={
                "path": "/workspace/conflict.md",
                "content": "conflict",
                "expected_revision": 0,
            },
        ),
    ]
    mapped = worker._workspace_batch_tool_results(
        calls,
        parent_effect_id=parent,
        disposition={
            "status": effect_outbox.APPLIED_WITH_RESULTS_STATUS,
            "result": result.result,
        },
    )
    assert [(item.call_id, item.content) for item in mapped] == [
        ("applied", "ok: workspace_write applied"),
        ("conflict", "error: workspace_write_failed"),
    ]


def test_terminal_child_does_not_block_a_later_conflicting_path(monkeypatch):
    _install_sink_seams(monkeypatch)
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    parent = "job9:workspace_batch_encrypted_v1:later"
    operations = [
        _operation(parent, 0, "/workspace/project"),
        _operation(parent, 1, "/workspace/project/child.md"),
    ]
    called = []

    def run_capability(op, _store, *, api_key, runtime_token, params):
        called.append(params["path"])
        return SimpleNamespace(
            ok=params["path"].endswith("child.md"),
            error={"retryable": False},
        )

    monkeypatch.setattr(
        serve_worker.cap_registry,
        "run_capability",
        run_capability,
    )
    result = serve_worker._sink_workspace_batch(
        "u_later",
        {"effect_id": parent, "operations": operations},
        runtime_token="token",
    )

    assert called == [
        "/workspace/project",
        "/workspace/project/child.md",
    ]
    assert [item["status"] for item in result.result["items"]] == [
        "discarded",
        "applied",
    ]


def test_applied_batch_result_rejects_plaintext_metadata():
    with pytest.raises(RuntimeError, match="child status"):
        effect_outbox._serialized_applied_result(
            effect_outbox.WorkspaceBatchAppliedResult(
                {
                    "kind": effect_outbox.WORKSPACE_BATCH_RESULT_KIND,
                    "items": [
                        {
                            "effect_id": "job1:workspace_batch_encrypted_v1:0:item:0",
                            "status": "applied",
                            "path": "/workspace/private.md",
                        }
                    ],
                }
            )
        )


def test_retryable_child_keeps_parent_unresolved_despite_terminal_sibling(
    monkeypatch,
):
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    parent = "job9:workspace_batch_encrypted_v1:retry-dominates"

    def apply(_user_id, operation, *, runtime_token):
        if operation["path"].endswith("terminal.md"):
            raise db.EffectTerminalError("workspace_write_failed")
        raise RuntimeError("transient backend failure")

    monkeypatch.setattr(
        serve_worker,
        "_apply_workspace_batch_operation",
        apply,
    )
    with pytest.raises(RuntimeError, match="transient backend failure") as exc_info:
        serve_worker._sink_workspace_batch(
            "u_retry_dominates",
            {
                "effect_id": parent,
                "operations": [
                    _operation(parent, 0, "/workspace/terminal.md"),
                    _operation(parent, 1, "/workspace/retry.md"),
                ],
            },
            runtime_token="token",
        )
    assert not isinstance(exc_info.value, db.EffectTerminalError)


def test_uncertain_child_dominates_terminal_sibling(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    parent = "job9:workspace_batch_encrypted_v1:uncertain"

    def apply(_user_id, operation, *, runtime_token):
        if operation["path"].endswith("terminal.md"):
            raise db.EffectTerminalError("workspace_revision_conflict")
        raise db.EffectDeliveryUncertainError("child outcome unknown")

    monkeypatch.setattr(
        serve_worker,
        "_apply_workspace_batch_operation",
        apply,
    )
    with pytest.raises(db.EffectDeliveryUncertainError):
        serve_worker._sink_workspace_batch(
            "u_uncertain",
            {
                "effect_id": parent,
                "operations": [
                    _operation(parent, 0, "/workspace/terminal.md"),
                    _operation(parent, 1, "/workspace/uncertain.md"),
                ],
            },
            runtime_token="token",
        )


def test_encrypted_batch_validation_binds_every_child_id_to_parent():
    parent = "job10:workspace_batch_encrypted_v1:0"
    payload = {
        "effect_id": parent,
        "operations": [_operation(parent, 0, "/workspace/ok.md")],
    }
    serve_worker._validate_decrypted_tool_effect("workspace_batch", payload)

    payload["operations"][0]["sub_effect_id"] = "forged"
    with pytest.raises(RuntimeError, match="child identity"):
        serve_worker._validate_decrypted_tool_effect(
            "workspace_batch", payload
        )


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="workspace outbox integration requires PostgreSQL",
)
def test_encrypted_parent_outbox_applies_real_child_rows_and_claims(
    monkeypatch,
):
    uid = "u_workspace_batch_outbox"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
    seed_user(uid)
    set_v2_runtime_owner(uid)
    generation = db.get_runtime_generation(uid)
    job_id = 9017
    stored_type = worker.ENCRYPTED_TOOL_EFFECT_TYPES["workspace_batch"]
    parent = effect_id.derive(
        job_id=job_id,
        effect_type=stored_type,
        ordinal=0,
    )
    plaintext_payload = {
        "operations": [
            _operation(parent, 0, "/workspace/outbox-a.md"),
            _operation(parent, 1, "/workspace/outbox-b.md"),
        ]
    }

    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    monkeypatch.setattr(
        serve_worker, "_mint_runtime_token", lambda _uid: "token"
    )
    monkeypatch.setattr(
        serve_worker.core_store,
        "get_store",
        lambda user_id: SimpleNamespace(user_id=user_id),
    )
    monkeypatch.setattr(
        serve_worker.core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, plaintext, item_id=None: (
            {
                "id": item_id,
                "owner_user_id": store.user_id,
                "body_ct": "opaque-workspace-ciphertext",
            },
            "",
        ),
    )

    def decrypt(_user_id, stored_payload, *, runtime_token):
        assert set(stored_payload) == {"effect_envelope", "effect_id"}
        assert runtime_token == "token"
        return {
            **plaintext_payload,
            "effect_id": stored_payload["effect_id"],
        }

    monkeypatch.setattr(
        serve_worker, "_decrypt_tool_effect_payload", decrypt
    )
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=stored_type,
        ordinal=0,
        expected_generation=generation,
        payload={
            "effect_envelope": {
                "id": worker._tool_effect_item_id(parent),
                "body_ct": "opaque-batch-ciphertext",
            }
        },
    )

    assert serve_worker._apply_pending_effects_for_user(uid) == {
        "applied": 1,
        "discarded": 0,
    }
    with db.get_pool().connection() as conn:
        effect_row = conn.execute(
            "SELECT status,payload::text FROM v2_effect_outbox "
            "WHERE effect_id=%s",
            (parent,),
        ).fetchone()
        entries = conn.execute(
            "SELECT path,content_envelope::text FROM v2_workspace_entries "
            "WHERE user_id=%s ORDER BY path",
            (uid,),
        ).fetchall()
        child_claims = conn.execute(
            "SELECT effect_id,claim_state FROM v2_effect_sink_applied "
            "WHERE effect_id LIKE %s ORDER BY effect_id",
            (parent + ":item:%",),
        ).fetchall()
    assert effect_row[0] == "applied"
    assert "content:/workspace" not in effect_row[1]
    assert [row[0] for row in entries] == [
        "/workspace/outbox-a.md",
        "/workspace/outbox-b.md",
    ]
    assert all("content:/workspace" not in row[1] for row in entries)
    assert child_claims == [
        (effect_id.derive_batch_item(parent_effect_id=parent, ordinal=0), "completed"),
        (effect_id.derive_batch_item(parent_effect_id=parent, ordinal=1), "completed"),
    ]


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="workspace outbox integration requires PostgreSQL",
)
def test_parent_outbox_is_applied_with_persisted_partial_terminal_results(
    monkeypatch,
):
    uid = "u_workspace_batch_partial_terminal"
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
    seed_user(uid)
    set_v2_runtime_owner(uid)
    generation = db.get_runtime_generation(uid)
    job_id = 9018
    stored_type = worker.ENCRYPTED_TOOL_EFFECT_TYPES["workspace_batch"]
    parent = effect_id.derive(
        job_id=job_id,
        effect_type=stored_type,
        ordinal=0,
    )
    operations = [
        _operation(parent, 0, "/workspace/partial-applied.md"),
        _operation(parent, 1, "/workspace/partial-conflict.md"),
    ]

    monkeypatch.setenv("FEEDLING_V2_WORKSPACE_WRITE_PARALLELISM", "2")
    monkeypatch.setattr(
        serve_worker,
        "_mint_runtime_token",
        lambda _uid: "token",
    )
    monkeypatch.setattr(
        serve_worker.core_store,
        "get_store",
        lambda user_id: SimpleNamespace(user_id=user_id),
    )

    def decrypt(_user_id, stored_payload, *, runtime_token):
        assert runtime_token == "token"
        return {
            "effect_id": stored_payload["effect_id"],
            "operations": operations,
        }

    monkeypatch.setattr(
        serve_worker,
        "_decrypt_tool_effect_payload",
        decrypt,
    )
    durable_calls = []

    def run_capability(op, _store, *, api_key, runtime_token, params):
        assert op == "workspace_write"
        assert api_key is None
        assert runtime_token == "token"
        durable_calls.append(params["path"])
        if params["path"].endswith("partial-conflict.md"):
            return SimpleNamespace(
                ok=False,
                error={"retryable": False},
            )
        return SimpleNamespace(ok=True, error=None)

    monkeypatch.setattr(
        serve_worker.cap_registry,
        "run_capability",
        run_capability,
    )
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type=stored_type,
        ordinal=0,
        expected_generation=generation,
        payload={
            "effect_envelope": {
                "id": worker._tool_effect_item_id(parent),
                "body_ct": "opaque-batch-ciphertext",
            }
        },
    )

    assert serve_worker._apply_pending_effects_for_user(uid) == {
        "applied": 1,
        "discarded": 0,
    }
    assert sorted(durable_calls) == sorted(
        operation["path"] for operation in operations
    )
    expected_result = {
        "kind": effect_outbox.WORKSPACE_BATCH_RESULT_KIND,
        "items": [
            {
                "effect_id": operations[0]["sub_effect_id"],
                "status": "applied",
            },
            {
                "effect_id": operations[1]["sub_effect_id"],
                "status": "discarded",
                "error": "workspace_write_failed",
            },
        ],
    }
    assert effect_outbox.get_effect_disposition(
        parent,
        user_id=uid,
        job_id=job_id,
        effect_type=stored_type,
    ) == {
        "status": effect_outbox.APPLIED_WITH_RESULTS_STATUS,
        "last_error": "",
        "result": expected_result,
    }
    with db.get_pool().connection() as conn:
        parent_row = conn.execute(
            "SELECT status,payload FROM v2_effect_outbox WHERE effect_id=%s",
            (parent,),
        ).fetchone()
        child_claims = conn.execute(
            "SELECT effect_id,claim_state FROM v2_effect_sink_applied "
            "WHERE effect_id LIKE %s ORDER BY effect_id",
            (parent + ":item:%",),
        ).fetchall()
    assert parent_row[0] == effect_outbox.APPLIED_WITH_RESULTS_STATUS
    assert parent_row[1] == {
        effect_outbox.APPLIED_RESULT_PAYLOAD_KEY: expected_result
    }
    assert child_claims == [
        (operations[0]["sub_effect_id"], "completed"),
    ]


def test_executor_emits_one_batch_effect_for_contiguous_workspace_writes():
    async def scenario():
        calls = [
            ToolCall(
                id="a",
                name="workspace_write",
                args={
                    "path": "/workspace/a.md",
                    "content": "a",
                    "expected_revision": 0,
                },
            ),
            ToolCall(
                id="b",
                name="workspace_delete",
                args={
                    "path": "/workspace/b.md",
                    "expected_revision": 1,
                },
            ),
        ]
        singles = []
        batches = []
        fences = []

        async def enqueue_single(call):
            singles.append(call.id)

        async def enqueue_batch(batch):
            batches.append([call.id for call in batch])

        async def before_write():
            fences.append("fence")

        results = await executor.dispatch_tool_calls(
            calls,
            store=None,
            api_key=None,
            runtime_token="",
            enclave_sem=asyncio.Semaphore(1),
            turn_authorization=True,
            enqueue_write_effect=enqueue_single,
            enqueue_workspace_batch_effect=enqueue_batch,
            before_write=before_write,
        )
        assert singles == []
        assert batches == [["a", "b"]]
        assert fences == ["fence", "fence"]
        assert [result.call_id for result in results] == ["a", "b"]

    asyncio.run(scenario())


def test_workspace_batch_reservation_consumes_one_ordered_parent_identity():
    async def scenario():
        reservations = worker._PlatformEffectReservations(
            job_id=88,
            ordinal_counter=itertools.count(),
        )
        before = ToolCall(id="before", name="identity_patch", args={})
        run = [
            ToolCall(
                id="wa",
                name="workspace_write",
                args={
                    "path": "/workspace/a.md",
                    "content": "a",
                    "expected_revision": 0,
                },
            ),
            ToolCall(
                id="wb",
                name="workspace_delete",
                args={
                    "path": "/workspace/b.md",
                    "expected_revision": 1,
                },
            ),
        ]
        after = ToolCall(id="after", name="identity_patch", args={})
        reservations.prepare(before)
        reservations.prepare_batch(run)
        reservations.prepare(after)

        first = reservations.get(before)
        parent = reservations.get_batch(run)
        last = reservations.get(after)
        assert (first.ordinal, parent.ordinal, last.ordinal) == (0, 1, 2)
        assert parent.effect_id == effect_id.derive(
            job_id=88,
            effect_type="workspace_batch_encrypted_v1",
            ordinal=1,
        )
        assert [
            operation["sub_effect_id"]
            for operation in parent.payload["operations"]
        ] == [
            effect_id.derive_batch_item(
                parent_effect_id=parent.effect_id,
                ordinal=0,
            ),
            effect_id.derive_batch_item(
                parent_effect_id=parent.effect_id,
                ordinal=1,
            ),
        ]

        parent_admitted = asyncio.Event()
        last_admitted = asyncio.Event()

        async def wait_parent():
            await reservations.wait_for_enqueue_turn(parent)
            parent_admitted.set()

        async def wait_last():
            await reservations.wait_for_enqueue_turn(last)
            last_admitted.set()

        parent_waiter = asyncio.create_task(wait_parent())
        last_waiter = asyncio.create_task(wait_last())
        await asyncio.sleep(0)
        assert not parent_admitted.is_set()
        assert not last_admitted.is_set()
        reservations.mark_ready(before)
        await asyncio.wait_for(parent_admitted.wait(), timeout=1)
        assert not last_admitted.is_set()
        reservations.mark_batch_ready(run)
        await asyncio.wait_for(last_admitted.wait(), timeout=1)
        await asyncio.gather(parent_waiter, last_waiter)

    asyncio.run(scenario())


def test_scheduler_reserves_only_valid_batch_children_and_records_each_call():
    class EmptyMcpTurn:
        def handles(self, _name):
            return False

    async def scenario():
        calls = [
            ToolCall(
                id="valid-a",
                name="workspace_write",
                args={
                    "path": "/workspace/a.md",
                    "content": "a",
                    "expected_revision": 0,
                },
            ),
            ToolCall(
                id="invalid",
                name="workspace_write",
                args={},
                args_ok=False,
            ),
            ToolCall(
                id="valid-b",
                name="workspace_delete",
                args={
                    "path": "/workspace/b.md",
                    "expected_revision": 1,
                },
            ),
        ]
        reservations = worker._PlatformEffectReservations(
            job_id=89,
            ordinal_counter=itertools.count(),
        )
        enqueued_children = []
        events = []

        def prepare_batch(valid_calls):
            reservations.prepare_batch(valid_calls)

        async def enqueue_batch(valid_calls):
            prepared = reservations.get_batch(valid_calls)
            enqueued_children.append(
                [
                    operation["sub_effect_id"]
                    for operation in prepared.payload["operations"]
                ]
            )
            reservations.mark_batch_ready(valid_calls)

        async def before_write():
            return None

        async def dispatch_batch(run):
            try:
                return await executor.dispatch_tool_calls(
                    list(run),
                    store=None,
                    api_key=None,
                    runtime_token="",
                    enclave_sem=asyncio.Semaphore(1),
                    turn_authorization=True,
                    enqueue_write_effect=lambda _call: None,
                    enqueue_workspace_batch_effect=enqueue_batch,
                    before_write=before_write,
                )
            finally:
                reservations.mark_batch_ready(run)

        async def tool_event(call, event_kind, _payload):
            events.append((call.id, event_kind))

        results = await worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=EmptyMcpTurn(),
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=lambda _call: None,
            dispatch_workspace_batch=dispatch_batch,
            prepare_workspace_batch=prepare_batch,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
            on_tool_event=tool_event,
        )

        assert [result.call_id for result in results] == [
            "valid-a",
            "invalid",
            "valid-b",
        ]
        assert results[1].content.startswith("error: unparseable args")
        parent = reservations.get_batch([calls[0], calls[2]])
        assert enqueued_children == [[
            effect_id.derive_batch_item(
                parent_effect_id=parent.effect_id,
                ordinal=0,
            ),
            effect_id.derive_batch_item(
                parent_effect_id=parent.effect_id,
                ordinal=1,
            ),
        ]]
        assert events == [
            ("valid-a", "tool_call_started"),
            ("invalid", "tool_call_started"),
            ("valid-b", "tool_call_started"),
            ("valid-a", "tool_call_result"),
            ("invalid", "tool_call_result"),
            ("valid-b", "tool_call_result"),
        ]

    asyncio.run(scenario())


def test_scheduler_splits_workspace_runs_at_encrypted_batch_limit():
    class EmptyMcpTurn:
        def handles(self, _name):
            return False

    async def scenario():
        calls = [
            ToolCall(
                id=f"call-{index}",
                name="workspace_write",
                args={
                    "path": f"/workspace/{index}.md",
                    "content": str(index),
                    "expected_revision": 0,
                },
            )
            for index in range(worker.MAX_WORKSPACE_BATCH_OPERATIONS + 1)
        ]
        prepared = []
        dispatched = []

        def prepare_batch(run):
            prepared.append([call.id for call in run])

        async def dispatch_batch(run):
            dispatched.append([call.id for call in run])
            return [ToolResult(call_id=call.id, content="ok") for call in run]

        results = await worker._dispatch_mixed_tool_calls(
            calls,
            mcp_turn=EmptyMcpTurn(),
            mutating_mcp_names=frozenset(),
            dispatch_platform_one=lambda _call: None,
            dispatch_workspace_batch=dispatch_batch,
            prepare_workspace_batch=prepare_batch,
            before_mcp_mutation=lambda: None,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )
        assert [len(run) for run in prepared] == [
            worker.MAX_WORKSPACE_BATCH_OPERATIONS,
            1,
        ]
        assert dispatched == prepared
        assert [result.call_id for result in results] == [
            call.id for call in calls
        ]

    asyncio.run(scenario())


def test_mixed_scheduler_keeps_batch_between_other_mutations_in_model_order():
    class McpTurn:
        def handles(self, name):
            return name == "mcp__write"

        async def dispatch(self, call):
            events.append("mcp")
            return ToolResult(call_id=call.id, content="ok")

    async def scenario():
        nonlocal_events = events

        async def platform_one(call):
            nonlocal_events.append(call.id)
            return ToolResult(call_id=call.id, content="ok")

        async def workspace_batch(calls):
            nonlocal_events.append("batch:" + ",".join(call.id for call in calls))
            return [ToolResult(call_id=call.id, content="ok") for call in calls]

        async def before_mcp_mutation():
            return None

        results = await worker._dispatch_mixed_tool_calls(
            [
                ToolCall(id="memory", name="memory_write", args={}),
                ToolCall(id="wa", name="workspace_write", args={}),
                ToolCall(id="wb", name="workspace_delete", args={}),
                ToolCall(id="remote", name="mcp__write", args={}),
                ToolCall(id="wc", name="workspace_write", args={}),
            ],
            mcp_turn=McpTurn(),
            mutating_mcp_names={"mcp__write"},
            dispatch_platform_one=platform_one,
            dispatch_workspace_batch=workspace_batch,
            before_mcp_mutation=before_mcp_mutation,
            read_parallelism=2,
            mcp_timeout_sec=1,
        )
        assert nonlocal_events == [
            "memory",
            "batch:wa,wb",
            "mcp",
            "batch:wc",
        ]
        assert [result.call_id for result in results] == [
            "memory", "wa", "wb", "remote", "wc"
        ]

    events: list[str] = []
    asyncio.run(scenario())
