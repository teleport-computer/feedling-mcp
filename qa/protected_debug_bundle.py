#!/usr/bin/env python3
"""Build an encrypted, failure-only qualification debug bundle.

The repository is public, so exact synthetic account and correlation identifiers
must never be uploaded as ordinary Actions artifacts.  This utility validates the
canonical qualification result and its already-sanitized public failure index,
projects only bounded code/metadata evidence for failed scenarios, and encrypts
that projection to one or more team X25519 public keys.

Prompts, responses, chat/persona/file payloads, trace payloads, rationales,
credentials, and hidden chain-of-thought are neither accepted in the projection
nor written to a plaintext temporary file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from nacl import exceptions as nacl_exceptions
from nacl import utils as nacl_utils
from nacl.bindings import crypto_box_SEALBYTES
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa import atomic_private_file  # noqa: E402
from qa import build_team_report as team_report  # noqa: E402
from qa import persona_protected_debug as persona_debug  # noqa: E402
from qa import render_artifacts as result_renderer  # noqa: E402
from qa import validate_run as release_gate  # noqa: E402


IDENTITY_SCHEMA_VERSION = 1
ENVELOPE_SCHEMA_VERSION = 1
PAYLOAD_SCHEMA_VERSION = 2
IDENTITY_KIND = "io_e2e_debug_identity"
ENVELOPE_KIND = "io_e2e_protected_debug_bundle"
PAYLOAD_KIND = "io_e2e_protected_debug_payload"
CURVE = "X25519"
CIPHER = "XSalsa20-Poly1305-SecretBox"
KEY_WRAP = "X25519-SealedBox"

MAX_IDENTITY_BYTES = 16 * 1024
MAX_RESULT_BYTES = 20 * 1024 * 1024
MAX_FAILURE_INDEX_BYTES = 8 * 1024 * 1024
MAX_PERSONA_SUMMARY_BYTES = 2 * 1024 * 1024
MAX_PERSONA_RESULT_BYTES = 64 * 1024 * 1024
MAX_ENVELOPE_BYTES = 24 * 1024 * 1024
MAX_PAYLOAD_BYTES = 20 * 1024 * 1024
MAX_RECIPIENTS = 32
MAX_FAILURES = 256
MAX_TURNS_PER_FAILURE = 128

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FULL_DEPLOYMENT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")
_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_SAFE_ASSERTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_MODEL_RE = re.compile(
    r"^[^\u0000-\u001F\u007F-\u009F\u200B-\u200F\u2028-\u202E\u2060-\u206F\uFEFF|`]{1,160}$"
)

_IDENTITY_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "curve",
        "private_key_b64",
        "public_key_b64",
        "fingerprint",
    }
)
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "cipher",
        "key_wrap",
        "run_id_sha256",
        "payload_sha256",
        "recipients",
        "ciphertext_b64",
    }
)
_RECIPIENT_FIELDS = frozenset({"fingerprint", "wrapped_key_b64"})
_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id_sha256",
        "failure_index_sha256",
        "persona_summary_sha256",
        "suite_id",
        "failure_count",
        "api_key_failure_count",
        "persona_memory_failure_count",
        "failures",
        "persona_memory_failures",
    }
)
_FAILURE_FIELDS = frozenset(
    {
        "profile_id",
        "route_family",
        "model_family",
        "provider",
        "model",
        "user_id",
        "scenario_id",
        "status",
        "started_at",
        "finished_at",
        "failure",
        "attempts",
        "attempt_results",
        "assertions",
        "evidence_codes",
        "diagnostic_codes",
        "request_ids",
        "turn_ids",
        "trace_ids",
        "turns",
        "latency",
        "trace",
        "reasoning",
        "persona",
    }
)
_FIXED_FAILURE_FIELDS = frozenset(
    {"category", "stage_code", "failure_code", "reproducible"}
)
_ATTEMPT_FIELDS = frozenset({"attempt", "status", "failure"})
_TURN_FIELDS = frozenset(
    {
        "scenario_id",
        "turn_index",
        "request_id",
        "turn_id",
        "trace_id",
        "ack_latency_ms",
        "reply_latency_ms",
        "stage_latency_ms",
        "reply_count",
        "content_assertion_passed",
        "fallback_detected",
        "duplicate_detected",
        "out_of_order_detected",
    }
)
_LATENCY_FIELDS = frozenset(
    {
        "sample_count",
        "ack_p50_ms",
        "reply_p50_ms",
        "reply_p95_ms",
        "stage_p50_ms",
        "missing_stages",
    }
)
_TRACE_FIELDS = frozenset(
    {
        "enabled",
        "deploy_enabled",
        "correlated_event_count",
        "observed_event_types",
        "missing_required_event_types",
    }
)
_REASONING_FIELDS = frozenset(
    {
        "expected",
        "capability_enabled",
        "requested_effort",
        "configured_effort",
        "effective_effort",
        "reasoning_event_count",
        "metadata_present",
        "token_metadata_present",
        "user_visible_disclosure_present",
        "request_id",
        "turn_id",
        "trace_id",
        "kind",
        "source",
        "model",
        "reasoning_token_count",
        "disclosure_length",
    }
)
_PERSONA_FIELDS = frozenset(
    {
        "fixture_id",
        "evidence_sha256",
        "request_id",
        "job_id",
        "semantic_judgment_bound",
        "finalizer_ok",
        "evidence_deleted",
        "archive_upload_count",
        "archive_receipts_verified",
        "genesis_upload_metadata_verified",
        "privacy_violation_count",
    }
)
_TRACE_STAGES = frozenset(team_report.TRACE_STAGES)
_NONPASS_STATUSES = frozenset(
    {
        "PRODUCT_FAIL",
        "BLOCKED_CREDENTIAL",
        "BLOCKED_EVIDENCE",
        "BLOCKED_DEPLOYMENT",
        "AGENT_ERROR",
        "SECURITY_FAIL",
    }
)
_EFFORTS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "unsupported", "unknown"}
)
_FORBIDDEN_KEY_RE = re.compile(
    r"(?i)(?:^raw(?:_|$)|prompt|response|rationale|chain[_-]?of[_-]?thought|"
    r"hidden[_-]?reasoning|thinking_body|body_ct|api[_-]?key|admin[_-]?token|"
    r"provider[_-]?key|credential|private[_-]?key|secret|file[_-]?(?:body|content|payload)|"
    r"chat[_-]?(?:body|content|payload)|trace[_-]?(?:body|content|payload)|"
    r"persona[_-]?(?:body|content|payload))"
)


class ProtectedDebugError(RuntimeError):
    """A fixed diagnostic that is safe to print in CI."""


class _DuplicateJSONKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateJSONKey
        document[key] = value
    return document


def _reject_nonstandard_number(_value: str) -> None:
    raise ValueError


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ProtectedDebugError("protected debug data is invalid") from None


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonstandard_number,
        )
    except _DuplicateJSONKey:
        raise ProtectedDebugError(f"{label} contains duplicate keys") from None
    except (UnicodeError, ValueError, RecursionError):
        raise ProtectedDebugError(f"{label} is invalid") from None


def _read_regular(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> bytes:
    if not path.is_absolute():
        raise ProtectedDebugError(f"{label} is missing or unsafe")
    try:
        before = path.lstat()
    except OSError:
        raise ProtectedDebugError(f"{label} is missing or unsafe") from None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > max_bytes
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or (owner_only and stat.S_IMODE(before.st_mode) != 0o600)
    ):
        raise ProtectedDebugError(f"{label} is missing or unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProtectedDebugError(f"{label} is missing or unsafe") from None
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.geteuid()
            or (owner_only and stat.S_IMODE(opened.st_mode) != 0o600)
        ):
            raise ProtectedDebugError(f"{label} changed while reading")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ProtectedDebugError(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProtectedDebugError(f"{label} changed while reading")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError:
        raise ProtectedDebugError(f"{label} changed while reading") from None
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != len(raw)
        or after.st_nlink != 1
    ):
        raise ProtectedDebugError(f"{label} changed while reading")
    return raw


def _read_json(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    owner_only: bool = False,
) -> Any:
    return _decode_json(
        _read_regular(path, label, max_bytes=max_bytes, owner_only=owner_only),
        label,
    )


def _publish_private(path: Path, content: bytes, label: str) -> None:
    if not path.is_absolute():
        raise ProtectedDebugError(f"{label} path is unsafe")
    try:
        atomic_private_file.create_private_file(path, content)
    except atomic_private_file.AtomicPrivateFileError:
        raise ProtectedDebugError(f"{label} could not be published") from None


def _decode_b64(value: Any, label: str, *, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_ENVELOPE_BYTES * 2:
        raise ProtectedDebugError(f"{label} is invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError):
        raise ProtectedDebugError(f"{label} is invalid") from None
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise ProtectedDebugError(f"{label} is invalid")
    return decoded


def _encode_b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _require_exact_fields(
    value: Any, fields: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProtectedDebugError(f"{label} shape is invalid")
    return value


def _identity_document(private_key: PrivateKey) -> dict[str, Any]:
    private_bytes = bytes(private_key)
    public_bytes = bytes(private_key.public_key)
    return {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "kind": IDENTITY_KIND,
        "curve": CURVE,
        "private_key_b64": _encode_b64(private_bytes),
        "public_key_b64": _encode_b64(public_bytes),
        "fingerprint": _fingerprint(public_bytes),
    }


def _load_identity(path: Path) -> tuple[PrivateKey, bytes, str]:
    value = _require_exact_fields(
        _read_json(
            path,
            "debug identity",
            max_bytes=MAX_IDENTITY_BYTES,
            owner_only=True,
        ),
        _IDENTITY_FIELDS,
        "debug identity",
    )
    if (
        value.get("schema_version") != IDENTITY_SCHEMA_VERSION
        or value.get("kind") != IDENTITY_KIND
        or value.get("curve") != CURVE
    ):
        raise ProtectedDebugError("debug identity is invalid")
    private_bytes = _decode_b64(
        value.get("private_key_b64"), "debug identity", expected_bytes=PrivateKey.SIZE
    )
    public_bytes = _decode_b64(
        value.get("public_key_b64"), "debug identity", expected_bytes=PublicKey.SIZE
    )
    try:
        private_key = PrivateKey(private_bytes)
    except (TypeError, ValueError):
        raise ProtectedDebugError("debug identity is invalid") from None
    if bytes(private_key.public_key) != public_bytes or value.get(
        "fingerprint"
    ) != _fingerprint(public_bytes):
        raise ProtectedDebugError("debug identity is invalid")
    return private_key, public_bytes, _fingerprint(public_bytes)


def generate_key(identity_out: Path) -> dict[str, str]:
    """Create one owner-only X25519 identity and return only public material."""

    private_key = PrivateKey.generate()
    document = _identity_document(private_key)
    _publish_private(identity_out, _json_bytes(document), "debug identity")
    return {
        "public_key_b64": document["public_key_b64"],
        "fingerprint": document["fingerprint"],
    }


def _parse_recipients(csv_value: str) -> list[tuple[bytes, str]]:
    if not isinstance(csv_value, str):
        raise ProtectedDebugError("debug recipients are invalid")
    values = [value.strip() for value in csv_value.split(",")]
    if not values or len(values) > MAX_RECIPIENTS or any(not value for value in values):
        raise ProtectedDebugError("debug recipients are invalid")
    recipients: list[tuple[bytes, str]] = []
    seen: set[str] = set()
    for value in values:
        public_bytes = _decode_b64(
            value, "debug recipient", expected_bytes=PublicKey.SIZE
        )
        fingerprint = _fingerprint(public_bytes)
        if fingerprint in seen:
            raise ProtectedDebugError("debug recipients are invalid")
        seen.add(fingerprint)
        recipients.append((public_bytes, fingerprint))
    return recipients


def _coverage() -> dict[str, Any]:
    value = _read_json(
        (_REPO_ROOT / "qa" / "coverage-lock.json").absolute(),
        "coverage lock",
        max_bytes=2 * 1024 * 1024,
    )
    if not isinstance(value, dict):
        raise ProtectedDebugError("coverage lock is invalid")
    return value


def _validate_canonical_result(
    value: Any,
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    if not isinstance(value, dict):
        raise ProtectedDebugError("canonical result shape is invalid")
    schema = _read_json(
        (_REPO_ROOT / "qa" / "schemas" / "run-result.schema.json").absolute(),
        "result schema",
        max_bytes=2 * 1024 * 1024,
    )
    try:
        result_renderer._validate_schema(schema, value)
        profiles = result_renderer._ordered_profiles(value)
    except result_renderer.RenderInputError:
        raise ProtectedDebugError("canonical result shape is invalid") from None
    if (
        value.get("redaction", {}).get("synthetic_users_only") is not True
        or value.get("suite_id") != "io-e2e-agent-driven-test-p0"
        or value.get("profiles_expected") != len(result_renderer.PROFILE_IDS)
        or value.get("profiles_completed") != len(result_renderer.PROFILE_IDS)
    ):
        raise ProtectedDebugError("canonical result is not synthetic-only")
    coverage = _coverage()
    coverage_profiles = {
        row.get("id"): row
        for row in coverage.get("profiles", [])
        if isinstance(row, dict)
    }
    seen_users: set[str] = set()
    for profile in profiles:
        user_id = profile.get("user_id")
        profile_id = profile.get("profile_id")
        locked = coverage_profiles.get(profile_id)
        if (
            profile.get("redaction", {}).get("synthetic_users_only") is not True
            or not isinstance(user_id, str)
            or _SAFE_ID_RE.fullmatch(user_id) is None
            or user_id in seen_users
            or not isinstance(locked, dict)
            or not isinstance(profile.get("model"), str)
        ):
            raise ProtectedDebugError("canonical result is not synthetic-only")
        allowed_model_regex = locked.get("allowed_model_regex")
        if (
            not isinstance(allowed_model_regex, str)
            or re.fullmatch(allowed_model_regex, profile["model"]) is None
        ):
            raise ProtectedDebugError("canonical result model binding is invalid")
        seen_users.add(user_id)
    return value, profiles


def _validate_trusted_provisioning_binding(
    manifest: Any,
    result: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    *,
    expected_runtime: str,
    expected_deployment_sha: str,
) -> None:
    """Bind agent-authored evidence to controller-owned provisioning facts.

    This is intentionally not a signing scheme.  Provenance depends on the
    protected controller building the bundle from its owner-only manifest, and
    on reviewers downloading the encrypted bundle and scanned failure index
    directly from the same immutable GitHub Actions run.
    """

    if (
        expected_runtime
        not in {release_gate.BASELINE_RUNTIME, release_gate.EXPECTED_RUNTIME}
        or _FULL_DEPLOYMENT_SHA_RE.fullmatch(expected_deployment_sha) is None
    ):
        raise ProtectedDebugError("trusted deployment expectation is invalid")
    expected_sha = expected_deployment_sha.lower()
    target = result.get("target")
    if (
        not isinstance(target, dict)
        or target.get("expected_runtime") != expected_runtime
        or str(target.get("expected_deployment_sha") or "").lower() != expected_sha
        or str(target.get("observed_backend_sha") or "").lower() != expected_sha
    ):
        raise ProtectedDebugError("canonical result deployment binding is invalid")
    try:
        errors = release_gate._validate_provisioning_manifest(
            manifest, result, expected_runtime
        )
    except (KeyError, TypeError, ValueError):
        raise ProtectedDebugError("trusted provisioning binding is invalid") from None
    if errors or not isinstance(manifest, dict):
        raise ProtectedDebugError("trusted provisioning binding is invalid")

    manifest_profiles = manifest.get("profiles")
    if not isinstance(manifest_profiles, list):
        raise ProtectedDebugError("trusted provisioning binding is invalid")
    manifest_by_id = {
        row.get("profile_id"): row for row in manifest_profiles if isinstance(row, dict)
    }
    for profile in profiles:
        profile_id = profile["profile_id"]
        entry = manifest_by_id.get(profile_id)
        metadata = release_gate._PROFILE_METADATA.get(profile_id)
        if (
            not isinstance(entry, dict)
            or not isinstance(metadata, tuple)
            or len(metadata) != 3
            or profile.get("route_family") != metadata[0]
            or profile.get("model_family") != metadata[1]
            or profile.get("provider") != metadata[2]
            or profile.get("user_id") != entry.get("user_id")
            or profile.get("model") != entry.get("configured_model")
            or profile.get("expected_runtime") != expected_runtime
            or profile.get("observed_runtime") != entry.get("runtime_mode")
            or profile.get("observed_runtime_version") != entry.get("runtime_version")
        ):
            raise ProtectedDebugError("trusted provisioning binding is invalid")


def _cleanup_receipt_for_public_binding(
    failure_index: Mapping[str, Any], result: Mapping[str, Any], profile_count: int
) -> dict[str, Any]:
    cleanup_values = [
        row.get("cleanup")
        for row in failure_index.get("failures", [])
        if isinstance(row, dict)
        and row.get("scenario_id") == "P0-13"
        and row.get("cleanup") is not None
    ]
    if cleanup_values:
        first = cleanup_values[0]
        fields = frozenset(
            {
                "status",
                "generated_at",
                "attempted",
                "cleaned",
                "failed_profile_ids",
                "manifest_deleted",
                "manifest_retained_for_scan",
            }
        )
        value = _require_exact_fields(first, fields, "public failure index cleanup")
        if (
            value.get("status") != "PASS"
            or not isinstance(value.get("generated_at"), str)
            or type(value.get("attempted")) is not int
            or type(value.get("cleaned")) is not int
            or not isinstance(value.get("failed_profile_ids"), list)
            or any(not isinstance(item, str) for item in value["failed_profile_ids"])
            or type(value.get("manifest_deleted")) is not bool
            or type(value.get("manifest_retained_for_scan")) is not bool
            or any(item != first for item in cleanup_values)
        ):
            raise ProtectedDebugError("public failure index cleanup is invalid")
        return {
            "generated_at": value["generated_at"],
            "attempted": value["attempted"],
            "cleaned": value["cleaned"],
            "failed_profile_ids": list(value["failed_profile_ids"]),
            "manifest_deleted": value["manifest_deleted"],
            "manifest_retained_for_scan": value["manifest_retained_for_scan"],
        }
    return {
        "generated_at": result["finished_at"],
        "attempted": profile_count + 1,
        "cleaned": profile_count + 1,
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_retained_for_scan": False,
    }


def _validate_failure_index_binding(
    value: Any,
    result: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    persona_summary: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(value, dict):
        raise ProtectedDebugError("public failure index shape is invalid")
    cleanup_receipt = _cleanup_receipt_for_public_binding(value, result, len(profiles))
    try:
        _, expected = team_report._build_indexes(
            result,
            _coverage(),
            profiles,
            cleanup_receipt,
            "protected-debug-builder",
            persona_summary,
        )
    except (KeyError, TypeError, ValueError, team_report.TeamReportError):
        raise ProtectedDebugError("public failure index binding is invalid") from None
    if value != expected:
        raise ProtectedDebugError("public failure index binding is invalid")


def _validated_public_persona_summary(
    path: Path, result: Mapping[str, Any]
) -> Mapping[str, Any]:
    if path.name != "persona-memory-summary.json":
        raise ProtectedDebugError("public persona summary path is invalid")
    value = _read_json(
        path,
        "public persona summary",
        max_bytes=MAX_PERSONA_SUMMARY_BYTES,
        owner_only=True,
    )
    if not isinstance(value, dict):
        raise ProtectedDebugError("public persona summary is invalid")
    coverage = value.get("coverage")
    outcomes = value.get("pipeline_outcomes")
    repetitions = coverage.get("repetitions") if isinstance(coverage, dict) else None
    try:
        unavailable = team_report._persona_unavailable_summary(
            result, repetitions, outcomes
        )
    except (KeyError, TypeError, ValueError, team_report.TeamReportError):
        unavailable = None
    if value == unavailable:
        return value
    try:
        projected = team_report._persona_summary(
            path.parent, result, repetitions, outcomes
        )
    except (KeyError, TypeError, ValueError, team_report.TeamReportError):
        raise ProtectedDebugError("public persona summary is invalid") from None
    if value != projected:
        raise ProtectedDebugError("public persona summary is invalid")
    return projected


def _fixed_failure(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "category": value["category"],
        "stage_code": value["stage_code"],
        "failure_code": value["failure_code"],
        "reproducible": value["reproducible"],
    }


def _fixed_attempt(value: Mapping[str, Any]) -> dict[str, Any]:
    failure = value.get("failure")
    return {
        "attempt": value["attempt"],
        "status": value["status"],
        "failure": _fixed_failure(failure) if isinstance(failure, Mapping) else None,
    }


def _turn(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": value["scenario_id"],
        "turn_index": value["turn_index"],
        "request_id": value["request_id"],
        "turn_id": value["turn_id"],
        "trace_id": value["trace_id"],
        "ack_latency_ms": value["ack_latency_ms"],
        "reply_latency_ms": value["reply_latency_ms"],
        "stage_latency_ms": dict(value["stage_latency_ms"]),
        "reply_count": value["reply_count"],
        "content_assertion_passed": value["content_assertion_passed"],
        "fallback_detected": value["fallback_detected"],
        "duplicate_detected": value["duplicate_detected"],
        "out_of_order_detected": value["out_of_order_detected"],
    }


def _matching_turns(
    profile: Mapping[str, Any], scenario: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    raw_ids = {
        item
        for field in ("request_ids", "turn_ids", "trace_ids")
        for item in scenario[field]
    }
    return [
        turn
        for turn in profile["turns"]
        if turn["scenario_id"] == scenario["scenario_id"]
        or raw_ids.intersection(
            {turn.get("request_id"), turn.get("turn_id"), turn.get("trace_id")}
        )
    ]


def _reasoning(profile: Mapping[str, Any], scenario_id: str) -> dict[str, Any] | None:
    if scenario_id != "P0-12":
        return None
    source = profile["reasoning"]
    return {field: source[field] for field in _REASONING_FIELDS}


def _persona(scenario: Mapping[str, Any]) -> dict[str, Any] | None:
    if scenario["scenario_id"] != "P0-06":
        return None
    source = scenario.get("persona_finalizer")
    if not isinstance(source, Mapping):
        return None
    return {
        "fixture_id": source["fixture_id"],
        "evidence_sha256": source["evidence_sha256"],
        "request_id": source["request_id"],
        "job_id": source["job_id"],
        "semantic_judgment_bound": source["semantic_judgment_bound"],
        "finalizer_ok": source["finalizer_ok"],
        "evidence_deleted": source["private_evidence_deleted"],
        "archive_upload_count": source["archive_upload_count"],
        "archive_receipts_verified": source["archive_receipts_verified"],
        "genesis_upload_metadata_verified": source["genesis_upload_metadata_verified"],
        "privacy_violation_count": source["privacy_violation_count"],
    }


def _build_payload(
    result: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    *,
    failure_index_sha256: str,
    persona_summary_sha256: str,
    persona_memory_failures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for profile in profiles:
        for scenario in profile["scenarios"]:
            if scenario["status"] == "PASS":
                continue
            failure = scenario.get("failure")
            if not isinstance(failure, Mapping):
                raise ProtectedDebugError("canonical failure evidence is incomplete")
            trace = profile["trace"]
            failures.append(
                {
                    "profile_id": profile["profile_id"],
                    "route_family": profile["route_family"],
                    "model_family": profile["model_family"],
                    "provider": profile["provider"],
                    "model": profile["model"],
                    "user_id": profile["user_id"],
                    "scenario_id": scenario["scenario_id"],
                    "status": scenario["status"],
                    "started_at": scenario["started_at"],
                    "finished_at": scenario["finished_at"],
                    "failure": _fixed_failure(failure),
                    "attempts": scenario["attempts"],
                    "attempt_results": [
                        _fixed_attempt(item) for item in scenario["attempt_results"]
                    ],
                    "assertions": dict(scenario["assertions"]),
                    "evidence_codes": list(scenario["evidence_codes"]),
                    "diagnostic_codes": list(profile["diagnostic_codes"]),
                    "request_ids": list(scenario["request_ids"]),
                    "turn_ids": list(scenario["turn_ids"]),
                    "trace_ids": list(scenario["trace_ids"]),
                    "turns": [
                        _turn(item) for item in _matching_turns(profile, scenario)
                    ],
                    "latency": dict(profile["latency"]),
                    "trace": {field: trace[field] for field in _TRACE_FIELDS},
                    "reasoning": _reasoning(profile, scenario["scenario_id"]),
                    "persona": _persona(scenario),
                }
            )
    run_id_hash = hashlib.sha256(result["run_id"].encode("utf-8")).hexdigest()
    payload = {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "kind": PAYLOAD_KIND,
        "run_id_sha256": run_id_hash,
        "failure_index_sha256": failure_index_sha256,
        "persona_summary_sha256": persona_summary_sha256,
        "suite_id": result["suite_id"],
        "failure_count": len(failures) + len(persona_memory_failures),
        "api_key_failure_count": len(failures),
        "persona_memory_failure_count": len(persona_memory_failures),
        "failures": failures,
        "persona_memory_failures": [
            dict(failure) for failure in persona_memory_failures
        ],
    }
    _validate_payload(payload)
    return payload


def _safe_id(value: Any, *, nullable: bool = False) -> bool:
    return bool(
        (nullable and value is None)
        or (isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) is not None)
    )


def _nonnegative_number(value: Any, *, nullable: bool = True) -> bool:
    if value is None:
        return nullable
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _validate_code_list(value: Any, *, maximum: int) -> bool:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(
            not isinstance(item, str) or _SAFE_CODE_RE.fullmatch(item) is None
            for item in value
        )
    ):
        return False
    return len(value) == len(set(value))


def _validate_id_list(value: Any) -> bool:
    if (
        not isinstance(value, list)
        or len(value) > 64
        or any(not _safe_id(item) for item in value)
    ):
        return False
    return len(value) == len(set(value))


def _validate_failure(value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    value = _require_exact_fields(value, _FIXED_FAILURE_FIELDS, "failure evidence")
    if (
        value.get("category") not in _NONPASS_STATUSES
        or not isinstance(value.get("stage_code"), str)
        or _SAFE_CODE_RE.fullmatch(value["stage_code"]) is None
        or not isinstance(value.get("failure_code"), str)
        or _SAFE_CODE_RE.fullmatch(value["failure_code"]) is None
        or type(value.get("reproducible")) is not bool
    ):
        raise ProtectedDebugError("failure evidence is invalid")


def _validate_stage_latency(value: Any) -> None:
    value = _require_exact_fields(value, _TRACE_STAGES, "stage latency evidence")
    if any(not _nonnegative_number(item) for item in value.values()):
        raise ProtectedDebugError("stage latency evidence is invalid")


def _validate_turn(value: Any) -> None:
    value = _require_exact_fields(value, _TURN_FIELDS, "turn evidence")
    if (
        not isinstance(value.get("scenario_id"), str)
        or value["scenario_id"] not in result_renderer.SCENARIO_IDS
        or type(value.get("turn_index")) is not int
        or not 1 <= value["turn_index"] <= 32
        or not _safe_id(value.get("request_id"))
        or not _safe_id(value.get("turn_id"), nullable=True)
        or not _safe_id(value.get("trace_id"), nullable=True)
        or not _nonnegative_number(value.get("ack_latency_ms"))
        or not _nonnegative_number(value.get("reply_latency_ms"))
        or type(value.get("reply_count")) is not int
        or value["reply_count"] < 0
        or any(
            type(value.get(field)) is not bool
            for field in (
                "content_assertion_passed",
                "fallback_detected",
                "duplicate_detected",
                "out_of_order_detected",
            )
        )
    ):
        raise ProtectedDebugError("turn evidence is invalid")
    _validate_stage_latency(value["stage_latency_ms"])


def _validate_latency(value: Any) -> None:
    value = _require_exact_fields(value, _LATENCY_FIELDS, "latency evidence")
    if (
        type(value.get("sample_count")) is not int
        or value["sample_count"] < 0
        or any(
            not _nonnegative_number(value.get(field))
            for field in ("ack_p50_ms", "reply_p50_ms", "reply_p95_ms")
        )
        or not isinstance(value.get("missing_stages"), list)
        or any(
            not isinstance(item, str) or item not in _TRACE_STAGES
            for item in value.get("missing_stages", [])
        )
        or len(value["missing_stages"]) != len(set(value["missing_stages"]))
    ):
        raise ProtectedDebugError("latency evidence is invalid")
    _validate_stage_latency(value["stage_p50_ms"])


def _validate_trace(value: Any) -> None:
    value = _require_exact_fields(value, _TRACE_FIELDS, "trace metadata")
    if (
        type(value.get("enabled")) is not bool
        or type(value.get("deploy_enabled")) is not bool
        or type(value.get("correlated_event_count")) is not int
        or value["correlated_event_count"] < 0
        or any(
            not isinstance(value.get(field), list)
            or any(
                not isinstance(item, str) or item not in _TRACE_STAGES
                for item in value.get(field, [])
            )
            or len(value[field]) != len(set(value[field]))
            for field in ("observed_event_types", "missing_required_event_types")
        )
    ):
        raise ProtectedDebugError("trace metadata is invalid")


def _validate_reasoning(value: Any) -> None:
    if value is None:
        return
    value = _require_exact_fields(value, _REASONING_FIELDS, "reasoning metadata")
    if (
        any(
            type(value.get(field)) is not bool
            for field in (
                "expected",
                "capability_enabled",
                "metadata_present",
                "token_metadata_present",
                "user_visible_disclosure_present",
            )
        )
        or any(
            value.get(field) not in _EFFORTS
            for field in ("requested_effort", "configured_effort", "effective_effort")
        )
        or type(value.get("reasoning_event_count")) is not int
        or value["reasoning_event_count"] < 0
        or not _safe_id(value.get("request_id"))
        or not _safe_id(value.get("turn_id"))
        or not _safe_id(value.get("trace_id"))
        or not _safe_id(value.get("kind"), nullable=True)
        or not _safe_id(value.get("source"), nullable=True)
        or not (
            value.get("model") is None
            or (
                isinstance(value.get("model"), str)
                and _SAFE_MODEL_RE.fullmatch(value["model"]) is not None
            )
        )
        or not (
            value.get("reasoning_token_count") is None
            or (
                type(value.get("reasoning_token_count")) is int
                and value["reasoning_token_count"] >= 0
            )
        )
        or not (
            value.get("disclosure_length") is None
            or (
                type(value.get("disclosure_length")) is int
                and value["disclosure_length"] >= 0
            )
        )
    ):
        raise ProtectedDebugError("reasoning metadata is invalid")


def _validate_persona(value: Any) -> None:
    if value is None:
        return
    value = _require_exact_fields(value, _PERSONA_FIELDS, "persona metadata")
    if (
        value.get("fixture_id") != "persona-import-v1"
        or not isinstance(value.get("evidence_sha256"), str)
        or _SHA256_RE.fullmatch(value["evidence_sha256"]) is None
        or not _safe_id(value.get("request_id"))
        or not _safe_id(value.get("job_id"))
        or any(
            type(value.get(field)) is not bool
            for field in (
                "semantic_judgment_bound",
                "finalizer_ok",
                "evidence_deleted",
                "archive_receipts_verified",
                "genesis_upload_metadata_verified",
            )
        )
        or type(value.get("archive_upload_count")) is not int
        or value["archive_upload_count"] < 0
        or type(value.get("privacy_violation_count")) is not int
        or value["privacy_violation_count"] < 0
    ):
        raise ProtectedDebugError("persona metadata is invalid")


def _validate_payload_failure(value: Any) -> None:
    value = _require_exact_fields(value, _FAILURE_FIELDS, "protected failure")
    if (
        value.get("profile_id") not in result_renderer.PROFILE_IDS
        or value.get("scenario_id") not in result_renderer.SCENARIO_IDS
        or value.get("status") not in _NONPASS_STATUSES
        or not all(
            isinstance(value.get(field), str) and _SAFE_ID_RE.fullmatch(value[field])
            for field in ("route_family", "model_family", "provider")
        )
        or not isinstance(value.get("model"), str)
        or _SAFE_MODEL_RE.fullmatch(value["model"]) is None
        or not _safe_id(value.get("user_id"))
        or not isinstance(value.get("started_at"), str)
        or not isinstance(value.get("finished_at"), str)
        or type(value.get("attempts")) is not int
        or not 1 <= value["attempts"] <= 2
        or not isinstance(value.get("attempt_results"), list)
        or len(value["attempt_results"]) != value["attempts"]
        or not isinstance(value.get("assertions"), dict)
        or len(value["assertions"]) > 64
        or any(
            not isinstance(name, str)
            or _SAFE_ASSERTION_RE.fullmatch(name) is None
            or type(passed) is not bool
            for name, passed in value["assertions"].items()
        )
        or not _validate_code_list(value.get("evidence_codes"), maximum=32)
        or not _validate_code_list(value.get("diagnostic_codes"), maximum=32)
        or not _validate_id_list(value.get("request_ids"))
        or not _validate_id_list(value.get("turn_ids"))
        or not _validate_id_list(value.get("trace_ids"))
        or not isinstance(value.get("turns"), list)
        or len(value["turns"]) > MAX_TURNS_PER_FAILURE
    ):
        raise ProtectedDebugError("protected failure is invalid")
    contracts = _coverage().get("scenario_contracts", {})
    contract = (
        contracts.get(value["scenario_id"]) if isinstance(contracts, dict) else None
    )
    if not isinstance(contract, dict) or set(value["assertions"]) != set(
        contract.get("required_assertions", [])
    ):
        raise ProtectedDebugError("protected failure assertions are invalid")
    _validate_failure(value["failure"])
    for index, attempt in enumerate(value["attempt_results"], start=1):
        attempt = _require_exact_fields(attempt, _ATTEMPT_FIELDS, "attempt evidence")
        if attempt.get("attempt") != index or attempt.get(
            "status"
        ) not in _NONPASS_STATUSES | {"PASS"}:
            raise ProtectedDebugError("attempt evidence is invalid")
        _validate_failure(
            attempt.get("failure"), nullable=attempt.get("status") == "PASS"
        )
        if attempt.get("status") != "PASS" and attempt.get("failure") is None:
            raise ProtectedDebugError("attempt evidence is invalid")
    for turn in value["turns"]:
        _validate_turn(turn)
    _validate_latency(value["latency"])
    _validate_trace(value["trace"])
    _validate_reasoning(value["reasoning"])
    _validate_persona(value["persona"])
    if value["scenario_id"] == "P0-12" and value["reasoning"] is None:
        raise ProtectedDebugError("reasoning metadata is incomplete")
    if value["scenario_id"] != "P0-12" and value["reasoning"] is not None:
        raise ProtectedDebugError("reasoning metadata is out of scope")
    if value["scenario_id"] == "P0-06" and value["persona"] is None:
        raise ProtectedDebugError("persona metadata is incomplete")
    if value["scenario_id"] != "P0-06" and value["persona"] is not None:
        raise ProtectedDebugError("persona metadata is out of scope")


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        # Assertion names are themselves locked by coverage.  Several safe
        # boolean attestations deliberately contain words such as ``raw`` or
        # ``credential`` (for example ``raw_private_reasoning_omitted``).
        if (
            key == "assertions"
            or key == "api_key_failure_count"
            or key in persona_debug.PERSONA_TRAJECTORY_FIELDS
        ):
            continue
        if _FORBIDDEN_KEY_RE.search(key):
            raise ProtectedDebugError(
                "protected debug payload contains forbidden fields"
            )
        _reject_forbidden_keys(child)


def _validate_payload(value: Any) -> None:
    value = _require_exact_fields(value, _PAYLOAD_FIELDS, "protected debug payload")
    if (
        value.get("schema_version") != PAYLOAD_SCHEMA_VERSION
        or value.get("kind") != PAYLOAD_KIND
        or not isinstance(value.get("run_id_sha256"), str)
        or _SHA256_RE.fullmatch(value["run_id_sha256"]) is None
        or not isinstance(value.get("failure_index_sha256"), str)
        or _SHA256_RE.fullmatch(value["failure_index_sha256"]) is None
        or not isinstance(value.get("persona_summary_sha256"), str)
        or _SHA256_RE.fullmatch(value["persona_summary_sha256"]) is None
        or value.get("suite_id") != "io-e2e-agent-driven-test-p0"
        or type(value.get("failure_count")) is not int
        or type(value.get("api_key_failure_count")) is not int
        or type(value.get("persona_memory_failure_count")) is not int
        or not isinstance(value.get("failures"), list)
        or not isinstance(value.get("persona_memory_failures"), list)
        or value["api_key_failure_count"] != len(value["failures"])
        or value["persona_memory_failure_count"]
        != len(value["persona_memory_failures"])
        or value["failure_count"]
        != value["api_key_failure_count"] + value["persona_memory_failure_count"]
        or len(value["failures"]) > MAX_FAILURES
    ):
        raise ProtectedDebugError("protected debug payload is invalid")
    _reject_forbidden_keys(value)
    seen: set[tuple[str, str]] = set()
    for failure in value["failures"]:
        _validate_payload_failure(failure)
        identity = (failure["profile_id"], failure["scenario_id"])
        if identity in seen:
            raise ProtectedDebugError(
                "protected debug payload contains duplicate failures"
            )
        seen.add(identity)
    try:
        persona_debug.validate_persona_failures(value["persona_memory_failures"])
    except persona_debug.PersonaDebugError:
        raise ProtectedDebugError(
            "protected persona debug payload is invalid"
        ) from None


def _validate_envelope(value: Any) -> Mapping[str, Any]:
    value = _require_exact_fields(value, _ENVELOPE_FIELDS, "protected debug bundle")
    if (
        value.get("schema_version") != ENVELOPE_SCHEMA_VERSION
        or value.get("kind") != ENVELOPE_KIND
        or value.get("cipher") != CIPHER
        or value.get("key_wrap") != KEY_WRAP
        or not isinstance(value.get("run_id_sha256"), str)
        or _SHA256_RE.fullmatch(value["run_id_sha256"]) is None
        or not isinstance(value.get("payload_sha256"), str)
        or _SHA256_RE.fullmatch(value["payload_sha256"]) is None
        or not isinstance(value.get("recipients"), list)
        or not 1 <= len(value["recipients"]) <= MAX_RECIPIENTS
    ):
        raise ProtectedDebugError("protected debug bundle is invalid")
    seen: set[str] = set()
    for recipient in value["recipients"]:
        recipient = _require_exact_fields(
            recipient, _RECIPIENT_FIELDS, "protected debug recipient"
        )
        fingerprint = recipient.get("fingerprint")
        if (
            not isinstance(fingerprint, str)
            or _SHA256_RE.fullmatch(fingerprint) is None
            or fingerprint in seen
        ):
            raise ProtectedDebugError("protected debug recipient is invalid")
        _decode_b64(
            recipient.get("wrapped_key_b64"),
            "protected debug recipient",
            expected_bytes=SecretBox.KEY_SIZE + crypto_box_SEALBYTES,
        )
        seen.add(fingerprint)
    _decode_b64(value.get("ciphertext_b64"), "protected debug ciphertext")
    return value


def build_bundle(
    *,
    result_path: Path,
    failure_index_path: Path,
    persona_summary_path: Path | None = None,
    persona_result_path: Path | None = None,
    provisioning_manifest_path: Path,
    expected_runtime: str,
    expected_deployment_sha: str,
    recipients_csv: str,
    output_path: Path,
) -> None:
    """Validate, project, encrypt, and atomically publish a debug bundle."""

    result_value = _read_json(
        result_path, "canonical result", max_bytes=MAX_RESULT_BYTES
    )
    result, profiles = _validate_canonical_result(result_value)
    provisioning_manifest = _read_json(
        provisioning_manifest_path,
        "trusted provisioning manifest",
        max_bytes=MAX_RESULT_BYTES,
        owner_only=True,
    )
    _validate_trusted_provisioning_binding(
        provisioning_manifest,
        result,
        profiles,
        expected_runtime=expected_runtime,
        expected_deployment_sha=expected_deployment_sha,
    )
    failure_index_bytes = _read_regular(
        failure_index_path,
        "public failure index",
        max_bytes=MAX_FAILURE_INDEX_BYTES,
    )
    failure_index = _decode_json(failure_index_bytes, "public failure index")
    persona_summary = (
        _validated_public_persona_summary(persona_summary_path, result)
        if persona_summary_path is not None
        else None
    )
    _validate_failure_index_binding(failure_index, result, profiles, persona_summary)
    persona_summary_bytes = (
        _read_regular(
            persona_summary_path,
            "public persona summary",
            max_bytes=MAX_PERSONA_SUMMARY_BYTES,
            owner_only=True,
        )
        if persona_summary_path is not None
        else b"{}\n"
    )
    expected_persona_exact = sum(
        isinstance(row, dict)
        and row.get("source") == "persona_memory"
        and row.get("exact_id_debug_available") is True
        for row in failure_index.get("failures", [])
    )
    persona_memory_failures: list[dict[str, Any]] = []
    if persona_result_path is not None and expected_persona_exact > 0:
        if persona_summary is None:
            raise ProtectedDebugError("persona debug summary binding is missing")
        persona_result = _read_json(
            persona_result_path,
            "private persona result",
            max_bytes=MAX_PERSONA_RESULT_BYTES,
            owner_only=True,
        )
        try:
            persona_memory_failures = persona_debug.build_persona_failures(
                persona_result,
                persona_summary,
                canonical_run_id=result["run_id"],
                expected_runtime=expected_runtime,
                expected_deployment_sha=expected_deployment_sha.lower(),
            )
        except persona_debug.PersonaDebugError:
            raise ProtectedDebugError(
                "private persona debug binding is invalid"
            ) from None
    if len(persona_memory_failures) != expected_persona_exact:
        raise ProtectedDebugError("private persona debug coverage is incomplete")
    recipients = _parse_recipients(recipients_csv)
    payload = _build_payload(
        result,
        profiles,
        failure_index_sha256=hashlib.sha256(failure_index_bytes).hexdigest(),
        persona_summary_sha256=hashlib.sha256(persona_summary_bytes).hexdigest(),
        persona_memory_failures=persona_memory_failures,
    )
    if payload["failure_count"] != failure_index.get("exact_id_failure_count"):
        raise ProtectedDebugError("protected debug failure coverage is incomplete")
    payload_bytes = _json_bytes(payload)
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise ProtectedDebugError("protected debug payload exceeds the size limit")

    secret_key = bytearray(nacl_utils.random(SecretBox.KEY_SIZE))
    try:
        try:
            ciphertext = bytes(SecretBox(bytes(secret_key)).encrypt(payload_bytes))
            wrapped = []
            for public_bytes, fingerprint in recipients:
                wrapped_key = SealedBox(PublicKey(public_bytes)).encrypt(
                    bytes(secret_key)
                )
                wrapped.append(
                    {
                        "fingerprint": fingerprint,
                        "wrapped_key_b64": _encode_b64(wrapped_key),
                    }
                )
        except (nacl_exceptions.CryptoError, TypeError, ValueError):
            raise ProtectedDebugError("protected debug encryption failed") from None
    finally:
        for index in range(len(secret_key)):
            secret_key[index] = 0

    envelope = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "kind": ENVELOPE_KIND,
        "cipher": CIPHER,
        "key_wrap": KEY_WRAP,
        "run_id_sha256": hashlib.sha256(result["run_id"].encode("utf-8")).hexdigest(),
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "recipients": wrapped,
        "ciphertext_b64": _encode_b64(ciphertext),
    }
    _validate_envelope(envelope)
    _publish_private(output_path, _json_bytes(envelope), "protected debug bundle")


def decrypt_bundle(
    *,
    identity_path: Path,
    input_path: Path,
    failure_index_path: Path,
    persona_summary_path: Path | None = None,
    output_path: Path,
) -> None:
    """Decrypt a bundle for one recipient and publish owner-only plaintext."""

    private_key, _public_bytes, fingerprint = _load_identity(identity_path)
    envelope = _validate_envelope(
        _read_json(
            input_path,
            "protected debug bundle",
            max_bytes=MAX_ENVELOPE_BYTES,
            owner_only=False,
        )
    )
    recipient = next(
        (item for item in envelope["recipients"] if item["fingerprint"] == fingerprint),
        None,
    )
    if recipient is None:
        raise ProtectedDebugError("debug identity is not a bundle recipient")
    wrapped_key = _decode_b64(recipient["wrapped_key_b64"], "protected debug recipient")
    ciphertext = _decode_b64(envelope["ciphertext_b64"], "protected debug ciphertext")
    try:
        secret_key = bytearray(SealedBox(private_key).decrypt(wrapped_key))
        if len(secret_key) != SecretBox.KEY_SIZE:
            raise nacl_exceptions.CryptoError
        payload_bytes = SecretBox(bytes(secret_key)).decrypt(ciphertext)
    except (nacl_exceptions.CryptoError, ValueError, TypeError):
        raise ProtectedDebugError(
            "protected debug bundle authentication failed"
        ) from None
    finally:
        if "secret_key" in locals():
            for index in range(len(secret_key)):
                secret_key[index] = 0
    if (
        len(payload_bytes) <= 0
        or len(payload_bytes) > MAX_PAYLOAD_BYTES
        or hashlib.sha256(payload_bytes).hexdigest() != envelope["payload_sha256"]
    ):
        raise ProtectedDebugError("protected debug payload digest is invalid")
    payload = _decode_json(payload_bytes, "protected debug payload")
    _validate_payload(payload)
    if payload["run_id_sha256"] != envelope["run_id_sha256"]:
        raise ProtectedDebugError("protected debug run binding is invalid")
    failure_index_bytes = _read_regular(
        failure_index_path,
        "public failure index",
        max_bytes=MAX_FAILURE_INDEX_BYTES,
    )
    failure_index = _decode_json(failure_index_bytes, "public failure index")
    persona_summary_bytes = (
        _read_regular(
            persona_summary_path,
            "public persona summary",
            max_bytes=MAX_PERSONA_SUMMARY_BYTES,
        )
        if persona_summary_path is not None
        else b"{}\n"
    )
    persona_summary = _decode_json(persona_summary_bytes, "public persona summary")
    if (
        hashlib.sha256(failure_index_bytes).hexdigest()
        != payload["failure_index_sha256"]
        or not isinstance(failure_index, dict)
        or failure_index.get("kind") != "io_e2e_team_failure_index"
        or type(failure_index.get("failure_count")) is not int
        or type(failure_index.get("api_key_failure_count")) is not int
        or type(failure_index.get("persona_memory_failure_count")) is not int
        or type(failure_index.get("exact_id_failure_count")) is not int
        or failure_index.get("exact_id_failure_count") != payload["failure_count"]
        or failure_index.get("api_key_failure_count")
        != payload["api_key_failure_count"]
        or payload["persona_memory_failure_count"]
        > failure_index.get("persona_memory_failure_count")
        or not isinstance(failure_index.get("failures"), list)
        or sum(
            isinstance(row, dict) and row.get("source") == "api_key_matrix"
            for row in failure_index.get("failures", [])
        )
        != payload["api_key_failure_count"]
        or sum(
            isinstance(row, dict)
            and row.get("source") == "persona_memory"
            and row.get("exact_id_debug_available") is True
            for row in failure_index.get("failures", [])
        )
        != payload["persona_memory_failure_count"]
        or failure_index.get("failure_count")
        != failure_index.get("api_key_failure_count")
        + failure_index.get("persona_memory_failure_count")
        or not isinstance(failure_index.get("run_id"), str)
        or hashlib.sha256(failure_index["run_id"].encode("utf-8")).hexdigest()
        != payload["run_id_sha256"]
    ):
        raise ProtectedDebugError("public failure index digest is invalid")
    if (
        hashlib.sha256(persona_summary_bytes).hexdigest()
        != payload["persona_summary_sha256"]
        or not isinstance(persona_summary, dict)
        or (
            persona_summary_path is not None
            and persona_summary.get("kind")
            != "persona_memory_qualification_summary"
        )
        or (persona_summary_path is None and persona_summary != {})
    ):
        raise ProtectedDebugError("public persona summary digest is invalid")
    canonical_payload = _json_bytes(payload)
    if canonical_payload != payload_bytes:
        raise ProtectedDebugError("protected debug payload encoding is invalid")
    _publish_private(output_path, payload_bytes, "decrypted debug payload")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encrypt exact-ID synthetic qualification failure evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-key")
    generate.add_argument("--identity-out", required=True, type=Path)

    build = subparsers.add_parser("build")
    build.add_argument("--result", required=True, type=Path)
    build.add_argument("--failure-index", required=True, type=Path)
    build.add_argument("--persona-summary", required=True, type=Path)
    build.add_argument("--persona-result", type=Path)
    build.add_argument(
        "--manifest", required=True, type=Path, dest="provisioning_manifest"
    )
    build.add_argument(
        "--expected-runtime",
        required=True,
        choices=(release_gate.BASELINE_RUNTIME, release_gate.EXPECTED_RUNTIME),
    )
    build.add_argument("--expected-sha", required=True, dest="expected_deployment_sha")
    build.add_argument("--recipients", required=True)
    build.add_argument("--output", required=True, type=Path)

    decrypt = subparsers.add_parser("decrypt")
    decrypt.add_argument("--identity", required=True, type=Path)
    decrypt.add_argument("--input", required=True, type=Path)
    decrypt.add_argument("--failure-index", required=True, type=Path)
    decrypt.add_argument("--persona-summary", required=True, type=Path)
    decrypt.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate-key":
            public = generate_key(args.identity_out.absolute())
            print(_json_bytes(public).decode("utf-8"), end="")
        elif args.command == "build":
            build_bundle(
                result_path=args.result.absolute(),
                failure_index_path=args.failure_index.absolute(),
                persona_summary_path=args.persona_summary.absolute(),
                persona_result_path=(
                    args.persona_result.absolute()
                    if args.persona_result is not None
                    else None
                ),
                provisioning_manifest_path=args.provisioning_manifest.absolute(),
                expected_runtime=args.expected_runtime,
                expected_deployment_sha=args.expected_deployment_sha,
                recipients_csv=args.recipients,
                output_path=args.output.absolute(),
            )
        elif args.command == "decrypt":
            decrypt_bundle(
                identity_path=args.identity.absolute(),
                input_path=args.input.absolute(),
                failure_index_path=args.failure_index.absolute(),
                persona_summary_path=args.persona_summary.absolute(),
                output_path=args.output.absolute(),
            )
        else:  # pragma: no cover - argparse makes this unreachable
            raise ProtectedDebugError("protected debug command is invalid")
    except ProtectedDebugError as exc:
        print(f"protected-debug: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
