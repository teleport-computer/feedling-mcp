"""Stable, fail-closed identities for managed Runtime V2 worker CVMs.

Local/test workers may keep their replica-unique ephemeral identity. Managed
Pre and production fleets use a stronger deployment contract: every inventory CVM
must publish one turn heartbeat and its matching Genesis heartbeat carrying the
same stable CVM/build label and boot-unique owner ID. This module is intentionally
stdlib-only so both ``serve_worker`` and the post-deploy gate use the same
identity grammar without importing the worker's production dependency graph.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping


RUNNER_CVM_ID_ENV = "FEEDLING_V2_RUNNER_CVM_ID"
DEPLOYED_BUILD_ENV = "FEEDLING_V2_DEPLOYED_BUILD"
FLEET_IDENTITY_REQUIRED_ENV = "FEEDLING_V2_FLEET_IDENTITY_REQUIRED"
EXPLICIT_WORKER_ID_ENV = "FEEDLING_V2_WORKER_ID"
IMAGE_COMMIT_ENV = "FEEDLING_GIT_COMMIT"

_CVM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUILD_RE = re.compile(r"^[0-9a-f]{7}$")
_IMAGE_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_FLEET_WORKER_RE = re.compile(
    r"^v2-fleet-cvm-(?P<cvm_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
    r"-build-(?P<build>[0-9a-f]{7})-boot-(?P<boot_id>[0-9a-f]{12})$"
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def validate_cvm_id(raw: str) -> str:
    value = str(raw or "").strip()
    if not _CVM_ID_RE.fullmatch(value):
        raise ValueError(
            f"{RUNNER_CVM_ID_ENV} must be 1-128 ASCII letters, digits, '.', '_' or '-'"
        )
    return value


def validate_build(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not _BUILD_RE.fullmatch(value):
        raise ValueError(f"{DEPLOYED_BUILD_ENV} must be exactly 7 lowercase hex chars")
    return value


def fleet_deployment_id(cvm_id: str, build: str) -> str:
    """Stable application identity for one inventory CVM/build pair."""
    return f"v2-fleet-cvm-{validate_cvm_id(cvm_id)}-build-{validate_build(build)}"


def fleet_worker_id(cvm_id: str, build: str, boot_id: str) -> str:
    """Boot-unique worker ID carrying the stable CVM/build identity.

    Job claims are owned by ``worker_id``. Reusing the exact same ID after a
    process crash could make abandoned work look owned by the replacement, so
    the stable deployment identity is a prefix and every process boot gets a
    nonce. The fleet gate matches the prefix and requires turn/Genesis to share
    the exact full boot identity.
    """
    nonce = str(boot_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{12}", nonce):
        raise ValueError("boot_id must be exactly 12 lowercase hex chars")
    return f"{fleet_deployment_id(cvm_id, build)}-boot-{nonce}"


def parse_fleet_worker_id(worker_id: str) -> tuple[str, str, str] | None:
    value = str(worker_id or "")
    match = _FLEET_WORKER_RE.fullmatch(value)
    if match is None:
        return None
    return match.group("cvm_id"), match.group("build"), match.group("boot_id")


def resolve_worker_id(
    fallback: Callable[[], str],
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve an ephemeral or managed-fleet worker identity.

    Managed Pre/production compose sets ``FLEET_IDENTITY_REQUIRED_ENV=1``. In that mode
    missing identity inputs, an arbitrary worker-id override, or disagreement
    between the deployment build and the commit baked into the image are fatal.
    The process therefore cannot silently fall back to a hostname/UUID identity
    that would make an aggregate fleet gate false-green.
    """
    env = os.environ if environ is None else environ
    cvm_id = str(env.get(RUNNER_CVM_ID_ENV, "") or "").strip()
    deployed_build = str(env.get(DEPLOYED_BUILD_ENV, "") or "").strip().lower()
    required = str(env.get(FLEET_IDENTITY_REQUIRED_ENV, "") or "").strip().lower()
    fleet_mode = required in _TRUE_VALUES or bool(cvm_id or deployed_build)

    if not fleet_mode:
        explicit = str(env.get(EXPLICIT_WORKER_ID_ENV, "") or "").strip()
        return explicit or fallback()

    if not cvm_id or not deployed_build:
        raise RuntimeError(
            "managed fleet identity requires both "
            f"{RUNNER_CVM_ID_ENV} and {DEPLOYED_BUILD_ENV}"
        )
    if str(env.get(EXPLICIT_WORKER_ID_ENV, "") or "").strip():
        raise RuntimeError(
            f"{EXPLICIT_WORKER_ID_ENV} cannot override a managed fleet identity"
        )

    image_commit = str(env.get(IMAGE_COMMIT_ENV, "") or "").strip().lower()
    if not _IMAGE_COMMIT_RE.fullmatch(image_commit):
        raise RuntimeError(
            f"{IMAGE_COMMIT_ENV} must be a baked hexadecimal commit in fleet mode"
        )
    build = validate_build(deployed_build)
    if image_commit[:7] != build:
        raise RuntimeError(
            "deployed build identity does not match the commit baked into the image"
        )
    return fleet_worker_id(cvm_id, build, uuid.uuid4().hex[:12])
