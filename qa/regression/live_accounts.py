"""Strict private contracts for live persona-memory account batches.

This module contains no network operations.  It converts the credential-bearing
pool manifest into in-memory sessions and verifies the content-free readiness
and cleanup receipts produced by trusted outer orchestration.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from qa.provision_profiles import _complete_pool_manifest
from qa.regression.contracts import canonical_json_sha256
from qa.regression.scenario_loader import LoaderError, loads_strict
from tools.provider_smoke.client import Session


LOCKED_BASE_URL = "https://test-api.feedling.app"
POOL_KIND = "persona_memory_account_pool"
READINESS_KIND = "persona_memory_account_readiness"
CLEANUP_KIND = "persona_memory_account_cleanup"
MAX_POOL_ACCOUNTS = 24
MAX_PRIVATE_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
READINESS_MAX_LIFETIME_SECONDS = 1800
ACCOUNT_RUN_LEASE_REMAINING_SECONDS = 9000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LEASE_ID_RE = re.compile(r"^lease_[0-9a-f]{32}$")
_ABSENCE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

_POOL_KEYS = {
    "schema_version",
    "manifest_kind",
    "generated_at",
    "base_url",
    "runtime_mode",
    "pool_profile_id",
    "pool_count",
    "synthetic_account_reaper",
    "profiles",
    "auxiliary_accounts",
}
_READINESS_KEYS = {
    "schema_version",
    "kind",
    "created_at",
    "expires_at",
    "base_url",
    "build_sha",
    "deployment_receipt_pre_sha256",
    "deployment_receipt_post_sha256",
    "import_started_at",
    "import_finished_at",
    "pool_manifest_sha256",
    "route_sha256",
    "persona_fixture_sha256",
    "source_bundle_sha256",
    "import_fixture_sha256",
    "account_count",
    "account_fingerprints",
    "accounts",
    "all_ready",
}
_READINESS_ACCOUNT_KEYS = {
    "account_fingerprint",
    "evidence_sha256",
    "fixture_sha256",
    "source_materials_verified",
    "surfaces_decryptable",
    "deterministic_acceptance",
    "post_import_chat_empty",
    "post_import_identity_present",
    "post_import_memory_present",
    "trace_cleared",
}
_READINESS_ACCOUNT_FLAGS = _READINESS_ACCOUNT_KEYS - {
    "account_fingerprint",
    "evidence_sha256",
    "fixture_sha256",
}
_CLEANUP_KEYS = {
    "schema_version",
    "kind",
    "created_at",
    "base_url",
    "pool_manifest_sha256",
    "route_sha256",
    "account_count",
    "account_fingerprints",
    "attempted",
    "cleaned",
    "failed_count",
    "complete",
    "manifest_deleted",
    "accounts",
}
_CLEANUP_ACCOUNT_KEYS = {
    "pool_index",
    "account_fingerprint",
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
_CLEANUP_ACCOUNT_BOOL_KEYS = {
    "attempted",
    "reset_response_accepted",
    "provider_config_preexisted",
    "provider_config_live_predelete_observed",
    "provider_config_deleted",
    "key_envelope_deleted",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
}
_CLEANUP_ACCOUNT_REQUIRED_TRUE = {
    "attempted",
    "reset_response_accepted",
    "provider_config_deleted",
    "key_envelope_deleted",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
}


class LiveAccountContractError(ValueError):
    """A pool or receipt is unsafe, malformed, stale, or mismatched."""


@dataclass(frozen=True, slots=True)
class AccountPool:
    path: Path
    manifest_sha256: str
    route_sha256: str
    account_fingerprints: tuple[str, ...]
    profile_id: str
    deployment_runtime: str
    rows: tuple[tuple[dict[str, Any], Session], ...]
    manifest: Mapping[str, Any]
    file_identity: tuple[int, int]


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise LiveAccountContractError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LiveAccountContractError(f"{label} timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise LiveAccountContractError(f"{label} timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise LiveAccountContractError(f"{label} digest is invalid")
    return value


def _fingerprints(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LiveAccountContractError(f"{label} account set is invalid")
    result = tuple(value)
    if (
        any(not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None for item in result)
        or len(result) != len(set(result))
        or result != tuple(sorted(result))
    ):
        raise LiveAccountContractError(f"{label} account set is invalid")
    return result


def _decode_key(value: Any, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise LiveAccountContractError(f"pool {label} is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        raise LiveAccountContractError(f"pool {label} is invalid") from None
    if len(decoded) != 32:
        raise LiveAccountContractError(f"pool {label} is invalid")
    return decoded


def _read_owner_file(
    path: Path, *, label: str, max_bytes: int
) -> tuple[bytes, Path, tuple[int, int]]:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise LiveAccountContractError(f"{label} path must be absolute")
    try:
        parent = candidate.parent.resolve(strict=True)
        parent_meta = parent.stat()
        before = candidate.lstat()
    except (OSError, RuntimeError):
        raise LiveAccountContractError(f"{label} is unavailable") from None
    if (
        parent != candidate.parent
        or not stat.S_ISDIR(parent_meta.st_mode)
        or parent_meta.st_uid != os.geteuid()
        or stat.S_IMODE(parent_meta.st_mode) != 0o700
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}
        or before.st_nlink != 1
        or not 1 <= before.st_size <= max_bytes
    ):
        raise LiveAccountContractError(f"{label} is not owner-controlled")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) not in {0o400, 0o600}
            or opened.st_nlink != 1
            or opened.st_size != before.st_size
        ):
            raise LiveAccountContractError(f"{label} changed while opening")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise LiveAccountContractError(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        ):
            raise LiveAccountContractError(f"{label} changed while reading")
        return b"".join(chunks), candidate, (opened.st_dev, opened.st_ino)
    except LiveAccountContractError:
        raise
    except OSError:
        raise LiveAccountContractError(f"{label} is unreadable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_private_json(
    path: Path, *, label: str = "private receipt", max_bytes: int = MAX_RECEIPT_BYTES
) -> tuple[dict[str, Any], str]:
    raw, _resolved, _identity = _read_owner_file(
        path, label=label, max_bytes=max_bytes
    )
    try:
        document = loads_strict(raw)
    except LoaderError:
        raise LiveAccountContractError(f"{label} JSON is invalid") from None
    if not isinstance(document, dict):
        raise LiveAccountContractError(f"{label} must be an object")
    try:
        digest = canonical_json_sha256(document)
    except ValueError:
        raise LiveAccountContractError(f"{label} JSON is invalid") from None
    return document, digest


def load_account_pool(path: Path, *, allow_expired_lease: bool = False) -> AccountPool:
    raw, resolved, file_identity = _read_owner_file(
        path, label="account pool manifest", max_bytes=MAX_PRIVATE_BYTES
    )
    try:
        document = loads_strict(raw)
    except LoaderError:
        raise LiveAccountContractError("account pool manifest JSON is invalid") from None
    if not isinstance(document, dict) or set(document) != _POOL_KEYS:
        raise LiveAccountContractError("account pool manifest contract is invalid")
    profile_id = document.get("pool_profile_id")
    count = document.get("pool_count")
    profiles = document.get("profiles")
    reaper = document.get("synthetic_account_reaper")
    if (
        document.get("schema_version") != 1
        or document.get("manifest_kind") != POOL_KIND
        or document.get("base_url") != LOCKED_BASE_URL
        or document.get("runtime_mode") not in {"deployed_current", "hosted_resident"}
        or not isinstance(profile_id, str)
        or _PROFILE_ID_RE.fullmatch(profile_id) is None
        or type(count) is not int
        or not 1 <= count <= MAX_POOL_ACCOUNTS
        or not isinstance(profiles, list)
        or len(profiles) != count
        or document.get("auxiliary_accounts") != []
        or not isinstance(reaper, dict)
        or set(reaper)
        != {"enabled", "ready", "heartbeat_fresh", "label_prefix", "max_ttl_seconds"}
        or reaper.get("enabled") is not True
        or reaper.get("ready") is not True
        or reaper.get("heartbeat_fresh") is not True
        or reaper.get("label_prefix") != "agent-e2e-"
        or type(reaper.get("max_ttl_seconds")) is not int
        or not 1 <= reaper["max_ttl_seconds"] <= 14_400
        or not _complete_pool_manifest(document)
    ):
        raise LiveAccountContractError("account pool manifest contract is invalid")
    _parse_time(document.get("generated_at"), "pool generated_at")

    rows: list[tuple[dict[str, Any], Session]] = []
    user_ids: set[str] = set()
    api_keys: set[str] = set()
    lease_ids: set[str] = set()
    routes: set[str] = set()
    for index, value in enumerate(profiles, start=1):
        if not isinstance(value, dict):
            raise LiveAccountContractError("account pool profile is invalid")
        profile = dict(value)
        lease = profile.get("synthetic_account_lease")
        user_id = profile.get("user_id")
        api_key = profile.get("api_key")
        runtime_version = profile.get("runtime_version")
        runtime_receipt = profile.get("runtime_readback_receipt")
        if (
            profile.get("profile_id") != profile_id
            or profile.get("pool_index") != index
            or profile.get("provision_status") != "ready"
            or profile.get("provision_failure_code") != "NONE"
            or profile.get("registration_verified") is not True
            or profile.get("fresh_state_verified") is not True
            or profile.get("invalid_key_rejected") is not True
            or profile.get("valid_key_configured") is not True
            or profile.get("trace_enabled") is not True
            or profile.get("runtime_mode_readback_verified") is not True
            or profile.get("runtime_mode_set_required") is not False
            or profile.get("runtime_mode_set_verified") is not False
            or not isinstance(profile.get("runtime_mode"), str)
            or not profile["runtime_mode"]
            or type(runtime_version) is not int
            or runtime_version < 1
            or not all(
                isinstance(profile.get(field), str) and bool(profile[field])
                for field in (
                    "provider",
                    "route_family",
                    "configured_model",
                    "configured_base_url",
                    "reasoning_effort",
                    "label",
                )
            )
            or not isinstance(user_id, str)
            or not user_id
            or not isinstance(api_key, str)
            or not api_key
            or not isinstance(lease, dict)
            or lease.get("registered") is not True
            or not isinstance(lease.get("lease_id"), str)
            or _LEASE_ID_RE.fullmatch(lease["lease_id"]) is None
            or not isinstance(lease.get("absence_token"), str)
            or _ABSENCE_TOKEN_RE.fullmatch(lease["absence_token"]) is None
            or lease.get("ttl_seconds") != reaper["max_ttl_seconds"]
            or type(lease.get("expires_at_epoch")) is not int
            or not isinstance(runtime_receipt, dict)
            or runtime_receipt
            != {
                "configured": True,
                "runtime_mode": profile.get("runtime_mode"),
                "runtime_version": runtime_version,
            }
        ):
            raise LiveAccountContractError("account pool profile is not ready")
        expires_at = _parse_time(lease.get("expires_at"), "pool lease expires_at")
        if abs(expires_at.timestamp() - lease["expires_at_epoch"]) > 1:
            raise LiveAccountContractError("account pool lease timestamps disagree")
        if not allow_expired_lease and expires_at <= datetime.now(timezone.utc):
            raise LiveAccountContractError("account pool profile lease is expired")
        if document["runtime_mode"] == "hosted_resident" and (
            profile["runtime_mode"] != "hosted_resident"
            or runtime_version != 2
        ):
            raise LiveAccountContractError("account pool Runtime V2 proof is invalid")
        if user_id in user_ids or api_key in api_keys or lease["lease_id"] in lease_ids:
            raise LiveAccountContractError("account pool identities are not unique")
        user_ids.add(user_id)
        api_keys.add(api_key)
        lease_ids.add(lease["lease_id"])
        route = {
            "profile_id": profile_id,
            "provider": profile["provider"],
            "route_family": profile["route_family"],
            "configured_model": profile["configured_model"],
            "configured_base_url": profile["configured_base_url"],
            "runtime_mode": profile["runtime_mode"],
            "runtime_version": runtime_version,
            "reasoning_effort": profile["reasoning_effort"],
            "trace_enabled": True,
        }
        routes.add(canonical_json_sha256(route))
        rows.append(
            (
                profile,
                Session(
                    user_id=user_id,
                    api_key=api_key,
                    sk=_decode_key(profile.get("secret_key_b64"), "secret key"),
                    pk=_decode_key(profile.get("public_key_b64"), "public key"),
                ),
            )
        )
    if len(routes) != 1:
        raise LiveAccountContractError("account pool routes are inconsistent")
    account_fingerprints = tuple(
        sorted(hashlib.sha256(value.encode("utf-8")).hexdigest() for value in user_ids)
    )
    return AccountPool(
        path=resolved,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        route_sha256=next(iter(routes)),
        account_fingerprints=account_fingerprints,
        profile_id=profile_id,
        deployment_runtime=document["runtime_mode"],
        rows=tuple(rows),
        manifest=document,
        file_identity=file_identity,
    )


def verify_readiness_receipt(
    path: Path,
    *,
    expected_build_sha: str,
    expected_persona_fixture_sha256: str,
    expected_source_bundle_sha256: str,
    expected_import_fixture_sha256: str | None = None,
    pool: AccountPool | None = None,
    expected_account_fingerprints: Sequence[str] | None = None,
    expected_pool_manifest_sha256: str | None = None,
    expected_route_sha256: str | None = None,
    at_time: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    document, digest = read_private_json(path, label="account readiness receipt")
    if set(document) != _READINESS_KEYS:
        raise LiveAccountContractError("account readiness receipt contract is invalid")
    created = _parse_time(document.get("created_at"), "readiness created_at")
    expires = _parse_time(document.get("expires_at"), "readiness expires_at")
    import_started = _parse_time(
        document.get("import_started_at"), "readiness import_started_at"
    )
    import_finished = _parse_time(
        document.get("import_finished_at"), "readiness import_finished_at"
    )
    reference = (at_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lifetime = (expires - created).total_seconds()
    if (
        not 0 < lifetime <= READINESS_MAX_LIFETIME_SECONDS
        or not import_started <= import_finished <= created <= reference <= expires
    ):
        raise LiveAccountContractError("account readiness receipt is stale")
    if pool is not None:
        expected_account_fingerprints = pool.account_fingerprints
        expected_pool_manifest_sha256 = pool.manifest_sha256
        expected_route_sha256 = pool.route_sha256
    expected_accounts = tuple(sorted(expected_account_fingerprints or ()))
    observed_accounts = _fingerprints(
        document.get("account_fingerprints"), "readiness"
    )
    if (
        document.get("schema_version") != 1
        or document.get("kind") != READINESS_KIND
        or document.get("base_url") != LOCKED_BASE_URL
        or not isinstance(expected_build_sha, str)
        or _BUILD_SHA_RE.fullmatch(expected_build_sha) is None
        or document.get("build_sha") != expected_build_sha
        or document.get("persona_fixture_sha256")
        != _sha(expected_persona_fixture_sha256, "expected persona fixture")
        or document.get("source_bundle_sha256")
        != _sha(expected_source_bundle_sha256, "expected source bundle")
        or (
            expected_import_fixture_sha256 is not None
            and document.get("import_fixture_sha256")
            != _sha(expected_import_fixture_sha256, "expected import fixture")
        )
        or document.get("pool_manifest_sha256")
        != _sha(expected_pool_manifest_sha256, "expected pool manifest")
        or document.get("route_sha256")
        != _sha(expected_route_sha256, "expected route")
        or _SHA256_RE.fullmatch(
            str(document.get("deployment_receipt_pre_sha256") or "")
        )
        is None
        or _SHA256_RE.fullmatch(
            str(document.get("deployment_receipt_post_sha256") or "")
        )
        is None
        or document.get("deployment_receipt_pre_sha256")
        == document.get("deployment_receipt_post_sha256")
        or _SHA256_RE.fullmatch(str(document.get("import_fixture_sha256") or ""))
        is None
        or type(document.get("account_count")) is not int
        or document["account_count"] != len(expected_accounts)
        or observed_accounts != expected_accounts
        or document.get("all_ready") is not True
    ):
        raise LiveAccountContractError("account readiness receipt does not match the run")
    raw_accounts = document.get("accounts")
    if not isinstance(raw_accounts, list) or len(raw_accounts) != len(expected_accounts):
        raise LiveAccountContractError("account readiness evidence is incomplete")
    observed_rows: list[str] = []
    import_fixture_hashes: set[str] = set()
    evidence_hashes: set[str] = set()
    for row in raw_accounts:
        if (
            not isinstance(row, dict)
            or set(row) != _READINESS_ACCOUNT_KEYS
            or any(row.get(flag) is not True for flag in _READINESS_ACCOUNT_FLAGS)
        ):
            raise LiveAccountContractError("account readiness evidence is invalid")
        observed_rows.append(_sha(row.get("account_fingerprint"), "account fingerprint"))
        evidence_hashes.add(_sha(row.get("evidence_sha256"), "account evidence"))
        import_fixture_hashes.add(
            _sha(row.get("fixture_sha256"), "account import fixture")
        )
    if (
        tuple(observed_rows) != expected_accounts
        or len(import_fixture_hashes) != 1
        or next(iter(import_fixture_hashes), None)
        != document.get("import_fixture_sha256")
        or len(evidence_hashes) != len(expected_accounts)
    ):
        raise LiveAccountContractError("account readiness evidence is not canonical")
    if pool is not None:
        minimum_lease_expiry = reference + timedelta(
            seconds=ACCOUNT_RUN_LEASE_REMAINING_SECONDS
        )
        for profile, _session in pool.rows:
            lease = profile["synthetic_account_lease"]
            lease_expiry = datetime.fromtimestamp(
                lease["expires_at_epoch"], tz=timezone.utc
            )
            if lease_expiry < minimum_lease_expiry:
                raise LiveAccountContractError(
                    "account leases have less than the required run lifetime remaining"
                )
    return document, digest


def verify_cleanup_receipt(
    path: Path,
    *,
    expected_pool_manifest_sha256: str,
    expected_route_sha256: str,
    expected_account_fingerprints: Sequence[str],
    require_complete: bool = True,
) -> tuple[dict[str, Any], str]:
    document, digest = read_private_json(path, label="account cleanup receipt")
    if set(document) != _CLEANUP_KEYS:
        raise LiveAccountContractError("account cleanup receipt contract is invalid")
    created = _parse_time(document.get("created_at"), "cleanup created_at")
    if created > datetime.now(timezone.utc):
        raise LiveAccountContractError("account cleanup receipt is from the future")
    expected_accounts = tuple(sorted(expected_account_fingerprints))
    observed_accounts = _fingerprints(document.get("account_fingerprints"), "cleanup")
    count = document.get("account_count")
    attempted = document.get("attempted")
    cleaned = document.get("cleaned")
    failed = document.get("failed_count")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != CLEANUP_KIND
        or document.get("base_url") != LOCKED_BASE_URL
        or document.get("pool_manifest_sha256")
        != _sha(expected_pool_manifest_sha256, "expected pool manifest")
        or document.get("route_sha256")
        != _sha(expected_route_sha256, "expected route")
        or observed_accounts != expected_accounts
        or any(type(value) is not int for value in (count, attempted, cleaned, failed))
        or count != len(expected_accounts)
        or not 0 <= attempted <= count
        or not 0 <= cleaned <= attempted
        or not 0 <= failed <= attempted
        or type(document.get("complete")) is not bool
        or type(document.get("manifest_deleted")) is not bool
    ):
        raise LiveAccountContractError("account cleanup receipt does not match the pool")
    raw_rows = document.get("accounts")
    if not isinstance(raw_rows, list) or len(raw_rows) != count:
        raise LiveAccountContractError("account cleanup evidence is incomplete")
    row_fingerprints: list[str] = []
    all_rows_pass = True
    attempted_rows = 0
    passed_rows = 0
    for expected_index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict) or set(raw) != _CLEANUP_ACCOUNT_KEYS:
            raise LiveAccountContractError("account cleanup evidence is invalid")
        fingerprint = raw.get("account_fingerprint")
        if (
            raw.get("pool_index") != expected_index
            or not isinstance(fingerprint, str)
            or _SHA256_RE.fullmatch(fingerprint) is None
            or not isinstance(raw.get("profile_id"), str)
            or _PROFILE_ID_RE.fullmatch(raw["profile_id"]) is None
            or any(type(raw.get(key)) is not bool for key in _CLEANUP_ACCOUNT_BOOL_KEYS)
            or raw.get("provider_config_deletion_source")
            not in {"explicit_api", "account_cascade"}
            or raw.get("status") not in {"PASS", "FAIL"}
        ):
            raise LiveAccountContractError("account cleanup evidence is invalid")
        row_pass = raw.get("status") == "PASS" and all(
            raw.get(key) is True for key in _CLEANUP_ACCOUNT_REQUIRED_TRUE
        )
        all_rows_pass = all_rows_pass and row_pass
        attempted_rows += int(raw.get("attempted") is True)
        passed_rows += int(row_pass)
        row_fingerprints.append(fingerprint)
    if tuple(sorted(row_fingerprints)) != expected_accounts:
        raise LiveAccountContractError("account cleanup evidence is mismatched")
    if (
        document["complete"] is not all_rows_pass
        or attempted != attempted_rows
        or cleaned != passed_rows
        or failed != attempted_rows - passed_rows
        or (document["manifest_deleted"] is True and not all_rows_pass)
    ):
        raise LiveAccountContractError("account cleanup aggregate is inconsistent")
    if require_complete and not (
        document["complete"] is True
        and document["manifest_deleted"] is True
        and attempted == count
        and cleaned == count
        and failed == 0
    ):
        raise LiveAccountContractError("account cleanup is incomplete")
    return document, digest


__all__ = [
    "AccountPool",
    "CLEANUP_KIND",
    "LiveAccountContractError",
    "POOL_KIND",
    "READINESS_KIND",
    "load_account_pool",
    "read_private_json",
    "verify_cleanup_receipt",
    "verify_readiness_receipt",
]
