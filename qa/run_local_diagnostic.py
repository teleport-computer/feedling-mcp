#!/usr/bin/env python3
"""Run API-key qualification locally against the deployed test environment.

This entry point is intentionally diagnostic-only.  It reuses the trusted
provisioning and isolated Codex worker boundaries, but it never claims release
qualification because local execution lacks the protected deployment and
server-reaper attestations required by the GitHub release gate.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

try:
    from qa import diagnostic_results
    from qa import harness_provenance
    from qa import install_codex_auth
    from qa import provision_profiles
    from qa import render_diagnostic_artifacts
    from qa import run_codex_profile_workers
    from qa import verify_deployment as deployment_verifier
    from qa import write_codex_config
    from qa.orchestration_contract import PROFILE_AGENT_TYPES, PROFILE_IDS
    from qa.validate_diagnostic_attempts import parent_cleanup_is_deferred
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    import diagnostic_results  # type: ignore[no-redef]
    import harness_provenance  # type: ignore[no-redef]
    import install_codex_auth  # type: ignore[no-redef]
    import provision_profiles  # type: ignore[no-redef]
    import render_diagnostic_artifacts  # type: ignore[no-redef]
    import run_codex_profile_workers  # type: ignore[no-redef]
    import verify_deployment as deployment_verifier  # type: ignore[no-redef]
    import write_codex_config  # type: ignore[no-redef]
    from orchestration_contract import PROFILE_AGENT_TYPES, PROFILE_IDS
    from validate_diagnostic_attempts import parent_cleanup_is_deferred


LOCKED_BASE_URL = "https://test-api.feedling.app"
PUBLIC_HEALTH_IDENTITY_SOURCE = "public_healthz_release_git_commit"
PUBLIC_HEALTH_IDENTITY_GAP = "DEPLOYMENT_SHA_NOT_INDEPENDENTLY_ATTESTED"
BASELINE_RUNTIME = provision_profiles.BASELINE_RUNTIME_REQUIREMENT
RUNTIME_V2_RUNTIME = provision_profiles.RUNTIME_V2_REQUIREMENT
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_PROVIDER_MODELS = {
    "official-deepseek": "deepseek-v4-flash",
    "official-anthropic": "claude-sonnet-4-5",
    "official-openai": "gpt-5.4",
    "official-gemini": "gemini-2.5-flash",
    "openrouter-claude": "anthropic/claude-sonnet-4.5",
    "openrouter-openai": "openai/gpt-4.1-mini",
    "openrouter-glm": "z-ai/glm-4.5-air",
    "relay-kongbeiqie": "[特价纯血]claude-opus-4-6",
}
MISSING_STRICT_EVIDENCE = (
    "trusted_backend_and_worker_sha_attestation",
    "server_side_synthetic_account_reaper_attestation",
    "protected_full_matrix_release_gate",
    "provider_reasoning_token_attestation_when_target_omits_it",
    "five_stage_internal_latency_when_target_omits_it",
)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DOTENV_ADMIN_SECRET_NAMES = ("IO_E2E_ADMIN_TOKEN",)
_MAX_ENV_BYTES = 1024 * 1024
_MAX_AUTH_BYTES = 128 * 1024
_MAX_PUBLIC_FILES = 64
_MAX_PUBLIC_FILE_BYTES = 32 * 1024 * 1024
_MAX_DEBUG_FILE_BYTES = 64 * 1024 * 1024
_MAX_DEBUG_FILES = 2048
_MAX_DEBUG_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
_MAX_SOURCE_FILES = 50_000
_MAX_SOURCE_TOTAL_BYTES = 512 * 1024 * 1024
_SOURCE_SNAPSHOT_ALLOWLIST = harness_provenance.WORKER_SOURCE_PATHS
_PERSONA_FIXTURE = Path("qa/fixtures/persona-import-v1.json")
_SOURCE_SNAPSHOT_EXCLUDED_NAMES = (
    harness_provenance.WORKER_SNAPSHOT_EXCLUDED_NAMES
)
_DEBUG_QUARANTINE_EXCLUDED_DIRECTORIES = frozenset(
    (".venv", "site-packages", "__pycache__")
)
_DEBUG_QUARANTINE_EXCLUDED_SUFFIXES = frozenset((".pyc", ".pyo"))
_FORBIDDEN_PUBLIC_JSON_KEY = re.compile(
    rb'(?i)"(?:api_key|secret_key_b64|private_key(?:_b64)?|provider_key|admin_token|'
    rb'raw_chat|raw_trace|raw_(?:private_)?reasoning|body_ct|thinking_body_ct|K_user)"\s*:'
)
_CREDENTIAL_SIGNATURE = re.compile(rb"(?:sk-ant-|sk-or-v1-|sk-proj-)[A-Za-z0-9_-]{8,}")
_MAX_CODEX_BINARY_BYTES = 512 * 1024 * 1024
_MAX_CODEX_PACKAGE_BYTES = 128 * 1024
_TRUSTED_LOCAL_CODEX = {
    ("darwin", "arm64"): {
        "package_dir": "codex-darwin-arm64",
        "package_version": "0.144.3-darwin-arm64",
        "target": "aarch64-apple-darwin",
        "tree_sha256": "0e56cced8d08acc8da8f27e3eb2688eab57902037efa8b856ceb1d188e6bbfda",
    }
}
_BENCHMARK_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class LocalDiagnosticError(RuntimeError):
    """Sanitized local diagnostic failure safe to show to an operator."""


def _read_trusted_codex_file(path: Path, *, max_bytes: int) -> bytes:
    """Read one immutable-looking package file without following a link."""

    if not path.is_absolute() or path.is_symlink():
        raise LocalDiagnosticError("trusted Codex installation is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LocalDiagnosticError("trusted Codex installation is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > max_bytes
        ):
            raise LocalDiagnosticError("trusted Codex installation is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise LocalDiagnosticError("trusted Codex installation changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LocalDiagnosticError("trusted Codex installation changed")
        completed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        metadata.st_dev != completed.st_dev
        or metadata.st_ino != completed.st_ino
        or metadata.st_size != completed.st_size
        or metadata.st_mtime_ns != completed.st_mtime_ns
    ):
        raise LocalDiagnosticError("trusted Codex installation changed")
    return b"".join(chunks)


def _trusted_codex_platform() -> Mapping[str, str]:
    machine = platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    spec = _TRUSTED_LOCAL_CODEX.get((sys.platform, machine))
    if spec is None:
        raise LocalDiagnosticError(
            "local diagnostic has no pinned Codex artifact for this platform"
        )
    return spec


def _verify_local_codex_provenance(codex_bin: Path) -> None:
    """Require the pinned official native Codex artifact before OAuth install."""

    spec = _trusted_codex_platform()
    candidate = codex_bin.expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise LocalDiagnosticError("trusted Codex executable is invalid")
    try:
        executable = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalDiagnosticError("trusted Codex executable is unavailable") from None
    expected_suffix = Path(
        "node_modules",
        "@openai",
        str(spec["package_dir"]),
        "vendor",
        str(spec["target"]),
        "bin",
        "codex",
    ).parts
    if executable.parts[-len(expected_suffix) :] != expected_suffix:
        raise LocalDiagnosticError("trusted Codex executable provenance is invalid")
    package_root = executable.parents[3]
    expected_files = {
        Path("README.md"),
        Path("package.json"),
        Path("vendor", str(spec["target"]), "bin", "codex"),
        Path("vendor", str(spec["target"]), "bin", "codex-code-mode-host"),
        Path("vendor", str(spec["target"]), "codex-package.json"),
        Path("vendor", str(spec["target"]), "codex-path", "rg"),
        Path(
            "vendor",
            str(spec["target"]),
            "codex-resources",
            "zsh",
            "bin",
            "zsh",
        ),
    }
    # Validate the npm package subtree itself. Broader prefixes such as
    # /usr/local/lib can legitimately be 0775 on a single-user macOS install;
    # the exact package-tree digest below is the executable provenance control.
    for directory in (package_root, *package_root.parents[:5]):
        try:
            metadata = directory.lstat()
        except OSError:
            raise LocalDiagnosticError(
                "trusted Codex installation is unavailable"
            ) from None
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise LocalDiagnosticError("trusted Codex installation is unsafe")
    try:
        entries = list(package_root.rglob("*"))
    except OSError:
        raise LocalDiagnosticError("trusted Codex installation is unavailable") from None
    files: set[Path] = set()
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError:
            raise LocalDiagnosticError(
                "trusted Codex installation is unavailable"
            ) from None
        if entry.is_symlink() or metadata.st_uid not in {0, os.geteuid()}:
            raise LocalDiagnosticError("trusted Codex installation is unsafe")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise LocalDiagnosticError("trusted Codex installation is unsafe")
        elif stat.S_ISREG(metadata.st_mode):
            files.add(entry.relative_to(package_root))
        else:
            raise LocalDiagnosticError("trusted Codex installation is unsafe")
    if files != expected_files:
        raise LocalDiagnosticError("trusted Codex package file set is invalid")
    try:
        package = json.loads(
            _read_trusted_codex_file(
                package_root / "package.json", max_bytes=_MAX_CODEX_PACKAGE_BYTES
            )
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LocalDiagnosticError("trusted Codex package metadata is invalid") from None
    if (
        not isinstance(package, dict)
        or package.get("name") != "@openai/codex"
        or package.get("version") != spec["package_version"]
    ):
        raise LocalDiagnosticError("trusted Codex package identity is invalid")
    tree_digest = hashlib.sha256()
    for relative in sorted(expected_files, key=lambda item: item.as_posix()):
        data = _read_trusted_codex_file(
            package_root / relative,
            max_bytes=(
                _MAX_CODEX_BINARY_BYTES
                if relative.parts[-2:] == ("bin", "codex")
                else 256 * 1024 * 1024
            ),
        )
        encoded = relative.as_posix().encode("utf-8")
        tree_digest.update(len(encoded).to_bytes(8, "big"))
        tree_digest.update(encoded)
        tree_digest.update(len(data).to_bytes(8, "big"))
        tree_digest.update(data)
    if tree_digest.hexdigest() != spec["tree_sha256"]:
        raise LocalDiagnosticError("trusted Codex package digest is invalid")
    try:
        metadata = executable.stat()
    except OSError:
        raise LocalDiagnosticError("trusted Codex executable is unavailable") from None
    if not os.access(executable, os.X_OK) or not stat.S_IMODE(metadata.st_mode) & 0o111:
        raise LocalDiagnosticError("trusted Codex executable is not executable")


def _resolve_trusted_codex_binary(explicit: Path | None = None) -> Path:
    """Resolve PATH only as a package locator; never execute its wrapper."""

    launcher = str(explicit.expanduser()) if explicit is not None else shutil.which("codex")
    if not launcher:
        raise LocalDiagnosticError("Codex CLI is not installed")
    try:
        resolved = Path(launcher).resolve(strict=True)
    except (OSError, RuntimeError):
        raise LocalDiagnosticError("Codex CLI installation is unavailable") from None
    spec = _trusted_codex_platform()
    expected_native_suffix = Path(
        "node_modules",
        "@openai",
        str(spec["package_dir"]),
        "vendor",
        str(spec["target"]),
        "bin",
        "codex",
    ).parts
    if resolved.parts[-len(expected_native_suffix) :] == expected_native_suffix:
        return resolved
    if resolved.name != "codex.js" or resolved.parent.name != "bin":
        raise LocalDiagnosticError(
            "Codex CLI must be the pinned official npm installation"
        )
    package_root = resolved.parent.parent
    try:
        package = json.loads(
            _read_trusted_codex_file(
                package_root / "package.json", max_bytes=_MAX_CODEX_PACKAGE_BYTES
            )
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LocalDiagnosticError("Codex CLI package metadata is invalid") from None
    if (
        not isinstance(package, dict)
        or package.get("name") != "@openai/codex"
        or package.get("version") != "0.144.3"
    ):
        raise LocalDiagnosticError("Codex CLI package identity is invalid")
    return (
        package_root
        / "node_modules"
        / "@openai"
        / str(spec["package_dir"])
        / "vendor"
        / str(spec["target"])
        / "bin"
        / "codex"
    )


@dataclass(frozen=True)
class DiagnosticOptions:
    env_file: Path
    candidate_sha: str
    codex_model: str
    profile_ids: tuple[str, ...]
    preflight_only: bool
    source_root: Path
    auth_file: Path
    private_base: Path
    codex_bin: Path
    worker_python: Path | None = None
    worker_runtime_roots: tuple[Path, ...] = ()
    runtime_requirement: str = BASELINE_RUNTIME
    allow_public_health_identity: bool = False


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    artifact_root: Path
    profile_artifacts: Path
    private_root: Path
    worker_source_root: Path
    codex_home: Path
    manifest: Path
    profile_manifests: Path
    supervisor_home: Path
    supervisor_tmp: Path
    supervisor_work: Path
    worker_root: Path
    worker_outputs: Path
    aggregation_inputs: Path
    orchestration_receipt: Path
    deployment_receipt: Path


@dataclass(frozen=True)
class DiagnosticManifestClassification:
    """Ordered local-only READY/BLOCKED provisioning classification."""

    ready_profile_ids: tuple[str, ...]
    blocked_profile_ids: tuple[str, ...]
    provision_statuses: tuple[tuple[str, str], ...]
    provision_failure_codes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DiagnosticDependencies:
    provision: Callable[..., dict[str, Any]] = provision_profiles.provision
    cleanup: Callable[..., dict[str, Any]] = provision_profiles.cleanup
    launch: Callable[..., dict[str, Any]] = run_codex_profile_workers.launch
    verify_deployment: Callable[..., dict[str, Any]] = (
        deployment_verifier.verify_deployment
    )
    observe_deployment: Callable[..., dict[str, Any]] | None = None
    verify_codex_version: Callable[[Path], None] = (
        run_codex_profile_workers.verify_codex_version
    )
    verify_codex_provenance: Callable[[Path], None] = (
        _verify_local_codex_provenance
    )
    verify_login: Callable[[Path, Path, Path, Path], None] | None = None
    verify_codex_exec: (
        Callable[[DiagnosticOptions, RunPaths], dict[str, Any]] | None
    ) = None
    collect_harness_provenance: Callable[[Path], dict[str, Any]] = (
        harness_provenance.collect
    )


def _known_env_names() -> set[str]:
    names = {"QA_CODEX_MODEL", *_DOTENV_ADMIN_SECRET_NAMES}
    for spec in provision_profiles.PROFILE_SPECS.values():
        names.add(spec.credential_env)
        names.add(spec.model_env)
        if spec.base_url_env:
            names.add(spec.base_url_env)
    return names


def _inspect_worker_python(path: Path) -> tuple[Path, tuple[Path, ...]]:
    """Validate one crypto-capable Python and return owner-controlled roots."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        executable = candidate.resolve(strict=True)
        metadata = executable.stat()
    except (OSError, RuntimeError):
        raise LocalDiagnosticError("qualification Python is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise LocalDiagnosticError(
            "qualification Python must be an owner-controlled executable"
        )
    probe = (
        "import cryptography,nacl,json,sys;"
        "print(json.dumps([sys.prefix,sys.base_prefix],separators=(',',':')))"
    )
    try:
        result = subprocess.run(
            [str(executable), "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError):
        raise LocalDiagnosticError("qualification Python could not execute") from None
    if result.returncode != 0:
        raise LocalDiagnosticError(
            "qualification Python is missing cryptography or PyNaCl support"
        )
    try:
        raw_roots = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise LocalDiagnosticError("qualification Python identity is invalid") from None
    if not isinstance(raw_roots, list) or len(raw_roots) != 2:
        raise LocalDiagnosticError("qualification Python identity is invalid")
    roots: list[Path] = []
    for raw in raw_roots:
        if not isinstance(raw, str):
            raise LocalDiagnosticError("qualification Python identity is invalid")
        try:
            root = Path(raw).resolve(strict=True)
            root_metadata = root.stat()
        except (OSError, RuntimeError):
            raise LocalDiagnosticError(
                "qualification Python runtime is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or root == Path(root.anchor)
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise LocalDiagnosticError(
                "qualification Python runtime must be owner-controlled"
            )
        roots.append(root)
    unique_roots = tuple(dict.fromkeys(roots))
    home = Path.home().resolve()
    codex_private = (home / ".codex").resolve()
    if any(
        root == home
        or root in home.parents
        or root == codex_private
        or codex_private in root.parents
        for root in unique_roots
    ):
        raise LocalDiagnosticError("qualification Python runtime root is too broad")
    if not any(executable.parent == root / "bin" for root in unique_roots):
        raise LocalDiagnosticError("qualification Python identity is invalid")
    try:
        bin_metadata = executable.parent.stat()
    except OSError:
        raise LocalDiagnosticError(
            "qualification Python runtime bin is unavailable"
        ) from None
    if (
        not stat.S_ISDIR(bin_metadata.st_mode)
        or bin_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(bin_metadata.st_mode) & 0o022
    ):
        raise LocalDiagnosticError(
            "qualification Python runtime bin must be owner-controlled"
        )
    return executable, unique_roots


def _resolve_worker_python(explicit: Path | None) -> tuple[Path, tuple[Path, ...]]:
    if explicit is not None:
        return _inspect_worker_python(explicit)
    candidates = (
        Path.home() / ".cache/io-e2e-qa-venv/bin/python",
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3",
        Path(sys.executable),
    )
    for candidate in dict.fromkeys(candidates):
        try:
            return _inspect_worker_python(candidate)
        except LocalDiagnosticError:
            continue
    raise LocalDiagnosticError(
        "no owner-controlled qualification Python with cryptography is available; "
        "pass --worker-python"
    )


def _resolve_runtime_options(options: DiagnosticOptions) -> DiagnosticOptions:
    """Resolve and bind one canonical worker runtime for the entire run."""

    if options.worker_python is not None and options.worker_runtime_roots:
        resolved = options
    elif options.worker_python is None and not options.worker_runtime_roots:
        executable, roots = _resolve_worker_python(None)
        resolved = replace(
            options,
            worker_python=executable,
            worker_runtime_roots=roots,
        )
    elif options.worker_python is not None and not options.worker_runtime_roots:
        executable, roots = _resolve_worker_python(options.worker_python)
        resolved = replace(
            options,
            worker_python=executable,
            worker_runtime_roots=roots,
        )
    else:
        raise LocalDiagnosticError(
            "qualification Python runtime identity is incomplete"
        )

    protected = (
        resolved.source_root,
        resolved.env_file,
        resolved.auth_file,
        resolved.private_base,
    )
    for root in resolved.worker_runtime_roots:
        try:
            runtime = root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise LocalDiagnosticError(
                "qualification Python runtime is unavailable"
            ) from None
        for path in protected:
            candidate = path.expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            candidate = candidate.resolve(strict=False)
            if (
                runtime == candidate
                or runtime in candidate.parents
                or candidate in runtime.parents
            ):
                raise LocalDiagnosticError(
                    "qualification Python runtime overlaps protected run data"
                )
    return resolved


def _canonical_worker_runtime(
    options: DiagnosticOptions,
) -> tuple[Path, tuple[Path, ...]]:
    if options.worker_python is None or not options.worker_runtime_roots:
        raise LocalDiagnosticError("qualification Python was not resolved")
    return options.worker_python, options.worker_runtime_roots


def _owned_private_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise LocalDiagnosticError(f"{label} is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LocalDiagnosticError(f"{label} is unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > max_bytes
        ):
            raise LocalDiagnosticError(f"{label} must be an owner-only regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a small dotenv file without shell evaluation or interpolation."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    raw = _owned_private_file(candidate, "environment file", max_bytes=_MAX_ENV_BYTES)
    if len(raw) > _MAX_ENV_BYTES:
        raise LocalDiagnosticError("environment file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise LocalDiagnosticError("environment file is not UTF-8") from None

    allowed = _known_env_names()
    values: dict[str, str] = {}
    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LocalDiagnosticError(
                f"environment file line {line_number} is not NAME=VALUE"
            )
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME_RE.fullmatch(name) or name not in allowed:
            safe_name = name if _ENV_NAME_RE.fullmatch(name) else "invalid-name"
            raise LocalDiagnosticError(
                f"environment file contains unsupported variable: {safe_name}"
            )
        if name in values:
            raise LocalDiagnosticError(
                f"environment file contains duplicate variable: {name}"
            )
        try:
            tokens = shlex.split(raw_value.strip(), comments=False, posix=True)
        except ValueError:
            raise LocalDiagnosticError(
                f"environment file has invalid quoting for variable: {name}"
            ) from None
        if len(tokens) != 1 or not tokens[0] or "\x00" in tokens[0]:
            raise LocalDiagnosticError(
                f"environment file has an invalid value for variable: {name}"
            )
        values[name] = tokens[0]
    return values


def _selected_profiles(requested: Sequence[str]) -> tuple[str, ...]:
    values = tuple(requested) if requested else tuple(PROFILE_IDS)
    if len(set(values)) != len(values) or any(
        value not in PROFILE_IDS for value in values
    ):
        raise LocalDiagnosticError("profile selection is invalid")
    selected = set(values)
    return tuple(profile_id for profile_id in PROFILE_IDS if profile_id in selected)


def prepare_environment(
    loaded: Mapping[str, str], profile_ids: Sequence[str], run_id: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Validate selected provider slots and add only non-secret run defaults."""

    env: dict[str, str] = {}
    validated_credentials: list[str] = []
    for profile_id in profile_ids:
        spec = provision_profiles.PROFILE_SPECS[profile_id]
        credential = str(loaded.get(spec.credential_env) or "")
        if (
            not credential
            or len(credential) > 64 * 1024
            or any(character.isspace() for character in credential)
        ):
            raise LocalDiagnosticError(
                f"missing or invalid credential variable: {spec.credential_env}"
            )
        if spec.credential_env not in validated_credentials:
            validated_credentials.append(spec.credential_env)
            env[spec.credential_env] = credential

        model = str(loaded.get(spec.model_env) or DEFAULT_PROVIDER_MODELS[profile_id])
        if not re.fullmatch(spec.allowed_model_regex, model):
            raise LocalDiagnosticError(
                f"model variable does not match its locked profile: {spec.model_env}"
            )
        env[spec.model_env] = model
        if spec.base_url_env:
            base_url = str(loaded.get(spec.base_url_env) or spec.allowed_base_url)
            if base_url.rstrip("/") != spec.allowed_base_url.rstrip("/"):
                raise LocalDiagnosticError(
                    f"base URL variable is not the locked relay endpoint: {spec.base_url_env}"
                )
            env[spec.base_url_env] = spec.allowed_base_url

    env["IO_E2E_BASE_URL"] = LOCKED_BASE_URL
    env["QA_RUN_ID"] = run_id
    return env, tuple(validated_credentials)


def _deployment_environment(loaded: Mapping[str, str]) -> dict[str, str]:
    token = str(loaded.get("IO_E2E_ADMIN_TOKEN") or "").strip()
    if (
        not token
        or len(token) < 8
        or len(token) > 64 * 1024
        or any(character.isspace() for character in token)
    ):
        raise LocalDiagnosticError(
            "missing or invalid test admin credential for deployment verification"
        )
    return {
        "IO_E2E_BASE_URL": LOCKED_BASE_URL,
        "IO_E2E_ADMIN_TOKEN": token,
    }


def _loaded_credential_values(loaded: Mapping[str, str]) -> tuple[str, ...]:
    """Return every provider credential loaded, not only the selected subset."""

    names = tuple(
        dict.fromkeys(
            (
                *(
                    spec.credential_env
                    for spec in provision_profiles.PROFILE_SPECS.values()
                ),
                *_DOTENV_ADMIN_SECRET_NAMES,
            )
        )
    )
    values: list[str] = []
    for name in names:
        value = str(loaded.get(name) or "")
        if not value:
            continue
        if (
            len(value) < 8
            or len(value) > 64 * 1024
            or any(character.isspace() for character in value)
        ):
            raise LocalDiagnosticError(f"invalid credential variable: {name}")
        values.append(value)
    return tuple(values)


def _fixture_privacy_values(source_root: Path) -> tuple[str, ...]:
    """Load the persona fixture's public-artifact privacy canaries."""

    path = source_root.resolve(strict=True) / _PERSONA_FIXTURE
    if path.is_symlink():
        raise LocalDiagnosticError("persona fixture privacy contract is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LocalDiagnosticError(
            "persona fixture privacy contract is unavailable"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise LocalDiagnosticError(
                    "persona fixture privacy contract changed while reading"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LocalDiagnosticError(
                "persona fixture privacy contract changed while reading"
            )
        completed = os.fstat(descriptor)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > _MAX_ENV_BYTES
        or metadata.st_dev != completed.st_dev
        or metadata.st_ino != completed.st_ino
        or metadata.st_size != completed.st_size
        or metadata.st_mtime_ns != completed.st_mtime_ns
        or len(data) != metadata.st_size
    ):
        raise LocalDiagnosticError("persona fixture privacy contract is invalid")
    try:
        document = json.loads(data)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LocalDiagnosticError("persona fixture privacy contract is invalid") from None
    privacy = document.get("privacy") if isinstance(document, dict) else None
    values = (
        privacy.get("forbidden_in_agent_identity_or_persona")
        if isinstance(privacy, dict)
        else None
    )
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or len(value) < 8 for value in values)
    ):
        raise LocalDiagnosticError("persona fixture privacy contract is invalid")
    return tuple(values)


def _make_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)
    return path


def create_run_paths(options: DiagnosticOptions, run_id: str) -> RunPaths:
    source = options.source_root.resolve(strict=True)
    artifact_parent = source / "qualification-artifacts"
    artifact_parent.mkdir(mode=0o700, exist_ok=True)
    artifact_parent.chmod(0o700)
    artifact_root = _make_private_directory(artifact_parent / run_id)
    profile_artifacts = _make_private_directory(artifact_root / "profiles")

    options.private_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    options.private_base.chmod(0o700)
    private_root = _make_private_directory(options.private_base / run_id)
    worker_source_root = _make_private_directory(private_root / "source-snapshot")
    codex_home = _make_private_directory(private_root / "codex-home")
    profile_manifests = _make_private_directory(private_root / "profile-manifests")
    supervisor_home = _make_private_directory(private_root / "supervisor-home")
    supervisor_tmp = _make_private_directory(private_root / "supervisor-tmp")
    supervisor_work = _make_private_directory(private_root / "supervisor-work")
    worker_root = _make_private_directory(private_root / "workers")
    for _, agent_type in PROFILE_AGENT_TYPES:
        agent_root = _make_private_directory(worker_root / agent_type)
        for leaf in ("home", "tmp", "work"):
            _make_private_directory(agent_root / leaf)
    worker_outputs = _make_private_directory(private_root / "worker-outputs")
    aggregation_inputs = _make_private_directory(private_root / "aggregation-inputs")
    return RunPaths(
        run_id=run_id,
        artifact_root=artifact_root,
        profile_artifacts=profile_artifacts,
        private_root=private_root,
        worker_source_root=worker_source_root,
        codex_home=codex_home,
        manifest=private_root / "provisioning-manifest.json",
        profile_manifests=profile_manifests,
        supervisor_home=supervisor_home,
        supervisor_tmp=supervisor_tmp,
        supervisor_work=supervisor_work,
        worker_root=worker_root,
        worker_outputs=worker_outputs,
        aggregation_inputs=aggregation_inputs,
        orchestration_receipt=private_root / "orchestration-receipt.json",
        deployment_receipt=private_root / "deployment-receipt.json",
    )


def _verify_codex_location_separation(
    codex_bin: Path, options: DiagnosticOptions, paths: RunPaths
) -> None:
    try:
        executable = codex_bin.resolve(strict=True)
        forbidden = (
            options.source_root.resolve(strict=True),
            paths.private_root.resolve(strict=True),
            paths.artifact_root.resolve(strict=True),
            options.auth_file.expanduser().parent.resolve(strict=True),
            Path(tempfile.gettempdir()).resolve(strict=True),
        )
    except (OSError, RuntimeError):
        raise LocalDiagnosticError("trusted Codex location is unavailable") from None
    if any(executable == root or root in executable.parents for root in forbidden):
        raise LocalDiagnosticError("trusted Codex installation location is unsafe")


def _source_snapshot_excluded(path: Path, sensitive_paths: set[Path]) -> bool:
    return (
        path in sensitive_paths
        or path.name.startswith(".env")
        or path.name in _SOURCE_SNAPSHOT_EXCLUDED_NAMES
    )


def _create_worker_source_snapshot(
    source_root: Path,
    destination: Path,
    *,
    sensitive_paths: Sequence[Path],
    secret_values: Sequence[str],
) -> None:
    """Copy code into an owner-only, credential-free worker read root."""

    try:
        source = source_root.resolve(strict=True)
        target = destination.resolve(strict=True)
        source_metadata = source.lstat()
        target_metadata = target.lstat()
    except (OSError, RuntimeError):
        raise LocalDiagnosticError(
            "worker source snapshot root is unavailable"
        ) from None
    if (
        not stat.S_ISDIR(source_metadata.st_mode)
        or source_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(source_metadata.st_mode) & 0o022
    ):
        raise LocalDiagnosticError("source checkout must be owner-controlled")
    if (
        not stat.S_ISDIR(target_metadata.st_mode)
        or target_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(target_metadata.st_mode) != 0o700
    ):
        raise LocalDiagnosticError("worker source snapshot must be owner-only")
    if source == target or source in target.parents or target in source.parents:
        raise LocalDiagnosticError(
            "worker source snapshot must be outside the checkout"
        )
    try:
        if any(target.iterdir()):
            raise LocalDiagnosticError("worker source snapshot must start empty")
    except OSError:
        raise LocalDiagnosticError("worker source snapshot is unreadable") from None

    protected: set[Path] = set()
    for path in sensitive_paths:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        protected.add(candidate.resolve(strict=False))
    secret_needles = _secret_needles(secret_values)
    copied_files = 0
    copied_bytes = 0

    def ensure_target_directory(relative: Path) -> Path:
        current = target
        for part in relative.parts:
            current = current / part
            if not current.exists():
                _make_private_directory(current)
        return current

    def copy_path(source_path: Path, target_path: Path) -> None:
        nonlocal copied_files, copied_bytes
        if _source_snapshot_excluded(source_path, protected):
            return
        try:
            metadata = source_path.lstat()
        except OSError:
            raise LocalDiagnosticError(
                "required worker source is unavailable"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise LocalDiagnosticError("source checkout contains an unsafe symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise LocalDiagnosticError(
                    "source checkout contains an unsafe directory"
                )
            _make_private_directory(target_path)
            try:
                with os.scandir(source_path) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError:
                raise LocalDiagnosticError(
                    "source checkout could not be snapshotted"
                ) from None
            for entry in entries:
                copy_path(Path(entry.path), target_path / entry.name)
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > _MAX_SOURCE_FILE_BYTES
        ):
            raise LocalDiagnosticError("source checkout contains an unsafe file")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source_path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                data = handle.read(_MAX_SOURCE_FILE_BYTES + 1)
                completed = os.fstat(handle.fileno())
        except OSError:
            raise LocalDiagnosticError(
                "source checkout changed while it was snapshotted"
            ) from None
        if (
            len(data) > _MAX_SOURCE_FILE_BYTES
            or metadata.st_dev != opened.st_dev
            or metadata.st_ino != opened.st_ino
            or metadata.st_size != opened.st_size
            or opened.st_dev != completed.st_dev
            or opened.st_ino != completed.st_ino
            or opened.st_size != completed.st_size
            or opened.st_mtime_ns != completed.st_mtime_ns
            or opened.st_size != len(data)
        ):
            raise LocalDiagnosticError(
                "source checkout changed while it was snapshotted"
            )
        copied_files += 1
        copied_bytes += len(data)
        if copied_files > _MAX_SOURCE_FILES or copied_bytes > _MAX_SOURCE_TOTAL_BYTES:
            raise LocalDiagnosticError("source checkout is too large to snapshot")
        if any(needle in data for needle in secret_needles):
            raise LocalDiagnosticError(
                "source checkout contains loaded credential material"
            )
        _write_private_bytes(target_path, data)
        if metadata.st_mode & stat.S_IXUSR:
            target_path.chmod(0o700)

    for relative in _SOURCE_SNAPSHOT_ALLOWLIST:
        ensure_target_directory(relative.parent)
        copy_path(source / relative, target / relative)


def install_local_codex_auth(auth_file: Path, codex_home: Path) -> tuple[str, ...]:
    source = auth_file.expanduser()
    if not source.is_absolute():
        source = Path.cwd() / source
    raw = _owned_private_file(source, "local Codex auth", max_bytes=_MAX_AUTH_BYTES)
    try:
        _destination, mask_values = install_codex_auth.install_auth(
            codex_home, base64.b64encode(raw)
        )
    except install_codex_auth.CodexAuthInstallError as exc:
        raise LocalDiagnosticError(str(exc)) from None
    return mask_values


def _verify_login(
    codex_bin: Path, codex_home: Path, home: Path, temporary: Path
) -> None:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "CODEX_HOME": str(codex_home),
    }
    try:
        result = subprocess.run(
            [str(codex_bin), "login", "status"],
            cwd=home,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise LocalDiagnosticError("unable to verify local Codex login") from None
    if result.returncode != 0:
        raise LocalDiagnosticError("local Codex login is not active")


def _create_private_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)
    except OSError:
        raise LocalDiagnosticError(
            "unable to create private preflight evidence"
        ) from None


def _preflight_events_have_no_tools(path: Path) -> bool:
    raw = _owned_private_file(
        path, "Codex preflight event stream", max_bytes=8 * 1024 * 1024
    )
    try:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        if row.get("type") not in ("item.started", "item.completed"):
            continue
        item = row.get("item")
        if not isinstance(item, Mapping) or item.get("type") not in (
            "agent_message",
            "reasoning",
        ):
            return False
    return True


def _verify_worker_runtime(
    options: DiagnosticOptions,
    paths: RunPaths,
    *,
    profile_id: str,
    agent_type: str,
) -> None:
    """Prove the selected Python and crypto helpers execute in the real sandbox."""

    worker_python, _runtime_roots = _canonical_worker_runtime(options)
    agent_root = paths.worker_root / agent_type
    work = agent_root / "work"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "HOME": str(agent_root / "home"),
        "TMPDIR": str(agent_root / "tmp"),
        "CODEX_HOME": str(paths.codex_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(paths.worker_source_root),
        "QA_PYTHON_BIN": str(worker_python),
        "QA_QUALIFICATION_MODE": "diagnostic",
        "QA_SOURCE_ROOT": str(paths.worker_source_root),
    }
    command = (
        str(options.codex_bin),
        "sandbox",
        "-p",
        agent_type,
        "-P",
        write_codex_config.worker_permission_profile(profile_id),
        "--include-managed-config",
        "-C",
        str(work),
        "--",
        "/bin/sh",
        "-eu",
        "-c",
        'test "$QA_PYTHON_BIN" = "$1"; exec "$QA_PYTHON_BIN" -I -B "$2" --help',
        "feedling-worker-runtime-preflight",
        str(worker_python),
        str(paths.worker_source_root / "qa" / "cot_delivery_probe.py"),
    )
    try:
        result = subprocess.run(
            command,
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            timeout=60,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise LocalDiagnosticError(
            "qualification Python sandbox preflight could not execute"
        ) from None
    if result.returncode != 0:
        raise LocalDiagnosticError(
            "qualification Python cannot load the COT probe inside the worker sandbox"
        )


def _verify_worker_network_disabled(
    options: DiagnosticOptions,
    paths: RunPaths,
    *,
    profile_id: str,
    agent_type: str,
) -> None:
    """Prove the real worker sandbox cannot use tool or shell networking.

    Codex's authenticated model transport is outside this sandbox boundary.  A
    profile worker therefore remains able to reason over parent-authored facts
    while every network attempt made by a model-controlled tool must fail.
    """

    worker_python, _runtime_roots = _canonical_worker_runtime(options)
    agent_root = paths.worker_root / agent_type
    work = agent_root / "work"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "HOME": str(agent_root / "home"),
        "TMPDIR": str(agent_root / "tmp"),
        "CODEX_HOME": str(paths.codex_home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = """\
import sys
import urllib.error
import urllib.request

for url in sys.argv[1:]:
    try:
        response = urllib.request.urlopen(
            urllib.request.Request(
                url,
                headers={"User-Agent": "io-e2e-agent-driven-test-network-preflight"},
            ),
            timeout=10,
        )
    except urllib.error.HTTPError:
        raise SystemExit("worker tool network unexpectedly enabled")
    except Exception:
        continue
    try:
        response.read(1)
    finally:
        response.close()
    raise SystemExit("worker tool network unexpectedly enabled")
"""
    command = (
        str(options.codex_bin),
        "sandbox",
        "-p",
        agent_type,
        "-P",
        write_codex_config.worker_permission_profile(profile_id),
        "--include-managed-config",
        "-C",
        str(work),
        "--",
        str(worker_python),
        "-I",
        "-B",
        "-c",
        script,
        f"{LOCKED_BASE_URL}/healthz",
        "https://example.com/",
    )
    try:
        result = subprocess.run(
            command,
            cwd=work,
            env=environment,
            check=False,
            capture_output=True,
            timeout=45,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise LocalDiagnosticError(
            "worker tool-network isolation preflight could not execute"
        ) from None
    if result.returncode != 0:
        raise LocalDiagnosticError(
            "worker tool network isolation is not enforced"
        )


def _verify_codex_exec(options: DiagnosticOptions, paths: RunPaths) -> dict[str, Any]:
    """Prove the isolated OAuth/model can complete one tool-free headless turn."""

    profile_id = options.profile_ids[0]
    agent_types = dict(PROFILE_AGENT_TYPES)
    agent_type = agent_types[profile_id]
    agent_root = paths.worker_root / agent_type
    home = agent_root / "home"
    temporary = agent_root / "tmp"
    work = agent_root / "work"
    _verify_worker_runtime(
        options,
        paths,
        profile_id=profile_id,
        agent_type=agent_type,
    )
    _verify_worker_network_disabled(
        options,
        paths,
        profile_id=profile_id,
        agent_type=agent_type,
    )
    preflight_root = _make_private_directory(paths.private_root / "codex-preflight")
    schema_path = preflight_root / "schema.json"
    result_path = preflight_root / "result.json"
    events_path = preflight_root / "events.jsonl"
    stderr_path = preflight_root / "stderr.log"
    _write_private_json(
        schema_path,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "profile_id"],
            "properties": {
                "ok": {"type": "boolean", "enum": [True]},
                "profile_id": {"type": "string", "enum": [profile_id]},
            },
        },
    )
    for path in (result_path, events_path, stderr_path):
        _create_private_file(path)

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "CODEX_HOME": str(paths.codex_home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = (
        str(options.codex_bin),
        "exec",
        "-p",
        agent_type,
        "-c",
        f'default_permissions="{write_codex_config.worker_permission_profile(profile_id)}"',
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
    prompt = (
        f'Return only {{"ok":true,"profile_id":"{profile_id}"}}. '
        "Do not call any tool or access any external endpoint."
    )
    try:
        with (
            events_path.open("wb", buffering=0) as stdout_handle,
            stderr_path.open("wb", buffering=0) as stderr_handle,
        ):
            result = subprocess.run(
                command,
                cwd=work,
                env=environment,
                input=prompt,
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                timeout=180,
                start_new_session=True,
            )
    except (OSError, subprocess.SubprocessError):
        raise LocalDiagnosticError(
            "headless Codex preflight could not execute"
        ) from None
    if result.returncode != 0:
        raise LocalDiagnosticError("headless Codex preflight did not complete")
    if _load_private_json(result_path, "Codex preflight result") != {
        "ok": True,
        "profile_id": profile_id,
    }:
        raise LocalDiagnosticError("headless Codex preflight result is invalid")
    try:
        run_codex_profile_workers.parse_exec_events(events_path)
    except Exception:
        raise LocalDiagnosticError(
            "headless Codex preflight event stream is invalid"
        ) from None
    if not _preflight_events_have_no_tools(events_path):
        raise LocalDiagnosticError("headless Codex preflight attempted a tool")
    try:
        if any(any(root.iterdir()) for root in (home, temporary, work)):
            raise LocalDiagnosticError(
                "headless Codex preflight polluted an isolated root"
            )
    except OSError:
        raise LocalDiagnosticError(
            "headless Codex preflight root is unreadable"
        ) from None
    return {
        "headless_exec_completed": True,
        "structured_output_valid": True,
        "event_stream_valid": True,
        "tool_calls_observed": False,
        "worker_runtime_valid": True,
        "worker_tool_network_disabled": True,
        "profile_id": profile_id,
        "model": options.codex_model,
    }


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise LocalDiagnosticError(
            "unable to create diagnostic JSON artifact"
        ) from None


def _observe_public_health_deployment(
    expected_sha: str | None,
    receipt_path: Path,
    *,
    env: Mapping[str, str],
    expected_runtime: str,
    client: Any | None = None,
) -> dict[str, Any]:
    """Pin a local diagnostic to the image SHA reported by public healthz.

    This is deliberately weaker than the protected QA build-identity receipt:
    it observes image-baked backend metadata but cannot independently prove the
    serialized deployment SHA or worker identity.  The protected workflow never
    calls this helper.
    """

    base_url = provision_profiles.validate_base_url(
        str(env.get("IO_E2E_BASE_URL") or "")
    )
    if base_url != LOCKED_BASE_URL:
        raise LocalDiagnosticError("public deployment observation target is invalid")
    if expected_runtime not in {BASELINE_RUNTIME, RUNTIME_V2_RUNTIME}:
        raise LocalDiagnosticError("runtime requirement is invalid")
    expected = str(expected_sha or "").strip().lower()
    if expected and not _SHA_RE.fullmatch(expected):
        raise LocalDiagnosticError("candidate SHA must be 40 or 64 lowercase hex characters")

    active_client = client or provision_profiles.SmokeClient(base_url)
    try:
        status, body = active_client._req(  # noqa: SLF001 - fixed local QA transport
            "GET", "/healthz", attempts=3, read_timeout=15
        )
    except Exception:
        raise LocalDiagnosticError(
            "deployed test health endpoint was unreachable"
        ) from None
    release = body.get("release") if isinstance(body, Mapping) else None
    observed = (
        str(release.get("git_commit") or "").strip().lower()
        if isinstance(release, Mapping)
        else ""
    )
    health_status = str(body.get("status") or "") if isinstance(body, Mapping) else ""
    if (
        status != 200
        or not isinstance(body, Mapping)
        or body.get("ok") is not True
        or health_status not in {"healthy", "degraded"}
        or not _SHA_RE.fullmatch(observed)
    ):
        raise LocalDiagnosticError("deployed test health identity is invalid")
    if expected and expected != observed:
        raise LocalDiagnosticError(
            "deployed backend health identity does not match the candidate"
        )

    receipt = {
        "schema_version": 1,
        "environment": "test",
        "base_url": base_url,
        "expected_runtime": expected_runtime,
        "expected_deployment_sha": observed,
        "observed_backend_sha": observed,
        "observed_deployment_sha": None,
        "observed_worker_sha": None,
        "live_worker_count": None,
        "runtime_evidence_source": (
            "per_profile_runtime_readback_and_live_scenarios"
            if expected_runtime == RUNTIME_V2_RUNTIME
            else "deployed_runtime_readback"
        ),
        "liveness_verified": True,
        "deployment_identity_verified": False,
        "deployment_identity_observed": True,
        "identity_evidence_source": PUBLIC_HEALTH_IDENTITY_SOURCE,
        "identity_gap_code": PUBLIC_HEALTH_IDENTITY_GAP,
        "health_status": health_status,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_private_json(receipt_path, receipt)
    try:
        receipt_path.chmod(0o400)
    except OSError:
        raise LocalDiagnosticError(
            "deployment observation receipt could not be protected"
        ) from None
    return receipt


def _write_private_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise LocalDiagnosticError("unable to create private debug evidence") from None


def _load_private_json(path: Path, label: str) -> dict[str, Any]:
    raw = _owned_private_file(path, label, max_bytes=32 * 1024 * 1024)
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LocalDiagnosticError(f"{label} is invalid") from None
    if not isinstance(payload, dict):
        raise LocalDiagnosticError(f"{label} is invalid")
    return payload


def _contains_secret(payload: Any, secret_values: Sequence[str]) -> bool:
    try:
        data = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return True
    return _public_data_is_unsafe(data, secret_values)


def _secret_needles(secret_values: Sequence[str]) -> set[bytes]:
    needles: set[bytes] = set()
    for value in secret_values:
        if not value:
            continue
        raw = value.encode("utf-8")
        candidates = {raw}
        compact = b"".join(raw.split())
        try:
            decoded = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error):
            decoded = b""
        if len(decoded) >= 8:
            candidates.add(decoded)
        for candidate in candidates:
            if len(candidate) < 8:
                continue
            needles.update(
                (
                    candidate,
                    base64.b64encode(candidate),
                    base64.urlsafe_b64encode(candidate),
                    candidate.hex().encode("ascii"),
                )
            )
    return needles


def _json_fragment_streams(data: bytes) -> tuple[tuple[bytes, ...], ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return ()
    values: list[bytes] = []
    keys: list[bytes] = []
    tokens: list[bytes] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="surrogatepass")
            values.append(encoded)
            tokens.append(encoded)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                encoded = str(key).encode("utf-8", errors="surrogatepass")
                keys.append(encoded)
                tokens.append(encoded)
                visit(item)

    try:
        visit(document)
    except RecursionError:
        return ()
    return tuple(
        stream for stream in (tuple(values), tuple(keys), tuple(tokens)) if stream
    )


def _reconstructs_secret(parts: tuple[bytes, ...], secret: bytes) -> bool:
    offset = 0
    fragments = 0
    minimum_fragment_bytes = 8
    max_fragments = (len(secret) + minimum_fragment_bytes - 1) // minimum_fragment_bytes + 1
    for part in parts:
        if not part or offset == len(secret):
            continue
        remaining = secret[offset:]
        low = 0
        high = len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if remaining[:middle] in part:
                low = middle
            else:
                high = middle - 1
        is_short_final_tail = (
            fragments > 0 and low == len(remaining) and 0 < low < minimum_fragment_bytes
        )
        if low < minimum_fragment_bytes and not is_short_final_tail:
            continue
        fragments += 1
        if fragments > max_fragments:
            return False
        offset += low
        if offset == len(secret):
            return True
    return False


def _public_data_is_unsafe(data: bytes, secret_values: Sequence[str]) -> bool:
    needles = _secret_needles(secret_values)
    fragment_streams = _json_fragment_streams(data)
    return bool(
        _FORBIDDEN_PUBLIC_JSON_KEY.search(data)
        or _CREDENTIAL_SIGNATURE.search(data)
        or any(needle in data for needle in needles)
        or any(
            _reconstructs_secret(stream, needle)
            for stream in fragment_streams
            for needle in needles
        )
    )


def _scan_public_artifacts(
    artifact_root: Path, secret_values: Sequence[str], profile_ids: Sequence[str]
) -> None:
    allowed = {
        "diagnostic-summary.json",
        "matrix.md",
        "latency.csv",
        "junit.xml",
        *(f"profiles/{profile_id}.json" for profile_id in profile_ids),
    }
    try:
        paths = list(artifact_root.rglob("*"))
    except OSError:
        raise LocalDiagnosticError("unable to scan diagnostic artifacts") from None
    files = [path for path in paths if not path.is_dir()]
    if len(files) > _MAX_PUBLIC_FILES:
        raise LocalDiagnosticError("diagnostic artifact boundary is invalid")
    for path in paths:
        if path.is_symlink():
            raise LocalDiagnosticError("diagnostic artifact boundary is unsafe")
        relative = path.relative_to(artifact_root).as_posix()
        if path.is_dir():
            if relative != "profiles":
                raise LocalDiagnosticError("diagnostic artifact boundary is invalid")
            continue
        if relative not in allowed:
            raise LocalDiagnosticError("diagnostic artifact boundary is invalid")
        try:
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > _MAX_PUBLIC_FILE_BYTES
            ):
                raise LocalDiagnosticError("diagnostic artifact boundary is unsafe")
            data = path.read_bytes()
        except LocalDiagnosticError:
            raise
        except OSError:
            raise LocalDiagnosticError("unable to scan diagnostic artifacts") from None
        if _public_data_is_unsafe(data, secret_values):
            raise LocalDiagnosticError("diagnostic artifact contains secret material")


def _scrub_sensitive_material(root: Path, secret_values: Sequence[str]) -> None:
    """Remove files containing known credentials before private retention."""

    try:
        candidates = list(root.rglob("*")) if root.exists() else []
    except OSError:
        raise LocalDiagnosticError("private debug root is unreadable") from None
    for path in candidates:
        try:
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            if path.is_dir():
                path.chmod(0o700)
                continue
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size > _MAX_DEBUG_FILE_BYTES
            ):
                path.unlink(missing_ok=True)
                continue
            data = path.read_bytes()
            if _public_data_is_unsafe(data, secret_values):
                path.unlink(missing_ok=True)
            else:
                path.chmod(0o600)
        except OSError:
            raise LocalDiagnosticError("private credential scrub failed") from None


def _retain_cleanup_retry_manifest(paths: RunPaths) -> None:
    """Delete all private run data except the owner-only cleanup manifest."""

    _owned_private_file(
        paths.manifest,
        "cleanup retry manifest",
        max_bytes=32 * 1024 * 1024,
    )
    try:
        root_metadata = paths.private_root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
        ):
            raise LocalDiagnosticError("cleanup retry root is unsafe")
        for child in tuple(paths.private_root.iterdir()):
            if child == paths.manifest:
                continue
            child_metadata = child.lstat()
            if stat.S_ISDIR(child_metadata.st_mode):
                shutil.rmtree(child)
            else:
                child.unlink()
        paths.private_root.chmod(0o700)
        paths.manifest.chmod(0o600)
        if set(paths.private_root.iterdir()) != {paths.manifest}:
            raise LocalDiagnosticError("cleanup retry root contains extra material")
        _owned_private_file(
            paths.manifest,
            "cleanup retry manifest",
            max_bytes=32 * 1024 * 1024,
        )
    except LocalDiagnosticError:
        raise
    except OSError:
        raise LocalDiagnosticError(
            "cleanup retry material could not be minimized"
        ) from None


def _is_debug_quarantine_noise(relative: Path) -> bool:
    return (
        any(part in _DEBUG_QUARANTINE_EXCLUDED_DIRECTORIES for part in relative.parts)
        or relative.suffix.lower() in _DEBUG_QUARANTINE_EXCLUDED_SUFFIXES
    )


def _retain_debug_quarantine(
    options: DiagnosticOptions,
    paths: RunPaths,
    secret_values: Sequence[str],
) -> Path:
    """Copy bounded worker diagnostics without manifests or known credentials."""

    parent = options.private_base.parent / "io-e2e-agent-driven-test-debug"
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
        ):
            raise LocalDiagnosticError("private debug parent is unsafe")
        parent.chmod(0o700)
    except LocalDiagnosticError:
        raise
    except OSError:
        raise LocalDiagnosticError("private debug parent is unsafe") from None
    destination = _make_private_directory(parent / paths.run_id)
    sources = (
        ("worker-outputs", paths.worker_outputs),
        ("worker-scratch", paths.worker_root),
        ("codex-sessions", paths.codex_home / "sessions"),
    )
    copied_files = 0
    copied_bytes = 0
    for label, source in sources:
        if not source.exists() or source.is_symlink():
            continue
        target_root = _make_private_directory(destination / label)
        try:
            candidates = sorted(source.rglob("*"))
        except OSError:
            raise LocalDiagnosticError("private debug source is unreadable") from None
        for candidate in candidates:
            try:
                relative = candidate.relative_to(source)
                if _is_debug_quarantine_noise(relative):
                    continue
                target = target_root / relative
                if candidate.is_symlink():
                    continue
                if candidate.is_dir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target.chmod(0o700)
                    continue
                try:
                    data = _owned_private_file(
                        candidate,
                        "private debug evidence",
                        max_bytes=_MAX_DEBUG_FILE_BYTES,
                    )
                except LocalDiagnosticError:
                    continue
                if (
                    copied_files >= _MAX_DEBUG_FILES
                    or copied_bytes + len(data) > _MAX_DEBUG_TOTAL_BYTES
                ):
                    raise LocalDiagnosticError("private debug evidence is too large")
                if _public_data_is_unsafe(data, secret_values):
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.parent.chmod(0o700)
                _write_private_bytes(target, data)
                copied_files += 1
                copied_bytes += len(data)
            except LocalDiagnosticError:
                raise
            except OSError:
                raise LocalDiagnosticError(
                    "private debug evidence could not be copied"
                ) from None
    _write_private_json(
        destination / "debug-manifest.json",
        {
            "schema_version": 1,
            "run_id": paths.run_id,
            "release_qualified": False,
            "contains_raw_worker_evidence": copied_files > 0,
            "oauth_material_retained": False,
            "credential_material_retained": False,
            "provisioning_manifest_retained": False,
            "copied_file_count": copied_files,
            "copied_bytes": copied_bytes,
        },
    )
    _scrub_sensitive_material(destination, secret_values)
    return destination


def _split_selected_manifest(
    manifest_path: Path, output_dir: Path, profile_ids: Sequence[str]
) -> None:
    payload = _load_private_json(manifest_path, "diagnostic provisioning manifest")
    rows = payload.get("profiles")
    if not isinstance(rows, list):
        raise LocalDiagnosticError("diagnostic provisioning manifest has no profiles")
    by_id = {
        str(row.get("profile_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("profile_id")
    }
    if tuple(by_id) != tuple(profile_ids) or any(output_dir.iterdir()):
        raise LocalDiagnosticError("diagnostic provisioning profile matrix is invalid")
    for profile_id in profile_ids:
        isolated = dict(payload)
        isolated["profiles"] = [by_id[profile_id]]
        isolated["selected_profile_ids"] = [profile_id]
        _write_private_json(output_dir / f"{profile_id}.json", isolated)


def _validate_diagnostic_manifest_structure(
    manifest: Mapping[str, Any],
    profile_ids: Sequence[str],
    runtime_requirement: str,
) -> tuple[Mapping[str, Any], ...]:
    """Validate the exact selected account rows before semantic classification.

    Once this succeeds, the parent has enough trustworthy account bindings to
    require and credit cleanup for every selected row, even if a later READY or
    BLOCKED evidence check rejects the manifest.
    """

    rows = manifest.get("profiles")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("qualification_mode") != "diagnostic"
        or manifest.get("runtime_mode") != runtime_requirement
        or manifest.get("runtime_requirement") != runtime_requirement
        or manifest.get("selected_profile_ids") != list(profile_ids)
        or not isinstance(rows, list)
        or len(rows) != len(profile_ids)
        or not all(isinstance(row, Mapping) for row in rows)
        or [row.get("profile_id") for row in rows] != list(profile_ids)
    ):
        raise LocalDiagnosticError(
            "provisioner returned an invalid diagnostic manifest"
        )

    typed_rows = tuple(row for row in rows if isinstance(row, Mapping))
    unique_bindings: dict[str, set[str]] = {
        field: set()
        for field in ("user_id", "api_key", "secret_key_b64", "public_key_b64")
    }
    for profile_id, row in zip(profile_ids, typed_rows, strict=True):
        spec = provision_profiles.PROFILE_SPECS[profile_id]
        model = row.get("configured_model")
        if (
            row.get("qualification_mode") != "diagnostic"
            or row.get("profile_id") != profile_id
            or row.get("provider") != spec.provider
            or row.get("route_family") != spec.route_family
            or not isinstance(model, str)
            or re.fullmatch(spec.allowed_model_regex, model) is None
            or row.get("configured_base_url")
            != spec.expected_configured_base_url
            or row.get("reasoning_effort") != "medium"
            or row.get("runtime_mode_set_required") is not False
            or row.get("runtime_mode_set_verified") is not False
            or type(row.get("trace_enabled")) is not bool
            or not all(
                isinstance(row.get(field), str) and bool(row.get(field))
                for field in (
                    "label",
                    "user_id",
                    "api_key",
                    "secret_key_b64",
                    "public_key_b64",
                )
            )
        ):
            raise LocalDiagnosticError(
                "provisioner returned an invalid diagnostic profile binding"
            )
        for field, observed in unique_bindings.items():
            value = str(row[field])
            if value in observed:
                raise LocalDiagnosticError(
                    "provisioner returned duplicate diagnostic profile bindings"
                )
            observed.add(value)
    return typed_rows


def _classify_diagnostic_manifest(
    rows: Sequence[Mapping[str, Any]],
    profile_ids: Sequence[str],
    runtime_requirement: str,
) -> DiagnosticManifestClassification:
    """Return an ordered, fail-closed READY/BLOCKED matrix classification."""

    if len(rows) != len(profile_ids):
        raise LocalDiagnosticError("diagnostic provisioning classification is invalid")

    ready: list[str] = []
    blocked: list[str] = []
    statuses: list[tuple[str, str]] = []
    failure_codes: list[tuple[str, str]] = []
    for profile_id, row in zip(profile_ids, rows, strict=True):
        if row.get("profile_id") != profile_id:
            raise LocalDiagnosticError(
                "diagnostic provisioning classification is invalid"
            )
        status = row.get("provision_status")
        failure_code = row.get("provision_failure_code")
        runtime_mode = row.get("runtime_mode")
        runtime_version = row.get("runtime_version")
        runtime_receipt = row.get("runtime_readback_receipt")
        runtime_readback_valid = (
            row.get("runtime_mode_readback_verified") is True
            and isinstance(runtime_mode, str)
            and bool(runtime_mode)
            and type(runtime_version) is int
            and runtime_version >= 1
            and runtime_receipt
            == {
                "configured": True,
                "runtime_mode": runtime_mode,
                "runtime_version": runtime_version,
            }
        )
        if status == provision_profiles.PROVISION_STATUS_READY:
            runtime_requirement_valid = (
                runtime_requirement != RUNTIME_V2_RUNTIME
                or (
                    runtime_mode == RUNTIME_V2_RUNTIME
                    and runtime_version == 2
                )
            )
            if (
                failure_code != provision_profiles.PROVISION_FAILURE_NONE
                or not runtime_readback_valid
                or not runtime_requirement_valid
            ):
                raise LocalDiagnosticError(
                    "diagnostic READY profile runtime evidence is invalid"
                )
            ready.append(profile_id)
        elif status == provision_profiles.PROVISION_STATUS_BLOCKED:
            invalid_key_receipt = row.get("invalid_key_receipt")
            provider_status_code = (
                invalid_key_receipt.get("provider_status_code")
                if isinstance(invalid_key_receipt, Mapping)
                else None
            )
            invalid_key_receipt_valid = (
                isinstance(invalid_key_receipt, Mapping)
                and set(invalid_key_receipt)
                == {"http_status", "error", "provider_status_code"}
                and type(invalid_key_receipt.get("http_status")) is int
                and invalid_key_receipt.get("http_status") == 400
                and invalid_key_receipt.get("error") == "provider_test_failed"
                and type(provider_status_code) is int
                and provider_status_code in {400, 401, 403}
            )
            if (
                failure_code != "VALID_KEY_REJECTED"
                or row.get("qualification_mode") != "diagnostic"
                or row.get("registration_verified") is not True
                or row.get("fresh_state_verified") is not True
                or row.get("invalid_key_rejected") is not True
                or not invalid_key_receipt_valid
                or row.get("valid_key_configured") is not False
                or row.get("valid_key_receipt") is not None
                or row.get("trace_enabled") is not False
                or runtime_mode != ""
                or type(runtime_version) is not int
                or runtime_version != 0
                or row.get("runtime_mode_set_required") is not False
                or row.get("runtime_mode_set_verified") is not False
                or row.get("runtime_mode_readback_verified") is not False
                or runtime_receipt is not None
            ):
                raise LocalDiagnosticError(
                    "diagnostic BLOCKED profile evidence is invalid"
                )
            blocked.append(profile_id)
        else:
            raise LocalDiagnosticError(
                "diagnostic provisioning classification is invalid"
            )
        statuses.append((profile_id, str(status)))
        failure_codes.append((profile_id, str(failure_code)))

    return DiagnosticManifestClassification(
        ready_profile_ids=tuple(ready),
        blocked_profile_ids=tuple(blocked),
        provision_statuses=tuple(statuses),
        provision_failure_codes=tuple(failure_codes),
    )


def _uses_benchmark_fake_ip(host: str) -> bool:
    """Return true only when every resolved address is in RFC 2544 test space."""

    try:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for row in rows:
        try:
            addresses.append(ipaddress.ip_address(row[4][0]))
        except (IndexError, TypeError, ValueError):
            return False
    return bool(addresses) and all(
        isinstance(address, ipaddress.IPv4Address)
        and address in _BENCHMARK_FAKE_IP_NETWORK
        for address in addresses
    )


def _build_codex_config(options: DiagnosticOptions, paths: RunPaths) -> bool:
    worker_python, runtime_roots = _canonical_worker_runtime(options)
    benchmark_fake_ip_detected = _uses_benchmark_fake_ip("test-api.feedling.app")
    bundle = write_codex_config.build_config_bundle(
        output=paths.codex_home / "config.toml",
        source_root=paths.worker_source_root,
        artifact_root=paths.artifact_root,
        full_manifest=paths.manifest,
        profile_manifest_dir=paths.profile_manifests,
        supervisor_home=paths.supervisor_home,
        supervisor_tmp=paths.supervisor_tmp,
        supervisor_work=paths.supervisor_work,
        worker_root=paths.worker_root,
        worker_output_root=paths.worker_outputs,
        aggregation_input_root=paths.aggregation_inputs,
        orchestration_receipt=paths.orchestration_receipt,
        codex_model=options.codex_model,
        allowed_host="test-api.feedling.app",
        runtime_read_roots=runtime_roots,
        worker_python=worker_python,
        qualification_mode="diagnostic",
        allow_local_binding=False,
    )
    write_codex_config.write_bundle(paths.codex_home / "config.toml", bundle)
    return benchmark_fake_ip_detected


def _provision_diagnostic(
    dependencies: DiagnosticDependencies,
    options: DiagnosticOptions,
    paths: RunPaths,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Small adapter isolating the diagnostic provisioner interface."""

    return dependencies.provision(
        paths.worker_source_root / "qa" / "coverage-lock.json",
        paths.manifest,
        env=env,
        diagnostic=True,
        profile_ids=options.profile_ids,
        runtime_requirement=options.runtime_requirement,
    )


def _launch_diagnostic(
    dependencies: DiagnosticDependencies,
    options: DiagnosticOptions,
    paths: RunPaths,
    *,
    profile_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    worker_python, _runtime_roots = _canonical_worker_runtime(options)
    selected = tuple(profile_ids) if profile_ids is not None else options.profile_ids
    return dependencies.launch(
        codex_bin=options.codex_bin,
        codex_home=paths.codex_home,
        source_root=paths.worker_source_root,
        artifact_root=paths.artifact_root,
        profile_manifest_dir=paths.profile_manifests,
        worker_root=paths.worker_root,
        worker_output_root=paths.worker_outputs,
        aggregation_input_root=paths.aggregation_inputs,
        authoring_schema_path=(
            paths.worker_source_root / "qa" / "schemas" / "codex-run-result.schema.json"
        ),
        receipt_path=paths.orchestration_receipt,
        run_id=paths.run_id,
        base_url=LOCKED_BASE_URL,
        expected_sha=options.candidate_sha,
        timeout_seconds=2400,
        diagnostic=True,
        profile_ids=selected,
        expected_runtime=options.runtime_requirement,
        worker_python=worker_python,
    )


def _blocked_profile_result(
    manifest_profile: Mapping[str, Any],
    *,
    profile_id: str,
    expected_runtime: str,
    provisioning_failure_code: str,
    authoring_schema_path: Path,
) -> dict[str, Any]:
    """Create and schema-check one parent-owned blocked profile artifact."""

    try:
        authoring = json.loads(authoring_schema_path.read_text(encoding="utf-8"))
        if not isinstance(authoring, dict):
            raise ValueError
        schema = run_codex_profile_workers.build_profile_schema(
            authoring, profile_id, expected_runtime
        )
        result = diagnostic_results.blocked_provision_profile(
            manifest_profile,
            profile_id=profile_id,
            expected_runtime=expected_runtime,
            provisioning_failure_code=provisioning_failure_code,
        )
        errors = list(Draft202012Validator(schema).iter_errors(result))
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
        diagnostic_results.DiagnosticResultError,
        run_codex_profile_workers.WorkerLaunchError,
    ):
        raise LocalDiagnosticError(
            "blocked diagnostic profile artifact is invalid"
        ) from None
    if errors:
        raise LocalDiagnosticError("blocked diagnostic profile artifact is invalid")
    return result


def _blocked_cot_projection(provision_failure_code: str) -> dict[str, Any]:
    """Return explicit NOT_RUN COT evidence for a profile with no agent launch."""

    return {
        "status": "NOT_RUN",
        "failure_code": provision_failure_code,
        "receipt_status": None,
        "receipt_failure_code": None,
        "delivery_qualified": False,
        "final_answer_correct": None,
        "reasoning_event_count": None,
        "metadata_present": None,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
        "user_visible_disclosure_present": None,
        "receipt_sha256": None,
    }


def _cot_delivery_projection(
    receipt: Mapping[str, Any], profile_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Project only fixed, sanitized COT facts from the trusted launcher receipt."""

    def trusted_fields(row: Mapping[str, Any]) -> dict[str, Any] | None:
        digest = row.get("cot_receipt_sha256")
        status = row.get("cot_delivery_status")
        failure_code = row.get("cot_failure_code")
        if digest is None and status is None and failure_code is None:
            return None
        token_status = row.get("cot_token_metadata_status")
        reasoning_token_count = row.get("cot_reasoning_token_count")
        token_evidence_valid = (
            token_status == "UNVERIFIED" and reasoning_token_count is None
        ) or (
            token_status == "PRESENT"
            and isinstance(reasoning_token_count, int)
            and not isinstance(reasoning_token_count, bool)
            and reasoning_token_count >= 0
        )
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or status not in {"PASS", "FAIL", "UNVERIFIED"}
            or not isinstance(failure_code, str)
            or not token_evidence_valid
            or not isinstance(row.get("cot_delivery_qualified"), bool)
            or not isinstance(row.get("cot_final_answer_correct"), bool)
            or not isinstance(row.get("cot_reasoning_event_count"), int)
            or isinstance(row.get("cot_reasoning_event_count"), bool)
            or row.get("cot_reasoning_event_count") not in {0, 1}
            or not isinstance(row.get("cot_metadata_present"), bool)
            or not isinstance(row.get("cot_user_visible_disclosure_present"), bool)
        ):
            raise LocalDiagnosticError("diagnostic COT receipt evidence is invalid")
        return {
            "receipt_status": status,
            "receipt_failure_code": failure_code,
            "delivery_qualified": row["cot_delivery_qualified"],
            "final_answer_correct": row["cot_final_answer_correct"],
            "reasoning_event_count": row["cot_reasoning_event_count"],
            "metadata_present": row["cot_metadata_present"],
            "token_metadata_status": token_status,
            "reasoning_token_count": reasoning_token_count,
            "user_visible_disclosure_present": row[
                "cot_user_visible_disclosure_present"
            ],
            "receipt_sha256": digest,
        }

    workers = receipt.get("workers")
    if not isinstance(workers, list) or len(workers) != len(profile_ids):
        raise LocalDiagnosticError("diagnostic COT receipt matrix is invalid")
    by_id = {
        str(row.get("profile_id")): row
        for row in workers
        if isinstance(row, Mapping) and row.get("profile_id")
    }
    if tuple(by_id) != tuple(profile_ids):
        raise LocalDiagnosticError("diagnostic COT receipt matrix is invalid")

    projected: dict[str, dict[str, Any]] = {}
    for profile_id in profile_ids:
        row = by_id[profile_id]
        if row.get("result_source") != "codex_worker":
            trusted = trusted_fields(row)
            projected[profile_id] = {
                "status": "NOT_RUN",
                "failure_code": str(
                    row.get("fallback_reason") or "WORKER_RESULT_INVALID"
                ),
                **(
                    trusted
                    or {
                        "receipt_status": None,
                        "receipt_failure_code": None,
                        "delivery_qualified": False,
                        "final_answer_correct": None,
                        "reasoning_event_count": None,
                        "metadata_present": None,
                        "token_metadata_status": "UNVERIFIED",
                        "reasoning_token_count": None,
                        "user_visible_disclosure_present": None,
                        "receipt_sha256": None,
                    }
                ),
            }
            continue

        cot_evidence_failure = row.get("cot_evidence_failure")
        if cot_evidence_failure in {"COT_RECEIPT_MISSING", "COT_RECEIPT_INVALID"}:
            projected[profile_id] = {
                "status": (
                    "NOT_RUN"
                    if cot_evidence_failure == "COT_RECEIPT_MISSING"
                    else "FAIL"
                ),
                "failure_code": cot_evidence_failure,
                "receipt_status": None,
                "receipt_failure_code": None,
                "delivery_qualified": False,
                "final_answer_correct": None,
                "reasoning_event_count": None,
                "metadata_present": None,
                "token_metadata_status": "UNVERIFIED",
                "reasoning_token_count": None,
                "user_visible_disclosure_present": None,
                "receipt_sha256": None,
            }
            continue
        binding_mismatch = cot_evidence_failure == "COT_RESULT_BINDING_MISMATCH"
        if cot_evidence_failure is not None and not binding_mismatch:
            raise LocalDiagnosticError("diagnostic COT failure code is invalid")

        trusted = trusted_fields(row)
        if trusted is None:
            raise LocalDiagnosticError("diagnostic COT receipt evidence is invalid")
        receipt_status = trusted["receipt_status"]
        receipt_failure_code = trusted["receipt_failure_code"]
        missing_tokens = receipt_status == "PASS" and not (
            trusted["token_metadata_status"] == "PRESENT"
            and isinstance(trusted["reasoning_token_count"], int)
            and not isinstance(trusted["reasoning_token_count"], bool)
            and trusted["reasoning_token_count"] > 0
        )
        projected[profile_id] = {
            "status": "FAIL" if binding_mismatch or missing_tokens else receipt_status,
            "failure_code": (
                "COT_RESULT_BINDING_MISMATCH"
                if binding_mismatch
                else "REASONING_TOKENS_MISSING"
                if missing_tokens
                else receipt_failure_code
            ),
            **trusted,
        }
    return projected


def _cot_delivery_passes(
    projection: Mapping[str, Mapping[str, Any]], profile_ids: Sequence[str]
) -> bool:
    """Return true only for trusted, end-to-end reasoning delivery evidence."""

    return tuple(projection) == tuple(profile_ids) and all(
        projection[profile_id].get("status") == "PASS"
        and projection[profile_id].get("failure_code") == "NONE"
        and projection[profile_id].get("delivery_qualified") is True
        and projection[profile_id].get("final_answer_correct") is True
        and projection[profile_id].get("reasoning_event_count") == 1
        and projection[profile_id].get("metadata_present") is True
        and projection[profile_id].get("token_metadata_status") == "PRESENT"
        and isinstance(
            projection[profile_id].get("reasoning_token_count"), int
        )
        and not isinstance(
            projection[profile_id].get("reasoning_token_count"), bool
        )
        and projection[profile_id].get("reasoning_token_count", 0) > 0
        and projection[profile_id].get("user_visible_disclosure_present") is True
        for profile_id in profile_ids
    )


def _profile_passes_before_parent_cleanup(
    profile_result: Mapping[str, Any], profile_id: str
) -> bool:
    """Require every user-path scenario to pass plus the exact P0-13 deferral."""

    scenarios = profile_result.get("scenarios")
    p0_13_assertions = (
        scenarios[-1].get("assertions")
        if isinstance(scenarios, list)
        and len(scenarios) == 13
        and isinstance(scenarios[-1], Mapping)
        else None
    )
    return (
        profile_result.get("profile_id") == profile_id
        and profile_result.get("status") == "BLOCKED_EVIDENCE"
        and isinstance(scenarios, list)
        and len(scenarios) == 13
        and [
            row.get("scenario_id") if isinstance(row, Mapping) else None
            for row in scenarios
        ]
        == [f"P0-{index:02d}" for index in range(1, 14)]
        and all(
            isinstance(row, Mapping) and row.get("status") == "PASS"
            for row in scenarios[:12]
        )
        and isinstance(p0_13_assertions, Mapping)
        and p0_13_assertions.get("trace_stages_complete") is True
        and p0_13_assertions.get("trace_correlation_confirmed") is True
        and p0_13_assertions.get("latency_attributed") is True
        and parent_cleanup_is_deferred(profile_result)
    )


def _parent_cleanup_projection(
    receipt: Mapping[str, Any] | None,
    profile_ids: Sequence[str],
    *,
    required: bool,
    manifest_profiles_validated: bool,
) -> dict[str, Any]:
    """Return an explicit, fail-closed view of deterministic parent cleanup."""

    expected = list(profile_ids) if required else []
    if not required:
        return {
            "owner": "deterministic_parent",
            "status": "NOT_APPLICABLE",
            "expected_profile_ids": expected,
            "manifest_profiles_validated": False,
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        }

    raw = receipt if isinstance(receipt, Mapping) else {}
    attempted = raw.get("attempted")
    cleaned = raw.get("cleaned")
    failed = raw.get("failed_profile_ids")
    manifest_deleted = raw.get("manifest_deleted")
    manifest_missing = raw.get("manifest_missing")
    failed_is_valid = (
        isinstance(failed, list)
        and all(isinstance(value, str) and value in expected for value in failed)
        and len(failed) == len(set(failed))
    )
    exact = (
        manifest_profiles_validated
        and isinstance(attempted, int)
        and not isinstance(attempted, bool)
        and attempted == len(expected)
        and isinstance(cleaned, int)
        and not isinstance(cleaned, bool)
        and cleaned == len(expected)
        and failed_is_valid
        and failed == []
        and manifest_deleted is True
        and manifest_missing is False
    )
    return {
        "owner": "deterministic_parent",
        "status": "PASS" if exact else "FAIL",
        "expected_profile_ids": expected,
        "manifest_profiles_validated": manifest_profiles_validated,
        "attempted": attempted if type(attempted) is int and attempted >= 0 else None,
        "cleaned": cleaned if type(cleaned) is int and cleaned >= 0 else None,
        "failed_profile_ids": failed if failed_is_valid else [],
        "manifest_deleted": manifest_deleted is True,
        "manifest_missing": manifest_missing is True,
    }


def _diagnostic_profile_projection(
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
    parent_cleanup: Mapping[str, Any],
) -> dict[str, str]:
    parent_passed = parent_cleanup.get("status") == "PASS"
    return {
        profile_id: (
            "PASS"
            if parent_passed
            and isinstance(profile_results.get(profile_id), Mapping)
            and _profile_passes_before_parent_cleanup(
                profile_results[profile_id], profile_id
            )
            else "FAIL"
        )
        for profile_id in profile_ids
    }


def _safe_failure(exc: Exception) -> str:
    safe_types = (
        LocalDiagnosticError,
        provision_profiles.ProvisionError,
        run_codex_profile_workers.WorkerLaunchError,
        render_diagnostic_artifacts.DiagnosticRenderError,
        write_codex_config.CodexConfigError,
        install_codex_auth.CodexAuthInstallError,
        harness_provenance.HarnessProvenanceError,
        deployment_verifier.DeploymentVerificationError,
    )
    if isinstance(exc, safe_types):
        return str(exc)
    return "local diagnostic encountered an internal failure"


def execute(
    options: DiagnosticOptions,
    *,
    dependencies: DiagnosticDependencies | None = None,
) -> tuple[dict[str, Any], Path]:
    dependencies = dependencies or DiagnosticDependencies()
    if options.candidate_sha and not _SHA_RE.fullmatch(options.candidate_sha):
        raise LocalDiagnosticError(
            "candidate SHA must be 40 or 64 lowercase hex characters"
        )
    if not _CODEX_MODEL_RE.fullmatch(options.codex_model):
        raise LocalDiagnosticError("Codex model must be one normalized model ID")
    if options.runtime_requirement not in {BASELINE_RUNTIME, RUNTIME_V2_RUNTIME}:
        raise LocalDiagnosticError("runtime requirement is invalid")
    if type(options.allow_public_health_identity) is not bool:
        raise LocalDiagnosticError("deployment identity mode is invalid")
    selected = _selected_profiles(options.profile_ids)
    if selected != options.profile_ids:
        options = replace(options, profile_ids=selected)
    options = _resolve_runtime_options(options)

    run_id = datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ-") + token_hex(4)
    paths = create_run_paths(options, run_id)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "qualification_mode": "diagnostic",
        "runtime_requirement": options.runtime_requirement,
        "release_qualified": False,
        "run_id": run_id,
        "target": "test",
        "base_url": LOCKED_BASE_URL,
        "candidate_sha": options.candidate_sha or None,
        "codex_model": options.codex_model,
        "qualification_harness": None,
        "requested_profile_ids": list(options.profile_ids),
        "preflight_only": options.preflight_only,
        "status": "RUNNING",
        "profile_statuses": {},
        "diagnostic_profile_statuses": {},
        "provisioning": {
            "profile_statuses": {},
            "failure_codes": {},
        },
        "orchestration": None,
        "validated_credential_names": [],
        "codex_preflight": None,
        "codex_network_profile": None,
        "deployment_identity": None,
        "cot_delivery": {},
        "missing_strict_evidence": list(MISSING_STRICT_EVIDENCE),
        "cleanup": None,
        "parent_cleanup_verification": None,
        "private_debug_retained": False,
        "private_debug_run_id": None,
        "private_cleanup_retry_retained": False,
        "private_scratch_remains": False,
        "error": None,
    }
    active_env: dict[str, str] = {}
    secret_values: list[str] = []
    oauth_values: list[str] = []
    worker_phase_started = False
    failure: str | None = None
    cleanup_receipt: dict[str, Any] | None = None
    profile_results: dict[str, dict[str, Any]] = {}
    manifest_profiles_validated = False
    try:
        summary["qualification_harness"] = dependencies.collect_harness_provenance(
            options.source_root
        )
        loaded = load_env_file(options.env_file)
        secret_values.extend(_loaded_credential_values(loaded))
        active_env, credential_names = prepare_environment(
            loaded, options.profile_ids, run_id
        )
        summary["validated_credential_names"] = list(credential_names)
        if options.allow_public_health_identity:
            observer = (
                dependencies.observe_deployment
                or _observe_public_health_deployment
            )
            deployment_receipt = observer(
                options.candidate_sha or None,
                paths.deployment_receipt,
                env={"IO_E2E_BASE_URL": LOCKED_BASE_URL},
                expected_runtime=options.runtime_requirement,
            )
        else:
            deployment_receipt = dependencies.verify_deployment(
                options.candidate_sha or None,
                paths.deployment_receipt,
                env=_deployment_environment(loaded),
                expected_runtime=options.runtime_requirement,
            )
        observed_backend_sha = str(
            deployment_receipt.get("observed_backend_sha") or ""
        ).lower()
        resolved_candidate_sha = str(
            deployment_receipt.get("expected_deployment_sha") or ""
        ).lower()
        observed_worker_sha = deployment_receipt.get("observed_worker_sha")
        if options.allow_public_health_identity:
            observed_deployment_sha = deployment_receipt.get(
                "observed_deployment_sha"
            )
            if (
                not _SHA_RE.fullmatch(observed_backend_sha)
                or resolved_candidate_sha != observed_backend_sha
                or observed_deployment_sha is not None
                or observed_worker_sha is not None
                or deployment_receipt.get("liveness_verified") is not True
                or deployment_receipt.get("deployment_identity_verified") is not False
                or deployment_receipt.get("deployment_identity_observed") is not True
                or deployment_receipt.get("identity_evidence_source")
                != PUBLIC_HEALTH_IDENTITY_SOURCE
                or deployment_receipt.get("identity_gap_code")
                != PUBLIC_HEALTH_IDENTITY_GAP
            ):
                raise LocalDiagnosticError(
                    "public deployment observation receipt is invalid"
                )
        else:
            observed_deployment_sha = str(
                deployment_receipt.get("observed_deployment_sha") or ""
            ).lower()
            if (
                not _SHA_RE.fullmatch(observed_backend_sha)
                or resolved_candidate_sha != observed_backend_sha
                or observed_deployment_sha != resolved_candidate_sha
                or observed_worker_sha is not None
                or deployment_receipt.get("liveness_verified") is not True
                or deployment_receipt.get("deployment_identity_verified") is not True
            ):
                raise LocalDiagnosticError(
                    "trusted deployment identity receipt is invalid"
                )
        options = replace(options, candidate_sha=resolved_candidate_sha)
        summary["candidate_sha"] = resolved_candidate_sha
        if options.allow_public_health_identity:
            summary["deployment_identity"] = {
                "expected_deployment_sha": resolved_candidate_sha,
                "observed_backend_sha": observed_backend_sha,
                "observed_deployment_sha": None,
                "observed_worker_sha": None,
                "identity_observed": True,
                "identity_verified": False,
                "identity_evidence_source": PUBLIC_HEALTH_IDENTITY_SOURCE,
                "identity_gap_code": PUBLIC_HEALTH_IDENTITY_GAP,
                "health_status": deployment_receipt.get("health_status"),
            }
        else:
            summary["deployment_identity"] = {
                "expected_deployment_sha": resolved_candidate_sha,
                "observed_backend_sha": observed_backend_sha,
                "observed_deployment_sha": observed_deployment_sha,
                "observed_worker_sha": observed_worker_sha,
                "identity_verified": True,
            }
        _verify_codex_location_separation(options.codex_bin, options, paths)
        dependencies.verify_codex_provenance(options.codex_bin)
        dependencies.verify_codex_version(options.codex_bin)
        oauth_values.extend(
            install_local_codex_auth(options.auth_file, paths.codex_home)
        )
        secret_values.extend(oauth_values)
        _create_worker_source_snapshot(
            options.source_root,
            paths.worker_source_root,
            sensitive_paths=(options.env_file, options.auth_file),
            secret_values=secret_values,
        )
        harness = summary.get("qualification_harness")
        snapshot_sha256 = harness_provenance.snapshot_digest(
            paths.worker_source_root
        )
        if (
            not isinstance(harness, dict)
            or harness.get("worker_source_sha256") != snapshot_sha256
        ):
            raise LocalDiagnosticError(
                "qualification worker snapshot does not match harness provenance"
            )
        harness["worker_snapshot_sha256"] = snapshot_sha256
        secret_values.extend(_fixture_privacy_values(paths.worker_source_root))
        benchmark_fake_ip_detected = _build_codex_config(options, paths)
        summary["codex_network_profile"] = {
            "tool_network_enabled": False,
            "allowed_hosts": [],
            "codex_model_transport_separate": True,
            "benchmark_fake_ip_detected": benchmark_fake_ip_detected,
            "allow_local_binding": False,
        }
        login_check = dependencies.verify_login or _verify_login
        login_check(
            options.codex_bin,
            paths.codex_home,
            paths.supervisor_home,
            paths.supervisor_tmp,
        )
        exec_check = dependencies.verify_codex_exec or _verify_codex_exec
        summary["codex_preflight"] = exec_check(options, paths)

        if options.preflight_only:
            summary["status"] = "PREFLIGHT_PASS"
        else:
            manifest = _provision_diagnostic(dependencies, options, paths, active_env)
            manifest_rows = _validate_diagnostic_manifest_structure(
                manifest, options.profile_ids, options.runtime_requirement
            )
            manifest_profiles_validated = True
            classification = _classify_diagnostic_manifest(
                manifest_rows, options.profile_ids, options.runtime_requirement
            )
            provision_statuses = dict(classification.provision_statuses)
            provision_failure_codes = dict(
                classification.provision_failure_codes
            )
            requested_profile_count = len(options.profile_ids)
            ready_profile_count = len(classification.ready_profile_ids)
            blocked_profile_count = len(classification.blocked_profile_ids)
            accounted_profile_count = ready_profile_count + blocked_profile_count
            summary["provisioning"] = {
                "profile_statuses": provision_statuses,
                "failure_codes": provision_failure_codes,
                "requested_profile_count": requested_profile_count,
                "ready_profile_count": ready_profile_count,
                "blocked_profile_count": blocked_profile_count,
                "accounted_profile_count": accounted_profile_count,
            }
            manifest_by_id = {
                str(row["profile_id"]): row for row in manifest_rows
            }
            for row in manifest_rows:
                secret_values.extend(
                    str(row.get(field) or "")
                    for field in ("api_key", "secret_key_b64")
                )
            _split_selected_manifest(
                paths.manifest, paths.profile_manifests, options.profile_ids
            )
            ready_profile_ids = classification.ready_profile_ids
            blocked_profile_ids = set(classification.blocked_profile_ids)
            if ready_profile_ids:
                worker_phase_started = True
                receipt = _launch_diagnostic(
                    dependencies,
                    options,
                    paths,
                    profile_ids=ready_profile_ids,
                )
            else:
                receipt = {
                    "launch_attempts": 0,
                    "max_observed_profile_concurrency": 0,
                    "workers": [],
                }
            workers = receipt.get("workers")
            if (
                receipt.get("launch_attempts") != len(ready_profile_ids)
                or not isinstance(workers, list)
                or len(workers) != len(ready_profile_ids)
                or not all(isinstance(row, Mapping) for row in workers)
                or [row.get("profile_id") for row in workers]
                != list(ready_profile_ids)
            ):
                raise LocalDiagnosticError(
                    "diagnostic agent launch receipt matrix is invalid"
                )
            worker_by_id = {
                str(row["profile_id"]): row
                for row in workers
                if isinstance(row, Mapping)
            }
            result_sources = {
                profile_id: (
                    "provision_blocked"
                    if profile_id in blocked_profile_ids
                    else str(worker_by_id[profile_id].get("result_source") or "invalid")
                )
                for profile_id in options.profile_ids
            }
            summary["orchestration"] = {
                "matrix_profile_attempts": len(options.profile_ids),
                "launch_attempts": receipt.get("launch_attempts"),
                "requested_profile_count": requested_profile_count,
                "ready_profile_count": ready_profile_count,
                "blocked_profile_count": blocked_profile_count,
                "accounted_profile_count": accounted_profile_count,
                "agent_launch_profile_ids": list(ready_profile_ids),
                "blocked_profile_ids": list(classification.blocked_profile_ids),
                "result_sources": result_sources,
                "max_observed_profile_concurrency": receipt.get(
                    "max_observed_profile_concurrency"
                ),
                "completed_command_execution_counts": {
                    profile_id: (
                        worker_by_id[profile_id].get(
                            "completed_command_execution_count"
                        )
                        if profile_id in worker_by_id
                        else 0
                    )
                    for profile_id in options.profile_ids
                },
                "completed_scenario_command_ids": {
                    profile_id: (
                        worker_by_id[profile_id].get(
                            "completed_scenario_command_ids"
                        )
                        if profile_id in worker_by_id
                        else []
                    )
                    for profile_id in options.profile_ids
                },
                "completed_scenario_command_counts": {
                    profile_id: (
                        worker_by_id[profile_id].get(
                            "completed_scenario_command_counts"
                        )
                        if profile_id in worker_by_id
                        else {}
                    )
                    for profile_id in options.profile_ids
                },
                "p0_06_command_phases": {
                    profile_id: (
                        worker_by_id[profile_id].get("p0_06_command_phases")
                        if profile_id in worker_by_id
                        else []
                    )
                    for profile_id in options.profile_ids
                },
            }
            ready_cot = (
                _cot_delivery_projection(receipt, ready_profile_ids)
                if ready_profile_ids
                else {}
            )
            summary["cot_delivery"] = {
                profile_id: (
                    _blocked_cot_projection(provision_failure_codes[profile_id])
                    if profile_id in blocked_profile_ids
                    else ready_cot[profile_id]
                )
                for profile_id in options.profile_ids
            }
            statuses: dict[str, str] = {}
            for profile_id in options.profile_ids:
                if profile_id in blocked_profile_ids:
                    result = _blocked_profile_result(
                        manifest_by_id[profile_id],
                        profile_id=profile_id,
                        expected_runtime=options.runtime_requirement,
                        provisioning_failure_code=provision_failure_codes[
                            profile_id
                        ],
                        authoring_schema_path=(
                            paths.worker_source_root
                            / "qa"
                            / "schemas"
                            / "codex-run-result.schema.json"
                        ),
                    )
                else:
                    source = paths.aggregation_inputs / f"{profile_id}.json"
                    result = _load_private_json(source, f"{profile_id} result")
                if _contains_secret(result, secret_values):
                    raise LocalDiagnosticError(
                        "diagnostic worker artifact contains secret material"
                    )
                statuses[profile_id] = str(result.get("status") or "AGENT_ERROR")
                profile_results[profile_id] = result
                _write_private_json(
                    paths.profile_artifacts / f"{profile_id}.json", result
                )
            summary["profile_statuses"] = statuses
            summary["status"] = "DIAGNOSTIC_FAIL"
    except Exception as exc:
        failure = _safe_failure(exc)
        summary["status"] = "DIAGNOSTIC_ERROR"
        summary["error"] = failure
    finally:
        try:
            cleanup_receipt = dependencies.cleanup(paths.manifest, env=active_env)
        except Exception:
            manifest_missing = not paths.manifest.exists()
            cleanup_receipt = {
                "attempted": 0,
                "cleaned": 0,
                "failed_profile_ids": (
                    [] if manifest_missing else list(options.profile_ids)
                ),
                "manifest_deleted": False,
                "manifest_missing": manifest_missing,
            }
        summary["cleanup"] = cleanup_receipt
        cleanup_required = not options.preflight_only and manifest_profiles_validated
        parent_cleanup = _parent_cleanup_projection(
            cleanup_receipt,
            options.profile_ids,
            required=cleanup_required,
            manifest_profiles_validated=manifest_profiles_validated,
        )
        summary["parent_cleanup_verification"] = parent_cleanup
        summary["diagnostic_profile_statuses"] = (
            _diagnostic_profile_projection(
                profile_results,
                options.profile_ids,
                parent_cleanup,
            )
            if cleanup_required
            else {}
        )
        cleanup_verification_failed = (
            cleanup_required and parent_cleanup.get("status") != "PASS"
        ) or bool(
            isinstance(cleanup_receipt, Mapping)
            and cleanup_receipt.get("failed_profile_ids")
        )
        if cleanup_verification_failed:
            summary["status"] = "CLEANUP_FAIL"
            summary["error"] = "deterministic parent cleanup could not be verified"
            failure = summary["error"]
        elif cleanup_required and failure is None:
            summary["status"] = (
                "DIAGNOSTIC_PASS"
                if summary["diagnostic_profile_statuses"]
                and set(summary["diagnostic_profile_statuses"].values()) == {"PASS"}
                and _cot_delivery_passes(
                    summary["cot_delivery"], options.profile_ids
                )
                else "DIAGNOSTIC_FAIL"
            )

    retain_debug = (
        worker_phase_started
        and not options.preflight_only
        and summary["status"] != "DIAGNOSTIC_PASS"
    )
    cleanup_failed = bool(
        (
            summary.get("parent_cleanup_verification", {}).get("status") == "FAIL"
            or (
                isinstance(cleanup_receipt, Mapping)
                and cleanup_receipt.get("failed_profile_ids")
            )
        )
        and paths.manifest.exists()
    )
    try:
        if cleanup_failed:
            _retain_cleanup_retry_manifest(paths)
            summary["private_cleanup_retry_retained"] = True
        else:
            if retain_debug:
                _retain_debug_quarantine(
                    options,
                    paths,
                    secret_values,
                )
                summary["private_debug_retained"] = True
                summary["private_debug_run_id"] = run_id
            shutil.rmtree(paths.private_root)
    except Exception as exc:
        shutil.rmtree(
            options.private_base.parent / "io-e2e-agent-driven-test-debug" / run_id,
            ignore_errors=True,
        )
        private_failure = _safe_failure(exc)
        if private_failure == "local diagnostic encountered an internal failure":
            private_failure = "private diagnostic finalization failed"
        summary["status"] = "DIAGNOSTIC_ERROR"
        summary["error"] = private_failure
        failure = private_failure
        summary["private_debug_retained"] = False
        summary["private_debug_run_id"] = None
        summary["private_cleanup_retry_retained"] = False
        # Once private finalization itself fails, retaining any raw run state is
        # less safe than losing local diagnostics. Remove the entire root for
        # both cleanup and non-cleanup failures, then scrub every known secret
        # and retry only as a last-resort best effort.
        shutil.rmtree(paths.private_root, ignore_errors=True)
        if paths.private_root.exists():
            try:
                _scrub_sensitive_material(paths.private_root, secret_values)
            except Exception:
                pass
            shutil.rmtree(paths.private_root, ignore_errors=True)
        summary["private_scratch_remains"] = paths.private_root.exists()
        if summary["private_scratch_remains"]:
            private_failure = (
                "private diagnostic finalization failed; private scratch remains"
            )
            summary["error"] = private_failure
            failure = private_failure

    summary_path = paths.artifact_root / "diagnostic-summary.json"
    if _contains_secret(summary, secret_values):
        summary["status"] = "SECURITY_FAIL"
        summary["error"] = "diagnostic summary contains secret material"
        failure = summary["error"]
    try:
        render_diagnostic_artifacts.render_operator_artifacts(
            summary=summary,
            profile_results=profile_results,
            profile_ids=options.profile_ids,
            artifact_root=paths.artifact_root,
        )
        _scan_public_artifacts(paths.artifact_root, secret_values, options.profile_ids)
        _write_private_json(summary_path, summary)
        _scan_public_artifacts(paths.artifact_root, secret_values, options.profile_ids)
    except Exception as exc:
        del exc
        artifact_failure = "diagnostic artifacts could not be produced safely"
        failure = artifact_failure
        safe_summary = {
            "schema_version": 1,
            "qualification_mode": "diagnostic",
            "runtime_requirement": options.runtime_requirement,
            "release_qualified": False,
            "run_id": run_id,
            "target": "test",
            "base_url": LOCKED_BASE_URL,
            "candidate_sha": options.candidate_sha,
            "codex_model": options.codex_model,
            "requested_profile_ids": list(options.profile_ids),
            "preflight_only": options.preflight_only,
            "status": "SECURITY_FAIL",
            "artifacts_quarantined": True,
            "error": artifact_failure,
        }
        summary = safe_summary
        shutil.rmtree(paths.artifact_root, ignore_errors=True)
        try:
            _make_private_directory(paths.artifact_root)
            if _contains_secret(safe_summary, secret_values):
                raise LocalDiagnosticError(
                    "sanitized diagnostic summary is unsafe"
                )
            _write_private_json(summary_path, safe_summary)
            if set(paths.artifact_root.iterdir()) != {summary_path}:
                raise LocalDiagnosticError(
                    "sanitized diagnostic artifact boundary is invalid"
                )
        except Exception:
            shutil.rmtree(paths.artifact_root, ignore_errors=True)
    if failure:
        raise LocalDiagnosticError(f"{failure}; summary: {summary_path}")
    return summary, summary_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.test"))
    parser.add_argument(
        "--candidate-sha",
        default="",
        help=(
            "optional full expected source SHA; omitted discovers the protected "
            "test backend identity, while a supplied value must match it exactly"
        ),
    )
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    parser.add_argument(
        "--codex-bin",
        type=Path,
        help=(
            "explicit pinned official Codex npm wrapper or native binary; "
            "PATH is used only as a package locator when omitted"
        ),
    )
    parser.add_argument(
        "--worker-python",
        type=Path,
        help=(
            "owner-controlled Python with cryptography; auto-detected from the "
            "Codex desktop runtime when omitted"
        ),
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        choices=PROFILE_IDS,
        help="run one locked profile (repeatable); omitted means all profiles",
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--allow-public-health-identity",
        action="store_true",
        help=(
            "diagnostic-only: pin the target to /healthz release.git_commit "
            "without claiming protected deployment attestation"
        ),
    )
    parser.add_argument(
        "--require-runtime-v2",
        action="store_true",
        help=(
            "add strict hosted_resident/version-2 runtime assertions; default "
            "tests the currently deployed runtime and records its identity"
        ),
    )
    return parser


def _options(args: argparse.Namespace) -> DiagnosticOptions:
    source_root = Path(__file__).resolve().parents[1]
    profile_ids = _selected_profiles(args.profile)
    return DiagnosticOptions(
        env_file=args.env_file,
        candidate_sha=args.candidate_sha,
        codex_model=args.codex_model,
        profile_ids=profile_ids,
        preflight_only=args.preflight_only,
        source_root=source_root,
        auth_file=Path.home() / ".codex" / "auth.json",
        private_base=Path.home() / ".codex" / "io-e2e-agent-driven-test-runs",
        codex_bin=_resolve_trusted_codex_binary(args.codex_bin),
        worker_python=args.worker_python,
        runtime_requirement=(
            RUNTIME_V2_RUNTIME if args.require_runtime_v2 else BASELINE_RUNTIME
        ),
        allow_public_health_identity=args.allow_public_health_identity,
    )


def main(argv: Sequence[str] | None = None) -> int:
    previous_umask = os.umask(0o077)
    try:
        args = _parser().parse_args(argv)
        summary, path = execute(_options(args))
    except LocalDiagnosticError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: local diagnostic encountered an internal failure", file=sys.stderr
        )
        return 1
    finally:
        os.umask(previous_umask)
    print(
        json.dumps(
            {
                "ok": summary["status"] in ("PREFLIGHT_PASS", "DIAGNOSTIC_PASS"),
                "status": summary["status"],
                "release_qualified": False,
                "summary": str(path),
            },
            sort_keys=True,
        )
    )
    return 0 if summary["status"] in ("PREFLIGHT_PASS", "DIAGNOSTIC_PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
