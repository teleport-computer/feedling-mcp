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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

import httpx
from psycopg.conninfo import conninfo_to_dict


SCHEMA_VERSION = 1
PRODUCER = "scripts/perf/provider_attempt_business_path.py"
PROVIDERS = ("openrouter", "anthropic", "google")
FAILURE_MODES = ("startup", "pool", "sql", "serialization")
MIN_SAMPLES_PER_PROVIDER = 20
DEFAULT_SAMPLES_PER_PROVIDER = 40
DEFAULT_WARMUPS_PER_PROVIDER = 3
HOT_PATH_PAIRED_P95_BUDGET_MS = 5.0
REPORT_STATEMENT_TIMEOUT_MS = 3_000


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "canonical_sha256"}
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


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
        _require(mode.get("observed") is True and isinstance(mode.get("dropped_count"), int) and mode["dropped_count"] > 0, f"recorder {name} failure not observed")

    pool = evidence.get("pool")
    _require(isinstance(pool, dict), "pool evidence missing")
    _require(pool.get("measurement") == "production_concurrent_paths", "pool measurement provenance missing")
    _require(isinstance(pool.get("capacity"), int) and pool["capacity"] >= 2, "pool capacity invalid")
    _require(isinstance(pool.get("peak_occupancy"), int) and 2 <= pool["peak_occupancy"] <= pool["capacity"], "pool peak invalid")
    _require(pool.get("timeouts") == 0, "pool timeout observed")
    _require(
        set(pool.get("operations") or ())
        == {"provider_recorder", "attempt_rollup_maintenance", "usage_report"},
        "pool operations incomplete",
    )
    _require(pool.get("provider_results_match_baseline") is True, "pool contention changed provider results")
    _require(pool.get("report_statement_timeout_ms") == REPORT_STATEMENT_TIMEOUT_MS, "report timeout budget changed")
    _require(pool.get("maintenance_second_connection_observed") is True, "maintenance second connection not observed")
    acquisitions = pool.get("raw_acquisitions")
    _require(isinstance(acquisitions, list) and acquisitions, "pool raw acquisitions missing")
    _require(
        all(
            isinstance(item, dict)
            and item.get("operation") in pool["operations"]
            and isinstance(item.get("occupancy"), int)
            and 1 <= item["occupancy"] <= pool["capacity"]
            and isinstance(item.get("wait_ms"), (int, float))
            and item["wait_ms"] >= 0
            for item in acquisitions
        ),
        "pool raw acquisition invalid",
    )
    _require(
        set(item["operation"] for item in acquisitions) == set(pool["operations"]),
        "pool raw operations incomplete",
    )
    return evidence


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
        self._operation = ""

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
        self._operation = self._tracker.current_operation()
        with self._tracker._lock:
            self._tracker._occupancy += 1
            self._tracker._peak = max(self._tracker._peak, self._tracker._occupancy)
            active = self._tracker._active_by_operation.get(self._operation, 0) + 1
            self._tracker._active_by_operation[self._operation] = active
            if self._operation == "attempt_rollup_maintenance" and active >= 2:
                self._tracker._maintenance_second = True
            self._tracker._raw.append(
                {
                    "operation": self._operation,
                    "occupancy": self._tracker._occupancy,
                    "wait_ms": round(wait_ms, 6),
                }
            )
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        with self._tracker._lock:
            self._tracker._occupancy -= 1
            self._tracker._active_by_operation[self._operation] -= 1
        return bool(self._inner.__exit__(exc_type, exc, tb))


class _TrackedPool:
    """Transparent pool facade with auditable per-acquisition samples."""

    def __init__(self, pool: Any):
        self._pool = pool
        self._local = threading.local()
        self._lock = threading.Lock()
        self._occupancy = 0
        self._peak = 0
        self._timeouts = 0
        self._active_by_operation: dict[str, int] = {}
        self._maintenance_second = False
        self._raw: list[dict[str, Any]] = []

    @property
    def max_size(self) -> int:
        return int(self._pool.max_size)

    def current_operation(self) -> str:
        explicit = getattr(self._local, "operation", "")
        if explicit:
            return str(explicit)
        name = threading.current_thread().name.lower()
        if "provider-attempt-recorder" in name:
            return "provider_recorder"
        if "usage" in name or "importer" in name:
            return "usage_report"
        return "unknown"

    @contextmanager
    def operation(self, name: str):
        previous = getattr(self._local, "operation", "")
        self._local.operation = name
        try:
            yield self
        finally:
            self._local.operation = previous

    def connection(self, **kwargs: Any) -> _TrackedConnectionContext:
        return _TrackedConnectionContext(self, kwargs)

    def evidence(self, *, provider_results_match_baseline: bool) -> dict[str, Any]:
        with self._lock:
            return {
                "measurement": "production_concurrent_paths",
                "capacity": self.max_size,
                "peak_occupancy": self._peak,
                "timeouts": self._timeouts,
                "operations": sorted({item["operation"] for item in self._raw}),
                "provider_results_match_baseline": provider_results_match_baseline,
                "report_statement_timeout_ms": REPORT_STATEMENT_TIMEOUT_MS,
                "maintenance_second_connection_observed": self._maintenance_second,
                "raw_acquisitions": list(self._raw),
            }


class _Cursor:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def executemany(self, _sql: str, _rows: list[dict[str, Any]]) -> None:
        if self.fail:
            raise RuntimeError("deterministic SQL failure")


class _Connection:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def cursor(self) -> _Context:
        return _Context(_Cursor(fail=self.fail))

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("deterministic SQL failure")


class _Pool:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def connection(self, *_args: Any, **_kwargs: Any) -> _Context:
        return _Context(_Connection(fail=self.fail))


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
            order = (
                ("baseline", "queue_saturation", "recorder_failures")
                if sample % 2 == 0
                else ("recorder_failures", "queue_saturation", "baseline")
            )
            for scenario in order:
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
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                actual_attempts = transport.requests - before
                if sample < 0:
                    continue
                path = paths[scenario]
                path["raw_latency_ms"].append(round(elapsed, 6))
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
    startup = accounting.ProviderAttemptRecorder(
        queue_capacity=1,
        thread_factory=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("startup")),
    )
    startup_started = startup.start()
    pool = accounting.ProviderAttemptRecorder(
        queue_capacity=16,
        batch_size=1,
        max_retries=0,
        flush_interval=0.001,
        pool_factory=lambda: (_ for _ in ()).throw(RuntimeError("pool")),
    )
    sql = accounting.ProviderAttemptRecorder(
        queue_capacity=16,
        batch_size=1,
        max_retries=0,
        flush_interval=0.001,
        pool_factory=lambda: _Pool(fail=True),
    )
    serialization = accounting.ProviderAttemptRecorder(
        queue_capacity=16,
        batch_size=1,
        flush_interval=0.001,
        pool_factory=lambda: _Pool(),
    )
    for recorder in (pool, sql, serialization):
        recorder.start()
    serialization._queue.put_nowait(object())

    def fanout(event: Any) -> None:
        for recorder in (startup, pool, sql, serialization):
            recorder.record(event)

    state: dict[str, Any] = {"startup_started": startup_started}

    def finish() -> None:
        _wait_for(lambda: pool.dropped_count > 0 and sql.dropped_count > 0 and serialization.dropped_count > 0)
        state["failure_modes"] = {
            "startup": {"observed": not startup_started, "dropped_count": startup.dropped_count},
            "pool": {"observed": pool.dropped_count > 0, "dropped_count": pool.dropped_count},
            "sql": {"observed": sql.dropped_count > 0, "dropped_count": sql.dropped_count},
            "serialization": {"observed": serialization.dropped_count > 0, "dropped_count": serialization.dropped_count},
        }
        for recorder in (startup, pool, sql, serialization):
            recorder.shutdown(timeout=1)

    return fanout, state, finish


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
) -> dict[str, Any]:
    """Run recorder, attempt maintenance and the 3-second report concurrently.

    The caller supplies the already validated dedicated local PostgreSQL pool.
    Only the two known rollup watermarks and one synthetic dirty day are seeded;
    they are removed in ``finally`` before the scale fixture is installed.
    """
    repo = Path(__file__).resolve().parents[2]
    backend = str(repo / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import provider_attempt_accounting as accounting  # noqa: PLC0415
    import provider_client as pc  # noqa: PLC0415

    tracker = _TrackedPool(real_pool)
    dirty_day = datetime.now(timezone.utc).date()
    baseline = _run_provider_path(
        pc=pc,
        accounting=accounting,
        provider="openrouter",
        samples=samples,
        warmups=1,
        record=lambda _event: None,
    )
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
    old_get_pool = db_module.get_pool

    def provider_work() -> None:
        try:
            start_gate.wait(timeout=2)
            outcomes["provider"] = _run_provider_path(
                pc=pc,
                accounting=accounting,
                provider="openrouter",
                samples=samples,
                warmups=1,
                record=recorder.record,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"provider:{type(exc).__name__}")

    def maintenance_work() -> None:
        try:
            start_gate.wait(timeout=2)
            with tracker.operation("attempt_rollup_maintenance"):
                outcomes["maintenance"] = provider_attempt_rollup.run_maintenance_tick(
                    max_days=1,
                    max_changed_rows=1,
                    max_dirty_days=1,
                    max_stale_rows=0,
                    max_retention_rows=0,
                    statement_timeout_ms=REPORT_STATEMENT_TIMEOUT_MS,
                    pool_timeout_seconds=0.5,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"maintenance:{type(exc).__name__}")

    def report_work() -> None:
        try:
            start_gate.wait(timeout=2)
            with tracker.operation("usage_report"):
                outcomes["report"] = jobs_store.usage_report_snapshot(usage_query)
        except BaseException as exc:  # noqa: BLE001
            errors.append(f"report:{type(exc).__name__}")

    threads = [
        threading.Thread(target=provider_work, name="load-proof-provider"),
        threading.Thread(target=maintenance_work, name="load-proof-maintenance"),
        threading.Thread(target=report_work, name="load-proof-usage-report"),
    ]
    with real_pool.connection() as conn:
        _assert_pool_probe_empty(conn)
        with conn.transaction():
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
                "(rollup_name,local_day,reason) VALUES "
                "('hosted_v2_attempt_usage',%s,'load_proof')",
                (dirty_day,),
            )
    try:
        db_module.get_pool = lambda: tracker
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
        candidate = outcomes.get("provider") or {}
        comparable_keys = (
            "result_digests",
            "exception_fingerprints",
            "http_attempts",
            "retries",
            "business_errors",
        )
        results_match = all(
            candidate.get(key) == baseline.get(key) for key in comparable_keys
        )
        evidence = tracker.evidence(
            provider_results_match_baseline=results_match
        )
        if set(evidence["operations"]) != {
            "provider_recorder",
            "attempt_rollup_maintenance",
            "usage_report",
        }:
            raise RuntimeError(
                f"pool proof did not observe every production path: {evidence['operations']}"
            )
        return evidence
    finally:
        recorder.shutdown(timeout=2)
        db_module.get_pool = old_get_pool
        with real_pool.connection() as conn:
            conn.execute(
                "DELETE FROM llm_usage_rollup_dirty_days "
                "WHERE rollup_name='hosted_v2_attempt_usage'"
            )
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
