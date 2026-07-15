#!/usr/bin/env python3
"""Launch exactly eight isolated top-level Codex qualification processes.

The launcher is intentionally deterministic and not intelligent.  Intelligence
lives inside each selected headless Codex profile.  The launcher owns process
count, concurrency, environment isolation, structured-output validation,
canonical aggregation inputs, and the trusted orchestration receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

try:
    from qa.atomic_private_file import AtomicPrivateFileError, create_private_file
    from qa.codex_output_schema import validate_authoring_schema
    from qa.diagnostic_results import DiagnosticResultError, agent_error_profile
    from qa.orchestration_contract import PROFILE_AGENT_TYPES
    from qa.validate_diagnostic_attempts import (
        DiagnosticAttemptError,
        validate_live_attempts,
    )
    from qa.validate_cot_receipt import CotReceiptError, validate_cot_receipt
    from qa.request_live_scenario_probe import (
        LIVE_SCENARIO_IDS as PARENT_LIVE_SCENARIO_IDS,
        LiveProbeRequestError,
        facts_path as live_facts_path,
        load_request_marker,
        request_path as live_request_path,
    )
    from qa.validate_live_scenario_receipts import (
        LiveScenarioReceiptError,
        canonical_json_sha256 as live_json_sha256,
        validate_aggregate_object,
        validate_live_scenario_receipts,
        validate_receipt_object as validate_live_receipt_object,
        validate_result_binding as validate_live_result_binding,
    )
    from qa.verify_codex_orchestration import (
        AGENT_LIVE_SCENARIO_IDS,
        MAX_CONFIGURED_CONCURRENCY,
        RECEIPT_SCHEMA_VERSION,
        OrchestrationError,
        canonical_json_sha256,
        completed_command_evidence,
        file_sha256,
        load_private_json,
        open_owned_regular,
        owned_directory,
        parse_exec_events,
        scenario_command_contract_satisfied,
        verify,
        write_receipt,
    )
    from qa.write_codex_config import worker_permission_profile
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from atomic_private_file import AtomicPrivateFileError, create_private_file
    from codex_output_schema import validate_authoring_schema
    from diagnostic_results import DiagnosticResultError, agent_error_profile
    from orchestration_contract import PROFILE_AGENT_TYPES
    from validate_diagnostic_attempts import (
        DiagnosticAttemptError,
        validate_live_attempts,
    )
    from validate_cot_receipt import CotReceiptError, validate_cot_receipt
    from request_live_scenario_probe import (
        LIVE_SCENARIO_IDS as PARENT_LIVE_SCENARIO_IDS,
        LiveProbeRequestError,
        facts_path as live_facts_path,
        load_request_marker,
        request_path as live_request_path,
    )
    from validate_live_scenario_receipts import (
        LiveScenarioReceiptError,
        canonical_json_sha256 as live_json_sha256,
        validate_aggregate_object,
        validate_live_scenario_receipts,
        validate_receipt_object as validate_live_receipt_object,
        validate_result_binding as validate_live_result_binding,
    )
    from verify_codex_orchestration import (
        AGENT_LIVE_SCENARIO_IDS,
        MAX_CONFIGURED_CONCURRENCY,
        RECEIPT_SCHEMA_VERSION,
        OrchestrationError,
        canonical_json_sha256,
        completed_command_evidence,
        file_sha256,
        load_private_json,
        open_owned_regular,
        owned_directory,
        parse_exec_events,
        scenario_command_contract_satisfied,
        verify,
        write_receipt,
    )
    from write_codex_config import worker_permission_profile


PINNED_CODEX_VERSION = "codex-cli 0.144.3"
LOCKED_BASE_URL = "https://test-api.feedling.app"
BASELINE_RUNTIME = "deployed_current"
LOCKED_RUNTIME = "hosted_resident"
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_SCHEMA_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_REQUEST_PUBLICATION_GRACE_SECONDS = 2.0
_AMBIENT_READ_ROOTS = tuple(
    dict.fromkeys(Path(value).resolve() for value in ("/tmp", "/var/tmp", "/dev/shm"))
)
_WORKER_AUTHORED_OUTPUT_FILES = frozenset(
    ("events.jsonl", "result.json", "schema.json", "stderr.log")
)
_EXPECTED_OUTPUT_FILES = _WORKER_AUTHORED_OUTPUT_FILES | frozenset(
    ("cot-delivery-receipt.json", "live-scenario-receipts.json")
)
_DIAGNOSTIC_RESULT_SOURCE_CODEX = "codex_worker"
_DIAGNOSTIC_RESULT_SOURCE_FALLBACK = "deterministic_fallback"
_DIAGNOSTIC_FALLBACK_INVOCATION = "INVOCATION_FAILED"
_DIAGNOSTIC_FALLBACK_PROCESS = "PROCESS_EXIT_NONZERO"
_DIAGNOSTIC_FALLBACK_WORKER_EVIDENCE = "WORKER_RESULT_INVALID"
_DIAGNOSTIC_FALLBACK_TOOL_USE = "AGENT_TOOL_USE_MISSING"
_DIAGNOSTIC_FALLBACK_SCENARIO_TOOL_USE = "AGENT_SCENARIO_TOOL_USE_MISSING"
_DIAGNOSTIC_FALLBACK_COT_MISSING = "COT_RECEIPT_MISSING"
_DIAGNOSTIC_FALLBACK_COT_INVALID = "COT_RECEIPT_INVALID"
_DIAGNOSTIC_COT_BINDING_MISMATCH = "COT_RESULT_BINDING_MISMATCH"
_PROFILE_PROMPT = """\
You are one independent intelligent qualification agent in the Feedling API-key
P0 suite. Read $QA_SOURCE_ROOT/qa/SOP.md,
$QA_SOURCE_ROOT/qa/coverage-lock.json, and
$QA_SOURCE_ROOT/qa/scenarios/api-key-journey.md before acting. QA_PRIVATE_MANIFEST is an
owner-only one-row manifest for exactly your assigned profile. Test only that
profile against IO_E2E_BASE_URL and execute all locked scenarios in order.
Copy QA_EXPECTED_RUNTIME exactly into `expected_runtime`. Copy the authenticated
manifest readback into `observed_runtime` and `observed_runtime_version`; never
turn a `deployed_current` requirement into `hosted_resident` merely because the
backend reports its legacy runtime label that way.
Your first response action MUST be a shell command execution, not a plan or a
final JSON response. Run exactly:
sed -n '1,999p' "$QA_SOURCE_ROOT/qa/SOP.md"
Then use shell commands to read the coverage lock, scenario file, and your
one-row manifest and drive the live API journey. The trusted launcher rejects a
result whose Codex event stream lacks completed command evidence and a parent-
owned receipt for every agent-driven live scenario. For P0-02, P0-03, P0-04,
P0-05, and P0-07 through P0-11, request attempt 1 with exactly this command,
substituting the same scenario ID in all four places:
QA_SCENARIO_ID=P0-XX "$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py" --scenario P0-XX --attempt 1 --request "$QA_WORK_ROOT/.live-probe-P0-XX-1.request" --facts "$QA_WORK_ROOT/live-probe-P0-XX-1.facts.json"
Read the resulting private facts file. The deterministic parent performs the
fixed network actions, owns the authoritative receipt outside your writable
roots, and binds its run/profile/scenario/attempt, IDs, turns, latencies, and
deterministic assertions. You retain semantic judgment only for the explicitly
listed P0-10/P0-11 semantic assertions. Only P0-08 through P0-11 may retry, and
only when attempt 1's parent receipt is `AGENT_ERROR` for a transient missing-
reply/transport observation with receipt failure code `CHAT_TIMEOUT` or
`MISSING_REPLY`. In that one case, make attempt 2 using the
identical command with every `1` changed to `2`, preserve both receipts and
attempt rows, set the first attempt's locked scenario stage code and
`reproducible: false`, and record `RETRY_OBSERVATION_RECORDED`, `RETRY_USED`,
and the matching transient diagnostic code. A PASS,
product failure, credential/deployment blocker, or evidence blocker is never
retryable. Never exceed two attempts or replace attempt 1. Execute scenarios
in SOP order. Generic markers, alternate executables, `python -c`, extra shell
tokens, wrong paths, duplicate/out-of-order attempts, or a result greener than
the parent receipt are rejected. P0-01, P0-12, and P0-13 have separate parent-
owned evidence.
P0-06 requires exactly three ordered, successful phase-marker commands. Run
these exact commands in separate Codex tool calls:
QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=CAPTURE "$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/tools/genesis_e2e.py" distill-existing-session --api-url "$IO_E2E_BASE_URL" --session-manifest "$QA_PRIVATE_MANIFEST" --profile-id "$QA_PROFILE_ID" --fixture "$QA_SOURCE_ROOT/qa/fixtures/persona-import-v1.json" --private-evidence "$QA_WORK_ROOT/p0-06-private-evidence.json" --artifact-dir "$QA_ARTIFACT_DIR"
QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=REVIEW "$QA_PYTHON_BIN" -I -B -c 'import pathlib,sys;j=pathlib.Path(sys.argv[2]);j.exists() and sys.exit(17);print(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))' "$QA_WORK_ROOT/p0-06-private-evidence.json" "$QA_WORK_ROOT/p0-06-semantic-judgment.json"
After observing REVIEW output, make the semantic decisions and write the bounded
owner-only judgment to exactly
`$QA_WORK_ROOT/p0-06-semantic-judgment.json` in a separate unmarked tool call.
Then run:
QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=FINALIZE "$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/tools/genesis_e2e.py" distill-existing-session-finalize --fixture "$QA_SOURCE_ROOT/qa/fixtures/persona-import-v1.json" --private-evidence "$QA_WORK_ROOT/p0-06-private-evidence.json" --semantic-judgment "$QA_WORK_ROOT/p0-06-semantic-judgment.json" --artifact-dir "$QA_ARTIFACT_DIR"
Bind the finalizer's exact `transport.archive_upload_count`,
`checks.archive_receipts_verified`, and
`checks.genesis_upload_metadata_verified` values into the correspondingly named
`persona_finalizer` fields; never infer or invent those values.
Do not generate or execute one script that pre-fills the judgment, copies
expected fact IDs into all-true answers, or runs capture/review/finalize without
an intervening model judgment. The launcher rejects nonzero, missing,
duplicate, generic, extra, modified, or out-of-order P0-06 commands.
For every qualification Python command, invoke the fixed, quoted
"$QA_PYTHON_BIN" only. Never fall back to python or python3, create a virtual
environment, or install packages; the deterministic parent already sandbox-
preflighted this interpreter and its dependencies.
Never claim that contract inputs, provisioning facts, API behavior, or traces
were observed unless you actually read or exercised them with tools in this turn.
Drive the live user journey, inspect correlated traces and latency stages,
adapt the next probe when evidence is ambiguous, and make semantic judgments
for chat, reasoning disclosure, memory, persona import, identity, and cleanup.
For P0-12, request the trusted parent delivery probe exactly once using the
fixed marker/facts handshake specified by the scenario document, even if an
earlier scenario failed or produced incomplete evidence. The deterministic
parent—not this agent—sends the reasoning turn and owns the authoritative
receipt outside every agent-writable root. Read the sanitized facts copy,
preserve FAIL/UNVERIFIED observations, do not retry the reasoning turn, and bind
the profile reasoning object plus the P0-12 scenario and turn IDs to those
facts. Never invoke qa/cot_delivery_probe.py or create, edit, replace, or delete
the facts copy or authoritative receipt yourself.
Copy receipt facts exactly: empty receipt request/turn/trace IDs map to empty
reasoning IDs and empty scenario ID arrays; empty delivered kind/source/model
strings map to null result fields. Never substitute the configured provider
model for an empty delivered-thinking model, and copy the receipt's disclosure
length exactly. Copy requested_effort, configured_effort, and effective_effort
exactly from the trusted receipt. Effective effort is not runtime-attested, so
it must remain unknown and reasoning_effective_effort_not_attested must be true;
never infer effective medium from requested/configured medium or from a visible
reasoning event. An unattested configured effort is BLOCKED_EVIDENCE with
PRECONDITION_MISSING; an attested route mismatch is PRODUCT_FAIL with
MODEL_ROUTE_MISMATCH; an attested non-medium effort on the matching route is
PRODUCT_FAIL with REASONING_EFFORT_CLAMPED. Preserve the delivery observations
in the reasoning object even when configuration evidence determines P0-12.
Missing provider reasoning-token evidence remains BLOCKED_EVIDENCE; do not infer
reasoning tokens from ordinary input/output token totals. Always leave the
trusted handshake files in place for launcher validation.
When QA_QUALIFICATION_MODE is diagnostic, do not call /v1/account/reset in
P0-13: the deterministic parent owns the only account reset so it can verify
cleanup without an admin token. Emit the fixed diagnostic deferral exactly:
P0-13 and its sole attempt use BLOCKED_EVIDENCE with CLEANUP /
PRECONDITION_MISSING / reproducible=true; its trace-stages, correlation, and
latency assertions are true, cleanup_confirmed is false, and its evidence codes
include TRACE_CORRELATION_CONFIRMED and LATENCY_ATTRIBUTED. The top-level status
is BLOCKED_EVIDENCE when P0-01 through P0-12 pass, and cleanup status is
BLOCKED_EVIDENCE; cleanup attempted/provider-config
deleted/account-reset/old-credential-rejected are all false; diagnostic_codes
includes CLEANUP_FALLBACK_USED. The deterministic parent preserves this result,
performs the reset, and records a separate cleanup verification. This local run
can never release-qualify.
In diagnostic mode, missing protected deployment-SHA or server-reaper
attestations are known release-evidence gaps, not permission to skip the live
journey. Record the affected assertion honestly, but continue every later
scenario. A blocked preflight assertion MUST NOT short-circuit P0-02 through
P0-13.
Never seek provider/admin credentials, the full provisioning manifest, another
profile manifest, public artifacts, raw output from another process, or nested
agents. Always attempt cleanup except for the fixed diagnostic-only P0-13
parent deferral above. Return exactly one profileResult JSON object
matching the supplied output schema; include only sanitized structured evidence.
"""


class WorkerLaunchError(RuntimeError):
    """Sanitized fixed failure from the deterministic process boundary."""


class WorkerToolUseError(WorkerLaunchError):
    """The profile returned a verdict without executing a qualification tool."""


class WorkerScenarioToolUseError(WorkerLaunchError):
    """The profile omitted trusted command evidence for live scenarios."""

    def __init__(
        self,
        count: int,
        scenario_ids: tuple[str, ...],
        scenario_counts: Mapping[str, int],
        p0_06_phases: tuple[str, ...],
    ) -> None:
        super().__init__("Codex worker omitted live-scenario command evidence")
        self.count = count
        self.scenario_ids = scenario_ids
        self.scenario_counts = dict(scenario_counts)
        self.p0_06_phases = p0_06_phases


@dataclass(frozen=True)
class WorkerSpec:
    profile_id: str
    agent_type: str
    command: tuple[str, ...]
    environment: Mapping[str, str]
    work: Path
    output_dir: Path
    schema_path: Path
    result_path: Path
    events_path: Path
    stderr_path: Path
    cot_receipt_path: Path
    cot_request_path: Path
    cot_facts_path: Path
    live_receipt_path: Path
    prompt: str


@dataclass(frozen=True)
class WorkerAttempt:
    spec: WorkerSpec
    exit_code: int
    started_at: str
    stopped_at: str
    invocation_failed: bool


ProcessRunner = Callable[[WorkerSpec, int], int]
CotProbeRunner = Callable[[WorkerSpec], Mapping[str, Any]]
LiveProbeRunner = Callable[
    [WorkerSpec, str, int, str], tuple[Mapping[str, Any], Mapping[str, Any]]
]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _private_file(path: Path, label: str, *, max_bytes: int) -> None:
    with open_owned_regular(path, label, max_bytes=max_bytes):
        pass


def _source_file(path: Path, label: str, *, max_bytes: int) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise WorkerLaunchError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerLaunchError(f"{label} is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size > max_bytes
    ):
        raise WorkerLaunchError(f"{label} is unsafe")
    return resolved


def _source_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise WorkerLaunchError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerLaunchError(f"{label} is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise WorkerLaunchError(f"{label} is unsafe")
    return resolved


def _trusted_executable(path: Path) -> Path:
    if not path.is_absolute():
        raise WorkerLaunchError("Codex executable must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise WorkerLaunchError("Codex executable is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise WorkerLaunchError("Codex executable is unsafe")
    return resolved


def _trusted_worker_python(path: Path) -> Path:
    """Require an executable Python with the qualification crypto dependency."""

    executable = _trusted_executable(path)
    try:
        metadata = executable.stat()
    except OSError:
        raise WorkerLaunchError("qualification Python is unavailable") from None
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise WorkerLaunchError(
            "qualification Python must be an owner-controlled executable"
        )
    try:
        result = subprocess.run(
            [str(executable), "-c", "import cryptography"],
            check=False,
            capture_output=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError):
        raise WorkerLaunchError("qualification Python could not execute") from None
    if result.returncode != 0:
        raise WorkerLaunchError(
            "qualification Python is missing cryptography support"
        )
    return executable


def verify_codex_version(codex_bin: Path) -> None:
    executable = _trusted_executable(codex_bin)
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError):
        raise WorkerLaunchError("unable to verify Codex version") from None
    if result.returncode != 0 or result.stdout.strip() != PINNED_CODEX_VERSION:
        raise WorkerLaunchError("Codex version does not match the qualification pin")


def _create_private_file(path: Path, content: bytes = b"") -> None:
    try:
        create_private_file(path, content)
    except AtomicPrivateFileError:
        raise WorkerLaunchError("unable to create private worker evidence") from None


def _copy_private_file(source: Path, destination: Path, *, max_bytes: int) -> None:
    try:
        with open_owned_regular(
            source, "validated worker result", max_bytes=max_bytes
        ) as handle:
            content = handle.read()
    except OrchestrationError as exc:
        raise WorkerLaunchError(str(exc)) from None
    _create_private_file(destination, content)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        content = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise WorkerLaunchError("diagnostic fallback result is invalid") from None
    _create_private_file(path, content)


def _trusted_cot_nonce(spec: WorkerSpec) -> str:
    run_id = str(spec.environment.get("QA_RUN_ID") or "")
    digest = hashlib.sha256(
        f"{run_id}:{spec.profile_id}".encode("utf-8")
    ).hexdigest()
    return f"cot_{digest[:32]}"


def _run_trusted_cot_probe(spec: WorkerSpec) -> Mapping[str, Any]:
    """Execute P0-12 in a fixed parent-owned subprocess, never in the agent."""

    source_root = Path(spec.environment["QA_SOURCE_ROOT"])
    worker_python = Path(spec.environment["QA_PYTHON_BIN"])
    command = (
        str(worker_python),
        "-I",
        "-B",
        str(source_root / "qa" / "cot_delivery_probe.py"),
        "--manifest",
        str(spec.environment["QA_PRIVATE_MANIFEST"]),
        "--output",
        str(spec.cot_receipt_path),
        "--nonce",
        _trusted_cot_nonce(spec),
        "--profile-id",
        spec.profile_id,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            capture_output=True,
            check=False,
            timeout=360,
        )
    except (OSError, subprocess.SubprocessError):
        raise WorkerLaunchError("trusted COT probe could not execute") from None
    # Exit 2 is the probe's documented FAIL/UNVERIFIED observation. It still
    # produces authoritative evidence and must never be retried.
    if completed.returncode not in (0, 2):
        raise WorkerLaunchError("trusted COT probe did not produce evidence")
    try:
        receipt, _ = validate_cot_receipt(spec.cot_receipt_path, spec.profile_id)
    except (CotReceiptError, OSError):
        raise WorkerLaunchError("trusted COT probe receipt is invalid") from None
    return receipt


def _run_trusted_live_probe(
    spec: WorkerSpec, scenario_id: str, attempt: int, nonce: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Execute one allowlisted scenario in a fixed parent-owned subprocess."""

    source_root = Path(spec.environment["QA_SOURCE_ROOT"])
    worker_python = Path(spec.environment["QA_PYTHON_BIN"])
    receipt_path = spec.output_dir / f".live-{scenario_id}-{attempt}.receipt.json"
    private_facts_path = spec.output_dir / f".live-{scenario_id}-{attempt}.private.json"
    if receipt_path.exists() or private_facts_path.exists():
        raise WorkerLaunchError("trusted live probe paths are not pristine")
    command = (
        str(worker_python),
        "-I",
        "-B",
        str(source_root / "qa" / "live_scenario_probe.py"),
        "--manifest",
        str(spec.environment["QA_PRIVATE_MANIFEST"]),
        "--output",
        str(receipt_path),
        "--private-facts",
        str(private_facts_path),
        "--run-id",
        str(spec.environment["QA_RUN_ID"]),
        "--profile-id",
        spec.profile_id,
        "--scenario",
        scenario_id,
        "--attempt",
        str(attempt),
        "--nonce",
        nonce,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=source_root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            capture_output=True,
            check=False,
            timeout=1800,
        )
        if completed.returncode not in (0, 2):
            raise WorkerLaunchError("trusted live probe did not produce evidence")
        raw_receipt = load_private_json(
            receipt_path,
            "trusted live scenario receipt",
            max_bytes=2 * 1024 * 1024,
        )
        receipt = validate_live_receipt_object(
            raw_receipt,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            scenario_id=scenario_id,
            attempt=attempt,
        )
        private_facts = load_private_json(
            private_facts_path,
            "trusted live scenario private facts",
            max_bytes=8 * 1024 * 1024,
        )
        if live_json_sha256(private_facts) != receipt["private_facts_sha256"]:
            raise WorkerLaunchError("trusted live probe facts are inconsistent")
        return receipt, private_facts
    except (
        LiveScenarioReceiptError,
        OrchestrationError,
        OSError,
        subprocess.SubprocessError,
    ):
        raise WorkerLaunchError("trusted live probe could not execute") from None
    finally:
        for path in (receipt_path, private_facts_path):
            try:
                path.unlink()
            except OSError:
                pass


def _live_handshake_paths(
    spec: WorkerSpec, scenario_id: str, attempt: int
) -> tuple[Path, Path]:
    return (
        live_request_path(spec.work, scenario_id, attempt),
        live_facts_path(spec.work, scenario_id, attempt),
    )


def _live_request_is_next(
    receipts: Sequence[Mapping[str, Any]], scenario_id: str, attempt: int
) -> bool:
    if not receipts:
        return scenario_id == PARENT_LIVE_SCENARIO_IDS[0] and attempt == 1
    last = receipts[-1]
    last_scenario = str(last.get("scenario_id") or "")
    if last_scenario not in PARENT_LIVE_SCENARIO_IDS:
        return False
    last_index = PARENT_LIVE_SCENARIO_IDS.index(last_scenario)
    if (
        last.get("attempt") == 1
        and last.get("status") == "AGENT_ERROR"
        and last_scenario in {"P0-08", "P0-09", "P0-10", "P0-11"}
        and scenario_id == last_scenario
        and attempt == 2
    ):
        return True
    return (
        attempt == 1
        and last_index + 1 < len(PARENT_LIVE_SCENARIO_IDS)
        and scenario_id == PARENT_LIVE_SCENARIO_IDS[last_index + 1]
    )


def _write_live_error_facts(
    spec: WorkerSpec, scenario_id: str, attempt: int, facts_path: Path
) -> None:
    if facts_path.exists():
        return
    _write_private_json(
        facts_path,
        {
            "schema_version": 1,
            "profile_id": spec.profile_id,
            "scenario_id": scenario_id,
            "attempt": attempt,
            "receipt_sha256": None,
            "receipt": None,
            "private_facts": None,
            "status": "UNAVAILABLE",
            "failure_code": "TRUSTED_PROBE_ERROR",
        },
    )


def _load_ready_live_request(
    path: Path,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
) -> Mapping[str, Any]:
    """Allow atomic hard-link publication to settle to one private link."""

    deadline = time.monotonic() + _REQUEST_PUBLICATION_GRACE_SECONDS
    while True:
        try:
            return load_request_marker(
                path,
                run_id=run_id,
                profile_id=profile_id,
                scenario_id=scenario_id,
                attempt=attempt,
            )
        except LiveProbeRequestError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _perform_trusted_live_handshake(
    spec: WorkerSpec,
    scenario_id: str,
    attempt: int,
    live_probe_runner: LiveProbeRunner,
    receipts: list[dict[str, Any]],
) -> None:
    request_path, facts_path = _live_handshake_paths(spec, scenario_id, attempt)
    try:
        _load_ready_live_request(
            request_path,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            scenario_id=scenario_id,
            attempt=attempt,
        )
        if facts_path.exists() or not _live_request_is_next(
            receipts, scenario_id, attempt
        ):
            raise WorkerLaunchError("trusted live probe request is out of order")
        nonce = f"live_{secrets.token_hex(16)}"
        returned_receipt, private_facts = live_probe_runner(
            spec, scenario_id, attempt, nonce
        )
        receipt = validate_live_receipt_object(
            returned_receipt,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            scenario_id=scenario_id,
            attempt=attempt,
        )
        if live_json_sha256(private_facts) != receipt["private_facts_sha256"]:
            raise WorkerLaunchError("trusted live probe facts are inconsistent")
        receipt_sha256 = live_json_sha256(receipt)
        _write_private_json(
            facts_path,
            {
                "schema_version": 1,
                "profile_id": spec.profile_id,
                "scenario_id": scenario_id,
                "attempt": attempt,
                "receipt_sha256": receipt_sha256,
                "receipt": receipt,
                "private_facts": private_facts,
            },
        )
        receipts.append(receipt)
    except (
        LiveProbeRequestError,
        LiveScenarioReceiptError,
        OSError,
        WorkerLaunchError,
    ):
        try:
            _write_live_error_facts(spec, scenario_id, attempt, facts_path)
        except WorkerLaunchError:
            pass


def _write_live_receipt_aggregate(
    spec: WorkerSpec, receipts: Sequence[Mapping[str, Any]]
) -> None:
    payload = {
        "schema_version": 1,
        "kind": "live_scenario_receipt_set",
        "run_id": str(spec.environment["QA_RUN_ID"]),
        "profile_id": spec.profile_id,
        "receipts": [dict(row) for row in receipts],
    }
    # Validate complete successful sets before persistence.  Partial/error sets
    # are still persisted so release verification fails closed with a bounded
    # evidence error instead of silently omitting the parent-owned artifact.
    try:
        validate_aggregate_object(
            payload,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
        )
    except LiveScenarioReceiptError:
        pass
    _write_private_json(spec.live_receipt_path, payload)


def _validate_cot_request(spec: WorkerSpec) -> None:
    try:
        with open_owned_regular(
            spec.cot_request_path,
            "COT probe request marker",
            max_bytes=512,
        ) as handle:
            payload = handle.read().decode("utf-8")
    except (OrchestrationError, OSError, UnicodeError):
        raise WorkerLaunchError("COT probe request marker is invalid") from None
    if payload != f"{spec.profile_id}\n":
        raise WorkerLaunchError("COT probe request marker is invalid")


def _validate_ready_cot_request(spec: WorkerSpec) -> None:
    """Retry only marker validation while shell redirection finishes writing."""

    deadline = time.monotonic() + _REQUEST_PUBLICATION_GRACE_SECONDS
    while True:
        try:
            _validate_cot_request(spec)
            return
        except WorkerLaunchError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _write_cot_error_facts(spec: WorkerSpec) -> None:
    """Unblock the agent without fabricating an authoritative receipt."""

    _write_private_json(
        spec.cot_facts_path,
        {
            "schema_version": 1,
            "profile_id": spec.profile_id,
            "receipt_sha256": None,
            "status": "UNAVAILABLE",
            "failure_code": "TRUSTED_PROBE_ERROR",
        },
    )


def _perform_trusted_cot_handshake(
    spec: WorkerSpec, cot_probe_runner: CotProbeRunner
) -> None:
    """Consume the agent's marker and publish only validated sanitized facts."""

    try:
        _validate_ready_cot_request(spec)
        if spec.cot_receipt_path.exists() or spec.cot_facts_path.exists():
            raise WorkerLaunchError("trusted COT probe paths are not pristine")
        returned = cot_probe_runner(spec)
        receipt, receipt_sha256 = validate_cot_receipt(
            spec.cot_receipt_path, spec.profile_id
        )
        if not isinstance(returned, Mapping) or dict(returned) != receipt:
            raise WorkerLaunchError("trusted COT probe result is inconsistent")
        _write_private_json(
            spec.cot_facts_path,
            {
                "schema_version": 1,
                "profile_id": spec.profile_id,
                "receipt_sha256": receipt_sha256,
                "receipt": receipt,
            },
        )
    except (CotReceiptError, OSError, WorkerLaunchError):
        if not spec.cot_facts_path.exists():
            try:
                _write_cot_error_facts(spec)
            except WorkerLaunchError:
                pass


def _run_process_with_trusted_cot(
    spec: WorkerSpec,
    timeout_seconds: int,
    process_runner: ProcessRunner,
    cot_probe_runner: CotProbeRunner,
    live_probe_runner: LiveProbeRunner,
) -> int:
    """Coordinate one Codex process with all parent-owned live probes.

    The profile agent can only signal fixed scenario/attempt requests.  The
    parent executes the allowlisted probes, owns their authoritative receipts
    under the supervisor-denied output root, and returns non-authoritative facts
    copies for semantic judgment.
    """

    result: dict[str, Any] = {"exit_code": 125, "failed": False}

    def run_worker() -> None:
        try:
            result["exit_code"] = process_runner(spec, timeout_seconds)
        except Exception:
            result["failed"] = True

    worker = threading.Thread(target=run_worker, daemon=False)
    worker.start()
    cot_probe_handled = False
    live_handled: set[tuple[str, int]] = set()
    live_receipts: list[dict[str, Any]] = []

    def handle_visible_requests() -> None:
        nonlocal cot_probe_handled
        if not cot_probe_handled and spec.cot_request_path.exists():
            cot_probe_handled = True
            _perform_trusted_cot_handshake(spec, cot_probe_runner)
        for scenario_id in PARENT_LIVE_SCENARIO_IDS:
            for attempt in (1, 2):
                key = (scenario_id, attempt)
                request_path, _ = _live_handshake_paths(
                    spec, scenario_id, attempt
                )
                if key in live_handled or not request_path.exists():
                    continue
                live_handled.add(key)
                _perform_trusted_live_handshake(
                    spec,
                    scenario_id,
                    attempt,
                    live_probe_runner,
                    live_receipts,
                )

    while worker.is_alive():
        handle_visible_requests()
        worker.join(timeout=0.05)
    # Close the race where markers and process completion become visible in the
    # opposite order, then freeze the parent-owned aggregate exactly once.
    handle_visible_requests()
    _write_live_receipt_aggregate(spec, live_receipts)
    if result["failed"]:
        raise WorkerLaunchError("Codex worker process runner failed")
    exit_code = result["exit_code"]
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise WorkerLaunchError("Codex worker process runner failed")
    return exit_code


def _load_authoring_schema(path: Path) -> dict[str, Any]:
    resolved = _source_file(path, "Codex authoring schema", max_bytes=_MAX_SCHEMA_BYTES)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WorkerLaunchError("Codex authoring schema is invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("$defs"), dict):
        raise WorkerLaunchError("Codex authoring schema is invalid")
    if not isinstance(payload["$defs"].get("profileResult"), dict):
        raise WorkerLaunchError("Codex authoring schema is missing profileResult")
    return payload


def _referenced_definitions(
    root: Mapping[str, Any], definitions: Mapping[str, Any]
) -> set[str]:
    found: set[str] = set()
    pending: list[Any] = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.removeprefix("#/$defs/")
                if name not in definitions:
                    raise WorkerLaunchError(
                        "profile schema contains an unresolved reference"
                    )
                if name not in found:
                    found.add(name)
                    pending.append(definitions[name])
            pending.extend(value for key, value in node.items() if key != "$ref")
        elif isinstance(node, list):
            pending.extend(node)
    return found


def build_profile_schema(
    authoring: Mapping[str, Any],
    profile_id: str,
    expected_runtime: str = LOCKED_RUNTIME,
) -> dict[str, Any]:
    definitions = authoring.get("$defs")
    if not isinstance(definitions, dict):
        raise WorkerLaunchError("Codex authoring schema is invalid")
    root = deepcopy(definitions.get("profileResult"))
    try:
        root["properties"]["profile_id"] = {
            "type": "string",
            "enum": [profile_id],
        }
        root["properties"]["expected_runtime"] = {
            "type": "string",
            "enum": [expected_runtime],
        }
    except (KeyError, TypeError):
        raise WorkerLaunchError("profileResult schema is invalid") from None
    names = _referenced_definitions(root, definitions)
    root["$defs"] = {name: deepcopy(definitions[name]) for name in sorted(names)}
    errors = validate_authoring_schema(root)
    if errors:
        raise WorkerLaunchError(
            "derived profile schema is not strict-output compatible"
        )
    try:
        Draft202012Validator.check_schema(root)
    except Exception:
        raise WorkerLaunchError("derived profile schema is invalid") from None
    return root


def _manifest_profile(path: Path, expected_profile: str) -> dict[str, Any]:
    try:
        payload = load_private_json(
            path, "isolated profile manifest", max_bytes=_MAX_MANIFEST_BYTES
        )
    except OrchestrationError as exc:
        raise WorkerLaunchError(str(exc)) from None
    profiles = payload.get("profiles")
    if (
        payload.get("schema_version") != 1
        or not isinstance(profiles, list)
        or len(profiles) != 1
        or not isinstance(profiles[0], dict)
        or profiles[0].get("profile_id") != expected_profile
    ):
        raise WorkerLaunchError("isolated profile manifest matrix is invalid")
    return profiles[0]


def _validate_config_profiles(
    codex_home: Path,
    profile_manifest_dir: Path,
    worker_python: Path,
    qualification_mode: str,
) -> None:
    main = codex_home / "config.toml"
    try:
        _private_file(main, "base Codex config", max_bytes=_MAX_SCHEMA_BYTES)
        base = tomllib.loads(main.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, OrchestrationError):
        raise WorkerLaunchError("base Codex config is invalid") from None
    features = base.get("features")
    if (
        "agents" in base
        or not isinstance(features, dict)
        or features.get("multi_agent") is not False
        or features.get("hooks") is not False
    ):
        raise WorkerLaunchError("base Codex config enables unsafe orchestration")
    permissions = base.get("permissions")
    if not isinstance(permissions, dict):
        raise WorkerLaunchError("base Codex permissions are missing")
    for profile_id, agent_type in PROFILE_AGENT_TYPES:
        profile_path = codex_home / f"{agent_type}.config.toml"
        try:
            _private_file(
                profile_path, "Codex worker profile", max_bytes=_MAX_SCHEMA_BYTES
            )
            profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, OrchestrationError):
            raise WorkerLaunchError("Codex worker profile is invalid") from None
        expected_permission = worker_permission_profile(profile_id)
        shell_policy = profile.get("shell_environment_policy")
        fixed_environment = (
            shell_policy.get("set") if isinstance(shell_policy, dict) else None
        )
        included_environment = (
            shell_policy.get("include_only")
            if isinstance(shell_policy, dict)
            else None
        )
        expected_environment = {
            "QA_PRIVATE_MANIFEST": str(
                profile_manifest_dir / f"{profile_id}.json"
            ),
            "QA_PROFILE_ID": profile_id,
            "QA_AGENT_TYPE": agent_type,
            "QA_PYTHON_BIN": str(worker_python),
            "QA_QUALIFICATION_MODE": qualification_mode,
        }
        if (
            profile.get("default_permissions") != expected_permission
            or expected_permission not in permissions
            or "agents" in profile
            or "permissions" in profile
            or not isinstance(fixed_environment, dict)
            or not isinstance(included_environment, list)
            or not set(expected_environment).issubset(included_environment)
            or any(
                fixed_environment.get(name) != value
                for name, value in expected_environment.items()
            )
        ):
            raise WorkerLaunchError("Codex worker profile binding is invalid")


def _worker_environment(
    *,
    codex_home: Path,
    source_root: Path,
    artifact_root: Path,
    manifest: Path,
    profile_id: str,
    agent_type: str,
    home: Path,
    temporary: Path,
    work: Path,
    run_id: str,
    expected_sha: str,
    base_url: str,
    expected_runtime: str,
    qualification_mode: str,
    worker_python: Path,
) -> dict[str, str]:
    # This allowlist is constructed from scratch. Provider/admin secrets in the
    # launcher's parent environment are deliberately not inherited.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "CODEX_HOME": str(codex_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(source_root),
        "QA_PYTHON_BIN": str(worker_python),
        "QA_SOURCE_ROOT": str(source_root),
        "QA_ARTIFACT_DIR": str(artifact_root),
        "QA_RUN_ID": run_id,
        "QA_PRIVATE_MANIFEST": str(manifest),
        "QA_PROFILE_ID": profile_id,
        "QA_AGENT_TYPE": agent_type,
        "QA_WORK_ROOT": str(work),
        "QA_EXPECTED_DEPLOYMENT_SHA": expected_sha,
        "QA_EXPECTED_RUNTIME": expected_runtime,
        "QA_QUALIFICATION_MODE": qualification_mode,
        "IO_E2E_BASE_URL": base_url,
    }


def _run_process(spec: WorkerSpec, timeout_seconds: int) -> int:
    try:
        with spec.events_path.open(
            "wb", buffering=0
        ) as stdout_handle, spec.stderr_path.open("wb", buffering=0) as stderr_handle:
            process = subprocess.Popen(
                list(spec.command),
                cwd=spec.work,
                env=dict(spec.environment),
                stdin=subprocess.PIPE,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
            try:
                process.communicate(spec.prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:  # pragma: no cover - qualification runners are Linux.
                    process.kill()
                process.communicate()
                return 124
            return int(process.returncode)
    except OSError:
        return 126


def _prepare_specs(
    *,
    codex_bin: Path,
    codex_home: Path,
    source_root: Path,
    artifact_root: Path,
    profile_manifest_dir: Path,
    worker_root: Path,
    worker_output_root: Path,
    authoring_schema: Mapping[str, Any],
    worker_python: Path,
    run_id: str,
    base_url: str,
    expected_sha: str,
    expected_runtime: str = LOCKED_RUNTIME,
    qualification_mode: str = "release",
    assignments: Sequence[tuple[str, str]] = PROFILE_AGENT_TYPES,
) -> list[WorkerSpec]:
    specs: list[WorkerSpec] = []
    for profile_id, agent_type in assignments:
        manifest = profile_manifest_dir / f"{profile_id}.json"
        _manifest_profile(manifest, profile_id)
        agent_root = owned_directory(
            worker_root / agent_type, f"{profile_id} worker root"
        )
        home = owned_directory(agent_root / "home", f"{profile_id} home", empty=True)
        temporary = owned_directory(
            agent_root / "tmp", f"{profile_id} temp", empty=True
        )
        work = owned_directory(agent_root / "work", f"{profile_id} work", empty=True)
        output_dir = worker_output_root / profile_id
        try:
            output_dir.mkdir(mode=0o700)
            output_dir.chmod(0o700)
        except OSError:
            raise WorkerLaunchError("unable to create private worker output") from None
        schema_path = output_dir / "schema.json"
        result_path = output_dir / "result.json"
        events_path = output_dir / "events.jsonl"
        stderr_path = output_dir / "stderr.log"
        cot_receipt_path = output_dir / "cot-delivery-receipt.json"
        live_receipt_path = output_dir / "live-scenario-receipts.json"
        cot_request_path = work / ".cot-probe-request"
        cot_facts_path = work / "cot-delivery-facts.json"
        profile_schema = build_profile_schema(
            authoring_schema, profile_id, expected_runtime
        )
        _create_private_file(
            schema_path,
            (
                json.dumps(profile_schema, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8"),
        )
        for path in (result_path, events_path, stderr_path):
            _create_private_file(path)
        command = (
            str(codex_bin),
            "exec",
            "-p",
            agent_type,
            "-c",
            f'default_permissions="{worker_permission_profile(profile_id)}"',
            "--ignore-rules",
            "--strict-config",
            "--enable",
            "network_proxy",
            "--skip-git-repo-check",
            "--cd",
            str(work),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "--color",
            "never",
            "--json",
            "-",
        )
        environment = _worker_environment(
            codex_home=codex_home,
            source_root=source_root,
            artifact_root=artifact_root,
            manifest=manifest,
            profile_id=profile_id,
            agent_type=agent_type,
            home=home,
            temporary=temporary,
            work=work,
            run_id=run_id,
            expected_sha=expected_sha,
            base_url=base_url,
            expected_runtime=expected_runtime,
            qualification_mode=qualification_mode,
            worker_python=worker_python,
        )
        specs.append(
            WorkerSpec(
                profile_id=profile_id,
                agent_type=agent_type,
                command=command,
                environment=environment,
                work=work,
                output_dir=output_dir,
                schema_path=schema_path,
                result_path=result_path,
                events_path=events_path,
                stderr_path=stderr_path,
                cot_receipt_path=cot_receipt_path,
                cot_request_path=cot_request_path,
                cot_facts_path=cot_facts_path,
                live_receipt_path=live_receipt_path,
                prompt=_PROFILE_PROMPT
                + f"\nLocked assignment: {profile_id} ({agent_type}).\n",
            )
        )
    return specs


def _peak_from_attempts(attempts: Sequence[WorkerAttempt]) -> int:
    points: list[tuple[datetime, int]] = []
    for attempt in attempts:
        start = datetime.fromisoformat(attempt.started_at.replace("Z", "+00:00"))
        stop = datetime.fromisoformat(attempt.stopped_at.replace("Z", "+00:00"))
        points.extend(((start, 1), (stop, -1)))
    points.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    for _, delta in points:
        active += delta
        peak = max(peak, active)
    return peak


def _validate_result(
    spec: WorkerSpec,
) -> tuple[str, str | None, dict[str, Any]]:
    try:
        schema = load_private_json(
            spec.schema_path, "Codex worker schema", max_bytes=_MAX_SCHEMA_BYTES
        )
        result = load_private_json(
            spec.result_path, "Codex worker result", max_bytes=_MAX_RESULT_BYTES
        )
        errors = list(Draft202012Validator(schema).iter_errors(result))
        if errors or result.get("profile_id") != spec.profile_id:
            raise WorkerLaunchError("Codex worker structured result is invalid")
        thread_id, session_id = parse_exec_events(spec.events_path)
        return thread_id, session_id, result
    except OrchestrationError as exc:
        raise WorkerLaunchError(str(exc)) from None


def _completed_command_execution_count(path: Path) -> int:
    """Count completed shell executions without retaining command text."""

    try:
        count, _, _, _, _ = completed_command_evidence(path)
    except OrchestrationError:
        raise WorkerLaunchError("Codex worker event stream is invalid") from None
    return count


def _validated_worker_evidence(
    spec: WorkerSpec, identities: set[str]
) -> tuple[
    str,
    str | None,
    dict[str, Any],
    int,
    tuple[str, ...],
    dict[str, int],
    tuple[str, ...],
]:
    try:
        names = {entry.name for entry in spec.output_dir.iterdir()}
    except OSError:
        raise WorkerLaunchError("worker output is unreadable") from None
    if (
        not _WORKER_AUTHORED_OUTPUT_FILES.issubset(names)
        or names - _EXPECTED_OUTPUT_FILES
    ):
        raise WorkerLaunchError("worker output contains missing or extra files")
    thread_id, session_id, profile_result = _validate_result(spec)
    try:
        (
            completed_commands,
            scenario_command_ids,
            sop_read_first,
            scenario_command_counts,
            p0_06_phases,
        ) = completed_command_evidence(spec.events_path)
    except OrchestrationError:
        raise WorkerLaunchError("Codex worker event stream is invalid") from None
    if completed_commands < 1:
        raise WorkerToolUseError(
            "Codex worker returned without executing qualification tools"
        )
    if (
        not sop_read_first
        or scenario_command_ids != AGENT_LIVE_SCENARIO_IDS
        or not scenario_command_contract_satisfied(
            scenario_command_counts, p0_06_phases
        )
    ):
        raise WorkerScenarioToolUseError(
            completed_commands,
            scenario_command_ids,
            scenario_command_counts,
            p0_06_phases,
        )
    if thread_id in identities:
        raise WorkerLaunchError("independent Codex worker identity is duplicated")
    return (
        thread_id,
        session_id,
        profile_result,
        completed_commands,
        scenario_command_ids,
        scenario_command_counts,
        p0_06_phases,
    )


def _validate_cot_result_binding(
    profile_result: Mapping[str, Any], receipt: Mapping[str, Any]
) -> None:
    """Require agent-authored P0-12 projections to match trusted receipt facts."""

    reasoning = profile_result.get("reasoning")
    scenarios = profile_result.get("scenarios")
    if not isinstance(reasoning, Mapping) or not isinstance(scenarios, list):
        raise WorkerLaunchError("COT receipt does not match worker result")
    cot_scenarios = [
        row
        for row in scenarios
        if isinstance(row, Mapping) and row.get("scenario_id") == "P0-12"
    ]
    if len(cot_scenarios) != 1:
        raise WorkerLaunchError("COT receipt does not match worker result")
    scenario = cot_scenarios[0]
    assertions = scenario.get("assertions")
    if not isinstance(assertions, Mapping):
        raise WorkerLaunchError("COT receipt does not match worker result")

    request_id = receipt.get("request_id")
    turn_id = receipt.get("turn_id")
    trace_id = receipt.get("trace_id")
    expected_request_ids = [request_id] if request_id else []
    expected_turn_ids = [turn_id] if turn_id else []
    expected_trace_ids = [trace_id] if trace_id else []
    token_present = bool(
        receipt.get("token_metadata_status") == "PRESENT"
        and isinstance(receipt.get("reasoning_token_count"), int)
        and not isinstance(receipt.get("reasoning_token_count"), bool)
        and receipt.get("reasoning_token_count", 0) > 0
    )
    capability_enabled = receipt.get("reasoning_event_count") == 1
    requested_medium = receipt.get("requested_effort") == "medium"
    configured_medium = bool(
        receipt.get("configured_effort") == "medium"
        and receipt.get("configured_effort_attested") is True
    )
    configured_route_matches = (
        receipt.get("configured_route_matches_manifest") is True
    )
    effective_not_attested = bool(
        receipt.get("effective_effort") == "unknown"
        and receipt.get("effective_effort_attested") is False
    )
    receipt_status = receipt.get("status")
    receipt_code = receipt.get("failure_code")
    if not requested_medium or receipt.get("configured_effort_attested") is not True:
        expected_status = "BLOCKED_EVIDENCE"
        expected_failure_code = "PRECONDITION_MISSING"
    elif not configured_route_matches:
        expected_status = "PRODUCT_FAIL"
        expected_failure_code = "MODEL_ROUTE_MISMATCH"
    elif not configured_medium:
        expected_status = "PRODUCT_FAIL"
        expected_failure_code = "REASONING_EFFORT_CLAMPED"
    elif receipt_status == "PASS" and not token_present:
        expected_status = "BLOCKED_EVIDENCE"
        expected_failure_code = "REASONING_TOKENS_MISSING"
    elif receipt_status == "PASS":
        expected_status = "PASS"
        expected_failure_code = None
    elif receipt_status == "FAIL":
        expected_status = "PRODUCT_FAIL"
        expected_failure_code = {
            "FINAL_ANSWER_WRONG": "CONTENT_ASSERTION_FAILED",
            "DOWNSTREAM_PARSE_DROPPED_REASONING": "REASONING_METADATA_MISSING",
            "THINKING_ENVELOPE_NOT_DELIVERED": "DISCLOSURE_MISSING",
            "THINKING_ENVELOPE_UNREADABLE": "DISCLOSURE_MISSING",
            "THINKING_METADATA_INVALID": "REASONING_METADATA_MISSING",
        }.get(str(receipt_code))
    else:
        expected_status = "BLOCKED_EVIDENCE"
        expected_failure_code = {
            "CHAT_TIMEOUT": "CHAT_TIMEOUT",
            "CHAT_REQUEST_FAILED": "TRACE_UNAVAILABLE",
            "MODEL_REASONING_NOT_OBSERVED": "TRACE_INCOMPLETE",
            "TRACE_AMBIGUOUS": "TRACE_INCOMPLETE",
            "TRACE_UNAVAILABLE": "TRACE_UNAVAILABLE",
        }.get(str(receipt_code))
    attempt_results = scenario.get("attempt_results")
    scenario_failure = scenario.get("failure")
    expected_failure_matches = (
        scenario_failure is None
        if expected_failure_code is None
        else isinstance(scenario_failure, Mapping)
        and scenario_failure.get("category") == expected_status
        and scenario_failure.get("stage_code") == "REASONING"
        and scenario_failure.get("failure_code") == expected_failure_code
    )
    attempt_failure_matches = False
    if isinstance(attempt_results, list) and len(attempt_results) == 1:
        attempt = attempt_results[0]
        attempt_failure = (
            attempt.get("failure") if isinstance(attempt, Mapping) else None
        )
        attempt_failure_matches = (
            attempt_failure is None
            if expected_failure_code is None
            else isinstance(attempt_failure, Mapping)
            and attempt_failure.get("category") == expected_status
            and attempt_failure.get("stage_code") == "REASONING"
            and attempt_failure.get("failure_code") == expected_failure_code
        )
    if (
        (expected_status != "PASS" and expected_failure_code is None)
        or scenario.get("status") != expected_status
        or scenario.get("attempts") != 1
        or not isinstance(attempt_results, list)
        or len(attempt_results) != 1
        or not isinstance(attempt_results[0], Mapping)
        or attempt_results[0].get("attempt") != 1
        or attempt_results[0].get("status") != expected_status
        or not expected_failure_matches
        or not attempt_failure_matches
        or (
            expected_status != "PASS"
            and profile_result.get("status") == "PASS"
        )
        or reasoning.get("request_id") != request_id
        or reasoning.get("turn_id") != turn_id
        or reasoning.get("trace_id") != trace_id
        or reasoning.get("expected") is not True
        or reasoning.get("capability_enabled") is not capability_enabled
        or reasoning.get("requested_effort") != receipt.get("requested_effort")
        or reasoning.get("configured_effort") != receipt.get("configured_effort")
        or reasoning.get("effective_effort") != receipt.get("effective_effort")
        or reasoning.get("reasoning_event_count")
        != receipt.get("reasoning_event_count")
        or reasoning.get("metadata_present") != receipt.get("metadata_present")
        or reasoning.get("token_metadata_present") is not token_present
        or reasoning.get("user_visible_disclosure_present")
        != receipt.get("user_visible_disclosure_present")
        or reasoning.get("reasoning_token_count")
        != receipt.get("reasoning_token_count")
        or reasoning.get("disclosure_length")
        != receipt.get("delivered_thinking_len")
        or str(reasoning.get("kind") or "")
        != receipt.get("delivered_thinking_kind")
        or str(reasoning.get("source") or "")
        != receipt.get("delivered_thinking_source")
        or str(reasoning.get("model") or "")
        != receipt.get("delivered_thinking_model")
        or reasoning.get("raw_private_reasoning_stored") is not False
        or scenario.get("request_ids") != expected_request_ids
        or scenario.get("turn_ids") != expected_turn_ids
        or scenario.get("trace_ids") != expected_trace_ids
        or assertions.get("objective_answer_correct")
        != receipt.get("final_answer_correct")
        or assertions.get("reasoning_capability_enabled") is not capability_enabled
        or assertions.get("reasoning_requested_effort_medium") is not requested_medium
        or assertions.get("reasoning_configured_effort_medium")
        is not configured_medium
        or assertions.get("reasoning_effective_effort_not_attested")
        is not effective_not_attested
        or assertions.get("reasoning_event_observed")
        is not (receipt.get("reasoning_event_count") == 1)
        or assertions.get("reasoning_metadata_present")
        != receipt.get("metadata_present")
        or assertions.get("reasoning_tokens_present") is not token_present
        or assertions.get("user_disclosure_present")
        != receipt.get("user_visible_disclosure_present")
        or assertions.get("raw_private_reasoning_omitted") is not True
    ):
        raise WorkerLaunchError("COT receipt does not match worker result")


def _validate_live_worker_evidence(
    spec: WorkerSpec, profile_result: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    try:
        receipts, receipt_sha256 = validate_live_scenario_receipts(
            spec.live_receipt_path,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
        )
        validate_live_result_binding(profile_result, receipts)
    except (LiveScenarioReceiptError, OSError):
        raise WorkerLaunchError(
            "live scenario receipt does not match worker result"
        ) from None
    return receipts, receipt_sha256


def _diagnostic_fallback_result(
    spec: WorkerSpec,
    manifest_dir: Path,
    *,
    expected_runtime: str,
) -> dict[str, Any]:
    manifest_profile = _manifest_profile(
        manifest_dir / f"{spec.profile_id}.json", spec.profile_id
    )
    try:
        result = agent_error_profile(
            manifest_profile,
            profile_id=spec.profile_id,
            expected_runtime=expected_runtime,
        )
        schema = load_private_json(
            spec.schema_path, "Codex worker schema", max_bytes=_MAX_SCHEMA_BYTES
        )
        errors = list(Draft202012Validator(schema).iter_errors(result))
    except (DiagnosticResultError, OrchestrationError):
        raise WorkerLaunchError("diagnostic fallback result is invalid") from None
    if errors:
        raise WorkerLaunchError("diagnostic fallback result is invalid")
    return result


def launch(
    *,
    codex_bin: Path,
    codex_home: Path,
    source_root: Path,
    artifact_root: Path,
    profile_manifest_dir: Path,
    worker_root: Path,
    worker_output_root: Path,
    aggregation_input_root: Path,
    authoring_schema_path: Path,
    receipt_path: Path,
    run_id: str,
    base_url: str,
    expected_sha: str,
    timeout_seconds: int,
    process_runner: ProcessRunner = _run_process,
    cot_probe_runner: CotProbeRunner = _run_trusted_cot_probe,
    live_probe_runner: LiveProbeRunner = _run_trusted_live_probe,
    diagnostic: bool = False,
    profile_ids: Sequence[str] | None = None,
    expected_runtime: str = LOCKED_RUNTIME,
    worker_python: Path | None = None,
) -> dict[str, Any]:
    executable = _trusted_executable(codex_bin)
    qualification_python = _trusted_worker_python(
        worker_python if worker_python is not None else Path(sys.executable)
    )
    codex_home = owned_directory(codex_home, "run-scoped CODEX_HOME")
    source_root = _source_directory(source_root, "source checkout")
    artifact_root = _source_directory(artifact_root, "public artifact root")
    manifests = owned_directory(profile_manifest_dir, "profile manifest directory")
    worker_root = owned_directory(worker_root, "worker root")
    outputs = owned_directory(worker_output_root, "worker output root", empty=True)
    aggregation = owned_directory(
        aggregation_input_root, "aggregation input root", empty=True
    )
    requested_profile_ids = tuple(profile_ids or ())
    if not diagnostic and requested_profile_ids:
        raise WorkerLaunchError("profile subsets require diagnostic mode")
    if diagnostic:
        if not requested_profile_ids:
            requested_profile_ids = tuple(
                profile_id for profile_id, _ in PROFILE_AGENT_TYPES
            )
        if len(set(requested_profile_ids)) != len(requested_profile_ids) or any(
            profile_id not in dict(PROFILE_AGENT_TYPES)
            for profile_id in requested_profile_ids
        ):
            raise WorkerLaunchError("diagnostic profile selection is invalid")
        requested = set(requested_profile_ids)
        assignments = tuple(
            assignment
            for assignment in PROFILE_AGENT_TYPES
            if assignment[0] in requested
        )
    else:
        assignments = PROFILE_AGENT_TYPES

    allowed_runtime_requirements = {BASELINE_RUNTIME, LOCKED_RUNTIME}
    if expected_runtime not in allowed_runtime_requirements:
        raise WorkerLaunchError("worker runtime expectation is invalid")

    if (
        not _SAFE_TOKEN_RE.fullmatch(run_id)
        or base_url != LOCKED_BASE_URL
        or not _SHA_RE.fullmatch(expected_sha)
        or not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 14_400
    ):
        raise WorkerLaunchError("worker launch contract is invalid")
    if (
        not receipt_path.is_absolute()
        or receipt_path.is_symlink()
        or receipt_path.exists()
    ):
        raise WorkerLaunchError("orchestration receipt path is unsafe")
    receipt_parent = owned_directory(
        receipt_path.parent, "orchestration receipt parent"
    )
    if any(
        left == right or left in right.parents or right in left.parents
        for index, left in enumerate(
            (codex_home, source_root, manifests, worker_root, outputs, aggregation)
        )
        for right in (
            codex_home,
            source_root,
            manifests,
            worker_root,
            outputs,
            aggregation,
        )[index + 1 :]
        if left != source_root and right != source_root
    ):
        raise WorkerLaunchError("private worker roots are not isolated")
    if any(
        source_root == private
        or source_root in private.parents
        or private in source_root.parents
        for private in (
            codex_home,
            manifests,
            worker_root,
            outputs,
            aggregation,
            receipt_path,
        )
    ):
        raise WorkerLaunchError("private worker roots overlap the source checkout")
    if (
        not diagnostic
        and artifact_root != source_root
        and source_root not in artifact_root.parents
    ):
        raise WorkerLaunchError("public artifact root is outside the source checkout")
    if any(
        artifact_root == private
        or artifact_root in private.parents
        or private in artifact_root.parents
        for private in (
            codex_home,
            manifests,
            worker_root,
            outputs,
            aggregation,
            receipt_path,
        )
    ):
        raise WorkerLaunchError("public artifact root overlaps private worker data")
    if any(
        private == ambient or ambient in private.parents
        for private in (
            codex_home,
            manifests,
            worker_root,
            outputs,
            aggregation,
            receipt_path,
            artifact_root,
        )
        for ambient in _AMBIENT_READ_ROOTS
    ):
        raise WorkerLaunchError("private worker roots are ambient-readable")
    if any(
        root in receipt_path.parents
        for root in (manifests, worker_root, outputs, aggregation)
    ):
        raise WorkerLaunchError("orchestration receipt path is not isolated")
    if receipt_path.parent.resolve() != receipt_parent:
        raise WorkerLaunchError("orchestration receipt parent is unsafe")
    _validate_config_profiles(
        codex_home,
        manifests,
        qualification_python,
        "diagnostic" if diagnostic else "release",
    )
    authoring = _load_authoring_schema(authoring_schema_path)
    specs = _prepare_specs(
        codex_bin=executable,
        codex_home=codex_home,
        source_root=source_root,
        artifact_root=artifact_root,
        profile_manifest_dir=manifests,
        worker_root=worker_root,
        worker_output_root=outputs,
        authoring_schema=authoring,
        worker_python=qualification_python,
        run_id=run_id,
        base_url=base_url,
        expected_sha=expected_sha,
        expected_runtime=expected_runtime,
        qualification_mode="diagnostic" if diagnostic else "release",
        assignments=assignments,
    )

    attempts: list[WorkerAttempt] = []
    active = 0
    observed_peak = 0
    lock = threading.Lock()

    def invoke(spec: WorkerSpec) -> WorkerAttempt:
        nonlocal active, observed_peak
        with lock:
            active += 1
            observed_peak = max(observed_peak, active)
            if active > MAX_CONFIGURED_CONCURRENCY:
                raise WorkerLaunchError("worker concurrency exceeded the fixed cap")
            started_at = _utc_now()
        failed = False
        try:
            exit_code = _run_process_with_trusted_cot(
                spec,
                timeout_seconds,
                process_runner,
                cot_probe_runner,
                live_probe_runner,
            )
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                failed = True
                exit_code = 125
        except Exception:
            failed = True
            exit_code = 125
        finally:
            with lock:
                stopped_at = _utc_now()
                active -= 1
        return WorkerAttempt(
            spec=spec,
            exit_code=exit_code,
            started_at=started_at,
            stopped_at=stopped_at,
            invocation_failed=failed,
        )

    # Three fixed batches (3+3+2) guarantee at most three simultaneous processes.
    # Every locked profile is attempted exactly once even when an earlier worker
    # fails.
    with ThreadPoolExecutor(max_workers=MAX_CONFIGURED_CONCURRENCY) as executor:
        for offset in range(0, len(specs), MAX_CONFIGURED_CONCURRENCY):
            batch = specs[offset : offset + MAX_CONFIGURED_CONCURRENCY]
            futures = [executor.submit(invoke, spec) for spec in batch]
            attempts.extend(future.result() for future in futures)

    if len(attempts) != len(assignments) or not (
        1 <= observed_peak <= MAX_CONFIGURED_CONCURRENCY
    ):
        raise WorkerLaunchError("one or more independent Codex workers failed")
    if not diagnostic and any(
        attempt.invocation_failed or attempt.exit_code != 0 for attempt in attempts
    ):
        raise WorkerLaunchError("one or more independent Codex workers failed")

    identities: set[str] = set()
    workers: list[dict[str, Any]] = []
    for (expected_profile, expected_agent), attempt in zip(
        assignments, attempts, strict=True
    ):
        spec = attempt.spec
        if (spec.profile_id, spec.agent_type) != (expected_profile, expected_agent):
            raise WorkerLaunchError(
                "worker launch order differs from the locked matrix"
            )

        fallback_reason: str | None = None
        thread_id: str | None = None
        session_id: str | None = None
        profile_result: dict[str, Any]
        events_sha256: str | None = None
        cot_receipt: dict[str, Any] | None = None
        cot_receipt_sha256: str | None = None
        live_receipt_sha256: str | None = None
        cot_evidence_failure: str | None = None
        completed_command_count: int | None = None
        scenario_command_ids: tuple[str, ...] = ()
        scenario_command_counts: dict[str, int] = {}
        p0_06_command_phases: tuple[str, ...] = ()
        if diagnostic and attempt.invocation_failed:
            fallback_reason = _DIAGNOSTIC_FALLBACK_INVOCATION
        elif diagnostic and attempt.exit_code != 0:
            fallback_reason = _DIAGNOSTIC_FALLBACK_PROCESS
        elif diagnostic:
            try:
                (
                    thread_id,
                    session_id,
                    profile_result,
                    completed_command_count,
                    scenario_command_ids,
                    scenario_command_counts,
                    p0_06_command_phases,
                ) = _validated_worker_evidence(spec, identities)
                validate_live_attempts(profile_result)
                _, live_receipt_sha256 = _validate_live_worker_evidence(
                    spec, profile_result
                )
                events_sha256 = file_sha256(
                    spec.events_path,
                    "Codex worker event stream",
                    max_bytes=_MAX_EVENTS_BYTES,
                )
            except WorkerToolUseError:
                thread_id = None
                session_id = None
                events_sha256 = None
                completed_command_count = 0
                fallback_reason = _DIAGNOSTIC_FALLBACK_TOOL_USE
            except WorkerScenarioToolUseError as exc:
                thread_id = None
                session_id = None
                events_sha256 = None
                completed_command_count = exc.count
                scenario_command_ids = exc.scenario_ids
                scenario_command_counts = exc.scenario_counts
                p0_06_command_phases = exc.p0_06_phases
                fallback_reason = _DIAGNOSTIC_FALLBACK_SCENARIO_TOOL_USE
            except (
                DiagnosticAttemptError,
                OrchestrationError,
                OSError,
                WorkerLaunchError,
            ):
                thread_id = None
                session_id = None
                events_sha256 = None
                fallback_reason = _DIAGNOSTIC_FALLBACK_WORKER_EVIDENCE
            cot_path = spec.cot_receipt_path
            if not cot_path.exists():
                cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_MISSING
            else:
                try:
                    cot_receipt, cot_receipt_sha256 = validate_cot_receipt(
                        cot_path, spec.profile_id
                    )
                except (CotReceiptError, OSError):
                    cot_receipt = None
                    cot_receipt_sha256 = None
                    cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_INVALID
                else:
                    if fallback_reason is None:
                        try:
                            _validate_cot_result_binding(
                                profile_result, cot_receipt
                            )
                        except WorkerLaunchError:
                            # The deterministic receipt remains trustworthy even
                            # when the agent-authored projection disagrees with it.
                            cot_evidence_failure = (
                                _DIAGNOSTIC_COT_BINDING_MISMATCH
                            )
            if fallback_reason is not None:
                thread_id = None
                session_id = None
        else:
            (
                thread_id,
                session_id,
                profile_result,
                completed_command_count,
                scenario_command_ids,
                scenario_command_counts,
                p0_06_command_phases,
                ) = _validated_worker_evidence(spec, identities)
            _, live_receipt_sha256 = _validate_live_worker_evidence(
                spec, profile_result
            )
            cot_path = spec.cot_receipt_path
            if not cot_path.exists():
                raise WorkerLaunchError("Codex worker COT receipt is missing")
            try:
                cot_receipt, cot_receipt_sha256 = validate_cot_receipt(
                    cot_path, spec.profile_id
                )
                _validate_cot_result_binding(profile_result, cot_receipt)
            except (CotReceiptError, OSError, WorkerLaunchError):
                raise WorkerLaunchError("Codex worker COT receipt is invalid") from None
        if (
            diagnostic
            and cot_receipt is None
            and cot_evidence_failure is None
        ):
            cot_path = spec.cot_receipt_path
            if not cot_path.exists():
                cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_MISSING
            else:
                try:
                    cot_receipt, cot_receipt_sha256 = validate_cot_receipt(
                        cot_path, spec.profile_id
                    )
                except (CotReceiptError, OSError):
                    cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_INVALID

        canonical = aggregation / f"{spec.profile_id}.json"
        if fallback_reason is not None:
            profile_result = _diagnostic_fallback_result(
                spec, manifests, expected_runtime=expected_runtime
            )
            _write_private_json(canonical, profile_result)
        else:
            if thread_id is None:
                raise WorkerLaunchError("Codex worker event identity is invalid")
            identities.add(thread_id)
            if diagnostic:
                _write_private_json(canonical, profile_result)
            else:
                _copy_private_file(
                    spec.result_path, canonical, max_bytes=_MAX_RESULT_BYTES
                )
                events_sha256 = file_sha256(
                    spec.events_path,
                    "Codex worker event stream",
                    max_bytes=_MAX_EVENTS_BYTES,
                )

        worker = {
            "profile_id": spec.profile_id,
            "agent_type": spec.agent_type,
            "attempt": 1,
            "process_exit_code": attempt.exit_code,
            "worker_id": thread_id,
            "thread_id": thread_id,
            "session_id": session_id,
            "permission_profile": worker_permission_profile(spec.profile_id),
            "started_at": attempt.started_at,
            "stopped_at": attempt.stopped_at,
            "profile_result_sha256": canonical_json_sha256(profile_result),
            "exec_events_sha256": events_sha256,
            "live_receipt_sha256": live_receipt_sha256,
            "cot_receipt_sha256": cot_receipt_sha256,
            "cot_delivery_status": (
                cot_receipt.get("status") if cot_receipt else None
            ),
            "cot_failure_code": (
                cot_receipt.get("failure_code") if cot_receipt else None
            ),
        }
        if diagnostic:
            worker.update(
                {
                    "result_source": (
                        _DIAGNOSTIC_RESULT_SOURCE_FALLBACK
                        if fallback_reason is not None
                        else _DIAGNOSTIC_RESULT_SOURCE_CODEX
                    ),
                    "fallback_reason": fallback_reason,
                    "cot_evidence_failure": cot_evidence_failure,
                    "completed_command_execution_count": completed_command_count,
                    "completed_scenario_command_ids": list(scenario_command_ids),
                    "completed_scenario_command_counts": scenario_command_counts,
                    "p0_06_command_phases": list(p0_06_command_phases),
                    "live_receipt_sha256": live_receipt_sha256,
                    "cot_receipt_sha256": cot_receipt_sha256,
                    "cot_delivery_status": (
                        cot_receipt.get("status") if cot_receipt else None
                    ),
                    "cot_failure_code": (
                        cot_receipt.get("failure_code") if cot_receipt else None
                    ),
                    "cot_delivery_qualified": (
                        cot_receipt.get("delivery_qualified")
                        if cot_receipt
                        else None
                    ),
                    "cot_final_answer_correct": (
                        cot_receipt.get("final_answer_correct")
                        if cot_receipt
                        else None
                    ),
                    "cot_reasoning_event_count": (
                        cot_receipt.get("reasoning_event_count")
                        if cot_receipt
                        else None
                    ),
                    "cot_metadata_present": (
                        cot_receipt.get("metadata_present")
                        if cot_receipt
                        else None
                    ),
                    "cot_token_metadata_status": (
                        cot_receipt.get("token_metadata_status")
                        if cot_receipt
                        else None
                    ),
                    "cot_reasoning_token_count": (
                        cot_receipt.get("reasoning_token_count")
                        if cot_receipt
                        else None
                    ),
                    "cot_user_visible_disclosure_present": (
                        cot_receipt.get("user_visible_disclosure_present")
                        if cot_receipt
                        else None
                    ),
                }
            )
        workers.append(worker)
    if {entry.name for entry in aggregation.iterdir()} != {
        f"{profile_id}.json" for profile_id, _ in assignments
    }:
        raise WorkerLaunchError("canonical aggregation input matrix is incomplete")

    peak = _peak_from_attempts(attempts)
    if peak != observed_peak or not 1 <= peak <= MAX_CONFIGURED_CONCURRENCY:
        raise WorkerLaunchError("worker concurrency evidence is inconsistent")
    if diagnostic:
        receipt = {
            "schema_version": 1,
            "qualification_mode": "diagnostic",
            "release_qualified": False,
            "requested_profile_ids": [profile_id for profile_id, _ in assignments],
            "launcher_id": run_id,
            "max_configured_profile_concurrency": MAX_CONFIGURED_CONCURRENCY,
            "max_observed_profile_concurrency": peak,
            "launch_attempts": len(attempts),
            "workers": workers,
        }
    else:
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "launcher_id": run_id,
            "max_configured_profile_concurrency": MAX_CONFIGURED_CONCURRENCY,
            "max_observed_profile_concurrency": peak,
            "launch_attempts": len(attempts),
            "workers": workers,
        }
    try:
        write_receipt(receipt_path, receipt)
        if diagnostic:
            persisted = load_private_json(
                receipt_path,
                "diagnostic orchestration receipt",
                max_bytes=_MAX_SCHEMA_BYTES,
            )
            if persisted != receipt:
                raise OrchestrationError(
                    "diagnostic orchestration receipt is inconsistent"
                )
        else:
            verify(receipt_path, outputs, aggregation)
    except (OrchestrationError, OSError) as exc:
        try:
            receipt_path.unlink()
        except OSError:
            pass
        raise WorkerLaunchError(str(exc)) from None
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run eight isolated headless Codex API-key qualification workers"
    )
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--profile-manifest-dir", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, required=True)
    parser.add_argument("--worker-output-root", type=Path, required=True)
    parser.add_argument("--aggregation-input-root", type=Path, required=True)
    parser.add_argument("--authoring-schema", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--expected-runtime", default=LOCKED_RUNTIME)
    parser.add_argument("--worker-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="allow a non-release profile subset and emit a diagnostic receipt",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="profile ID to run in diagnostic mode (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        verify_codex_version(args.codex_bin)
        launch(
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            source_root=args.source_root,
            artifact_root=args.artifact_root,
            profile_manifest_dir=args.profile_manifest_dir,
            worker_root=args.worker_root,
            worker_output_root=args.worker_output_root,
            aggregation_input_root=args.aggregation_input_root,
            authoring_schema_path=args.authoring_schema,
            receipt_path=args.receipt,
            run_id=args.run_id,
            base_url=args.base_url,
            expected_sha=args.expected_sha,
            timeout_seconds=args.timeout_seconds,
            diagnostic=args.diagnostic,
            profile_ids=args.profile,
            expected_runtime=args.expected_runtime,
            worker_python=args.worker_python,
        )
    except WorkerLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: independent Codex worker launcher encountered an internal error",
            file=sys.stderr,
        )
        return 1
    finally:
        os.umask(previous_umask)
    if args.diagnostic:
        print("diagnostic Codex qualification workers completed")
    else:
        print("eight independent Codex qualification workers completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
