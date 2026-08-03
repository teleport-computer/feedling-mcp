from __future__ import annotations

import copy
from datetime import date
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


def test_maintenance_outcome_is_normalized_to_canonical_json_types() -> None:
    from scripts.perf.provider_attempt_business_path import (
        _maintenance_outcome_json,
    )

    assert _maintenance_outcome_json(
        {
            "status": "ok",
            "completed_through_day": date(2026, 8, 3),
            "retention": {"retained_from": date(2025, 6, 29)},
        }
    ) == {
        "status": "ok",
        "completed_through_day": "2026-08-03",
        "retention": {"retained_from": "2025-06-29"},
    }


def _healthy_artifact(commit: str = "a" * 40) -> dict:
    samples = 20
    provider_path = {
        "raw_latency_ms": [1.0] * samples,
        "started_ns": [120] * samples,
        "finished_ns": [121] * samples,
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
                "startup": {
                    "stage": "thread_factory",
                    "exception_type": "RuntimeError",
                    "injection_calls": 1,
                    "queue_size_before": 0,
                    "queue_size_after": 100,
                    "queue_capacity": 4096,
                    "drop_before": 0,
                    "drop_after": 1,
                    "drop_delta": 1,
                    "queue_full_drops": 0,
                    "start_returned": False,
                },
                "pool": {
                    "stage": "pool_factory",
                    "exception_type": "RuntimeError",
                    "injection_calls": 10,
                    "queue_size_before": 0,
                    "queue_size_after": 0,
                    "queue_capacity": 4096,
                    "drop_before": 0,
                    "drop_after": 20,
                    "drop_delta": 20,
                    "queue_full_drops": 0,
                },
                "sql": {
                    "stage": "cursor_executemany",
                    "exception_type": "RuntimeError",
                    "injection_calls": 10,
                    "queue_size_before": 0,
                    "queue_size_after": 0,
                    "queue_capacity": 4096,
                    "drop_before": 0,
                    "drop_after": 20,
                    "drop_delta": 20,
                    "queue_full_drops": 0,
                },
                "serialization": {
                    "stage": "event_type_check",
                    "exception_type": "TypeError",
                    "injected_items": 1,
                    "consumed_items": 1,
                    "queue_size_before": 0,
                    "queue_size_after": 0,
                    "queue_capacity": 4096,
                    "drop_before": 0,
                    "drop_after": 1,
                    "drop_delta": 1,
                    "queue_full_drops": 0,
                },
            },
        }
    )
    pool_roles = (
        "usage_exporter",
        "usage_importer_core",
        "usage_importer_attempt",
        "provider_recorder",
        "attempt_rollup_outer",
        "attempt_rollup_rebuild",
    )
    raw_acquisitions = [
        {
            "event_id": index + 1,
            "role": role,
            "thread_id": index + 10,
            "acquired_ns": 100 + index,
            "released_ns": 200 + index,
            "wait_ms": 0.1,
            "maintenance_tick_index": (
                0 if role.startswith("attempt_rollup_") else None
            ),
        }
        for index, role in enumerate(pool_roles)
    ]
    active = []
    active_snapshots = []
    for index, role in enumerate(pool_roles):
        active.append(role)
        active_snapshots.append(
            {
                "at_ns": 100 + index,
                "active_count": len(active),
                "active_roles": sorted(set(active)),
            }
        )
    for index, role in enumerate(pool_roles):
        active.remove(role)
        active_snapshots.append(
            {
                "at_ns": 200 + index,
                "active_count": len(active),
                "active_roles": sorted(set(active)),
            }
        )
    pool_paths = {
        "baseline": {
            provider: copy.deepcopy(provider_path)
            for provider in ("openrouter", "anthropic", "google")
        },
        "pool_contention": {
            provider: copy.deepcopy(provider_path)
            for provider in ("openrouter", "anthropic", "google")
        },
    }
    for path in pool_paths["pool_contention"].values():
        path["raw_latency_ms"] = [1.1] * samples
        path["overlapping_roles"] = [
            sorted(pool_roles) for _ in range(samples)
        ]
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
            "peak_occupancy": 6,
            "timeouts": 0,
            "roles": sorted(pool_roles),
            "usage_report_total_deadline_ms": 15000,
            "attempt_subsection_timeout_ms": 3000,
            "maintenance_statement_timeout_ms": 3000,
            "maintenance_second_connection_observed": True,
            "required_overlap_observed": True,
            "active_role_snapshots": active_snapshots,
            "raw_acquisitions": raw_acquisitions,
            "provider_paths": {
                "samples_per_provider": samples,
                "paths": pool_paths,
                "execution_order": [
                    {"provider": provider, "sample": sample, "scenario": scenario}
                    for provider in ("openrouter", "anthropic", "google")
                    for sample in range(samples)
                    for scenario in (
                        ("baseline", "pool_contention")
                        if sample % 2 == 0
                        else ("pool_contention", "baseline")
                    )
                ],
                "paired_latency_delta_ms": [0.1] * (samples * 3),
                "paired_p95_regression_ms": 0.1,
            },
            "maintenance": {
                "seeded_dirty_days": 20,
                "ticks": [
                    {
                        "tick_index": 0,
                        "started_ns": 99,
                        "finished_ns": 206,
                        "outcome": {
                            "status": "ok",
                            "days_refreshed": 20,
                            "refreshed_days": [
                                f"2026-07-{day:02d}" for day in range(1, 21)
                            ],
                        },
                    }
                ],
                "refreshed_local_days": [
                    f"2026-07-{day:02d}" for day in range(1, 21)
                ],
                "dirty_remaining_before_cleanup": 0,
            },
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


def test_validator_rejects_queue_full_as_failure_mode_witness() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    mode = artifact["scenarios"]["recorder_failures"]["failure_modes"]["pool"]
    mode["injection_calls"] = 0
    mode["queue_full_drops"] = mode["drop_delta"]
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="queue-full evidence is ambiguous"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_validator_rejects_missing_pool_role_or_nonoverlapping_intervals() -> None:
    from scripts.perf.provider_attempt_business_path import (
        _derive_pool_timeline,
        validate_business_path_evidence,
    )

    missing = _healthy_artifact()
    missing["pool"]["raw_acquisitions"][1]["role"] = "usage_importer_attempt"
    missing["canonical_sha256"] = _canonical_digest(missing)
    with pytest.raises(ValueError, match="roles incomplete"):
        validate_business_path_evidence(missing, expected_commit="a" * 40)

    disjoint = _healthy_artifact()
    for index, item in enumerate(disjoint["pool"]["raw_acquisitions"]):
        item["acquired_ns"] = index * 20
        item["released_ns"] = index * 20 + 10
    timeline = _derive_pool_timeline(disjoint["pool"]["raw_acquisitions"])
    disjoint["pool"].update(timeline)
    disjoint["canonical_sha256"] = _canonical_digest(disjoint)
    with pytest.raises(ValueError, match="maintenance second connection"):
        validate_business_path_evidence(disjoint, expected_commit="a" * 40)


def test_validator_rejects_pool_contention_latency_regression_from_raw_pairs() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    payload = artifact["pool"]["provider_paths"]
    candidate = payload["paths"]["pool_contention"]["openrouter"][
        "raw_latency_ms"
    ]
    candidate[:4] = [10.0] * 4
    payload["paired_latency_delta_ms"][:4] = [9.0] * 4
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="pool paired p95"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_validator_binds_every_pool_candidate_to_raw_db_contention_window() -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    candidate = artifact["pool"]["provider_paths"]["paths"]["pool_contention"][
        "openrouter"
    ]
    candidate["started_ns"][0] = 300
    candidate["finished_ns"][0] = 301
    candidate["overlapping_roles"][0] = []
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="candidate contention overlap"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_validator_requires_candidate_batch_to_cover_every_db_role() -> None:
    from scripts.perf.provider_attempt_business_path import (
        _derive_pool_timeline,
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    pool = artifact["pool"]
    for acquisition in pool["raw_acquisitions"]:
        if acquisition["role"] == "attempt_rollup_rebuild":
            acquisition["acquired_ns"] = 130
            acquisition["released_ns"] = 140
    pool.update(_derive_pool_timeline(pool["raw_acquisitions"]))
    for path in pool["provider_paths"]["paths"]["pool_contention"].values():
        path["overlapping_roles"] = [
            [role for role in roles if role != "attempt_rollup_rebuild"]
            for roles in path["overlapping_roles"]
        ]
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="role coverage incomplete"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("usage_report_total_deadline_ms", 3000),
        ("attempt_subsection_timeout_ms", 15000),
        ("maintenance_statement_timeout_ms", 15000),
    ],
)
def test_validator_keeps_report_attempt_and_maintenance_timeouts_distinct(
    field, value
) -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    artifact["pool"][field] = value
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match="timeout contract"):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda maintenance: maintenance["ticks"][0]["outcome"].update(
                status="error", error="RollupBuildError"
            ),
            "maintenance tick status",
        ),
        (
            lambda maintenance: maintenance["ticks"][0]["outcome"].update(
                days_refreshed=0, refreshed_days=[]
            ),
            "maintenance tick refreshed no days",
        ),
        (
            lambda maintenance: maintenance["ticks"][0]["outcome"][
                "refreshed_days"
            ].__setitem__(19, "2026-07-01"),
            "maintenance refreshed days not unique",
        ),
        (
            lambda maintenance: maintenance["ticks"][0]["outcome"][
                "refreshed_days"
            ].__setitem__(0, 20260701),
            "maintenance refreshed local day invalid",
        ),
        (
            lambda maintenance: maintenance["ticks"][0]["outcome"][
                "refreshed_days"
            ].__setitem__(0, "2026-02-30"),
            "maintenance refreshed local day invalid",
        ),
        (
            lambda maintenance: (
                maintenance["ticks"][0]["outcome"].update(
                    days_refreshed=19,
                    refreshed_days=maintenance["ticks"][0]["outcome"][
                        "refreshed_days"
                    ][:19],
                ),
                maintenance.update(
                    refreshed_local_days=maintenance["refreshed_local_days"][:19]
                ),
            ),
            "maintenance seeded dirty-day coverage incomplete",
        ),
    ],
)
def test_validator_rejects_failed_noop_repeated_or_incomplete_maintenance(
    mutation, message
) -> None:
    from scripts.perf.provider_attempt_business_path import (
        validate_business_path_evidence,
    )

    artifact = _healthy_artifact()
    mutation(artifact["pool"]["maintenance"])
    artifact["canonical_sha256"] = _canonical_digest(artifact)

    with pytest.raises(ValueError, match=message):
        validate_business_path_evidence(artifact, expected_commit="a" * 40)


def test_tracked_pool_records_raw_peak_and_nested_maintenance_connection() -> None:
    from scripts.perf.provider_attempt_business_path import (
        _TrackedPool,
        _derive_pool_timeline,
    )

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
        with tracker.operation("usage_exporter"):
            with tracker.connection():
                report_ready.set()
                assert release.wait(1)

    thread = threading.Thread(target=report)
    thread.start()
    assert report_ready.wait(1)
    with tracker.operation("attempt_rollup"):
        with tracker.connection():
            with tracker.connection():
                release.set()
    thread.join(1)

    timeline = _derive_pool_timeline(tracker._raw)
    assert timeline["peak_occupancy"] == 3
    assert timeline["maintenance_second_connection_observed"] is True
    assert {item["role"] for item in tracker._raw} == {
        "usage_exporter",
        "attempt_rollup_outer",
        "attempt_rollup_rebuild",
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


def test_serialization_witness_is_not_invalidated_by_later_queue_activity() -> None:
    from scripts.perf.provider_attempt_business_path import (
        _serialization_mode_evidence,
    )

    class Recorder:
        queue_size = 7
        dropped_count = 4

    evidence = _serialization_mode_evidence(
        recorder=Recorder(),
        before=(0, 3),
        capacity=4096,
        witness_consumed=True,
    )

    assert evidence["injected_items"] == 1
    assert evidence["consumed_items"] == 1
    assert evidence["queue_size_after"] == 7
    assert evidence["drop_delta"] == 1


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
