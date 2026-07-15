from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from qa import live_scenario_probe as probe
from qa import request_live_scenario_probe as request
from qa import validate_live_scenario_receipts as receipts
from tools.provider_smoke.client import SmokeError


_TURN_COUNTS = {
    "P0-02": 0,
    "P0-03": 0,
    "P0-04": 0,
    "P0-05": 0,
    "P0-07": 0,
    "P0-08": 1,
    "P0-09": 10,
    "P0-10": 2,
    "P0-11": 1,
}


def _history(*, duplicate: bool = False, reversed_order: bool = False) -> list[dict]:
    rows = [
        {
            "id": "user-1",
            "role": "user",
            "ts": 100.0,
            "reply_message_id": "reply-1",
        },
        {
            "id": "reply-1",
            "role": "assistant",
            "ts": 101.0,
            "body_ct": "ciphertext-1",
            "reply_to_message_id": "user-1",
        },
    ]
    if duplicate:
        rows.append(
            {
                "id": "reply-2",
                "role": "assistant",
                "ts": 102.0,
                "body_ct": "ciphertext-2",
                "reply_to_message_id": "user-1",
            }
        )
    return list(reversed(rows)) if reversed_order else rows


class _SettlingClient:
    def __init__(
        self,
        snapshots: list[list[dict]],
        *,
        correlation_error: bool = False,
    ) -> None:
        self.snapshots = snapshots
        self.correlation_error = correlation_error
        self.history_calls = 0

    def send(self, _session, _prompt):
        return {"user_message": {"id": "user-1", "ts": 100.0}}

    def poll_reply_record(self, *_args, **_kwargs):
        if self.correlation_error:
            raise SmokeError("reply-correlation", "duplicate replies")
        return {
            "message": {"id": "reply-1"},
            "reply": "expected reply",
        }

    def _req(self, *_args, **_kwargs):
        snapshot = self.snapshots[min(self.history_calls, len(self.snapshots) - 1)]
        self.history_calls += 1
        return 200, {"messages": snapshot}


class _RuntimeTargetClient:
    def __init__(self, runtime: dict) -> None:
        self.runtime = runtime

    def runtime_status(self, _session):
        return dict(self.runtime)

    def _req(self, method, path, *, api_key):
        assert method == "GET"
        assert path == "/v1/chat/history?limit=200"
        assert api_key == "key"
        return 200, {"messages": []}

    def enable_hosting(self, _session):
        return "model_api"

    def open_chat_gate(self, _session):
        return {"passing": True}


def _receipt(
    scenario_id: str,
    attempt: int = 1,
    *,
    status: str = "PASS",
) -> dict:
    turn_count = _TURN_COUNTS[scenario_id] if status == "PASS" else 0
    turns = [
        {
            "turn_index": index,
            "request_id": f"request-{scenario_id.lower()}-{attempt}-{index}",
            "turn_id": f"request-{scenario_id.lower()}-{attempt}-{index}",
            "trace_id": f"request-{scenario_id.lower()}-{attempt}-{index}",
            "ack_latency_ms": float(index * 10),
            "reply_latency_ms": float(index * 100),
            "reply_count": 1,
            "content_assertion_passed": (
                None if scenario_id in {"P0-10", "P0-11"} else True
            ),
            "fallback_detected": False,
            "duplicate_detected": False,
            "out_of_order_detected": False,
        }
        for index in range(1, turn_count + 1)
    ]
    private_facts = {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "raw_reply": "private-only" if scenario_id in {"P0-10", "P0-11"} else "",
    }
    ids = [turn["request_id"] for turn in turns]
    return {
        "schema_version": 1,
        "kind": "live_scenario_probe",
        "run_id": "run-123",
        "profile_id": "official-gemini",
        "scenario_id": scenario_id,
        "attempt": attempt,
        "nonce": f"nonce-{scenario_id.lower()}-{attempt}",
        "started_at": f"2026-01-01T00:00:0{attempt}.000000Z",
        "finished_at": f"2026-01-01T00:00:1{attempt}.000000Z",
        "status": status,
        "failure_code": (
            "NONE"
            if status == "PASS"
            else "CHAT_TIMEOUT"
            if status == "AGENT_ERROR"
            else "ASSERTION_FAILED"
        ),
        "assertions": {
            key: status == "PASS"
            for key in receipts.DETERMINISTIC_ASSERTIONS[scenario_id]
        },
        "semantic_assertions": list(receipts.SEMANTIC_ASSERTIONS[scenario_id]),
        "request_ids": (
            ids
            if ids
            else [f"probe-{scenario_id.lower()}-{attempt}"]
            if status == "PASS"
            else []
        ),
        "turn_ids": ids,
        "trace_ids": ids,
        "turns": turns,
        "private_facts_sha256": receipts.canonical_json_sha256(private_facts),
        "raw_content_stored": False,
    }


def _aggregate(*, retry_statuses: tuple[str, str] | None = None) -> dict:
    rows: list[dict] = []
    for scenario_id in request.LIVE_SCENARIO_IDS:
        if scenario_id == "P0-08" and retry_statuses is not None:
            rows.append(_receipt(scenario_id, 1, status=retry_statuses[0]))
            rows.append(_receipt(scenario_id, 2, status=retry_statuses[1]))
        else:
            rows.append(_receipt(scenario_id))
    return {
        "schema_version": 1,
        "kind": "live_scenario_receipt_set",
        "run_id": "run-123",
        "profile_id": "official-gemini",
        "receipts": rows,
    }


def _profile_projection(aggregate: dict) -> dict:
    grouped = {scenario_id: [] for scenario_id in request.LIVE_SCENARIO_IDS}
    for receipt in aggregate["receipts"]:
        grouped[receipt["scenario_id"]].append(receipt)
    scenarios = []
    turns = []
    for scenario_id, rows in grouped.items():
        final = rows[-1]
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "status": final["status"],
                "started_at": rows[0]["started_at"],
                "finished_at": final["finished_at"],
                "attempts": len(rows),
                "attempt_results": [
                    {
                        "attempt": index,
                        "status": row["status"],
                        "failure": None if row["status"] == "PASS" else {},
                    }
                    for index, row in enumerate(rows, start=1)
                ],
                "assertions": {
                    **final["assertions"],
                    **{
                        key: True for key in final["semantic_assertions"]
                    },
                },
                "request_ids": [
                    value for row in rows for value in row["request_ids"]
                ],
                "turn_ids": [value for row in rows for value in row["turn_ids"]],
                "trace_ids": [value for row in rows for value in row["trace_ids"]],
            }
        )
        for row in rows:
            for turn in row["turns"]:
                turns.append(
                    {
                        "scenario_id": scenario_id,
                        **turn,
                    }
                )
    return {"scenarios": scenarios, "turns": turns}


@pytest.mark.parametrize(
    "runtime_requirement", ("deployed_current", "hosted_resident")
)
def test_one_profile_manifest_preserves_runtime_requirement(
    tmp_path: Path, runtime_requirement: str
):
    manifest = tmp_path / "profile.json"
    encoded_key = base64.b64encode(b"k" * 32).decode("ascii")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": probe.LOCKED_BASE_URL,
                "runtime_mode": runtime_requirement,
                "profiles": [
                    {
                        "profile_id": "official-gemini",
                        "provision_status": "ready",
                        "user_id": "synthetic-user",
                        "api_key": "synthetic-key",
                        "secret_key_b64": encoded_key,
                        "public_key_b64": encoded_key,
                    }
                ],
            }
        )
    )
    manifest.chmod(0o600)

    profile, session = probe.load_profile(manifest, "official-gemini")

    assert profile["_qualification_runtime_requirement"] == runtime_requirement
    assert session.user_id == "synthetic-user"


def test_one_profile_manifest_rejects_unknown_runtime_requirement(tmp_path: Path):
    manifest = tmp_path / "profile.json"
    encoded_key = base64.b64encode(b"k" * 32).decode("ascii")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": probe.LOCKED_BASE_URL,
                "runtime_mode": "unlocked-runtime",
                "profiles": [
                    {
                        "profile_id": "official-gemini",
                        "provision_status": "ready",
                        "user_id": "synthetic-user",
                        "api_key": "synthetic-key",
                        "secret_key_b64": encoded_key,
                        "public_key_b64": encoded_key,
                    }
                ],
            }
        )
    )
    manifest.chmod(0o600)

    with pytest.raises(probe.LiveScenarioProbeError, match="not ready"):
        probe.load_profile(manifest, "official-gemini")


def test_request_marker_is_exact_one_shot_and_profile_bound(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    marker = request.request_path(work, "P0-08", 1)
    request.write_request_marker(
        marker,
        run_id="run-123",
        profile_id="official-gemini",
        scenario_id="P0-08",
        attempt=1,
    )
    assert request.load_request_marker(
        marker,
        run_id="run-123",
        profile_id="official-gemini",
        scenario_id="P0-08",
        attempt=1,
    )["scenario_id"] == "P0-08"
    with pytest.raises(request.LiveProbeRequestError):
        request.write_request_marker(
            marker,
            run_id="run-123",
            profile_id="official-gemini",
            scenario_id="P0-08",
            attempt=1,
        )
    with pytest.raises(request.LiveProbeRequestError):
        request.load_request_marker(
            marker,
            run_id="another-run",
            profile_id="official-gemini",
            scenario_id="P0-08",
            attempt=1,
        )


def test_request_marker_rejects_non_retryable_second_attempt(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    with pytest.raises(
        request.LiveProbeRequestError,
        match="identity is invalid",
    ):
        request.write_request_marker(
            request.request_path(work, "P0-02", 2),
            run_id="run-123",
            profile_id="official-gemini",
            scenario_id="P0-02",
            attempt=2,
        )


def test_request_marker_rejects_duplicate_json_keys(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    marker = request.request_path(work, "P0-08", 1)
    marker.write_text(
        '{"schema_version":1,"run_id":"run-123","run_id":"run-123",'
        '"profile_id":"official-gemini","scenario_id":"P0-08","attempt":1}\n',
        encoding="utf-8",
    )
    marker.chmod(0o600)
    with pytest.raises(
        request.LiveProbeRequestError,
        match="duplicate keys",
    ):
        request.load_request_marker(
            marker,
            run_id="run-123",
            profile_id="official-gemini",
            scenario_id="P0-08",
            attempt=1,
        )


@pytest.mark.parametrize(
    ("stage", "detail", "expected"),
    (
        ("chat", "correlated reply timed out", ("AGENT_ERROR", "CHAT_TIMEOUT")),
        ("chat", "hosted acknowledgement is incomplete", ("AGENT_ERROR", "MISSING_REPLY")),
        ("history", "status=503", ("AGENT_ERROR", "MISSING_REPLY")),
        ("not-hosted", "expected 202", ("PRODUCT_FAIL", "ASSERTION_FAILED")),
        ("trace", "read status=503", ("BLOCKED_EVIDENCE", "LIVE_PROBE_ERROR")),
    ),
)
def test_live_probe_classifies_only_missing_chat_as_retryable(
    stage: str, detail: str, expected: tuple[str, str]
):
    assert probe._classify_smoke_error("P0-08", SmokeError(stage, detail)) == expected


def test_chat_turn_settle_repoll_catches_late_duplicate():
    client = _SettlingClient([_history(), _history(duplicate=True)])
    turn, private = probe._chat_turn(
        client,
        probe.Session("user", "key", b"s" * 32, b"p" * 32),
        turn_index=1,
        prompt="test",
        content_check=lambda reply: reply == "expected reply",
        settle_seconds=0.002,
        settle_interval_seconds=0.001,
    )

    assert client.history_calls >= 2
    assert turn["reply_count"] == 2
    assert turn["duplicate_detected"] is True
    assert turn["out_of_order_detected"] is False
    assert private["settled_reply_message_ids"] == ["reply-1", "reply-2"]


def test_chat_turn_records_immediate_correlation_duplicate_as_product_evidence():
    client = _SettlingClient(
        [_history(duplicate=True)], correlation_error=True
    )
    turn, private = probe._chat_turn(
        client,
        probe.Session("user", "key", b"s" * 32, b"p" * 32),
        turn_index=1,
        prompt="test",
        content_check=lambda _reply: True,
        settle_seconds=0,
        settle_interval_seconds=0.001,
    )

    assert turn["reply_count"] == 2
    assert turn["duplicate_detected"] is True
    assert turn["content_assertion_passed"] is False
    assert private["correlation_error_stage"] == "reply-correlation"


def test_settled_turn_summary_flags_out_of_order_history():
    summary = probe._settled_turn_summary(
        _history(reversed_order=True),
        user_message_id="user-1",
        user_message_ts=100.0,
        expected_reply_id="reply-1",
    )

    assert summary["reply_count"] == 1
    assert summary["duplicate_detected"] is False
    assert summary["out_of_order_detected"] is True


@pytest.mark.parametrize(
    ("requirement", "runtime", "expected_match"),
    (
        (
            "hosted_resident",
            {
                "configured": True,
                "runtime_mode": "hosted_resident",
                "runtime_version": 2,
            },
            True,
        ),
        (
            "hosted_resident",
            {
                "configured": True,
                "runtime_mode": "hosted_resident",
                "runtime_version": 1,
            },
            False,
        ),
        (
            "hosted_resident",
            {
                "configured": True,
                "runtime_mode": "legacy_container",
                "runtime_version": 2,
            },
            False,
        ),
        (
            "deployed_current",
            {
                "configured": True,
                "runtime_mode": "legacy_container",
                "runtime_version": 1,
            },
            True,
        ),
    ),
)
def test_p0_05_runtime_readback_is_target_aware(requirement, runtime, expected_match):
    assertions, turns, observations = probe._run_actions(
        "P0-05",
        nonce="runtime-target",
        profile={"_qualification_runtime_requirement": requirement},
        session=probe.Session("user", "key", b"s" * 32, b"p" * 32),
        client=_RuntimeTargetClient(runtime),
    )

    assert assertions == {
        "runtime_status_readback_succeeds": True,
        "runtime_configured": True,
        "runtime_metadata_recorded": expected_match,
    }
    assert turns == []
    assert observations == {
        "runtime_mode": runtime["runtime_mode"],
        "runtime_version": runtime["runtime_version"],
    }


@pytest.mark.parametrize(
    ("requirement", "runtime", "expected_match"),
    (
        (
            "hosted_resident",
            {
                "configured": True,
                "runtime_mode": "hosted_resident",
                "runtime_version": 2,
            },
            True,
        ),
        (
            "hosted_resident",
            {
                "configured": True,
                "runtime_mode": "legacy_container",
                "runtime_version": 2,
            },
            False,
        ),
        (
            "deployed_current",
            {
                "configured": True,
                "runtime_mode": "legacy_container",
                "runtime_version": 1,
            },
            True,
        ),
    ),
)
def test_p0_07_hosted_loop_rechecks_selected_runtime_target(
    requirement, runtime, expected_match
):
    assertions, turns, observations = probe._run_actions(
        "P0-07",
        nonce="runtime-target",
        profile={"_qualification_runtime_requirement": requirement},
        session=probe.Session("user", "key", b"s" * 32, b"p" * 32),
        client=_RuntimeTargetClient(runtime),
    )

    assert assertions == {
        "driver_enabled": True,
        "chat_loop_verified": True,
        "runtime_status_readback_succeeds": expected_match,
        "no_orphan_turn": True,
    }
    assert turns == []
    assert observations == {
        "driver": "model_api",
        "verify_passing": True,
        "runtime_mode": runtime["runtime_mode"],
        "runtime_version": runtime["runtime_version"],
    }


def test_bounded_transient_retry_can_bind_final_pass():
    aggregate = receipts.validate_aggregate_object(
        _aggregate(retry_statuses=("AGENT_ERROR", "PASS")),
        run_id="run-123",
        profile_id="official-gemini",
    )
    result = _profile_projection(aggregate)
    receipts.validate_result_binding(result, aggregate)
    retried = next(
        row for row in result["scenarios"] if row["scenario_id"] == "P0-08"
    )
    assert [row["status"] for row in retried["attempt_results"]] == [
        "AGENT_ERROR",
        "PASS",
    ]
    assert retried["status"] == "PASS"


@pytest.mark.parametrize(
    "statuses",
    (("PASS", "PASS"), ("PRODUCT_FAIL", "PASS"), ("BLOCKED_EVIDENCE", "PASS")),
)
def test_retry_rejects_non_transient_first_observation(statuses):
    with pytest.raises(
        receipts.LiveScenarioReceiptError,
        match="bounded transient retry",
    ):
        receipts.validate_aggregate_object(
            _aggregate(retry_statuses=statuses),
            run_id="run-123",
            profile_id="official-gemini",
        )


def test_retry_replay_and_cross_run_replay_are_rejected():
    aggregate = _aggregate(retry_statuses=("AGENT_ERROR", "PASS"))
    p0_08_second = next(
        row
        for row in aggregate["receipts"]
        if row["scenario_id"] == "P0-08" and row["attempt"] == 2
    )
    insertion = aggregate["receipts"].index(p0_08_second) + 1
    aggregate["receipts"].insert(insertion, dict(p0_08_second))
    with pytest.raises(receipts.LiveScenarioReceiptError):
        receipts.validate_aggregate_object(
            aggregate, run_id="run-123", profile_id="official-gemini"
        )

    with pytest.raises(receipts.LiveScenarioReceiptError):
        receipts.validate_aggregate_object(
            _aggregate(), run_id="other-run", profile_id="official-gemini"
        )


def test_result_cannot_be_greener_than_parent_receipt():
    aggregate = _aggregate()
    failed = next(
        row for row in aggregate["receipts"] if row["scenario_id"] == "P0-08"
    )
    failed["status"] = "PRODUCT_FAIL"
    failed["failure_code"] = "ASSERTION_FAILED"
    failed["assertions"]["nonce_echo_confirmed"] = False
    aggregate = receipts.validate_aggregate_object(
        aggregate, run_id="run-123", profile_id="official-gemini"
    )
    result = _profile_projection(aggregate)
    scenario = next(
        row for row in result["scenarios"] if row["scenario_id"] == "P0-08"
    )
    scenario["status"] = "PASS"
    scenario["attempt_results"][0]["status"] = "PASS"
    scenario["assertions"]["nonce_echo_confirmed"] = True
    with pytest.raises(receipts.LiveScenarioReceiptError):
        receipts.validate_result_binding(result, aggregate)


def test_authoritative_receipt_never_contains_private_semantic_text():
    aggregate = _aggregate()
    serialized = json.dumps(aggregate, sort_keys=True)
    assert "private-only" not in serialized
    assert all(row["raw_content_stored"] is False for row in aggregate["receipts"])
