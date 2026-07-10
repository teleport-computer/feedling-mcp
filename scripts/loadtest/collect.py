"""D4 load-test metric collection: pure math (mean/p95) + DB samplers over
agent_jobs / v2_turn_metrics, plus a best-effort peak-RSS reader.

Pure math has no DB dependency. The DB samplers read the same tables the V2
pool writes in production (agent_jobs from D0's durable job queue,
v2_turn_metrics from D0 Task 3's per-turn instrumentation) so a manual load
test run and the driver (Task 3) can both call these helpers to build a
report.

p95 definition: nearest-rank on the sorted sample list, using
``ceil(0.95 * n) - 1`` as the (0-indexed) rank. This is the simple textbook
"nearest rank" percentile (no interpolation between neighbors) — chosen over
linear interpolation because it's exact for the boundary case p95([5]) == 5
and needs no numpy/statistics dependency.
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

_backend = str(Path(__file__).resolve().parent.parent.parent / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import db  # noqa: E402


def mean(samples: list[float]) -> float | None:
    """Arithmetic mean, or None for an empty sample list."""
    if not samples:
        return None
    return sum(samples) / len(samples)


def p95(samples: list[float]) -> float | None:
    """95th percentile via nearest-rank (see module docstring), or None if empty."""
    if not samples:
        return None
    ordered = sorted(samples)
    n = len(ordered)
    rank = math.ceil(0.95 * n) - 1
    rank = max(0, min(rank, n - 1))
    return ordered[rank]


def queue_wait_samples(*, within_hours: int = 24) -> list[float]:
    """Seconds between agent_jobs.created_at and claimed_at, for jobs claimed
    within the last ``within_hours``. Unclaimed (claimed_at IS NULL) jobs are
    excluded — they haven't waited a bounded amount yet."""
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (claimed_at - created_at)) "
                "FROM agent_jobs "
                "WHERE claimed_at IS NOT NULL "
                "  AND created_at > now() - make_interval(hours => %s)",
                (int(within_hours),),
            )
            return [float(row[0]) for row in cur.fetchall()]


def turn_latency_ms_samples(*, within_hours: int = 24) -> list[float]:
    """v2_turn_metrics.latency_ms values (milliseconds) from the recent window."""
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT latency_ms FROM v2_turn_metrics "
                "WHERE latency_ms IS NOT NULL "
                "  AND created_at > now() - make_interval(hours => %s)",
                (int(within_hours),),
            )
            return [float(row[0]) for row in cur.fetchall()]


def tokens_per_turn_samples(*, within_hours: int = 24) -> list[float]:
    """prompt_tokens + completion_tokens per v2_turn_metrics row, only rows
    where both are non-null, from the recent window."""
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prompt_tokens + completion_tokens FROM v2_turn_metrics "
                "WHERE prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL "
                "  AND created_at > now() - make_interval(hours => %s)",
                (int(within_hours),),
            )
            return [float(row[0]) for row in cur.fetchall()]


def stuck_job_count(*, within_hours: int = 24) -> int:
    """Count of agent_jobs reaper-expired (status='expired') within the window
    — these are jobs the pool claimed but never finished in time."""
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agent_jobs "
                "WHERE status = 'expired' "
                "  AND created_at > now() - make_interval(hours => %s)",
                (int(within_hours),),
            )
            return int(cur.fetchone()[0])


def peak_rss_kb(pid: int) -> int | None:
    """Best-effort resident set size (KB) for ``pid``. Tries psutil first
    (not installed in this dev env, imported lazily so its absence never
    breaks module import); falls back to shelling out to ``ps``. Returns
    None on any failure (bogus pid, no psutil, ps parse failure, etc.) —
    never raises."""
    try:
        import psutil  # type: ignore

        return int(psutil.Process(pid).memory_info().rss // 1024)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        text = result.stdout.strip()
        if not text:
            return None
        return int(text)
    except Exception:
        return None
