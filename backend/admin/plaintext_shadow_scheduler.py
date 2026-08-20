"""Single-leader drain and health loop for the decrypted plaintext shadow."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass

import db
from admin import plaintext_shadow as plaintext_shadow_admin
from plaintext_shadow import config, outbox
from tee_shadow import mirror


@dataclass(frozen=True)
class TickReport:
    duration_ms: int
    applied: int
    deleted: int
    retried: int
    quarantined: int
    pending: int
    oldest_pending_seconds: float | None
    target_ok: bool
    target_probe_ms: float | None
    verify_ok: bool | None
    table_metrics: dict


_last_verify_at: float | None = None


def should_start() -> bool:
    config.validate_startup()
    return config.load_target() is not None


def _interval() -> float:
    try:
        return max(
            5.0,
            float(os.environ.get("FEEDLING_PLAINTEXT_SHADOW_INTERVAL_SEC", "30")),
        )
    except (TypeError, ValueError):
        return 30.0


def _verify_interval() -> float:
    try:
        return max(
            30.0,
            float(
                os.environ.get("FEEDLING_PLAINTEXT_SHADOW_VERIFY_INTERVAL_SEC", "300")
            ),
        )
    except (TypeError, ValueError):
        return 300.0


def _probe_target() -> dict:
    started = time.monotonic()
    try:
        policy = config.require_target()
        with mirror.get_target_pool(policy).connection() as conn:
            ok = conn.execute("SELECT 1").fetchone() == (1,)
    except Exception:  # noqa: BLE001 - never expose endpoint or exception text
        ok = False
    return {
        "ok": ok,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
    }


def _queue_metrics() -> dict:
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT table_name, count(*), "
            "count(*) FILTER (WHERE quarantined_at IS NOT NULL) "
            "FROM plaintext_shadow_dirty_keys GROUP BY table_name ORDER BY table_name"
        ).fetchall()
        oldest = conn.execute(
            "SELECT EXTRACT(epoch FROM now() - min(created_at)) "
            "FROM plaintext_shadow_dirty_keys"
        ).fetchone()[0]
    tables = {
        table: {"pending": int(pending), "quarantined": int(quarantined)}
        for table, pending, quarantined in rows
    }
    return {
        "pending": sum(item["pending"] for item in tables.values()),
        "quarantined": sum(item["quarantined"] for item in tables.values()),
        "oldest_pending_seconds": None if oldest is None else float(oldest),
        "tables": tables,
    }


def _sync_tick(*, force_verify: bool = False) -> TickReport:
    global _last_verify_at

    started = time.monotonic()
    table_metrics: dict = {}
    try:
        drained = outbox.drain_once(limit=500)
    except Exception:  # noqa: BLE001 - fixed scalar diagnostics only
        drained = outbox.DrainReport()
        table_metrics["_tick"] = {"drain_failed": 1}

    try:
        queue = _queue_metrics()
        table_metrics.update(queue["tables"])
    except Exception:  # noqa: BLE001
        queue = {
            "pending": 0,
            "quarantined": 0,
            "oldest_pending_seconds": None,
        }
        table_metrics.setdefault("_tick", {})["queue_probe_failed"] = 1

    probe = _probe_target()
    now = time.monotonic()
    verify_due = force_verify or _last_verify_at is None or (
        now - _last_verify_at >= _verify_interval()
    )
    verify_ok: bool | None = None
    if verify_due:
        try:
            verify_ok = bool(plaintext_shadow_admin.strict_report().get("ok"))
        except Exception:  # noqa: BLE001
            verify_ok = False
            table_metrics.setdefault("_tick", {})["verify_failed"] = 1
        _last_verify_at = now

    report = TickReport(
        duration_ms=round((time.monotonic() - started) * 1000),
        applied=drained.applied,
        deleted=drained.deleted,
        retried=drained.retried,
        quarantined=drained.quarantined,
        pending=int(queue["pending"]),
        oldest_pending_seconds=queue["oldest_pending_seconds"],
        target_ok=bool(probe["ok"]),
        target_probe_ms=probe["latency_ms"],
        verify_ok=verify_ok,
        table_metrics=table_metrics,
    )
    try:
        db.record_plaintext_shadow_sync_run(asdict(report))
    except Exception:  # noqa: BLE001 - metrics cannot terminate the scheduler
        pass
    return report


def _loop() -> None:
    while True:
        time.sleep(_interval())
        if not should_start():
            continue
        try:
            _sync_tick()
        except Exception:  # noqa: BLE001 - keep the elected loop alive
            continue


def start() -> None:
    threading.Thread(
        target=_loop, daemon=True, name="plaintext-shadow-sync"
    ).start()


def start_elected() -> None:
    from core import leader

    leader.run_singleton("plaintext-shadow-sync", start)
