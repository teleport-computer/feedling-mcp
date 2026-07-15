#!/usr/bin/env python3
"""Bind the agent's cleanup assertions to deterministic post-run evidence."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.orchestration_contract import (  # noqa: E402
    MEMORY_CONTRACT_PROFILE_ID,
    PROFILE_IDS,
)

MAX_JSON_BYTES = 2 * 1024 * 1024
_TOP_LEVEL_KEYS = {
    "schema_version",
    "kind",
    "run_id",
    "generated_at",
    "attempted",
    "cleaned",
    "failed_profile_ids",
    "manifest_deleted",
    "manifest_retained_for_scan",
    "profiles",
    "auxiliary_accounts",
}
_ROW_KEYS = {
    "profile_id",
    "attempted",
    "reset_response_accepted",
    "provider_config_preexisted",
    "provider_config_live_predelete_observed",
    "provider_config_deleted",
    "key_envelope_deleted",
    "provider_config_deletion_source",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
    "status",
}
_TRUE_FIELDS = (
    "attempted",
    "reset_response_accepted",
    "provider_config_deleted",
    "key_envelope_deleted",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
)


class CleanupReceiptError(RuntimeError):
    """A fixed cleanup-evidence failure safe to print in CI."""


def _read_json(path: Path, kind: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError:
        raise CleanupReceiptError(f"{kind} is missing or unsafe") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_JSON_BYTES
    ):
        raise CleanupReceiptError(f"{kind} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CleanupReceiptError(f"{kind} is not valid JSON") from None
    if not isinstance(value, dict):
        raise CleanupReceiptError(f"{kind} has an invalid shape")
    return value


def _validate_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_rows(
    rows: Any,
    expected_ids: Sequence[str],
    kind: str,
    *,
    provider_config_preexisted: bool,
    allowed_deletion_sources: frozenset[str],
) -> None:
    if not isinstance(rows, list) or len(rows) != len(expected_ids):
        raise CleanupReceiptError(f"cleanup receipt {kind} matrix is incomplete")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise CleanupReceiptError(f"cleanup receipt {kind} row is invalid")
        profile_id = row.get("profile_id")
        if not isinstance(profile_id, str):
            raise CleanupReceiptError(f"cleanup receipt {kind} row is invalid")
        ids.append(profile_id)
        if (
            row.get("status") != "PASS"
            or row.get("provider_config_preexisted") is not provider_config_preexisted
            or not isinstance(row.get("provider_config_live_predelete_observed"), bool)
            or row.get("provider_config_deletion_source")
            not in allowed_deletion_sources
            or (
                row.get("provider_config_live_predelete_observed") is True
                and row.get("provider_config_deletion_source") != "explicit_api"
            )
            or any(row.get(field) is not True for field in _TRUE_FIELDS)
        ):
            raise CleanupReceiptError(f"cleanup receipt {kind} proof is incomplete")
    if ids != list(expected_ids):
        raise CleanupReceiptError(f"cleanup receipt {kind} matrix is not locked")


def validate_cleanup_receipt(receipt_path: Path, result_path: Path) -> None:
    receipt = _read_json(receipt_path, "cleanup receipt")
    result = _read_json(result_path, "canonical run result")
    if set(receipt) != _TOP_LEVEL_KEYS:
        raise CleanupReceiptError("cleanup receipt top-level shape is invalid")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "deterministic_cleanup_receipt"
        or not _validate_timestamp(receipt.get("generated_at"))
        or receipt.get("attempted") != len(PROFILE_IDS) + 1
        or receipt.get("cleaned") != len(PROFILE_IDS) + 1
        or receipt.get("failed_profile_ids") != []
        or receipt.get("manifest_deleted") is not False
        or receipt.get("manifest_retained_for_scan") is not True
    ):
        raise CleanupReceiptError("cleanup receipt summary is incomplete")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not run_id or result.get("run_id") != run_id:
        raise CleanupReceiptError("cleanup receipt run identity does not match")

    _validate_rows(
        receipt.get("profiles"),
        PROFILE_IDS,
        "profile",
        provider_config_preexisted=True,
        allowed_deletion_sources=frozenset({"explicit_api", "account_cascade"}),
    )
    _validate_rows(
        receipt.get("auxiliary_accounts"),
        (MEMORY_CONTRACT_PROFILE_ID,),
        "auxiliary",
        provider_config_preexisted=False,
        allowed_deletion_sources=frozenset({"not_applicable"}),
    )

    profiles = result.get("profiles")
    if not isinstance(profiles, list) or [
        row.get("profile_id") if isinstance(row, Mapping) else None for row in profiles
    ] != list(PROFILE_IDS):
        raise CleanupReceiptError("canonical result profile matrix is not locked")
    for row in profiles:
        cleanup = row.get("cleanup") if isinstance(row, Mapping) else None
        if (
            not isinstance(cleanup, Mapping)
            or cleanup.get("status") != "PASS"
            or any(
                cleanup.get(field) is not True
                for field in (
                    "attempted",
                    "provider_config_deleted",
                    "account_reset",
                    "old_credential_rejected",
                )
            )
        ):
            raise CleanupReceiptError(
                "canonical result cleanup assertions do not match deterministic evidence"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_cleanup_receipt(args.receipt, args.result)
    except CleanupReceiptError as exc:
        print(f"cleanup receipt validation: FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("cleanup receipt validation: FAIL: internal error", file=sys.stderr)
        return 2
    print("cleanup receipt validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
