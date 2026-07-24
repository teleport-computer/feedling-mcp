from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from core import store as core_store  # noqa: E402
from model_api_runtime.v2 import extraction, jobs_store, serve_worker, worker  # noqa: E402
from proactive import capture_scheduler, proactive_core  # noqa: E402


class _Store:
    def __init__(self, messages=None):
        self.user_id = "u-v2-capture"
        self.chat_messages = list(messages or [])
        self.chat_lock = threading.RLock()
        self.events = []

    def append_device_event(self, event):
        self.events.append(event)


def _envelope(message_id: str) -> dict:
    return {
        "id": message_id,
        "body_ct": "ct",
        "nonce": "nonce",
        "K_user": "wrapped",
    }


def _fake_capture_protocol(state: dict, terminal: list):
    batches = {}

    def _prepare(**kwargs):
        batch_id = len(batches) + 1
        batches[batch_id] = dict(kwargs["window"])
        return {"id": batch_id, "actions_json": list(kwargs["actions"])}

    def _commit(**kwargs):
        window = batches.pop(kwargs["batch_id"])
        state.update(
            {
                "last_captured_until_message_id": window["until_message_id"],
                "last_captured_until_ts": window["until_ts"],
                "last_captured_until_seq": window["through_seq"],
                "capture_seq_initialized": True,
            }
        )
        terminal.append(dict(window))
        return {"committed": True}

    return {
        "get_prepared_capture_batch": lambda **_kwargs: None,
        "prepare_capture_batch": _prepare,
        "authorize_capture_provider_call": lambda **_kwargs: {"authorized": True},
        "commit_capture_batch": _commit,
        "fail_capture_job": lambda **_kwargs: False,
    }


def test_exact_v2_chat_send_and_reply_refresh_without_legacy_enqueue(monkeypatch):
    store = _Store()
    monkeypatch.setattr(
        core_store.db,
        "chat_append_and_enqueue",
        lambda *_args, **_kwargs: (1, "job-1"),
    )
    monkeypatch.setattr(
        core_store.db,
        "chat_append_effect_with_cursor",
        lambda *_args, **_kwargs: (2, True),
    )
    monkeypatch.setattr(core_store.wake_bus, "notify", lambda *_args: None)
    refreshed = []
    legacy = []
    monkeypatch.setattr(
        capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda _store, **kwargs: refreshed.append(kwargs) or {},
    )
    monkeypatch.setattr(
        capture_scheduler,
        "record_chat_append",
        lambda *_args, **_kwargs: legacy.append(True),
    )

    core_store.UserStore.append_chat(
        store,
        "user",
        "chat",
        _envelope("user-1"),
        strict=True,
        enqueue={
            "lane": "chat",
            "expected_runtime_mode": "db_action_v2",
        },
    )
    core_store.UserStore.append_chat(
        store,
        "openclaw",
        "model_api",
        _envelope("reply-1"),
        strict=True,
        reply_through_seq=1,
    )

    assert len(refreshed) == 2
    assert legacy == []


def test_transactional_v2_reply_refreshes_without_legacy_enqueue(monkeypatch):
    class _ReplyStore(_Store):
        def _build_chat_message(self, role, source, envelope):
            return {
                "id": envelope["id"],
                "role": role,
                "source": source,
                "ts": 10.0,
            }

        def reload_chat_strict(self):
            return []

        def notify_chat_waiters(self):
            return None

    class _Connection:
        def execute(self, *_args, **_kwargs):
            return None

    store = _ReplyStore()
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: store)
    monkeypatch.setattr(
        serve_worker.db,
        "chat_append_effect_with_cursor",
        lambda *_args, **_kwargs: (7, True, lambda: None),
    )
    refreshed = []
    legacy = []
    monkeypatch.setattr(
        capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda *_args, **_kwargs: refreshed.append(True) or {},
    )
    monkeypatch.setattr(
        capture_scheduler,
        "record_chat_append",
        lambda *_args, **_kwargs: legacy.append(True),
    )

    finish = serve_worker._sink_reply_in_transaction(
        "u-v2-capture",
        {"envelope": _envelope("reply-tx"), "reply_through_seq": 5},
        _Connection(),
    )
    finish()

    assert refreshed == [True]
    assert legacy == []


def test_v2_device_boundary_and_compatibility_endpoints_never_write_legacy(
    monkeypatch,
):
    store = _Store()
    monkeypatch.setattr(
        proactive_core.hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda _store: proactive_core.hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2,
    )
    monkeypatch.setattr(
        proactive_core.service,
        "_make_device_event",
        lambda **_kwargs: {"type": "app_background", "payload": {}, "ts": 10.0},
    )
    monkeypatch.setattr(
        capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        proactive_core.dream_scheduler, "load_dream_state", lambda _store: {}
    )

    monkeypatch.setenv("FEEDLING_V2_CAPTURE_ENABLED", "1")
    calls = []

    def _v2_scheduler(_store, *_args, **kwargs):
        assert kwargs.get("submit") is proactive_core._submit_v2_capture
        calls.append(True)
        return {"enqueued": False, "reason": "v2_coalesced", "state": {}, "job": None}

    monkeypatch.setattr(capture_scheduler, "handle_device_event", _v2_scheduler)
    monkeypatch.setattr(capture_scheduler, "tick_quiet_capture", _v2_scheduler)
    monkeypatch.setattr(capture_scheduler, "force_capture", _v2_scheduler)
    def _legacy_forbidden(*_args, **_kwargs):
        raise AssertionError("V2 compatibility path touched the legacy substrate")

    monkeypatch.setattr(
        proactive_core.dream_scheduler, "tick_memory_dream", _legacy_forbidden
    )

    event = proactive_core.device_events_append(
        store, {"type": "app_background", "payload": {}}
    )
    capture_tick, status = proactive_core.capture_tick(store, {})
    capture_force = proactive_core.capture_force(store)
    dream_tick, dream_status = proactive_core.dream_tick(store, {})

    assert event["capture"]["reason"] == "v2_coalesced"
    assert status == 200 and capture_tick["reason"] == "v2_coalesced"
    assert capture_tick["dream"]["reason"] == "v2_scheduler_owned"
    assert capture_tick["migrate"]["reason"] == "v2_scheduler_owned"
    assert capture_force["reason"] == "v2_coalesced"
    assert dream_status == 200 and dream_tick["reason"] == "v2_scheduler_owned"
    assert len(calls) == 3


def test_v2_capture_endpoints_fail_closed_when_global_flag_is_off(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_CAPTURE_ENABLED", raising=False)
    called = []
    result = proactive_core._submit_v2_capture(
        _Store(),
        trigger="manual_force",
        now=1.0,
        window={"after_seq": 0},
        capture_key="capture:k",
    )
    assert result == {"enqueued": False, "reason": "capture_disabled", "job": None}
    assert called == []


@pytest.mark.parametrize("settings_result", [{"capture_enabled": False}, RuntimeError("db down")])
def test_device_boundary_fails_closed_on_user_opt_out_or_settings_error(
    monkeypatch, settings_result
):
    store = _Store(
        [{"id": "m1", "ts": 1.0, "role": "user", "source": "chat"}]
    )
    if isinstance(settings_result, Exception):
        monkeypatch.setattr(
            capture_scheduler.db,
            "get_blob_strict",
            lambda *_args: (_ for _ in ()).throw(settings_result),
        )
    else:
        monkeypatch.setattr(
            capture_scheduler.db,
            "get_blob_strict",
            lambda *_args: dict(settings_result),
        )
    submitted = []
    result = capture_scheduler.handle_device_event(
        store,
        {"type": "app_background", "ts": 2.0},
        submit=lambda *_args, **_kwargs: submitted.append(True),
    )
    assert result["reason"] == "capture_disabled"
    assert submitted == []


def test_device_control_read_failure_never_falls_back_to_legacy(monkeypatch):
    store = _Store()
    monkeypatch.setattr(
        proactive_core.service,
        "_make_device_event",
        lambda **_kwargs: {"type": "app_background", "payload": {}, "ts": 10.0},
    )
    monkeypatch.setattr(
        proactive_core.hosted_config_store,
        "get_hosted_runtime_mode_strict",
        lambda _store: (_ for _ in ()).throw(RuntimeError("control unavailable")),
    )
    legacy = []
    monkeypatch.setattr(
        capture_scheduler,
        "handle_device_event",
        lambda *_args: legacy.append(True),
    )

    with pytest.raises(RuntimeError, match="control unavailable"):
        proactive_core.device_events_append(store, {"type": "app_background"})
    assert legacy == []


@pytest.mark.parametrize(
    ("state_patch", "reason"),
    [
        ({"last_capture_completed_at": 999.0}, "min_interval"),
        (
            {"capture_fail_streak": 1, "last_capture_failed_at": 999.0},
            "failure_backoff",
        ),
    ],
)
def test_v2_submit_preserves_min_interval_and_failure_backoff(
    monkeypatch, state_patch, reason
):
    state = {
        "last_seen_message_id": "m9",
        "last_seen_ts": 1.0,
        "last_captured_until_message_id": "",
        "message_count": 3,
        "turns_since_capture": 1,
        **state_patch,
    }
    monkeypatch.setattr(
        capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda *_args, **_kwargs: dict(state),
    )
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda _store: True)
    monkeypatch.setattr(capture_scheduler, "quiet_sec", lambda: 0.0)
    submitted = []

    result = capture_scheduler.tick_quiet_capture(
        object(),
        now=1000.0,
        submit=lambda *_args, **_kwargs: submitted.append(True),
    )

    assert result["reason"] == reason
    assert submitted == []


def test_v2_turn_backstop_is_runner_owned(monkeypatch):
    state = {
        "last_seen_message_id": "m24",
        "last_seen_ts": 999.9,
        "last_captured_until_message_id": "",
        "message_count": 24,
        "turns_since_capture": 24,
    }
    monkeypatch.setattr(
        capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda *_args, **_kwargs: dict(state),
    )
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda _store: True)
    monkeypatch.setattr(capture_scheduler, "turn_backstop", lambda: 24)
    monkeypatch.setattr(
        capture_scheduler,
        "_patch_capture_state",
        lambda _store, saved, **_kwargs: dict(saved),
    )
    seen = []

    def _submit(_store, **kwargs):
        seen.append(kwargs)
        return {
            "enqueued": True,
            "reason": "v2",
            "job": {
                "job_kind": "memory_capture",
                "status": "pending",
                "capture_key": kwargs["capture_key"],
            },
        }

    result = capture_scheduler.tick_quiet_capture(
        object(), now=1000.0, submit=_submit
    )

    assert result["enqueued"] is True
    assert seen[0]["trigger"] == "turn_backstop"


def test_v2_submit_ignores_stale_legacy_pending_rows(monkeypatch):
    state = {
        "last_seen_message_id": "m2",
        "last_seen_ts": 1.0,
        "last_captured_until_message_id": "m1",
        "pending_capture_key": "legacy-stale",
        "message_count": 1,
        "turns_since_capture": 1,
    }
    monkeypatch.setattr(
        capture_scheduler,
        "refresh_capture_state_from_chat",
        lambda *_args, **_kwargs: dict(state),
    )
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda _store: True)
    monkeypatch.setattr(capture_scheduler, "quiet_sec", lambda: 0.0)
    monkeypatch.setattr(
        capture_scheduler.capture_jobs,
        "_find_active_capture",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("V2 consulted legacy pending rows")
        ),
    )
    monkeypatch.setattr(
        capture_scheduler,
        "_patch_capture_state",
        lambda _store, saved, **_kwargs: dict(saved),
    )

    result = capture_scheduler.tick_quiet_capture(
        object(),
        now=1000.0,
        submit=lambda _store, **kwargs: {
            "enqueued": False,
            "reason": "v2_coalesced",
            "job": {
                "job_kind": "memory_capture",
                "status": "pending",
                "capture_key": kwargs["capture_key"],
            },
        },
    )

    assert result["reason"] == "v2_coalesced"


def test_v2_capture_coalesce_does_not_notify_or_count(monkeypatch):
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: object())

    def _tick(store, *, submit):
        return submit(
            store,
            trigger="quiet_timeout",
            now=10.0,
            window={"until_message_id": "m1"},
            capture_key="capture:k",
        )

    monkeypatch.setattr(capture_scheduler, "tick_quiet_capture", _tick)
    monkeypatch.setattr(
        jobs_store, "enqueue_job", lambda *_args, **_kwargs: ("job-1", True)
    )
    notifications = []
    monkeypatch.setattr(
        serve_worker.core_wake_bus,
        "notify",
        lambda *_args: notifications.append(True),
    )

    assert serve_worker._tick_capture_for_user("u1") == 0
    assert notifications == []


def test_v2_dream_coalesce_does_not_notify_or_count(monkeypatch):
    monkeypatch.setattr(serve_worker.core_store, "get_store", lambda _uid: object())

    def _tick(store, *, submit):
        return submit(store, trigger="nightly_dream", now=10.0)

    monkeypatch.setattr(
        serve_worker.dream_scheduler, "tick_memory_dream", _tick
    )
    monkeypatch.setattr(
        jobs_store, "enqueue_job", lambda *_args, **_kwargs: ("job-dream", True)
    )
    notifications = []
    monkeypatch.setattr(
        serve_worker.core_wake_bus,
        "notify",
        lambda *_args: notifications.append(True),
    )

    assert serve_worker._tick_dream_for_user("u1") == 0
    assert notifications == []


def _capture_blob(monkeypatch, initial):
    blob = dict(initial)
    monkeypatch.setattr(
        capture_scheduler.db,
        "get_blob",
        lambda _uid, _kind: dict(blob),
    )
    monkeypatch.setattr(
        capture_scheduler.db,
        "get_blob_strict",
        lambda _uid, _kind: dict(blob),
    )

    def _patch(_store, value, *, expected_frontier_id=None, **_kwargs):
        if (
            expected_frontier_id is not None
            and str(blob.get("last_captured_until_message_id") or "")
            != expected_frontier_id
        ):
            return capture_scheduler._state_doc(blob)
        blob.update(value)
        return capture_scheduler._state_doc(blob)

    monkeypatch.setattr(capture_scheduler, "_patch_capture_state", _patch)
    monkeypatch.setattr(
        capture_scheduler.capture_jobs, "notify_backoff", lambda *_args, **_kwargs: None
    )
    return blob


def test_v2_capture_success_advances_exact_processed_frontier(monkeypatch):
    store = _Store(
        [
            {"id": "m0", "ts": 1.0, "role": "user", "source": "chat"},
            {"id": "m1", "ts": 2.0, "role": "openclaw", "source": "model_api"},
            {"id": "m2", "ts": 3.0, "role": "user", "source": "chat"},
        ]
    )
    blob = _capture_blob(
        monkeypatch,
        {
            "last_captured_until_message_id": "m0",
            "last_captured_until_ts": 1.0,
            "pending_capture_key": "capture:k",
        },
    )
    monkeypatch.setattr(
        capture_scheduler.db,
        "chat_capture_messages_after_seq",
        lambda *_args, **_kwargs: [
            {"id": "m2", "ts": 3.0, "role": "user", "source": "chat", "seq": 3}
        ],
    )

    result = capture_scheduler.record_v2_capture_status(
        store,
        status="completed",
        window={
            "after_message_id": "m0",
            "until_message_id": "m1",
            "until_ts": 2.0,
        },
        now=20.0,
    )

    assert blob["last_captured_until_message_id"] == "m1"
    assert blob["last_captured_until_ts"] == 2.0
    assert blob["pending_capture_key"] == ""
    assert result["last_seen_message_id"] == "m2"
    assert result["message_count"] == 1


def test_v2_capture_failure_arms_backoff_without_advancing(monkeypatch):
    store = _Store()
    blob = _capture_blob(
        monkeypatch,
        {
            "last_captured_until_message_id": "m0",
            "last_captured_until_ts": 1.0,
            "pending_capture_key": "capture:k",
        },
    )

    capture_scheduler.record_v2_capture_status(
        store,
        status="failed",
        window={"after_message_id": "m0", "until_message_id": "m1"},
        now=20.0,
    )

    assert blob["last_captured_until_message_id"] == "m0"
    assert blob["capture_fail_streak"] == 1
    assert blob["last_capture_failed_at"] == 20.0
    assert capture_scheduler.capture_jobs.in_failure_backoff(1, 20.0, 21.0)


def test_chat_refresh_cannot_restore_a_concurrently_advanced_frontier(monkeypatch):
    store = _Store()
    blob = _capture_blob(
        monkeypatch,
        {
            "last_captured_until_message_id": "m0",
            "last_captured_until_ts": 1.0,
        },
    )

    def _interleaved_messages(_store, _stale_state):
        # Runner commits while this API process still holds its old snapshot.
        blob["last_captured_until_message_id"] = "m1"
        blob["last_captured_until_ts"] = 2.0
        return [
            {"id": "m2", "ts": 3.0, "role": "user", "source": "chat"}
        ]

    monkeypatch.setattr(
        capture_scheduler,
        "_live_messages_after_capture",
        _interleaved_messages,
    )

    capture_scheduler.refresh_capture_state_from_chat(store, now=4.0)

    assert blob["last_captured_until_message_id"] == "m1"
    assert blob["last_captured_until_ts"] == 2.0
    assert blob["last_seen_message_id"] == "m2"


def test_capture_extraction_pages_oldest_contiguous_batches_of_60(monkeypatch):
    rows = [
        {
            "id": f"m{i}",
            "seq": i,
            "ts": float(i),
            "role": "user" if i % 2 else "assistant",
            "raw_role": "user" if i % 2 else "openclaw",
            "source": "chat" if i % 2 else "model_api",
            "capture_eligible": True,
            "content": f"message {i}",
        }
        for i in range(1, 76)
    ]
    state = {
        "last_captured_until_message_id": "",
        "last_captured_until_ts": 0.0,
    }
    reads = []
    terminal = []

    def _read_oldest(_uid, after_seq, limit, *, through_seq):
        reads.append((after_seq, limit, through_seq))
        return [
            dict(row)
            for row in rows
            if after_seq < row["seq"] <= through_seq
        ][:limit]

    async def _empty(**_kwargs):
        return [], None

    monkeypatch.setattr(extraction, "extract", _empty)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: True)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 75)
    monkeypatch.setattr(
        worker.db,
        "chat_seq_for_msg_id",
        lambda _uid, message_id: int(message_id.removeprefix("m")),
    )
    protocol = _fake_capture_protocol(state, terminal)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_compaction_tail_after_seq=_read_oldest,
        read_memory_context=lambda _uid: {},
        read_capture_state=lambda _uid: dict(state),
        build_memory_envelope=lambda *_args: {},
        **protocol,
    )

    first = asyncio.run(
        worker._run_extraction(
            "job-1", "u-v2-capture", "capture", deps, object(), asyncio.Semaphore(1),
            claimed_by="owner",
        )
    )
    second = asyncio.run(
        worker._run_extraction(
            "job-2", "u-v2-capture", "capture", deps, object(), asyncio.Semaphore(1),
            claimed_by="owner",
        )
    )

    assert first == second == "completed"
    assert reads == [(0, 60, 75), (60, 60, 75)]
    assert terminal[0]["until_message_id"] == "m60"
    assert terminal[0]["through_seq"] == 60
    assert terminal[0]["message_count"] == 60
    assert terminal[1]["until_message_id"] == "m75"
    assert terminal[1]["message_count"] == 15


def test_capture_legacy_missing_boundary_restarts_from_zero(monkeypatch):
    read_args = []
    terminal = []

    async def _empty(**_kwargs):
        return [], None

    monkeypatch.setattr(extraction, "extract", _empty)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: True)
    monkeypatch.setattr(worker.db, "chat_seq_for_msg_id", lambda *_args: None)
    monkeypatch.setattr(
        worker.db,
        "seq_for_watermark_ts",
        lambda *_args: (_ for _ in ()).throw(AssertionError("timestamp fallback used")),
    )
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 5)
    state = {
        "last_captured_until_message_id": "pruned",
        "last_captured_until_ts": 10.0,
        "capture_seq_initialized": False,
    }
    protocol = _fake_capture_protocol(state, terminal)

    def _read(_uid, after_seq, limit, *, through_seq):
        read_args.append((after_seq, limit, through_seq))
        return [
            {
                "id": "m5",
                "seq": 5,
                "ts": 10.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "x",
            }
        ]

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_compaction_tail_after_seq=_read,
        read_capture_state=lambda _uid: dict(state),
        build_memory_envelope=lambda *_args: {},
        **protocol,
    )

    result = asyncio.run(
        worker._run_extraction(
            "job-fallback",
            "u-v2-capture",
            "capture",
            deps,
            object(),
            asyncio.Semaphore(1),
            claimed_by="owner",
        )
    )

    assert result == "completed"
    assert read_args == [(0, 60, 5)]
    assert terminal[0]["after_seq"] == 0


def test_capture_legacy_existing_boundary_translates_exact_seq(monkeypatch):
    state = {
        "last_captured_until_message_id": "m4",
        "last_captured_until_ts": 99.0,
        "capture_seq_initialized": False,
    }
    seen = []
    terminal = []
    monkeypatch.setattr(worker.db, "chat_seq_for_msg_id", lambda *_args: 4)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 5)
    monkeypatch.setattr(extraction, "extract", lambda **_kwargs: None)

    async def _empty(**_kwargs):
        return [], None

    monkeypatch.setattr(extraction, "extract", _empty)
    monkeypatch.setattr(jobs_store, "renew_job_lease", lambda *_a, **_k: True)
    protocol = _fake_capture_protocol(state, terminal)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_compaction_tail_after_seq=lambda _uid, after, limit, **kw: (
            seen.append(after)
            or [
                {
                    "id": "m5",
                    "seq": 5,
                    "ts": 100.0,
                    "role": "user",
                    "raw_role": "user",
                    "source": "chat",
                    "capture_eligible": True,
                    "content": "x",
                }
            ]
        ),
        read_capture_state=lambda _uid: dict(state),
        build_memory_envelope=lambda *_args: {},
        **protocol,
    )
    assert asyncio.run(
        worker._run_extraction(
            "job-exact", "u-v2-capture", "capture", deps, object(),
            asyncio.Semaphore(1), claimed_by="owner",
        )
    ) == "completed"
    assert seen == [4]


def test_lost_lease_never_advances_capture_frontier(monkeypatch):
    statuses = []

    async def _empty(**_kwargs):
        return [], None

    monkeypatch.setattr(extraction, "extract", _empty)
    monkeypatch.setattr(worker.db, "chat_max_seq", lambda _uid: 1)
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (object(), {}),
        mint_enclave_token=lambda _uid: "rt",
        read_compaction_tail_after_seq=lambda *_args, **_kwargs: [
            {
                "id": "m1",
                "seq": 1,
                "ts": 1.0,
                "role": "user",
                "raw_role": "user",
                "source": "chat",
                "capture_eligible": True,
                "content": "one",
            }
        ],
        read_capture_state=lambda _uid: {
            "last_captured_until_message_id": "",
            "last_captured_until_ts": 0.0,
        },
        build_memory_envelope=lambda *_args: {},
        get_prepared_capture_batch=lambda **_kwargs: None,
        prepare_capture_batch=lambda **_kwargs: None,
        authorize_capture_provider_call=lambda **_kwargs: {"authorized": True},
        commit_capture_batch=lambda **_kwargs: {"committed": False},
        fail_capture_job=lambda **_kwargs: statuses.append(True) or False,
    )

    result = asyncio.run(
        worker._run_extraction(
            "job-lost",
            "u-v2-capture",
            "capture",
            deps,
            object(),
            asyncio.Semaphore(1),
            claimed_by="old-owner",
        )
    )

    assert result == "failed"
    assert statuses == [True]
