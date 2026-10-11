"""Operator-dispatched test-only entrypoint. Never invoke without reviewed approval."""

import hashlib
import json
import os
from pathlib import Path

from eth_account import Account

from compose_publisher import RPC, Stop
from compose_recovery import recover
from compose_recovery_github import GitHubRecovery, REPO


def main():
    os.umask(0o077)
    destination = Path(os.environ["RECOVERY_EVIDENCE_DIR"])
    try:
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RECOVERY_OPERATION") != "reconcile-test-compose"
            or os.environ.get("RECOVERY_HOLD") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/test"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or os.environ.get("GITHUB_WORKFLOW") != "CI"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        ):
            raise Stop("recovery_reviewed_test_dispatch_required")
        raw = os.environ["RECOVERY_AUTHORIZATION"].encode()
        expected = os.environ["RECOVERY_AUTHORIZATION_SHA256"]
        if hashlib.sha256(raw).hexdigest() != expected:
            raise Stop("recovery_authorization_digest")
        auth = json.loads(raw)
        if auth.get("execution_sha") != os.environ["GITHUB_SHA"]:
            raise Stop("recovery_execution_sha_changed")
        store = GitHubRecovery(destination)
        result = recover(
            authorization=raw,
            expected_digest=expected,
            store=store,
            rpc=RPC("https://ethereum-sepolia-rpc.publicnode.com"),
            signer_factory=lambda: Account.from_key(os.environ["PRIVATE_KEY"]),
        )
        destination.joinpath("result.json").write_text(json.dumps(result))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # No arbitrary exception text, RPC bodies, raw transaction or keys.
        reason = str(exc) if isinstance(exc, Stop) else type(exc).__name__
        result = {"result": "STOP", "reason": reason, "retry_authorized": False}
        destination.mkdir(parents=True, exist_ok=True)
        destination.joinpath("failure.json").write_text(json.dumps(result))
        print(json.dumps(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
