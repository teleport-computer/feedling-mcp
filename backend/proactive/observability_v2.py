"""Observability and eval primitives for Proactive/Perception Runtime V2."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid
from typing import Any, Mapping, Protocol, Sequence


RUNTIME_METRICS_STREAM_V2 = "proactive_runtime_metrics_v2"

METRIC_WAKE_SUBMITTED = "wake.submitted"
METRIC_TURN_STARTED = "turn.started"
METRIC_TURN_COMPLETED = "turn.completed"
METRIC_BACKGROUND_JOB = "background.job"
METRIC_PHASH_FRAME = "phash.frame"
METRIC_SCHEDULED_WAKE = "scheduled.wake"
METRIC_DOUBLE_SEND_DETECTED = "double_send.detected"

BACKGROUND_STALE_STATUSES_V2 = frozenset({"not_claimed", "completion_lost", "stale"})
ROUND3_REVIEW_LABELS_V2 = (
    "good_presence",
    "missed_moment",
    "went_dark",
    "too_much_buzz",
    "too_chatty",
    "wrong_voice",
    "ignored_manual",
    "stutter",
    "late_irrelevant",
    "privacy_bad",
)


@dataclass(frozen=True)
class MetricEventV2:
    user_id: str
    name: str
    value: float = 1.0
    tags: Mapping[str, Any] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: "metric_" + uuid.uuid4().hex[:16])

    def to_doc(self) -> dict[str, Any]:
        return {
            "kind": "runtime_metric_v2",
            "event_id": self.event_id,
            "user_id": self.user_id,
            "name": self.name,
            "value": float(self.value),
            "tags": dict(self.tags or {}),
            "data": dict(self.data or {}),
            "ts": float(self.ts),
        }


class MetricsSinkV2(Protocol):
    def record(self, event: MetricEventV2) -> None:
        ...


class InMemoryMetricsSinkV2:
    def __init__(self) -> None:
        self.events: list[MetricEventV2] = []

    def record(self, event: MetricEventV2) -> None:
        self.events.append(event)


def record_metric_v2(
    sink: MetricsSinkV2 | None,
    *,
    user_id: str,
    name: str,
    value: float = 1.0,
    tags: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    ts: float | None = None,
) -> None:
    if sink is None:
        return
    try:
        sink.record(
            MetricEventV2(
                user_id=str(user_id or ""),
                name=str(name or ""),
                value=float(value),
                tags=dict(tags or {}),
                data=dict(data or {}),
                ts=time.time() if ts is None else float(ts),
            )
        )
    except Exception:
        return


@dataclass(frozen=True)
class ProactiveHealthSnapshotV2:
    wake_volume: int
    turn_count: int
    raw_wakes_processed: int
    merged_wake_count: int
    merge_rate: float
    double_send_count: int
    double_send_rate: float
    scheduled_expected_count: int
    missed_scheduled_wake_count: int
    missed_scheduled_wake_rate: float
    latency_count: int
    latency_p50_ms: float
    latency_p95_ms: float
    latency_max_ms: float
    background_completed_count: int
    background_stale_count: int
    background_append_success_rate: float
    phash_observed_count: int
    phash_deduped_count: int
    phash_scene_change_count: int
    phash_dedupe_rate: float

    def to_doc(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _percentile(values: Sequence[float], q: float) -> float:
    nums = sorted(float(value) for value in values)
    if not nums:
        return 0.0
    if len(nums) == 1:
        return nums[0]
    pos = max(0.0, min(1.0, q)) * (len(nums) - 1)
    low = int(pos)
    high = min(low + 1, len(nums) - 1)
    frac = pos - low
    return nums[low] * (1.0 - frac) + nums[high] * frac


class ProactiveMetricsAggregatorV2:
    def snapshot(self, events: Sequence[MetricEventV2 | Mapping[str, Any]]) -> ProactiveHealthSnapshotV2:
        normalized = [self._coerce(event) for event in events]
        wake_volume = 0
        turn_count = 0
        raw_wakes_processed = 0
        merged_wake_count = 0
        double_send_count = 0
        scheduled_expected_count = 0
        missed_scheduled_wake_count = 0
        latency_values: list[float] = []
        background_completed_count = 0
        background_stale_count = 0
        background_append_success_count = 0
        background_terminal_count = 0
        phash_observed_count = 0
        phash_deduped_count = 0
        phash_scene_change_count = 0

        for event in normalized:
            if event.name == METRIC_WAKE_SUBMITTED and bool(event.data.get("accepted", True)):
                wake_volume += int(event.value)
            elif event.name == METRIC_TURN_COMPLETED:
                turn_count += 1
                wake_count = int(event.data.get("wake_count") or 0)
                raw_wakes_processed += wake_count
                merged_wake_count += max(0, wake_count - 1)
                latency_values.append(float(event.data.get("latency_ms") or 0.0))
            elif event.name == METRIC_DOUBLE_SEND_DETECTED:
                double_send_count += int(event.value)
            elif event.name == METRIC_SCHEDULED_WAKE:
                status = str(event.tags.get("status") or "")
                if status in {"expected", "triggered", "missed"}:
                    scheduled_expected_count += int(event.value)
                if status == "missed":
                    missed_scheduled_wake_count += int(event.value)
            elif event.name == METRIC_BACKGROUND_JOB:
                status = str(event.tags.get("status") or "")
                if status == "completed" or status == "failed" or status in BACKGROUND_STALE_STATUSES_V2:
                    background_terminal_count += 1
                if status == "completed":
                    background_completed_count += 1
                    if bool(event.data.get("wake_submitted")):
                        background_append_success_count += 1
                elif status in BACKGROUND_STALE_STATUSES_V2:
                    background_stale_count += 1
            elif event.name == METRIC_PHASH_FRAME:
                outcome = str(event.tags.get("outcome") or "")
                phash_observed_count += 1
                if outcome == "deduped":
                    phash_deduped_count += 1
                elif outcome == "scene_change":
                    phash_scene_change_count += 1

        merge_rate = merged_wake_count / raw_wakes_processed if raw_wakes_processed else 0.0
        double_send_rate = double_send_count / turn_count if turn_count else 0.0
        missed_scheduled_wake_rate = (
            missed_scheduled_wake_count / scheduled_expected_count
            if scheduled_expected_count
            else 0.0
        )
        background_append_success_rate = (
            background_append_success_count / background_terminal_count
            if background_terminal_count
            else 0.0
        )
        phash_dedupe_rate = phash_deduped_count / phash_observed_count if phash_observed_count else 0.0
        return ProactiveHealthSnapshotV2(
            wake_volume=wake_volume,
            turn_count=turn_count,
            raw_wakes_processed=raw_wakes_processed,
            merged_wake_count=merged_wake_count,
            merge_rate=merge_rate,
            double_send_count=double_send_count,
            double_send_rate=double_send_rate,
            scheduled_expected_count=scheduled_expected_count,
            missed_scheduled_wake_count=missed_scheduled_wake_count,
            missed_scheduled_wake_rate=missed_scheduled_wake_rate,
            latency_count=len(latency_values),
            latency_p50_ms=_percentile(latency_values, 0.50),
            latency_p95_ms=_percentile(latency_values, 0.95),
            latency_max_ms=max(latency_values) if latency_values else 0.0,
            background_completed_count=background_completed_count,
            background_stale_count=background_stale_count,
            background_append_success_rate=background_append_success_rate,
            phash_observed_count=phash_observed_count,
            phash_deduped_count=phash_deduped_count,
            phash_scene_change_count=phash_scene_change_count,
            phash_dedupe_rate=phash_dedupe_rate,
        )

    @staticmethod
    def _coerce(event: MetricEventV2 | Mapping[str, Any]) -> MetricEventV2:
        if isinstance(event, MetricEventV2):
            return event
        doc = event if isinstance(event, Mapping) else {}
        return MetricEventV2(
            user_id=str(doc.get("user_id") or ""),
            name=str(doc.get("name") or ""),
            value=float(doc.get("value") or 1.0),
            tags=dict(doc.get("tags") or {}),
            data=dict(doc.get("data") or {}),
            ts=float(doc.get("ts") or 0.0),
            event_id=str(doc.get("event_id") or ""),
        )
