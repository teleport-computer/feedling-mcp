"""Dry-run-first coordinator for effective-off historical plaintext repair."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import fcntl
import threading
import time
from typing import Callable

import db
import httpx
from content import plaintext_migration


APPLY_ENV = plaintext_migration.APPLY_ENV
FAILURE_LOG_ENV = "FEEDLING_PLAINTEXT_MIGRATION_FAILURE_LOG"
DEFAULT_FAILURE_LOG = "/data/plaintext-migration-failures.jsonl"
CHECKPOINT_ENV = "FEEDLING_PLAINTEXT_MIGRATION_CHECKPOINT"
DEFAULT_CHECKPOINT = "/data/plaintext-migration-checkpoint.json"
_FAILURE_LOG_LOCK = threading.Lock()


def append_failure_log(*, run_id: str, user_id: str, failures: list[dict]) -> None:
    """Append content-free failed item records to the persistent CVM volume."""
    if not failures:
        return
    path = Path(os.environ.get(FAILURE_LOG_ENV, DEFAULT_FAILURE_LOG))
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with _FAILURE_LOG_LOCK, path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        for failure in failures:
            record = {
                "timestamp": timestamp,
                "run_id": str(run_id),
                "user_id": str(user_id),
                "surface": str(failure.get("surface", "unknown")),
                "item_id": str(failure.get("item_id", "")),
                "status": str(failure.get("status", "unknown")),
                "stage": str(failure.get("stage", "unknown")),
                "exception_type": str(failure.get("exception_type", "unknown")),
            }
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_checkpoint(*, path: str, run_id: str, user_id: str, failures: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = {
        "run_id": str(run_id),
        "last_completed_user_id": str(user_id),
        "failures": int(failures),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, target)


class HealthGateError(RuntimeError):
    """Raised when the enclave does not recover within the pause budget."""


@dataclass(frozen=True)
class RepairResult:
    apply: bool
    users_selected: int
    users_completed: int
    last_completed_user_id: str
    item_counts: dict[str, int]
    failures: int = 0

    def public_dict(self) -> dict:
        return {
            "apply": self.apply,
            "failures": self.failures,
            "item_counts": self.item_counts,
            "last_completed_user_id": self.last_completed_user_id,
            "users_completed": self.users_completed,
            "users_selected": self.users_selected,
        }


def eligible_user_ids(
    *, start_after: str = "", user_limit: int = 0, pool=None
) -> list[str]:
    """List existing effective-off users in a deterministic resume order."""
    if int(user_limit) < 0:
        raise ValueError("user_limit must be >= 0")
    sql = (
        "SELECT user_id FROM users WHERE user_id > %s "
        "AND lower(trim(coalesce(doc->>'content_encryption',''))) <> 'on' "
        "ORDER BY user_id"
    )
    params: tuple = (str(start_after or ""),)
    if user_limit:
        sql += " LIMIT %s"
        params += (int(user_limit),)
    source = pool if pool is not None else db.get_pool()
    with source.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [str(row[0]) for row in rows]


def probe_enclave_health(*, max_latency_sec: float = 10.0) -> bool:
    """Return whether the configured enclave health endpoint is timely."""
    base = os.environ.get("FEEDLING_ENCLAVE_URL", "").strip().rstrip("/")
    if not base or float(max_latency_sec) <= 0:
        return False
    started = time.monotonic()
    try:
        response = httpx.get(
            f"{base}/healthz",
            timeout=float(max_latency_sec),
            follow_redirects=False,
            # The in-cluster enclave endpoint uses its self-signed runtime
            # certificate.  This matches the existing enclave clients; the
            # URL is supplied by the trusted deployment configuration.
            verify=False,
        )
    except Exception:  # noqa: BLE001 - health result is deliberately boolean
        return False
    return response.status_code == 200 and (
        time.monotonic() - started <= float(max_latency_sec)
    )

def _wait_until_healthy(
    probe: Callable[[], bool],
    *,
    healthy_streak: int,
    health_poll_sec: float,
    max_pause_sec: float,
    sleep: Callable[[float], None],
) -> None:
    if healthy_streak <= 0:
        raise ValueError("healthy_streak must be > 0")
    if health_poll_sec <= 0 or max_pause_sec < 0:
        raise ValueError("health timing values are invalid")
    deadline = time.monotonic() + max_pause_sec
    consecutive = 0
    while consecutive < healthy_streak:
        try:
            healthy = bool(probe())
        except Exception:  # noqa: BLE001 - probe failures are unhealthy
            healthy = False
        consecutive = consecutive + 1 if healthy else 0
        if consecutive >= healthy_streak:
            return
        if time.monotonic() >= deadline:
            raise HealthGateError("enclave health gate timed out")
        sleep(health_poll_sec)


def run(
    *,
    apply: bool = False,
    start_after: str = "",
    user_limit: int = 0,
    row_limit: int = 0,
    rate: float = 1.0,
    workers: int = 1,
    health_probe: Callable[[], bool] | None = None,
    healthy_streak: int = 2,
    health_poll_sec: float = 5.0,
    max_pause_sec: float = 300.0,
    continue_on_failure: bool = False,
    run_id: str = "",
    retry_items: dict[str, set[str]] | None = None,
    checkpoint_path: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> RepairResult:
    """Inventory or repair effective-off users, optionally continuing after failures."""
    if int(row_limit) < 0:
        raise ValueError("row_limit must be >= 0")
    if float(rate) <= 0:
        raise ValueError("rate must be > 0")
    if int(workers) < 1 or int(workers) > 4:
        raise ValueError("workers must be between 1 and 4")
    users = eligible_user_ids(
        start_after=start_after,
        user_limit=user_limit,
    )
    counts: Counter[str] = Counter()
    completed = 0
    failures = 0
    last_completed = ""
    probe = health_probe or probe_enclave_health

    for user_id in users:
        if apply:
            try:
                _wait_until_healthy(
                    probe,
                    healthy_streak=healthy_streak,
                    health_poll_sec=health_poll_sec,
                    max_pause_sec=max_pause_sec,
                    sleep=sleep,
                )
            except HealthGateError:
                counts["failed_health_gate"] += 1
                failures += 1
                if continue_on_failure:
                    time.sleep(max(1.0, min(float(max_pause_sec), 60.0)))
                    continue
                break
        try:
            kwargs = {
                "apply": apply,
                "limit": row_limit,
                "rate": rate,
                "workers": workers,
            }
            if retry_items is not None:
                kwargs["item_ids"] = retry_items.get(user_id, set())
            result = plaintext_migration.run(user_id, **kwargs)
        except (PermissionError, ValueError):
            counts["failed_tier_or_user_changed"] += 1
            failures += 1
            append_failure_log(
                run_id=run_id,
                user_id=user_id,
                failures=[{"surface": "user", "item_id": "", "status": "failed_tier_or_user_changed"}],
            )
            if continue_on_failure:
                completed += 1
                last_completed = user_id
                continue
            break
        except Exception:  # noqa: BLE001 - never expose content-bearing details
            counts["failed_migration_setup"] += 1
            failures += 1
            append_failure_log(
                run_id=run_id,
                user_id=user_id,
                failures=[{"surface": "user", "item_id": "", "status": "failed_migration_setup"}],
            )
            if continue_on_failure:
                completed += 1
                last_completed = user_id
                continue
            break
        counts.update(result.counts)
        append_failure_log(
            run_id=run_id,
            user_id=user_id,
            failures=list(getattr(result, "failure_items", ())),
        )
        if result.failures:
            failures += int(result.failures)
            if not continue_on_failure:
                break
        if int(result.counts.get("not_attempted_limit", 0)) > 0:
            break
        completed += 1
        last_completed = user_id
        if checkpoint_path:
            write_checkpoint(
                path=checkpoint_path,
                run_id=run_id,
                user_id=user_id,
                failures=failures,
            )

    return RepairResult(
        apply=bool(apply),
        users_selected=len(users),
        users_completed=completed,
        last_completed_user_id=last_completed,
        item_counts=dict(sorted(counts.items())),
        failures=failures,
    )
