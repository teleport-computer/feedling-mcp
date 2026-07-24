from __future__ import annotations

import base64
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import trajectory
from model_api_runtime.v2 import trajectory_inspect


def _encoded(kind: str, payload: dict) -> bytes:
    value, _truncated, _size = trajectory.encode_payload(kind, payload)
    return value


def _deps(*, events: list[dict] | None = None, source: dict | None = None):
    calls: list[tuple] = []
    stored = events if events is not None else [{
        "event_index": 0,
        "payload_envelope": {"owner_user_id": "u1", "body_ct": "cipher"},
        "truncated": False,
    }]

    def audit(**kwargs):
        calls.append(("audit", kwargs["phase"], kwargs["result_code"]))

    def authorize(**_kwargs):
        calls.append(("audit", "succeeded", "ok"))
        return True

    def decrypt(user_id, envelope, token):
        calls.append(("decrypt", user_id, token))
        return _encoded("provider_request", {"secret": "visible only to operator"})

    deps = trajectory_inspect.InspectDeps(
        source_job=lambda job_id, user_id: (
            {"lane": "chat", "status": "failed"}
            if source is None
            else source
        ),
        capture_state=lambda job_id, user_id: {
            "next_event_index": len(stored),
            "capture_status": "complete",
        },
        list_events=lambda job_id, user_id, *, after_index, limit: [
            row for row in stored if int(row["event_index"]) > after_index
        ][:limit],
        append_audit=audit,
        authorize_success=authorize,
        mint_token=lambda user_id: "runtime-token",
        decrypt=decrypt,
    )
    return deps, calls


def test_inspection_audits_before_decrypt_and_before_plaintext_return(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "1")
    deps, calls = _deps()

    result = trajectory_inspect.inspect_trajectory(
        user_id="u1",
        job_id=7,
        operator_id="alice@example.com",
        reason_code="incident",
        case_ref="INC-123",
        deps=deps,
    )

    assert [call[0:2] for call in calls] == [
        ("audit", "requested"),
        ("decrypt", "u1"),
        ("audit", "succeeded"),
    ]
    assert result["events"][0]["payload"]["secret"] == "visible only to operator"


def test_disabled_inspector_never_audits_or_decrypts(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", raising=False)
    deps, calls = _deps()
    with pytest.raises(
        trajectory_inspect.TrajectoryInspectError,
        match="trajectory_inspection_disabled",
    ):
        trajectory_inspect.inspect_trajectory(
            user_id="u1",
            job_id=7,
            operator_id="alice@example.com",
            reason_code="incident",
            case_ref="INC-123",
            deps=deps,
        )
    assert calls == []


def test_source_mismatch_is_audited_and_never_decrypted(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "1")
    deps, calls = _deps()
    deps = trajectory_inspect.InspectDeps(
        source_job=lambda *_args: None,
        capture_state=deps.capture_state,
        list_events=deps.list_events,
        append_audit=deps.append_audit,
        authorize_success=deps.authorize_success,
        mint_token=deps.mint_token,
        decrypt=deps.decrypt,
    )
    with pytest.raises(
        trajectory_inspect.TrajectoryInspectError,
        match="trajectory_source_not_found",
    ):
        trajectory_inspect.inspect_trajectory(
            user_id="u1",
            job_id=99,
            operator_id="alice@example.com",
            reason_code="support",
            case_ref="SUP-9",
            deps=deps,
        )
    assert calls == [
        ("audit", "requested", "pending"),
        ("audit", "failed", "trajectory_source_not_found"),
    ]


def test_audit_failure_blocks_decryption(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "1")
    deps, calls = _deps()

    def unavailable(**_kwargs):
        raise RuntimeError("database unavailable")

    deps = trajectory_inspect.InspectDeps(
        source_job=deps.source_job,
        capture_state=deps.capture_state,
        list_events=deps.list_events,
        append_audit=unavailable,
        authorize_success=deps.authorize_success,
        mint_token=deps.mint_token,
        decrypt=deps.decrypt,
    )
    with pytest.raises(
        trajectory_inspect.TrajectoryInspectError,
        match="trajectory_access_audit_unavailable",
    ):
        trajectory_inspect.inspect_trajectory(
            user_id="u1",
            job_id=7,
            operator_id="alice@example.com",
            reason_code="security",
            case_ref="SEC-7",
            deps=deps,
        )
    assert calls == []


def test_chunked_trajectory_is_fully_paged_and_reassembled(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "1")
    payload = {"secret": "x" * 100_000}
    parts, _size = trajectory.encode_payload_parts(
        "provider_request", payload, max_json_bytes=64 * 1024
    )
    rows = [
        {
            "event_index": index,
            "payload_envelope": {
                "owner_user_id": "u1",
                "body_ct": base64.b64encode(part).decode(),
            },
            "truncated": False,
        }
        for index, part in enumerate(parts)
    ]
    deps, _calls = _deps(events=rows)
    deps = trajectory_inspect.InspectDeps(
        source_job=deps.source_job,
        capture_state=deps.capture_state,
        list_events=deps.list_events,
        append_audit=deps.append_audit,
        authorize_success=deps.authorize_success,
        mint_token=deps.mint_token,
        decrypt=lambda _uid, envelope, _token: base64.b64decode(envelope["body_ct"]),
    )
    result = trajectory_inspect.inspect_trajectory(
        user_id="u1",
        job_id=7,
        operator_id="alice@example.com",
        reason_code="debug",
        case_ref="DBG-7",
        deps=deps,
    )
    assert result["events"][0]["payload"] == payload


def test_frontier_change_after_decrypt_blocks_plaintext_return(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "1")
    deps, calls = _deps()
    deps = trajectory_inspect.InspectDeps(
        source_job=deps.source_job,
        capture_state=deps.capture_state,
        list_events=deps.list_events,
        append_audit=deps.append_audit,
        authorize_success=lambda **_kwargs: False,
        mint_token=deps.mint_token,
        decrypt=deps.decrypt,
    )

    with pytest.raises(
        trajectory_inspect.TrajectoryInspectError,
        match="trajectory_source_changed",
    ):
        trajectory_inspect.inspect_trajectory(
            user_id="u1",
            job_id=7,
            operator_id="alice@example.com",
            reason_code="incident",
            case_ref="INC-456",
            deps=deps,
        )

    assert ("decrypt", "u1", "runtime-token") in calls
    assert calls[-1] == ("audit", "failed", "trajectory_source_changed")


def test_decoded_byte_budget_fails_closed(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "1")
    deps, calls = _deps()

    with pytest.raises(
        trajectory_inspect.TrajectoryInspectError,
        match="trajectory_decoded_byte_limit_exceeded",
    ):
        trajectory_inspect.inspect_trajectory(
            user_id="u1",
            job_id=7,
            operator_id="alice@example.com",
            reason_code="security",
            case_ref="SEC-999",
            deps=deps,
            max_decoded_bytes=64,
        )

    assert ("decrypt", "u1", "runtime-token") in calls
    assert calls[-1] == (
        "audit",
        "failed",
        "trajectory_decoded_byte_limit_exceeded",
    )
