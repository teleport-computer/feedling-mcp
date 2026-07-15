#!/usr/bin/env python3
"""Create a trusted deployed-runtime receipt before Codex runs.

The headless qualification agent must not be the authority for its own target.
Every mode reads the test-only admin build-identity endpoint and requires the
image-baked source SHA to equal the SHA injected by the serialized test deploy.
Strict V2 mode is completed later by parent-owned per-profile runtime readbacks
and live driver/chat receipts. The read-only build receipt stays outside the
agent's writable artifact directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.provision_profiles import (  # noqa: E402
    AdminClient,
    ProvisionError,
    validate_base_url,
)


_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
RECEIPT_SCHEMA_VERSION = 1
BASELINE_RUNTIME = "deployed_current"
RUNTIME_V2_RUNTIME = "hosted_resident"


class DeploymentVerificationError(RuntimeError):
    """A fixed deployment-preflight failure safe to print in CI."""


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise DeploymentVerificationError(
            f"missing required environment variable: {name}"
        )
    return value


def _write_read_only_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o400)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def verify_deployment(
    expected_sha: str | None,
    receipt_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    admin_client: AdminClient | None = None,
    expected_runtime: str = RUNTIME_V2_RUNTIME,
) -> dict[str, Any]:
    active_env = os.environ if env is None else env
    expected_input = str(expected_sha or "").strip().lower()
    if expected_input and not _SHA_RE.fullmatch(expected_input):
        raise DeploymentVerificationError("expected deployment SHA is malformed")
    if expected_runtime not in {BASELINE_RUNTIME, RUNTIME_V2_RUNTIME}:
        raise DeploymentVerificationError("runtime requirement is invalid")
    base_url = validate_base_url(_required_env(active_env, "QA_FEEDLING_BASE_URL"))
    token = _required_env(active_env, "QA_TEST_ADMIN_TOKEN")
    client = admin_client or AdminClient(base_url, token)

    try:
        status, identity = client.request("GET", "/v1/admin/qa/build-identity")
    except ProvisionError:
        raise DeploymentVerificationError(
            "test deployment build identity endpoint was unreachable"
        ) from None
    if status != 200 or not isinstance(identity, dict):
        raise DeploymentVerificationError(
            "test deployment build identity endpoint is unavailable"
        )
    backend_sha = str(identity.get("backend_sha") or "").strip().lower()
    deployment_sha = str(identity.get("deployment_sha") or "").strip().lower()
    if (
        identity.get("schema_version") != 1
        or identity.get("environment") != "test"
        or identity.get("identity_verified") is not True
        or not _SHA_RE.fullmatch(backend_sha)
        or not _SHA_RE.fullmatch(deployment_sha)
        or backend_sha != deployment_sha
    ):
        raise DeploymentVerificationError(
            "test deployment build identity is invalid"
        )
    expected = expected_input or backend_sha
    if backend_sha != expected:
        raise DeploymentVerificationError(
            "deployed backend build does not match the candidate"
        )

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "environment": "test",
        "base_url": base_url,
        "expected_runtime": expected_runtime,
        "expected_deployment_sha": expected,
        "observed_backend_sha": backend_sha,
        "observed_deployment_sha": deployment_sha,
        "observed_worker_sha": None,
        "live_worker_count": None,
        "runtime_evidence_source": (
            "per_profile_runtime_readback_and_live_scenarios"
            if expected_runtime == RUNTIME_V2_RUNTIME
            else "deployed_runtime_readback"
        ),
        "liveness_verified": True,
        "deployment_identity_verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _write_read_only_json(receipt_path, receipt)
    except OSError:
        raise DeploymentVerificationError(
            "deployment receipt could not be checkpointed"
        ) from None
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--expected-runtime",
        choices=(BASELINE_RUNTIME, RUNTIME_V2_RUNTIME),
        default=RUNTIME_V2_RUNTIME,
    )
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = verify_deployment(
            args.expected_sha,
            args.receipt,
            expected_runtime=args.expected_runtime,
        )
    except (DeploymentVerificationError, ProvisionError) as exc:
        print(f"deployment verification error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "expected_runtime": receipt["expected_runtime"],
                "runtime_evidence_source": receipt["runtime_evidence_source"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
