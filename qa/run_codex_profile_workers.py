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

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import genesis_e2e  # noqa: E402

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
        DETERMINISTIC_ASSERTIONS,
        SEMANTIC_ASSERTIONS,
        LiveScenarioReceiptError,
        canonical_json_sha256 as live_json_sha256,
        failed_persona_result_projection,
        latency_projection as live_latency_projection,
        persona_finalizer_failure,
        unfinalized_persona_result_projection,
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
        DETERMINISTIC_ASSERTIONS,
        SEMANTIC_ASSERTIONS,
        LiveScenarioReceiptError,
        canonical_json_sha256 as live_json_sha256,
        failed_persona_result_projection,
        latency_projection as live_latency_projection,
        persona_finalizer_failure,
        unfinalized_persona_result_projection,
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
_MAX_PERSONA_JUDGMENT_BYTES = 64 * 1024
_REQUEST_PUBLICATION_GRACE_SECONDS = 2.0
_LIVE_REQUEST_PROTOCOL_VIOLATION = "LIVE_REQUEST_PROTOCOL_VIOLATION"
_COT_PROBE_TIMEOUT_SECONDS = 300
_DEFAULT_LIVE_PROBE_TIMEOUT_SECONDS = 300
_LIVE_PROBE_TIMEOUT_SECONDS = {
    "P0-06": 1500,
    "P0-07": 600,
    "P0-09": 1500,
}
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
DIAGNOSTIC_FALLBACK_REASONS = frozenset(
    (
        _DIAGNOSTIC_FALLBACK_INVOCATION,
        _DIAGNOSTIC_FALLBACK_PROCESS,
        _DIAGNOSTIC_FALLBACK_WORKER_EVIDENCE,
        _DIAGNOSTIC_FALLBACK_TOOL_USE,
        _DIAGNOSTIC_FALLBACK_SCENARIO_TOOL_USE,
    )
)
DIAGNOSTIC_COT_EVIDENCE_FAILURES = frozenset(
    (
        _DIAGNOSTIC_FALLBACK_COT_MISSING,
        _DIAGNOSTIC_FALLBACK_COT_INVALID,
        _DIAGNOSTIC_COT_BINDING_MISMATCH,
    )
)
DIAGNOSTIC_FAILURE_CODES_BY_STAGE = {
    "INVOCATION": frozenset(("INVOCATION_FAILED",)),
    "PROCESS_EXIT": frozenset(("PROCESS_EXIT_NONZERO",)),
    "OUTPUT_FILE_SET": frozenset(("OUTPUT_FILE_SET_INVALID",)),
    "STRUCTURED_RESULT": frozenset(("STRUCTURED_RESULT_INVALID",)),
    "EVENT_IDENTITY_PARSE": frozenset(
        (
            "EVENT_IDENTITY_PARSE_INVALID",
            "EVENT_IDENTITY_DUPLICATED",
            "EVENT_STREAM_DIGEST_INVALID",
        )
    ),
    "COMMAND_EVIDENCE_PARSE": frozenset(("COMMAND_EVIDENCE_PARSE_INVALID",)),
    "SCENARIO_COMMAND_EVIDENCE": frozenset(
        ("AGENT_TOOL_USE_MISSING", "AGENT_SCENARIO_TOOL_USE_MISSING")
    ),
    "LIVE_RECEIPT_LOAD": frozenset(("LIVE_RECEIPT_INVALID",)),
    "LIVE_RECEIPT_PROJECTION": frozenset(("LIVE_RECEIPT_PROJECTION_INVALID",)),
    "LIVE_RECEIPT_SHAPE": frozenset(("LIVE_RECEIPT_SHAPE_INVALID",)),
    "LIVE_RECEIPT_BINDING": frozenset(("LIVE_RECEIPT_BINDING_INVALID",)),
    "COT_RECEIPT_LOAD": frozenset(
        ("COT_RECEIPT_MISSING", "COT_RECEIPT_INVALID")
    ),
    "COT_BINDING": frozenset(("COT_RESULT_BINDING_MISMATCH",)),
    # Defensive catch-all for a sanitized validation exception that does not
    # originate at one of the explicitly instrumented boundaries above.
    "WORKER_EVIDENCE": frozenset(("WORKER_EVIDENCE_INVALID",)),
}
_PROFILE_PROMPT = """\
You are one independent intelligent qualification agent in the Feedling API-key
P0 suite. Read $QA_SOURCE_ROOT/qa/SOP.md,
$QA_SOURCE_ROOT/qa/coverage-lock.json, and
$QA_SOURCE_ROOT/qa/scenarios/api-key-journey.md before acting. QA_PRIVATE_MANIFEST is an
owner-only one-row manifest for exactly your assigned profile. Test only that
profile against IO_E2E_BASE_URL and execute all locked scenarios in order.
This is a live qualification run, not a source-code review. Complete only the
required SOP, coverage lock, scenario, manifest, private-facts, and finalizer reads.
Do not inspect QA implementation files or tests, enumerate the repository, search
for status precedence, or reverse-engineer the launcher. The supplied commands,
facts, scenario contract, and output schema are the complete interface. Spend
the turn budget executing the journey, making the bounded semantic judgments,
and returning the structured result.
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
Exactly one live-scenario command may be active at a time. A yielded tool call
that reports a running cell or session is not complete: poll or wait on that
same execution identifier until it reaches terminal exit. While it is running,
do not start another tool call, issue another scenario request, or read any
facts file. Only after terminal exit may you read that command's facts, finish
the current scenario, and proceed to the next command or scenario.
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
P0-06 requires exactly three ordered, successful phase-marker commands. The
CAPTURE command only requests the parent-owned network mutation; REVIEW and
FINALIZE remain offline semantic steps. Run
these exact commands in separate Codex tool calls:
QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=CAPTURE "$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py" --scenario P0-06 --attempt 1 --request "$QA_WORK_ROOT/.live-probe-P0-06-1.request" --facts "$QA_WORK_ROOT/live-probe-P0-06-1.facts.json"
QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=REVIEW "$QA_PYTHON_BIN" -I -B -c 'import hashlib,json,pathlib,sys;p=pathlib.Path(sys.argv[1]);j=pathlib.Path(sys.argv[2]);j.exists() and sys.exit(17);b=p.read_bytes();d=json.loads(b);isinstance(d,dict) or sys.exit(18);"evidence_sha256" not in d or sys.exit(19);d["evidence_sha256"]=hashlib.sha256(b).hexdigest();print(json.dumps(d,sort_keys=True,separators=(",",":")))' "$QA_WORK_ROOT/p0-06-private-evidence.json" "$QA_WORK_ROOT/p0-06-semantic-judgment.json"
After observing REVIEW output, make the semantic decisions and write the bounded
owner-only judgment to exactly
`$QA_WORK_ROOT/p0-06-semantic-judgment.json` in a separate unmarked tool call.
That JSON object MUST contain exactly these eight keys and no aliases:
`schema_version`, `judge`, `evidence_sha256`, `reviewed_surfaces`,
`reviewed_fact_ids`, `persona_identity_consistent`,
`ground_truth_facts_supported`, and `contradictions_absent`. Set
`schema_version` to `1`, `judge` to `qualification_agent`,
`reviewed_surfaces` to exactly `["identity","persona","memories"]`, copy the
REVIEW command's computed `evidence_sha256` and the reviewed evidence's
`expected_fact_ids` into the
judgment's `evidence_sha256` and `reviewed_fact_ids` fields, and set the three
boolean fields from your actual semantic review. In particular, do not write
`expected_fact_ids`, `consistency`, `support`, or `contradiction` as judgment
keys. Never default the three decisions to true without reviewing the surfaces.
Then run:
QA_SCENARIO_ID=P0-06 QA_SCENARIO_PHASE=FINALIZE "$QA_PYTHON_BIN" -I -B "$QA_SOURCE_ROOT/qa/finalize_persona_review.py" --fixture "$QA_SOURCE_ROOT/qa/fixtures/persona-import-v1.json" --private-evidence "$QA_WORK_ROOT/p0-06-private-evidence.json" --semantic-judgment "$QA_WORK_ROOT/p0-06-semantic-judgment.json" --artifact-dir "$QA_ARTIFACT_DIR"
The sole diagnostic exception is a schema-valid non-PASS CAPTURE receipt with
no private evidence file. In that exact case, do not run REVIEW or FINALIZE:
record the failed capture and continue directly to P0-07. The trusted parent
binds this capture-only exception; it is never valid for a PASS receipt or a
release qualification.
This local finalizer operates only on the review copy so you can author the
bounded result. It exits zero for either a schema-valid positive or negative
verdict, and nonzero only for an operational/input error; never change your
judgment to make the command green. After your process exits, the parent independently applies the
same judgment to its worker-inaccessible authoritative capture and binds that
sanitized finalizer projection to the receipt set. A changed review copy,
changed judgment hash, or invented finalizer result therefore cannot pass.
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
For P0-12, request the trusted parent delivery probe exactly once, after P0-11
and before P0-13, using this exact single command even if an earlier scenario
failed or produced incomplete evidence:
QA_SCENARIO_ID=P0-12 "$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/qa/request_cot_delivery_probe.py" --request "$QA_WORK_ROOT/.cot-probe-request" --facts "$QA_WORK_ROOT/cot-delivery-facts.json"
The deterministic parent—not this agent—sends the reasoning turn and owns the
authoritative receipt outside every agent-writable root. The helper alone
creates the fixed marker, waits for the sanitized facts copy, and validates its
bounded receipt. A helper exit zero completes PASS, FAIL, or UNVERIFIED
evidence. Preserve FAIL/UNVERIFIED observations, do not retry the reasoning
turn, and bind the profile reasoning object plus the P0-12 scenario and turn IDs
to those facts. Never invoke qa/cot_delivery_probe.py; never directly create,
edit, replace, or delete the marker, facts copy, or authoritative receipt.
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
For P0-13, request the final parent-owned trace/latency/cleanup probe exactly
once after P0-12 with:
QA_SCENARIO_ID=P0-13 "$QA_PYTHON_BIN" "$QA_SOURCE_ROOT/qa/request_live_scenario_probe.py" --scenario P0-13 --attempt 1 --request "$QA_WORK_ROOT/.live-probe-P0-13-1.request" --facts "$QA_WORK_ROOT/live-probe-P0-13-1.facts.json"
Read the resulting facts and copy its exact P0-13 identifiers, assertions,
turn stage latencies, profile latency/trace summary, and cleanup projection.
The parent owns every product network call. Never call trace, hosting, model
configuration, registration, or account-reset endpoints yourself.
When QA_QUALIFICATION_MODE is diagnostic, do not call /v1/account/reset in
P0-13: the in-worker parent probe records trace/latency but defers mutation, and
the post-worker deterministic parent owns the only account reset so it can
verify cleanup without an admin token. Emit the fixed diagnostic deferral exactly:
P0-13 and its sole attempt use BLOCKED_EVIDENCE with CLEANUP /
PRECONDITION_MISSING / reproducible=true; preserve the parent receipt's exact
trace-stage, correlation, and latency assertions, keep cleanup_confirmed false,
and include only evidence codes whose bound assertions are true. The top-level
status is BLOCKED_EVIDENCE when P0-01 through P0-12 pass, and cleanup status is
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
agents. Always request parent cleanup except for the fixed diagnostic-only P0-13
deferral above. Return exactly one profileResult JSON object
matching the supplied output schema; include only sanitized structured evidence.
"""


class WorkerLaunchError(RuntimeError):
    """Sanitized fixed failure from the deterministic process boundary."""


class PersonaFinalizeError(WorkerLaunchError):
    """A fixed diagnostic code for independent persona re-finalization."""

    def __init__(self, failure_code: str) -> None:
        if failure_code not in {
            "SEMANTIC_JUDGMENT_INVALID",
            "PERSONA_FINALIZER_FAILED",
        }:
            raise WorkerLaunchError("persona finalizer failure code is invalid")
        super().__init__("trusted persona finalizer failed")
        self.failure_code = failure_code


class DiagnosticWorkerEvidenceError(WorkerLaunchError):
    """One fixed, allowlisted diagnostic evidence-boundary failure."""

    def __init__(self, failure_stage: str, failure_code: str) -> None:
        allowed = DIAGNOSTIC_FAILURE_CODES_BY_STAGE.get(failure_stage)
        if allowed is None or failure_code not in allowed:
            raise WorkerLaunchError("diagnostic worker failure code is invalid")
        super().__init__("Codex worker diagnostic evidence is invalid")
        self.failure_stage = failure_stage
        self.failure_code = failure_code


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
    [WorkerSpec, str, int, str, Sequence[Mapping[str, Any]]],
    tuple[Mapping[str, Any], Mapping[str, Any]],
]
PersonaFinalizeRunner = Callable[
    [WorkerSpec, Mapping[str, Any]], Mapping[str, Any]
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


def _redact_persona_review_event_output(path: Path) -> None:
    """Remove decrypted P0-06 REVIEW stdout from the private Codex event log."""

    if not path.exists():
        return
    temporary = path.with_name(f".{path.name}.persona-redacted")
    try:
        with open_owned_regular(
            path, "Codex worker event stream", max_bytes=_MAX_EVENTS_BYTES
        ) as handle:
            raw_lines = handle.readlines()
        rows: list[dict[str, Any]] = []
        review_item_ids: set[str] = set()
        for raw in raw_lines:
            if len(raw) > 16 * 1024 * 1024:
                raise WorkerLaunchError("Codex worker event stream is invalid")
            try:
                row = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise WorkerLaunchError("Codex worker event stream is invalid") from None
            if not isinstance(row, dict):
                raise WorkerLaunchError("Codex worker event stream is invalid")
            item = row.get("item")
            if isinstance(item, Mapping):
                command = item.get("command")
                if (
                    isinstance(command, str)
                    and "QA_SCENARIO_ID=P0-06" in command
                    and "QA_SCENARIO_PHASE=REVIEW" in command
                    and isinstance(item.get("id"), str)
                ):
                    review_item_ids.add(str(item["id"]))
            rows.append(row)
        changed = False
        rendered: list[bytes] = []
        for row in rows:
            item = row.get("item")
            if isinstance(item, Mapping):
                command = item.get("command")
                review_row = bool(
                    (
                        isinstance(command, str)
                        and "QA_SCENARIO_ID=P0-06" in command
                        and "QA_SCENARIO_PHASE=REVIEW" in command
                    )
                    or (
                        isinstance(item.get("id"), str)
                        and str(item["id"]) in review_item_ids
                    )
                )
                if review_row:
                    safe_item = {
                        key: item[key]
                        for key in ("type", "id", "command", "status", "exit_code")
                        if key in item
                    }
                    safe_item["output_redacted"] = True
                    row = {
                        key: row[key]
                        for key in ("type", "thread_id", "turn_id", "timestamp")
                        if key in row
                    }
                    row["item"] = safe_item
                    changed = True
            rendered.append(
                (
                    json.dumps(
                        row,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
        if not changed:
            return
        _unlink_private_path(temporary)
        _create_private_file(temporary, b"".join(rendered))
        os.replace(temporary, path)
    except (
        OSError,
        TypeError,
        ValueError,
        RecursionError,
        OrchestrationError,
        WorkerLaunchError,
    ):
        _unlink_private_path(temporary)
        _unlink_private_path(path)
        raise WorkerLaunchError("unable to redact persona review evidence") from None


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
            timeout=_COT_PROBE_TIMEOUT_SECONDS,
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


def _persona_authoritative_evidence_path(spec: WorkerSpec) -> Path:
    return spec.output_dir / ".p0-06-authoritative-evidence.json"


def _persona_worker_evidence_path(spec: WorkerSpec) -> Path:
    return spec.work / "p0-06-private-evidence.json"


def _persona_judgment_path(spec: WorkerSpec) -> Path:
    return spec.work / "p0-06-semantic-judgment.json"


def _unlink_private_path(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _run_trusted_persona_finalize(
    spec: WorkerSpec, capture_receipt: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Re-finalize the immutable parent capture against Codex's judgment.

    The worker may finalize its review copy to understand the bounded result,
    but only this independent finalization of the parent-owned copy is trusted
    by the receipt/result binder.  Every plaintext copy and the bounded
    judgment are removed after the worker exits, including failure paths.
    """

    authoritative = _persona_authoritative_evidence_path(spec)
    worker_copy = _persona_worker_evidence_path(spec)
    judgment = _persona_judgment_path(spec)
    fixture_path = Path(spec.environment["QA_SOURCE_ROOT"]) / "qa" / "fixtures" / "persona-import-v1.json"
    try:
        # The judgment is worker-authored. Bound it before handing it to the
        # legacy Genesis reader (which otherwise reads the entire file) so a
        # prompt-injected worker cannot OOM the trusted parent. The worker has
        # already exited, so this owned-regular-file check also closes the
        # replacement race for the subsequent finalization call.
        _private_file(
            judgment,
            "persona semantic judgment",
            max_bytes=_MAX_PERSONA_JUDGMENT_BYTES,
        )
        report = genesis_e2e.finalize_existing_session_distill_acceptance(
            private_evidence_path=str(authoritative),
            semantic_judgment_path=str(judgment),
            fixture=genesis_e2e._load_fixture(str(fixture_path)),
            artifact_dir=str(spec.environment["QA_ARTIFACT_DIR"]),
        )
        capture = capture_receipt.get("result_projection")
        evidence = report.get("evidence") if isinstance(report, Mapping) else None
        checks = report.get("checks") if isinstance(report, Mapping) else None
        transport = report.get("transport") if isinstance(report, Mapping) else None
        privacy = report.get("privacy") if isinstance(report, Mapping) else None
        request_ids = capture_receipt.get("request_ids")
        if (
            capture_receipt.get("scenario_id") != "P0-06"
            or capture_receipt.get("status") != "PASS"
            or not isinstance(capture, Mapping)
            or not isinstance(evidence, Mapping)
            or not isinstance(checks, Mapping)
            or not isinstance(transport, Mapping)
            or not isinstance(privacy, Mapping)
            or not isinstance(request_ids, list)
            or len(request_ids) != 1
            or evidence.get("sha256") != capture.get("evidence_sha256")
            or report.get("job_id") != capture.get("job_id")
            or transport.get("archive_upload_count")
            != capture.get("archive_upload_count")
            or checks.get("archive_receipts_verified")
            != capture.get("archive_receipts_verified")
            or checks.get("genesis_upload_metadata_verified")
            != capture.get("genesis_upload_metadata_verified")
            or evidence.get("semantic_judgment_bound") is not True
            or evidence.get("private_evidence_deleted") is not True
            or type(privacy.get("violation_count")) is not int
            or privacy.get("violation_count", -1) < 0
        ):
            raise PersonaFinalizeError(
                "PERSONA_FINALIZER_FAILED"
            )
        finalizer = {
            "fixture_id": "persona-import-v1",
            "evidence_sha256": evidence["sha256"],
            "request_id": request_ids[0],
            "job_id": report["job_id"],
            "semantic_judgment_bound": True,
            "finalizer_ok": report.get("ok") is True,
            "private_evidence_deleted": True,
            "archive_upload_count": transport["archive_upload_count"],
            "archive_receipts_verified": checks[
                "archive_receipts_verified"
            ],
            "genesis_upload_metadata_verified": checks[
                "genesis_upload_metadata_verified"
            ],
            "privacy_violation_count": privacy["violation_count"],
        }
        return {
            "kind": "persona_finalizer",
            "semantic_assertions": {
                "persona_acceptance_passed": report.get("ok") is True,
                "privacy_canary_absent": privacy["violation_count"] == 0,
            },
            "persona_finalizer": finalizer,
        }
    except PersonaFinalizeError:
        raise
    except genesis_e2e.ExistingSessionDistillError as exc:
        raise PersonaFinalizeError(
            "SEMANTIC_JUDGMENT_INVALID"
            if exc.stage == "semantic"
            else "PERSONA_FINALIZER_FAILED"
        ) from None
    except WorkerLaunchError:
        raise PersonaFinalizeError("SEMANTIC_JUDGMENT_INVALID") from None
    except (
        OrchestrationError,
        OSError,
        KeyError,
        TypeError,
    ):
        raise PersonaFinalizeError("PERSONA_FINALIZER_FAILED") from None
    finally:
        for path in (authoritative, worker_copy, judgment):
            _unlink_private_path(path)


def _trace_cleanup_turn_context(
    spec: WorkerSpec, prior_receipts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for receipt in prior_receipts:
        scenario_id = str(receipt.get("scenario_id") or "")
        if scenario_id not in {"P0-08", "P0-09", "P0-10", "P0-11"}:
            continue
        for turn in receipt.get("turns", []):
            if not isinstance(turn, Mapping):
                raise WorkerLaunchError("trusted trace-cleanup context is invalid")
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "turn_index": turn.get("turn_index"),
                    "request_id": turn.get("request_id"),
                    "turn_id": turn.get("turn_id"),
                    "trace_id": turn.get("trace_id"),
                    "ack_latency_ms": turn.get("ack_latency_ms"),
                    "reply_latency_ms": turn.get("reply_latency_ms"),
                    "reply_count": turn.get("reply_count"),
                    "content_assertion_passed": turn.get(
                        "content_assertion_passed"
                    ),
                    "fallback_detected": turn.get("fallback_detected"),
                    "duplicate_detected": turn.get("duplicate_detected"),
                    "out_of_order_detected": turn.get("out_of_order_detected"),
                }
            )
    try:
        cot, _ = validate_cot_receipt(spec.cot_receipt_path, spec.profile_id)
    except (CotReceiptError, OSError):
        cot = None
    # A valid negative COT receipt can legitimately have no correlated turn
    # identifiers (for example CHAT_REQUEST_FAILED).  Preserve every real turn
    # that exists, but never invent identifiers merely to make the latency
    # projection look complete.  Missing or invalid COT evidence similarly
    # omits only P0-12 so P0-13 can inspect all trustworthy live turns.  The
    # independent COT validation after live-receipt validation still rejects a
    # strict/release run and records the bounded diagnostic failure.
    cot_ids = (
        tuple(cot.get(field) for field in ("request_id", "turn_id", "trace_id"))
        if cot is not None
        else ()
    )
    if cot is not None and all(
        isinstance(value, str) and value for value in cot_ids
    ):
        rows.append(
            {
                "scenario_id": "P0-12",
                "turn_index": 1,
                "request_id": cot_ids[0],
                "turn_id": cot_ids[1],
                "trace_id": cot_ids[2],
                "ack_latency_ms": cot.get("ack_latency_ms"),
                "reply_latency_ms": cot.get("reply_latency_ms"),
                "reply_count": cot.get("chat_response_match_count"),
                "content_assertion_passed": cot.get("final_answer_correct"),
                "fallback_detected": False,
                "duplicate_detected": cot.get("chat_response_count") != 1,
                "out_of_order_detected": cot.get("chat_response_match_count") != 1,
            }
        )
    return {
        "schema_version": 1,
        "run_id": str(spec.environment["QA_RUN_ID"]),
        "profile_id": spec.profile_id,
        "turns": rows,
    }


def _run_trusted_live_probe(
    spec: WorkerSpec,
    scenario_id: str,
    attempt: int,
    nonce: str,
    prior_receipts: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Execute one allowlisted scenario in a fixed parent-owned subprocess."""

    source_root = Path(spec.environment["QA_SOURCE_ROOT"])
    worker_python = Path(spec.environment["QA_PYTHON_BIN"])
    receipt_path = spec.output_dir / f".live-{scenario_id}-{attempt}.receipt.json"
    private_facts_path = spec.output_dir / f".live-{scenario_id}-{attempt}.private.json"
    context_path = spec.output_dir / f".live-{scenario_id}-{attempt}.context.json"
    authoritative_persona = _persona_authoritative_evidence_path(spec)
    worker_persona = _persona_worker_evidence_path(spec)
    preserve_persona_capture = False
    if (
        receipt_path.exists()
        or private_facts_path.exists()
        or context_path.exists()
        or (scenario_id == "P0-06" and authoritative_persona.exists())
        or (scenario_id == "P0-06" and worker_persona.exists())
    ):
        raise WorkerLaunchError("trusted live probe paths are not pristine")
    command: tuple[str, ...] = (
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
        "--qualification-mode",
        str(spec.environment["QA_QUALIFICATION_MODE"]),
    )
    try:
        if scenario_id == "P0-06":
            command += (
                "--persona-fixture",
                str(source_root / "qa" / "fixtures" / "persona-import-v1.json"),
                "--persona-private-evidence",
                str(authoritative_persona),
                "--artifact-dir",
                str(spec.environment["QA_ARTIFACT_DIR"]),
            )
        elif scenario_id == "P0-13":
            _write_private_json(
                context_path, _trace_cleanup_turn_context(spec, prior_receipts)
            )
            command += ("--prior-turn-context", str(context_path))
        completed = subprocess.run(
            command,
            cwd=source_root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            capture_output=True,
            check=False,
            timeout=_LIVE_PROBE_TIMEOUT_SECONDS.get(
                scenario_id, _DEFAULT_LIVE_PROBE_TIMEOUT_SECONDS
            ),
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
        if scenario_id == "P0-06" and receipt["status"] == "PASS":
            _copy_private_file(
                authoritative_persona,
                worker_persona,
                max_bytes=8 * 1024 * 1024,
            )
            preserve_persona_capture = True
        return receipt, private_facts
    except (
        LiveScenarioReceiptError,
        OrchestrationError,
        OSError,
        subprocess.SubprocessError,
    ):
        raise WorkerLaunchError("trusted live probe could not execute") from None
    finally:
        for path in (receipt_path, private_facts_path, context_path):
            _unlink_private_path(path)
        if scenario_id == "P0-06" and not preserve_persona_capture:
            for path in (authoritative_persona, worker_persona):
                _unlink_private_path(path)


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
        and last.get("failure_code") in {"CHAT_TIMEOUT", "MISSING_REPLY"}
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
    spec: WorkerSpec,
    scenario_id: str,
    attempt: int,
    facts_path: Path,
    *,
    failure_code: str = "TRUSTED_PROBE_ERROR",
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
            "failure_code": failure_code,
        },
    )


def _premature_live_request_keys(
    spec: WorkerSpec,
    receipts: Sequence[Mapping[str, Any]],
    current_key: tuple[str, int],
) -> set[tuple[str, int]]:
    """Find markers issued before the current trusted probe was published."""

    represented = {
        (str(receipt.get("scenario_id") or ""), receipt.get("attempt"))
        for receipt in receipts
    }
    premature: set[tuple[str, int]] = set()
    for scenario_id in PARENT_LIVE_SCENARIO_IDS:
        for attempt in (1, 2):
            key = (scenario_id, attempt)
            if key == current_key or key in represented:
                continue
            request_path, _ = _live_handshake_paths(spec, scenario_id, attempt)
            if request_path.exists():
                premature.add(key)
    return premature


def _diagnostic_probe_error_evidence(
    spec: WorkerSpec,
    scenario_id: str,
    attempt: int,
    nonce: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a fail-closed parent receipt after a trusted probe crashes.

    This is diagnostic-only evidence that the row was attempted but could not
    be observed.  It never asserts user success and is not accepted by formal
    release orchestration.
    """

    private_facts = {
        "schema_version": 1,
        "run_id": str(spec.environment["QA_RUN_ID"]),
        "profile_id": spec.profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "observations": {"error_stage": "trusted_probe"},
    }
    request_ids = (
        []
        if scenario_id in {"P0-08", "P0-09", "P0-10", "P0-11"}
        else [f"probe-{nonce}"]
    )
    now = _utc_now()
    receipt = {
        "schema_version": 1,
        "kind": "live_scenario_probe",
        "run_id": str(spec.environment["QA_RUN_ID"]),
        "profile_id": spec.profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "nonce": nonce,
        "started_at": now,
        "finished_at": now,
        "status": "BLOCKED_EVIDENCE",
        "failure_code": "LIVE_PROBE_ERROR",
        "assertions": {
            key: False for key in DETERMINISTIC_ASSERTIONS[scenario_id]
        },
        "semantic_assertions": list(SEMANTIC_ASSERTIONS[scenario_id]),
        "request_ids": request_ids,
        "turn_ids": [],
        "trace_ids": [],
        "turns": [],
        "result_projection": None,
        "private_facts_sha256": live_json_sha256(private_facts),
        "raw_content_stored": False,
    }
    return (
        validate_live_receipt_object(
            receipt,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            scenario_id=scenario_id,
            attempt=attempt,
        ),
        private_facts,
    )


def _load_ready_live_request(
    path: Path,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
    previous_receipt_sha256: str | None,
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
                previous_receipt_sha256=previous_receipt_sha256,
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
) -> set[tuple[str, int]]:
    request_path, facts_path = _live_handshake_paths(spec, scenario_id, attempt)
    nonce = ""
    probe_started = False
    premature: set[tuple[str, int]] = set()
    try:
        _load_ready_live_request(
            request_path,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            scenario_id=scenario_id,
            attempt=attempt,
            previous_receipt_sha256=(
                live_json_sha256(receipts[-1]) if receipts else None
            ),
        )
        if facts_path.exists() or not _live_request_is_next(
            receipts, scenario_id, attempt
        ):
            raise WorkerLaunchError("trusted live probe request is out of order")
        nonce = f"live_{secrets.token_hex(16)}"
        probe_started = True
        returned_receipt, private_facts = live_probe_runner(
            spec, scenario_id, attempt, nonce, tuple(receipts)
        )
        premature = _premature_live_request_keys(
            spec, receipts, (scenario_id, attempt)
        )
        if premature:
            for premature_scenario, premature_attempt in premature:
                _, premature_facts = _live_handshake_paths(
                    spec, premature_scenario, premature_attempt
                )
                _write_live_error_facts(
                    spec,
                    premature_scenario,
                    premature_attempt,
                    premature_facts,
                    failure_code=_LIVE_REQUEST_PROTOCOL_VIOLATION,
                )
            _write_live_error_facts(
                spec,
                scenario_id,
                attempt,
                facts_path,
                failure_code=_LIVE_REQUEST_PROTOCOL_VIOLATION,
            )
            raise WorkerLaunchError("live probe request protocol was violated")
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
        return premature
    except (
        LiveProbeRequestError,
        LiveScenarioReceiptError,
        OSError,
        WorkerLaunchError,
    ):
        if (
            probe_started
            and nonce
            and spec.environment.get("QA_QUALIFICATION_MODE") == "diagnostic"
            and not facts_path.exists()
        ):
            try:
                receipt, private_facts = _diagnostic_probe_error_evidence(
                    spec, scenario_id, attempt, nonce
                )
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
                return premature
            except (
                LiveScenarioReceiptError,
                OSError,
                WorkerLaunchError,
            ):
                pass
        try:
            _write_live_error_facts(spec, scenario_id, attempt, facts_path)
        except WorkerLaunchError:
            pass
    return premature


def _write_live_receipt_aggregate(
    spec: WorkerSpec,
    receipts: Sequence[Mapping[str, Any]],
    persona_finalizer: Mapping[str, Any] | None,
) -> None:
    payload = {
        "schema_version": 1,
        "kind": "live_scenario_receipt_set",
        "run_id": str(spec.environment["QA_RUN_ID"]),
        "profile_id": spec.profile_id,
        "receipts": [dict(row) for row in receipts],
        "persona_finalizer": (
            dict(persona_finalizer) if persona_finalizer is not None else None
        ),
    }
    # Validate complete successful sets before persistence.  Partial/error sets
    # are still persisted so release verification fails closed with a bounded
    # evidence error instead of silently omitting the parent-owned artifact.
    try:
        validate_aggregate_object(
            payload,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            allow_failed_persona=(
                spec.environment.get("QA_QUALIFICATION_MODE") == "diagnostic"
            ),
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
    persona_finalize_runner: PersonaFinalizeRunner,
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
                premature = _perform_trusted_live_handshake(
                    spec,
                    scenario_id,
                    attempt,
                    live_probe_runner,
                    live_receipts,
                )
                live_handled.update(premature)

    while worker.is_alive():
        handle_visible_requests()
        worker.join(timeout=0.05)
    # Close the race where markers and process completion become visible in the
    # opposite order, then freeze the parent-owned aggregate exactly once.
    handle_visible_requests()
    persona_finalizer: Mapping[str, Any] | None = None
    capture_receipts = [
        receipt
        for receipt in live_receipts
        if receipt.get("scenario_id") == "P0-06"
    ]
    try:
        if (
            len(capture_receipts) == 1
            and capture_receipts[0].get("status") == "PASS"
        ):
            try:
                persona_finalizer = persona_finalize_runner(
                    spec, capture_receipts[0]
                )
            except PersonaFinalizeError as exc:
                persona_finalizer = persona_finalizer_failure(
                    exc.failure_code
                )
            except Exception:
                # Preserve the worker's actual exit/tool-use diagnostic.  The
                # bounded diagnostic sentinel retains all other parent-owned
                # receipts while strict release validation still fails closed.
                persona_finalizer = persona_finalizer_failure(
                    "PERSONA_FINALIZER_FAILED"
                )
    finally:
        try:
            _write_live_receipt_aggregate(
                spec, live_receipts, persona_finalizer
            )
        finally:
            if persona_finalizer is None or persona_finalizer.get(
                "kind"
            ) == "persona_finalizer_failure":
                for path in (
                    _persona_authoritative_evidence_path(spec),
                    _persona_worker_evidence_path(spec),
                    _persona_judgment_path(spec),
                ):
                    _unlink_private_path(path)
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
        permission = permissions.get(expected_permission)
        network = permission.get("network") if isinstance(permission, dict) else None
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
            or not isinstance(network, dict)
            or network.get("enabled") is not False
            or "domains" in network
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
            "--disable",
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
            raise DiagnosticWorkerEvidenceError(
                "STRUCTURED_RESULT", "STRUCTURED_RESULT_INVALID"
            )
    except OrchestrationError:
        raise DiagnosticWorkerEvidenceError(
            "STRUCTURED_RESULT", "STRUCTURED_RESULT_INVALID"
        ) from None
    try:
        thread_id, session_id = parse_exec_events(spec.events_path)
    except OrchestrationError:
        raise DiagnosticWorkerEvidenceError(
            "EVENT_IDENTITY_PARSE", "EVENT_IDENTITY_PARSE_INVALID"
        ) from None
    return thread_id, session_id, result


def _completed_command_execution_count(path: Path) -> int:
    """Count completed shell executions without retaining command text."""

    try:
        count, _, _, _, _ = completed_command_evidence(path)
    except OrchestrationError:
        raise WorkerLaunchError("Codex worker event stream is invalid") from None
    return count


def _validated_worker_evidence(
    spec: WorkerSpec,
    identities: set[str],
    *,
    allow_failed_persona_capture: bool = False,
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
        raise DiagnosticWorkerEvidenceError(
            "OUTPUT_FILE_SET", "OUTPUT_FILE_SET_INVALID"
        ) from None
    if (
        not _WORKER_AUTHORED_OUTPUT_FILES.issubset(names)
        or names - _EXPECTED_OUTPUT_FILES
    ):
        raise DiagnosticWorkerEvidenceError(
            "OUTPUT_FILE_SET", "OUTPUT_FILE_SET_INVALID"
        )
    thread_id, session_id, profile_result = _validate_result(spec)
    try:
        (
            completed_commands,
            scenario_command_ids,
            sop_read_first,
            scenario_command_counts,
            p0_06_phases,
        ) = completed_command_evidence(
            spec.events_path,
            allow_failed_persona_capture=allow_failed_persona_capture,
        )
    except OrchestrationError:
        raise DiagnosticWorkerEvidenceError(
            "COMMAND_EVIDENCE_PARSE", "COMMAND_EVIDENCE_PARSE_INVALID"
        ) from None
    if completed_commands < 1:
        raise WorkerToolUseError(
            "Codex worker returned without executing qualification tools"
        )
    if (
        not sop_read_first
        or scenario_command_ids != AGENT_LIVE_SCENARIO_IDS
        or not scenario_command_contract_satisfied(
            scenario_command_counts,
            p0_06_phases,
            allow_failed_persona_capture=allow_failed_persona_capture,
        )
    ):
        raise WorkerScenarioToolUseError(
            completed_commands,
            scenario_command_ids,
            scenario_command_counts,
            p0_06_phases,
        )
    if thread_id in identities:
        raise DiagnosticWorkerEvidenceError(
            "EVENT_IDENTITY_PARSE", "EVENT_IDENTITY_DUPLICATED"
        )
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


def _validate_projected_result_shape(
    spec: WorkerSpec, profile_result: Mapping[str, Any]
) -> None:
    """Revalidate a parent-projected diagnostic result against the worker schema."""

    try:
        schema = load_private_json(
            spec.schema_path, "Codex worker schema", max_bytes=_MAX_SCHEMA_BYTES
        )
        errors = list(Draft202012Validator(schema).iter_errors(profile_result))
    except (OrchestrationError, TypeError, ValueError, RecursionError):
        raise WorkerLaunchError("parent-projected worker result is invalid") from None
    if errors or profile_result.get("profile_id") != spec.profile_id:
        raise WorkerLaunchError("parent-projected worker result is invalid")


def _failure_projection(
    *, status: str, stage_code: str, failure_code: str
) -> dict[str, Any] | None:
    if status == "PASS":
        return None
    return {
        "category": status,
        "stage_code": stage_code,
        "failure_code": failure_code,
        "reproducible": True,
    }


_TRACE_STAGE_NAMES = ("routing", "queue", "provider", "persistence", "delivery")


def _failed_trace_cleanup_receipt(
    aggregate: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the one bounded diagnostic P0-13 probe-crash receipt, if present."""

    receipts = aggregate.get("receipts")
    if not isinstance(receipts, list):
        return None
    rows = [
        row
        for row in receipts
        if isinstance(row, Mapping) and row.get("scenario_id") == "P0-13"
    ]
    if len(rows) != 1:
        return None
    receipt = rows[0]
    assertions = receipt.get("assertions")
    if (
        receipt.get("status") != "BLOCKED_EVIDENCE"
        or receipt.get("failure_code") != "LIVE_PROBE_ERROR"
        or receipt.get("result_projection") is not None
        or receipt.get("turns") != []
        or receipt.get("turn_ids") != []
        or receipt.get("trace_ids") != []
        or not isinstance(assertions, Mapping)
        or set(assertions) != set(DETERMINISTIC_ASSERTIONS["P0-13"])
        or any(value is not False for value in assertions.values())
    ):
        return None
    return receipt


def _unavailable_trace_cleanup_projection(
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project only evidence that remains trustworthy after P0-13 crashes."""

    return {
        "kind": "trace_cleanup",
        "latency": live_latency_projection(turns),
        "trace": {
            "enabled": False,
            "deploy_enabled": False,
            "correlated_event_count": 0,
            "observed_event_types": [],
            "missing_required_event_types": list(_TRACE_STAGE_NAMES),
            "raw_trace_stored": False,
        },
        "cleanup": {
            "attempted": False,
            "provider_config_deleted": False,
            "account_reset": False,
            "old_credential_rejected": False,
            "status": "BLOCKED_EVIDENCE",
        },
    }


def _trusted_prior_turns_with_unavailable_stages(
    aggregate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Retain prior receipt-owned turns without inventing final trace timings."""

    receipts = aggregate.get("receipts")
    if not isinstance(receipts, list):
        raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
    turns: list[dict[str, Any]] = []
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
        if receipt.get("scenario_id") == "P0-13":
            continue
        receipt_turns = receipt.get("turns")
        if not isinstance(receipt_turns, list):
            raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
        for turn in receipt_turns:
            if not isinstance(turn, Mapping):
                raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
            projected = deepcopy(dict(turn))
            projected["stage_latency_ms"] = {
                stage: None for stage in _TRACE_STAGE_NAMES
            }
            turns.append(projected)
    return turns


def _validate_diagnostic_live_projection(
    profile_result: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    *,
    allow_failed_persona: bool,
) -> None:
    """Validate normal evidence or the bounded P0-13 probe-crash projection."""

    failed_trace = _failed_trace_cleanup_receipt(aggregate)
    if failed_trace is None:
        validate_live_attempts(profile_result)
        validate_live_result_binding(
            profile_result,
            aggregate,
            allow_failed_persona=allow_failed_persona,
        )
        return

    # Reuse the strict attempt validator for P0-02 through P0-12.  Its sole
    # P0-13 exception models deterministic cleanup deferral, so validate a
    # private view with that fixed failure while retaining the real trace
    # failure in the canonical result.
    attempt_view = deepcopy(dict(profile_result))
    scenarios = attempt_view.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 13:
        raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
    p0_13 = scenarios[-1]
    if not isinstance(p0_13, dict) or p0_13.get("scenario_id") != "P0-13":
        raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
    deferred_failure = {
        "category": "BLOCKED_EVIDENCE",
        "stage_code": "CLEANUP",
        "failure_code": "PRECONDITION_MISSING",
        "reproducible": True,
    }
    p0_13["failure"] = deferred_failure
    p0_13["attempt_results"] = [
        {
            "attempt": 1,
            "status": "BLOCKED_EVIDENCE",
            "failure": dict(deferred_failure),
        }
    ]
    validate_live_attempts(attempt_view)

    # The persisted receipt correctly has no P0-13 projection.  Build a
    # validation-only derivative from earlier trusted receipts so the existing
    # binder can still prove every retained identifier and transport fact.  No
    # trace stage, correlation, or cleanup success is synthesized.
    trusted_turns = _trusted_prior_turns_with_unavailable_stages(aggregate)
    binding_aggregate = deepcopy(dict(aggregate))
    binding_receipts = binding_aggregate.get("receipts")
    if not isinstance(binding_receipts, list):
        raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
    binding_trace = next(
        (
            row
            for row in binding_receipts
            if isinstance(row, dict) and row.get("scenario_id") == "P0-13"
        ),
        None,
    )
    if not isinstance(binding_trace, dict):
        raise WorkerLaunchError("failed trace-cleanup evidence is unavailable")
    binding_trace["failure_code"] = "TRACE_UNAVAILABLE"
    binding_trace["turns"] = trusted_turns
    binding_trace["result_projection"] = _unavailable_trace_cleanup_projection(
        trusted_turns
    )
    validate_live_result_binding(
        profile_result,
        binding_aggregate,
        allow_failed_persona=allow_failed_persona,
    )


def _project_diagnostic_live_evidence(
    profile_result: Mapping[str, Any], aggregate: Mapping[str, Any]
) -> dict[str, Any]:
    """Install receipt-owned live fields without replacing agent semantics.

    The agent remains the judge for P0-10/P0-11 and authors the P0-06 semantic
    judgment.  The parent already binds that persona judgment independently.
    Identifiers, timestamps, transport assertions, stage latencies, cleanup,
    and the bound persona finalizer are deterministic receipt facts and should
    never depend on a model copying long opaque values without a typo.
    """

    result = deepcopy(dict(profile_result))
    scenarios = result.get("scenarios")
    turns = result.get("turns")
    receipts = aggregate.get("receipts")
    parent_persona = aggregate.get("persona_finalizer")
    if (
        not isinstance(scenarios, list)
        or not isinstance(turns, list)
        or not isinstance(receipts, list)
        or not (isinstance(parent_persona, Mapping) or parent_persona is None)
    ):
        raise WorkerLaunchError("live diagnostic projection is unavailable")
    by_scenario = {
        row.get("scenario_id"): row
        for row in scenarios
        if isinstance(row, dict)
    }
    if len(by_scenario) != len(scenarios):
        raise WorkerLaunchError("live diagnostic projection is unavailable")
    grouped: dict[str, list[Mapping[str, Any]]] = {
        scenario_id: [] for scenario_id in PARENT_LIVE_SCENARIO_IDS
    }
    for receipt in receipts:
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("scenario_id") not in grouped
        ):
            raise WorkerLaunchError("live diagnostic projection is unavailable")
        grouped[str(receipt["scenario_id"])].append(receipt)
    if any(not grouped[scenario_id] for scenario_id in PARENT_LIVE_SCENARIO_IDS):
        raise WorkerLaunchError("live diagnostic projection is unavailable")

    prior_turns = {
        (row.get("scenario_id"), row.get("trace_id")): row
        for row in turns
        if isinstance(row, Mapping)
    }
    projected_turns: list[dict[str, Any]] = []
    trace_cleanup_receipt: Mapping[str, Any] | None = None
    deterministic_scenarios = {
        "P0-02",
        "P0-03",
        "P0-04",
        "P0-05",
        "P0-07",
        "P0-08",
        "P0-09",
    }

    for scenario_id in PARENT_LIVE_SCENARIO_IDS:
        scenario = by_scenario.get(scenario_id)
        rows = grouped[scenario_id]
        if not isinstance(scenario, dict):
            raise WorkerLaunchError("live diagnostic projection is unavailable")
        scenario.update(
            {
                "started_at": rows[0]["started_at"],
                "finished_at": rows[-1]["finished_at"],
                "attempts": len(rows),
                "request_ids": [
                    value for receipt in rows for value in receipt["request_ids"]
                ],
                "turn_ids": [
                    value for receipt in rows for value in receipt["turn_ids"]
                ],
                "trace_ids": [
                    value for receipt in rows for value in receipt["trace_ids"]
                ],
            }
        )
        assertions = scenario.get("assertions")
        if not isinstance(assertions, dict):
            raise WorkerLaunchError("live diagnostic projection is unavailable")
        for key, value in rows[-1]["assertions"].items():
            if key in assertions:
                assertions[key] = value

        attempt_results = scenario.get("attempt_results")
        if not isinstance(attempt_results, list) or len(attempt_results) != len(rows):
            raise WorkerLaunchError("live diagnostic projection is unavailable")
        for index, (attempt_result, receipt) in enumerate(
            zip(attempt_results, rows, strict=True), start=1
        ):
            if not isinstance(attempt_result, dict):
                raise WorkerLaunchError("live diagnostic projection is unavailable")
            attempt_result["attempt"] = index
            if scenario_id in deterministic_scenarios or receipt["status"] != "PASS":
                attempt_result["status"] = receipt["status"]
                if receipt["status"] == "PASS":
                    attempt_result["failure"] = None
        if scenario_id in deterministic_scenarios:
            scenario["status"] = rows[-1]["status"]
            if scenario["status"] == "PASS":
                scenario["failure"] = None

        if scenario_id == "P0-06":
            if parent_persona is None and rows[-1]["status"] != "PASS":
                capture_failure = failed_persona_result_projection(rows[-1])
                failure = capture_failure["failure"]
                scenario.update(
                    {
                        **capture_failure,
                        "attempt_results": [
                            {
                                "attempt": 1,
                                "status": capture_failure["status"],
                                "failure": failure,
                            }
                        ],
                    }
                )
                if result.get("status") == "PASS":
                    result["status"] = capture_failure["status"]
            elif (
                rows[-1]["status"] == "PASS"
                and isinstance(parent_persona, Mapping)
                and parent_persona.get("kind")
                == "persona_finalizer_failure"
            ):
                review_failure = unfinalized_persona_result_projection(
                    rows[-1], parent_persona
                )
                failure = review_failure["failure"]
                scenario.update(
                    {
                        **review_failure,
                        "attempt_results": [
                            {
                                "attempt": 1,
                                "status": review_failure["status"],
                                "failure": failure,
                            }
                        ],
                    }
                )
                if result.get("status") == "PASS":
                    result["status"] = review_failure["status"]
            else:
                if not isinstance(parent_persona, Mapping):
                    raise WorkerLaunchError(
                        "live diagnostic projection is unavailable"
                    )
                semantic = parent_persona.get("semantic_assertions")
                finalizer = parent_persona.get("persona_finalizer")
                if not isinstance(semantic, Mapping) or not isinstance(
                    finalizer, Mapping
                ):
                    raise WorkerLaunchError(
                        "live diagnostic projection is unavailable"
                    )
                assertions = {**rows[-1]["assertions"], **dict(semantic)}
                scenario["assertions"] = assertions
                scenario["persona_finalizer"] = dict(finalizer)
                privacy_ok = semantic.get("privacy_canary_absent") is True
                acceptance_ok = semantic.get("persona_acceptance_passed") is True
                if privacy_ok and acceptance_ok:
                    status = "PASS"
                    failure = None
                elif not privacy_ok:
                    status = "SECURITY_FAIL"
                    failure = _failure_projection(
                        status=status,
                        stage_code="PERSONA_IMPORT",
                        failure_code="REDACTION_ASSERTION_FAILED",
                    )
                else:
                    status = "PRODUCT_FAIL"
                    failure = _failure_projection(
                        status=status,
                        stage_code="PERSONA_IMPORT",
                        failure_code="PERSONA_ACCEPTANCE_FAILED",
                    )
                scenario.update(
                    {
                        "status": status,
                        "failure": failure,
                        "attempt_results": [
                            {"attempt": 1, "status": status, "failure": failure}
                        ],
                        "evidence_codes": [
                            code
                            for assertion, code in (
                                (
                                    "persona_files_archived",
                                    "PERSONA_FILES_ARCHIVED",
                                ),
                                (
                                    "persona_source_metadata_verified",
                                    "PERSONA_SOURCE_METADATA_VERIFIED",
                                ),
                                ("persona_import_done", "PERSONA_IMPORT_DONE"),
                                (
                                    "persona_acceptance_passed",
                                    "PERSONA_ACCEPTANCE_PASSED",
                                ),
                                ("privacy_canary_absent", "PRIVACY_CANARY_ABSENT"),
                            )
                            if assertions.get(assertion) is True
                        ],
                    }
                )

        if scenario_id == "P0-13":
            receipt = rows[-1]
            status = str(receipt["status"])
            failed_trace_cleanup = bool(
                receipt.get("result_projection") is None
                and receipt.get("failure_code") == "LIVE_PROBE_ERROR"
            )
            failure_code = (
                "TRACE_UNAVAILABLE"
                if failed_trace_cleanup
                else str(receipt["failure_code"])
            )
            failure = _failure_projection(
                status=status,
                stage_code=(
                    "CLEANUP"
                    if failure_code == "PRECONDITION_MISSING"
                    else "TRACE_LATENCY_CLEANUP"
                ),
                failure_code=failure_code,
            )
            scenario.update(
                {
                    "status": status,
                    "attempt_results": [
                        {"attempt": 1, "status": status, "failure": failure}
                    ],
                    "assertions": dict(receipt["assertions"]),
                    "evidence_codes": [
                        code
                        for assertion, code in (
                            (
                                "trace_correlation_confirmed",
                                "TRACE_CORRELATION_CONFIRMED",
                            ),
                            ("latency_attributed", "LATENCY_ATTRIBUTED"),
                            ("cleanup_confirmed", "CLEANUP_CONFIRMED"),
                        )
                        if receipt["assertions"][assertion] is True
                    ],
                    "failure": failure,
                }
            )
            if status != "PASS" and result.get("status") == "PASS":
                result["status"] = status
            trace_cleanup_receipt = receipt
            continue

        for receipt in rows:
            for turn in receipt["turns"]:
                content_assertion = turn["content_assertion_passed"]
                if content_assertion is None:
                    prior = prior_turns.get((scenario_id, turn["trace_id"]))
                    prior_value = (
                        prior.get("content_assertion_passed")
                        if isinstance(prior, Mapping)
                        else None
                    )
                    content_assertion = (
                        prior_value if type(prior_value) is bool else False
                    )
                projected_turns.append(
                    {
                        "scenario_id": scenario_id,
                        "turn_index": turn["turn_index"],
                        "request_id": turn["request_id"],
                        "turn_id": turn["turn_id"],
                        "trace_id": turn["trace_id"],
                        "ack_latency_ms": turn["ack_latency_ms"],
                        "reply_latency_ms": turn["reply_latency_ms"],
                        "stage_latency_ms": dict(turn["stage_latency_ms"]),
                        "reply_count": turn["reply_count"],
                        "content_assertion_passed": content_assertion,
                        "fallback_detected": turn["fallback_detected"],
                        "duplicate_detected": turn["duplicate_detected"],
                        "out_of_order_detected": turn["out_of_order_detected"],
                    }
                )

    if trace_cleanup_receipt is None:
        raise WorkerLaunchError("live diagnostic projection is unavailable")
    trace_turns = trace_cleanup_receipt.get("turns")
    projection = trace_cleanup_receipt.get("result_projection")
    failed_trace_cleanup = _failed_trace_cleanup_receipt(aggregate)
    if failed_trace_cleanup is not None:
        projected_turns = [
            {
                **turn,
                "stage_latency_ms": {
                    stage: None for stage in _TRACE_STAGE_NAMES
                },
            }
            for turn in projected_turns
        ]
        projection = _unavailable_trace_cleanup_projection(projected_turns)
        result["turns"] = projected_turns
        result["latency"] = deepcopy(projection["latency"])
        result["trace"] = deepcopy(projection["trace"])
        result["cleanup"] = deepcopy(projection["cleanup"])
        return result
    if not isinstance(trace_turns, list) or not isinstance(projection, Mapping):
        raise WorkerLaunchError("live diagnostic projection is unavailable")
    projected_by_trace = {
        turn.get("trace_id"): turn
        for turn in trace_turns
        if isinstance(turn, Mapping)
    }
    for turn in projected_turns:
        projected = projected_by_trace.get(turn["trace_id"])
        if not isinstance(projected, Mapping):
            raise WorkerLaunchError("live diagnostic projection is unavailable")
        turn["stage_latency_ms"] = dict(projected["stage_latency_ms"])
    existing_trace_ids = {turn["trace_id"] for turn in projected_turns}
    cot_turns = [
        turn
        for turn in trace_turns
        if isinstance(turn, Mapping) and turn.get("trace_id") not in existing_trace_ids
    ]
    if len(cot_turns) != 1:
        raise WorkerLaunchError("live diagnostic projection is unavailable")
    cot_turn = cot_turns[0]
    projected_turns.append(
        {
            "scenario_id": "P0-12",
            # Profile turn indexes are scenario-local.  The trace receipt uses
            # a global sequence number across all probes, so never copy it
            # into the canonical profile row.
            "turn_index": 1,
            "request_id": cot_turn["request_id"],
            "turn_id": cot_turn["turn_id"],
            "trace_id": cot_turn["trace_id"],
            "ack_latency_ms": cot_turn["ack_latency_ms"],
            "reply_latency_ms": cot_turn["reply_latency_ms"],
            "stage_latency_ms": dict(cot_turn["stage_latency_ms"]),
            "reply_count": cot_turn["reply_count"],
            "content_assertion_passed": bool(
                cot_turn["content_assertion_passed"]
            ),
            "fallback_detected": cot_turn["fallback_detected"],
            "duplicate_detected": cot_turn["duplicate_detected"],
            "out_of_order_detected": cot_turn["out_of_order_detected"],
        }
    )
    result["turns"] = projected_turns
    result["latency"] = deepcopy(projection["latency"])
    result["trace"] = deepcopy(projection["trace"])
    result["cleanup"] = deepcopy(projection["cleanup"])
    return result


def _project_diagnostic_cot_evidence(
    profile_result: Mapping[str, Any], receipt: Mapping[str, Any]
) -> dict[str, Any]:
    """Install the parent-owned P0-12 delivery projection deterministically."""

    result = deepcopy(dict(profile_result))
    scenarios = result.get("scenarios")
    reasoning = result.get("reasoning")
    if not isinstance(scenarios, list) or not isinstance(reasoning, dict):
        raise WorkerLaunchError("COT diagnostic projection is unavailable")
    scenario = next(
        (
            row
            for row in scenarios
            if isinstance(row, dict) and row.get("scenario_id") == "P0-12"
        ),
        None,
    )
    if not isinstance(scenario, dict):
        raise WorkerLaunchError("COT diagnostic projection is unavailable")

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
        status, failure_code = "BLOCKED_EVIDENCE", "PRECONDITION_MISSING"
    elif not configured_route_matches:
        status, failure_code = "PRODUCT_FAIL", "MODEL_ROUTE_MISMATCH"
    elif not configured_medium:
        status, failure_code = "PRODUCT_FAIL", "REASONING_EFFORT_CLAMPED"
    elif receipt_status == "PASS" and not token_present:
        status, failure_code = "BLOCKED_EVIDENCE", "REASONING_TOKENS_MISSING"
    elif receipt_status == "PASS":
        status, failure_code = "PASS", None
    elif receipt_status == "FAIL":
        status = "PRODUCT_FAIL"
        failure_code = {
            "FINAL_ANSWER_WRONG": "CONTENT_ASSERTION_FAILED",
            "DOWNSTREAM_PARSE_DROPPED_REASONING": "REASONING_METADATA_MISSING",
            "THINKING_ENVELOPE_NOT_DELIVERED": "DISCLOSURE_MISSING",
            "THINKING_ENVELOPE_UNREADABLE": "DISCLOSURE_MISSING",
            "THINKING_METADATA_INVALID": "REASONING_METADATA_MISSING",
        }.get(str(receipt_code))
    else:
        status = "BLOCKED_EVIDENCE"
        failure_code = {
            "CHAT_TIMEOUT": "CHAT_TIMEOUT",
            "CHAT_REQUEST_FAILED": "TRACE_UNAVAILABLE",
            "MODEL_REASONING_NOT_OBSERVED": "TRACE_INCOMPLETE",
            "TRACE_AMBIGUOUS": "TRACE_INCOMPLETE",
            "TRACE_UNAVAILABLE": "TRACE_UNAVAILABLE",
        }.get(str(receipt_code))
    if status != "PASS" and failure_code is None:
        raise WorkerLaunchError("COT diagnostic projection is unavailable")
    failure = (
        None
        if failure_code is None
        else _failure_projection(
            status=status,
            stage_code="REASONING",
            failure_code=failure_code,
        )
    )
    request_id = receipt.get("request_id")
    turn_id = receipt.get("turn_id")
    trace_id = receipt.get("trace_id")
    reasoning.update(
        {
            "request_id": request_id,
            "turn_id": turn_id,
            "trace_id": trace_id,
            "expected": True,
            "capability_enabled": capability_enabled,
            "requested_effort": receipt.get("requested_effort"),
            "configured_effort": receipt.get("configured_effort"),
            "effective_effort": receipt.get("effective_effort"),
            "reasoning_event_count": receipt.get("reasoning_event_count"),
            "metadata_present": receipt.get("metadata_present"),
            "token_metadata_present": token_present,
            "user_visible_disclosure_present": receipt.get(
                "user_visible_disclosure_present"
            ),
            "reasoning_token_count": receipt.get("reasoning_token_count"),
            "disclosure_length": receipt.get("delivered_thinking_len"),
            "kind": receipt.get("delivered_thinking_kind") or None,
            "source": receipt.get("delivered_thinking_source") or None,
            "model": receipt.get("delivered_thinking_model") or None,
            "raw_private_reasoning_stored": False,
        }
    )
    scenario.update(
        {
            "status": status,
            "attempts": 1,
            "attempt_results": [
                {"attempt": 1, "status": status, "failure": failure}
            ],
            "request_ids": [request_id] if request_id else [],
            "turn_ids": [turn_id] if turn_id else [],
            "trace_ids": [trace_id] if trace_id else [],
            "assertions": {
                "objective_answer_correct": receipt.get("final_answer_correct"),
                "reasoning_capability_enabled": capability_enabled,
                "reasoning_requested_effort_medium": requested_medium,
                "reasoning_configured_effort_medium": configured_medium,
                "reasoning_effective_effort_not_attested": effective_not_attested,
                "reasoning_event_observed": capability_enabled,
                "reasoning_metadata_present": receipt.get("metadata_present"),
                "reasoning_tokens_present": token_present,
                "user_disclosure_present": receipt.get(
                    "user_visible_disclosure_present"
                ),
                "raw_private_reasoning_omitted": True,
            },
            "failure": failure,
        }
    )
    # A non-passing trusted COT receipt must never leave an otherwise PASS
    # agent-authored profile looking greener than its authoritative evidence.
    # Diagnostic profiles normally remain BLOCKED_EVIDENCE until parent
    # cleanup, so preserve that existing aggregate status.
    if status != "PASS" and result.get("status") == "PASS":
        result["status"] = status
    return result


def _load_live_worker_evidence(
    spec: WorkerSpec, *, allow_failed_persona: bool = False
) -> tuple[dict[str, Any], str]:
    try:
        return validate_live_scenario_receipts(
            spec.live_receipt_path,
            run_id=str(spec.environment["QA_RUN_ID"]),
            profile_id=spec.profile_id,
            allow_failed_persona=allow_failed_persona,
        )
    except (LiveScenarioReceiptError, OSError):
        raise DiagnosticWorkerEvidenceError(
            "LIVE_RECEIPT_LOAD", "LIVE_RECEIPT_INVALID"
        ) from None


def _has_failed_persona_capture(aggregate: Mapping[str, Any]) -> bool:
    receipts = aggregate.get("receipts")
    if not isinstance(receipts, list):
        return False
    capture = [
        row
        for row in receipts
        if isinstance(row, Mapping) and row.get("scenario_id") == "P0-06"
    ]
    if len(capture) != 1:
        return False
    finalizer = aggregate.get("persona_finalizer")
    return bool(
        (capture[0].get("status") != "PASS" and finalizer is None)
        or (
            capture[0].get("status") == "PASS"
            and isinstance(finalizer, Mapping)
            and finalizer.get("kind") == "persona_finalizer_failure"
        )
    )


def _validate_live_worker_evidence(
    spec: WorkerSpec, profile_result: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    try:
        receipts, receipt_sha256 = _load_live_worker_evidence(spec)
        validate_live_result_binding(profile_result, receipts)
    except (LiveScenarioReceiptError, OSError, WorkerLaunchError):
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
    persona_finalize_runner: PersonaFinalizeRunner = _run_trusted_persona_finalize,
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
                persona_finalize_runner,
            )
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                failed = True
                exit_code = 125
        except Exception:
            failed = True
            exit_code = 125
        finally:
            try:
                _redact_persona_review_event_output(spec.events_path)
            except WorkerLaunchError:
                failed = True
                exit_code = 125
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
        failure_stage: str | None = None
        failure_code: str | None = None
        completed_command_count: int | None = None
        scenario_command_ids: tuple[str, ...] = ()
        scenario_command_counts: dict[str, int] = {}
        p0_06_command_phases: tuple[str, ...] = ()
        if diagnostic and attempt.invocation_failed:
            fallback_reason = _DIAGNOSTIC_FALLBACK_INVOCATION
            failure_stage = "INVOCATION"
            failure_code = "INVOCATION_FAILED"
        elif diagnostic and attempt.exit_code != 0:
            fallback_reason = _DIAGNOSTIC_FALLBACK_PROCESS
            failure_stage = "PROCESS_EXIT"
            failure_code = "PROCESS_EXIT_NONZERO"
        elif diagnostic:
            try:
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
                    live_receipts, live_receipt_sha256 = (
                        _load_live_worker_evidence(
                            spec, allow_failed_persona=True
                        )
                    )
                    failed_persona_capture = _has_failed_persona_capture(
                        live_receipts
                    )
                except WorkerScenarioToolUseError:
                    # Only a trusted non-PASS capture with no finalizer may
                    # relax P0-06 to its single successful CAPTURE command.
                    # Load that parent evidence after the strict tool gate has
                    # already preserved no-tool and unrelated tool failures.
                    live_receipts, live_receipt_sha256 = (
                        _load_live_worker_evidence(
                            spec, allow_failed_persona=True
                        )
                    )
                    failed_persona_capture = _has_failed_persona_capture(
                        live_receipts
                    )
                    if not failed_persona_capture:
                        raise
                    (
                        thread_id,
                        session_id,
                        profile_result,
                        completed_command_count,
                        scenario_command_ids,
                        scenario_command_counts,
                        p0_06_command_phases,
                    ) = _validated_worker_evidence(
                        spec,
                        identities,
                        allow_failed_persona_capture=True,
                    )
                try:
                    profile_result = _project_diagnostic_live_evidence(
                        profile_result, live_receipts
                    )
                except (WorkerLaunchError, DiagnosticAttemptError, OSError):
                    raise DiagnosticWorkerEvidenceError(
                        "LIVE_RECEIPT_PROJECTION",
                        "LIVE_RECEIPT_PROJECTION_INVALID",
                    ) from None
                try:
                    _validate_projected_result_shape(spec, profile_result)
                except WorkerLaunchError:
                    raise DiagnosticWorkerEvidenceError(
                        "LIVE_RECEIPT_SHAPE", "LIVE_RECEIPT_SHAPE_INVALID"
                    ) from None
                try:
                    _validate_diagnostic_live_projection(
                        profile_result,
                        live_receipts,
                        allow_failed_persona=failed_persona_capture,
                    )
                except (WorkerLaunchError, DiagnosticAttemptError, OSError):
                    raise DiagnosticWorkerEvidenceError(
                        "LIVE_RECEIPT_BINDING", "LIVE_RECEIPT_BINDING_INVALID"
                    ) from None
                try:
                    events_sha256 = file_sha256(
                        spec.events_path,
                        "Codex worker event stream",
                        max_bytes=_MAX_EVENTS_BYTES,
                    )
                except (OrchestrationError, OSError):
                    raise DiagnosticWorkerEvidenceError(
                        "EVENT_IDENTITY_PARSE", "EVENT_STREAM_DIGEST_INVALID"
                    ) from None
            except WorkerToolUseError:
                thread_id = None
                session_id = None
                events_sha256 = None
                completed_command_count = 0
                fallback_reason = _DIAGNOSTIC_FALLBACK_TOOL_USE
                failure_stage = "SCENARIO_COMMAND_EVIDENCE"
                failure_code = "AGENT_TOOL_USE_MISSING"
            except WorkerScenarioToolUseError as exc:
                thread_id = None
                session_id = None
                events_sha256 = None
                completed_command_count = exc.count
                scenario_command_ids = exc.scenario_ids
                scenario_command_counts = exc.scenario_counts
                p0_06_command_phases = exc.p0_06_phases
                fallback_reason = _DIAGNOSTIC_FALLBACK_SCENARIO_TOOL_USE
                failure_stage = "SCENARIO_COMMAND_EVIDENCE"
                failure_code = "AGENT_SCENARIO_TOOL_USE_MISSING"
            except DiagnosticWorkerEvidenceError as exc:
                thread_id = None
                session_id = None
                events_sha256 = None
                fallback_reason = _DIAGNOSTIC_FALLBACK_WORKER_EVIDENCE
                failure_stage = exc.failure_stage
                failure_code = exc.failure_code
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
                failure_stage = "WORKER_EVIDENCE"
                failure_code = "WORKER_EVIDENCE_INVALID"
            cot_path = spec.cot_receipt_path
            if not cot_path.exists():
                cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_MISSING
                if failure_stage is None:
                    failure_stage = "COT_RECEIPT_LOAD"
                    failure_code = "COT_RECEIPT_MISSING"
            else:
                try:
                    cot_receipt, cot_receipt_sha256 = validate_cot_receipt(
                        cot_path, spec.profile_id
                    )
                except (CotReceiptError, OSError):
                    cot_receipt = None
                    cot_receipt_sha256 = None
                    cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_INVALID
                    if failure_stage is None:
                        failure_stage = "COT_RECEIPT_LOAD"
                        failure_code = "COT_RECEIPT_INVALID"
                else:
                    if fallback_reason is None:
                        try:
                            projected = _project_diagnostic_cot_evidence(
                                profile_result, cot_receipt
                            )
                            _validate_projected_result_shape(spec, projected)
                            validate_live_attempts(projected)
                            _validate_cot_result_binding(
                                projected, cot_receipt
                            )
                            profile_result = projected
                        except (DiagnosticAttemptError, WorkerLaunchError):
                            # The deterministic receipt remains trustworthy even
                            # when the agent-authored projection disagrees with it.
                            cot_evidence_failure = (
                                _DIAGNOSTIC_COT_BINDING_MISMATCH
                            )
                            if failure_stage is None:
                                failure_stage = "COT_BINDING"
                                failure_code = _DIAGNOSTIC_COT_BINDING_MISMATCH
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
                if failure_stage is None:
                    failure_stage = "COT_RECEIPT_LOAD"
                    failure_code = "COT_RECEIPT_MISSING"
            else:
                try:
                    cot_receipt, cot_receipt_sha256 = validate_cot_receipt(
                        cot_path, spec.profile_id
                    )
                except (CotReceiptError, OSError):
                    cot_evidence_failure = _DIAGNOSTIC_FALLBACK_COT_INVALID
                    if failure_stage is None:
                        failure_stage = "COT_RECEIPT_LOAD"
                        failure_code = "COT_RECEIPT_INVALID"

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
            allowed_failure_codes = (
                DIAGNOSTIC_FAILURE_CODES_BY_STAGE.get(failure_stage)
                if failure_stage is not None
                else None
            )
            if (failure_stage is None) != (failure_code is None) or (
                failure_code is not None
                and (
                    allowed_failure_codes is None
                    or failure_code not in allowed_failure_codes
                )
            ):
                raise WorkerLaunchError(
                    "diagnostic worker failure evidence is invalid"
                )
            worker.update(
                {
                    "result_source": (
                        _DIAGNOSTIC_RESULT_SOURCE_FALLBACK
                        if fallback_reason is not None
                        else _DIAGNOSTIC_RESULT_SOURCE_CODEX
                    ),
                    "fallback_reason": fallback_reason,
                    "failure_stage": failure_stage,
                    "failure_code": failure_code,
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
