"""Explicit, single-attempt recovery of an archived test transaction.

This is not a retry mode for the normal publisher. Authorization is an exact
operator-reviewed document; storage and RPC adapters are injected for offline
proofs. No repricing, changed transaction, or implicit recovery is permitted.
"""

import hashlib
import json
import re
import time
import uuid

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from compose_publisher import MAX_CONFIGURED_GAS, MAX_PRICE, Stop, gas_limit

TEST_CONTRACT = "0x9ac034aaef6bb80690be4d1f698b51796bb7f2d5"


def read_authorization(raw, expected_digest, intent_raw, *, now):
    if len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != expected_digest:
        raise Stop("recovery_authorization_digest")
    auth = json.loads(raw)
    required = {
        "version",
        "environment",
        "recovery_id",
        "issued_at",
        "expires_at",
        "source_run",
        "source_sha",
        "execution_sha",
        "intent_artifact",
        "evidence_artifact",
        "intent_sha256",
        "intent",
    }
    if not isinstance(auth, dict) or set(auth) != required:
        raise Stop("recovery_authorization_fields")
    if (
        type(auth["version"]) is not int
        or auth["version"] != 1
        or auth["environment"] != "test"
    ):
        raise Stop("recovery_test_only")
    try:
        if str(uuid.UUID(auth["recovery_id"])) != auth["recovery_id"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise Stop("recovery_id_invalid") from None
    for key in (
        "issued_at",
        "expires_at",
        "source_run",
        "intent_artifact",
        "evidence_artifact",
    ):
        if type(auth[key]) is not int or auth[key] <= 0:
            raise Stop("recovery_authorization_integer")
    if not auth["issued_at"] <= now < auth["expires_at"] <= auth["issued_at"] + 3600:
        raise Stop("recovery_expired_or_not_yet_valid")
    if not all(
        re.fullmatch(r"[0-9a-f]{40}", auth[k]) for k in ("source_sha", "execution_sha")
    ):
        raise Stop("recovery_source_sha")
    if hashlib.sha256(intent_raw).hexdigest() != auth["intent_sha256"]:
        raise Stop("recovery_intent_digest")
    frozen = json.loads(intent_raw)
    fields = {
        "transaction_hash",
        "chain_id",
        "contract",
        "owner",
        "compose_hash",
        "commit",
        "yaml_url",
        "calldata_sha256",
        "nonce",
        "estimate",
        "gas",
        "gas_price",
        "maximum_cost_wei",
    }
    if (
        not isinstance(frozen, dict)
        or set(frozen) != fields
        or frozen != auth["intent"]
    ):
        raise Stop("recovery_intent_binding")
    if frozen["chain_id"] != 11155111 or frozen["contract"].lower() != TEST_CONTRACT:
        raise Stop("recovery_test_target_only")
    for key, width in (("transaction_hash", 64), ("compose_hash", 64), ("owner", 40)):
        if not re.fullmatch(r"0x[0-9a-fA-F]{%d}" % width, frozen[key]):
            raise Stop("recovery_invalid_identity")
    for key, width in (("commit", 40), ("calldata_sha256", 64)):
        if not re.fullmatch(r"[0-9a-f]{%d}" % width, frozen[key]):
            raise Stop("recovery_invalid_identity")
    expected_url = (
        "https://github.com/teleport-computer/feedling-mcp/raw/"
        + frozen["commit"]
        + "/deploy/docker-compose.phala.runner.yaml"
    )
    if frozen["yaml_url"] != expected_url:
        raise Stop("recovery_runner_url_binding")
    for key in ("nonce", "estimate", "gas", "gas_price", "maximum_cost_wei"):
        if type(frozen[key]) is not int or frozen[key] < 0:
            raise Stop("recovery_invalid_limits")
    if not (
        gas_limit(frozen["estimate"], MAX_CONFIGURED_GAS)
        <= frozen["gas"]
        <= MAX_CONFIGURED_GAS
        and 0 < frozen["gas_price"] <= MAX_PRICE
        and frozen["maximum_cost_wei"] == frozen["gas"] * frozen["gas_price"]
    ):
        raise Stop("recovery_invalid_limits")
    return auth


def target_key(frozen):
    # Deliberately excludes recovery ID/run/path: a new directory cannot reset it.
    return hashlib.sha256(
        (
            str(frozen["chain_id"])
            + ":"
            + frozen["contract"].lower()
            + ":"
            + frozen["compose_hash"].lower()
        ).encode()
    ).hexdigest()


def recover(
    *,
    authorization,
    expected_digest,
    store,
    rpc,
    signer_factory,
    now=time.time,
    sleep=time.sleep
):
    """Store must verify archives and atomically claim the global target once.

    A consumed claim is never released, including failed signing or ambiguous
    broadcast. Future invocations may only report ALREADY_AUTHORIZED with zero
    sends; they cannot resume or resend a claimed transaction.
    """
    if (
        len(authorization) > 16384
        or hashlib.sha256(authorization).hexdigest() != expected_digest
    ):
        raise Stop("recovery_authorization_digest")
    raw_auth = json.loads(authorization)
    intent_raw = store.original_intent(raw_auth)
    auth = read_authorization(authorization, expected_digest, intent_raw, now=now())
    f = auth["intent"]
    # Includes original final journal, run/attempt/target, complete prior history.
    store.verify_original(auth, intent_raw)
    data = (
        "0x"
        + (
            keccak(text="addComposeHash(bytes32,string,string)")[:4]
            + encode(
                ["bytes32", "string", "string"],
                [bytes.fromhex(f["compose_hash"][2:]), f["commit"], f["yaml_url"]],
            )
        ).hex()
    )
    if hashlib.sha256(bytes.fromhex(data[2:])).hexdigest() != f["calldata_sha256"]:
        raise Stop("recovery_calldata_changed")
    allowed_data = (
        "0x" + keccak(text="isAppAllowed(bytes32)")[:4].hex() + f["compose_hash"][2:]
    )

    def allowed():
        value = rpc("eth_call", [{"to": f["contract"], "data": allowed_data}, "latest"])
        if value not in ("0x" + "0" * 64, "0x" + "0" * 63 + "1"):
            raise Stop("recovery_authorization_read_unknown")
        return value.endswith("1")

    def identities():
        if int(rpc("eth_chainId", []), 16) != f["chain_id"]:
            raise Stop("recovery_wrong_chain")
        owner = rpc("eth_call", [{"to": f["contract"], "data": "0x8da5cb5b"}, "latest"])
        if owner.lower() != "0x" + "0" * 24 + f["owner"][2:].lower():
            raise Stop("recovery_wrong_owner")

    call = {
        "from": f["owner"],
        "to": f["contract"],
        "data": data,
        "value": "0x0",
        "gas": hex(f["gas"]),
    }

    def preflight():
        if not auth["issued_at"] <= now() < auth["expires_at"]:
            raise Stop("recovery_expired_or_not_yet_valid")
        store.verify_execution_window(auth)
        store.verify_live_target(f)
        identities()
        # Any observation of an existing transaction is reconciliation, not permission.
        if rpc("eth_getTransactionReceipt", [f["transaction_hash"]]) is not None:
            raise Stop("recovery_receipt_exists")
        if rpc("eth_getTransactionByHash", [f["transaction_hash"]]) is not None:
            raise Stop("recovery_transaction_exists")
        if any(
            int(rpc("eth_getTransactionCount", [f["owner"], tag]), 16) != f["nonce"]
            for tag in ("latest", "pending")
        ):
            raise Stop("recovery_nonce_conflict")
        estimate = int(rpc("eth_estimateGas", [call, "latest"]), 16)
        price = int(rpc("eth_gasPrice", []), 16)
        if (
            gas_limit(estimate, MAX_CONFIGURED_GAS) > f["gas"]
            or not 0 < price * 3 <= MAX_PRICE
            or not 0 < price * 2 <= f["gas_price"]
        ):
            raise Stop("recovery_frozen_limits_insufficient")
        if rpc("eth_call", [call, "latest"]) != "0x":
            raise Stop("recovery_simulation_failed")
        return {"estimate": estimate, "gas_price": price, "checked_at": now()}

    identities()
    if allowed():
        return {"result": "ALREADY_AUTHORIZED", "sends": 0}
    key = target_key(f)
    if store.claim_exists(key):
        raise Stop("recovery_target_already_claimed")
    check = preflight()
    # Immutable remote evidence must exist before claim and before key access.
    store.archive(auth, authorization, intent_raw, check)
    preflight()
    if allowed():
        return {"result": "ALREADY_AUTHORIZED", "sends": 0}
    store.claim_once(key, auth)  # Atomic and global; throws for the losing contender.
    signer = signer_factory()
    if signer.address.lower() != f["owner"].lower():
        raise Stop("recovery_signer_mismatch")
    tx = {
        "chainId": f["chain_id"],
        "nonce": f["nonce"],
        "to": to_checksum_address(f["contract"]),
        "data": data,
        "value": 0,
        "gas": f["gas"],
        "gasPrice": f["gas_price"],
    }
    signed = signer.sign_transaction(tx)
    tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
    if (
        tx_hash.lower() != f["transaction_hash"].lower()
        or "0x" + keccak(signed.raw_transaction).hex() != tx_hash.lower()
    ):
        raise Stop("recovery_signed_hash_mismatch")
    preflight()
    if allowed():
        return {"result": "ALREADY_AUTHORIZED", "sends": 0}
    store.before_broadcast(key, auth, tx_hash)
    store.verify_execution_window(auth)
    if not auth["issued_at"] <= now() < auth["expires_at"]:
        raise Stop("recovery_expired_or_not_yet_valid")
    returned = rpc(
        "eth_sendRawTransaction",
        ["0x" + signed.raw_transaction.hex().removeprefix("0x")],
    )
    if returned.lower() != tx_hash.lower():
        raise Stop("recovery_broadcast_hash_unknown")
    for _ in range(12):
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            if (
                receipt.get("transactionHash", "").lower() != tx_hash.lower()
                or receipt.get("from", "").lower() != f["owner"].lower()
                or receipt.get("to", "").lower() != f["contract"].lower()
                or receipt.get("status") != "0x1"
            ):
                raise Stop("recovery_receipt_failed_or_mismatched")
            store.receipt(receipt)
            if not allowed():
                raise Stop("recovery_readback_failed")
            return {"result": "AUTHORIZED", "transaction_hash": tx_hash, "sends": 1}
        sleep(5)
    raise Stop("recovery_receipt_timeout_no_resend")
