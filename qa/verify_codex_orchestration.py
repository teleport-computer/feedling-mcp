#!/usr/bin/env python3
"""Verify trusted receipts from eight independent top-level Codex processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Sequence

try:
    from qa.orchestration_contract import PROFILE_AGENT_TYPES
    from qa.validate_cot_receipt import CotReceiptError, validate_cot_receipt
    from qa.validate_live_scenario_receipts import (
        LiveScenarioReceiptError,
        validate_live_scenario_receipts,
        validate_result_binding as validate_live_result_binding,
    )
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from orchestration_contract import PROFILE_AGENT_TYPES
    from validate_cot_receipt import CotReceiptError, validate_cot_receipt
    from validate_live_scenario_receipts import (
        LiveScenarioReceiptError,
        validate_live_scenario_receipts,
        validate_result_binding as validate_live_result_binding,
    )


RECEIPT_SCHEMA_VERSION = 4
MAX_CONFIGURED_CONCURRENCY = 3
WORKER_FILES = frozenset(
    (
        "events.jsonl",
        "result.json",
        "schema.json",
        "stderr.log",
        "cot-delivery-receipt.json",
        "live-scenario-receipts.json",
    )
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_MAX_SCHEMA_BYTES = 8 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024 * 1024
_MAX_JSON_LINE_BYTES = 16 * 1024 * 1024
AGENT_LIVE_SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(2, 14))
MIN_SCENARIO_COMMAND_COUNTS = {
    scenario_id: (3 if scenario_id == "P0-06" else 1)
    for scenario_id in AGENT_LIVE_SCENARIO_IDS
}
MANDATORY_SOP_READ_COMMAND = 'sed -n \'1,999p\' "$QA_SOURCE_ROOT/qa/SOP.md"'
_SCENARIO_COMMAND_RE = re.compile(
    r"^QA_SCENARIO_ID=(P0-(?:0[2-9]|1[0-3]))(?:[ \t]|$)"
)
_RAW_SCENARIO_COMMAND_RE = re.compile(
    r"^QA_SCENARIO_ID=(P0-(?:0[2-9]|1[0-3]))(?:[ \t]|$)"
)
P0_06_COMMAND_PHASES = ("CAPTURE", "REVIEW", "FINALIZE")
_CODEX_SHELLS = frozenset(("/bin/bash", "/bin/sh", "/bin/zsh"))
_SHELL_CONTROL_PUNCTUATION = ";&|<>()"
P0_06_REVIEW_PROGRAM = (
    "import hashlib,json,pathlib,sys;p=pathlib.Path(sys.argv[1]);"
    "j=pathlib.Path(sys.argv[2]);"
    "j.exists() and sys.exit(17);"
    "b=p.read_bytes();d=json.loads(b);"
    "isinstance(d,dict) or sys.exit(18);"
    '"evidence_sha256" not in d or sys.exit(19);'
    'd["evidence_sha256"]=hashlib.sha256(b).hexdigest();'
    'print(json.dumps(d,sort_keys=True,separators=(",",":")))'
)
_P0_06_EVIDENCE_PATH = "$QA_WORK_ROOT/p0-06-private-evidence.json"
_P0_06_JUDGMENT_PATH = "$QA_WORK_ROOT/p0-06-semantic-judgment.json"
_P0_06_CAPTURE_TOKENS = (
    "$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py",
    "--scenario",
    "P0-06",
    "--attempt",
    "1",
    "--request",
    "$QA_WORK_ROOT/.live-probe-P0-06-1.request",
    "--facts",
    "$QA_WORK_ROOT/live-probe-P0-06-1.facts.json",
)
_P0_06_REVIEW_TOKENS = (
    "-I",
    "-B",
    "-c",
    P0_06_REVIEW_PROGRAM,
    _P0_06_EVIDENCE_PATH,
    _P0_06_JUDGMENT_PATH,
)
_P0_06_FINALIZE_TOKENS = (
    "-I",
    "-B",
    "$QA_SOURCE_ROOT/qa/finalize_persona_review.py",
    "--fixture",
    "$QA_SOURCE_ROOT/qa/fixtures/persona-import-v1.json",
    "--private-evidence",
    _P0_06_EVIDENCE_PATH,
    "--semantic-judgment",
    _P0_06_JUDGMENT_PATH,
    "--artifact-dir",
    "$QA_ARTIFACT_DIR",
)
_P0_12_REQUEST_TOKENS = (
    "QA_SCENARIO_ID=P0-12",
    "$QA_PYTHON_BIN",
    "$QA_SOURCE_ROOT/qa/request_cot_delivery_probe.py",
    "--request",
    "$QA_WORK_ROOT/.cot-probe-request",
    "--facts",
    "$QA_WORK_ROOT/cot-delivery-facts.json",
)
_PARENT_LIVE_SCENARIO_IDS = tuple(
    scenario_id
    for scenario_id in AGENT_LIVE_SCENARIO_IDS
    if scenario_id not in {"P0-06", "P0-12"}
)
_RETRYABLE_LIVE_SCENARIO_IDS = frozenset({"P0-08", "P0-09", "P0-10", "P0-11"})


def _live_request_tokens(scenario_id: str, attempt: int) -> tuple[str, ...]:
    return (
        f"QA_SCENARIO_ID={scenario_id}",
        "$QA_PYTHON_BIN",
        "$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py",
        "--scenario",
        scenario_id,
        "--attempt",
        str(attempt),
        "--request",
        f"$QA_WORK_ROOT/.live-probe-{scenario_id}-{attempt}.request",
        "--facts",
        f"$QA_WORK_ROOT/live-probe-{scenario_id}-{attempt}.facts.json",
    )


def live_request_command(scenario_id: str, attempt: int = 1) -> str:
    """Return the exact agent-visible command for one parent-owned probe."""

    if (
        scenario_id not in _PARENT_LIVE_SCENARIO_IDS
        or attempt not in (1, 2)
        or (attempt == 2 and scenario_id not in _RETRYABLE_LIVE_SCENARIO_IDS)
    ):
        raise ValueError("unsupported live request command")
    return " ".join(
        (
            f"QA_SCENARIO_ID={scenario_id}",
            '"$QA_PYTHON_BIN"',
            '"$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py"',
            "--scenario",
            scenario_id,
            "--attempt",
            str(attempt),
            "--request",
            f'"$QA_WORK_ROOT/.live-probe-{scenario_id}-{attempt}.request"',
            "--facts",
            f'"$QA_WORK_ROOT/live-probe-{scenario_id}-{attempt}.facts.json"',
        )
    )


def cot_request_command() -> str:
    """Return the sole exact agent-visible P0-12 request command."""

    return " ".join(
        (
            "QA_SCENARIO_ID=P0-12",
            '"$QA_PYTHON_BIN"',
            '"$QA_SOURCE_ROOT/qa/request_cot_delivery_probe.py"',
            "--request",
            '"$QA_WORK_ROOT/.cot-probe-request"',
            "--facts",
            '"$QA_WORK_ROOT/cot-delivery-facts.json"',
        )
    )


class OrchestrationError(RuntimeError):
    """Sanitized deterministic-verifier failure."""


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def owned_directory(path: Path, label: str, *, empty: bool = False) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise OrchestrationError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        entries = list(path.iterdir()) if empty else None
    except (OSError, RuntimeError):
        raise OrchestrationError(f"{label} is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (empty and entries)
    ):
        raise OrchestrationError(f"{label} is unsafe")
    return resolved


def open_owned_regular(
    path: Path, label: str, *, max_bytes: int, require_mode: int = 0o600
) -> BinaryIO:
    if not path.is_absolute() or path.is_symlink():
        raise OrchestrationError(f"{label} is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise OrchestrationError(f"{label} is unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != require_mode
            or metadata.st_size > max_bytes
        ):
            raise OrchestrationError(f"{label} is unsafe")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def file_sha256(path: Path, label: str, *, max_bytes: int) -> str:
    digest = hashlib.sha256()
    with open_owned_regular(path, label, max_bytes=max_bytes) as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    """Hash a JSON object independently of whitespace and object key order."""

    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise OrchestrationError("canonical worker result is invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def load_private_json(path: Path, label: str, *, max_bytes: int) -> dict[str, Any]:
    try:
        with open_owned_regular(path, label, max_bytes=max_bytes) as handle:
            payload = json.load(handle)
    except OrchestrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise OrchestrationError(f"{label} is invalid") from None
    if not isinstance(payload, dict):
        raise OrchestrationError(f"{label} is invalid")
    return payload


def _json_lines(path: Path, label: str) -> Iterable[dict[str, Any]]:
    try:
        with open_owned_regular(path, label, max_bytes=_MAX_EVENTS_BYTES) as handle:
            for raw in handle:
                if len(raw) > _MAX_JSON_LINE_BYTES:
                    raise OrchestrationError(f"{label} contains an oversized row")
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError, RecursionError):
                    raise OrchestrationError(f"{label} is invalid") from None
                if not isinstance(row, dict):
                    raise OrchestrationError(f"{label} is invalid")
                yield row
    except OrchestrationError:
        raise
    except OSError:
        raise OrchestrationError(f"{label} is unreadable") from None


def parse_exec_events(path: Path) -> tuple[str, str | None]:
    """Return the single completed root thread/session identity."""

    threads: list[str] = []
    sessions: list[str] = []
    turn_started = 0
    turn_completed = 0
    failed = False
    for row in _json_lines(path, "Codex worker event stream"):
        row_type = row.get("type")
        if row_type == "thread.started":
            thread_id = row.get("thread_id")
            if not _valid_identifier(thread_id):
                raise OrchestrationError("Codex worker event identity is invalid")
            threads.append(thread_id)
            session_id = row.get("session_id")
            if session_id is not None:
                if not _valid_identifier(session_id):
                    raise OrchestrationError("Codex worker session identity is invalid")
                sessions.append(session_id)
        elif row_type == "turn.started":
            turn_started += 1
        elif row_type == "turn.completed":
            turn_completed += 1
        elif row_type in ("turn.failed", "error"):
            failed = True
        if row_type in ("item.started", "item.completed"):
            item = row.get("item")
            if isinstance(item, dict) and item.get("type") == "collab_tool_call":
                raise OrchestrationError("Codex worker attempted nested orchestration")
    if (
        failed
        or len(threads) != 1
        or turn_started != 1
        or turn_completed != 1
        or len(set(sessions)) > 1
    ):
        raise OrchestrationError("Codex worker execution is incomplete")
    return threads[0], (sessions[0] if sessions else None)


def _shell_tokens(payload: str) -> tuple[str, ...]:
    """Tokenize one simple command while rejecting shell composition."""

    if "\n" in payload or "\r" in payload:
        return ()
    try:
        lexer = shlex.shlex(
            payload,
            posix=True,
            punctuation_chars=_SHELL_CONTROL_PUNCTUATION,
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = tuple(lexer)
    except ValueError:
        return ()
    if any(
        token == "$"
        or "`" in token
        or (
            bool(token)
            and set(token).issubset(set(_SHELL_CONTROL_PUNCTUATION))
        )
        for token in tokens
    ):
        return ()
    return tokens


def _command_tokens(command: str) -> tuple[str, ...]:
    """Safely unwrap Codex's fixed shell wrapper and tokenize its payload."""

    outer = _shell_tokens(command.strip())
    if len(outer) == 3 and outer[0] in _CODEX_SHELLS and outer[1] in {"-c", "-lc"}:
        return _shell_tokens(outer[2])
    return tuple(outer)


def _raw_command_payload(command: str) -> str:
    """Expose only an anchored wrapper payload for marker accounting.

    This parser deliberately does not certify command safety. Its sole purpose
    is to ensure an unsafe or failed P0-06 marker cannot disappear before the
    exact-three check.
    """

    stripped = command.strip()
    try:
        outer = shlex.split(stripped)
    except ValueError:
        return stripped
    if len(outer) >= 3 and outer[0] in _CODEX_SHELLS and outer[1] in {"-c", "-lc"}:
        return outer[2].lstrip()
    return stripped


def _is_mandatory_sop_read(tokens: tuple[str, ...]) -> bool:
    return tokens == (
        "sed",
        "-n",
        "1,999p",
        "$QA_SOURCE_ROOT/qa/SOP.md",
    )


def _p0_06_phase(tokens: tuple[str, ...]) -> str | None:
    if len(tokens) < 4 or tokens[0] != "QA_SCENARIO_ID=P0-06":
        return None
    prefix = "QA_SCENARIO_PHASE="
    if not tokens[1].startswith(prefix) or tokens[2] != "$QA_PYTHON_BIN":
        return None
    phase = tokens[1].removeprefix(prefix)
    arguments = tokens[3:]
    expected = {
        "CAPTURE": _P0_06_CAPTURE_TOKENS,
        "REVIEW": _P0_06_REVIEW_TOKENS,
        "FINALIZE": _P0_06_FINALIZE_TOKENS,
    }
    return phase if arguments == expected.get(phase) else None


ScenarioCommandMarker = tuple[str, int | str]


def _scenario_command_marker(command: str) -> ScenarioCommandMarker | None:
    tokens = _command_tokens(command)
    if not tokens:
        return None
    match = _SCENARIO_COMMAND_RE.match(tokens[0])
    if not match:
        return None
    scenario_id = match.group(1)
    if scenario_id == "P0-06":
        phase = _p0_06_phase(tokens)
        return (scenario_id, phase) if phase is not None else None
    if scenario_id == "P0-12":
        return (scenario_id, 1) if tokens == _P0_12_REQUEST_TOKENS else None
    if scenario_id not in _PARENT_LIVE_SCENARIO_IDS:
        return None
    matched_attempt = next(
        (
            attempt
            for attempt in (
                (1, 2)
                if scenario_id in _RETRYABLE_LIVE_SCENARIO_IDS
                else (1,)
            )
            if tokens == _live_request_tokens(scenario_id, attempt)
        ),
        None,
    )
    return (
        (scenario_id, matched_attempt) if matched_attempt is not None else None
    )


def _successful_scenario_marker(
    row: Mapping[str, Any],
) -> ScenarioCommandMarker | None:
    item = row.get("item")
    if not (
        row.get("type") == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "command_execution"
        and item.get("status") == "completed"
        and type(item.get("exit_code")) is int
        and item.get("exit_code") == 0
        and isinstance(item.get("command"), str)
    ):
        return None
    return _scenario_command_marker(item["command"])


@dataclass(frozen=True)
class ScenarioCommandEventSnapshot:
    """Bound exact command starts and their parent-observed terminal exits."""

    started: tuple[ScenarioCommandMarker, ...]
    completed: tuple[tuple[ScenarioCommandMarker, int], ...]


class ScenarioCommandEventCursor:
    """Incrementally parse one append-only, parent-owned Codex JSONL stream."""

    def __init__(self, path: Path, *, max_bytes: int = _MAX_EVENTS_BYTES):
        if max_bytes <= 0 or max_bytes > _MAX_EVENTS_BYTES:
            raise ValueError("event cursor byte bound is invalid")
        self.path = path
        self.max_bytes = max_bytes
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._tail = b""
        self._started_items: dict[
            str, tuple[ScenarioCommandMarker, str]
        ] = {}
        self._seen_item_ids: set[str] = set()
        self._started: list[ScenarioCommandMarker] = []
        self._completed: list[tuple[ScenarioCommandMarker, int]] = []
        self._rows_parsed = 0

    @property
    def bytes_consumed(self) -> int:
        return self._offset

    @property
    def rows_parsed(self) -> int:
        return self._rows_parsed

    def _consume_row(self, row: Mapping[str, Any]) -> None:
        row_type = row.get("type")
        if row_type not in {"item.started", "item.completed"}:
            return
        item = row.get("item")
        if not (
            isinstance(item, dict)
            and item.get("type") == "command_execution"
            and isinstance(item.get("command"), str)
        ):
            return
        marker = _scenario_command_marker(item["command"])
        item_id = item.get("id")
        if row_type == "item.started":
            if marker is None:
                return
            if not _valid_identifier(item_id) or item_id in self._seen_item_ids:
                raise OrchestrationError(
                    "live Codex command lifecycle is invalid"
                )
            normalized_id = str(item_id)
            self._seen_item_ids.add(normalized_id)
            self._started_items[normalized_id] = (
                marker,
                item["command"],
            )
            self._started.append(marker)
            return

        if not _valid_identifier(item_id):
            if marker is not None:
                raise OrchestrationError(
                    "live Codex command lifecycle is invalid"
                )
            return
        normalized_id = str(item_id)
        started = self._started_items.get(normalized_id)
        if started is None:
            if marker is not None:
                raise OrchestrationError(
                    "live Codex command lifecycle is invalid"
                )
            return
        if (
            marker != started[0]
            or item["command"] != started[1]
            or item.get("status") != "completed"
            or type(item.get("exit_code")) is not int
        ):
            raise OrchestrationError("live Codex command lifecycle is invalid")
        del self._started_items[normalized_id]
        self._completed.append((started[0], item["exit_code"]))

    def snapshot(self) -> ScenarioCommandEventSnapshot:
        label = "live Codex worker event stream"
        try:
            with open_owned_regular(
                self.path,
                label,
                max_bytes=self.max_bytes,
            ) as handle:
                before = os.fstat(handle.fileno())
                identity = (before.st_dev, before.st_ino)
                if self._identity is None:
                    self._identity = identity
                elif identity != self._identity:
                    raise OrchestrationError(f"{label} changed while reading")
                if before.st_size < self._offset:
                    raise OrchestrationError(f"{label} changed while reading")
                handle.seek(self._offset)
                expected = before.st_size - self._offset
                chunk = handle.read(expected)
                after = os.fstat(handle.fileno())
                linked = self.path.stat(follow_symlinks=False)
        except OrchestrationError:
            raise
        except OSError:
            raise OrchestrationError(f"{label} is unreadable") from None
        if (
            len(chunk) != expected
            or after.st_size < before.st_size
            or after.st_size > self.max_bytes
            or (after.st_dev, after.st_ino) != self._identity
            or not stat.S_ISREG(linked.st_mode)
            or linked.st_uid != os.geteuid()
            or stat.S_IMODE(linked.st_mode) != 0o600
            or linked.st_nlink != 1
            or linked.st_size < before.st_size
            or linked.st_size > self.max_bytes
            or (linked.st_dev, linked.st_ino) != self._identity
        ):
            raise OrchestrationError(f"{label} changed while reading")
        self._offset = before.st_size
        pending = self._tail + chunk
        raw_rows = pending.split(b"\n")
        self._tail = raw_rows.pop()
        if len(self._tail) > _MAX_JSON_LINE_BYTES:
            raise OrchestrationError(
                "live Codex worker event stream contains an oversized row"
            )
        for raw in raw_rows:
            if not raw or len(raw) > _MAX_JSON_LINE_BYTES:
                raise OrchestrationError("live Codex worker event stream is invalid")
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise OrchestrationError(
                    "live Codex worker event stream is invalid"
                ) from None
            if not isinstance(row, dict):
                raise OrchestrationError("live Codex worker event stream is invalid")
            self._consume_row(row)
            self._rows_parsed += 1
        return ScenarioCommandEventSnapshot(
            started=tuple(self._started),
            completed=tuple(self._completed),
        )


def completed_scenario_command_sequence_snapshot(
    path: Path,
) -> tuple[tuple[str, int | str], ...]:
    """Return exact successful lifecycles from one bounded snapshot."""

    snapshot = ScenarioCommandEventCursor(path).snapshot()
    return tuple(
        marker for marker, exit_code in snapshot.completed if exit_code == 0
    )


def completed_command_evidence(
    path: Path,
    *,
    allow_failed_persona_capture: bool = False,
) -> tuple[int, tuple[str, ...], bool, dict[str, int], tuple[str, ...]]:
    """Return fail-closed command evidence without retaining command text.

    Non-persona scenarios count only when the command exactly invokes the checked-
    in request helper with its scenario/attempt-bound paths.  Raw anchored marker
    counts are retained separately so an extra, failed, generic, or composed
    marker cannot hide beside one valid command.
    """

    count = 0
    scenario_counts = {scenario_id: 0 for scenario_id in AGENT_LIVE_SCENARIO_IDS}
    first_terminal_command: tuple[str, ...] | None = None
    first_terminal_command_succeeded = False
    p0_06_phases: list[str] = []
    valid_attempts = {scenario_id: [] for scenario_id in _PARENT_LIVE_SCENARIO_IDS}
    valid_cot_commands = 0
    valid_sequence: list[tuple[str, int | str]] = []
    for row in _json_lines(path, "Codex worker event stream"):
        item = row.get("item")
        if not (
            row.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "command_execution"
        ):
            continue
        command = item.get("command")
        tokens = _command_tokens(command) if isinstance(command, str) else ()
        exit_code = item.get("exit_code")
        command_succeeded = (
            isinstance(command, str)
            and
            item.get("status") == "completed"
            and isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code == 0
        )
        if first_terminal_command is None:
            first_terminal_command = tokens
            first_terminal_command_succeeded = command_succeeded
        if item.get("status") == "completed":
            count += 1
        if not isinstance(command, str):
            continue
        raw_payload = _raw_command_payload(command)
        raw_match = _RAW_SCENARIO_COMMAND_RE.match(raw_payload)
        if raw_match:
            scenario_counts[raw_match.group(1)] += 1
        if item.get("status") != "completed":
            continue
        if not tokens:
            continue
        match = _SCENARIO_COMMAND_RE.match(tokens[0])
        if not match:
            continue
        scenario_id = match.group(1)
        if not command_succeeded:
            continue
        if scenario_id == "P0-06":
            phase = _p0_06_phase(tokens)
            if phase is None:
                continue
            p0_06_phases.append(phase)
            valid_sequence.append((scenario_id, phase))
            continue
        if scenario_id == "P0-12":
            if tokens != _P0_12_REQUEST_TOKENS:
                continue
            valid_cot_commands += 1
            valid_sequence.append((scenario_id, 1))
            continue
        if scenario_id not in _PARENT_LIVE_SCENARIO_IDS:
            continue
        matched_attempt = next(
            (
                attempt
                for attempt in (
                    (1, 2)
                    if scenario_id in _RETRYABLE_LIVE_SCENARIO_IDS
                    else (1,)
                )
                if tokens == _live_request_tokens(scenario_id, attempt)
            ),
            None,
        )
        if matched_attempt is None:
            continue
        valid_attempts[scenario_id].append(matched_attempt)
        valid_sequence.append((scenario_id, matched_attempt))

    expected_sequence: list[tuple[str, int | str]] = []
    valid_ids: list[str] = []
    for scenario_id in AGENT_LIVE_SCENARIO_IDS:
        if scenario_id == "P0-06":
            full_persona = (
                tuple(p0_06_phases) == P0_06_COMMAND_PHASES
                and scenario_counts[scenario_id] == len(P0_06_COMMAND_PHASES)
            )
            failed_capture_only = (
                allow_failed_persona_capture
                and tuple(p0_06_phases) == ("CAPTURE",)
                and scenario_counts[scenario_id] == 1
            )
            phases = (
                P0_06_COMMAND_PHASES
                if full_persona
                else ("CAPTURE",)
                if failed_capture_only
                else P0_06_COMMAND_PHASES
            )
            expected_sequence.extend((scenario_id, phase) for phase in phases)
            if full_persona or failed_capture_only:
                valid_ids.append(scenario_id)
            continue
        if scenario_id == "P0-12":
            if valid_cot_commands == 1 and scenario_counts[scenario_id] == 1:
                valid_ids.append(scenario_id)
                expected_sequence.append((scenario_id, 1))
            continue
        attempts = valid_attempts[scenario_id]
        if attempts in ([1], [1, 2]) and scenario_counts[scenario_id] == len(attempts):
            valid_ids.append(scenario_id)
            expected_sequence.extend((scenario_id, attempt) for attempt in attempts)
    ordered = valid_sequence == expected_sequence
    return (
        count,
        tuple(valid_ids) if ordered else (),
        first_terminal_command_succeeded
        and _is_mandatory_sop_read(first_terminal_command or ()),
        scenario_counts,
        tuple(p0_06_phases),
    )


def scenario_command_contract_satisfied(
    counts: Mapping[str, int],
    p0_06_phases: Sequence[str],
    *,
    allow_failed_persona_capture: bool = False,
) -> bool:
    """Require every live marker and separate P0-06 capture/review/finalize calls."""

    persona_contract = bool(
        (
            tuple(p0_06_phases) == P0_06_COMMAND_PHASES
            and counts.get("P0-06") == len(P0_06_COMMAND_PHASES)
        )
        or (
            allow_failed_persona_capture
            and tuple(p0_06_phases) == ("CAPTURE",)
            and counts.get("P0-06") == 1
        )
    )
    return (
        persona_contract
        and set(counts) == set(AGENT_LIVE_SCENARIO_IDS)
        and all(
            isinstance(counts[scenario_id], int)
            and not isinstance(counts[scenario_id], bool)
            and (
                persona_contract
                if scenario_id == "P0-06"
                else (
                    minimum <= counts[scenario_id] <= 2
                    if scenario_id in _RETRYABLE_LIVE_SCENARIO_IDS
                    else counts[scenario_id] == minimum
                )
            )
            for scenario_id, minimum in MIN_SCENARIO_COMMAND_COUNTS.items()
        )
    )


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise OrchestrationError("orchestration receipt path is unsafe")
    parent = owned_directory(path.parent, "orchestration receipt parent")
    if path.parent.resolve() != parent:
        raise OrchestrationError("orchestration receipt parent is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise OrchestrationError("unable to create orchestration receipt") from None
    with open_owned_regular(
        path, "orchestration receipt", max_bytes=_MAX_RECEIPT_BYTES
    ):
        pass


def _peak_concurrency(workers: Sequence[Mapping[str, Any]]) -> int:
    points: list[tuple[datetime, int]] = []
    for worker in workers:
        start = _parse_timestamp(worker.get("started_at"))
        stop = _parse_timestamp(worker.get("stopped_at"))
        if start is None or stop is None or stop < start:
            raise OrchestrationError("Codex worker lifecycle timestamps are invalid")
        points.extend(((start, 1), (stop, -1)))
    # A stop and a start at the exact same instant are not concurrent.
    points.sort(key=lambda point: (point[0], point[1]))
    active = 0
    peak = 0
    for _, delta in points:
        active += delta
        if active < 0:
            raise OrchestrationError("Codex worker lifecycle is inconsistent")
        peak = max(peak, active)
    if active != 0:
        raise OrchestrationError("Codex worker lifecycle is incomplete")
    return peak


def _validate_receipt_shape(receipt: Any) -> list[dict[str, Any]]:
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "launcher_id",
        "max_configured_profile_concurrency",
        "max_observed_profile_concurrency",
        "launch_attempts",
        "workers",
    }:
        raise OrchestrationError("orchestration receipt shape is invalid")
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or not _valid_identifier(receipt.get("launcher_id"))
        or receipt.get("max_configured_profile_concurrency")
        != MAX_CONFIGURED_CONCURRENCY
        or receipt.get("launch_attempts") != len(PROFILE_AGENT_TYPES)
    ):
        raise OrchestrationError("orchestration receipt contract is invalid")
    workers = receipt.get("workers")
    if not isinstance(workers, list) or len(workers) != len(PROFILE_AGENT_TYPES):
        raise OrchestrationError("orchestration receipt worker count is invalid")
    expected_keys = {
        "profile_id",
        "agent_type",
        "attempt",
        "process_exit_code",
        "worker_id",
        "thread_id",
        "session_id",
        "permission_profile",
        "started_at",
        "stopped_at",
        "profile_result_sha256",
        "exec_events_sha256",
        "live_receipt_sha256",
        "cot_receipt_sha256",
        "cot_delivery_status",
        "cot_failure_code",
    }
    identities: list[str] = []
    for index, row in enumerate(workers):
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise OrchestrationError("orchestration receipt worker shape is invalid")
        expected_profile, expected_agent = PROFILE_AGENT_TYPES[index]
        if (
            row.get("profile_id") != expected_profile
            or row.get("agent_type") != expected_agent
            or row.get("attempt") != 1
            or row.get("process_exit_code") != 0
            or row.get("permission_profile") != f"io-e2e-agent-driven-test-{expected_profile}"
            or not _valid_identifier(row.get("worker_id"))
            or row.get("thread_id") != row.get("worker_id")
            or (
                row.get("session_id") is not None
                and not _valid_identifier(row.get("session_id"))
            )
            or not isinstance(row.get("profile_result_sha256"), str)
            or not _SHA256_RE.fullmatch(row["profile_result_sha256"])
            or not isinstance(row.get("exec_events_sha256"), str)
            or not _SHA256_RE.fullmatch(row["exec_events_sha256"])
            or not isinstance(row.get("live_receipt_sha256"), str)
            or not _SHA256_RE.fullmatch(row["live_receipt_sha256"])
            or not isinstance(row.get("cot_receipt_sha256"), str)
            or not _SHA256_RE.fullmatch(row["cot_receipt_sha256"])
            or row.get("cot_delivery_status") not in {"PASS", "FAIL", "UNVERIFIED"}
            or not _valid_identifier(row.get("cot_failure_code"))
        ):
            raise OrchestrationError("orchestration receipt worker contract is invalid")
        # Also validates ordering and timezone-awareness.
        start = _parse_timestamp(row.get("started_at"))
        stop = _parse_timestamp(row.get("stopped_at"))
        if start is None or stop is None or stop < start:
            raise OrchestrationError("orchestration receipt timestamps are invalid")
        identities.append(row["worker_id"])
    if len(set(identities)) != len(PROFILE_AGENT_TYPES):
        raise OrchestrationError(
            "orchestration receipt worker identities are duplicated"
        )
    peak = _peak_concurrency(workers)
    if (
        not 1 <= peak <= MAX_CONFIGURED_CONCURRENCY
        or receipt.get("max_observed_profile_concurrency") != peak
    ):
        raise OrchestrationError("orchestration receipt concurrency is invalid")
    return workers


def verify(
    receipt_path: Path,
    worker_output_root: Path,
    aggregation_input_root: Path,
) -> dict[str, Any]:
    """Verify receipt identity, exact output set, lifecycle, and content hashes."""

    root = owned_directory(worker_output_root, "worker output root")
    aggregation = owned_directory(aggregation_input_root, "aggregation input root")
    receipt = load_private_json(
        receipt_path, "orchestration receipt", max_bytes=_MAX_RECEIPT_BYTES
    )
    workers = _validate_receipt_shape(receipt)
    try:
        entries = list(root.iterdir())
    except OSError:
        raise OrchestrationError("worker output root is unreadable") from None
    if {entry.name for entry in entries} != {
        profile_id for profile_id, _ in PROFILE_AGENT_TYPES
    } or any(entry.is_symlink() for entry in entries):
        raise OrchestrationError(
            "worker output matrix is incomplete or contains extras"
        )
    try:
        aggregation_entries = list(aggregation.iterdir())
    except OSError:
        raise OrchestrationError("aggregation input root is unreadable") from None
    if {entry.name for entry in aggregation_entries} != {
        f"{profile_id}.json" for profile_id, _ in PROFILE_AGENT_TYPES
    } or any(entry.is_symlink() for entry in aggregation_entries):
        raise OrchestrationError(
            "aggregation input matrix is incomplete or contains extras"
        )

    for row in workers:
        profile_id = row["profile_id"]
        directory = owned_directory(root / profile_id, f"{profile_id} output directory")
        try:
            names = {entry.name for entry in directory.iterdir()}
        except OSError:
            raise OrchestrationError("worker output directory is unreadable") from None
        if names != WORKER_FILES:
            raise OrchestrationError(
                "worker output file set is incomplete or contains extras"
            )
        events = directory / "events.jsonl"
        result = directory / "result.json"
        cot_receipt_path = directory / "cot-delivery-receipt.json"
        live_receipt_path = directory / "live-scenario-receipts.json"
        thread_id, session_id = parse_exec_events(events)
        (
            _,
            scenario_command_ids,
            sop_read_first,
            scenario_command_counts,
            p0_06_phases,
        ) = completed_command_evidence(events)
        if (
            thread_id != row["thread_id"]
            or session_id != row["session_id"]
            or not sop_read_first
            or scenario_command_ids != AGENT_LIVE_SCENARIO_IDS
            or not scenario_command_contract_satisfied(
                scenario_command_counts, p0_06_phases
            )
            or file_sha256(
                events, "Codex worker event stream", max_bytes=_MAX_EVENTS_BYTES
            )
            != row["exec_events_sha256"]
        ):
            raise OrchestrationError("worker output does not match trusted receipt")
        try:
            cot_receipt, cot_receipt_sha256 = validate_cot_receipt(
                cot_receipt_path, profile_id
            )
        except (CotReceiptError, OSError):
            raise OrchestrationError(
                "worker COT evidence does not match trusted receipt"
            ) from None
        if (
            cot_receipt_sha256 != row["cot_receipt_sha256"]
            or cot_receipt.get("status") != row["cot_delivery_status"]
            or cot_receipt.get("failure_code") != row["cot_failure_code"]
        ):
            raise OrchestrationError(
                "worker COT evidence does not match trusted receipt"
            )
        try:
            live_receipts, live_receipt_sha256 = validate_live_scenario_receipts(
                live_receipt_path,
                run_id=str(receipt["launcher_id"]),
                profile_id=profile_id,
            )
        except (LiveScenarioReceiptError, OSError):
            raise OrchestrationError(
                "worker live evidence does not match trusted receipt"
            ) from None
        if live_receipt_sha256 != row["live_receipt_sha256"]:
            raise OrchestrationError(
                "worker live evidence does not match trusted receipt"
            )
        canonical = aggregation / f"{profile_id}.json"
        result_payload = load_private_json(
            result, "Codex worker result", max_bytes=_MAX_RESULT_BYTES
        )
        canonical_payload = load_private_json(
            canonical, "canonical aggregation input", max_bytes=_MAX_RESULT_BYTES
        )
        if (
            canonical_json_sha256(result_payload) != row["profile_result_sha256"]
            or canonical_json_sha256(canonical_payload) != row["profile_result_sha256"]
        ):
            raise OrchestrationError(
                "canonical aggregation input does not match receipt"
            )
        if result_payload.get("profile_id") != profile_id:
            raise OrchestrationError("Codex worker result profile is invalid")
        try:
            validate_live_result_binding(result_payload, live_receipts)
        except LiveScenarioReceiptError:
            raise OrchestrationError(
                "worker live evidence does not match worker result"
            ) from None
        # Validate ownership and modes even when their contents are intentionally ignored.
        with open_owned_regular(
            directory / "schema.json",
            "Codex worker schema",
            max_bytes=_MAX_SCHEMA_BYTES,
        ):
            pass
        with open_owned_regular(
            directory / "stderr.log", "Codex worker stderr", max_bytes=_MAX_STDERR_BYTES
        ):
            pass
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify independent-process Codex orchestration evidence"
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--worker-output-root", type=Path, required=True)
    parser.add_argument("--aggregation-input-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify(args.receipt, args.worker_output_root, args.aggregation_input_root)
    except OrchestrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: orchestration verifier encountered an internal error",
            file=sys.stderr,
        )
        return 1
    print("trusted independent Codex orchestration verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
