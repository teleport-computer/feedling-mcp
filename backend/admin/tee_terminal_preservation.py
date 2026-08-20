"""Preserve terminal ciphertext rows in TEE without decrypting them.

Dry-run is the default. Mutations require an exact confirmation phrase plus
the count and SHA-256 emitted by the immediately preceding dry-run.
"""

from __future__ import annotations

import argparse
import json
import os
import re

import psycopg

from admin.phase4_cutover import (
    _actual_tee_heads,
    _expected_tee_heads,
    _fingerprint,
)
from tee_replicator import terminal_preservation as preservation


_APPLY_CONFIRM = "PRESERVE-TERMINAL-CIPHERTEXT"
_REVERT_CONFIRM = "REVERT-PRESERVED-CIPHERTEXT"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _validate_mode(
    *,
    apply: bool,
    revert: bool,
    confirm: str | None,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> str:
    if apply and revert:
        raise RuntimeError("--apply and --revert are mutually exclusive")
    if not apply and not revert:
        if confirm is not None or expected_count is not None or expected_plan_sha256 is not None:
            raise RuntimeError("approval guards require --apply or --revert")
        return "dry-run"
    expected_confirm = _APPLY_CONFIRM if apply else _REVERT_CONFIRM
    if confirm != expected_confirm:
        raise RuntimeError("confirm mismatch")
    if expected_count is None or expected_count < 0:
        raise RuntimeError("--expected-count must be a non-negative integer")
    if expected_plan_sha256 is None or not _DIGEST_RE.fullmatch(
        expected_plan_sha256
    ):
        raise RuntimeError("--expected-plan-sha256 must be 64 lowercase hex characters")
    return "apply" if apply else "revert"


def _plan_report(plan: preservation.PreservationPlan) -> dict[str, object]:
    return {
        "ok": not plan.blockers,
        "eligible": len(plan.rows),
        "counts": plan.counts,
        "blockers": list(plan.blockers),
        "plan_sha256": plan.sha256,
    }


def run(
    *,
    apply: bool,
    revert: bool,
    confirm: str | None,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> dict[str, object]:
    """Validate topology/schema and dispatch dry-run, apply, or pre-cutover revert."""
    mode = _validate_mode(
        apply=apply,
        revert=revert,
        confirm=confirm,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )
    source_url = _required_env("DATABASE_URL")
    app_url = _required_env("TEE_DATABASE_URL")
    owner_url = _required_env("TEE_MIGRATION_DATABASE_URL")

    with (
        psycopg.connect(source_url, autocommit=True) as source,
        psycopg.connect(app_url, autocommit=True) as app,
        psycopg.connect(owner_url, autocommit=True) as owner,
    ):
        source_fingerprint = _fingerprint(source)
        app_fingerprint = _fingerprint(app)
        owner_fingerprint = _fingerprint(owner)
        if source_fingerprint == app_fingerprint:
            raise RuntimeError(
                "DATABASE_URL and TEE_DATABASE_URL resolve to the same database"
            )
        if owner_fingerprint != app_fingerprint:
            raise RuntimeError(
                "TEE_MIGRATION_DATABASE_URL does not resolve to TEE_DATABASE_URL"
            )
        expected_heads = _expected_tee_heads()
        actual_heads = _actual_tee_heads(app)
        if actual_heads != expected_heads:
            raise RuntimeError(
                "TEE schema is not at head: "
                f"expected={sorted(expected_heads)} actual={sorted(actual_heads)}"
            )

        source.execute("SET default_transaction_read_only = on")
        builder = (
            preservation.build_revert_plan
            if mode == "revert"
            else preservation.build_plan
        )
        plan = builder(source, app)
        result: dict[str, object] = {
            **_plan_report(plan),
            "mode": mode,
            "source": source_fingerprint,
            "destination": app_fingerprint,
            "tee_heads": sorted(actual_heads),
        }
        if mode == "dry-run":
            return result

        assert expected_count is not None
        assert expected_plan_sha256 is not None
        if mode == "apply":
            operation = preservation.apply_plan(
                source,
                owner,
                plan,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
            )
        else:
            operation = preservation.revert_plan(
                source,
                owner,
                plan,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
            )
        result.update(operation)
        result["mode"] = mode
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revert", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()
    report = run(
        apply=args.apply,
        revert=args.revert,
        confirm=args.confirm,
        expected_count=args.expected_count,
        expected_plan_sha256=args.expected_plan_sha256,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not report.get("ok"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
