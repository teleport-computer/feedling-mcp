"""Test-only authoritative backend build identity for qualification gates.

The container image already bakes ``FEEDLING_GIT_COMMIT`` at build time.  The
test deploy separately injects ``IO_E2E_TEST_DEPLOY_SHA`` through Phala's
encrypted environment channel.  A qualification may trust the identity only
when both full SHAs are valid and equal, and only while the test-only synthetic
account feature is enabled.  Production compose files expose neither switch.
"""

from __future__ import annotations

import os
import re

IMAGE_SHA_ENV = "FEEDLING_GIT_COMMIT"
DEPLOY_SHA_ENV = "IO_E2E_TEST_DEPLOY_SHA"
SYNTHETIC_ENABLED_ENV = "IO_E2E_SYNTHETIC_ACCOUNTS_ENABLED"
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class BuildIdentityUnavailable(RuntimeError):
    """The process cannot prove that its image is the intended test deploy."""


def _full_sha(name: str) -> str:
    value = os.environ.get(name, "").strip().lower()
    if not _SHA_RE.fullmatch(value):
        raise BuildIdentityUnavailable("test build identity is unavailable")
    return value


def status_payload() -> dict[str, object]:
    """Return a bounded identity only for a coherently configured test deploy."""

    if os.environ.get(SYNTHETIC_ENABLED_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise BuildIdentityUnavailable("test build identity is unavailable")
    image_sha = _full_sha(IMAGE_SHA_ENV)
    deployment_sha = _full_sha(DEPLOY_SHA_ENV)
    if image_sha != deployment_sha:
        raise BuildIdentityUnavailable("test build identity is unavailable")
    return {
        "schema_version": 1,
        "environment": "test",
        "backend_sha": image_sha,
        "deployment_sha": deployment_sha,
        "identity_verified": True,
    }
