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
import fcntl
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
REQUEST_SCHEMA_VERSION = 2
MAX_REQUEST_BYTES = 4096
# The parent caps the longest allowlisted live probe at 1,500 seconds.  Leave a
# full minute for authoritative receipt validation and atomic facts publication
# so the unprivileged helper never races the process it is waiting on.
FACTS_WAIT_SECONDS = 1560.0
FACTS_PUBLISH_GRACE_SECONDS = 2.0
# A concurrently started future helper waits outside the lock until its exact
# predecessor finishes.  The gate wait plus one probe wait remains below the
# 3,600-second profile-agent deadline.
SEQUENCE_WAIT_SECONDS = 1800.0
SEQUENCE_POLL_SECONDS = 0.01
SEQUENCE_STATE_SCHEMA_VERSION = 1
MAX_SEQUENCE_STATE_BYTES = 32 * 1024
MAX_PERSONA_JUDGMENT_BYTES = 64 * 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEQUENCE_GATE_NAME = ".live-probe-sequence.lock"
_PERSONA_EVIDENCE_NAME = "p0-06-private-evidence.json"
_PERSONA_JUDGMENT_NAME = "p0-06-semantic-judgment.json"
_COT_FACTS_NAME = "cot-delivery-facts.json"
_RETRYABLE_FAILURE_CODES = frozenset({"CHAT_TIMEOUT", "MISSING_REPLY"})


class LiveProbeRequestError(RuntimeError):
    """A fixed, non-sensitive request-handshake failure."""


def request_path(work_root: Path, scenario_id: str, attempt: int) -> Path:
    return work_root / f".live-probe-{scenario_id}-{attempt}.request"


def facts_path(work_root: Path, scenario_id: str, attempt: int) -> Path:
    return work_root / f"live-probe-{scenario_id}-{attempt}.facts.json"


def sequence_gate_path(work_root: Path) -> Path:
    """Return the fixed owner-only lock/state path for one profile."""

    return work_root / _SEQUENCE_GATE_NAME


def _expected_payload(
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
    previous_receipt_sha256: str | None,
    cot_terminal_sha256: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "run_id": run_id,
        "profile_id": profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "previous_receipt_sha256": previous_receipt_sha256,
        "cot_terminal_sha256": cot_terminal_sha256,
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
    previous_receipt_sha256: str | None,
    cot_terminal_sha256: str | None = None,
) -> None:
    """Create the one-shot marker with O_EXCL and mode 0600."""

    _validate_identity(
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    if previous_receipt_sha256 is not None and not _SHA256_RE.fullmatch(
        previous_receipt_sha256
    ):
        raise LiveProbeRequestError("live probe predecessor digest is invalid")
    if cot_terminal_sha256 is not None and not _SHA256_RE.fullmatch(
        cot_terminal_sha256
    ):
        raise LiveProbeRequestError("COT terminal digest is invalid")
    if (scenario_id == "P0-13") != (cot_terminal_sha256 is not None):
        raise LiveProbeRequestError("COT terminal digest binding is invalid")
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
                previous_receipt_sha256=previous_receipt_sha256,
                cot_terminal_sha256=cot_terminal_sha256,
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
    previous_receipt_sha256: str | None,
    cot_terminal_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a worker marker without trusting any worker-selected fields."""

    _validate_identity(
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    if previous_receipt_sha256 is not None and not _SHA256_RE.fullmatch(
        previous_receipt_sha256
    ):
        raise LiveProbeRequestError("live probe predecessor digest is invalid")
    if cot_terminal_sha256 is not None and not _SHA256_RE.fullmatch(
        cot_terminal_sha256
    ):
        raise LiveProbeRequestError("COT terminal digest is invalid")
    if (scenario_id == "P0-13") != (cot_terminal_sha256 is not None):
        raise LiveProbeRequestError("COT terminal digest binding is invalid")
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
        previous_receipt_sha256=previous_receipt_sha256,
        cot_terminal_sha256=cot_terminal_sha256,
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


def _validated_facts(
    path: Path,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
) -> Mapping[str, Any]:
    payload = _load_facts(path)
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
        and receipt["private_facts_sha256"] == _canonical_sha256(private_facts)
    )
    if not valid:
        raise LiveProbeRequestError("trusted live probe facts are invalid")
    return payload


def _validate_work_root(work_root: Path) -> None:
    if not work_root.is_absolute() or work_root.is_symlink():
        raise LiveProbeRequestError("live probe work root is unsafe")
    try:
        resolved = work_root.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise LiveProbeRequestError("live probe work root is unavailable") from None
    if (
        resolved != work_root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LiveProbeRequestError("live probe work root is unsafe")


def _sequence_inode_is_safe(descriptor: int, path: Path) -> bool:
    try:
        opened = os.fstat(descriptor)
        linked = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(
        stat.S_ISREG(opened.st_mode)
        and opened.st_uid == os.geteuid()
        and stat.S_IMODE(opened.st_mode) == 0o600
        and opened.st_nlink == 1
        and stat.S_ISREG(linked.st_mode)
        and linked.st_uid == os.geteuid()
        and stat.S_IMODE(linked.st_mode) == 0o600
        and linked.st_nlink == 1
        and (opened.st_dev, opened.st_ino) == (linked.st_dev, linked.st_ino)
    )


def _open_sequence_gate(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise LiveProbeRequestError("live probe sequence gate is unavailable") from None
    if not _sequence_inode_is_safe(descriptor, path):
        os.close(descriptor)
        raise LiveProbeRequestError("live probe sequence gate is unsafe")
    return descriptor


def _read_sequence_state(
    descriptor: int,
    path: Path,
    *,
    run_id: str,
    profile_id: str,
) -> dict[str, Any]:
    if not _sequence_inode_is_safe(descriptor, path):
        raise LiveProbeRequestError("live probe sequence gate is unsafe")
    before = os.fstat(descriptor)
    if before.st_size == 0:
        return {
            "schema_version": SEQUENCE_STATE_SCHEMA_VERSION,
            "run_id": run_id,
            "profile_id": profile_id,
            "completed": [],
        }
    if before.st_size > MAX_SEQUENCE_STATE_BYTES:
        raise LiveProbeRequestError("live probe sequence state is unsafe")
    os.lseek(descriptor, 0, os.SEEK_SET)
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
        getattr(before, field) != getattr(after, field) for field in stable_fields
    ):
        raise LiveProbeRequestError("live probe sequence state changed while reading")
    try:
        payload = json.loads(
            content,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LiveProbeRequestError("live probe sequence state is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema_version", "run_id", "profile_id", "completed"}
        or payload.get("schema_version") != SEQUENCE_STATE_SCHEMA_VERSION
        or payload.get("run_id") != run_id
        or payload.get("profile_id") != profile_id
        or not isinstance(payload.get("completed"), list)
        or len(payload["completed"]) > len(LIVE_SCENARIO_IDS) + len(RETRYABLE_SCENARIO_IDS)
    ):
        raise LiveProbeRequestError("live probe sequence state is invalid")
    return payload


def _write_sequence_state(
    descriptor: int, path: Path, state: Mapping[str, Any]
) -> None:
    try:
        content = (
            json.dumps(
                state,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise LiveProbeRequestError("live probe sequence state is invalid") from None
    if len(content) > MAX_SEQUENCE_STATE_BYTES or not _sequence_inode_is_safe(
        descriptor, path
    ):
        raise LiveProbeRequestError("live probe sequence gate is unsafe")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("sequence state write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, len(content) + 1) != content:
            raise OSError("sequence state verification failed")
    except OSError:
        raise LiveProbeRequestError("unable to write live probe sequence state") from None
    if not _sequence_inode_is_safe(descriptor, path):
        raise LiveProbeRequestError("live probe sequence gate is unsafe")


def _next_sequence_key(
    scenario_id: str, attempt: int, receipt: Mapping[str, Any]
) -> tuple[str, int] | None:
    if (
        attempt == 1
        and scenario_id in RETRYABLE_SCENARIO_IDS
        and receipt.get("status") == "AGENT_ERROR"
        and receipt.get("failure_code") in _RETRYABLE_FAILURE_CODES
    ):
        return scenario_id, 2
    index = LIVE_SCENARIO_IDS.index(scenario_id)
    if index + 1 == len(LIVE_SCENARIO_IDS):
        return None
    return LIVE_SCENARIO_IDS[index + 1], 1


def _replay_sequence_state(
    work_root: Path,
    state: Mapping[str, Any],
    *,
    run_id: str,
    profile_id: str,
) -> tuple[tuple[str, int] | None, dict[tuple[str, int], Mapping[str, Any]]]:
    expected: tuple[str, int] | None = (LIVE_SCENARIO_IDS[0], 1)
    facts_by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    for raw_entry in state["completed"]:
        if (
            not isinstance(raw_entry, dict)
            or set(raw_entry) != {"scenario_id", "attempt", "receipt_sha256"}
            or not isinstance(raw_entry.get("scenario_id"), str)
            or type(raw_entry.get("attempt")) is not int
            or not isinstance(raw_entry.get("receipt_sha256"), str)
            or not _SHA256_RE.fullmatch(raw_entry["receipt_sha256"])
        ):
            raise LiveProbeRequestError("live probe sequence state is invalid")
        key = (raw_entry["scenario_id"], raw_entry["attempt"])
        if expected is None or key != expected:
            raise LiveProbeRequestError("live probe sequence state is out of order")
        payload = _validated_facts(
            facts_path(work_root, *key),
            run_id=run_id,
            profile_id=profile_id,
            scenario_id=key[0],
            attempt=key[1],
        )
        if payload["receipt_sha256"] != raw_entry["receipt_sha256"]:
            raise LiveProbeRequestError("live probe sequence state is inconsistent")
        facts_by_key[key] = payload
        expected = _next_sequence_key(key[0], key[1], payload["receipt"])
    return expected, facts_by_key


def _handshake_presence(path: Path) -> str:
    if path.is_symlink():
        return "unsafe"
    return "present" if path.exists() else "absent"


def _reconcile_completed_facts(
    work_root: Path,
    state: dict[str, Any],
    gate_path: Path,
    descriptor: int,
    *,
    run_id: str,
    profile_id: str,
) -> tuple[tuple[str, int] | None, dict[tuple[str, int], Mapping[str, Any]]]:
    """Recover only a fully published expected receipt after helper death."""

    while True:
        expected, facts_by_key = _replay_sequence_state(
            work_root,
            state,
            run_id=run_id,
            profile_id=profile_id,
        )
        completed = set(facts_by_key)
        unexpected = []
        for candidate_scenario in LIVE_SCENARIO_IDS:
            for candidate_attempt in (1, 2):
                key = (candidate_scenario, candidate_attempt)
                if key in completed or key == expected:
                    continue
                candidate_request = request_path(work_root, *key)
                candidate_facts = facts_path(work_root, *key)
                if (
                    _handshake_presence(candidate_request) != "absent"
                    or _handshake_presence(candidate_facts) != "absent"
                ):
                    unexpected.append(key)
        if unexpected:
            raise LiveProbeRequestError("live probe handshake is out of order")
        if expected is None:
            return expected, facts_by_key
        expected_request = request_path(work_root, *expected)
        expected_facts = facts_path(work_root, *expected)
        request_presence = _handshake_presence(expected_request)
        facts_presence = _handshake_presence(expected_facts)
        if "unsafe" in {request_presence, facts_presence}:
            raise LiveProbeRequestError("live probe predecessor is unsafe")
        if request_presence == facts_presence == "absent":
            return expected, facts_by_key
        if request_presence != "present" or facts_presence != "present":
            raise LiveProbeRequestError("live probe predecessor is incomplete")
        load_request_marker(
            expected_request,
            run_id=run_id,
            profile_id=profile_id,
            scenario_id=expected[0],
            attempt=expected[1],
            previous_receipt_sha256=(
                state["completed"][-1]["receipt_sha256"]
                if state["completed"]
                else None
            ),
            cot_terminal_sha256=(
                _cot_predecessor_digest(work_root, profile_id)
                if expected[0] == "P0-13"
                else None
            ),
        )
        payload = _validated_facts(
            expected_facts,
            run_id=run_id,
            profile_id=profile_id,
            scenario_id=expected[0],
            attempt=expected[1],
        )
        state["completed"].append(
            {
                "scenario_id": expected[0],
                "attempt": expected[1],
                "receipt_sha256": payload["receipt_sha256"],
            }
        )
        _write_sequence_state(descriptor, gate_path, state)


def _private_path_state(path: Path, label: str, *, max_bytes: int) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise LiveProbeRequestError(f"{label} is unsafe")
    if not path.exists():
        return "absent"
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
        return "present"
    finally:
        os.close(descriptor)


def _persona_predecessor_ready(
    work_root: Path,
    facts_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
) -> bool:
    capture = facts_by_key.get(("P0-06", 1))
    if not isinstance(capture, Mapping) or not isinstance(capture.get("receipt"), dict):
        raise LiveProbeRequestError("persona capture predecessor is unavailable")
    evidence_state = _private_path_state(
        work_root / _PERSONA_EVIDENCE_NAME,
        "persona review evidence",
        max_bytes=8 * 1024 * 1024,
    )
    judgment_state = _private_path_state(
        work_root / _PERSONA_JUDGMENT_NAME,
        "persona semantic judgment",
        max_bytes=MAX_PERSONA_JUDGMENT_BYTES,
    )
    if capture["receipt"].get("status") != "PASS":
        if evidence_state != "absent" or judgment_state != "absent":
            raise LiveProbeRequestError("failed persona capture has review artifacts")
        return True
    if evidence_state == "absent" and judgment_state == "absent":
        raise LiveProbeRequestError("persona review predecessor is incomplete")
    return evidence_state == "absent" and judgment_state == "present"


def _cot_predecessor_digest(work_root: Path, profile_id: str) -> str | None:
    try:
        from qa.request_cot_delivery_probe import (
            CotProbeRequestError,
            _load_facts as load_cot_delivery_facts,
        )
    except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
        from request_cot_delivery_probe import (  # type: ignore[no-redef]
            CotProbeRequestError,
            _load_facts as load_cot_delivery_facts,
        )

    path = work_root / _COT_FACTS_NAME
    if path.is_symlink():
        raise LiveProbeRequestError("COT predecessor facts are unsafe")
    if not path.exists():
        return None
    try:
        payload = load_cot_delivery_facts(
            path, profile_id, allow_unavailable=True
        )
    except CotProbeRequestError:
        return None
    terminal_sha256 = payload.get("terminal_sha256")
    return terminal_sha256 if isinstance(terminal_sha256, str) else None


def _sequence_rank(key: tuple[str, int]) -> tuple[int, int]:
    return LIVE_SCENARIO_IDS.index(key[0]), key[1]


def _acquire_sequence_turn(
    work_root: Path,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
    wait_seconds: float,
) -> tuple[int, dict[str, Any], str | None]:
    gate_path = sequence_gate_path(work_root)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    requested = (scenario_id, attempt)
    while True:
        descriptor = _open_sequence_gate(gate_path)
        locked = False
        should_wait = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                should_wait = True
            if locked:
                state = _read_sequence_state(
                    descriptor,
                    gate_path,
                    run_id=run_id,
                    profile_id=profile_id,
                )
                expected, facts_by_key = _reconcile_completed_facts(
                    work_root,
                    state,
                    gate_path,
                    descriptor,
                    run_id=run_id,
                    profile_id=profile_id,
                )
                if expected is None or _sequence_rank(requested) < _sequence_rank(
                    expected
                ):
                    raise LiveProbeRequestError(
                        "live probe request is duplicate or out of order"
                    )
                if requested != expected:
                    should_wait = True
                elif scenario_id == "P0-07" and not _persona_predecessor_ready(
                    work_root, facts_by_key
                ):
                    should_wait = True
                elif scenario_id == "P0-13":
                    cot_terminal_sha256 = _cot_predecessor_digest(
                        work_root, profile_id
                    )
                    if cot_terminal_sha256 is None:
                        should_wait = True
                    else:
                        return descriptor, state, cot_terminal_sha256
                else:
                    return descriptor, state, None
        except Exception:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        if not should_wait or time.monotonic() >= deadline:
            raise LiveProbeRequestError("live probe sequence wait timed out")
        time.sleep(SEQUENCE_POLL_SECONDS)


def acquire_sequence_phase_gate(
    work_root: Path,
    *,
    run_id: str,
    profile_id: str,
    phase: str,
    wait_seconds: float = SEQUENCE_WAIT_SECONDS,
) -> int:
    """Hold the profile gate through an exact non-live phase process exit.

    The returned raw descriptor intentionally remains open.  FINALIZE and
    P0-12 CLI entrypoints retain it until their process exits, so a concurrently
    launched P0-07 or P0-13 command cannot complete first.
    """

    expected_for_phase = {
        "P0-06-FINALIZE": ("P0-07", 1),
        "P0-12": ("P0-13", 1),
    }
    desired = expected_for_phase.get(phase)
    if (
        desired is None
        or not _IDENTIFIER_RE.fullmatch(run_id)
        or not _IDENTIFIER_RE.fullmatch(profile_id)
    ):
        raise LiveProbeRequestError("live probe sequence phase is invalid")
    _validate_work_root(work_root)
    gate_path = sequence_gate_path(work_root)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        descriptor = _open_sequence_gate(gate_path)
        locked = False
        should_wait = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                should_wait = True
            if locked:
                state = _read_sequence_state(
                    descriptor,
                    gate_path,
                    run_id=run_id,
                    profile_id=profile_id,
                )
                expected, facts_by_key = _reconcile_completed_facts(
                    work_root,
                    state,
                    gate_path,
                    descriptor,
                    run_id=run_id,
                    profile_id=profile_id,
                )
                if expected is None or _sequence_rank(expected) > _sequence_rank(
                    desired
                ):
                    raise LiveProbeRequestError(
                        "live probe sequence phase is duplicate or out of order"
                    )
                if expected != desired:
                    should_wait = True
                elif phase == "P0-06-FINALIZE":
                    capture = facts_by_key.get(("P0-06", 1))
                    if (
                        not isinstance(capture, Mapping)
                        or not isinstance(capture.get("receipt"), dict)
                        or capture["receipt"].get("status") != "PASS"
                    ):
                        raise LiveProbeRequestError(
                            "persona finalizer predecessor is invalid"
                        )
                    evidence_state = _private_path_state(
                        work_root / _PERSONA_EVIDENCE_NAME,
                        "persona review evidence",
                        max_bytes=8 * 1024 * 1024,
                    )
                    judgment_state = _private_path_state(
                        work_root / _PERSONA_JUDGMENT_NAME,
                        "persona semantic judgment",
                        max_bytes=MAX_PERSONA_JUDGMENT_BYTES,
                    )
                    if evidence_state == "absent":
                        raise LiveProbeRequestError(
                            "persona finalizer phase is duplicate or incomplete"
                        )
                    if judgment_state != "present":
                        should_wait = True
                    else:
                        return descriptor
                elif (
                    _handshake_presence(work_root / ".cot-probe-request")
                    != "absent"
                    or _handshake_presence(work_root / _COT_FACTS_NAME) != "absent"
                ):
                    raise LiveProbeRequestError(
                        "COT sequence phase is duplicate or out of order"
                    )
                else:
                    return descriptor
        except Exception:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        if not should_wait or time.monotonic() >= deadline:
            raise LiveProbeRequestError("live probe sequence phase timed out")
        time.sleep(SEQUENCE_POLL_SECONDS)


def _release_sequence_turn(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def request_and_wait(
    *,
    scenario_id: str,
    attempt: int,
    request: Path,
    facts: Path,
    environment: Mapping[str, str] | None = None,
    wait_seconds: float = FACTS_WAIT_SECONDS,
    sequence_wait_seconds: float = SEQUENCE_WAIT_SECONDS,
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
    _validate_work_root(work_root)
    if (
        request != request_path(work_root, scenario_id, attempt)
        or facts != facts_path(work_root, scenario_id, attempt)
        or request.is_symlink()
        or facts.is_symlink()
        or request.exists()
        or facts.exists()
    ):
        raise LiveProbeRequestError("live probe handshake paths are invalid")
    descriptor, state, cot_terminal_sha256 = _acquire_sequence_turn(
        work_root,
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
        wait_seconds=sequence_wait_seconds,
    )
    try:
        write_request_marker(
            request,
            run_id=run_id,
            profile_id=profile_id,
            scenario_id=scenario_id,
            attempt=attempt,
            previous_receipt_sha256=(
                state["completed"][-1]["receipt_sha256"]
                if state["completed"]
                else None
            ),
            cot_terminal_sha256=cot_terminal_sha256,
        )
        deadline = time.monotonic() + max(0.0, wait_seconds)
        publish_deadline: float | None = None
        while time.monotonic() < deadline:
            if facts.exists() or facts.is_symlink():
                now = time.monotonic()
                if publish_deadline is None:
                    publish_deadline = min(
                        deadline, now + FACTS_PUBLISH_GRACE_SECONDS
                    )
                try:
                    payload = _validated_facts(
                        facts,
                        run_id=run_id,
                        profile_id=profile_id,
                        scenario_id=scenario_id,
                        attempt=attempt,
                    )
                except (LiveProbeRequestError, TypeError, ValueError, RecursionError):
                    payload = None
                if payload is not None:
                    state["completed"].append(
                        {
                            "scenario_id": scenario_id,
                            "attempt": attempt,
                            "receipt_sha256": payload["receipt_sha256"],
                        }
                    )
                    _write_sequence_state(
                        descriptor,
                        sequence_gate_path(work_root),
                        state,
                    )
                    return payload
                if publish_deadline is not None and now >= publish_deadline:
                    raise LiveProbeRequestError(
                        "trusted live probe facts are unavailable"
                    )
            time.sleep(0.01)
        raise LiveProbeRequestError("trusted live probe timed out")
    finally:
        _release_sequence_turn(descriptor)


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
