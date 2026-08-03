from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))


def _canonical_digest(payload: dict) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "canonical_sha256"}
    encoded = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _healthy_artifact(commit: str = "a" * 40) -> dict:
    samples = 20
    provider_path = {
        "raw_latency_ms": [1.0] * samples,
        "result_digests": ["b" * 64] * samples,
        "exception_fingerprints": [None] * samples,
        "http_attempts": [2] * samples,
        "retries": [1] * samples,
        "business_errors": 0,
    }
    scenarios = {}
    for name in ("baseline", "queue_saturation", "recorder_failures"):
        scenarios[name] = {
            "providers": {
                provider: copy.deepcopy(provider_path)
                for provider in ("openrouter", "anthropic", "google")
            }
        }
    for name in ("queue_saturation", "recorder_failures"):
        for path in scenarios[name]["providers"].values():
            path["raw_latency_ms"] = [1.1] * samples
    scenarios["queue_saturation"].update(
        {
            "queue_capacity": 1,
            "max_queue_size": 1,
            "dropped_count": 119,
            "paired_latency_delta_ms": [0.1] * (samples * 3),
            "paired_p95_regression_ms": 0.1,
        }
    )
    scenarios["recorder_failures"].update(
        {
            "paired_latency_delta_ms": [0.1] * (samples * 3),
            "paired_p95_regression_ms": 0.1,
            "failure_modes": {
                name: {"observed": True, "dropped_count": 1}
                for name in ("startup", "pool", "sql", "serialization")
            },
        }
    )
    payload = {
        "schema_version": 1,
        "producer": "scripts/perf/provider_attempt_business_path.py",
        "run_id": "1f13cb8d-9434-48f4-a6e6-bde67db663a6",
        "git_commit": commit,
        "generated_at": "2026-08-03T00:00:00+00:00",
        "config": {
            "samples_per_provider": samples,
            "warmups_per_provider": 2,
            "hot_path_paired_p95_budget_ms": 5.0,
            "providers": ["openrouter", "anthropic", "google"],
            "pairing_order": "per-provider/per-index/alternating-direction",
        },
        "execution_order": [
            {"provider": provider, "sample": sample, "scenario": scenario}
            for provider in ("openrouter", "anthropic", "google")
            for sample in range(samples)
            for scenario in (
                ("baseline", "queue_saturation", "recorder_failures")
                if sample % 2 == 0
                else ("recorder_failures", "queue_saturation", "baseline")
            )
        ],
        "scenarios": scenarios,
        "pool": {
            "measurement": "production_concurrent_paths",
            "capacity": 16,
            "peak_occupancy": 5,
            "timeouts": 0,
            "operations": ["provider_recorder", "attempt_rollup_maintenance", "usage_report"],
            "provider_results_match_baseline": True,
            "report_statement_timeout_ms": 3000,
            "maintenance_second_connection_observed": True,
            "raw_acquisitions": [
                {"operation": name, "occupancy": index + 1, "wait_ms": 0.1}
                for index, name in enumerate(
                    ("provider_recorder", "attempt_rollup_maintenance", "usage_report")
                )
            ],
        },
    }
    payload["canonical_sha256"] = _canonical_digest(payload)
    return payload


def test_validator_accepts_only_complete_canonical_current_commit_artifact() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    validated = validate_business_path_evidence(artifact, expected_commit="a" * 40)

    assert validated is artifact


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(git_commit="c" * 40), "commit mismatch"),
        (
            lambda item: item["scenarios"]["queue_saturation"]["providers"][
                "openrouter"
            ]["http_attempts"].__setitem__(0, 1),
            "digest mismatch",
        ),
        (lambda item: item.pop("canonical_sha256"), "canonical_sha256"),
        (
            lambda item: item["scenarios"]["baseline"]["providers"].pop("google"),
            "digest mismatch",
        ),
    ],
)
def test_validator_rejects_stale_tampered_or_incomplete_artifact(
    mutation, message
) -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    mutation(artifact)
    with pytest.raises(ValueError, match=message):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_validator_rejects_handwritten_healthy_summary_even_with_fresh_digest() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    fixture = {
        "schema_version": 1,
        "git_commit": "a" * 40,
        "providers": {
            name: {"requests": 100, "p95_ms": 1, "business_errors": 0}
            for name in ("openrouter", "anthropic", "google")
        },
        "pool": {"peak_occupancy": 1, "capacity": 16, "timeouts": 0},
    }
    fixture["canonical_sha256"] = _canonical_digest(fixture)

    with pytest.raises(ValueError, match="producer"):
        validate_business_path_evidence(fixture, expected_commit="a" * 40)


def test_validator_recomputes_raw_paired_latency_and_rejects_claimed_p95() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    artifact["scenarios"]["queue_saturation"]["paired_latency_delta_ms"][:4] = [
        9.0
    ] * 4
    artifact["scenarios"]["queue_saturation"]["providers"]["openrouter"][
        "raw_latency_ms"
    ][:4] = [10.0] * 4
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="paired p95"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_validator_recomputes_each_delta_from_raw_latency_pairs() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    artifact["scenarios"]["queue_saturation"]["providers"]["openrouter"][
        "raw_latency_ms"
    ][0] = 2.0
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="raw paired deltas mismatch"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_admin_scale_gate_delegates_to_strict_business_artifact_validator() -> None:
    from scripts.perf import admin_usage_scale

    artifact = _healthy_artifact()
    assert admin_usage_scale._business_path_evidence_passed(
        artifact, expected_commit="a" * 40
    )

    artifact["canonical_sha256"] = "0" * 64
    assert not admin_usage_scale._business_path_evidence_passed(
        artifact, expected_commit="a" * 40
    )


def test_producer_refuses_to_emit_artifact_without_measured_pool_probe() -> None:
    from scripts.perf.provider_attempt_business_path import (
        produce_business_path_evidence,
    )

    with pytest.raises(ValueError, match="measured pool evidence"):
        produce_business_path_evidence(
            samples_per_provider=20,
            warmups_per_provider=0,
            repo=ROOT,
        )


def test_validator_rejects_non_interleaved_execution_claim() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    artifact["execution_order"][1], artifact["execution_order"][2] = (
        artifact["execution_order"][2],
        artifact["execution_order"][1],
    )
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="interleaved execution order"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_tracked_pool_records_raw_peak_and_nested_maintenance_connection() -> None:
    from scripts.perf.provider_attempt_business_path import _TrackedPool

    class ConnectionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    class Pool:
        max_size = 4

        def connection(self, **_kwargs):
            return ConnectionContext()

    tracker = _TrackedPool(Pool())
    report_ready = threading.Event()
    release = threading.Event()

    def report() -> None:
        with tracker.operation("usage_report"):
            with tracker.connection():
                report_ready.set()
                assert release.wait(1)

    thread = threading.Thread(target=report)
    thread.start()
    assert report_ready.wait(1)
    with tracker.operation("attempt_rollup_maintenance"):
        with tracker.connection():
            with tracker.connection():
                release.set()
    thread.join(1)

    evidence = tracker.evidence(provider_results_match_baseline=True)
    assert evidence["peak_occupancy"] == 3
    assert evidence["maintenance_second_connection_observed"] is True
    assert {item["operation"] for item in evidence["raw_acquisitions"]} == {
        "usage_report",
        "attempt_rollup_maintenance",
    }


def test_scale_cleanup_counts_turn_and_attempt_watermarks_separately() -> None:
    from scripts.perf.admin_usage_scale import _fixture_counts

    class Result:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class Connection:
        def execute(self, sql, _params=None):
            if "FROM v2_usage_rollup_watermarks" in sql:
                return Result(2)
            if "FROM llm_usage_rollup_watermarks" in sql:
                return Result(3)
            return Result(0)

    counts = _fixture_counts(Connection(), "scale_")

    assert counts["turn_watermark"] == 2
    assert counts["attempt_watermark"] == 3


def test_pool_probe_refuses_preexisting_rollup_state() -> None:
    from scripts.perf.provider_attempt_business_path import _assert_pool_probe_empty

    class Result:
        def fetchone(self):
            return (1, 0, 0)

    class Connection:
        def execute(self, _sql):
            return Result()

    with pytest.raises(RuntimeError, match="not empty"):
        _assert_pool_probe_empty(Connection())
