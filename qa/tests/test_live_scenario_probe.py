from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa import live_scenario_probe as probe
from qa import run_codex_profile_workers as launcher
from tools.provider_smoke.client import Session


def _prior_turns() -> list[dict[str, object]]:
    return [
        {
            "scenario_id": "P0-12" if index == 15 else "P0-09",
            "turn_index": index,
            "request_id": f"request-{index}",
            "turn_id": f"turn-{index}",
            "trace_id": f"trace-{index}",
            "ack_latency_ms": 10.0,
            "reply_latency_ms": 100.0,
            "reply_count": 1,
            "content_assertion_passed": True,
            "fallback_detected": False,
            "duplicate_detected": False,
            "out_of_order_detected": False,
        }
        for index in range(1, 16)
    ]


def _trace_events(*, include_delivery: bool) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for index in range(1, 16):
        trace_id = f"trace-{index}"
        events.extend(
            [
                {"type": "chat.message", "trace_id": trace_id, "ts": 1.0},
                {"type": "route.decided", "trace_id": trace_id, "ts": 1.1},
                {
                    "type": "agent.model.call.start",
                    "trace_id": trace_id,
                    "ts": 1.2,
                },
                {
                    "type": "agent.model.call.done",
                    "trace_id": trace_id,
                    "ts": 1.7,
                    "dur_ms": 500.0,
                },
                {"type": "agent.reply", "trace_id": trace_id, "ts": 1.8},
                {"type": "chat.response", "trace_id": trace_id, "ts": 1.9},
            ]
        )
        if include_delivery:
            events.append(
                {
                    "type": "chat.delivery.confirmed",
                    "trace_id": trace_id,
                    "ts": 2.0,
                }
            )
    return events


class _TraceOnlyClient:
    def __init__(self, *, include_delivery: bool):
        self.include_delivery = include_delivery

    def read_trace(self, _session, *, limit):
        assert limit == 500
        return {"events": _trace_events(include_delivery=self.include_delivery)}

    def _req(self, *_args, **_kwargs):
        raise AssertionError("diagnostic trace projection must not infer delivery from history")


class _CleanupClient:
    def __init__(self):
        self.calls: list[str] = []

    def read_trace(self, _session, *, limit):
        self.calls.append("trace")
        assert limit == 500
        return {"events": []}

    def reset_account(self, _session):
        self.calls.append("reset")
        return {"deleted": True}


def test_trace_cleanup_leaves_delivery_unknown_without_explicit_event():
    assertions, turns, _observations, projection = probe._run_trace_cleanup(
        profile={"trace_enabled": True},
        session=Session("user", "key", b"s" * 32, b"p" * 32),
        client=_TraceOnlyClient(include_delivery=False),
        prior_turns=_prior_turns(),
        perform_cleanup=False,
    )

    assert len(turns) == 15
    assert all(turn["stage_latency_ms"]["delivery"] is None for turn in turns)
    assert projection["latency"]["missing_stages"] == ["delivery"]
    assert assertions == {
        "trace_stages_complete": False,
        "trace_correlation_confirmed": True,
        "latency_attributed": False,
        "cleanup_confirmed": False,
    }


def test_trace_cleanup_accepts_only_explicit_delivery_confirmation():
    assertions, turns, _observations, projection = probe._run_trace_cleanup(
        profile={"trace_enabled": True},
        session=Session("user", "key", b"s" * 32, b"p" * 32),
        client=_TraceOnlyClient(include_delivery=True),
        prior_turns=_prior_turns(),
        perform_cleanup=False,
    )

    assert all(turn["stage_latency_ms"]["delivery"] == 100.0 for turn in turns)
    assert projection["latency"]["missing_stages"] == []
    assert assertions["trace_stages_complete"] is True
    assert assertions["latency_attributed"] is True
    assert assertions["cleanup_confirmed"] is False


def test_trace_cleanup_runs_before_malformed_projection(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _CleanupClient()

    def delete_provider(_client, _session):
        client.calls.append("provider-delete")
        return True

    def credential_rejected(_client, _api_key):
        client.calls.append("credential-rejected")
        return True

    monkeypatch.setattr(probe, "_delete_provider_config", delete_provider)
    monkeypatch.setattr(probe, "_old_credential_rejected", credential_rejected)

    assertions, turns, observations, projection = probe._run_trace_cleanup(
        profile={"trace_enabled": True},
        session=Session("user", "key", b"s" * 32, b"p" * 32),
        client=client,
        # This simulates an unexpected projection-contract bug. Cleanup must
        # still finish and be preserved as authoritative evidence.
        prior_turns=[{}],
        perform_cleanup=True,
    )

    assert client.calls == [
        "trace",
        "provider-delete",
        "reset",
        "credential-rejected",
    ]
    assert turns == []
    assert observations["trace_error"] is True
    assert projection["cleanup"] == {
        "attempted": True,
        "provider_config_deleted": True,
        "account_reset": True,
        "old_credential_rejected": True,
        "status": "PASS",
    }
    assert assertions == {
        "trace_stages_complete": False,
        "trace_correlation_confirmed": False,
        "latency_attributed": False,
        "cleanup_confirmed": True,
    }


def test_cleanup_attempts_reset_when_provider_deletion_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _CleanupClient()

    def delete_provider(_client, _session):
        client.calls.append("provider-delete")
        raise RuntimeError("simulated delete bug")

    def credential_rejected(_client, _api_key):
        client.calls.append("credential-rejected")
        return True

    monkeypatch.setattr(probe, "_delete_provider_config", delete_provider)
    monkeypatch.setattr(probe, "_old_credential_rejected", credential_rejected)

    cleanup, ok = probe._perform_account_cleanup(
        client,
        Session("user", "key", b"s" * 32, b"p" * 32),
        perform_cleanup=True,
    )

    assert client.calls == [
        "provider-delete",
        "reset",
        "credential-rejected",
    ]
    assert ok is False
    assert cleanup["provider_config_deleted"] is False
    assert cleanup["account_reset"] is True
    assert cleanup["old_credential_rejected"] is True
    assert cleanup["status"] == "PRODUCT_FAIL"


def test_partial_trace_context_is_valid_and_does_not_invent_missing_turns(
    tmp_path: Path,
):
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "profile_id": "profile-1",
                "turns": [
                    {
                        "scenario_id": "P0-08",
                        "turn_index": 1,
                        "request_id": "request-1",
                        "turn_id": "turn-1",
                        "trace_id": "trace-1",
                        "ack_latency_ms": 10.0,
                        "reply_latency_ms": None,
                        "reply_count": 0,
                        "content_assertion_passed": False,
                        "fallback_detected": False,
                        "duplicate_detected": False,
                        "out_of_order_detected": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    context.chmod(0o600)

    rows = probe._load_prior_turn_context(
        context, run_id="run-1", profile_id="profile-1"
    )

    assert len(rows) == 1
    assert rows[0]["trace_id"] == "trace-1"
    assert rows[0]["reply_latency_ms"] is None


def test_trace_context_omits_unstarted_cot_turn_without_blocking_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "output"
    output.mkdir()
    spec = SimpleNamespace(
        profile_id="profile-1",
        cot_receipt_path=output / "cot.json",
        environment={"QA_RUN_ID": "run-1"},
    )
    monkeypatch.setattr(
        launcher,
        "validate_cot_receipt",
        lambda _path, _profile: (
            {
                "request_id": "",
                "turn_id": "",
                "trace_id": "",
                "ack_latency_ms": None,
                "reply_latency_ms": None,
                "chat_response_match_count": 0,
                "chat_response_count": 0,
                "final_answer_correct": False,
            },
            "a" * 64,
        ),
    )
    prior = [
        {
            "scenario_id": "P0-08",
            "turns": [
                {
                    "turn_index": 1,
                    "request_id": "request-1",
                    "turn_id": "turn-1",
                    "trace_id": "trace-1",
                    "ack_latency_ms": 10.0,
                    "reply_latency_ms": None,
                    "reply_count": 0,
                    "content_assertion_passed": False,
                    "fallback_detected": False,
                    "duplicate_detected": False,
                    "out_of_order_detected": False,
                }
            ],
        }
    ]

    context = launcher._trace_cleanup_turn_context(spec, prior)

    assert [row["scenario_id"] for row in context["turns"]] == ["P0-08"]
    assert all(row["scenario_id"] != "P0-12" for row in context["turns"])


def _persona_spec(tmp_path: Path):
    output = tmp_path / "output"
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    for path in (output, work, artifacts):
        path.mkdir(mode=0o700)
    return SimpleNamespace(
        output_dir=output,
        work=work,
        environment={
            "QA_SOURCE_ROOT": str(Path(__file__).resolve().parents[2]),
            "QA_ARTIFACT_DIR": str(artifacts),
        },
    )


def _capture_receipt() -> dict[str, object]:
    return {
        "scenario_id": "P0-06",
        "status": "PASS",
        "request_ids": ["probe-p0-06"],
        "result_projection": {
            "kind": "persona_capture",
            "evidence_sha256": "a" * 64,
            "job_id": "genesis-job",
            "archive_upload_count": 4,
            "archive_receipts_verified": True,
            "genesis_upload_metadata_verified": True,
        },
    }


def test_parent_persona_finalizer_uses_authoritative_copy_and_cleans_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = _persona_spec(tmp_path)
    authoritative = launcher._persona_authoritative_evidence_path(spec)
    review_copy = launcher._persona_worker_evidence_path(spec)
    judgment = launcher._persona_judgment_path(spec)
    for path, payload in (
        (authoritative, {"authoritative": True}),
        (review_copy, {"tampered_review_copy": True}),
        (judgment, {"evidence_sha256": "a" * 64}),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)

    def finalize(**kwargs):
        assert kwargs["private_evidence_path"] == str(authoritative)
        assert kwargs["private_evidence_path"] != str(review_copy)
        return {
            "ok": True,
            "job_id": "genesis-job",
            "evidence": {
                "sha256": "a" * 64,
                "semantic_judgment_bound": True,
                "private_evidence_deleted": True,
            },
            "checks": {
                "archive_receipts_verified": True,
                "genesis_upload_metadata_verified": True,
            },
            "transport": {"archive_upload_count": 4},
            "privacy": {"violation_count": 0},
        }

    monkeypatch.setattr(
        launcher.genesis_e2e,
        "finalize_existing_session_distill_acceptance",
        finalize,
    )
    projection = launcher._run_trusted_persona_finalize(
        spec, _capture_receipt()
    )

    assert projection["semantic_assertions"] == {
        "persona_acceptance_passed": True,
        "privacy_canary_absent": True,
    }
    assert projection["persona_finalizer"]["evidence_sha256"] == "a" * 64
    assert not authoritative.exists()
    assert not review_copy.exists()
    assert not judgment.exists()


def test_parent_persona_finalizer_failure_still_deletes_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = _persona_spec(tmp_path)
    paths = (
        launcher._persona_authoritative_evidence_path(spec),
        launcher._persona_worker_evidence_path(spec),
        launcher._persona_judgment_path(spec),
    )
    for path in paths:
        path.write_text("private", encoding="utf-8")
        path.chmod(0o600)

    def fail(**_kwargs):
        raise OSError("simulated finalizer failure")

    monkeypatch.setattr(
        launcher.genesis_e2e,
        "finalize_existing_session_distill_acceptance",
        fail,
    )
    with pytest.raises(launcher.WorkerLaunchError, match="persona finalizer"):
        launcher._run_trusted_persona_finalize(spec, _capture_receipt())

    assert all(not path.exists() for path in paths)


def test_parent_persona_finalizer_rejects_oversized_worker_judgment_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spec = _persona_spec(tmp_path)
    authoritative = launcher._persona_authoritative_evidence_path(spec)
    review_copy = launcher._persona_worker_evidence_path(spec)
    judgment = launcher._persona_judgment_path(spec)
    authoritative.write_text("authoritative", encoding="utf-8")
    review_copy.write_text("review", encoding="utf-8")
    judgment.write_bytes(b"x" * (launcher._MAX_PERSONA_JUDGMENT_BYTES + 1))
    for path in (authoritative, review_copy, judgment):
        path.chmod(0o600)

    def must_not_run(**_kwargs):
        raise AssertionError("oversized judgment reached the unbounded reader")

    monkeypatch.setattr(
        launcher.genesis_e2e,
        "finalize_existing_session_distill_acceptance",
        must_not_run,
    )
    with pytest.raises(launcher.WorkerLaunchError, match="persona finalizer"):
        launcher._run_trusted_persona_finalize(spec, _capture_receipt())

    assert all(
        not path.exists() for path in (authoritative, review_copy, judgment)
    )
