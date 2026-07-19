#!/usr/bin/env python3
"""Fail-closed scan of the team-safe qualification report boundary.

The scanner receives real credential values only in its own CI step. It never
prints a value or an artifact-controlled path; findings are fixed categories
that are safe for GitHub Actions logs.  The validated canonical result remains
outside this boundary and supplies the synthetic identifiers that must not be
published.  Team artifacts may expose only independently derived handles.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Mapping, Sequence


SECRET_ENV_NAMES = (
    "QA_CODEX_AUTH_JSON_B64",
    "IO_E2E_ADMIN_TOKEN",
    "QA_DEEPSEEK_API_KEY",
    "QA_ANTHROPIC_API_KEY",
    "QA_OPENAI_PROVIDER_API_KEY",
    "QA_OPENROUTER_API_KEY",
    "QA_GEMINI_API_KEY",
    "QA_KONGBEIQIE_API_KEY",
)
PROFILE_IDS = (
    "official-deepseek",
    "official-anthropic",
    "official-openai",
    "official-gemini",
    "openrouter-claude",
    "openrouter-openai",
    "openrouter-glm",
    "openrouter-kimi",
    "relay-kongbeiqie",
)
MEMORY_CONTRACT_PROFILE_ID = "memory-contract"
EXPECTED_PUBLIC_FILES = {
    "run-index.json",
    "failure-index.json",
    "team-summary.md",
    "cleanup-receipt.json",
    "memory-contract.json",
    "persona-memory-summary.json",
    "persona-memory-matrix.md",
    "matrix.md",
    "latency.csv",
    "junit.xml",
}
MAX_FILES = 512
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CANONICAL_RESULT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MIN_RECONSTRUCTED_FRAGMENT_BYTES = 8
_FORBIDDEN_JSON_KEY = re.compile(
    rb'(?i)"(?:api_key|secret_key_b64|private_key(?:_b64)?|provider_key|admin_token|'
    rb"body_ct|thinking_body_ct|K_user|"
    rb"prompt|response|rationale|evidence_turn_ids|account_fingerprints|user_id|"
    rb'turn_id|session_id|request_id|response_id|trace_id|job_id)"\s*:'
)
_SAFE_RAW_ATTESTATIONS = {
    "raw_chat_omitted": True,
    "raw_correlation_identifiers_omitted": True,
    "raw_memory_ids_persisted": False,
    "raw_persona_omitted": True,
    "raw_private_reasoning_stored": False,
    "raw_reasoning_omitted": True,
    "raw_responses_persisted": False,
    "raw_trace_omitted": True,
    "raw_trace_stored": False,
}
_CREDENTIAL_SIGNATURE = re.compile(rb"(?:sk-ant-|sk-or-v1-|sk-proj-)[A-Za-z0-9_-]{8,}")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{7,159}$")
_CANONICAL_IDENTIFIER_FIELDS = frozenset(
    {"user_id", "request_id", "turn_id", "trace_id", "job_id", "session_id"}
)
_CANONICAL_IDENTIFIER_LIST_FIELDS = frozenset({"request_ids", "turn_ids", "trace_ids"})


class ArtifactScanError(RuntimeError):
    """A fixed diagnostic safe to print without exposing artifact content."""


class _DuplicateTeamJSONKey(ValueError):
    pass


def _team_object_without_duplicate_keys(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateTeamJSONKey
        result[key] = value
    return result


def _team_json_private_field_issue(data: bytes) -> bool:
    try:
        document = json.loads(
            data.decode("utf-8"), object_pairs_hook=_team_object_without_duplicate_keys
        )
    except (_DuplicateTeamJSONKey, UnicodeError, json.JSONDecodeError, RecursionError):
        return True

    def visit(value: object) -> bool:
        if isinstance(value, list):
            return any(visit(item) for item in value)
        if not isinstance(value, dict):
            return False
        for key, child in value.items():
            if key.lower().startswith("raw_") and (
                key not in _SAFE_RAW_ATTESTATIONS
                or child is not _SAFE_RAW_ATTESTATIONS[key]
            ):
                return True
            if visit(child):
                return True
        return False

    return visit(document)


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactScanError("canonical result contains duplicate keys")
        result[key] = value
    return result


def _read_canonical_result(path: Path) -> tuple[dict, Path]:
    """Read the validated staging result without following or trusting links."""

    if not path.is_absolute() or path.is_symlink() or path.name != "run-result.json":
        raise ArtifactScanError("canonical result is missing or unreadable")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ArtifactScanError("canonical result is missing or unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_CANONICAL_RESULT_BYTES
        ):
            raise ArtifactScanError("canonical result is missing or unreadable")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ArtifactScanError("canonical result changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArtifactScanError("canonical result changed while reading")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
    except ArtifactScanError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ArtifactScanError("canonical result is missing or invalid") from None
    if not isinstance(document, dict):
        raise ArtifactScanError("canonical result shape is invalid")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ArtifactScanError("canonical result is missing or unreadable") from None
    return document, resolved


def _canonical_identifier_values(document: dict) -> set[bytes]:
    """Extract every bounded synthetic correlation identifier from the result."""

    profiles = document.get("profiles")
    redaction = document.get("redaction")
    if (
        not isinstance(profiles, list)
        or len(profiles) != len(PROFILE_IDS)
        or not isinstance(redaction, dict)
        or redaction.get("synthetic_users_only") is not True
    ):
        raise ArtifactScanError("canonical result shape is invalid")
    profile_ids: list[str] = []
    for profile in profiles:
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("scenarios"), list)
            or not isinstance(profile.get("turns"), list)
            or not isinstance(profile.get("reasoning"), dict)
            or not isinstance(profile.get("redaction"), dict)
            or profile["redaction"].get("synthetic_users_only") is not True
        ):
            raise ArtifactScanError("canonical result shape is invalid")
        profile_id = profile.get("profile_id")
        if not isinstance(profile_id, str):
            raise ArtifactScanError("canonical result shape is invalid")
        profile_ids.append(profile_id)
        if any(not isinstance(row, dict) for row in profile["scenarios"]):
            raise ArtifactScanError("canonical result shape is invalid")
        if any(not isinstance(row, dict) for row in profile["turns"]):
            raise ArtifactScanError("canonical result shape is invalid")
    if tuple(profile_ids) != PROFILE_IDS:
        raise ArtifactScanError("canonical result profile set is invalid")

    raw_values: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key in _CANONICAL_IDENTIFIER_FIELDS:
                if child is None:
                    continue
                if (
                    not isinstance(child, str)
                    or _SAFE_IDENTIFIER_RE.fullmatch(child) is None
                ):
                    raise ArtifactScanError(
                        "canonical result identifier shape is invalid"
                    )
                raw_values.add(child)
            elif key in _CANONICAL_IDENTIFIER_LIST_FIELDS:
                if not isinstance(child, list) or any(
                    not isinstance(item, str)
                    or _SAFE_IDENTIFIER_RE.fullmatch(item) is None
                    for item in child
                ):
                    raise ArtifactScanError(
                        "canonical result identifier shape is invalid"
                    )
                raw_values.update(child)
            visit(child)

    visit(document)
    if len(raw_values) < len(PROFILE_IDS):
        raise ArtifactScanError("canonical result identifier set is incomplete")
    variants: set[bytes] = set()
    for value in raw_values:
        variants.update(_encoded_variants(value))
    return variants


def _byte_variants(value: bytes) -> set[bytes]:
    """Return the fixed, bounded set of encodings checked for one secret."""

    return {
        value,
        base64.b64encode(value),
        base64.urlsafe_b64encode(value),
        value.hex().encode("ascii"),
    }


def _encoded_variants(value: str, *, decode_base64: bool = False) -> set[bytes]:
    raw = value.encode("utf-8")
    variants = _byte_variants(raw)
    if decode_base64:
        compact = b"".join(raw.split())
        variants.add(compact)
        try:
            decoded = base64.b64decode(compact, validate=True)
        except (ValueError, base64.binascii.Error):
            decoded = b""
        if decoded:
            variants.update(_byte_variants(decoded))
    return variants


def _read_manifest(path: Path) -> dict:
    if path.is_symlink():
        raise ArtifactScanError("private manifest is missing or unreadable")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactScanError("private manifest is missing or unreadable") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("profiles"), list):
        raise ArtifactScanError("private manifest shape is invalid")
    return doc


def _read_memory_manifest(path: Path, provisioning_manifest: dict) -> dict:
    if path.is_symlink():
        raise ArtifactScanError("private memory manifest is missing or unreadable")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactScanError(
            "private memory manifest is missing or unreadable"
        ) from None
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    auxiliary = provisioning_manifest.get("auxiliary_accounts")
    if (
        not isinstance(doc, dict)
        or doc.get("schema_version") != 1
        or not isinstance(profiles, list)
        or len(profiles) != 1
        or not isinstance(profiles[0], dict)
        or profiles[0].get("profile_id") != MEMORY_CONTRACT_PROFILE_ID
        or not isinstance(auxiliary, list)
        or len(auxiliary) != 1
        or not isinstance(auxiliary[0], dict)
        or auxiliary[0].get("profile_id") != MEMORY_CONTRACT_PROFILE_ID
        or profiles[0] != auxiliary[0]
    ):
        raise ArtifactScanError("private memory manifest shape is invalid")
    return doc


def _read_codex_auth(path: Path) -> dict:
    if path.is_symlink():
        raise ArtifactScanError("private Codex auth is missing or unreadable")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactScanError("private Codex auth is missing or unreadable") from None
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    if not isinstance(tokens, dict):
        raise ArtifactScanError("private Codex auth shape is invalid")
    for field in ("id_token", "access_token", "refresh_token"):
        if not isinstance(tokens.get(field), str) or not tokens[field]:
            raise ArtifactScanError("private Codex auth secret material is incomplete")
    return doc


def _read_fixture_forbidden_values(path: Path) -> set[bytes]:
    if path.is_symlink():
        raise ArtifactScanError("persona fixture is missing or unreadable")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactScanError("persona fixture is missing or unreadable") from None
    privacy = doc.get("privacy") if isinstance(doc, dict) else None
    raw_values = (
        privacy.get("forbidden_in_agent_identity_or_persona")
        if isinstance(privacy, dict)
        else None
    )
    if not isinstance(raw_values, list) or not raw_values:
        raise ArtifactScanError("persona fixture privacy contract is invalid")
    variants: set[bytes] = set()
    for value in raw_values:
        if not isinstance(value, str) or not value:
            raise ArtifactScanError("persona fixture privacy contract is invalid")
        variants.update(_encoded_variants(value))
    return variants


def _secret_values(
    manifest: dict,
    memory_manifest: dict,
    codex_auth: dict,
    env: Mapping[str, str],
) -> set[bytes]:
    values: set[bytes] = set()

    def add(value: str, *, decode_base64: bool = False) -> None:
        values.update(_encoded_variants(value, decode_base64=decode_base64))

    for name in SECRET_ENV_NAMES:
        value = str(env.get(name) or "")
        if not value:
            raise ArtifactScanError("artifact scan credential inputs are incomplete")
        add(value, decode_base64=(name == "QA_CODEX_AUTH_JSON_B64"))

    for field in ("id_token", "access_token", "refresh_token"):
        add(codex_auth["tokens"][field])

    profiles = manifest["profiles"]
    if len(profiles) != len(PROFILE_IDS):
        raise ArtifactScanError("private manifest profile set is incomplete")
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ArtifactScanError("private manifest profile shape is invalid")
        for field in ("api_key", "secret_key_b64"):
            value = profile.get(field)
            if not isinstance(value, str) or not value:
                raise ArtifactScanError(
                    "private manifest secret material is incomplete"
                )
            add(value, decode_base64=(field == "secret_key_b64"))
        lease = profile.get("synthetic_account_lease")
        absence_token = lease.get("absence_token") if isinstance(lease, dict) else None
        if not isinstance(absence_token, str) or not absence_token:
            raise ArtifactScanError(
                "private manifest absence attestation is incomplete"
            )
        add(absence_token)

    memory_profiles = memory_manifest["profiles"]
    if len(memory_profiles) != 1 or not isinstance(memory_profiles[0], dict):
        raise ArtifactScanError("private memory manifest shape is invalid")
    memory_profile = memory_profiles[0]
    if memory_profile.get("profile_id") != MEMORY_CONTRACT_PROFILE_ID:
        raise ArtifactScanError("private memory manifest shape is invalid")
    for field in ("api_key", "secret_key_b64"):
        value = memory_profile.get(field)
        if not isinstance(value, str) or not value:
            raise ArtifactScanError(
                "private memory manifest secret material is incomplete"
            )
        add(value, decode_base64=(field == "secret_key_b64"))
    memory_lease = memory_profile.get("synthetic_account_lease")
    memory_absence_token = (
        memory_lease.get("absence_token") if isinstance(memory_lease, dict) else None
    )
    if not isinstance(memory_absence_token, str) or not memory_absence_token:
        raise ArtifactScanError(
            "private memory manifest absence attestation is incomplete"
        )
    add(memory_absence_token)
    return values


def _json_string_fragment_streams(data: bytes) -> tuple[tuple[bytes, ...], ...]:
    """Return ordered decoded JSON string streams for split-secret detection.

    A raw byte scan misses ``{"part1": "sec", "part2": "ret"}`` because JSON
    syntax separates the fragments.  Reconstructing value, key, and full-token
    streams in document order catches that boundary while also normalizing JSON
    escapes.  These streams are detection-only and are never printed.
    """
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return ()

    value_parts: list[bytes] = []
    key_parts: list[bytes] = []
    token_parts: list[bytes] = []

    def encoded(value: str) -> bytes:
        return value.encode("utf-8", errors="surrogatepass")

    def visit(value: object) -> None:
        if isinstance(value, str):
            part = encoded(value)
            value_parts.append(part)
            token_parts.append(part)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for key, item in value.items():
                part = encoded(str(key))
                key_parts.append(part)
                token_parts.append(part)
                visit(item)

    try:
        visit(document)
    except RecursionError:
        return ()
    return tuple(
        stream
        for stream in (tuple(value_parts), tuple(key_parts), tuple(token_parts))
        if stream
    )


def _can_reconstruct_secret(parts: tuple[bytes, ...], secret: bytes) -> bool:
    """Detect a secret split across padded, ordered JSON strings.

    A fragment may be surrounded by unrelated bytes inside its JSON string,
    for example ``"prefix-sec-suffix"`` followed by
    ``"prefix-ret-suffix"``.  Only a contiguous slice of each string may
    advance the reconstruction, and strings may be skipped.  Fragments are at
    least eight bytes except for a final tail after a strong match; accepting
    arbitrary one-byte matches would make ordinary JSON text reconstruct most
    long secrets as a coincidental character subsequence.  Keeping the
    furthest secret offset is sufficient: any later string that could advance
    an earlier offset beyond it necessarily contains the suffix starting at
    the furthest offset too.
    """
    offset = 0
    fragments = 0
    max_fragments = (len(secret) + MIN_RECONSTRUCTED_FRAGMENT_BYTES - 1) // (
        MIN_RECONSTRUCTED_FRAGMENT_BYTES
    ) + 1
    for part in parts:
        if not part or offset == len(secret):
            continue
        remaining = secret[offset:]
        low = 0
        high = len(remaining)
        # Prefix containment is monotonic, so binary search avoids a
        # quadratic scan for long OAuth material or large JSON strings.
        while low < high:
            middle = (low + high + 1) // 2
            if remaining[:middle] in part:
                low = middle
            else:
                high = middle - 1
        is_short_final_tail = (
            fragments > 0
            and low == len(remaining)
            and 0 < low < MIN_RECONSTRUCTED_FRAGMENT_BYTES
        )
        if low < MIN_RECONSTRUCTED_FRAGMENT_BYTES and not is_short_final_tail:
            continue
        fragments += 1
        if fragments > max_fragments:
            return False
        offset += low
        if offset == len(secret):
            return True
    return False


def _contains_exact_secret(data: bytes, secrets: set[bytes]) -> bool:
    return any(secret in data for secret in secrets)


def scan_artifacts(
    artifact_root: Path,
    manifest_path: Path,
    memory_manifest_path: Path,
    codex_auth_path: Path,
    fixture_path: Path,
    canonical_result_path: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return fixed finding categories; an empty list means the boundary is clean."""
    active_env = os.environ if env is None else env
    manifest = _read_manifest(manifest_path)
    memory_manifest = _read_memory_manifest(memory_manifest_path, manifest)
    codex_auth = _read_codex_auth(codex_auth_path)
    secrets = _secret_values(manifest, memory_manifest, codex_auth, active_env)
    forbidden_fixture_material = _read_fixture_forbidden_values(fixture_path)
    canonical_result, canonical_result_file = _read_canonical_result(
        canonical_result_path
    )
    forbidden_identifiers = _canonical_identifier_values(canonical_result)
    if artifact_root.is_symlink():
        raise ArtifactScanError("public artifact root is missing or unreadable")
    try:
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ArtifactScanError(
            "public artifact root is missing or unreadable"
        ) from None
    if not root.is_dir():
        raise ArtifactScanError("public artifact root is not a directory")
    if root == canonical_result_file or root in canonical_result_file.parents:
        raise ArtifactScanError("canonical result must remain outside public artifacts")

    findings: set[str] = set()
    file_count = 0
    total_bytes = 0
    try:
        paths = sorted(root.rglob("*"))
    except OSError:
        raise ArtifactScanError("public artifact tree is unreadable") from None
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.add("public artifact tree contains a symbolic link")
            continue
        if path.is_dir():
            findings.add("public artifact tree contains an unexpected directory")
            continue
        if not path.is_file():
            findings.add("public artifact tree contains a non-regular file")
            continue
        if relative not in EXPECTED_PUBLIC_FILES:
            findings.add("public artifact tree contains an unexpected file")
        file_count += 1
        if file_count > MAX_FILES:
            findings.add("public artifact file-count limit exceeded")
            break
        try:
            size = path.stat().st_size
        except OSError:
            findings.add("public artifact file metadata is unreadable")
            continue
        total_bytes += size
        if size > MAX_FILE_BYTES:
            findings.add("public artifact per-file size limit exceeded")
            continue
        if total_bytes > MAX_TOTAL_BYTES:
            findings.add("public artifact total-size limit exceeded")
            break
        try:
            data = path.read_bytes()
        except OSError:
            findings.add("public artifact file is unreadable")
            continue
        fragment_streams = (
            _json_string_fragment_streams(data) if path.suffix == ".json" else ()
        )
        if _contains_exact_secret(data, secrets) or any(
            _can_reconstruct_secret(stream, secret)
            for stream in fragment_streams
            for secret in secrets
        ):
            findings.add("public artifact contains exact credential material")
        if _contains_exact_secret(data, forbidden_fixture_material) or any(
            _can_reconstruct_secret(stream, forbidden)
            for stream in fragment_streams
            for forbidden in forbidden_fixture_material
        ):
            findings.add("public artifact contains forbidden persona fixture material")
        if _contains_exact_secret(data, forbidden_identifiers) or any(
            _can_reconstruct_secret(stream, identifier)
            for stream in fragment_streams
            for identifier in forbidden_identifiers
        ):
            findings.add(
                "public artifact contains forbidden synthetic identifier material"
            )
        if _FORBIDDEN_JSON_KEY.search(data) or (
            path.suffix == ".json" and _team_json_private_field_issue(data)
        ):
            findings.add("public artifact contains a forbidden private-data field")
        if _CREDENTIAL_SIGNATURE.search(data):
            findings.add("public artifact contains a credential-shaped token")
    missing = EXPECTED_PUBLIC_FILES - {
        path.relative_to(root).as_posix()
        for path in paths
        if path.is_file() and not path.is_symlink()
    }
    if missing:
        findings.add("public artifact tree is missing required files")
    return sorted(findings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan public E2E artifacts for secrets"
    )
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--memory-manifest", type=Path, required=True)
    parser.add_argument("--codex-auth", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--canonical-result", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        findings = scan_artifacts(
            args.artifacts,
            args.manifest,
            args.memory_manifest,
            args.codex_auth,
            args.fixture,
            args.canonical_result,
        )
    except ArtifactScanError as exc:
        findings = [str(exc)]
    except Exception:
        findings = ["artifact scan encountered an internal error"]
    if findings:
        print("artifact secret scan: FAIL", file=sys.stderr)
        for finding in findings:
            print(f"ERROR: {finding}", file=sys.stderr)
        return 1
    print("artifact secret scan: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
