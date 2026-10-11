"""GitHub-backed immutable evidence and atomic target claims for test recovery."""

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

from compose_publisher import MAX_CONFIGURED_GAS, MAX_PRICE, Stop

REPO = "teleport-computer/feedling-mcp"


def verify_frozen_limits_diagnostic(raw, frozen):
    """Admit only the producer's exact pre-send numeric failure evidence.

    This is supporting evidence, never permission to replace a frozen transaction.
    Original intent/source checks and all current recovery preflights still apply.
    """
    reason = "recovery_invalid_frozen_limits_diagnostic"

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Stop(reason)
            result[key] = value
        return result

    try:
        detail = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise Stop(reason) from None
    integers = {
        "estimate",
        "required_gas",
        "frozen_gas",
        "gas_cap",
        "required_gas_price",
        "frozen_gas_price",
        "gas_price_cap",
        "sends",
    }
    if (
        not isinstance(detail, dict)
        or set(detail)
        != integers | {"result", "phase", "gas_sufficient", "fee_sufficient"}
        or any(type(detail[key]) is not int for key in integers)
        or any(
            type(detail[key]) is not bool
            for key in ("gas_sufficient", "fee_sufficient")
        )
        or detail["result"] != "STOP"
        or detail["phase"] != "send_preflight"
        or detail["sends"] != 0
    ):
        raise Stop(reason)
    gas, price = detail["required_gas"], detail["required_gas_price"]
    if (
        not 0 < detail["estimate"]
        or gas != (detail["estimate"] * 120 + 99) // 100
        or not 0 < gas <= detail["gas_cap"] <= MAX_CONFIGURED_GAS
        or not 0 < price
        or price % 2 != 0
        or (price // 2) * 3 > MAX_PRICE
        or detail["gas_price_cap"] != MAX_PRICE
        or detail["frozen_gas"] != frozen["gas"]
        or detail["frozen_gas_price"] != frozen["gas_price"]
        or frozen["maximum_cost_wei"]
        != detail["frozen_gas"] * detail["frozen_gas_price"]
    ):
        raise Stop(reason)
    gas_sufficient = gas <= frozen["gas"] <= detail["gas_cap"]
    fee_sufficient = price <= frozen["gas_price"] <= MAX_PRICE
    if (
        detail["gas_sufficient"] != gas_sufficient
        or detail["fee_sufficient"] != fee_sufficient
        or (gas_sufficient and fee_sufficient)
    ):
        raise Stop(reason)


class GitHubRecovery:
    def __init__(self, evidence_dir):
        self.path = Path(evidence_dir)
        self.path.mkdir(parents=True, exist_ok=False)
        self.archive_sha = None

    def api(self, endpoint, *, body=None, binary=False, missing=False):
        command = ["gh", "api", "repos/" + REPO + "/" + endpoint]
        if body is not None:
            command += ["--method", "POST", "--input", "-"]
        if missing:
            command += ["--include"]
        result = subprocess.run(
            command,
            input=json.dumps(body).encode() if body is not None else None,
            capture_output=True,
            timeout=30,
        )
        if missing:
            # Only an authenticated HTTP 404 means absent. Transport/403 stays unknown.
            header, _, payload = result.stdout.partition(b"\r\n\r\n")
            if not payload:
                header, _, payload = result.stdout.partition(b"\n\n")
            first = header.splitlines()[0] if header else b""
            if re.match(rb"HTTP/\S+ 404(?: |$)", first):
                return None
            if result.returncode or not re.match(rb"HTTP/\S+ 200(?: |$)", first):
                raise Stop("recovery_github_read_unknown")
            return json.loads(payload)
        if result.returncode:
            raise Stop("recovery_github_operation_failed")
        if binary:
            if len(result.stdout) > 1_000_000:
                raise Stop("recovery_archive_too_large")
            return result.stdout
        return json.loads(result.stdout)

    @staticmethod
    def original_key(auth):
        f = auth["intent"]
        return "eth_sepolia-" + f["contract"].lower() + "-" + f["compose_hash"].lower()

    def artifact(self, artifact_id, auth, expected_name):
        if type(artifact_id) is not int or artifact_id <= 0:
            raise Stop("recovery_artifact_id")
        meta = self.api("actions/artifacts/" + str(artifact_id))
        if (
            meta.get("expired")
            or meta.get("name") != expected_name
            or meta.get("workflow_run", {}).get("id") != auth["source_run"]
            or meta.get("size_in_bytes", 0) <= 0
        ):
            raise Stop("recovery_archive_identity")
        raw = self.api("actions/artifacts/" + str(artifact_id) + "/zip", binary=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 12:
                raise Stop("recovery_archive_entries")
            result = {}
            for name in names:
                if archive.getinfo(name).is_dir():
                    continue
                if archive.getinfo(name).file_size > 16384:
                    raise Stop("recovery_archive_entry_size")
                result[name] = archive.read(name)
        return result

    def original_intent(self, auth):
        key = self.original_key(auth)
        files = self.artifact(auth["intent_artifact"], auth, "compose-intent-" + key)
        if set(files) != {key + "/intent.json"}:
            raise Stop("recovery_intent_archive_shape")
        return files[key + "/intent.json"]

    def verify_original(self, auth, intent_raw):
        run = self.api("actions/runs/" + str(auth["source_run"]))
        if (
            run.get("head_sha") != auth["source_sha"]
            or run.get("head_branch") != "test"
            or run.get("path") != ".github/workflows/ci.yml"
            or run.get("run_attempt") != 1
            or run.get("status") != "completed"
            or run.get("conclusion") != "failure"
        ):
            raise Stop("recovery_source_run_binding")
        key = self.original_key(auth)
        evidence = self.artifact(
            auth["evidence_artifact"],
            auth,
            f'compose-publish-{auth["source_run"]}-deploy-test-runner-cvm-1',
        )
        required = {key + "/intent.json", key + "/failure.json"}
        diagnostic = key + "/frozen-limits-check.json"
        if set(evidence) not in (required, required | {diagnostic}):
            # Only the qualified diagnostic may accompany the original two files.
            # Claims, broadcast evidence, unknown entries and incomplete sets fail closed.
            raise Stop("recovery_original_broadcast_or_unknown")
        if evidence[key + "/intent.json"] != intent_raw:
            raise Stop("recovery_original_intent_changed")
        if json.loads(evidence[key + "/failure.json"]) != {
            "result": "STOP",
            "reason": "frozen_limits_insufficient",
            "retry_authorized": False,
        }:
            raise Stop("recovery_original_stop_unqualified")
        if diagnostic in evidence:
            verify_frozen_limits_diagnostic(evidence[diagnostic], auth["intent"])
        # One complete original intent history, including expired artifacts.
        from urllib.parse import urlencode

        history = self.api(
            "actions/artifacts?"
            + urlencode({"name": "compose-intent-" + key, "per_page": 100})
        )
        artifacts = history.get("artifacts", [])
        if history.get("total_count") != len(artifacts) or len(artifacts) != 1:
            raise Stop("recovery_prior_history_unknown")
        if artifacts[0].get("id") != auth["intent_artifact"]:
            raise Stop("recovery_prior_intent_conflict")

    def verify_execution_window(self, auth):
        # The test runner lock alone does not cover the shared signing account.
        # Require a drained repository; the operator must also prevent new deploy
        # admissions throughout the window. This is a fail-closed precondition,
        # not an atomic lock against administrators starting another workflow.
        head = self.api("git/ref/heads/test")
        if head.get("object", {}).get("sha") != auth["execution_sha"]:
            raise Stop("recovery_test_head_changed")
        own = int(os.environ["GITHUB_RUN_ID"])
        for status in ("queued", "in_progress", "waiting", "pending", "requested"):
            runs = self.api("actions/runs?status=" + status + "&per_page=100")
            rows = runs.get("workflow_runs")
            if not isinstance(rows, list) or runs.get("total_count") != len(rows):
                raise Stop("recovery_workflow_history_unknown")
            if any(row.get("id") != own for row in rows):
                raise Stop("recovery_concurrent_workflow")

    def verify_live_target(self, frozen):
        if not os.environ.get("PHALA_CLOUD_API_KEY"):
            raise Stop("recovery_missing_test_cvm_key")
        proc = subprocess.run(
            ["phala", "cvms", "get", "0f065d29-37c6-4c79-b871-04e526c6c91d", "-j"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode:
            raise Stop("recovery_live_cvm_unknown")
        value = json.loads(proc.stdout).get("compose_hash", "")
        if "0x" + value.removeprefix("0x").lower() != frozen["compose_hash"].lower():
            raise Stop("recovery_live_compose_changed")

    def claim_exists(self, key):
        return (
            self.api("git/ref/tags/compose-recovery-claim-" + key, missing=True)
            is not None
        )

    def archive(self, auth, authorization, intent_raw, check):
        evidence = {
            "authorization_base64": base64.b64encode(authorization).decode(),
            "authorization_sha256": hashlib.sha256(authorization).hexdigest(),
            "intent_base64": base64.b64encode(intent_raw).decode(),
            "preflight": check,
            "recovery_run": os.environ["GITHUB_RUN_ID"],
            "source_code_sha": os.environ["GITHUB_SHA"],
        }
        self.path.joinpath("evidence.json").write_text(json.dumps(evidence, indent=2))
        tag = self.api(
            "git/tags",
            body={
                "tag": "compose-recovery-evidence-" + auth["recovery_id"],
                "message": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                "object": os.environ["GITHUB_SHA"],
                "type": "commit",
            },
        )
        self.archive_sha = tag["sha"]
        self.api(
            "git/refs",
            body={
                "ref": "refs/tags/compose-recovery-evidence-" + auth["recovery_id"],
                "sha": self.archive_sha,
            },
        )
        self.path.joinpath("archive.json").write_text(
            json.dumps({"tag_object": self.archive_sha})
        )

    def claim_once(self, key, auth):
        if not self.archive_sha:
            raise Stop("recovery_evidence_not_archived")
        # GitHub ref creation is atomic across runners/directories; never PATCH or DELETE.
        self.api(
            "git/refs",
            body={
                "ref": "refs/tags/compose-recovery-claim-" + key,
                "sha": self.archive_sha,
            },
        )
        self.path.joinpath("claim.json").write_text(
            json.dumps({"key": key, "archive": self.archive_sha})
        )

    def before_broadcast(self, key, auth, tx_hash):
        claim = self.api("git/ref/tags/compose-recovery-claim-" + key)
        if claim.get("object", {}).get("sha") != self.archive_sha:
            raise Stop("recovery_claim_owner_changed")
        self.api(
            "git/refs",
            body={
                "ref": "refs/tags/compose-recovery-broadcast-" + key,
                "sha": self.archive_sha,
            },
        )
        self.path.joinpath("broadcast-started.json").write_text(
            json.dumps({"transaction_hash": tx_hash})
        )

    def receipt(self, receipt):
        safe = {
            k: receipt.get(k)
            for k in (
                "transactionHash",
                "status",
                "blockNumber",
                "blockHash",
                "gasUsed",
                "from",
                "to",
            )
        }
        self.path.joinpath("receipt.json").write_text(json.dumps(safe, indent=2))
