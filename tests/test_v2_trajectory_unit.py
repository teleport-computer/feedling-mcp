from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import provider_client
from provider_attempt_accounting import AttemptLane, ProviderAttemptContext
from provider_types import ToolResult
from model_api_runtime.v2 import extraction
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import tool_loop
from model_api_runtime.v2 import trajectory
from model_api_runtime.v2 import worker


def _attempt_config(*, job_id: int = 77, lane: AttemptLane = AttemptLane.CHAT):
    return provider_client.ProviderConfig(
        provider="anthropic",
        model="claude-test",
        api_key="secret",
        provider_attempt_context=ProviderAttemptContext(
            user_id="usr_attempt_v2",
            lane=lane,
            job_id=job_id,
            call_id=f"v2job:{job_id}:base",
            turn_id=f"v2job:{job_id}",
        ),
    )


def test_worker_binds_the_same_content_free_attempt_context_after_redelivery():
    """Changing claim attempt_count must not mint a new provider-attempt identity."""
    config = provider_client.ProviderConfig(
        provider="anthropic",
        model="claude-test",
        api_key="secret",
    )

    first = worker._bind_provider_attempt_context(
        config,
        job_id=77,
        user_id="usr_attempt_v2",
        lane="capture",
    )
    redelivered = worker._bind_provider_attempt_context(
        config,
        job_id=77,
        user_id="usr_attempt_v2",
        lane="capture",
    )

    assert first.provider_attempt_context == redelivered.provider_attempt_context
    assert first.provider_attempt_context == ProviderAttemptContext(
        user_id="usr_attempt_v2",
        lane=AttemptLane.CAPTURE,
        job_id=77,
        call_id="v2job:77:base",
        turn_id="v2job:77",
    )


def test_tool_loop_binds_a_distinct_stable_logical_call_per_provider_round(
    monkeypatch,
):
    seen_contexts = []
    responses = iter([
        {
            "reply": "",
            "tool_calls": [{"id": "c1", "name": "memory_search", "args": {}}],
            "usage": {},
        },
        {"reply": "done", "tool_calls": [], "usage": {}},
    ])

    async def provider(config, _messages, **_kwargs):
        seen_contexts.append(config.provider_attempt_context)
        return next(responses)

    async def dispatch(calls):
        return [ToolResult(call_id=calls[0].id, content="found")]

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", provider)
    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=_attempt_config(),
            build_messages=lambda transcript: [
                {"role": "user", "content": "hi"},
                *transcript,
            ],
            dispatch_tools=dispatch,
            on_reply=lambda *_args, **_kwargs: asyncio.sleep(0),
            fold_new_messages=lambda: asyncio.sleep(0, result=[]),
            add_usage=lambda _usage: None,
            max_calls=3,
        )
    )

    assert outcome.final_text == "done"
    assert [context.call_id for context in seen_contexts] == [
        "v2job:77:provider:1",
        "v2job:77:provider:2",
    ]
    assert [context.round_id for context in seen_contexts] == [
        "provider:1",
        "provider:2",
    ]


def test_extraction_parse_retry_uses_two_stable_logical_call_ids(monkeypatch):
    seen_contexts = []
    responses = iter([
        {"reply": "bad", "usage": {}},
        {"reply": "good", "usage": {}},
    ])

    async def provider(config, _messages, **_kwargs):
        seen_contexts.append(config.provider_attempt_context)
        return next(responses)

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", provider)
    result = asyncio.run(
        extraction.extract(
            provider_config=_attempt_config(job_id=88, lane=AttemptLane.CAPTURE),
            prompt="private prompt",
            parse=lambda reply: (None, "format") if reply == "bad" else (reply, None),
            parse_retry=extraction.ParseRetry(
                should_retry=lambda _reason: True,
                build_prompt=lambda prompt, _reason: prompt,
                parse=lambda reply: (reply, None),
            ),
        )
    )

    assert result == ("good", None)
    assert [context.call_id for context in seen_contexts] == [
        "v2job:88:extraction:1",
        "v2job:88:extraction:2",
    ]
    assert all(context.lane is AttemptLane.CAPTURE for context in seen_contexts)


def test_worker_loop_starts_and_stops_attempt_recorder_off_provider_path(monkeypatch):
    lifecycle = []
    monkeypatch.setattr(
        worker.provider_attempt_accounting,
        "start_provider_attempt_recorder",
        lambda: lifecycle.append("start") or True,
    )
    monkeypatch.setattr(
        worker.provider_attempt_accounting,
        "shutdown_provider_attempt_recorder",
        lambda timeout=1: lifecycle.append(("shutdown", timeout)) or True,
    )
    stop = asyncio.Event()
    stop.set()
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "token",
    )

    asyncio.run(
        worker.run_worker_loop(
            "worker-attempt-recorder",
            max_workers=1,
            poll_interval=0.01,
            stop_event=stop,
            deps=deps,
        )
    )

    assert lifecycle == ["start", ("shutdown", 1.0)]


def test_compaction_wrapper_binds_its_explicit_stable_logical_call(monkeypatch):
    seen = []

    async def provider(config, _messages, **_kwargs):
        seen.append(config.provider_attempt_context)
        return {"reply": "summary", "usage": {}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", provider)
    monkeypatch.setattr(worker, "_record_provider_success", lambda *_a, **_k: asyncio.sleep(0))
    result = asyncio.run(
        worker._compaction_llm_with_progress(
            "usr_attempt_v2",
            _attempt_config(job_id=91, lane=AttemptLane.MAINTENANCE),
            [{"role": "user", "content": "private"}],
            attempt_call_id="v2job:91:maintenance:batch:1",
        )
    )

    assert result["reply"] == "summary"
    assert seen[0].call_id == "v2job:91:maintenance:batch:1"
    assert seen[0].round_id == "maintenance:batch:1"


def test_photo_observer_derives_stable_tool_call_identity_before_assembly():
    deliveries = []

    def observe(user_id, **kwargs):
        deliveries[-1].append((user_id, kwargs["main_provider_config"]))
        return "observation"

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "token",
        observe_photo=observe,
    )
    async def redelivery():
        for _ in range(2):
            deliveries.append([])
            observer = worker._make_photo_observer(
                deps,
                user_id="usr_vision_tool",
                provider_config=_attempt_config(job_id=94),
                api_key=None,
                runtime_token="token",
            )
            assert await observer(
                "image/jpeg", "private-b64", call_id="photo-call-1"
            ) == "observation"
            assert await observer(
                "image/jpeg", "private-b64", call_id="photo-call-1"
            ) == "observation"

    asyncio.run(redelivery())

    call_ids = [item[1].provider_attempt_context.call_id for item in deliveries[0]]
    assert len(set(call_ids)) == 2
    assert call_ids == [
        item[1].provider_attempt_context.call_id for item in deliveries[1]
    ]


def test_payload_is_bounded_compressed_and_explicitly_truncated():
    secret = "private-conversation-needle"
    encoded, truncated, original_size = trajectory.encode_payload(
        "provider_request",
        {"messages": [{"role": "user", "content": secret * 100}]},
        max_json_bytes=600,
    )
    assert truncated is True
    assert original_size > 600
    assert secret.encode() not in encoded
    decoded = trajectory.decode_payload(encoded)
    assert decoded["truncated"] is True
    assert decoded["original_json_bytes"] == original_size
    assert len(decoded["json_prefix"].encode("utf-8")) < 600


def test_exact_payload_parts_round_trip_without_truncation():
    secret = "完整上下文-" * 12_000
    encoded_parts, original_size = trajectory.encode_payload_parts(
        "provider_request",
        {"messages": [{"role": "user", "content": secret}]},
        max_json_bytes=64 * 1024,
    )
    assert len(encoded_parts) > 1
    assert original_size > 64 * 1024
    physical = []
    for index, encoded in enumerate(encoded_parts):
        decoded = trajectory.decode_payload(encoded)
        decoded["event_index"] = index
        decoded["capture_truncated"] = False
        physical.append(decoded)
    logical = trajectory.reassemble_payload_parts(physical)
    assert len(logical) == 1
    assert logical[0]["payload"]["messages"][0]["content"] == secret
    assert logical[0]["storage_chunk_count"] == len(encoded_parts)


def test_identical_chunked_events_remain_distinct_logical_documents():
    payload = {"messages": [{"role": "user", "content": "same" * 30_000}]}
    physical = []
    for document_id in ("logical-a", "logical-b"):
        encoded_parts, _ = trajectory.encode_payload_parts(
            "provider_request",
            payload,
            max_json_bytes=64 * 1024,
            document_id=document_id,
        )
        for encoded in encoded_parts:
            event = trajectory.decode_payload(encoded)
            event["event_index"] = len(physical)
            physical.append(event)
    logical = trajectory.reassemble_payload_parts(physical)
    assert len(logical) == 2
    assert logical[0]["payload"] == logical[1]["payload"] == payload


def test_chunk_reassembly_rejects_tampering_and_labels_incomplete_windows():
    encoded_parts, _ = trajectory.encode_payload_parts(
        "provider_request",
        {"messages": [{"role": "user", "content": "private" * 20_000}]},
        max_json_bytes=64 * 1024,
        document_id="logical-tamper-test",
    )
    decoded = [trajectory.decode_payload(part) for part in encoded_parts]
    incomplete = trajectory.reassemble_payload_parts(decoded[1:])
    assert incomplete == [
        {
            "schema": "feedling.runtime_v2.trajectory_chunk_incomplete.v1",
            "kind": "provider_request",
            "document_id": "logical-tamper-test",
            "document_sha256": decoded[0]["document_sha256"],
            "original_json_bytes": decoded[0]["original_json_bytes"],
            "chunk_count": len(decoded),
            "captured_chunk_count": len(decoded) - 1,
            "event_index": None,
            "capture_truncated": False,
        }
    ]
    original_chunk = base64.b64decode(decoded[0]["chunk_b64"])
    decoded[0]["chunk_b64"] = base64.b64encode(
        bytes([original_chunk[0] ^ 1]) + original_chunk[1:]
    ).decode("ascii")
    with pytest.raises(ValueError, match="digest mismatch"):
        trajectory.reassemble_payload_parts(decoded)


def test_exact_codec_rejects_part_caps_below_safe_batch_floor():
    with pytest.raises(ValueError, match="invalid trajectory event byte cap"):
        trajectory.encode_payload_parts(
            "provider_request",
            {"messages": []},
            max_json_bytes=(64 * 1024) - 1,
        )


def test_recorder_batches_exact_large_event_once():
    plaintext_by_item: dict[str, bytes] = {}
    batches: list[list[dict]] = []

    def seal(user_id, plaintext, item_id):
        plaintext_by_item[item_id] = plaintext
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": base64.b64encode(plaintext).decode(),
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(_job_id, _user_id, **_kwargs):
        raise AssertionError("large production event should use the batch append")

    def append_batch(_job_id, _user_id, *, events):
        batches.append(events)
        return list(range(9, 9 + len(events)))

    content = "x" * (trajectory.MAX_EVENT_JSON_BYTES + 1024)
    recorder = trajectory.TrajectoryRecorder(
        job_id=11,
        user_id="u1",
        seal=seal,
        append=append,
        append_batch=append_batch,
    )
    index = asyncio.run(
        recorder.record(
            "provider_request",
            {"messages": [{"role": "user", "content": content}]},
        )
    )
    assert index == 9
    assert len(batches) == 1
    assert len(batches[0]) > 1
    physical = []
    for offset, event in enumerate(batches[0]):
        assert event["truncated"] is False
        decoded = trajectory.decode_payload(
            plaintext_by_item[event["payload_envelope"]["id"]]
        )
        decoded["event_index"] = 9 + offset
        decoded["capture_truncated"] = False
        physical.append(decoded)
    logical = trajectory.reassemble_payload_parts(physical)
    assert logical[0]["payload"]["messages"][0]["content"] == content


def test_exact_codec_preserves_deep_json_without_omission():
    payload: dict = {"leaf": "kept"}
    for index in range(40):
        payload = {f"level_{index}": payload}

    parts, _size = trajectory.encode_payload_parts(
        "provider_request",
        payload,
        max_json_bytes=900 * 1024,
    )

    decoded = trajectory.decode_payload(parts[0])
    assert "omitted" not in json.dumps(decoded)
    cursor = decoded["payload"]
    for index in reversed(range(40)):
        cursor = cursor[f"level_{index}"]
    assert cursor == {"leaf": "kept"}


def test_exact_codec_rejects_unsupported_values_instead_of_omitting():
    with pytest.raises(TypeError, match="unsupported exact trajectory value"):
        trajectory.encode_payload_parts("provider_request", {"bad": object()})


def test_recorder_seals_before_append_and_uses_deterministic_ids():
    calls = []

    def seal(user_id, plaintext, item_id):
        calls.append(("seal", user_id, item_id, plaintext))
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": base64.b64encode(plaintext).decode(),
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(job_id, user_id, **kwargs):
        calls.append(("append", job_id, user_id, kwargs))
        return 4

    recorder = trajectory.TrajectoryRecorder(
        job_id=12,
        user_id="u1",
        seal=seal,
        append=append,
    )
    index = asyncio.run(recorder.record("reply_planned", {"text": "secret"}))
    assert index == 4
    assert calls[0][0] == "seal"
    assert calls[1][0] == "append"
    event_key = calls[1][3]["idempotency_key"]
    assert event_key.endswith("_0000_reply_planned")
    assert calls[0][2] == trajectory.trajectory_item_id(12, event_key)
    assert "secret" not in str(calls[1][3]["payload_envelope"])


def test_best_effort_failure_emits_gap_marker_before_terminal():
    plaintext_by_id: dict[str, dict] = {}
    appended: list[dict] = []

    def seal(user_id, plaintext, item_id):
        plaintext_by_id[item_id] = trajectory.decode_payload(plaintext)
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": "ct",
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(_job_id, _user_id, **event):
        if event["event_kind"] == "reply_effect_disposition":
            raise RuntimeError("simulated append failure")
        appended.append(event)
        return len(appended) - 1

    recorder = trajectory.TrajectoryRecorder(
        job_id=13,
        user_id="u1",
        seal=seal,
        append=append,
    )
    assert asyncio.run(
        recorder.record_best_effort(
            "reply_effect_disposition",
            {"status": "applied"},
        )
    ) is False
    assert asyncio.run(
        recorder.record_best_effort("turn_terminal", {"outcome": "completed"})
    ) is True
    assert [event["event_kind"] for event in appended] == [
        "capture_gap",
        "turn_terminal",
    ]
    gap_envelope = appended[0]["payload_envelope"]
    gap = plaintext_by_id[gap_envelope["id"]]["payload"]
    assert gap == {
        "failed_capture_events": 1,
        "failed_event_kinds": {"reply_effect_disposition": 1},
    }


def test_terminal_waits_for_parallel_scope_failure_before_gap_snapshot():
    entered = threading.Event()
    release = threading.Event()
    appended = []

    def seal(user_id, _plaintext, item_id):
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": "ct",
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(_job_id, _user_id, **event):
        if event["event_kind"] == "provider_response":
            entered.set()
            assert release.wait(timeout=2)
            raise RuntimeError("parallel append failed")
        appended.append(event["event_kind"])
        return len(appended) - 1

    recorder = trajectory.TrajectoryRecorder(
        job_id=14,
        user_id="u1",
        seal=seal,
        append=append,
    )

    async def scenario():
        child = asyncio.create_task(
            recorder.scoped("child").record(
                "provider_response",
                {"reply": "late"},
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        terminal = asyncio.create_task(
            recorder.record("turn_terminal", {"outcome": "failed"})
        )
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError, match="parallel append failed"):
            await child
        await terminal

    asyncio.run(scenario())
    assert appended == ["capture_gap", "turn_terminal"]


def test_recorder_attempt_identity_dedupes_redelivery_but_separates_new_attempt():
    seen: dict[str, int] = {}
    appended: list[str] = []

    def seal(user_id, _plaintext, item_id):
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": "ct",
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(_job_id, _user_id, **kwargs):
        key = kwargs["idempotency_key"]
        appended.append(key)
        return seen.setdefault(key, len(seen))

    def recorder(attempt):
        return trajectory.TrajectoryRecorder(
            job_id=21,
            user_id="u1",
            seal=seal,
            append=append,
            attempt_identity=attempt,
        )

    first = asyncio.run(recorder(3).record("provider_request", {"round": 1}))
    redelivered = asyncio.run(recorder(3).record("provider_request", {"round": 1}))
    new_attempt = asyncio.run(recorder(4).record("provider_request", {"round": 1}))
    assert (first, redelivered, new_attempt) == (0, 0, 1)
    assert appended[0] == appended[1]
    assert appended[2] != appended[0]


def test_parallel_recorder_scopes_have_stable_independent_ordinals():
    keys = []

    def seal(user_id, _plaintext, item_id):
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": "ct",
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(_job_id, _user_id, **kwargs):
        keys.append(kwargs["idempotency_key"])
        return len(keys) - 1

    recorder = trajectory.TrajectoryRecorder(
        job_id=22,
        user_id="u1",
        seal=seal,
        append=append,
        attempt_identity=7,
    )

    async def run():
        left = recorder.scoped("subagent:left")
        right = recorder.scoped("subagent:right")
        await asyncio.gather(
            left.record("provider_request", {"round": 1}),
            right.record("provider_request", {"round": 1}),
        )
        await recorder.scoped("subagent:left").record(
            "provider_response",
            {"round": 1},
        )

    asyncio.run(run())
    assert len(set(keys)) == 3
    assert keys[0].endswith("_0000_provider_request")
    assert keys[1].endswith("_0000_provider_request")
    assert keys[2].endswith("_0001_provider_response")
    assert keys[0].split("_0000_")[0] != keys[1].split("_0000_")[0]


def test_recorder_retries_ambiguous_append_with_the_same_event_key():
    attempted_keys = []
    stored = {}

    def seal(user_id, _plaintext, item_id):
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": "ct",
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    def append(_job_id, _user_id, **kwargs):
        key = kwargs["idempotency_key"]
        attempted_keys.append(key)
        index = stored.setdefault(key, len(stored))
        if len(attempted_keys) == 1:
            raise RuntimeError("commit acknowledgement lost")
        return index

    recorder = trajectory.TrajectoryRecorder(
        job_id=23,
        user_id="u1",
        seal=seal,
        append=append,
        attempt_identity=2,
    )

    async def run():
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            await recorder.record("provider_request", {"round": 1})
        return await recorder.record("provider_request", {"round": 1})

    index = asyncio.run(run())
    assert index == 0
    assert attempted_keys[0] == attempted_keys[1]
    assert len(stored) == 1


def test_tool_trajectory_result_includes_durable_effect_evidence():
    captured = []

    class Recorder:
        def scoped(self, _scope):
            return self

        async def record(self, kind, payload):
            captured.append((kind, payload))

    callback = worker._make_tool_trajectory_callback(
        Recorder(),
        {
            "call-1": {
                "domain": "platform",
                "effect_id": "effect-1",
                "effect_type": "workspace_write",
                "status": "applied",
            }
        },
    )
    asyncio.run(
        callback(
            SimpleNamespace(id="call-1", name="workspace_write"),
            "tool_call_result",
            {
                "phase": "platform_mutation",
                "duration_ms": 4.25,
                "result": ToolResult(call_id="call-1", content="ok"),
            },
        )
    )
    assert captured[0][1]["effect"] == {
        "domain": "platform",
        "effect_id": "effect-1",
        "effect_type": "workspace_write",
        "status": "applied",
    }
    assert captured[0][1]["duration_ms"] == 4.25


def test_reused_tool_call_id_does_not_inherit_prior_round_effect():
    captured = []

    class Recorder:
        def scoped(self, _scope):
            return self

        async def record(self, kind, payload):
            captured.append((kind, payload))

    evidence = {
        "same-id": {
            "domain": "platform",
            "effect_id": "old-effect",
            "status": "applied",
        }
    }
    callback = worker._make_tool_trajectory_callback(Recorder(), evidence)
    call = SimpleNamespace(id="same-id", name="memory_search")
    asyncio.run(
        callback(call, "tool_call_started", {"phase": "platform_read"})
    )
    asyncio.run(
        callback(
            call,
            "tool_call_result",
            {
                "phase": "platform_read",
                "result": ToolResult(call_id="same-id", content="found"),
            },
        )
    )
    assert evidence == {}
    assert all("effect" not in payload for _kind, payload in captured)


def test_tool_loop_records_request_response_tools_results_and_replies(monkeypatch):
    responses = iter(
        [
            {
                "reply": "",
                "tool_calls": [
                    {"id": "c1", "name": "memory_search", "args": {"query": "needle"}},
                ],
                "usage": {},
            },
            {"reply": "done", "tool_calls": [], "usage": {}},
        ]
    )

    async def provider(_config, _messages, *, tools=None):
        return next(responses)

    async def dispatch(calls):
        return [ToolResult(call_id=calls[0].id, content="found" * 400_000)]

    async def reply(_text, *, final, reasoning=""):
        assert isinstance(final, bool)

    async def fold():
        return []

    events = []

    async def record(kind, payload):
        events.append((kind, payload))

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    outcome = asyncio.run(
        tool_loop.run_tool_loop(
            provider_config=provider_client.ProviderConfig(
                provider="anthropic",
                model="claude-sonnet-4-test",
                api_key="secret-key",
            ),
            build_messages=lambda transcript: [
                {"role": "user", "content": "hi"},
                *transcript,
            ],
            dispatch_tools=dispatch,
            on_reply=reply,
            fold_new_messages=fold,
            add_usage=lambda _usage: None,
            max_calls=3,
            on_trajectory_event=record,
        )
    )
    assert outcome.final_text == "done"
    kinds = [kind for kind, _payload in events]
    assert kinds == [
        "provider_request",
        "provider_response",
        "tool_batch_planned",
        "tool_batch_result",
        "provider_request",
        "provider_response",
        "reply_planned",
    ]
    assert all("secret-key" not in str(payload) for _kind, payload in events)
    result_event = next(
        payload for kind, payload in events if kind == "tool_batch_result"
    )
    assert len(result_event["results"][0].content) < 100_000


def test_provider_failure_leaves_a_partial_request_error_trajectory(monkeypatch):
    async def provider(_config, _messages, *, tools=None):
        error = RuntimeError("upstream private diagnostic")
        error.feedling_provider_attempt_trace = {
            "version": 1,
            "attempts": [
                {
                    "ordinal": 1,
                    "kind": "http_attempt",
                    "status": 503,
                    "duration_ms": 12.5,
                }
            ],
        }
        raise error

    async def forbidden_dispatch(_calls):
        raise AssertionError("failed provider call must not dispatch")

    async def forbidden_reply(_text, *, final, reasoning=""):
        raise AssertionError("failed provider call must not reply")

    events = []

    async def record(kind, payload):
        events.append((kind, payload))

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    with pytest.raises(RuntimeError, match="upstream private diagnostic"):
        asyncio.run(
            tool_loop.run_tool_loop(
                provider_config=provider_client.ProviderConfig(
                    provider="anthropic",
                    model="claude-sonnet-4-test",
                    api_key="provider-secret",
                ),
                build_messages=lambda _transcript: [
                    {"role": "user", "content": "private prompt"},
                ],
                dispatch_tools=forbidden_dispatch,
                on_reply=forbidden_reply,
                fold_new_messages=lambda: None,
                add_usage=lambda _usage: None,
                max_calls=1,
                on_trajectory_event=record,
            )
        )
    assert [kind for kind, _payload in events] == [
        "provider_request",
        "provider_error",
    ]
    assert events[0][1]["messages"][0]["content"] == "private prompt"
    assert events[1][1]["provider_attempt_trace"]["attempts"][0]["status"] == 503


def test_failure_review_has_no_reply_effect_mcp_or_workspace_surface(monkeypatch):
    source_event, _truncated, _size = trajectory.encode_payload(
        "turn_exception",
        {"error": "provider timeout after tool read"},
    )
    provider_calls = []
    finished = []

    monkeypatch.setattr(jobs_store, "mark_running", lambda *_a, **_k: True)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: True)
    monkeypatch.setattr(jobs_store, "trajectory_review_enabled", lambda: True)
    monkeypatch.setattr(worker.kill_switch, "turns_halted", lambda: False)
    monkeypatch.setattr(
        jobs_store,
        "claim_failure_review",
        lambda *_a, **_k: {"source_job_id": 7},
    )
    monkeypatch.setattr(
        jobs_store,
        "get_trajectory_capture_state",
        lambda *_a, **_k: {
            "last_event_index": 0,
            "event_count": 1,
            "any_truncated": False,
        },
    )
    monkeypatch.setattr(
        jobs_store,
        "list_trajectory_events",
        lambda *_a, **_k: [
            {
                "event_index": 0,
                "payload_envelope": {"opaque": True},
                "truncated": False,
            }
        ],
    )
    monkeypatch.setattr(
        jobs_store,
        "finish_failure_review",
        lambda **kwargs: (
            finished.append(kwargs)
            or {
                "settled": True,
                "review_status": "completed",
            }
        ),
    )
    monkeypatch.setattr(jobs_store, "record_whole_turn_metric", lambda *_a, **_k: None)

    async def provider(config, messages, *, tools=None, **kwargs):
        provider_calls.append({
            "messages": messages,
            "tools": tools,
            "attempt_context": config.provider_attempt_context,
            **kwargs,
        })
        return {"reply": '{"failure_class":"timeout"}', "tool_calls": [], "usage": {}}

    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("review lane touched a live side-effect dependency")

    def seal(user_id, plaintext, item_id):
        return {
            "v": 1,
            "id": item_id,
            "owner_user_id": user_id,
            "visibility": "shared",
            "body_ct": base64.b64encode(plaintext).decode(),
            "nonce": "n",
            "K_user": "u",
            "K_enclave": "e",
        }

    deps = worker.TurnDeps(
        read_messages=forbidden,
        resolve_provider=lambda _uid: (
            provider_client.ProviderConfig(
                provider="anthropic",
                model="review-test",
                api_key="provider-secret",
            ),
            {},
        ),
        mint_enclave_token=lambda _uid: "runtime-token",
        apply_pending_effects=forbidden,
        load_mcp_turn=forbidden,
        load_workspace_prompt=forbidden,
        seal_trajectory_payload=seal,
        open_trajectory_payload=lambda _uid, _envelope, _token: source_event,
    )
    tm = worker.TurnMetrics(job_id=99, user_id="u1", lane="trajectory_review")
    result = asyncio.run(
        worker._run_trajectory_review_turn(
            {"id": 99, "user_id": "u1", "claimed_by": "worker-1"},
            deps,
            tm,
        )
    )
    assert result == "completed"
    assert len(provider_calls) == 1
    assert provider_calls[0]["tools"] is None
    assert provider_calls[0]["attempt_context"].call_id == "v2job:99:review:1"
    assert finished[0]["source_job_id"] == 7
    assert "review_envelope" in finished[0]
    assert finished[0]["captured_next_event_index"] == 0


def test_failure_review_kill_switch_stops_before_provider(monkeypatch):
    failed = []

    monkeypatch.setattr(jobs_store, "mark_running", lambda *_a, **_k: True)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: True)
    monkeypatch.setattr(jobs_store, "trajectory_review_enabled", lambda: False)
    monkeypatch.setattr(
        jobs_store,
        "mark_failed",
        lambda *args, **kwargs: failed.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(jobs_store, "record_whole_turn_metric", lambda *_a, **_k: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled review reached a provider/decrypt dependency")

    deps = worker.TurnDeps(
        read_messages=forbidden,
        resolve_provider=forbidden,
        mint_enclave_token=forbidden,
        apply_pending_effects=forbidden,
        open_trajectory_payload=forbidden,
        seal_trajectory_payload=forbidden,
    )
    tm = worker.TurnMetrics(job_id=100, user_id="u1", lane="trajectory_review")

    result = asyncio.run(
        worker._run_trajectory_review_turn(
            {"id": 100, "user_id": "u1", "claimed_by": "worker-1"},
            deps,
            tm,
        )
    )

    assert result == "failed"
    assert failed[0][0][1] == "trajectory_review_disabled"


@pytest.mark.parametrize("raw_cap", ["0", "-1", "nope", "10001"])
def test_failure_review_bad_admission_config_fails_closed(monkeypatch, raw_cap):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE", raw_cap)
    assert jobs_store.trajectory_review_enabled() is False


def test_failure_review_reconciler_default_off_does_not_touch_db(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED", raising=False)

    def forbidden_pool():
        raise AssertionError("default-off review reconciler touched PostgreSQL")

    monkeypatch.setattr(jobs_store, "_pool", forbidden_pool)
    assert jobs_store.reconcile_failure_review_runners() == 0


@pytest.mark.parametrize("limit", [0, -1, 1001, True])
def test_failure_review_reconciler_rejects_unbounded_limits(limit):
    with pytest.raises(ValueError, match="1..1000"):
        jobs_store.reconcile_failure_review_runners(limit=limit)


def test_review_prompt_retains_an_oversized_newest_event_as_bounded_evidence():
    messages = trajectory.build_review_messages(
        [
            {"event_index": 1, "kind": "provider_request", "payload": "older"},
            {
                "event_index": 2,
                "kind": "turn_exception",
                "payload": "failure" * 100_000,
            },
        ],
        source_job_id=9,
        max_prompt_bytes=4096,
    )
    content = messages[1]["content"]
    assert '"event_index":2' in content
    assert '"kind":"turn_exception"' in content
    assert '"review_truncated":true' in content
    assert len(content.encode("utf-8")) < 5_000
