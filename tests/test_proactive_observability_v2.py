from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from perception.differ_v2 import PerceptionDifferV2
from proactive.background_v2 import BackgroundWorkerV2, InMemoryBackgroundJobStoreV2
from proactive.observability_v2 import (
    InMemoryMetricsSinkV2,
    METRIC_DOUBLE_SEND_DETECTED,
    METRIC_SCHEDULED_WAKE,
    ProactiveMetricsAggregatorV2,
    record_metric_v2,
)
from proactive.runtime_v2 import BackgroundLeaseRegistryV2, RuntimeSpineV2, TurnRunnerV2, WakeEventV2


def test_runtime_metrics_capture_wake_volume_merge_rate_and_latency():
    metrics = InMemoryMetricsSinkV2()
    spine = RuntimeSpineV2(metrics_sink=metrics, merge_window_sec=1.0)
    runner = TurnRunnerV2(
        spine,
        metrics_sink=metrics,
        run_agent=lambda _context: {"messages": ["one turn"]},
    )

    spine.submit(WakeEventV2(user_id="u1", source="heartbeat", trigger="heartbeat", created_at=100.0))
    spine.submit(
        WakeEventV2(
            user_id="u1",
            source="perception_event",
            trigger="arrived_at_anchor",
            created_at=100.4,
        )
    )
    result = runner.run_ready_turn("u1", now=101.2)
    health = ProactiveMetricsAggregatorV2().snapshot(metrics.events)

    assert result.status == "completed"
    assert health.wake_volume == 2
    assert health.turn_count == 1
    assert health.raw_wakes_processed == 2
    assert health.merged_wake_count == 1
    assert health.merge_rate == 0.5
    assert health.latency_count == 1
    assert round(health.latency_max_ms) == 1200


def test_background_metrics_capture_append_success_and_stale_completion():
    metrics = InMemoryMetricsSinkV2()
    spine = RuntimeSpineV2(metrics_sink=metrics, merge_window_sec=0.0)
    jobs = InMemoryBackgroundJobStoreV2()
    leases = BackgroundLeaseRegistryV2()
    job = jobs.create_job("u1", {"tool": "memory.fetch"}, now=10.0, job_id="bg_1")
    worker = BackgroundWorkerV2(
        spine,
        jobs,
        background_leases=leases,
        metrics_sink=metrics,
        run_background=lambda _job: {"items": 1},
    )

    completed = worker.run_job("u1", job.job_id, now=11.0)
    duplicate = worker.run_job("u1", job.job_id, now=12.0)
    health = ProactiveMetricsAggregatorV2().snapshot(metrics.events)

    assert completed.status == "completed"
    assert duplicate.status == "not_claimed"
    assert health.background_completed_count == 1
    assert health.background_stale_count == 1
    assert health.background_append_success_rate == 0.5


def test_phash_metrics_capture_scene_change_and_dedupe_rate():
    metrics = InMemoryMetricsSinkV2()
    differ = PerceptionDifferV2(metrics_sink=metrics)

    first = differ.observe("u1", "screen_phash", "hash_a", ts=1.0)
    second = differ.observe("u1", "screen_phash", "hash_a", ts=2.0)
    third = differ.observe("u1", "screen_phash", "hash_b", ts=3.0)
    health = ProactiveMetricsAggregatorV2().snapshot(metrics.events)

    assert len(first.events) == 1
    assert second.events == ()
    assert len(third.events) == 1
    assert health.phash_observed_count == 3
    assert health.phash_deduped_count == 1
    assert health.phash_scene_change_count == 2
    assert health.phash_dedupe_rate == 1 / 3


def test_cross_wake_metrics_cover_double_send_and_missed_scheduled_rates():
    metrics = InMemoryMetricsSinkV2()
    record_metric_v2(metrics, user_id="u1", name=METRIC_DOUBLE_SEND_DETECTED)
    record_metric_v2(metrics, user_id="u1", name=METRIC_SCHEDULED_WAKE, tags={"status": "expected"})
    record_metric_v2(metrics, user_id="u1", name=METRIC_SCHEDULED_WAKE, tags={"status": "missed"})

    health = ProactiveMetricsAggregatorV2().snapshot(metrics.events)

    assert health.double_send_count == 1
    assert health.missed_scheduled_wake_count == 1
    assert health.missed_scheduled_wake_rate == 0.5

