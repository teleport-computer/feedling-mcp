#!/usr/bin/env python3
"""Produce canonical no-business-impact evidence through real provider adapters.

The network boundary is an in-process deterministic ``httpx.MockTransport``;
provider parsing, retry classification and provider-attempt instrumentation are
the production implementations.  The artifact is not signed (there is no
secret to protect): the formal runner binds it to the current full Git commit,
validates every raw sample and recomputes a canonical SHA-256 digest.
"""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

import httpx
from psycopg.conninfo import conninfo_to_dict


SCHEMA_VERSION = 1
PRODUCER = "scripts/perf/provider_attempt_business_path.py"
PROVIDERS = ("openrouter", "anthropic", "google")
FAILURE_MODES = ("startup", "pool", "sql", "serialization")
FAILURE_STAGES = {
    "startup": "thread_factory",
    "pool": "pool_factory",
    "sql": "cursor_executemany",
    "serialization": "event_type_check",
}
MIN_SAMPLES_PER_PROVIDER = 20
DEFAULT_SAMPLES_PER_PROVIDER = 40
DEFAULT_WARMUPS_PER_PROVIDER = 3
HOT_PATH_PAIRED_P95_BUDGET_MS = 5.0
USAGE_REPORT_TOTAL_DEADLINE_MS = 15_000
ATTEMPT_SUBSECTION_TIMEOUT_MS = 3_000
MAINTENANCE_STATEMENT_TIMEOUT_MS = 3_000
SEEDED_DIRTY_DAYS = 20
POOL_ROLES = (
    "usage_exporter",
    "usage_importer_core",
    "usage_importer_attempt",
    "provider_recorder",
    "attempt_rollup_outer",
    "attempt_rollup_rebuild",
)


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "canonical_sha256"}
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _maintenance_outcome_json(value: Any) -> Any:
    """Normalize a raw maintenance outcome to explicit canonical JSON types."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("maintenance outcome keys must be strings")
        return {key: _maintenance_outcome_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_maintenance_outcome_json(item) for item in value]
    raise TypeError(
        f"maintenance outcome contains non-JSON type: {type(value).__name__}"
    )


def canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("raw latency samples required")
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_provider_path(path: Any, *, samples: int, label: str) -> None:
    _require(isinstance(path, dict), f"{label} provider path missing")
    arrays = {
        key: path.get(key)
        for key in (
            "raw_latency_ms",
            "started_ns",
            "finished_ns",
            "result_digests",
            "exception_fingerprints",
            "http_attempts",
            "retries",
        )
    }
    for key, values in arrays.items():
        _require(isinstance(values, list), f"{label} {key} raw samples missing")
        _require(len(values) == samples, f"{label} {key} sample count mismatch")
    _require(
        all(isinstance(value, (int, float)) and value >= 0 for value in arrays["raw_latency_ms"]),
        f"{label} latency sample invalid",
    )
    _require(
        all(
            isinstance(started, int)
            and isinstance(finished, int)
            and finished > started
            for started, finished in zip(
                arrays["started_ns"], arrays["finished_ns"], strict=True
            )
        ),
        f"{label} call interval invalid",
    )
    _require(
        all(isinstance(value, str) and len(value) == 64 for value in arrays["result_digests"]),
        f"{label} result digest invalid",
    )
    _require(
        all(value is None or isinstance(value, str) for value in arrays["exception_fingerprints"]),
        f"{label} exception sample invalid",
    )
    _require(
        all(isinstance(value, int) and value >= 1 for value in arrays["http_attempts"]),
        f"{label} HTTP attempt sample invalid",
    )
    _require(
        all(isinstance(value, int) and value >= 0 for value in arrays["retries"]),
        f"{label} retry sample invalid",
    )
    _require(path.get("business_errors") == 0, f"{label} leaked a business error")


def validate_business_path_evidence(
    evidence: Any, *, expected_commit: str
) -> dict[str, Any]:
    """Validate provenance, raw samples and all no-impact invariants."""
    _require(isinstance(evidence, dict), "business evidence must be an object")
    _require(evidence.get("producer") == PRODUCER, "producer mismatch")
    _require(evidence.get("schema_version") == SCHEMA_VERSION, "schema version mismatch")
    _require(
        isinstance(expected_commit, str) and len(expected_commit) == 40,
        "expected full git commit required",
    )
    _require(evidence.get("git_commit") == expected_commit, "commit mismatch")
    try:
        UUID(str(evidence.get("run_id")))
        datetime.fromisoformat(str(evidence.get("generated_at")))
    except (TypeError, ValueError) as exc:
        raise ValueError("run identity invalid") from exc
    claimed_digest = evidence.get("canonical_sha256")
    _require(
        isinstance(claimed_digest, str) and len(claimed_digest) == 64,
        "canonical_sha256 missing",
    )
    _require(claimed_digest == canonical_digest(evidence), "digest mismatch")

    config = evidence.get("config")
    _require(isinstance(config, dict), "config missing")
    samples = config.get("samples_per_provider")
    _require(
        isinstance(samples, int) and samples >= MIN_SAMPLES_PER_PROVIDER,
        "insufficient provider samples",
    )
    budget = config.get("hot_path_paired_p95_budget_ms")
    _require(
        isinstance(budget, (int, float))
        and 0 < float(budget) <= HOT_PATH_PAIRED_P95_BUDGET_MS,
        "latency budget invalid",
    )
    _require(config.get("providers") == list(PROVIDERS), "provider config mismatch")
    _require(
        config.get("pairing_order")
        == "per-provider/per-index/alternating-direction",
        "pairing order config mismatch",
    )
    expected_execution_order = [
        {"provider": provider, "sample": sample, "scenario": scenario}
        for provider in PROVIDERS
        for sample in range(samples)
        for scenario in (
            ("baseline", "queue_saturation", "recorder_failures")
            if sample % 2 == 0
            else ("recorder_failures", "queue_saturation", "baseline")
        )
    ]
    _require(
        evidence.get("execution_order") == expected_execution_order,
        "interleaved execution order invalid",
    )
    scenarios = evidence.get("scenarios")
    _require(
        isinstance(scenarios, dict)
        and set(scenarios) == {"baseline", "queue_saturation", "recorder_failures"},
        "scenario set mismatch",
    )
    baseline_paths = scenarios["baseline"].get("providers")
    _require(isinstance(baseline_paths, dict), "baseline providers missing")
    for scenario_name, scenario in scenarios.items():
        paths = scenario.get("providers") if isinstance(scenario, dict) else None
        _require(isinstance(paths, dict) and set(paths) == set(PROVIDERS), f"{scenario_name} provider set mismatch")
        for provider in PROVIDERS:
            label = f"{scenario_name}/{provider}"
            _validate_provider_path(paths[provider], samples=samples, label=label)
            if scenario_name != "baseline":
                baseline = baseline_paths[provider]
                _require(paths[provider]["result_digests"] == baseline["result_digests"], f"{label} results differ from baseline")
                _require(paths[provider]["exception_fingerprints"] == baseline["exception_fingerprints"], f"{label} exceptions differ from baseline")
                _require(paths[provider]["http_attempts"] == baseline["http_attempts"], f"{label} HTTP attempts differ from baseline")
                _require(paths[provider]["retries"] == baseline["retries"], f"{label} retries differ from baseline")

    for scenario_name in ("queue_saturation", "recorder_failures"):
        scenario = scenarios[scenario_name]
        deltas = scenario.get("paired_latency_delta_ms")
        _require(isinstance(deltas, list) and len(deltas) == samples * len(PROVIDERS), f"{scenario_name} paired raw samples missing")
        _require(all(isinstance(value, (int, float)) for value in deltas), f"{scenario_name} paired sample invalid")
        expected_deltas = [
            round(float(candidate) - float(control), 6)
            for provider in PROVIDERS
            for control, candidate in zip(
                baseline_paths[provider]["raw_latency_ms"],
                scenario["providers"][provider]["raw_latency_ms"],
                strict=True,
            )
        ]
        _require(
            deltas == expected_deltas,
            f"{scenario_name} raw paired deltas mismatch",
        )
        recomputed = _nearest_rank([float(value) for value in deltas], 0.95)
        _require(recomputed < float(budget), f"{scenario_name} paired p95 exceeds strict budget")
        _require(abs(float(scenario.get("paired_p95_regression_ms")) - recomputed) < 0.001, f"{scenario_name} paired p95 claim mismatch")

    saturation = scenarios["queue_saturation"]
    capacity = saturation.get("queue_capacity")
    _require(isinstance(capacity, int) and capacity > 0, "queue capacity invalid")
    _require(
        isinstance(saturation.get("max_queue_size"), int)
        and 0 <= saturation["max_queue_size"] <= capacity,
        "queue exceeded capacity",
    )
    _require(isinstance(saturation.get("dropped_count"), int) and saturation["dropped_count"] > 0, "queue saturation did not drop")

    failure_modes = scenarios["recorder_failures"].get("failure_modes")
    _require(isinstance(failure_modes, dict) and set(failure_modes) == set(FAILURE_MODES), "recorder failure modes incomplete")
    for name in FAILURE_MODES:
        mode = failure_modes[name]
        _require(mode.get("stage") == FAILURE_STAGES[name], f"recorder {name} stage mismatch")
        _require(mode.get("exception_type") in {"RuntimeError", "TypeError"}, f"recorder {name} exception witness missing")
        _require(mode.get("queue_size_before") == 0, f"recorder {name} queue precondition invalid")
        _require(mode.get("queue_full_drops") == 0, f"recorder {name} queue-full evidence is ambiguous")
        _require(
            isinstance(mode.get("drop_before"), int)
            and isinstance(mode.get("drop_after"), int)
            and mode.get("drop_delta") == mode["drop_after"] - mode["drop_before"]
            and mode["drop_delta"] > 0,
            f"recorder {name} dedicated drop delta invalid",
        )
        if name == "serialization":
            _require(mode.get("injected_items") == 1 and mode.get("consumed_items") == 1, "recorder serialization witness missing")
            _require(mode["drop_delta"] == 1, "recorder serialization drop delta invalid")
        else:
            _require(isinstance(mode.get("injection_calls"), int) and mode["injection_calls"] > 0, f"recorder {name} injection was not called")
        if name == "startup":
            _require(mode["injection_calls"] == 1 and mode["drop_delta"] == 1, "recorder startup witness invalid")

    pool = evidence.get("pool")
    _require(isinstance(pool, dict), "pool evidence missing")
    _require(pool.get("measurement") == "production_concurrent_paths", "pool measurement provenance missing")
    _require(isinstance(pool.get("capacity"), int) and pool["capacity"] >= 2, "pool capacity invalid")
    _require(pool.get("timeouts") == 0, "pool timeout observed")
    _require(
        pool.get("usage_report_total_deadline_ms")
        == USAGE_REPORT_TOTAL_DEADLINE_MS
        and pool.get("attempt_subsection_timeout_ms")
        == ATTEMPT_SUBSECTION_TIMEOUT_MS
        and pool.get("maintenance_statement_timeout_ms")
        == MAINTENANCE_STATEMENT_TIMEOUT_MS,
        "pool timeout contract mismatch",
    )
    acquisitions = pool.get("raw_acquisitions")
    _require(isinstance(acquisitions, list) and acquisitions, "pool raw acquisitions missing")
    _require(
        all(
            isinstance(item, dict)
            and item.get("role") in POOL_ROLES
            and isinstance(item.get("event_id"), int)
            and isinstance(item.get("wait_ms"), (int, float))
            and item["wait_ms"] >= 0
            for item in acquisitions
        ),
        "pool raw acquisition invalid",
    )
    timeline = _derive_pool_timeline(acquisitions)
    _require(
        set(item["role"] for item in acquisitions) == set(POOL_ROLES),
        "pool roles incomplete",
    )
    _require(pool.get("roles") == sorted(POOL_ROLES), "pool role summary mismatch")
    for key in (
        "peak_occupancy",
        "maintenance_second_connection_observed",
        "required_overlap_observed",
        "active_role_snapshots",
    ):
        _require(pool.get(key) == timeline[key], f"pool {key} summary mismatch")
    _require(timeline["peak_occupancy"] <= pool["capacity"], "pool peak exceeds capacity")
    _require(timeline["maintenance_second_connection_observed"] is True, "maintenance second connection not observed")
    _require(timeline["required_overlap_observed"] is True, "required pool role overlap absent")
    _validate_maintenance_evidence(pool.get("maintenance"), acquisitions)
    _validate_pool_provider_paths(
        pool.get("provider_paths"),
        budget=float(budget),
        acquisitions=acquisitions,
    )
    return evidence


def _validate_maintenance_evidence(
    maintenance: Any, acquisitions: list[dict[str, Any]]
) -> None:
    _require(isinstance(maintenance, dict), "maintenance evidence missing")
    _require(
        maintenance.get("seeded_dirty_days") == SEEDED_DIRTY_DAYS,
        "maintenance seeded dirty-day claim invalid",
    )
    ticks = maintenance.get("ticks")
    _require(isinstance(ticks, list) and ticks, "maintenance raw ticks missing")
    refreshed: list[str] = []
    valid_indices: set[int] = set()
    for tick in ticks:
        _require(isinstance(tick, dict), "maintenance raw tick invalid")
        index = tick.get("tick_index")
        started = tick.get("started_ns")
        finished = tick.get("finished_ns")
        outcome = tick.get("outcome")
        _require(
            isinstance(index, int) and index not in valid_indices,
            "maintenance tick index invalid",
        )
        valid_indices.add(index)
        _require(
            isinstance(started, int)
            and isinstance(finished, int)
            and finished > started,
            "maintenance tick interval invalid",
        )
        _require(
            isinstance(outcome, dict)
            and outcome.get("status") == "ok"
            and not any(
                key in outcome
                for key in ("error", "cancelled", "canceled", "lock_busy")
            ),
            "maintenance tick status invalid",
        )
        days = outcome.get("refreshed_days")
        _require(
            isinstance(days, list)
            and days
            and outcome.get("days_refreshed") == len(days),
            "maintenance tick refreshed no days",
        )
        try:
            for day in days:
                _require(
                    isinstance(day, str)
                    and datetime.strptime(day, "%Y-%m-%d").date().isoformat() == day,
                    "maintenance refreshed local day invalid",
                )
        except (TypeError, ValueError) as exc:
            raise ValueError("maintenance refreshed local day invalid") from exc
        refreshed.extend(days)

        tick_acquisitions = [
            item
            for item in acquisitions
            if item.get("maintenance_tick_index") == index
        ]
        _require(
            {item["role"] for item in tick_acquisitions}
            == {"attempt_rollup_outer", "attempt_rollup_rebuild"},
            "maintenance tick recompute intervals incomplete",
        )
        _require(
            all(
                started <= int(item["acquired_ns"])
                and int(item["released_ns"]) <= finished
                for item in tick_acquisitions
            ),
            "maintenance tick recompute interval mismatch",
        )

    _require(
        len(refreshed) == len(set(refreshed)),
        "maintenance refreshed days not unique",
    )
    _require(
        len(refreshed) == SEEDED_DIRTY_DAYS,
        "maintenance seeded dirty-day coverage incomplete",
    )
    _require(
        maintenance.get("refreshed_local_days") == refreshed,
        "maintenance refreshed-day claim mismatch",
    )
    _require(
        maintenance.get("dirty_remaining_before_cleanup") == 0,
        "maintenance dirty backlog not drained",
    )
    maintenance_acquisitions = [
        item for item in acquisitions if str(item.get("role", "")).startswith("attempt_rollup_")
    ]
    _require(
        all(item.get("maintenance_tick_index") in valid_indices for item in maintenance_acquisitions),
        "maintenance interval lacks successful tick",
    )


def _validate_pool_provider_paths(
    payload: Any, *, budget: float, acquisitions: list[dict[str, Any]]
) -> None:
    _require(isinstance(payload, dict), "pool provider paths missing")
    samples = payload.get("samples_per_provider")
    _require(isinstance(samples, int) and samples >= MIN_SAMPLES_PER_PROVIDER, "pool provider samples insufficient")
    paths = payload.get("paths")
    _require(isinstance(paths, dict) and set(paths) == {"baseline", "pool_contention"}, "pool provider scenarios invalid")
    for scenario in ("baseline", "pool_contention"):
        _require(set(paths[scenario]) == set(PROVIDERS), f"pool {scenario} providers invalid")
        for provider in PROVIDERS:
            _validate_provider_path(
                paths[scenario][provider],
                samples=samples,
                label=f"pool/{scenario}/{provider}",
            )
    expected_order = [
        {"provider": provider, "sample": sample, "scenario": scenario}
        for provider in PROVIDERS
        for sample in range(samples)
        for scenario in (
            ("baseline", "pool_contention")
            if sample % 2 == 0
            else ("pool_contention", "baseline")
        )
    ]
    _require(payload.get("execution_order") == expected_order, "pool provider execution order invalid")
    for provider in PROVIDERS:
        baseline = paths["baseline"][provider]
        candidate = paths["pool_contention"][provider]
        for key in (
            "result_digests",
            "exception_fingerprints",
            "http_attempts",
            "retries",
        ):
            _require(candidate[key] == baseline[key], f"pool {provider} {key} changed")
        claimed_overlaps = candidate.get("overlapping_roles")
        _require(
            isinstance(claimed_overlaps, list) and len(claimed_overlaps) == samples,
            f"pool {provider} candidate contention overlap missing",
        )
        for index, (started, finished) in enumerate(
            zip(candidate["started_ns"], candidate["finished_ns"], strict=True)
        ):
            expected_roles = _overlapping_pool_roles(
                started, finished, acquisitions
            )
            _require(
                claimed_overlaps[index] == expected_roles and expected_roles,
                f"pool {provider} candidate contention overlap invalid",
            )
    covered_roles = {
        role
        for provider in PROVIDERS
        for roles in paths["pool_contention"][provider]["overlapping_roles"]
        for role in roles
    }
    _require(
        covered_roles == set(POOL_ROLES),
        "pool candidate contention role coverage incomplete: "
        f"{sorted(covered_roles)}",
    )
    expected_deltas = _paired_deltas(paths["baseline"], paths["pool_contention"])
    _require(payload.get("paired_latency_delta_ms") == expected_deltas, "pool raw paired deltas mismatch")
    p95 = _nearest_rank(expected_deltas, 0.95)
    _require(p95 < budget, "pool paired p95 exceeds strict budget")
    _require(abs(float(payload.get("paired_p95_regression_ms")) - p95) < 0.001, "pool paired p95 claim mismatch")


def _overlapping_pool_roles(
    started_ns: int,
    finished_ns: int,
    acquisitions: list[dict[str, Any]],
) -> list[str]:
    """Return exact DB roles whose half-open intervals intersect a call."""

    return sorted(
        {
            str(item["role"])
            for item in acquisitions
            if int(item["acquired_ns"]) < finished_ns
            and started_ns < int(item["released_ns"])
        }
    )


class _Context(AbstractContextManager):
    def __init__(self, value: Any):
        self.value = value

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, *_args: Any) -> bool:
        return False


class _TrackedConnectionContext(AbstractContextManager):
    def __init__(self, tracker: "_TrackedPool", kwargs: dict[str, Any]):
        self._tracker = tracker
        self._kwargs = kwargs
        self._inner: Any = None
        self._connection: Any = None
        self._role = ""
        self._event_id = 0

    def __enter__(self) -> Any:
        started = time.perf_counter_ns()
        try:
            self._inner = self._tracker._pool.connection(**self._kwargs)
            self._connection = self._inner.__enter__()
        except BaseException:  # noqa: BLE001 - record and preserve pool failure
            with self._tracker._lock:
                self._tracker._timeouts += 1
            raise
        wait_ms = (time.perf_counter_ns() - started) / 1_000_000
        acquired_ns = time.perf_counter_ns()
        with self._tracker._lock:
            self._role = self._tracker._current_role_locked()
            self._tracker._occupancy += 1
            self._tracker._peak = max(self._tracker._peak, self._tracker._occupancy)
            self._tracker._next_event_id += 1
            self._event_id = self._tracker._next_event_id
            thread_id = threading.get_ident()
            self._tracker._active_by_thread.setdefault(thread_id, []).append(
                self._event_id
            )
            self._tracker._raw.append(
                {
                    "event_id": self._event_id,
                    "role": self._role,
                    "thread_id": thread_id,
                    "acquired_ns": acquired_ns,
                    "released_ns": None,
                    "wait_ms": round(wait_ms, 6),
                    "maintenance_tick_index": getattr(
                        self._tracker._local, "maintenance_tick_index", None
                    ),
                }
            )
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            return bool(self._inner.__exit__(exc_type, exc, tb))
        finally:
            released_ns = time.perf_counter_ns()
            with self._tracker._lock:
                self._tracker._occupancy -= 1
                for item in self._tracker._raw:
                    if item["event_id"] == self._event_id:
                        item["released_ns"] = released_ns
                        break
                active = self._tracker._active_by_thread.get(threading.get_ident(), [])
                if self._event_id in active:
                    active.remove(self._event_id)


class _TrackedPool:
    """Transparent pool facade with auditable per-acquisition samples."""

    def __init__(self, pool: Any):
        self._pool = pool
        self._local = threading.local()
        self._lock = threading.Lock()
        self._occupancy = 0
        self._peak = 0
        self._timeouts = 0
        self._active_by_thread: dict[int, list[int]] = {}
        self._next_event_id = 0
        self._raw: list[dict[str, Any]] = []

    @property
    def max_size(self) -> int:
        return int(self._pool.max_size)

    def _current_role_locked(self) -> str:
        explicit = getattr(self._local, "operation", "")
        if explicit == "attempt_rollup":
            active = self._active_by_thread.get(threading.get_ident(), [])
            return "attempt_rollup_rebuild" if active else "attempt_rollup_outer"
        if explicit:
            return str(explicit)
        name = threading.current_thread().name.lower()
        if "provider-attempt-recorder" in name:
            return "provider_recorder"
        if "usage" in name or "importer" in name:
            return "usage_importer_pending"
        return "unknown"

    def assign_current_role(self, role: str) -> None:
        if role not in {"usage_importer_core", "usage_importer_attempt"}:
            raise ValueError("invalid importer role")
        with self._lock:
            active = self._active_by_thread.get(threading.get_ident(), [])
            if not active:
                raise RuntimeError("no active importer acquisition")
            event_id = active[-1]
            for item in self._raw:
                if item["event_id"] == event_id:
                    item["role"] = role
                    return
            raise RuntimeError("active importer acquisition missing")

    def wait_for_active_roles(
        self, required: set[str], *, timeout: float
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                active_ids = {
                    event_id
                    for event_ids in self._active_by_thread.values()
                    for event_id in event_ids
                }
                active_roles = {
                    str(item["role"])
                    for item in self._raw
                    if item["event_id"] in active_ids
                }
            if required <= active_roles:
                return True
            threading.Event().wait(0.001)
        return False

    @contextmanager
    def operation(self, name: str, *, maintenance_tick_index: int | None = None):
        previous = getattr(self._local, "operation", "")
        previous_tick = getattr(self._local, "maintenance_tick_index", None)
        self._local.operation = name
        self._local.maintenance_tick_index = maintenance_tick_index
        try:
            yield self
        finally:
            self._local.operation = previous
            self._local.maintenance_tick_index = previous_tick

    def connection(self, **kwargs: Any) -> _TrackedConnectionContext:
        return _TrackedConnectionContext(self, kwargs)

    def evidence(self, *, provider_paths: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            timeline = _derive_pool_timeline(self._raw)
            return {
                "measurement": "production_concurrent_paths",
                "capacity": self.max_size,
                "peak_occupancy": timeline["peak_occupancy"],
                "timeouts": self._timeouts,
                "roles": sorted({item["role"] for item in self._raw}),
                "maintenance_second_connection_observed": timeline[
                    "maintenance_second_connection_observed"
                ],
                "required_overlap_observed": timeline[
                    "required_overlap_observed"
                ],
                "active_role_snapshots": timeline["active_role_snapshots"],
                "raw_acquisitions": list(self._raw),
                "provider_paths": provider_paths,
            }


def _derive_pool_timeline(raw: list[dict[str, Any]]) -> dict[str, Any]:
    points: list[tuple[int, int, int, str]] = []
    for item in raw:
        acquired = item.get("acquired_ns")
        released = item.get("released_ns")
        if not isinstance(acquired, int) or not isinstance(released, int):
            raise ValueError("pool acquisition interval incomplete")
        if released < acquired:
            raise ValueError("pool acquisition interval reversed")
        # A release and a later acquisition sharing the same clock tick are not
        # overlapping; process releases first to keep the proof fail-closed.
        points.append((acquired, 1, int(item["event_id"]), str(item["role"])))
        points.append((released, 0, int(item["event_id"]), str(item["role"])))
    active: dict[int, str] = {}
    snapshots: list[dict[str, Any]] = []
    peak = 0
    for at_ns, kind, event_id, role in sorted(points):
        if kind == 0:
            active.pop(event_id, None)
        else:
            active[event_id] = role
        roles = sorted(set(active.values()))
        peak = max(peak, len(active))
        snapshots.append(
            {"at_ns": at_ns, "active_count": len(active), "active_roles": roles}
        )
    role_sets = [set(item["active_roles"]) for item in snapshots]
    maintenance = {"attempt_rollup_outer", "attempt_rollup_rebuild"}
    usage = {
        "usage_exporter",
        "usage_importer_core",
        "usage_importer_attempt",
    }
    maintenance_second = any(maintenance <= roles for roles in role_sets)
    required_overlap = bool(
        any(usage <= roles for roles in role_sets)
        and maintenance_second
        and any(
            "provider_recorder" in roles
            and "attempt_rollup_outer" in roles
            and len(roles & usage) >= 1
            for roles in role_sets
        )
    )
    return {
        "peak_occupancy": peak,
        "maintenance_second_connection_observed": maintenance_second,
        "required_overlap_observed": required_overlap,
        "active_role_snapshots": snapshots,
    }


class _InjectionWitness:
    def __init__(self, stage: str):
        self.stage = stage
        self.calls = 0
        self.exception_type = ""

    def fail(self) -> None:
        self.calls += 1
        try:
            raise RuntimeError(f"deterministic {self.stage} failure")
        except RuntimeError as exc:
            self.exception_type = type(exc).__name__
            raise


class _Cursor:
    def __init__(self, *, witness: _InjectionWitness | None = None):
        self.witness = witness

    def executemany(self, _sql: str, _rows: list[dict[str, Any]]) -> None:
        if self.witness is not None:
            self.witness.fail()


class _Connection:
    def __init__(self, *, witness: _InjectionWitness | None = None):
        self.witness = witness

    def cursor(self) -> _Context:
        return _Context(_Cursor(witness=self.witness))

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        if self.witness is not None:
            self.witness.fail()


class _Pool:
    def __init__(self, *, witness: _InjectionWitness | None = None):
        self.witness = witness

    def connection(self, *_args: Any, **_kwargs: Any) -> _Context:
        return _Context(_Connection(witness=self.witness))


class _StoppedThread:
    def start(self) -> None:
        return None

    def join(self, _timeout: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


def _result_digest(result: Any) -> str:
    return hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class _DeterministicProviderTransport:
    """Return one retryable 503 then one provider-shaped success per call."""

    def __init__(self) -> None:
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.requests % 2:
            return httpx.Response(503, json={"error": {"message": "retry"}}, request=request)
        path = request.url.path
        if path.endswith("/messages"):
            body = {
                "id": "msg_deterministic",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 4, "output_tokens": 1},
            }
        elif ":generateContent" in path:
            body = {
                "responseId": "gemini_deterministic",
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1},
            }
        else:
            body = {
                "id": "chatcmpl_deterministic",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            }
        return httpx.Response(200, json=body, headers={"x-request-id": "deterministic"}, request=request)


def _provider_config(pc: Any, accounting: Any, provider: str, call_index: int) -> Any:
    if provider == "openrouter":
        model = "openai/gpt-4.1-mini"
    elif provider == "anthropic":
        model = "claude-sonnet-4-20250514"
    else:
        provider = "gemini"
        model = "gemini-2.5-flash"
    context = accounting.ProviderAttemptContext(
        user_id="load-proof-user",
        lane=accounting.AttemptLane.CHAT,
        job_id=call_index,
        call_id=f"load-proof-{provider}-{call_index}",
    )
    return pc.ProviderConfig(
        provider, model, "deterministic-key", provider_attempt_context=context
    )


def _run_provider_path(
    *,
    pc: Any,
    accounting: Any,
    provider: str,
    samples: int,
    warmups: int,
    record: Callable[[Any], None],
) -> dict[str, Any]:
    transport = _DeterministicProviderTransport()
    old_client = pc._shared_client
    old_record = pc.record_provider_attempt
    pc._shared_client = httpx.Client(transport=httpx.MockTransport(transport))
    pc.record_provider_attempt = record
    latencies: list[float] = []
    result_digests: list[str] = []
    exception_fingerprints: list[str | None] = []
    attempts: list[int] = []
    retries: list[int] = []
    business_errors = 0
    try:
        total = warmups + samples
        for index in range(total):
            before = transport.requests
            started = time.perf_counter_ns()
            try:
                result = pc.reliable_chat_completion(
                    _provider_config(pc, accounting, provider, index),
                    [{"role": "user", "content": "deterministic load proof"}],
                    max_attempts=2,
                    base_delay_sec=0,
                    max_delay_sec=0,
                )
                exception = None
            except BaseException as exc:  # noqa: BLE001 - evidence records leakage
                result = None
                exception = f"{type(exc).__name__}:{exc}"
                business_errors += 1
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            actual_attempts = transport.requests - before
            if index >= warmups:
                latencies.append(round(elapsed, 6))
                result_digests.append(_result_digest(result))
                exception_fingerprints.append(exception)
                attempts.append(actual_attempts)
                retries.append(max(0, actual_attempts - 1))
    finally:
        pc.record_provider_attempt = old_record
        pc._shared_client.close()
        pc._shared_client = old_client
    return {
        "raw_latency_ms": latencies,
        "result_digests": result_digests,
        "exception_fingerprints": exception_fingerprints,
        "http_attempts": attempts,
        "retries": retries,
        "business_errors": business_errors,
    }


def _run_interleaved_provider_paths(
    *,
    pc: Any,
    accounting: Any,
    provider: str,
    samples: int,
    warmups: int,
    records: dict[str, Callable[[Any], None]],
    scenario_order: tuple[str, ...] = (
        "baseline",
        "queue_saturation",
        "recorder_failures",
    ),
    before_call: Callable[[str, int], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Run control/candidates adjacent, reversing order every sample."""
    transports = {
        scenario: _DeterministicProviderTransport() for scenario in records
    }
    clients = {
        scenario: httpx.Client(transport=httpx.MockTransport(transports[scenario]))
        for scenario in records
    }
    paths = {
        scenario: {
            "raw_latency_ms": [],
            "started_ns": [],
            "finished_ns": [],
            "result_digests": [],
            "exception_fingerprints": [],
            "http_attempts": [],
            "retries": [],
            "business_errors": 0,
        }
        for scenario in records
    }
    execution_order: list[dict[str, Any]] = []
    old_client = pc._shared_client
    old_record = pc.record_provider_attempt
    try:
        for sample in range(-warmups, samples):
            order = scenario_order if sample % 2 == 0 else tuple(reversed(scenario_order))
            for scenario in order:
                if before_call is not None:
                    before_call(scenario, sample)
                transport = transports[scenario]
                pc._shared_client = clients[scenario]
                pc.record_provider_attempt = records[scenario]
                before = transport.requests
                started = time.perf_counter_ns()
                try:
                    result = pc.reliable_chat_completion(
                        _provider_config(
                            pc,
                            accounting,
                            provider,
                            max(0, sample) * 10 + list(PROVIDERS).index(provider),
                        ),
                        [{"role": "user", "content": "deterministic load proof"}],
                        max_attempts=2,
                        base_delay_sec=0,
                        max_delay_sec=0,
                    )
                    exception = None
                except BaseException as exc:  # noqa: BLE001 - evidence captures leak
                    result = None
                    exception = f"{type(exc).__name__}:{exc}"
                finished = time.perf_counter_ns()
                elapsed = (finished - started) / 1_000_000
                actual_attempts = transport.requests - before
                if sample < 0:
                    continue
                path = paths[scenario]
                path["raw_latency_ms"].append(round(elapsed, 6))
                path["started_ns"].append(started)
                path["finished_ns"].append(finished)
                path["result_digests"].append(_result_digest(result))
                path["exception_fingerprints"].append(exception)
                path["http_attempts"].append(actual_attempts)
                path["retries"].append(max(0, actual_attempts - 1))
                if exception is not None:
                    path["business_errors"] += 1
                execution_order.append(
                    {"provider": provider, "sample": sample, "scenario": scenario}
                )
    finally:
        pc.record_provider_attempt = old_record
        pc._shared_client = old_client
        for client in clients.values():
            client.close()
    return paths, execution_order


def _paired_deltas(baseline: dict[str, Any], scenario: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for provider in PROVIDERS:
        values.extend(
            round(candidate - control, 6)
            for control, candidate in zip(
                baseline[provider]["raw_latency_ms"],
                scenario[provider]["raw_latency_ms"],
                strict=True,
            )
        )
    return values


def _recorder_failure_fanout(accounting: Any) -> tuple[Callable[[Any], None], dict[str, Any], Callable[[], None]]:
    capacity = 4096
    startup_witness = _InjectionWitness("thread_factory")
    startup = accounting.ProviderAttemptRecorder(
        queue_capacity=capacity,
        thread_factory=lambda **_kwargs: startup_witness.fail(),
    )
    startup_before = {
        "queue_size_before": startup.queue_size,
        "drop_before": startup.dropped_count,
    }
    startup_started = startup.start()
    pool_witness = _InjectionWitness("pool_factory")
    pool = accounting.ProviderAttemptRecorder(
        queue_capacity=capacity,
        batch_size=1,
        max_retries=0,
        flush_interval=0.001,
        pool_factory=pool_witness.fail,
    )
    sql_witness = _InjectionWitness("cursor_executemany")
    sql = accounting.ProviderAttemptRecorder(
        queue_capacity=capacity,
        batch_size=1,
        max_retries=0,
        flush_interval=0.001,
        pool_factory=lambda: _Pool(witness=sql_witness),
    )
    serialization = accounting.ProviderAttemptRecorder(
        queue_capacity=capacity,
        batch_size=1,
        flush_interval=0.001,
        pool_factory=lambda: _Pool(),
    )
    mode_before = {
        "pool": (pool.queue_size, pool.dropped_count),
        "sql": (sql.queue_size, sql.dropped_count),
        "serialization": (serialization.queue_size, serialization.dropped_count),
    }
    for recorder in (pool, sql, serialization):
        recorder.start()
    serialization._queue.put_nowait(object())
    _wait_for(
        lambda: serialization.queue_size == 0
        and serialization.dropped_count - mode_before["serialization"][1] == 1
    )

    def fanout(event: Any) -> None:
        for recorder in (startup, pool, sql, serialization):
            recorder.record(event)

    state: dict[str, Any] = {"startup_started": startup_started}

    def finish() -> None:
        _wait_for(lambda: pool.queue_size == 0 and sql.queue_size == 0)
        startup_drop_after = startup.dropped_count
        state["failure_modes"] = {
            "startup": {
                **startup_before,
                "stage": "thread_factory",
                "exception_type": startup_witness.exception_type,
                "injection_calls": startup_witness.calls,
                "drop_after": startup_drop_after,
                "drop_delta": startup_drop_after - startup_before["drop_before"],
                "queue_size_after": startup.queue_size,
                "queue_capacity": capacity,
                "queue_full_drops": 0,
                "start_returned": startup_started,
            },
            "pool": _recorder_mode_evidence(
                pool, mode_before["pool"], pool_witness, "pool_factory", capacity
            ),
            "sql": _recorder_mode_evidence(
                sql, mode_before["sql"], sql_witness, "cursor_executemany", capacity
            ),
            "serialization": {
                "stage": "event_type_check",
                "exception_type": "TypeError",
                "injected_items": 1,
                "consumed_items": 1 if serialization.queue_size == 0 else 0,
                "queue_size_before": mode_before["serialization"][0],
                "queue_size_after": serialization.queue_size,
                "queue_capacity": capacity,
                "drop_before": mode_before["serialization"][1],
                "drop_after": serialization.dropped_count,
                "drop_delta": serialization.dropped_count
                - mode_before["serialization"][1],
                "queue_full_drops": 0,
            },
        }
        for recorder in (startup, pool, sql, serialization):
            recorder.shutdown(timeout=1)

    return fanout, state, finish


def _recorder_mode_evidence(
    recorder: Any,
    before: tuple[int, int],
    witness: _InjectionWitness,
    stage: str,
    capacity: int,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "exception_type": witness.exception_type,
        "injection_calls": witness.calls,
        "queue_size_before": before[0],
        "queue_size_after": recorder.queue_size,
        "queue_capacity": capacity,
        "drop_before": before[1],
        "drop_after": recorder.dropped_count,
        "drop_delta": recorder.dropped_count - before[1],
        "queue_full_drops": 0,
    }


def _git_commit(repo: Path) -> str:
    value = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    if len(value) != 40:
        raise RuntimeError("full Git commit unavailable")
    return value


def _assert_pool_probe_empty(conn: Any) -> None:
    counts = conn.execute(
        "SELECT "
        "(SELECT count(*) FROM v2_usage_rollup_watermarks),"
        "(SELECT count(*) FROM llm_usage_rollup_watermarks),"
        "(SELECT count(*) FROM llm_usage_rollup_dirty_days)"
    ).fetchone()
    if counts is None or any(int(value) for value in counts):
        raise RuntimeError(
            f"dedicated pool-proof rollup state is not empty: {counts}"
        )


def measure_pool_contention_evidence(
    *,
    real_pool: Any,
    db_module: Any,
    jobs_store: Any,
    provider_attempt_rollup: Any,
    usage_query: Any,
    samples: int = MIN_SAMPLES_PER_PROVIDER,
    preserve_existing_rollup_state: bool = False,
) -> dict[str, Any]:
    """Run recorder, attempt maintenance and the bounded report concurrently.

    The caller supplies the already validated dedicated local PostgreSQL pool.
    Only the two known rollup watermarks and twenty synthetic dirty days are
    seeded.  The dirty days force real rebuild work while calls are sampled;
    everything is removed in ``finally`` before the scale fixture is installed.
    """
    repo = Path(__file__).resolve().parents[2]
    backend = str(repo / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import provider_attempt_accounting as accounting  # noqa: PLC0415
    import provider_client as pc  # noqa: PLC0415

    tracker = _TrackedPool(real_pool)
    usage_report_total_deadline_ms = int(
        jobs_store._USAGE_REPORT_STATEMENT_TIMEOUT_MS
    )
    attempt_subsection_timeout_ms = int(
        jobs_store._RUNTIME_ATTEMPT_USAGE_STATEMENT_TIMEOUT_MS
    )
    if (
        usage_report_total_deadline_ms != USAGE_REPORT_TOTAL_DEADLINE_MS
        or attempt_subsection_timeout_ms != ATTEMPT_SUBSECTION_TIMEOUT_MS
    ):
        raise RuntimeError("production usage timeout contract changed")
    dirty_day = datetime.now(timezone.utc).date()
    recorder = accounting.ProviderAttemptRecorder(
        queue_capacity=128,
        batch_size=8,
        max_retries=0,
        flush_interval=0.001,
        reconcile_interval=3600,
        pool_factory=lambda: tracker,
    )
    start_gate = threading.Barrier(3)
    outcomes: dict[str, Any] = {}
    errors: list[str] = []
    provider_done = threading.Event()
    old_get_pool = db_module.get_pool

    def provider_work() -> None:
        try:
            start_gate.wait(timeout=2)
            if not tracker.wait_for_active_roles(
                {"usage_exporter", "attempt_rollup_outer"}, timeout=2
            ):
                raise RuntimeError("real DB contention roles did not become active")
            paths = {"baseline": {}, "pool_contention": {}}
            execution_order = []
            coverage_targets = iter(
                (
                    "attempt_rollup_rebuild",
                    "usage_importer_core",
                    "usage_importer_attempt",
                    "provider_recorder",
                )
            )

            def await_real_db_overlap(scenario: str, sample: int) -> None:
                if scenario != "pool_contention" or sample < 0:
                    return
                target = next(coverage_targets, "usage_exporter")
                if not tracker.wait_for_active_roles({target}, timeout=2):
                    raise RuntimeError(
                        f"real DB overlap role did not become active: {target}"
                    )

            for provider in PROVIDERS:
                provider_paths, provider_order = _run_interleaved_provider_paths(
                    pc=pc,
                    accounting=accounting,
                    provider=provider,
                    samples=samples,
                    warmups=1,
                    records={
                        "baseline": lambda _event: None,
                        "pool_contention": recorder.record,
                    },
                    scenario_order=("baseline", "pool_contention"),
                    before_call=await_real_db_overlap,
                )
                for scenario, path in provider_paths.items():
                    paths[scenario][provider] = path
                execution_order.extend(provider_order)
            deltas = _paired_deltas(paths["baseline"], paths["pool_contention"])
            outcomes["provider"] = {
                "samples_per_provider": samples,
                "paths": paths,
                "execution_order": execution_order,
                "paired_latency_delta_ms": deltas,
                "paired_p95_regression_ms": round(_nearest_rank(deltas, 0.95), 6),
            }
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"provider:{type(exc).__name__}")
        finally:
            provider_done.set()

    def maintenance_work() -> None:
        try:
            start_gate.wait(timeout=2)
            ticks = []
            refreshed_days: list[str] = []
            while len(refreshed_days) < SEEDED_DIRTY_DAYS:
                tick_index = len(ticks)
                started_ns = time.perf_counter_ns()
                with tracker.operation(
                    "attempt_rollup", maintenance_tick_index=tick_index
                ):
                    outcome = provider_attempt_rollup.run_maintenance_tick(
                            max_days=1,
                            max_changed_rows=1,
                            max_dirty_days=1,
                            max_stale_rows=0,
                            max_retention_rows=0,
                            statement_timeout_ms=MAINTENANCE_STATEMENT_TIMEOUT_MS,
                            pool_timeout_seconds=0.5,
                    )
                finished_ns = time.perf_counter_ns()
                outcome_json = _maintenance_outcome_json(outcome)
                ticks.append(
                    {
                        "tick_index": tick_index,
                        "started_ns": started_ns,
                        "finished_ns": finished_ns,
                        "outcome": outcome_json,
                    }
                )
                days = outcome_json.get("refreshed_days")
                if (
                    outcome_json.get("status") != "ok"
                    or not isinstance(days, list)
                    or not days
                ):
                    break
                refreshed_days.extend(days)
                if len(ticks) >= SEEDED_DIRTY_DAYS * 2:
                    break
            outcomes["maintenance"] = {
                "seeded_dirty_days": SEEDED_DIRTY_DAYS,
                "ticks": ticks,
                "refreshed_local_days": refreshed_days,
            }
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"maintenance:{type(exc).__name__}")

    def report_work() -> None:
        try:
            start_gate.wait(timeout=2)
            reports = []
            while not provider_done.is_set():
                with tracker.operation("usage_exporter"):
                    reports.append(jobs_store.usage_report_snapshot(usage_query))
            outcomes["report"] = reports
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"report:{type(exc).__name__}")

    threads = [
        threading.Thread(target=provider_work, name="load-proof-provider"),
        threading.Thread(target=maintenance_work, name="load-proof-maintenance"),
        threading.Thread(target=report_work, name="load-proof-usage-report"),
    ]
    with real_pool.connection() as conn:
        if not preserve_existing_rollup_state:
            _assert_pool_probe_empty(conn)
        with conn.transaction():
            if not preserve_existing_rollup_state:
                conn.execute(
                    "INSERT INTO v2_usage_rollup_watermarks "
                    "(rollup_name,bootstrap_complete,source_updated_at,source_id) "
                    "VALUES ('hosted_v2_usage',true,'epoch',0)"
                )
                conn.execute(
                    "INSERT INTO llm_usage_rollup_watermarks "
                    "(rollup_name,bootstrap_complete,completed_through_day) "
                    "VALUES ('hosted_v2_attempt_usage',true,%s)",
                    (dirty_day,),
                )
            conn.execute(
                "INSERT INTO llm_usage_rollup_dirty_days "
                "(rollup_name,local_day,reason) "
                "SELECT 'hosted_v2_attempt_usage',%s - day_offset,'load_proof' "
                "FROM generate_series(0,%s - 1) AS days(day_offset)",
                (dirty_day, SEEDED_DIRTY_DAYS),
            )
    try:
        db_module.get_pool = lambda: tracker
        old_observer = jobs_store._usage_snapshot_observer

        def observe_usage(event: str, **fields: Any) -> None:
            if event == "imported":
                reader = str(fields.get("role") or "")
                tracker.assign_current_role(
                    "usage_importer_attempt"
                    if "attempt" in reader
                    else "usage_importer_core"
                )

        jobs_store._usage_snapshot_observer = observe_usage
        recorder.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("pool proof thread exceeded bounded join")
        if errors:
            raise RuntimeError(f"pool proof production path failed: {errors}")
        _wait_for(lambda: recorder.queue_size == 0, timeout=2)
        # An empty queue does not mean the worker has released the connection for
        # the batch it just dequeued.  Join it before freezing interval evidence.
        recorder.shutdown(timeout=2)
        provider_paths = outcomes.get("provider") or {}
        maintenance = outcomes.get("maintenance") or {}
        with real_pool.connection() as conn:
            dirty_remaining = int(
                conn.execute(
                    "SELECT count(*) FROM llm_usage_rollup_dirty_days "
                    "WHERE rollup_name='hosted_v2_attempt_usage'"
                ).fetchone()[0]
            )
        maintenance["dirty_remaining_before_cleanup"] = dirty_remaining
        for provider in PROVIDERS:
            candidate = provider_paths["paths"]["pool_contention"][provider]
            candidate["overlapping_roles"] = [
                _overlapping_pool_roles(started, finished, tracker._raw)
                for started, finished in zip(
                    candidate["started_ns"],
                    candidate["finished_ns"],
                    strict=True,
                )
            ]
        evidence = tracker.evidence(provider_paths=provider_paths)
        evidence.update(
            {
                "usage_report_total_deadline_ms": usage_report_total_deadline_ms,
                "attempt_subsection_timeout_ms": attempt_subsection_timeout_ms,
                "maintenance_statement_timeout_ms": MAINTENANCE_STATEMENT_TIMEOUT_MS,
                "maintenance": maintenance,
            }
        )
        if not set(POOL_ROLES) <= set(evidence["roles"]):
            raise RuntimeError(
                f"pool proof did not observe every production role: {evidence['roles']}"
            )
        return evidence
    finally:
        recorder.shutdown(timeout=2)
        if "old_observer" in locals():
            jobs_store._usage_snapshot_observer = old_observer
        db_module.get_pool = old_get_pool
        with real_pool.connection() as conn:
            conn.execute(
                "DELETE FROM llm_usage_rollup_dirty_days "
                "WHERE rollup_name='hosted_v2_attempt_usage' "
                "AND reason='load_proof'"
            )
            if not preserve_existing_rollup_state:
                conn.execute(
                    "DELETE FROM llm_usage_rollup_watermarks "
                    "WHERE rollup_name='hosted_v2_attempt_usage'"
                )
                conn.execute(
                    "DELETE FROM v2_usage_rollup_watermarks "
                    "WHERE rollup_name='hosted_v2_usage'"
                )


def produce_business_path_evidence(
    *,
    samples_per_provider: int = DEFAULT_SAMPLES_PER_PROVIDER,
    warmups_per_provider: int = DEFAULT_WARMUPS_PER_PROVIDER,
    pool_evidence: dict[str, Any] | None = None,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Run deterministic production dispatch paths and return a sealed artifact."""
    if samples_per_provider < MIN_SAMPLES_PER_PROVIDER:
        raise ValueError(f"samples_per_provider must be >= {MIN_SAMPLES_PER_PROVIDER}")
    if pool_evidence is None:
        raise ValueError("measured pool evidence is required")
    repo = repo or Path(__file__).resolve().parents[2]
    backend = str(repo / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import provider_attempt_accounting as accounting  # noqa: PLC0415
    import provider_client as pc  # noqa: PLC0415

    saturation_recorder = accounting.ProviderAttemptRecorder(
        queue_capacity=1, thread_factory=lambda **_kwargs: _StoppedThread()
    )
    failure_record, failure_state, finish_failures = _recorder_failure_fanout(accounting)
    all_paths = {name: {} for name in ("baseline", "queue_saturation", "recorder_failures")}
    execution_order: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        paths, order = _run_interleaved_provider_paths(
            pc=pc,
            accounting=accounting,
            provider=provider,
            samples=samples_per_provider,
            warmups=warmups_per_provider,
            records={
                "baseline": lambda _event: None,
                "queue_saturation": saturation_recorder.record,
                "recorder_failures": failure_record,
            },
        )
        for scenario, path in paths.items():
            all_paths[scenario][provider] = path
        execution_order.extend(order)
    finish_failures()
    baseline = all_paths["baseline"]
    saturated = all_paths["queue_saturation"]
    failed = all_paths["recorder_failures"]
    saturation_deltas = _paired_deltas(baseline, saturated)
    failure_deltas = _paired_deltas(baseline, failed)
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "run_id": str(uuid4()),
        "git_commit": _git_commit(repo),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "samples_per_provider": samples_per_provider,
            "warmups_per_provider": warmups_per_provider,
            "hot_path_paired_p95_budget_ms": HOT_PATH_PAIRED_P95_BUDGET_MS,
            "providers": list(PROVIDERS),
            "pairing_order": "per-provider/per-index/alternating-direction",
        },
        "execution_order": execution_order,
        "scenarios": {
            "baseline": {"providers": baseline},
            "queue_saturation": {
                "providers": saturated,
                "queue_capacity": saturation_recorder._queue.maxsize,
                "max_queue_size": saturation_recorder.queue_size,
                "dropped_count": saturation_recorder.dropped_count,
                "paired_latency_delta_ms": saturation_deltas,
                "paired_p95_regression_ms": round(_nearest_rank(saturation_deltas, 0.95), 6),
            },
            "recorder_failures": {
                "providers": failed,
                "paired_latency_delta_ms": failure_deltas,
                "paired_p95_regression_ms": round(_nearest_rank(failure_deltas, 0.95), 6),
                "failure_modes": failure_state["failure_modes"],
            },
        },
        "pool": pool_evidence,
    }
    artifact["canonical_sha256"] = canonical_digest(artifact)
    return validate_business_path_evidence(
        artifact, expected_commit=artifact["git_commit"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--samples-per-provider", type=int, default=DEFAULT_SAMPLES_PER_PROVIDER)
    parser.add_argument("--warmups-per-provider", type=int, default=DEFAULT_WARMUPS_PER_PROVIDER)
    args = parser.parse_args()
    identity = conninfo_to_dict(args.database_url)
    if identity.get("host") != "127.0.0.1" or identity.get("port") != "55432":
        raise SystemExit("pool proof requires explicit 127.0.0.1:55432 test PostgreSQL")
    if identity.get("dbname") in {None, "", "postgres", "template0", "template1"}:
        raise SystemExit("pool proof requires a named dedicated test database")
    os.environ["DATABASE_URL"] = args.database_url
    repo = Path(__file__).resolve().parents[2]
    backend = str(repo / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import db  # noqa: PLC0415
    from admin.usage import UsageQuery  # noqa: PLC0415
    from model_api_runtime.v2 import jobs_store, provider_attempt_rollup  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    pool_evidence = measure_pool_contention_evidence(
        real_pool=db.get_pool(),
        db_module=db,
        jobs_store=jobs_store,
        provider_attempt_rollup=provider_attempt_rollup,
        usage_query=UsageQuery(
            start_at_utc=(now - timedelta(days=2)).replace(microsecond=0),
            end_at_utc=now.replace(microsecond=0),
            timezone="Asia/Shanghai",
            preset="custom",
        ),
    )
    artifact = produce_business_path_evidence(
        samples_per_provider=args.samples_per_provider,
        warmups_per_provider=args.warmups_per_provider,
        pool_evidence=pool_evidence,
        repo=repo,
    )
    rendered = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
