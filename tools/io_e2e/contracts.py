"""Fail-closed input and output contracts for the IO E2E control client."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "io-e2e-control.v1"
CANONICAL_REPOSITORY = "teleport-computer/feedling-mcp"
CONTROLLER_BRANCH = "main"
WORKFLOW_PATH = ".github/workflows/io-e2e-control.yml"
WORKFLOW_FILE = Path(WORKFLOW_PATH).name
SUPPORTED_LANES = frozenset({"deployed_test"})
KNOWN_LANES = frozenset({"deployed_test", "branch_preview"})
SUPPORTED_SUITES = frozenset({"full"})
RUNTIME_TARGETS = frozenset({"deployed_current", "hosted_resident"})
PERSONA_REPETITIONS = frozenset({1, 3})

_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


class IoE2EError(RuntimeError):
    """A safe, stable error suitable for both people and agent callers."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def validate_repository(value: str) -> str:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise IoE2EError("INVALID_REPOSITORY", "repository must be OWNER/REPO")
    return value


def validate_canonical_repository(value: str) -> str:
    repository = validate_repository(value)
    if repository != CANONICAL_REPOSITORY:
        raise IoE2EError(
            "UNTRUSTED_REPOSITORY",
            f"IO E2E is available only for {CANONICAL_REPOSITORY}",
            details={"requested_repository": repository},
            exit_code=3,
        )
    return repository


def validate_ref(value: str) -> str:
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        raise IoE2EError("INVALID_TARGET_REF", "target ref has an invalid format")
    if (
        value.endswith(("/", ".", ".lock"))
        or value.startswith(".")
        or ".." in value
        or "//" in value
        or "@{" in value
    ):
        raise IoE2EError("INVALID_TARGET_REF", "target ref has an invalid format")
    return value


def validate_commit_sha(value: str) -> str:
    if not isinstance(value, str):
        raise IoE2EError("INVALID_TARGET_SHA", "target SHA must be a full commit SHA")
    normalized = value.lower()
    if _COMMIT_SHA.fullmatch(normalized) is None:
        raise IoE2EError("INVALID_TARGET_SHA", "target SHA must be a full commit SHA")
    return normalized


def validate_request_id(value: str) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value.lower()) is None:
        raise IoE2EError("INVALID_REQUEST_ID", "request ID must be a UUIDv4")
    return value.lower()


def validate_run_identifier(value: str) -> int | str:
    if not isinstance(value, str):
        raise IoE2EError(
            "INVALID_RUN_ID", "run identifier must be a run ID or request UUID"
        )
    if value.isascii() and value.isdecimal():
        parsed = int(value)
        if parsed > 0 and str(parsed) == value:
            return parsed
    try:
        return validate_request_id(value)
    except IoE2EError as exc:
        raise IoE2EError(
            "INVALID_RUN_ID", "run identifier must be a positive run ID or request UUID"
        ) from exc


def validate_lane(value: str) -> str:
    if value not in KNOWN_LANES:
        raise IoE2EError(
            "INVALID_LANE",
            "lane must be deployed_test or branch_preview",
            details={"supported": sorted(SUPPORTED_LANES)},
        )
    if value not in SUPPORTED_LANES:
        raise IoE2EError(
            "NOT_IMPLEMENTED",
            "branch_preview is not implemented; no candidate branch will be exposed to QA secrets",
            details={
                "requested_lane": value,
                "supported_lanes": sorted(SUPPORTED_LANES),
            },
        )
    return value


def validate_suite(value: str) -> str:
    if value not in SUPPORTED_SUITES:
        raise IoE2EError(
            "NOT_IMPLEMENTED",
            "only the full qualification suite is currently implemented",
            details={
                "requested_suite": value,
                "supported_suites": sorted(SUPPORTED_SUITES),
            },
        )
    return value


def validate_persona_repetitions(value: int) -> int:
    if isinstance(value, bool) or value not in PERSONA_REPETITIONS:
        raise IoE2EError(
            "INVALID_PERSONA_REPETITIONS",
            "persona repetitions must be exactly 1 or 3",
        )
    return value


def validate_runtime_target(value: str) -> str:
    if value not in RUNTIME_TARGETS:
        raise IoE2EError(
            "INVALID_RUNTIME_TARGET",
            "runtime target must be deployed_current or hosted_resident",
        )
    return value


def validate_lane_target(lane: str, target_ref: str) -> None:
    """Keep the deployed lane honest until isolated branch previews exist."""

    if lane == "deployed_test" and target_ref != "test":
        raise IoE2EError(
            "NOT_IMPLEMENTED",
            "deployed_test can qualify only the test branch; arbitrary refs require branch_preview",
            details={
                "requested_ref": target_ref,
                "supported_ref": "test",
                "required_lane": "branch_preview",
            },
        )


@dataclass(frozen=True)
class RunPlan:
    request_id: str
    repository: str
    controller_ref: str
    controller_workflow: str
    target_ref: str
    target_sha: str
    lane: str
    suite: str
    persona_repetitions: int
    runtime_target: str

    def __post_init__(self) -> None:
        validate_request_id(self.request_id)
        validate_canonical_repository(self.repository)
        validate_ref(self.controller_ref)
        if self.controller_ref != CONTROLLER_BRANCH:
            raise IoE2EError(
                "UNTRUSTED_CONTROLLER",
                f"controller branch must be {CONTROLLER_BRANCH}",
            )
        if self.controller_workflow != WORKFLOW_PATH:
            raise IoE2EError(
                "UNTRUSTED_WORKFLOW",
                f"controller workflow must be {WORKFLOW_PATH}",
            )
        validate_ref(self.target_ref)
        validate_commit_sha(self.target_sha)
        validate_lane(self.lane)
        validate_lane_target(self.lane, self.target_ref)
        validate_suite(self.suite)
        validate_persona_repetitions(self.persona_repetitions)
        validate_runtime_target(self.runtime_target)

    def workflow_inputs(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "target_ref": self.target_ref,
            "target_sha": self.target_sha,
            "lane": self.lane,
            "suite": self.suite,
            "persona_repetitions": str(self.persona_repetitions),
            "runtime_target": self.runtime_target,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "plan",
            **asdict(self),
            "trust_boundary": {
                "controller": "repository default branch",
                "target": "immutable data only",
                "credentials": "GitHub Environment; never passed by this client",
            },
        }
