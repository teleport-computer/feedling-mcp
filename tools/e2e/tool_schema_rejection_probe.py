#!/usr/bin/env python3
"""Read-only T363 acceptance probe for plaintext provider fallback metadata.

This probe performs one authenticated admin GET and never mutates user state.
It deliberately has four outcomes so an unavailable instrument cannot be
mistaken for an absent field:

* exit 0, ``READABLE``: per-attempt status codes and the admin aggregate exist;
* exit 1, ``FIELD_ABSENT``: a fresh payload has provider errors but no status;
* exit 2, ``IMPL_ONLY``: attempt fields exist but the admin aggregate does not;
* exit 3, ``INSTRUMENT_DOWN``: fetch/payload/phenomenon is unavailable, so the
  probe makes no claim about the implementation.
* exit 4, ``REASON_UNANCHORED``: an aggregate claims the fallback, but no failed
  attempt carries that reason together with its 400/422 status.

Unlike a curl ``-o`` workflow, the response body exists only in this process;
a failed request therefore cannot reuse a stale success file.

Pre-fix production anchor (2026-08-28 12:5xZ):

    user=usr_fee1dfed872e74d3 http=200 elapsed=199s bytes=154967
      ledger rows=200 provider_error rows=129
      error_class values: {'ProviderError': 129}
      VERDICT=FIELD_ABSENT exit=1

After deployment, run only after ``/healthz`` reports the expected
``release.git_commit`` and a canary succeeds. The same command must then reach
``READABLE``; unit tests alone do not prove the admin readback boundary closed.

Usage (requires ``~/.feedling/data-track-admin-token`` by default):

    NO_PROXY='*' python3 tools/e2e/tool_schema_rejection_probe.py
    NO_PROXY='*' python3 tools/e2e/tool_schema_rejection_probe.py \
        --user-id usr_fee1dfed872e74d3 --budget 420
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_USER_ID = "usr_fee1dfed872e74d3"
DEFAULT_BASE_URL = "https://api.feedling.app"


def _instrument_down(reason: str) -> int:
    print(f"VERDICT=INSTRUMENT_DOWN reason={reason}")
    print("  -> says NOTHING about whether the field exists.")
    return 3


def _scan_semantic_fields(value: Any, found: dict[str, list[Any]]) -> None:
    """Find status/reason fields by meaning, not one guessed field path."""
    if isinstance(value, dict):
        for key, inner in value.items():
            normalized = str(key).lower()
            if "status" in normalized and "code" in normalized:
                found["status_code"].append(inner)
            if any(
                token in normalized
                for token in ("fallback", "rejection", "reason")
            ):
                found["reason"].append((str(key), inner))
            _scan_semantic_fields(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _scan_semantic_fields(inner, found)


def _has_failed_then_ok_signature(attempts: list[Any]) -> bool:
    """Return whether one job visibly recovered after a provider error."""
    seen_failed_jobs: set[str] = set()
    for row in attempts:
        if not isinstance(row, dict):
            continue
        job = str(row.get("parent_message_id") or "").strip()
        if not job:
            continue
        outcome = str(row.get("outcome") or "")
        if outcome == "provider_error":
            seen_failed_jobs.add(job)
        elif outcome == "ok" and job in seen_failed_jobs:
            return True
    return False


def _evaluate(doc: Any) -> int:
    if not isinstance(doc, dict) or not isinstance(doc.get("user"), dict):
        return _instrument_down("no_user_object")
    ledger_detail = doc["user"].get("provider_attempt_ledger")
    if not isinstance(ledger_detail, dict):
        return _instrument_down("no_provider_attempt_ledger")
    attempts = ledger_detail.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return _instrument_down("no_ledger_attempts")

    errors = [
        row
        for row in attempts
        if isinstance(row, dict) and row.get("outcome") == "provider_error"
    ]
    print(f"  ledger rows={len(attempts)} provider_error rows={len(errors)}")
    if not errors:
        return _instrument_down("no_error_rows_in_window")

    found: dict[str, list[Any]] = {"status_code": [], "reason": []}
    matching_codes: list[int] = []
    target_reason_outcomes: list[str] = []
    for row in attempts:
        if not isinstance(row, dict):
            continue
        row_found: dict[str, list[Any]] = {"status_code": [], "reason": []}
        _scan_semantic_fields(row, row_found)
        if any(
            str(value).strip() == "tool_schema_rejected"
            for _key, value in row_found["reason"]
        ):
            target_reason_outcomes.append(str(row.get("outcome") or ""))

    for row in errors:
        row_found: dict[str, list[Any]] = {"status_code": [], "reason": []}
        _scan_semantic_fields(row, row_found)
        found["status_code"].extend(row_found["status_code"])
        found["reason"].extend(row_found["reason"])
        row_codes = {
            int(code)
            for code in row_found["status_code"]
            if str(code).strip() in {"400", "422"}
        }
        has_schema_reason = any(
            str(value).strip() == "tool_schema_rejected"
            for _key, value in row_found["reason"]
        )
        if has_schema_reason:
            matching_codes.extend(sorted(row_codes))
    codes = [
        code
        for code in found["status_code"]
        if code not in (None, "", 0)
    ]
    reasons = [
        (key, value)
        for key, value in found["reason"]
        if value not in (None, "", [], {})
    ]

    error_classes = collections.Counter(
        str(row.get("error_class", "")) for row in errors
    )
    print(f"  error_class values: {dict(error_classes)}")
    durations = sorted(
        float(value)
        for row in errors
        for key, value in row.items()
        if "dur" in str(key).lower()
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )
    if durations:
        print(
            "  failed-call duration present: "
            f"n={len(durations)} median_ms={durations[len(durations) // 2]:.0f}"
        )
    else:
        print("  failed-call duration: ABSENT")

    if not codes:
        if not _has_failed_then_ok_signature(attempts):
            return _instrument_down(
                "phenomenon_absent_no_failed_then_ok_job"
            )
        print("VERDICT=FIELD_ABSENT reason=no_status_code_on_provider_errors")
        print("  -> fresh payload; the discriminator is genuinely missing.")
        return 1

    aggregate = ledger_detail.get("fallback_counts")
    aggregate_key_present = "fallback_counts" in ledger_detail
    aggregate_pairs = (
        {
            (int(row["status_code"]), int(row["count"]))
            for row in aggregate
            if isinstance(row, dict)
            and row.get("fallback_reason") == "tool_schema_rejected"
            and str(row.get("status_code")).strip() in {"400", "422"}
            and str(row.get("count", "")).isdigit()
            and int(row["count"]) > 0
        }
        if isinstance(aggregate, list)
        else set()
    )
    distribution = collections.Counter(str(code) for code in matching_codes)
    if not distribution:
        if aggregate_pairs or any(
            outcome != "provider_error"
            for outcome in target_reason_outcomes
        ):
            print(
                "VERDICT=REASON_UNANCHORED "
                "reason=target_reason_has_no_matching_failed_attempt"
            )
            print("  -> fallback reason is absent or attached to the wrong row.")
            return 4
        return _instrument_down("no_tool_schema_rejection_in_window")
    print(f"  status_code distribution: {dict(distribution)}")
    if reasons:
        reason_keys = collections.Counter(key for key, _ in reasons)
        print(f"  reason-ish fields: {dict(reason_keys)}")

    if not aggregate_pairs:
        missing_reason = (
            "admin_aggregate_key_absent"
            if not aggregate_key_present
            else "admin_aggregate_has_no_matching_target_count"
        )
        print(
            "VERDICT=IMPL_ONLY "
            f"reason={missing_reason}"
        )
        print("  -> producer closed; observed response boundary did not.")
        return 2
    print(f"  admin aggregate: {json.dumps(aggregate, ensure_ascii=False)[:300]}")

    if len(distribution) == 1 and set(distribution) & {"400", "422"}:
        print(
            "VERDICT=READABLE note=only_one_of_400_or_422_seen_in_window"
        )
    else:
        print("VERDICT=READABLE")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--budget", type=float, default=420.0)
    parser.add_argument(
        "--base", default=os.environ.get("T363_BASE", DEFAULT_BASE_URL)
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".feedling" / "data-track-admin-token",
    )
    args = parser.parse_args()

    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return _instrument_down("no_admin_token")
    if not token:
        return _instrument_down("empty_admin_token")

    url = (
        f"{str(args.base).rstrip('/')}/v1/admin/data-track/users/"
        f"{args.user_id}"
    )
    started = time.monotonic()
    try:
        with httpx.Client(timeout=args.budget, trust_env=False) as client:
            response = client.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
    except Exception as exc:  # noqa: BLE001 - instrument failure is a verdict
        elapsed = time.monotonic() - started
        print(f"user={args.user_id} http=000 elapsed={elapsed:.1f}s bytes=0")
        return _instrument_down(f"fetch_failed({type(exc).__name__})")

    elapsed = time.monotonic() - started
    print(
        f"user={args.user_id} http={response.status_code} "
        f"elapsed={elapsed:.1f}s bytes={len(response.content)}"
    )
    if response.status_code != 200 or not response.content:
        return _instrument_down(f"fetch_failed(http={response.status_code})")
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - instrument failure is a verdict
        return _instrument_down(f"unparseable_body({type(exc).__name__})")
    return _evaluate(payload)


if __name__ == "__main__":
    raise SystemExit(main())
