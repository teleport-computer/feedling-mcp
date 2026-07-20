from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qa.regression.feedling_rotation import (
    RotationEvidenceError,
    prove_codex_runtime_session_rotation,
)


def _trace_event(request_id: str, thread_id: str) -> dict:
    return {
        "type": "agent.model.call.done",
        "status": "ok",
        "trace_id": request_id,
        "detail": {"driver": "codex"},
        "content_excerpt": {
            "reply_head": json.dumps(
                {"type": "thread.started", "thread_id": thread_id}
            )
            + "\n"
            + json.dumps({"type": "item.completed"}),
        },
    }


class _Client:
    def __init__(
        self,
        *,
        verbose: bool = True,
        capture_status: str = "completed",
        capture_mutations: object = 1,
        capture_superseded: object = 0,
        force_enqueued: bool = True,
        force_reason: str = "enqueued",
        force_job_status: str = "pending",
        force_job_present: bool | None = None,
        force_window_response_id: str = "assistant-learn",
        force_window_ts: object = 101.0,
        force_last_seen_response_id: str = "assistant-learn",
        force_last_seen_ts: object = 101.0,
        persisted_capture_key: str = "capture-key-private",
        duplicate_capture_job: bool = False,
        same_thread: bool = False,
        duplicate_before_trace: bool = False,
        fail_second_clear: bool = False,
    ) -> None:
        self.verbose = verbose
        self.capture_status = capture_status
        self.capture_mutations = capture_mutations
        self.capture_superseded = capture_superseded
        self.force_enqueued = force_enqueued
        self.force_reason = force_reason
        self.force_job_status = force_job_status
        self.force_job_present = (
            force_enqueued if force_job_present is None else force_job_present
        )
        self.force_window_response_id = force_window_response_id
        self.force_window_ts = force_window_ts
        self.force_last_seen_response_id = force_last_seen_response_id
        self.force_last_seen_ts = force_last_seen_ts
        self.persisted_capture_key = persisted_capture_key
        self.duplicate_capture_job = duplicate_capture_job
        self.same_thread = same_thread
        self.duplicate_before_trace = duplicate_before_trace
        self.fail_second_clear = fail_second_clear
        self.probe_sent = False
        self.clear_count = 0
        self.debug_reads = 0

    def _req(self, method, path, **_kwargs):
        if (method, path) == ("POST", "/v1/capture/force"):
            job = (
                {
                    "job_id": "capture-private",
                    "job_kind": "memory_capture",
                    "status": self.force_job_status,
                    "capture_key": "capture-key-private",
                }
                if self.force_job_present
                else None
            )
            return 200, {
                "enqueued": self.force_enqueued,
                "reason": self.force_reason,
                "state": {
                    "last_seen_message_id": self.force_last_seen_response_id,
                    "last_seen_ts": self.force_last_seen_ts,
                    "pending_capture_key": "capture-key-private",
                },
                "job": job,
            }
        if (method, path) == ("GET", "/v1/proactive/debug"):
            self.debug_reads += 1
            status = self.capture_status
            if not self.force_enqueued and self.debug_reads == 1:
                status = self.force_job_status
            job = {
                "job_id": "capture-private",
                "job_kind": "memory_capture",
                "status": status,
                "capture_key": self.persisted_capture_key,
                (
                    "window"
                    if status in {"pending", "claimed", "realizing"}
                    else "capture_window"
                ): {
                    "until_message_id": self.force_window_response_id,
                    "until_ts": self.force_window_ts,
                },
                "cards_added": self.capture_mutations,
                "cards_superseded": self.capture_superseded,
            }
            return 200, {
                "jobs": [job, dict(job)] if self.duplicate_capture_job else [job]
            }
        if (method, path) == ("DELETE", "/v1/chat/history"):
            self.clear_count += 1
            if self.fail_second_clear and self.clear_count == 2:
                return 503, {"error": "secret-response-body"}
            return 200, {"cleared": True, "deleted": 2}
        raise AssertionError((method, path))

    def read_trace(self, _credentials, **_kwargs):
        events = [_trace_event("request-learn", "thread-before")]
        if self.duplicate_before_trace:
            events.append(_trace_event("request-learn", "thread-other"))
        if self.probe_sent:
            events.insert(
                0,
                _trace_event(
                    "request-probe",
                    "thread-before" if self.same_thread else "thread-after",
                ),
            )
        return {"verbose": self.verbose, "events": events}

    def send(self, _credentials, _message, **_kwargs):
        self.probe_sent = True
        return {"user_message": {"id": "request-probe", "ts": 200.0}}

    def poll_reply_record(self, *_args, **_kwargs):
        return {"message": {"id": "assistant-probe"}, "reply": "ack"}


def _credentials():
    return SimpleNamespace(api_key="private-api-key")


def _prove(client: _Client):
    return prove_codex_runtime_session_rotation(
        client,
        _credentials(),
        {
            "prior_request_id": "request-learn",
            "prior_response_id": "assistant-learn",
        },
        capture_timeout_seconds=0,
        trace_timeout_seconds=0,
        reply_timeout_seconds=1,
        poll_interval_seconds=0,
    )


def test_rotation_proof_commits_memory_and_cleans_the_boundary_probe():
    client = _Client()

    evidence = _prove(client)

    assert evidence["rotated"] is True
    assert evidence["before_runtime_session_id"] == "thread-before"
    assert evidence["after_runtime_session_id"] == "thread-after"
    assert evidence["protected_debug_identifiers"] == {
        "capture_job_ids": ["capture-private"],
        "request_ids": ["request-probe"],
        "response_ids": ["assistant-probe"],
        "trace_ids": ["request-learn", "request-probe"],
        "runtime_session_ids": ["thread-before", "thread-after"],
    }
    assert len(evidence["evidence_id"]) == 64
    assert client.clear_count == 2


def test_rotation_proof_accepts_exact_capture_already_pending_race():
    client = _Client(
        force_enqueued=False,
        force_reason="capture_already_pending",
    )

    evidence = _prove(client)

    assert evidence["rotated"] is True
    assert client.debug_reads == 2


def test_rotation_proof_accepts_exact_returned_capture_already_pending_job():
    client = _Client(
        force_enqueued=False,
        force_reason="capture_already_pending",
        force_job_present=True,
    )

    evidence = _prove(client)

    assert evidence["rotated"] is True


def test_rotation_proof_accepts_job_null_race_that_completed_before_lookup():
    client = _Client(
        force_enqueued=False,
        force_reason="capture_already_pending",
        force_job_status="completed",
    )

    evidence = _prove(client)

    assert evidence["rotated"] is True


@pytest.mark.parametrize(
    "client",
    [
        _Client(
            force_enqueued=False,
            force_reason="duplicate_capture_key",
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            force_window_response_id="assistant-other",
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            force_window_ts=102.0,
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            force_window_ts="101.0",
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            force_last_seen_response_id="assistant-other",
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            force_last_seen_ts=True,
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            persisted_capture_key="capture-key-other",
        ),
        _Client(
            force_enqueued=False,
            force_reason="capture_already_pending",
            duplicate_capture_job=True,
        ),
    ],
)
def test_rotation_proof_rejects_unbound_or_ambiguous_capture_race(client):
    with pytest.raises(RotationEvidenceError) as raised:
        _prove(client)

    assert raised.value.code == "ROTATION_CAPTURE_FORCE_UNPROVEN"


@pytest.mark.parametrize(
    ("client", "code"),
    [
        (_Client(verbose=False), "ROTATION_VERBOSE_TRACE_REQUIRED"),
        (
            _Client(capture_status="completed", capture_mutations=0),
            "ROTATION_MEMORY_COMMIT_UNPROVEN",
        ),
        (_Client(capture_status="failed"), "ROTATION_MEMORY_COMMIT_UNPROVEN"),
        (_Client(duplicate_before_trace=True), "ROTATION_TRACE_AMBIGUOUS"),
        (_Client(same_thread=True), "ROTATION_SESSION_NOT_DISTINCT"),
    ],
)
def test_rotation_proof_fails_closed_when_required_evidence_is_missing(client, code):
    with pytest.raises(RotationEvidenceError) as raised:
        _prove(client)

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("cards_added", "cards_superseded"),
    [
        (True, 0),
        ("1", 0),
        (1.0, 0),
        (-1, 2),
        (1, False),
        (1, "0"),
        (1, 0.0),
        (1, -1),
        (None, 1),
        (1, None),
    ],
)
def test_rotation_proof_rejects_malformed_capture_mutation_counts(
    cards_added, cards_superseded
):
    client = _Client(
        capture_mutations=cards_added,
        capture_superseded=cards_superseded,
    )

    with pytest.raises(RotationEvidenceError) as raised:
        _prove(client)

    assert raised.value.code == "ROTATION_MEMORY_COMMIT_UNPROVEN"


def test_rotation_proof_prioritizes_removing_the_probe_on_failure():
    client = _Client(fail_second_clear=True)

    with pytest.raises(RotationEvidenceError) as raised:
        _prove(client)

    assert raised.value.code == "ROTATION_PROBE_CLEANUP_FAILED"
    assert client.clear_count == 2


def test_rotation_errors_do_not_expose_private_identifiers_or_bodies():
    client = _Client(capture_status="failed")

    with pytest.raises(RotationEvidenceError) as raised:
        _prove(client)

    rendered = str(raised.value)
    for private_value in (
        "private-api-key",
        "request-learn",
        "assistant-learn",
        "capture-private",
        "secret-response-body",
    ):
        assert private_value not in rendered
    assert raised.value.protected_debug_identifiers == {
        "capture_job_ids": ["capture-private"],
        "request_ids": ["request-learn"],
        "response_ids": ["assistant-learn"],
        "trace_ids": ["request-learn"],
        "runtime_session_ids": ["thread-before"],
    }


def test_rotation_requires_an_exact_preceding_turn():
    with pytest.raises(RotationEvidenceError) as raised:
        prove_codex_runtime_session_rotation(
            _Client(),
            _credentials(),
            {},
            capture_timeout_seconds=0,
            trace_timeout_seconds=0,
            poll_interval_seconds=0,
        )

    assert raised.value.code == "ROTATION_PRIOR_TURN_MISSING"
