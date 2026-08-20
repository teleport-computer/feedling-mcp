from __future__ import annotations

import json
import os
from contextlib import contextmanager

import psycopg
import db
from admin import plaintext_shadow_scheduler as scheduler
from plaintext_shadow import outbox


def test_scheduler_does_not_start_when_disabled(monkeypatch):
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0")
    assert scheduler.should_start() is False


def test_scheduler_starts_only_for_valid_enabled_topology(monkeypatch):
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("DATABASE_URL", "postgresql://primary.invalid/primary")
    monkeypatch.setenv(
        "PLAINTEXT_SHADOW_DATABASE_URL", "postgresql://shadow.invalid/plaintext"
    )
    assert scheduler.should_start() is True


def test_sync_tick_persists_scalar_redacted_metrics(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        scheduler.outbox,
        "drain_once",
        lambda limit=500: outbox.DrainReport(
            claimed=3, applied=2, deleted=1, retried=0, quarantined=0
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "_queue_metrics",
        lambda: {
            "pending": 0,
            "quarantined": 0,
            "oldest_pending_seconds": None,
            "tables": {"chat_messages": {"pending": 0, "quarantined": 0}},
        },
    )
    monkeypatch.setattr(
        scheduler, "_probe_target", lambda: {"ok": True, "latency_ms": 1.25}
    )
    monkeypatch.setattr(
        scheduler.plaintext_shadow_admin,
        "strict_report",
        lambda: {"ok": True, "failure_slugs": []},
    )
    monkeypatch.setattr(
        scheduler.db,
        "record_plaintext_shadow_sync_run",
        lambda summary: recorded.append(summary),
    )

    report = scheduler._sync_tick(force_verify=True)

    assert report.applied == 2
    assert report.verify_ok is True
    assert recorded and recorded[0]["target_ok"] is True
    rendered = json.dumps(recorded)
    assert "postgresql://" not in rendered
    assert "body_ct" not in rendered
    assert "secret" not in rendered


def test_leader_uses_dedicated_singleton_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.leader.run_singleton",
        lambda name, start: calls.append((name, start)),
    )

    scheduler.start_elected()

    assert calls == [("plaintext-shadow-sync", scheduler.start)]


def test_scalar_run_metrics_persist_in_primary_control_table(monkeypatch):
    class Pool:
        @contextmanager
        def connection(self):
            with psycopg.connect(
                os.environ["TEE_DATABASE_URL"], autocommit=True
            ) as conn:
                yield conn

    monkeypatch.setattr(db, "get_pool", lambda: Pool())
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE plaintext_shadow_sync_runs")

    try:
        db.record_plaintext_shadow_sync_run(
            {
                "duration_ms": 12,
                "applied": 2,
                "deleted": 1,
                "retried": 0,
                "quarantined": 0,
                "pending": 0,
                "oldest_pending_seconds": None,
                "target_ok": True,
                "target_probe_ms": 1.5,
                "verify_ok": True,
                "table_metrics": {"chat_messages": {"pending": 0}},
            }
        )
        rows = db.recent_plaintext_shadow_sync_runs(limit=1)

        assert rows[0]["applied"] == 2
        assert rows[0]["target_ok"] is True
        assert rows[0]["table_metrics"] == {"chat_messages": {"pending": 0}}
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("TRUNCATE plaintext_shadow_sync_runs")
