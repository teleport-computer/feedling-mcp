"""Real publisher failure artifacts through offline recovery; synthetic keys only."""

import json

import pytest

from test_compose_recovery import (
    RecoveryRPC,
    execute,
    publisher_tests,
    scenario,
    test_concurrent_recoveries_only_one_can_sign_and_send as concurrent_recovery,
)
from compose_publisher import Stop


@pytest.fixture(params=["gas", "fee", "both"])
def diagnosed(tmp_path, monkeypatch, scenario, request):
    """Use the actual CLI failure writer, not a manually constructed diagnosis."""
    auth, raw, backend = scenario
    frozen = auth["intent"]
    publisher = publisher_tests.p
    key = "eth_sepolia-" + frozen["contract"].lower() + "-" + frozen["compose_hash"]
    directory = tmp_path / "producer" / key
    directory.mkdir(parents=True)
    (directory / "intent.json").write_bytes(raw)
    rpc = RecoveryRPC()
    original_call = rpc.__call__

    def drift(method, args):
        if method == "eth_estimateGas" and request.param in {"gas", "both"}:
            return hex(frozen["estimate"] + 1)
        if method == "eth_gasPrice" and request.param in {"fee", "both"}:
            return hex(frozen["gas_price"] // 2 + 1)
        return original_call(method, args)

    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ATTEMPT": "1",
        "FEEDLING_PUBLISH_PHASE": "send",
        "FEEDLING_PUBLISH_CHAIN": "eth_sepolia",
        "FEEDLING_APP_AUTH_CONTRACT": frozen["contract"],
        "FEEDLING_COMPOSE_HASH": frozen["compose_hash"],
        "FEEDLING_GIT_COMMIT": frozen["commit"],
        "FEEDLING_COMPOSE_YAML_URL": frozen["yaml_url"],
        "FEEDLING_PUBLISH_EVIDENCE_DIR": str(directory.parent),
        "FEEDLING_PUBLISH_RPC_URL": "https://offline.invalid",
        "FEEDLING_PUBLISH_MAX_GAS": "3000000",
        "PRIVATE_KEY": "01" * 32,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(publisher, "RPC", lambda _: drift)
    monkeypatch.setattr(publisher, "prior_in_ci", lambda: None)
    monkeypatch.setattr(publisher, "archived_in_ci", lambda: None)
    assert publisher.main() == 1
    assert rpc.sent == 0
    assert {p.name for p in directory.iterdir()} == {
        "intent.json",
        "failure.json",
        "frozen-limits-check.json",
    }
    assert (directory / "intent.json").read_bytes() == raw
    assert json.loads((directory / "failure.json").read_bytes()) == {
        "result": "STOP",
        "reason": "frozen_limits_insufficient",
        "retry_authorized": False,
    }
    backend.journal_extra["frozen-limits-check.json"] = (
        directory / "frozen-limits-check.json"
    ).read_bytes()
    return scenario


def test_real_diagnostic_archive_recovers_once(tmp_path, diagnosed):
    rpc = RecoveryRPC()
    result, loads = execute(tmp_path / "recovery", diagnosed, rpc=rpc)
    assert result["sends"] == 1 and rpc.sent == 1 and loads == [True]
    assert diagnosed[2].claim_calls == 1
    assert result["transaction_hash"] == diagnosed[0]["intent"]["transaction_hash"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("result", "OK"),
        ("phase", "after_send"),
        ("sends", True),
        ("sends", 1),
        ("estimate", True),
        ("estimate", 0),
        ("estimate", "1829804"),
        ("required_gas", 1),
        ("frozen_gas", 1),
        ("gas_cap", 8000001),
        ("gas_cap", 0),
        ("required_gas_price", 1),
        ("required_gas_price", 0),
        ("gas_price_cap", 99999999),
        ("frozen_gas_price", 1),
        ("gas_sufficient", 0),
        ("fee_sufficient", 1),
    ],
)
def test_diagnostic_mutation_rejects_before_claim(tmp_path, diagnosed, field, value):
    backend = diagnosed[2]
    detail = json.loads(backend.journal_extra["frozen-limits-check.json"])
    detail[field] = value
    backend.journal_extra["frozen-limits-check.json"] = json.dumps(detail).encode()
    rpc = RecoveryRPC()
    with pytest.raises(Stop, match="recovery_invalid_frozen_limits_diagnostic"):
        execute(tmp_path / "recovery", diagnosed, rpc=rpc)
    assert rpc.sent == backend.claim_calls == 0


@pytest.mark.parametrize(
    "mutation",
    ["extra", "missing", "flip_gas", "flip_fee", "both_true", "not_object", "bad_json"],
)
def test_diagnostic_shape_and_boolean_semantics(tmp_path, diagnosed, mutation):
    backend = diagnosed[2]
    detail = json.loads(backend.journal_extra["frozen-limits-check.json"])
    if mutation == "extra":
        detail["extra"] = 0
    elif mutation == "missing":
        del detail["phase"]
    elif mutation.startswith("flip_"):
        field = mutation.removeprefix("flip_") + "_sufficient"
        detail[field] = not detail[field]
    elif mutation == "both_true":
        detail["gas_sufficient"] = detail["fee_sufficient"] = True
    elif mutation == "not_object":
        detail = []
    backend.journal_extra["frozen-limits-check.json"] = (
        b"{" if mutation == "bad_json" else json.dumps(detail).encode()
    )
    rpc = RecoveryRPC()
    with pytest.raises(Stop, match="recovery_invalid_frozen_limits_diagnostic"):
        execute(tmp_path / "recovery", diagnosed, rpc=rpc)
    assert rpc.sent == backend.claim_calls == 0


@pytest.mark.parametrize(
    "entry",
    [
        "claim.json",
        "broadcast-started.json",
        "receipt.json",
        "result.json",
        "unknown.json",
    ],
)
def test_diagnostic_does_not_allow_other_files(tmp_path, diagnosed, entry):
    diagnosed[2].journal_extra[entry] = b"{}"
    with pytest.raises(Stop, match="recovery_original_broadcast_or_unknown"):
        execute(tmp_path / "recovery", diagnosed)
    assert diagnosed[2].claim_calls == 0


def test_diagnostic_different_recovery_ids_share_claim(
    tmp_path, diagnosed, monkeypatch
):
    # Count actual synthetic transaction signatures after the fixture's preparation.
    original = publisher_tests.SIGNER
    signatures = []

    class CountingSigner:
        address = original.address

        def sign_transaction(self, tx):
            signatures.append(tx)
            return original.sign_transaction(tx)

    monkeypatch.setattr(publisher_tests, "SIGNER", CountingSigner())
    concurrent_recovery(tmp_path, diagnosed)
    assert len(signatures) == 1


@pytest.mark.parametrize(
    "change",
    ["expiry", "digest", "intent", "price", "gas", "live", "history", "archive"],
)
def test_diagnostic_preserves_existing_preconditions(tmp_path, diagnosed, change):
    from test_compose_recovery import (
        test_invalid_or_drifted_preconditions_never_broadcast,
    )

    test_invalid_or_drifted_preconditions_never_broadcast(tmp_path, diagnosed, change)


@pytest.mark.parametrize("mode", ["unknown", "timeout"])
def test_diagnostic_unknown_outcome_never_resends(tmp_path, diagnosed, mode):
    from test_compose_recovery import (
        test_broadcast_unknown_or_receipt_timeout_never_resends,
    )

    test_broadcast_unknown_or_receipt_timeout_never_resends(tmp_path, diagnosed, mode)


@pytest.mark.parametrize(
    "field",
    [
        "estimate",
        "required_gas",
        "frozen_gas",
        "gas_cap",
        "required_gas_price",
        "frozen_gas_price",
        "gas_price_cap",
        "sends",
    ],
)
def test_diagnostic_rejects_boolean_for_every_integer(tmp_path, diagnosed, field):
    test_diagnostic_mutation_rejects_before_claim(tmp_path, diagnosed, field, False)


def test_diagnostic_duplicate_json_key_rejects(tmp_path, diagnosed):
    backend = diagnosed[2]
    raw = backend.journal_extra["frozen-limits-check.json"]
    backend.journal_extra["frozen-limits-check.json"] = (
        b'{"sends":0,' + raw.lstrip()[1:]
    )
    with pytest.raises(Stop, match="recovery_invalid_frozen_limits_diagnostic"):
        execute(tmp_path / "recovery", diagnosed)
    assert backend.claim_calls == 0
