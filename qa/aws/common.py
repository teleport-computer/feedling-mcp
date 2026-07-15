"""Shared, fail-closed primitives for disposable AWS qualification runners."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


MANAGED_BY = "feedling-agentic-e2e"
INSTANCE_TYPE = "m7i.xlarge"
CANONICAL_OWNER_ID = "099720109477"
MAX_USER_DATA_BYTES = 16_384
MAX_JIT_CONFIG_BYTES = 65_536
MIN_TTL_SECONDS = 900
MAX_TTL_SECONDS = 21_600

_AWS_ID = re.compile(r"^(?:ami|subnet|sg|i)-[0-9a-f]{8,17}$")
_REGION = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")
_AWS_CHILD_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CA_BUNDLE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    }
)


class AwsRunnerError(RuntimeError):
    """A safe validation or AWS control-plane operation failed."""


def require_match(name: str, value: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AwsRunnerError(f"invalid {name}")
    return value


def validate_region(value: str) -> str:
    return require_match("AWS region", value, _REGION)


def validate_run_id(value: str) -> str:
    return require_match("run id", value, _RUN_ID)


def validate_repository(value: str) -> str:
    return require_match("GitHub repository", value, _REPOSITORY)


def validate_commit_sha(value: str) -> str:
    if not isinstance(value, str):
        raise AwsRunnerError("invalid target commit SHA")
    normalized = value.lower()
    return require_match("target commit SHA", normalized, _COMMIT_SHA)


def validate_aws_id(name: str, value: str, prefix: str) -> str:
    require_match(name, value, _AWS_ID)
    if not value.startswith(f"{prefix}-"):
        raise AwsRunnerError(f"invalid {name}")
    return value


def validate_instance_id(value: str) -> str:
    return require_match("EC2 instance id", value, _INSTANCE_ID)


def validate_runner_version(value: str) -> str:
    return require_match("GitHub Actions runner version", value, _VERSION)


def validate_sha256(value: str) -> str:
    if not isinstance(value, str):
        raise AwsRunnerError("invalid GitHub Actions runner SHA-256")
    return require_match("GitHub Actions runner SHA-256", value.lower(), _SHA256)


def validate_run_attempt(value: int | str) -> int:
    if isinstance(value, bool):
        raise AwsRunnerError("invalid run attempt")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AwsRunnerError("invalid run attempt") from exc
    if str(parsed) != str(value) or parsed < 1 or parsed > 999_999:
        raise AwsRunnerError("invalid run attempt")
    return parsed


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AwsRunnerError("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AwsRunnerError("managed instance has an invalid expiry tag")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AwsRunnerError("managed instance has an invalid expiry tag") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AwsRunnerError("managed instance has an invalid expiry tag")
    return parsed.astimezone(timezone.utc)


def tags_by_key(resource: Mapping[str, Any]) -> dict[str, str]:
    raw_tags = resource.get("Tags", [])
    if not isinstance(raw_tags, list):
        raise AwsRunnerError("AWS resource returned malformed tags")
    tags: dict[str, str] = {}
    for item in raw_tags:
        if not isinstance(item, dict):
            raise AwsRunnerError("AWS resource returned malformed tags")
        key = item.get("Key")
        value = item.get("Value")
        if not isinstance(key, str) or not isinstance(value, str) or key in tags:
            raise AwsRunnerError("AWS resource returned malformed tags")
        tags[key] = value
    return tags


def instances_from_response(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    reservations = response.get("Reservations", [])
    if not isinstance(reservations, list):
        raise AwsRunnerError("AWS returned malformed reservations")
    instances: list[dict[str, Any]] = []
    for reservation in reservations:
        if not isinstance(reservation, dict):
            raise AwsRunnerError("AWS returned malformed reservations")
        items = reservation.get("Instances", [])
        if not isinstance(items, list):
            raise AwsRunnerError("AWS returned malformed instances")
        for instance in items:
            if not isinstance(instance, dict):
                raise AwsRunnerError("AWS returned malformed instances")
            instances.append(instance)
    return instances


def instance_state(instance: Mapping[str, Any]) -> str:
    state = instance.get("State")
    if not isinstance(state, dict) or not isinstance(state.get("Name"), str):
        raise AwsRunnerError("AWS returned an instance without state")
    return state["Name"]


def checked_instance_id(instance: Mapping[str, Any]) -> str:
    value = instance.get("InstanceId")
    if not isinstance(value, str):
        raise AwsRunnerError("AWS returned an instance without an id")
    return validate_instance_id(value)


@dataclass(frozen=True)
class AwsCli:
    """Small AWS CLI adapter that never puts JSON request bodies in argv."""

    region: str
    profile: str | None = None
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        validate_region(self.region)
        if self.profile is not None and (
            not self.profile
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.profile) is None
        ):
            raise AwsRunnerError("invalid AWS profile")

    def _base(self) -> list[str]:
        command = ["aws", "--no-cli-pager", "--region", self.region]
        if self.profile is not None:
            command.extend(("--profile", self.profile))
        return command

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        child_environment = {
            name: value for name, value in os.environ.items() if name in _AWS_CHILD_ENV
        }
        child_environment["AWS_PAGER"] = ""
        # Provisioning must never silently inherit endpoint overrides or fetch
        # credentials from the host's EC2 metadata service. OIDC, environment,
        # and explicitly selected profile credentials remain available.
        child_environment["AWS_EC2_METADATA_DISABLED"] = "true"
        child_environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
        try:
            completed = subprocess.run(
                [*self._base(), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=child_environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AwsRunnerError("AWS CLI invocation failed") from exc
        if completed.returncode != 0:
            message = completed.stderr.strip()
            if len(message) > 500:
                message = message[:500] + "..."
            raise AwsRunnerError(f"AWS CLI failed: {message or 'unknown error'}")
        return completed

    def json(self, *arguments: str) -> dict[str, Any]:
        completed = self._run((*arguments, "--output", "json"))
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AwsRunnerError("AWS CLI returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise AwsRunnerError("AWS CLI returned a non-object response")
        return value

    def json_input(
        self, service: str, operation: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="feedling-qa-aws-", suffix=".json"
        )
        path = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            return self.json(
                service,
                operation,
                "--cli-input-json",
                f"file://{path}",
            )
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def run(self, *arguments: str) -> None:
        self._run(arguments)


def load_private_ascii_file(path: Path, *, max_bytes: int) -> str:
    if not path.is_absolute():
        raise AwsRunnerError("JIT configuration path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AwsRunnerError("cannot read JIT configuration file") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise AwsRunnerError("JIT configuration file is unsafe")

        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            try:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
            except OSError as exc:
                raise AwsRunnerError("cannot read JIT configuration file") from exc
            if not chunk:
                raise AwsRunnerError("JIT configuration changed while it was read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AwsRunnerError("JIT configuration changed while it was read")

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
        if any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise AwsRunnerError("JIT configuration changed while it was read")
        try:
            return b"".join(chunks).decode("ascii")
        except UnicodeDecodeError as exc:
            raise AwsRunnerError("JIT configuration must be ASCII") from exc
    finally:
        os.close(descriptor)
