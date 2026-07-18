#!/usr/bin/env python3
"""Request the parent-owned P0-12 probe and validate its bounded facts copy.

This helper is intentionally unprivileged.  It can create only the fixed
profile marker in the worker's private work root.  The trusted parent performs
the network operation and publishes a sanitized facts envelope.  A completed
PASS, FAIL, or UNVERIFIED receipt is a successful observation; unavailable or
malformed protocol facts are an operational failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from qa.atomic_private_file import AtomicPrivateFileError, create_private_file
    from qa.request_live_scenario_probe import (
        LiveProbeRequestError,
        acquire_sequence_phase_gate,
    )
    from qa.validate_cot_receipt import (
        CotReceiptError,
        validate_cot_receipt_document,
    )
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from atomic_private_file import AtomicPrivateFileError, create_private_file
    from request_live_scenario_probe import (  # type: ignore[no-redef]
        LiveProbeRequestError,
        acquire_sequence_phase_gate,
    )
    from validate_cot_receipt import CotReceiptError, validate_cot_receipt_document


FACTS_SCHEMA_VERSION = 1
MAX_FACTS_BYTES = 128 * 1024
FACTS_WAIT_SECONDS = 360.0
FACTS_PUBLISH_GRACE_SECONDS = 2.0
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUCCESS_FACT_KEYS = frozenset(
    {"schema_version", "profile_id", "receipt_sha256", "receipt"}
)
_UNAVAILABLE_FACT_KEYS = frozenset(
    {"schema_version", "profile_id", "receipt_sha256", "status", "failure_code"}
)


class CotProbeRequestError(RuntimeError):
    """A fixed, non-sensitive P0-12 request-handshake failure."""


class CotProbeUnavailableError(CotProbeRequestError):
    """The trusted parent explicitly could not produce protocol evidence."""


def request_path(work_root: Path) -> Path:
    return work_root / ".cot-probe-request"


def facts_path(work_root: Path) -> Path:
    return work_root / "cot-delivery-facts.json"


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CotProbeRequestError("COT delivery facts contain duplicate keys")
        result[key] = value
    return result


def _owned_private_file(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise CotProbeRequestError("COT delivery facts path is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CotProbeRequestError("COT delivery facts are unavailable") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_FACTS_BYTES
        ):
            raise CotProbeRequestError("COT delivery facts are unsafe")
        content = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(content) != before.st_size or any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ):
            raise CotProbeRequestError("COT delivery facts changed while reading")
        return content
    finally:
        os.close(descriptor)


def _validate_work_root(work_root: Path) -> None:
    if not work_root.is_absolute() or work_root.is_symlink():
        raise CotProbeRequestError("COT request work root is unsafe")
    try:
        resolved = work_root.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise CotProbeRequestError("COT request work root is unavailable") from None
    if (
        resolved != work_root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CotProbeRequestError("COT request work root is unsafe")


def write_request_marker(path: Path, profile_id: str) -> None:
    """Create the fixed one-shot profile marker with mode 0600."""

    if not _IDENTIFIER_RE.fullmatch(profile_id):
        raise CotProbeRequestError("COT request profile identity is invalid")
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise CotProbeRequestError("COT request marker path is unsafe")
    try:
        create_private_file(path, f"{profile_id}\n".encode("utf-8"))
    except AtomicPrivateFileError:
        raise CotProbeRequestError("unable to create COT request marker") from None


def _load_facts(path: Path, profile_id: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            _owned_private_file(path),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except CotProbeRequestError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CotProbeRequestError("COT delivery facts are invalid") from None
    if not isinstance(payload, dict):
        raise CotProbeRequestError("COT delivery facts are invalid")

    if set(payload) == _UNAVAILABLE_FACT_KEYS:
        if payload != {
            "schema_version": FACTS_SCHEMA_VERSION,
            "profile_id": profile_id,
            "receipt_sha256": None,
            "status": "UNAVAILABLE",
            "failure_code": "TRUSTED_PROBE_ERROR",
        }:
            raise CotProbeRequestError("COT unavailable facts are invalid")
        raise CotProbeUnavailableError("trusted COT probe evidence is unavailable")

    if (
        set(payload) != _SUCCESS_FACT_KEYS
        or payload.get("schema_version") != FACTS_SCHEMA_VERSION
        or payload.get("profile_id") != profile_id
        or not isinstance(payload.get("receipt_sha256"), str)
        or not _SHA256_RE.fullmatch(payload["receipt_sha256"])
    ):
        raise CotProbeRequestError("COT delivery facts are invalid")
    try:
        receipt, digest = validate_cot_receipt_document(
            payload.get("receipt"), profile_id
        )
    except CotReceiptError:
        raise CotProbeRequestError("COT delivery receipt is invalid") from None
    if payload["receipt_sha256"] != digest or receipt.get("status") not in {
        "PASS",
        "FAIL",
        "UNVERIFIED",
    }:
        raise CotProbeRequestError("COT delivery receipt binding is invalid")
    return payload


def request_and_wait(
    *,
    request: Path,
    facts: Path,
    environment: Mapping[str, str] | None = None,
    wait_seconds: float = FACTS_WAIT_SECONDS,
) -> Mapping[str, Any]:
    env = os.environ if environment is None else environment
    profile_id = str(env.get("QA_PROFILE_ID") or "")
    raw_work_root = str(env.get("QA_WORK_ROOT") or "")
    work_root = Path(raw_work_root)
    _validate_work_root(work_root)
    if (
        not _IDENTIFIER_RE.fullmatch(profile_id)
        or request != request_path(work_root)
        or facts != facts_path(work_root)
        or request.exists()
        or request.is_symlink()
        or facts.exists()
        or facts.is_symlink()
    ):
        raise CotProbeRequestError("COT handshake paths are invalid")
    write_request_marker(request, profile_id)

    deadline = time.monotonic() + wait_seconds
    publish_deadline: float | None = None
    while time.monotonic() < deadline:
        if facts.exists() or facts.is_symlink():
            now = time.monotonic()
            if publish_deadline is None:
                publish_deadline = min(
                    deadline, now + FACTS_PUBLISH_GRACE_SECONDS
                )
            try:
                return _load_facts(facts, profile_id)
            except CotProbeUnavailableError:
                raise
            except CotProbeRequestError:
                if publish_deadline is not None and now >= publish_deadline:
                    raise CotProbeRequestError(
                        "trusted COT probe facts are unavailable"
                    ) from None
        time.sleep(0.01)
    raise CotProbeRequestError("trusted COT probe timed out")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="request the trusted P0-12 probe")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    gate_descriptor: int | None = None
    try:
        run_id = str(os.environ.get("QA_RUN_ID") or "")
        profile_id = str(os.environ.get("QA_PROFILE_ID") or "")
        raw_work_root = str(os.environ.get("QA_WORK_ROOT") or "")
        sequence_identity = (run_id, profile_id, raw_work_root)
        if any(sequence_identity):
            if not all(sequence_identity):
                raise LiveProbeRequestError(
                    "COT sequence identity is incomplete"
                )
            work_root = Path(raw_work_root)
            if args.request.parent != work_root or args.facts.parent != work_root:
                raise LiveProbeRequestError("COT sequence work root is inconsistent")
            gate_descriptor = acquire_sequence_phase_gate(
                work_root,
                run_id=run_id,
                profile_id=profile_id,
                phase="P0-12",
            )
        payload = request_and_wait(request=args.request, facts=args.facts)
    except LiveProbeRequestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except CotProbeUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except CotProbeRequestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    receipt = payload["receipt"]
    print(
        json.dumps(
            {
                "scenario_id": "P0-12",
                "status": receipt["status"],
                "failure_code": receipt["failure_code"],
                "facts_path": str(args.facts),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # Intentionally leave the descriptor open until this exact P0-12 process
    # exits; P0-13 cannot acquire the shared profile gate before then.
    _ = gate_descriptor
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
