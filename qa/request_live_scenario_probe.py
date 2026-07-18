#!/usr/bin/env python3
"""Request one parent-owned live qualification probe and wait for its facts.

This helper is intentionally unprivileged.  A profile agent may create only the
fixed request marker under its own work directory.  The launcher validates that
marker, performs the network mutation in a parent-owned process, and publishes a
private facts copy back to the worker.  Running this helper never creates
authoritative evidence by itself.
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
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from atomic_private_file import AtomicPrivateFileError, create_private_file


LIVE_SCENARIO_IDS = (
    "P0-02",
    "P0-03",
    "P0-04",
    "P0-05",
    "P0-06",
    "P0-07",
    "P0-08",
    "P0-09",
    "P0-10",
    "P0-11",
    "P0-13",
)
RETRYABLE_SCENARIO_IDS = frozenset({"P0-08", "P0-09", "P0-10", "P0-11"})
REQUEST_SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 4096
# The parent caps the longest allowlisted live probe at 1,500 seconds.  Leave a
# full minute for authoritative receipt validation and atomic facts publication
# so the unprivileged helper never races the process it is waiting on.
FACTS_WAIT_SECONDS = 1560.0
FACTS_PUBLISH_GRACE_SECONDS = 2.0
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class LiveProbeRequestError(RuntimeError):
    """A fixed, non-sensitive request-handshake failure."""


def request_path(work_root: Path, scenario_id: str, attempt: int) -> Path:
    return work_root / f".live-probe-{scenario_id}-{attempt}.request"


def facts_path(work_root: Path, scenario_id: str, attempt: int) -> Path:
    return work_root / f"live-probe-{scenario_id}-{attempt}.facts.json"


def _expected_payload(
    *, run_id: str, profile_id: str, scenario_id: str, attempt: int
) -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "run_id": run_id,
        "profile_id": profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
    }


def _validate_identity(
    *, run_id: str, profile_id: str, scenario_id: str, attempt: int
) -> None:
    if (
        not _IDENTIFIER_RE.fullmatch(run_id)
        or not _IDENTIFIER_RE.fullmatch(profile_id)
        or scenario_id not in LIVE_SCENARIO_IDS
        or type(attempt) is not int
        or attempt not in (1, 2)
        or (attempt == 2 and scenario_id not in RETRYABLE_SCENARIO_IDS)
    ):
        raise LiveProbeRequestError("live probe request identity is invalid")


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LiveProbeRequestError("live probe JSON contains duplicate keys")
        result[key] = value
    return result


def _canonical_sha256(value: object) -> str:
    import hashlib

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _owned_private_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise LiveProbeRequestError(f"{label} is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LiveProbeRequestError(f"{label} is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > max_bytes
        ):
            raise LiveProbeRequestError(f"{label} is unsafe")
        content = os.read(descriptor, metadata.st_size + 1)
        if len(content) != metadata.st_size:
            raise LiveProbeRequestError(f"{label} changed while reading")
        return content
    finally:
        os.close(descriptor)


def write_request_marker(
    path: Path,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
) -> None:
    """Create the one-shot marker with O_EXCL and mode 0600."""

    _validate_identity(
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise LiveProbeRequestError("live probe request path is unsafe")
    try:
        parent = path.parent.resolve(strict=True)
        metadata = parent.stat()
    except (OSError, RuntimeError):
        raise LiveProbeRequestError("live probe request parent is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LiveProbeRequestError("live probe request parent is unsafe")
    payload = (
        json.dumps(
            _expected_payload(
                run_id=run_id,
                profile_id=profile_id,
                scenario_id=scenario_id,
                attempt=attempt,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        create_private_file(path, payload)
    except AtomicPrivateFileError:
        raise LiveProbeRequestError("unable to create live probe request") from None


def load_request_marker(
    path: Path,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
) -> dict[str, Any]:
    """Validate a worker marker without trusting any worker-selected fields."""

    _validate_identity(
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    try:
        payload = json.loads(
            _owned_private_file(
                path, "live probe request marker", max_bytes=MAX_REQUEST_BYTES
            ),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LiveProbeRequestError("live probe request marker is invalid") from None
    expected = _expected_payload(
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    if not isinstance(payload, dict) or payload != expected:
        raise LiveProbeRequestError("live probe request marker is invalid")
    return payload


def _load_facts(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            _owned_private_file(path, "live probe facts", max_bytes=8 * 1024 * 1024),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LiveProbeRequestError("live probe facts are invalid") from None
    if not isinstance(payload, dict):
        raise LiveProbeRequestError("live probe facts are invalid")
    return payload


def request_and_wait(
    *,
    scenario_id: str,
    attempt: int,
    request: Path,
    facts: Path,
    environment: Mapping[str, str] | None = None,
    wait_seconds: float = FACTS_WAIT_SECONDS,
) -> Mapping[str, Any]:
    env = os.environ if environment is None else environment
    run_id = str(env.get("QA_RUN_ID") or "")
    profile_id = str(env.get("QA_PROFILE_ID") or "")
    raw_work_root = str(env.get("QA_WORK_ROOT") or "")
    work_root = Path(raw_work_root)
    _validate_identity(
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    if (
        not work_root.is_absolute()
        or work_root.is_symlink()
        or request != request_path(work_root, scenario_id, attempt)
        or facts != facts_path(work_root, scenario_id, attempt)
        or request.exists()
        or facts.exists()
    ):
        raise LiveProbeRequestError("live probe handshake paths are invalid")
    write_request_marker(
        request,
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    deadline = time.monotonic() + wait_seconds
    publish_deadline: float | None = None
    while time.monotonic() < deadline:
        if facts.exists():
            now = time.monotonic()
            if publish_deadline is None:
                publish_deadline = min(
                    deadline, now + FACTS_PUBLISH_GRACE_SECONDS
                )
            try:
                payload = _load_facts(facts)
                receipt = payload.get("receipt")
                private_facts = payload.get("private_facts")
                valid = bool(
                    payload.get("schema_version") == 1
                    and payload.get("profile_id") == profile_id
                    and payload.get("scenario_id") == scenario_id
                    and payload.get("attempt") == attempt
                    and isinstance(receipt, dict)
                    and receipt.get("run_id") == run_id
                    and receipt.get("profile_id") == profile_id
                    and receipt.get("scenario_id") == scenario_id
                    and receipt.get("attempt") == attempt
                    and isinstance(private_facts, dict)
                    and isinstance(payload.get("receipt_sha256"), str)
                    and payload["receipt_sha256"] == _canonical_sha256(receipt)
                    and isinstance(receipt.get("private_facts_sha256"), str)
                    and receipt["private_facts_sha256"]
                    == _canonical_sha256(private_facts)
                )
            except (LiveProbeRequestError, TypeError, ValueError, RecursionError):
                valid = False
            if valid:
                return payload
            if publish_deadline is not None and now >= publish_deadline:
                raise LiveProbeRequestError(
                    "trusted live probe facts are unavailable"
                )
        time.sleep(0.01)
    raise LiveProbeRequestError("trusted live probe timed out")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="request one trusted live QA probe")
    parser.add_argument("--scenario", choices=LIVE_SCENARIO_IDS, required=True)
    parser.add_argument("--attempt", type=int, choices=(1, 2), required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = request_and_wait(
            scenario_id=args.scenario,
            attempt=args.attempt,
            request=args.request,
            facts=args.facts,
        )
    except LiveProbeRequestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    receipt = payload["receipt"]
    print(
        json.dumps(
            {
                "scenario_id": args.scenario,
                "attempt": args.attempt,
                "status": receipt.get("status"),
                "failure_code": receipt.get("failure_code"),
                "facts_path": str(args.facts),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
