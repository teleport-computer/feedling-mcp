#!/usr/bin/env python3
"""Summarize bounded, operator-supplied resident job snapshots offline.

Snapshot provenance is not authenticated here. Categories are consumer reports,
not independently verified causes or attempt-level attribution. No database,
network, credential, or historical backfill operation is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from proactive.failure_diagnostics import ERROR_CLASSES, SOURCE, normalize_stored

MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 100_000


def summarize(snapshot: dict) -> dict:
    if (type(snapshot) is not dict or type(snapshot.get("schema_version")) is not int
            or snapshot["schema_version"] != 1 or type(snapshot.get("jobs")) is not list
            or len(snapshot["jobs"]) > MAX_RECORDS):
        raise ValueError("invalid snapshot")
    identities: dict[tuple[str, str], list[dict]] = {}
    for row in snapshot["jobs"]:
        if type(row) is not dict:
            raise ValueError("invalid snapshot")
        user, job, doc = row.get("user_id"), row.get("job_id"), row.get("doc")
        if (type(user) is not str or not user.strip() or len(user) > 160
                or type(job) is not str or not job.strip() or len(job) > 160
                or type(doc) is not dict or doc.get("job_id") != job
                or type(doc.get("status")) is not str
                or (doc.get("source") is not None and type(doc["source"]) is not str)):
            raise ValueError("invalid snapshot")
        identities.setdefault((user, job), []).append(doc)
    counts = Counter({name: 0 for name in (
        "candidate_jobs", "confirmed_failed_jobs", "conflicting_candidate_jobs",
        "scope_unknown_jobs", "non_failed_jobs", "out_of_scope_jobs",
    )})
    evidence = Counter({name: 0 for name in (
        "reported", "missing", "invalid", "binding_unavailable", "conflicting_reports", "diagnostic_conflict",
    )})
    classes = Counter({name: 0 for name in sorted(ERROR_CLASSES)})
    for docs in identities.values():
        states = {(doc["status"], doc.get("source")) for doc in docs}
        if ("failed", SOURCE) not in states:
            bucket = ("scope_unknown_jobs" if any(source is None for _, source in states)
                      else "non_failed_jobs" if all(source == SOURCE for _, source in states)
                      else "out_of_scope_jobs")
            counts[bucket] += 1
            continue
        counts["candidate_jobs"] += 1
        if len(states) != 1:
            counts["conflicting_candidate_jobs"] += 1
            continue
        counts["confirmed_failed_jobs"] += 1
        projections = {json.dumps(normalize_stored(doc.get("failure_diagnostic")), sort_keys=True)
                       for doc in docs}
        if len(projections) != 1:
            evidence["diagnostic_conflict"] += 1
            continue
        diagnostic = json.loads(next(iter(projections)))
        evidence[diagnostic["state"]] += 1
        if diagnostic["state"] == "reported":
            classes[diagnostic["reported_error_class"]] += 1
    return {"schema_version": 1, **counts, "evidence_states": dict(evidence),
            "reported_classes": dict(classes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    try:
        with args.snapshot.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("invalid snapshot")
        result = summarize(json.loads(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        print("Invalid resident diagnostic snapshot", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
