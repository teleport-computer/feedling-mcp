#!/usr/bin/env python3
"""Offline, observation-only attribution of confirmed failed V2 heartbeat jobs.

Input JSON contains jobs (user_id, job_id, status, lane) and flat trace events
(user_id, job_id, actor, lane, type, provider, model). Use only an independently
approved, bounded snapshot. This tool never queries a database or a provider.
Old trace-only events are deliberately not guessed onto jobs. Output contains
only aggregate counts, never identifiers, model names, or arbitrary input text.
No event proves the terminal cause; missing events never prove no model call.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

_MAX_RECORDS = 100_000
_MAX_BYTES = 16 * 1024 * 1024
_TYPES = {
    "agent.model.call.start": "start",
    "agent.model.call.done": "done",
    "agent.model.call.error": "error",
}


def _identity(row: dict) -> tuple[str, str] | None:
    user, job = row.get("user_id"), row.get("job_id")
    if not isinstance(user, str) or not isinstance(job, str):
        return None
    if not user.strip() or not job.strip():
        return None
    return user, job


def summarize(snapshot: dict) -> dict:
    """Count confirmed failures separately from conflicting candidate jobs."""
    jobs, events = snapshot.get("jobs"), snapshot.get("events")
    if not isinstance(jobs, list) or not isinstance(events, list):
        raise ValueError("jobs and events must be arrays")
    if len(jobs) > _MAX_RECORDS or len(events) > _MAX_RECORDS:
        raise ValueError("snapshot record limit exceeded")
    states: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for row in jobs:
        if not isinstance(row, dict) or _identity(row) is None:
            raise ValueError("invalid job identity")
        status, lane = row.get("status"), row.get("lane")
        if not isinstance(status, str) or not isinstance(lane, str):
            raise ValueError("invalid job state")
        states.setdefault(_identity(row), set()).add((status, lane))
    candidates = {key for key, values in states.items() if ("failed", "heartbeat") in values}
    conflicts = {key for key in candidates if len(states[key]) != 1}
    targets = candidates - conflicts
    observations = {key: set() for key in targets}
    routes = {key: set() for key in targets}
    incomplete = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid event")
        key = _identity(event)
        if (key not in targets
                or event.get("actor") != "hosted_v2"
                or event.get("lane") != "heartbeat"):
            continue
        kind = _TYPES.get(event.get("type")) if isinstance(event.get("type"), str) else None
        if kind is None:
            continue
        observations[key].add(kind)
        provider, model = event.get("provider"), event.get("model")
        if (isinstance(provider, str) and isinstance(model, str)
                and provider.strip() and model.strip()
                and provider.strip().lower() != "unknown"
                and model.strip().lower() != "unknown"):
            routes[key].add((provider, model))
        else:
            incomplete.add(key)

    evidence = Counter({
        "error_observed": 0, "done_without_error_observed": 0,
        "start_only_observed": 0, "unobserved": 0,
    })
    route_counts = Counter({
        "single_route_observed": 0, "multiple_routes_observed": 0,
        "unknown_or_partial_route": 0,
    })
    for key in targets:
        kinds = observations[key]
        bucket = (
            "error_observed" if "error" in kinds else
            "done_without_error_observed" if "done" in kinds else
            "start_only_observed" if "start" in kinds else "unobserved"
        )
        evidence[bucket] += 1
        route_bucket = (
            "multiple_routes_observed" if len(routes[key]) > 1 else
            "unknown_or_partial_route" if not routes[key] or key in incomplete else
            "single_route_observed"
        )
        route_counts[route_bucket] += 1
    return {
        "scope": "confirmed_failed_v2_heartbeat_jobs",
        "observation_only": True,
        "candidate_jobs": len(candidates),
        "conflicting_candidate_jobs": len(conflicts),
        "failed_jobs": len(targets),
        "evidence": dict(evidence),
        "routes": dict(route_counts),
        "jobs_with_incomplete_route_evidence": len(incomplete),
        "historical_cause_reconstructed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="bounded local JSON snapshot")
    args = parser.parse_args()
    try:
        with args.snapshot.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("snapshot byte limit exceeded")
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            raise ValueError("invalid snapshot")
        result = summarize(snapshot)
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Invalid or oversized attribution snapshot.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
