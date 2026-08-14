"""Hosted Runtime V2 D0 Task 4 — GET /v1/admin/v2-metrics.

Admin-token-gated JSON endpoint that surfaces jobs_store's queue-depth/worker-
liveness/service-time/token-throughput counters, which D4 load-testing
consumes. Mirrors the admin-token gate + route style of
test_admin_runtime_mode.py, but the five jobs_store functions
admin_core.v2_metrics composes are monkeypatched directly rather than
requiring seeded rows — admin_core.v2_metrics is a thin composition with no
logic of its own to exercise against real data here (jobs_store's own
functions already have coverage in test_v2_jobs_store.py/test_v2_turn_metrics.py).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import routes_asgi as admin_asgi  # noqa: E402
from admin import admin_core  # noqa: E402
from asgi import middleware  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

ADMIN_TOKEN = "admin-test-token"


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    admin_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()

_HEARTBEATS = [
    {
        "worker_id": "worker:foreground",
        "kind": "turn",
        "capacity": 4,
        "pool": "foreground",
        "runtime_state": {
            "slots": {"configured": 4, "healthy": 4, "busy": 2, "restarting": 0},
            "enclave": {
                "limit": 4,
                "granted": {"foreground": 2, "wake": 1, "heavy": 1},
                "waiting": {"foreground": 0, "wake": 0, "heavy": 2},
                "wait_p95_ms": {"foreground": 5.0, "wake": 12.0, "heavy": 300.0},
            },
            "db_pools": {
                "parent": {"max": 8, "used": 3, "waiting": 0, "timeouts": 0},
                "slot": {"processes": 8, "max_each": 2, "used": 7, "waiting": 0, "timeouts": 0},
            },
            "isolation_events": {
                "watchdog_kills": {"heavy:profile:stall": 1},
                "stale_owner_rejections": 2,
                "preemption_exit_p95_ms": 140.0,
                "watchdog_release_p95_ms": 250.0,
                "admission_rejects": {"no_foreground_capacity": 0, "over_sla": 1, "control_halted": 0},
            },
            "profile_runtime": {
                "card_count_max": 554,
                "batch_count": 9,
                "provider_calls_max": 3,
                "stage_p95_ms": {"fetch_batch": 410.0, "provider": 42000.0},
            },
        },
        "beat_at_epoch": 1234.0,
        "age_sec": 2.0,
    },
    {
        "worker_id": "worker:wake",
        "kind": "turn",
        "capacity": 2,
        "pool": "wake",
        "runtime_state": {"slots": {"configured": 2, "healthy": 2, "busy": 1, "restarting": 0}},
        "beat_at_epoch": 1233.0,
        "age_sec": 3.0,
    },
    {
        "worker_id": "worker:heavy",
        "kind": "turn",
        "capacity": 2,
        "pool": "heavy",
        "runtime_state": {"slots": {"configured": 2, "healthy": 2, "busy": 1, "restarting": 0}},
        "beat_at_epoch": 1232.0,
        "age_sec": 4.0,
    },
]


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(jobs_store, "inflight_job_count", lambda: 3)
    monkeypatch.setattr(jobs_store, "pending_job_count", lambda: 1)
    monkeypatch.setattr(jobs_store, "live_worker_count", lambda **kw: 2)
    monkeypatch.setattr(jobs_store, "live_worker_capacity", lambda **kw: 8)
    monkeypatch.setattr(jobs_store, "recent_worker_heartbeats", lambda **kw: _HEARTBEATS)
    monkeypatch.setattr(jobs_store, "recent_worker_heartbeat_count", lambda **kw: 3)
    monkeypatch.setattr(
        jobs_store,
        "pool_queue_metrics",
        lambda: {
            "foreground": {"pending": 1, "oldest_pending_sec": 3.5, "claim_p95_ms": 80.0},
            "wake": {"pending": 0, "oldest_pending_sec": None, "claim_p95_ms": 120.0},
            "heavy": {"pending": 2, "oldest_pending_sec": 40.0, "claim_p95_ms": 900.0},
        },
    )
    monkeypatch.setattr(
        jobs_store,
        "job_counts_by_lane",
        lambda: {"chat": {"pending": 1, "active": 2}, "profile": {"pending": 0, "active": 1}},
    )
    monkeypatch.setattr(
        jobs_store,
        "recent_preemption_counts",
        lambda **kw: {"profile:terminal": 1, "scheduled:requeued": 2},
    )
    monkeypatch.setattr(
        jobs_store,
        "recent_watchdog_recovery_counts",
        lambda **kw: {"chat:terminal": 1, "profile:requeued": 1},
    )
    monkeypatch.setattr(jobs_store, "recent_mean_service_sec", lambda **kw: 4.5)
    monkeypatch.setattr(jobs_store, "recent_mean_tokens_per_turn", lambda **kw: 123.0)
    monkeypatch.setattr(
        jobs_store,
        "recent_chat_operational_health",
        lambda **kw: {
            "window_hours": 24,
            "sample_limit": 1000,
            "jobs": {
                "sampled_terminal_jobs": 10,
                "completed": 8,
                "failed": 1,
                "expired": 1,
                "queue_expired": 1,
                "lease_expired": 0,
                "superseded": 0,
                "failure_rate": 0.1,
                "expiry_rate": 0.1,
                "error_or_expiry_rate": 0.2,
                "pending": 1,
                "oldest_pending_age_sec": 12.5,
            },
            "latency": {"sampled_turns": 10, "p95_ms": 4200.0},
            "trajectory": {
                "sampled_jobs": 12,
                "complete": 10,
                "partial": 1,
                "missing": 0,
                "open": 1,
                "capture_gap": 1,
                "complete_rate": 10 / 12,
            },
        },
    )
    monkeypatch.setattr(
        jobs_store,
        "recent_runtime_health",
        lambda **kw: {
            "window_hours": 24,
            "pending": 1,
            "oldest_pending_age_sec": 12.5,
            "lanes": [
                {
                    "lane": "capture",
                    "sampled_jobs": 3,
                    "completed": 2,
                    "failed": 1,
                    "expired": 0,
                    "superseded": 0,
                    "failure_rate": 1 / 3,
                },
                {
                    "lane": "dream",
                    "sampled_jobs": 1,
                    "completed": 1,
                    "failed": 0,
                    "expired": 0,
                    "superseded": 0,
                    "failure_rate": 0.0,
                },
            ],
        },
    )
    monkeypatch.setattr(
        jobs_store,
        "recent_prompt_cache_stats",
        lambda **kw: {
            "sampled_turns": 4,
            "model_calls": 5,
            "usage_reported_calls": 5,
            "cache_reported_calls": 4,
            "usage_telemetry_coverage": 1.0,
            "cache_telemetry_coverage": 0.8,
            "route_identity_coverage": 1.0,
            "route_fingerprint_count": 1,
            "route_fingerprint": "feedling-v2-route-test",
            "prompt_tokens": 1000,
            "cache_read_tokens": 600,
            "cache_write_tokens": 100,
            "cache_miss_tokens": 400,
            "effective_input_tokens": 1000,
            "hit_ratio": 0.6,
        },
    )
    monkeypatch.setattr(
        jobs_store,
        "recent_tail_window_stats",
        lambda **kw: {
            "lane": kw["lane"],
            "sample_limit": 1000,
            "sampled_turns": 4,
            "measured_turns": 4,
            "measurement_coverage": 1.0,
            "effective_tail_turns_min": 12,
            "effective_tail_turns_avg": 24.0,
            "effective_tail_turns_max": 40,
            "fallback_turns": 1,
            "fallback_rate": 0.25,
            "prompt_frontier_exhaustion_count": 0,
            "prompt_tokens": 1000,
        },
    )
    monkeypatch.setattr(jobs_store, "genesis_worker_alive", lambda **kw: True)
    monkeypatch.setattr(
        admin_core.config_store,
        "hosted_runtime_policy_status",
        lambda: {
            "policy": "v2_only",
            "target_mode": "db_action_v2",
            "eligible_count": 3,
            "ready_count": 3,
            "inconsistent_count": 0,
            "inconsistent_user_ids": [],
        },
    )
    monkeypatch.setattr(
        admin_core.db,
        "effect_outbox_health",
        lambda: {
            "pending": 2,
            "needs_reconciliation": 1,
            "oldest_unresolved_age_sec": 45.0,
        },
    )
    monkeypatch.setattr(
        jobs_store,
        "wake_success_stats",
        lambda **kw: {
            "completed": 4,
            "failed": 1,
            "expired": 0,
            "success_rate": 0.8,
            "by_lane": {"heartbeat": {"completed": 4}, "scheduled": {"failed": 1}},
        },
    )
    # 刻意给一组与 wake 不同的数字：整字典断言才能证明两个块没有串位
    monkeypatch.setattr(
        jobs_store,
        "memory_lane_health",
        lambda **kw: {
            "completed": 7,
            "failed": 2,
            "expired": 1,
            "success_rate": 0.7,
            "by_lane": {"capture": {"completed": 7}, "dream": {"failed": 2, "expired": 1}},
        },
    )
    yield


def _admin(token=ADMIN_TOKEN):
    return {"X-Admin-Token": token}


def _asgi(method, path, headers=None, **kw):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.request(method, path, headers=headers or {}, **kw)

    return asyncio.run(go())


def _asgi_json(method, path, headers=None, **kw):
    resp = _asgi(method, path, headers=headers, **kw)
    body = None
    if resp.content:
        try:
            body = resp.json()
        except Exception:
            body = None
    return resp.status_code, body


def test_v2_metrics_returns_every_field(env):
    status, body = _asgi_json("GET", "/v1/admin/v2-metrics", headers=_admin())

    assert status == 200
    assert body == {
        "inflight": 3,
        "pending": 1,
        "live_workers": 2,
        "live_worker_capacity": 8,
        "worker_heartbeats": _HEARTBEATS,
        "worker_heartbeat_count": 3,
        "runtime_policy": {
            "policy": "v2_only",
            "target_mode": "db_action_v2",
            "eligible_count": 3,
            "ready_count": 3,
            "inconsistent_count": 0,
            "inconsistent_user_ids": [],
        },
        "mean_service_sec": 4.5,
        "recent_mean_tokens_per_turn": 123.0,
        "turn_health": {
            "window_hours": 24,
            "sample_limit": 1000,
            "jobs": {
                "sampled_terminal_jobs": 10,
                "completed": 8,
                "failed": 1,
                "expired": 1,
                "queue_expired": 1,
                "lease_expired": 0,
                "superseded": 0,
                "failure_rate": 0.1,
                "expiry_rate": 0.1,
                "error_or_expiry_rate": 0.2,
                "pending": 1,
                "oldest_pending_age_sec": 12.5,
            },
            "latency": {"sampled_turns": 10, "p95_ms": 4200.0},
            "trajectory": {
                "sampled_jobs": 12,
                "complete": 10,
                "partial": 1,
                "missing": 0,
                "open": 1,
                "capture_gap": 1,
                "complete_rate": 10 / 12,
            },
        },
        "runtime_health": {
            "window_hours": 24,
            "pending": 1,
            "oldest_pending_age_sec": 12.5,
            "lanes": [
                {
                    "lane": "capture",
                    "sampled_jobs": 3,
                    "completed": 2,
                    "failed": 1,
                    "expired": 0,
                    "superseded": 0,
                    "failure_rate": 1 / 3,
                },
                {
                    "lane": "dream",
                    "sampled_jobs": 1,
                    "completed": 1,
                    "failed": 0,
                    "expired": 0,
                    "superseded": 0,
                    "failure_rate": 0.0,
                },
            ],
        },
        "prompt_cache": {
            "sampled_turns": 4,
            "model_calls": 5,
            "usage_reported_calls": 5,
            "cache_reported_calls": 4,
            "usage_telemetry_coverage": 1.0,
            "cache_telemetry_coverage": 0.8,
            "route_identity_coverage": 1.0,
            "route_fingerprint_count": 1,
            "route_fingerprint": "feedling-v2-route-test",
            "prompt_tokens": 1000,
            "cache_read_tokens": 600,
            "cache_write_tokens": 100,
            "cache_miss_tokens": 400,
            "effective_input_tokens": 1000,
            "hit_ratio": 0.6,
        },
        "tail_window": {
            lane: {
                "lane": lane,
                "sample_limit": 1000,
                "sampled_turns": 4,
                "measured_turns": 4,
                "measurement_coverage": 1.0,
                "effective_tail_turns_min": 12,
                "effective_tail_turns_avg": 24.0,
                "effective_tail_turns_max": 40,
                "fallback_turns": 1,
                "fallback_rate": 0.25,
                "prompt_frontier_exhaustion_count": 0,
                "prompt_tokens": 1000,
            }
            for lane in (
                "chat",
                "heartbeat",
                "scheduled",
                "manual_wake",
                "screen_watch",
            )
        },
        "wake": {
            "completed": 4,
            "failed": 1,
            "expired": 0,
            "success_rate": 0.8,
            "by_lane": {"heartbeat": {"completed": 4}, "scheduled": {"failed": 1}},
        },
        "memory_lanes": {
            "completed": 7,
            "failed": 2,
            "expired": 1,
            "success_rate": 0.7,
            "by_lane": {"capture": {"completed": 7}, "dream": {"failed": 2, "expired": 1}},
        },
        "effects": {
            "pending": 2,
            "needs_reconciliation": 1,
            "oldest_unresolved_age_sec": 45.0,
        },
        "pools": {
            "foreground": {"configured": 4, "healthy": 4, "busy": 2, "restarting": 0, "pending": 1, "oldest_pending_sec": 3.5, "claim_p95_ms": 80.0},
            "wake": {"configured": 2, "healthy": 2, "busy": 1, "restarting": 0, "pending": 0, "oldest_pending_sec": None, "claim_p95_ms": 120.0},
            "heavy": {"configured": 2, "healthy": 2, "busy": 1, "restarting": 0, "pending": 2, "oldest_pending_sec": 40.0, "claim_p95_ms": 900.0},
        },
        "jobs_by_lane": {"chat": {"pending": 1, "active": 2}, "profile": {"pending": 0, "active": 1}},
        "preemptions_24h": {"profile:terminal": 1, "scheduled:requeued": 2},
        "watchdog_recoveries_24h": {"chat:terminal": 1, "profile:requeued": 1},
        "enclave": _HEARTBEATS[0]["runtime_state"]["enclave"],
        "db_pools": _HEARTBEATS[0]["runtime_state"]["db_pools"],
        "isolation_events": _HEARTBEATS[0]["runtime_state"]["isolation_events"],
        "profile_runtime": _HEARTBEATS[0]["runtime_state"]["profile_runtime"],
        "genesis_alive": True,
    }


def test_v2_metrics_surfaces_a_dead_genesis_thread(env, monkeypatch):
    """A dead genesis thread must be visible even when every turn worker is healthy.

    `live_workers` counts kind='turn' rows only, so a genesis thread that died to a
    lazy-import error inside `run_loop` leaves no other trace anywhere.
    """
    monkeypatch.setattr(jobs_store, "genesis_worker_alive", lambda **kw: False)

    status, body = _asgi_json("GET", "/v1/admin/v2-metrics", headers=_admin())

    assert status == 200
    assert body["genesis_alive"] is False
    assert body["live_workers"] == 2


def test_v2_metrics_filters_cache_proof_to_route_and_window(env, monkeypatch):
    seen = {}

    def _cache_stats(**kwargs):
        seen.update(kwargs)
        return {
            "sampled_turns": 2,
            "model_calls": 2,
            "cache_read_tokens": 900,
            "filter": {
                "provider": kwargs.get("provider"),
                "model": kwargs.get("model"),
                "cache_route_fingerprint": kwargs.get("cache_route_fingerprint"),
                "user_id": kwargs.get("user_id"),
                "since_ts": kwargs.get("since_ts"),
                "until_ts": kwargs.get("until_ts"),
                "include_turns": kwargs.get("include_turns"),
            },
        }

    monkeypatch.setattr(jobs_store, "recent_prompt_cache_stats", _cache_stats)
    status, body = _asgi_json(
        "GET",
        "/v1/admin/v2-metrics?cache_provider=openai&cache_model=gpt-5"
        "&cache_route_fingerprint=feedling-v2-route-test&cache_user_id=u-canary"
        "&cache_since_ts=123.5&cache_until_ts=456.5",
        headers=_admin(),
    )

    assert status == 200
    assert seen == {
        "lane": "chat",
        "provider": "openai",
        "model": "gpt-5",
        "cache_route_fingerprint": "feedling-v2-route-test",
        "user_id": "u-canary",
        "since_ts": 123.5,
        "until_ts": 456.5,
        "include_turns": True,
    }
    assert body["prompt_cache"]["filter"] == {
        "provider": "openai",
        "model": "gpt-5",
        "cache_route_fingerprint": "feedling-v2-route-test",
        "user_id": "u-canary",
        "since_ts": 123.5,
        "until_ts": 456.5,
        "include_turns": True,
    }


@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "1e308", "not-a-number"])
def test_v2_metrics_rejects_invalid_cache_window(env, raw):
    status, body = _asgi_json(
        "GET", f"/v1/admin/v2-metrics?cache_since_ts={raw}", headers=_admin())

    assert status == 400
    assert body == {"error": "invalid_cache_since_ts"}


def test_v2_metrics_rejects_reversed_cache_window(env):
    status, body = _asgi_json(
        "GET",
        "/v1/admin/v2-metrics?cache_since_ts=200&cache_until_ts=100",
        headers=_admin(),
    )

    assert status == 400
    assert body == {"error": "invalid_cache_window"}


def test_v2_metrics_no_token_is_401(env):
    status, body = _asgi_json("GET", "/v1/admin/v2-metrics")

    assert status == 401
    assert body == {"error": "unauthorized"}


def test_v2_metrics_wrong_token_is_401(env):
    status, body = _asgi_json("GET", "/v1/admin/v2-metrics", headers=_admin("wrong-token"))

    assert status == 401
    assert body == {"error": "unauthorized"}


def test_v2_wake_shadow_uses_explicit_report_bucket(env, monkeypatch):
    seen = {}

    def _report(**kwargs):
        seen.update(kwargs)
        return {
            "days": kwargs["days"],
            "bucket": {
                "start_hour_inclusive": kwargs["bucket_start_hour"],
                "end_hour_exclusive": kwargs["bucket_end_hour"],
                "purpose": "observation_only_not_product_policy",
            },
            "allowed": 20,
            "bucket_allowed": 7,
            "bucket_allowed_apns_alert_sent": 3,
        }

    monkeypatch.setattr(jobs_store, "wake_shadow_report", _report)
    status, body = _asgi_json(
        "GET",
        "/v1/admin/v2-wake-shadow?days=14&start_hour=23&end_hour=7",
        headers=_admin(),
    )

    assert status == 200
    assert seen == {
        "days": 14,
        "bucket_start_hour": 23,
        "bucket_end_hour": 7,
    }
    assert body["allowed"] == 20
    assert body["bucket_allowed"] == 7
    assert body["bucket_allowed_apns_alert_sent"] == 3
    assert body["bucket"]["purpose"] == "observation_only_not_product_policy"


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("start_hour=23&end_hour=7", "invalid_days"),
        ("days=0&start_hour=23&end_hour=7", "invalid_days"),
        ("days=7&start_hour=24&end_hour=7", "invalid_start_hour"),
        ("days=7&start_hour=23&end_hour=23", "invalid_hour_bucket"),
    ],
)
def test_v2_wake_shadow_rejects_implicit_or_invalid_bucket(env, query, error):
    status, body = _asgi_json(
        "GET", f"/v1/admin/v2-wake-shadow?{query}", headers=_admin())

    assert status == 400
    assert body == {"error": error}


def test_v2_wake_shadow_requires_admin(env):
    status, body = _asgi_json(
        "GET", "/v1/admin/v2-wake-shadow?days=7&start_hour=23&end_hour=7")

    assert status == 401
    assert body == {"error": "unauthorized"}
