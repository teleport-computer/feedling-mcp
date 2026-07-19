from __future__ import annotations

import json

import pytest

from qa import validate_cleanup_receipt as validator


def _row(
    profile_id: str,
    *,
    provider_config_preexisted: bool = True,
    provider_config_live_predelete_observed: bool = True,
    provider_config_deletion_source: str = "explicit_api",
) -> dict:
    return {
        "profile_id": profile_id,
        "attempted": True,
        "reset_response_accepted": True,
        "provider_config_preexisted": provider_config_preexisted,
        "provider_config_live_predelete_observed": (
            provider_config_live_predelete_observed
        ),
        "provider_config_deleted": True,
        "key_envelope_deleted": True,
        "provider_config_deletion_source": provider_config_deletion_source,
        "account_reset": True,
        "old_credential_rejected": True,
        "user_absence_verified": True,
        "status": "PASS",
    }


def _write_pair(tmp_path):
    receipt = {
        "schema_version": 1,
        "kind": "deterministic_cleanup_receipt",
        "run_id": "unit-run-0001",
        "generated_at": "2026-07-15T12:00:00+00:00",
        "attempted": 10,
        "cleaned": 10,
        "failed_profile_ids": [],
        "manifest_deleted": False,
        "manifest_retained_for_scan": True,
        "profiles": [_row(profile_id) for profile_id in validator.PROFILE_IDS],
        "auxiliary_accounts": [
            _row(
                validator.MEMORY_CONTRACT_PROFILE_ID,
                provider_config_preexisted=False,
                provider_config_live_predelete_observed=False,
                provider_config_deletion_source="not_applicable",
            )
        ],
    }
    result = {
        "run_id": "unit-run-0001",
        "profiles": [
            {
                "profile_id": profile_id,
                "cleanup": {
                    "attempted": True,
                    "provider_config_deleted": True,
                    "account_reset": True,
                    "old_credential_rejected": True,
                    "status": "PASS",
                },
            }
            for profile_id in validator.PROFILE_IDS
        ],
    }
    receipt_path = tmp_path / "cleanup-receipt.json"
    result_path = tmp_path / "run-result.json"
    receipt_path.write_text(json.dumps(receipt))
    result_path.write_text(json.dumps(result))
    return receipt_path, result_path


def test_valid_cleanup_receipt_binds_canonical_result(tmp_path):
    receipt_path, result_path = _write_pair(tmp_path)

    validator.validate_cleanup_receipt(receipt_path, result_path)


def test_cleanup_receipt_accepts_admin_verified_account_cascade(tmp_path):
    receipt_path, result_path = _write_pair(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    receipt["profiles"][0].update(
        provider_config_live_predelete_observed=False,
        provider_config_deletion_source="account_cascade",
    )
    receipt_path.write_text(json.dumps(receipt))

    validator.validate_cleanup_receipt(receipt_path, result_path)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda receipt: receipt.update(cleaned=9), "summary"),
        (
            lambda receipt: receipt["profiles"][0].update(
                old_credential_rejected=False
            ),
            "proof",
        ),
        (
            lambda receipt: receipt["profiles"].reverse(),
            "matrix is not locked",
        ),
        (lambda receipt: receipt.update(run_id="other-run"), "run identity"),
    ],
)
def test_cleanup_receipt_rejects_incomplete_or_unbound_proof(tmp_path, mutation, match):
    receipt_path, result_path = _write_pair(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    mutation(receipt)
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(validator.CleanupReceiptError, match=match):
        validator.validate_cleanup_receipt(receipt_path, result_path)


def test_cleanup_receipt_rejects_agent_cleanup_claim_mismatch(tmp_path):
    receipt_path, result_path = _write_pair(tmp_path)
    result = json.loads(result_path.read_text())
    result["profiles"][0]["cleanup"]["account_reset"] = False
    result_path.write_text(json.dumps(result))

    with pytest.raises(validator.CleanupReceiptError, match="canonical result cleanup"):
        validator.validate_cleanup_receipt(receipt_path, result_path)


def test_cleanup_receipt_rejects_symlink(tmp_path):
    receipt_path, result_path = _write_pair(tmp_path)
    target = tmp_path / "target.json"
    receipt_path.rename(target)
    receipt_path.symlink_to(target)

    with pytest.raises(validator.CleanupReceiptError, match="missing or unsafe"):
        validator.validate_cleanup_receipt(receipt_path, result_path)
