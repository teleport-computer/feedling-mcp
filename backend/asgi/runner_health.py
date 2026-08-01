"""Pure runner-fleet health policy for public health reporting."""

from __future__ import annotations


def parse_expected_runner_count(raw: str | None) -> int:
    try:
        value = int(raw or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("expected runner count must be a positive integer") from exc
    if value < 1 or str(value) != str(raw).strip():
        raise ValueError("expected runner count must be a positive integer")
    return value


def evaluate_runner_fleet(instances, *, expected, now, max_age):
    observed = len(instances)
    healthy = 0
    for heartbeat in instances:
        try:
            ts = float(heartbeat.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0 and now - ts <= max_age and bool(heartbeat.get("host_all")):
            healthy += 1
    ok = observed == expected and healthy == expected
    result = {
        "status": "ok" if ok else "down",
        "expected": expected,
        "healthy": healthy,
        "observed": observed,
        "max_age_seconds": float(max_age),
    }
    if not ok:
        result["reason"] = "runner_count_mismatch"
    return result
