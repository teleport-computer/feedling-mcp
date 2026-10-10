"""Bounded compose authorization; one broadcast, durable local reconciliation evidence."""

import io
import zipfile
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time
import urllib.error
import urllib.request

from eth_account import Account
from eth_abi import encode
from eth_utils import keccak

CHAINS = {"eth_sepolia": 11155111, "base_sepolia": 84532, "base": 8453}
MAX_GAS = 3_000_000
MAX_PRICE = 100_000_000  # 0.1 gwei; hard cap, not the default price


class Stop(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Stop("rpc_redirect_refused")


class RPC:
    def __init__(self, url):
        if not url.startswith("https://"):
            raise Stop("rpc_https_required")
        self.url = url
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def __call__(self, method, params):
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "feedling-compose-publisher/1",
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(131073)
                if len(raw) > 131072:
                    raise Stop("rpc_response_size")
        except urllib.error.HTTPError as exc:
            raise Stop(f"{method}_http_{exc.code}") from None
        except urllib.error.URLError:
            raise Stop(f"{method}_transport_unknown") from None
        data = json.loads(raw)
        if "error" in data:
            code = data["error"].get("code")
            raise Stop(f'{method}_rpc_{code if isinstance(code,int) else "unknown"}')
        if "result" not in data:
            raise Stop("rpc_missing_result")
        return data["result"]


class Journal:
    def __init__(self, path):
        self.path = Path(path)

    def write(self, name, value):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.path / (name + ".json")).open("x") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def gas_limit(estimate):
    if type(estimate) is not int or estimate <= 0:
        raise Stop("invalid_gas_estimate")
    gas = (estimate * 120 + 99) // 100
    if gas > MAX_GAS:
        raise Stop("gas_cap_exceeded")
    return gas


def publish(
    rpc,
    signer,
    *,
    chain,
    contract,
    compose_hash,
    commit,
    yaml_url,
    journal,
    sleep=time.sleep,
    run_attempt="1",
    phase="publish",
    archived=lambda: None,
    prior=lambda: None,
):
    if (
        chain not in CHAINS
        or not re.fullmatch("0x[0-9a-fA-F]{40}", contract)
        or not re.fullmatch("0x[0-9a-fA-F]{64}", compose_hash)
        or not re.fullmatch("[0-9a-f]{40}", commit)
    ):
        raise Stop("invalid_target")
    if not yaml_url.startswith("https://") or len(yaml_url) > 2048:
        raise Stop("invalid_yaml_url")
    owner = signer.address.lower()
    if int(rpc("eth_chainId", []), 16) != CHAINS[chain]:
        raise Stop("wrong_chain")
    onchain_owner = rpc("eth_call", [{"to": contract, "data": "0x8da5cb5b"}, "latest"])
    if onchain_owner.lower() != "0x" + "0" * 24 + owner[2:]:
        raise Stop("wrong_owner")
    allowed_data = (
        "0x" + keccak(text="isAppAllowed(bytes32)")[:4].hex() + compose_hash[2:]
    )

    def allowed():
        value = rpc("eth_call", [{"to": contract, "data": allowed_data}, "latest"])
        if value not in ("0x" + "0" * 64, "0x" + "0" * 63 + "1"):
            raise Stop("invalid_authorization_response")
        return value.endswith("1")

    if allowed():
        return {"result": "ALREADY_AUTHORIZED", "sends": 0}
    if run_attempt != "1":
        raise Stop("workflow_rerun_requires_reconciliation")
    if phase not in ("publish", "prepare", "send"):
        raise Stop("invalid_phase")
    prior()
    if (
        phase == "prepare"
        and (journal.path / "intent.json").exists()
        and not (journal.path / "claim.json").exists()
    ):
        existing = json.loads((journal.path / "intent.json").read_text())
        if any(
            existing.get(k) != v
            for k, v in {
                "compose_hash": compose_hash,
                "contract": contract,
                "commit": commit,
                "yaml_url": yaml_url,
                "owner": owner,
                "chain_id": CHAINS[chain],
            }.items()
        ):
            raise Stop("existing_prepared_target_changed")
        return {
            "result": "PREPARED_NOT_SENT",
            "transaction_hash": existing["transaction_hash"],
            "sends": 0,
        }
    if (journal.path / "claim.json").exists() or (
        phase != "send" and (journal.path / "intent.json").exists()
    ):
        raise Stop("prior_attempt_requires_reconciliation")
    frozen = None
    if phase == "send":
        archived()
        frozen = json.loads((journal.path / "intent.json").read_text())
    data = (
        "0x"
        + (
            keccak(text="addComposeHash(bytes32,string,string)")[:4]
            + encode(
                ["bytes32", "string", "string"],
                [bytes.fromhex(compose_hash[2:]), commit, yaml_url],
            )
        ).hex()
    )
    latest = int(rpc("eth_getTransactionCount", [owner, "latest"]), 16)
    if int(rpc("eth_getTransactionCount", [owner, "pending"]), 16) != latest:
        raise Stop("pending_nonce_conflict")
    call = {
        "from": owner,
        "to": contract,
        "data": data,
        "value": "0x0",
        "gas": hex(MAX_GAS),
    }
    estimate = int(rpc("eth_estimateGas", [call, "latest"]), 16)
    gas = gas_limit(estimate)
    price = int(rpc("eth_gasPrice", []), 16) * 3  # bounded fixed headroom; no repricing
    if not 0 < price <= MAX_PRICE:
        raise Stop("fee_cap_exceeded")
    if frozen is not None:
        if (
            not gas <= frozen["gas"] <= MAX_GAS
            or not (price // 3) * 2 <= frozen["gas_price"] <= MAX_PRICE
        ):
            raise Stop("frozen_limits_insufficient")
        gas, price = frozen["gas"], frozen["gas_price"]
        if latest != frozen["nonce"]:
            raise Stop("frozen_nonce_changed")
    call["gas"] = hex(gas)
    if rpc("eth_call", [call, "latest"]) != "0x":
        raise Stop("simulation_failed")
    if allowed():
        return {"result": "ALREADY_AUTHORIZED", "sends": 0}
    if gas_limit(int(rpc("eth_estimateGas", [call, "latest"]), 16)) > gas:
        raise Stop("gas_estimate_increased")
    if int(rpc("eth_gasPrice", []), 16) * 2 > price:
        raise Stop("fee_changed_beyond_headroom")
    if any(
        int(rpc("eth_getTransactionCount", [owner, tag]), 16) != latest
        for tag in ("latest", "pending")
    ):
        raise Stop("nonce_changed")
    tx = {
        "chainId": CHAINS[chain],
        "nonce": latest,
        "to": contract,
        "data": data,
        "value": 0,
        "gas": gas,
        "gasPrice": price,
    }
    signed = signer.sign_transaction(tx)
    tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
    intent = {
        "transaction_hash": tx_hash,
        "chain_id": CHAINS[chain],
        "contract": contract,
        "owner": owner,
        "compose_hash": compose_hash,
        "commit": commit,
        "yaml_url": yaml_url,
        "calldata_sha256": hashlib.sha256(bytes.fromhex(data[2:])).hexdigest(),
        "nonce": latest,
        "estimate": estimate if frozen is None else frozen["estimate"],
        "gas": gas,
        "gas_price": price,
        "maximum_cost_wei": gas * price,
    }
    if frozen is not None and intent != frozen:
        raise Stop("frozen_intent_changed")
    if phase == "prepare":
        journal.write("intent", intent)
        return {"result": "PREPARED_NOT_SENT", "transaction_hash": tx_hash, "sends": 0}
    # Exclusive claim and fsync before the only send. Never persist key/raw signed bytes.
    journal.write("claim", {"result": "ATTEMPT_CLAIMED", "transaction_hash": tx_hash})
    if frozen is None:
        journal.write("intent", intent)
    journal.write(
        "broadcast-started",
        {"transaction_hash": tx_hash, "result": "OUTCOME_UNKNOWN_UNTIL_RECEIPT"},
    )
    returned = rpc(
        "eth_sendRawTransaction",
        ["0x" + signed.raw_transaction.hex().removeprefix("0x")],
    )
    if returned.lower() != tx_hash.lower():
        raise Stop("broadcast_hash_mismatch_reconcile")
    for _ in range(12):
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            safe = {
                key: receipt.get(key)
                for key in (
                    "transactionHash",
                    "status",
                    "blockNumber",
                    "blockHash",
                    "gasUsed",
                    "from",
                    "to",
                )
            }
            journal.write("receipt", safe)
            if (
                receipt.get("transactionHash", "").lower() != tx_hash.lower()
                or receipt.get("from", "").lower() != owner
                or receipt.get("to", "").lower() != contract.lower()
            ):
                raise Stop("receipt_identity_mismatch")
            if receipt.get("status") != "0x1":
                reason = (
                    "receipt_failed_gas_exhausted"
                    if int(receipt.get("gasUsed", "0x0"), 16) == gas
                    else "receipt_reverted"
                )
                raise Stop(reason)
            if not allowed():
                raise Stop("authorization_readback_failed")
            result = {"result": "AUTHORIZED", "transaction_hash": tx_hash, "sends": 1}
            journal.write("result", result)
            return result
        sleep(5)
    raise Stop("receipt_timeout_reconcile_no_resend")


def target_key():
    return (
        os.environ["FEEDLING_PUBLISH_CHAIN"]
        + "-"
        + os.environ["FEEDLING_APP_AUTH_CONTRACT"].lower()
        + "-"
        + os.environ["FEEDLING_COMPOSE_HASH"].lower()
    )


def prior_in_ci():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    result = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "repos/" + os.environ["GITHUB_REPOSITORY"] + "/actions/artifacts",
            "-f",
            "name=compose-intent-" + target_key(),
            "-f",
            "per_page=100",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    data = json.loads(result.stdout)
    if data["total_count"] != len(data["artifacts"]):
        raise Stop("prior_artifact_history_incomplete")
    for artifact in data["artifacts"]:
        if str(artifact["workflow_run"]["id"]) != os.environ["GITHUB_RUN_ID"]:
            raise Stop("prior_ci_intent_requires_reconciliation")


def archived_in_ci():
    artifact_id = os.environ.get("FEEDLING_PUBLISH_ARTIFACT_ID", "")
    if not artifact_id.isdecimal():
        raise Stop("intent_not_archived")
    result = subprocess.run(
        [
            "gh",
            "api",
            "repos/"
            + os.environ["GITHUB_REPOSITORY"]
            + "/actions/artifacts/"
            + artifact_id,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    a = json.loads(result.stdout)
    expected = "compose-intent-" + target_key()
    if (
        a["expired"]
        or a["size_in_bytes"] <= 0
        or a["name"] != expected
        or str(a["workflow_run"]["id"]) != os.environ["GITHUB_RUN_ID"]
    ):
        raise Stop("intent_archive_identity_mismatch")
    # Fetch into a fresh in-memory archive on every invocation: no stale files
    # or gh download overwrite behavior can stand in for the frozen intent.
    result = subprocess.run(
        [
            "gh",
            "api",
            "repos/"
            + os.environ["GITHUB_REPOSITORY"]
            + "/actions/artifacts/"
            + artifact_id
            + "/zip",
        ],
        capture_output=True,
        timeout=20,
        check=True,
    )
    if len(result.stdout) > 1_000_000:
        raise Stop("intent_archive_too_large")
    with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
        name = target_key() + "/intent.json"
        if (
            archive.namelist().count(name) != 1
            or archive.getinfo(name).file_size > 16384
        ):
            raise Stop("intent_archive_content_missing")
        local = Path(os.environ["FEEDLING_PUBLISH_EVIDENCE_DIR"]) / name
        if archive.read(name) != local.read_bytes():
            raise Stop("intent_archive_content_mismatch")


def main():
    os.umask(0o077)
    journal = None
    try:
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get(
            "FEEDLING_PUBLISH_PHASE"
        ) not in ("prepare", "send"):
            raise Stop("ci_archive_phase_required")
        chain = os.environ["FEEDLING_PUBLISH_CHAIN"]
        target = os.environ["FEEDLING_APP_AUTH_CONTRACT"]
        compose = os.environ["FEEDLING_COMPOSE_HASH"]
        journal = Journal(
            Path(os.environ["FEEDLING_PUBLISH_EVIDENCE_DIR"])
            / (chain + "-" + target.lower() + "-" + compose.lower())
        )
        result = publish(
            RPC(
                os.environ.get("FEEDLING_PUBLISH_RPC_URL")
                or os.environ[chain.upper() + "_RPC_URL"]
            ),
            Account.from_key(os.environ["PRIVATE_KEY"]),
            chain=chain,
            contract=target,
            compose_hash=compose,
            commit=os.environ["FEEDLING_GIT_COMMIT"],
            yaml_url=os.environ["FEEDLING_COMPOSE_YAML_URL"],
            journal=journal,
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
            phase=os.environ.get("FEEDLING_PUBLISH_PHASE", "publish"),
            archived=archived_in_ci,
            prior=prior_in_ci,
        )
        if os.environ.get("GITHUB_OUTPUT") and result["result"] == "PREPARED_NOT_SENT":
            key = target_key()
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write("target_key=" + key + "\n")
        print(json.dumps(result))
        return 0
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, Stop) else type(exc).__name__
        failure = {"result": "STOP", "reason": reason, "retry_authorized": False}
        if journal is not None:
            try:
                journal.write("failure", failure)
            except Exception:
                pass  # Preserve the original failure even if evidence storage failed.
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
