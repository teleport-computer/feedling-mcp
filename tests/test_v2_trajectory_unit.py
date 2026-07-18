from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import provider_client
from provider_types import ToolResult
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import tool_loop
from model_api_runtime.v2 import trajectory
from model_api_runtime.v2 import worker


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

    async def reply(_text, *, final):
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
        raise RuntimeError("upstream private diagnostic")

    async def forbidden_dispatch(_calls):
        raise AssertionError("failed provider call must not dispatch")

    async def forbidden_reply(_text, *, final):
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

    async def provider(_config, messages, *, tools=None, **kwargs):
        provider_calls.append({"messages": messages, "tools": tools, **kwargs})
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
