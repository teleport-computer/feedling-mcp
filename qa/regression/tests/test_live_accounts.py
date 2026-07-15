from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qa.regression import live_accounts


BUILD_SHA = "a" * 40
PERSONA_SHA = "b" * 64
SOURCE_SHA = "c" * 64


def _private_dir(tmp_path: Path, name: str = "private") -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write_private(path: Path, payload: dict, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(mode)
    return path


def _pool_payload(count: int = 2) -> dict:
    now = datetime.now(timezone.utc)
    profiles = []
    for index in range(1, count + 1):
        profiles.append(
            {
                "profile_id": "official-openai",
                "pool_index": index,
                "provider": "openai",
                "route_family": "official",
                "configured_model": "gpt-5.4",
                "configured_base_url": "https://api.openai.com/v1",
                "reasoning_effort": "medium",
                "label": f"agent-e2e-test-official-openai-{index:03d}",
                "user_id": f"user-{index}",
                "api_key": f"feedling-key-{index}",
                "secret_key_b64": base64.b64encode(bytes([index]) * 32).decode(),
                "public_key_b64": base64.b64encode(bytes([index + 20]) * 32).decode(),
                "trace_enabled": True,
                "runtime_mode": "hosted_resident",
                "runtime_version": 2,
                "registration_verified": True,
                "fresh_state_verified": True,
                "invalid_key_rejected": True,
                "valid_key_configured": True,
                "runtime_mode_readback_verified": True,
                "runtime_mode_set_required": False,
                "runtime_mode_set_verified": False,
                "runtime_readback_receipt": {
                    "configured": True,
                    "runtime_mode": "hosted_resident",
                    "runtime_version": 2,
                },
                "provision_status": "ready",
                "provision_failure_code": "NONE",
                "synthetic_account_lease": {
                    "registered": True,
                    "lease_id": f"lease_{index:032x}",
                    "absence_token": f"{index:064x}",
                    "expires_at": (now + timedelta(hours=3)).isoformat(),
                    "expires_at_epoch": int((now + timedelta(hours=3)).timestamp()),
                    "ttl_seconds": 14_400,
                },
            }
        )
    return {
        "schema_version": 1,
        "manifest_kind": live_accounts.POOL_KIND,
        "generated_at": now.isoformat(),
        "base_url": live_accounts.LOCKED_BASE_URL,
        "runtime_mode": "hosted_resident",
        "pool_profile_id": "official-openai",
        "pool_count": count,
        "synthetic_account_reaper": {
            "enabled": True,
            "ready": True,
            "heartbeat_fresh": True,
            "label_prefix": "agent-e2e-",
            "max_ttl_seconds": 14_400,
        },
        "profiles": profiles,
        "auxiliary_accounts": [],
    }


def _pool(tmp_path: Path, count: int = 2) -> live_accounts.AccountPool:
    private = _private_dir(tmp_path)
    manifest = _write_private(private / "pool.json", _pool_payload(count))
    return live_accounts.load_account_pool(manifest)


def _readiness(pool: live_accounts.AccountPool, now: datetime) -> dict:
    accounts = [
        {
            "account_fingerprint": fingerprint,
            "evidence_sha256": hashlib.sha256(f"e-{fingerprint}".encode()).hexdigest(),
            "fixture_sha256": "d" * 64,
            "source_materials_verified": True,
            "surfaces_decryptable": True,
            "deterministic_acceptance": True,
            "post_import_chat_empty": True,
            "post_import_identity_present": True,
            "post_import_memory_present": True,
            "trace_cleared": True,
        }
        for fingerprint in pool.account_fingerprints
    ]
    return {
        "schema_version": 1,
        "kind": live_accounts.READINESS_KIND,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "base_url": live_accounts.LOCKED_BASE_URL,
        "build_sha": BUILD_SHA,
        "deployment_receipt_pre_sha256": "e" * 64,
        "deployment_receipt_post_sha256": "f" * 64,
        "import_started_at": (now - timedelta(minutes=2)).isoformat(),
        "import_finished_at": (now - timedelta(minutes=1)).isoformat(),
        "pool_manifest_sha256": pool.manifest_sha256,
        "route_sha256": pool.route_sha256,
        "persona_fixture_sha256": PERSONA_SHA,
        "source_bundle_sha256": SOURCE_SHA,
        "import_fixture_sha256": "d" * 64,
        "account_count": len(accounts),
        "account_fingerprints": list(pool.account_fingerprints),
        "accounts": accounts,
        "all_ready": True,
    }


def _cleanup(pool: live_accounts.AccountPool, now: datetime, complete: bool = True) -> dict:
    count = len(pool.account_fingerprints)
    accounts = []
    for index, (_profile, session) in enumerate(pool.rows, start=1):
        passed = complete or index < count
        accounts.append(
            {
                "pool_index": index,
                "account_fingerprint": hashlib.sha256(
                    session.user_id.encode("utf-8")
                ).hexdigest(),
                "profile_id": pool.profile_id,
                "attempted": True,
                "reset_response_accepted": passed,
                "provider_config_preexisted": True,
                "provider_config_live_predelete_observed": passed,
                "provider_config_deleted": passed,
                "key_envelope_deleted": passed,
                "provider_config_deletion_source": "explicit_api",
                "account_reset": passed,
                "old_credential_rejected": passed,
                "user_absence_verified": passed,
                "status": "PASS" if passed else "FAIL",
            }
        )
    return {
        "schema_version": 1,
        "kind": live_accounts.CLEANUP_KIND,
        "created_at": now.isoformat(),
        "base_url": live_accounts.LOCKED_BASE_URL,
        "pool_manifest_sha256": pool.manifest_sha256,
        "route_sha256": pool.route_sha256,
        "account_count": count,
        "account_fingerprints": list(pool.account_fingerprints),
        "attempted": count,
        "cleaned": count if complete else count - 1,
        "failed_count": 0 if complete else 1,
        "complete": complete,
        "manifest_deleted": complete,
        "accounts": accounts,
    }


def test_load_account_pool_binds_route_keys_and_unique_accounts(tmp_path: Path):
    pool = _pool(tmp_path)

    assert pool.profile_id == "official-openai"
    assert pool.deployment_runtime == "hosted_resident"
    assert len(pool.rows) == 2
    assert len(pool.account_fingerprints) == 2
    assert len(pool.manifest_sha256) == 64
    assert len(pool.route_sha256) == 64
    assert pool.rows[0][1].sk == b"\x01" * 32


def test_baseline_deployment_requirement_is_distinct_from_observed_account_runtime(
    tmp_path: Path,
):
    private = _private_dir(tmp_path)
    payload = _pool_payload()
    payload["runtime_mode"] = "deployed_current"
    for row in payload["profiles"]:
        row["runtime_mode_set_required"] = False
        row["runtime_mode_set_verified"] = False
    manifest = _write_private(private / "pool.json", payload)

    pool = live_accounts.load_account_pool(manifest)

    assert pool.deployment_runtime == "deployed_current"
    assert {row["runtime_mode"] for row, _session in pool.rows} == {
        "hosted_resident"
    }


def test_cleanup_loader_accepts_expired_but_otherwise_valid_pool(tmp_path: Path):
    private = _private_dir(tmp_path)
    payload = _pool_payload()
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    for row in payload["profiles"]:
        row["synthetic_account_lease"]["expires_at"] = expired.isoformat()
        row["synthetic_account_lease"]["expires_at_epoch"] = int(expired.timestamp())
    manifest = _write_private(private / "pool.json", payload)

    with pytest.raises(live_accounts.LiveAccountContractError, match="expired"):
        live_accounts.load_account_pool(manifest)

    pool = live_accounts.load_account_pool(manifest, allow_expired_lease=True)
    assert len(pool.rows) == 2


@pytest.mark.parametrize("mutation", ["duplicate-user", "mixed-route", "expired", "bad-key"])
def test_load_account_pool_rejects_unsafe_or_mixed_entries(tmp_path: Path, mutation: str):
    private = _private_dir(tmp_path)
    payload = _pool_payload()
    if mutation == "duplicate-user":
        payload["profiles"][1]["user_id"] = payload["profiles"][0]["user_id"]
    elif mutation == "mixed-route":
        payload["profiles"][1]["configured_model"] = "gpt-different"
    elif mutation == "expired":
        payload["profiles"][0]["synthetic_account_lease"]["expires_at_epoch"] = 1
    else:
        payload["profiles"][0]["secret_key_b64"] = "not-base64"
    manifest = _write_private(private / "pool.json", payload)

    with pytest.raises(live_accounts.LiveAccountContractError):
        live_accounts.load_account_pool(manifest)


def test_private_contract_rejects_world_readable_file_and_non_private_parent(tmp_path: Path):
    private = _private_dir(tmp_path)
    manifest = _write_private(private / "pool.json", _pool_payload(), 0o644)
    with pytest.raises(live_accounts.LiveAccountContractError, match="owner-controlled"):
        live_accounts.load_account_pool(manifest)

    manifest.chmod(0o600)
    private.chmod(0o755)
    with pytest.raises(live_accounts.LiveAccountContractError, match="owner-controlled"):
        live_accounts.load_account_pool(manifest)


def test_readiness_receipt_is_fresh_and_exactly_bound_to_pool(tmp_path: Path):
    pool = _pool(tmp_path)
    now = datetime.now(timezone.utc)
    receipt = _write_private(pool.path.parent / "ready.json", _readiness(pool, now))

    document, digest = live_accounts.verify_readiness_receipt(
        receipt,
        pool=pool,
        expected_build_sha=BUILD_SHA,
        expected_persona_fixture_sha256=PERSONA_SHA,
        expected_source_bundle_sha256=SOURCE_SHA,
        expected_import_fixture_sha256="d" * 64,
        at_time=now + timedelta(minutes=1),
    )

    assert document["all_ready"] is True
    assert len(digest) == 64


def test_readiness_requires_at_least_9000_seconds_of_lease_remaining(
    tmp_path: Path,
):
    pool = _pool(tmp_path)
    now = datetime.now(timezone.utc)
    at_time = now + timedelta(minutes=1)
    receipt = _write_private(pool.path.parent / "ready.json", _readiness(pool, now))
    for profile, _session in pool.rows:
        profile["synthetic_account_lease"]["expires_at_epoch"] = int(
            (at_time + timedelta(seconds=8_999)).timestamp()
        )

    with pytest.raises(
        live_accounts.LiveAccountContractError,
        match="required run lifetime remaining",
    ):
        live_accounts.verify_readiness_receipt(
            receipt,
            pool=pool,
            expected_build_sha=BUILD_SHA,
            expected_persona_fixture_sha256=PERSONA_SHA,
            expected_source_bundle_sha256=SOURCE_SHA,
            expected_import_fixture_sha256="d" * 64,
            at_time=at_time,
        )


@pytest.mark.parametrize("mutation", ["stale", "wrong-build", "partial", "reordered"])
def test_readiness_receipt_fails_closed_on_stale_or_mismatched_evidence(
    tmp_path: Path, mutation: str
):
    pool = _pool(tmp_path)
    now = datetime.now(timezone.utc)
    payload = _readiness(pool, now)
    at_time = now + timedelta(minutes=1)
    if mutation == "stale":
        at_time = now + timedelta(hours=1)
    elif mutation == "wrong-build":
        payload["build_sha"] = "f" * 40
    elif mutation == "partial":
        payload["accounts"][0]["surfaces_decryptable"] = False
    else:
        payload["accounts"].reverse()
    receipt = _write_private(pool.path.parent / "ready.json", payload)

    with pytest.raises(live_accounts.LiveAccountContractError):
        live_accounts.verify_readiness_receipt(
            receipt,
            pool=pool,
            expected_build_sha=BUILD_SHA,
            expected_persona_fixture_sha256=PERSONA_SHA,
            expected_source_bundle_sha256=SOURCE_SHA,
            expected_import_fixture_sha256="d" * 64,
            at_time=at_time,
        )


@pytest.mark.parametrize(
    "payload", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}']
)
def test_private_json_rejects_duplicate_keys_and_non_finite_numbers(
    tmp_path: Path, payload: bytes
):
    private = _private_dir(tmp_path)
    path = private / "ambiguous.json"
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(live_accounts.LiveAccountContractError, match="JSON is invalid"):
        live_accounts.read_private_json(path)


def test_readiness_requires_unique_per_account_evidence(tmp_path: Path):
    pool = _pool(tmp_path)
    now = datetime.now(timezone.utc)
    payload = _readiness(pool, now)
    payload["accounts"][1]["evidence_sha256"] = payload["accounts"][0][
        "evidence_sha256"
    ]
    receipt = _write_private(pool.path.parent / "ready.json", payload)

    with pytest.raises(live_accounts.LiveAccountContractError, match="canonical"):
        live_accounts.verify_readiness_receipt(
            receipt,
            pool=pool,
            expected_build_sha=BUILD_SHA,
            expected_persona_fixture_sha256=PERSONA_SHA,
            expected_source_bundle_sha256=SOURCE_SHA,
            expected_import_fixture_sha256="d" * 64,
            at_time=now + timedelta(minutes=1),
        )


def test_cleanup_receipt_requires_every_pool_account_to_be_deleted(tmp_path: Path):
    pool = _pool(tmp_path)
    now = datetime.now(timezone.utc)
    success = _write_private(pool.path.parent / "cleanup-ok.json", _cleanup(pool, now))

    document, digest = live_accounts.verify_cleanup_receipt(
        success,
        expected_pool_manifest_sha256=pool.manifest_sha256,
        expected_route_sha256=pool.route_sha256,
        expected_account_fingerprints=pool.account_fingerprints,
    )

    assert document["complete"] is True
    assert len(digest) == 64

    failed = _write_private(
        pool.path.parent / "cleanup-failed.json", _cleanup(pool, now, False)
    )
    with pytest.raises(live_accounts.LiveAccountContractError, match="incomplete"):
        live_accounts.verify_cleanup_receipt(
            failed,
            expected_pool_manifest_sha256=pool.manifest_sha256,
            expected_route_sha256=pool.route_sha256,
            expected_account_fingerprints=pool.account_fingerprints,
        )


@pytest.mark.parametrize(
    "mutation",
    ["wrong-index", "wrong-fingerprint", "missing-db-proof", "secret-field"],
)
def test_cleanup_receipt_rejects_tampered_per_account_evidence(
    tmp_path: Path, mutation: str
):
    pool = _pool(tmp_path)
    payload = _cleanup(pool, datetime.now(timezone.utc))
    if mutation == "wrong-index":
        payload["accounts"][0]["pool_index"] = 2
    elif mutation == "wrong-fingerprint":
        payload["accounts"][0]["account_fingerprint"] = "9" * 64
    elif mutation == "missing-db-proof":
        payload["accounts"][0]["user_absence_verified"] = False
    else:
        payload["accounts"][0]["user_id"] = "must-never-be-published"
    receipt = _write_private(pool.path.parent / "cleanup-tampered.json", payload)

    with pytest.raises(live_accounts.LiveAccountContractError):
        live_accounts.verify_cleanup_receipt(
            receipt,
            expected_pool_manifest_sha256=pool.manifest_sha256,
            expected_route_sha256=pool.route_sha256,
            expected_account_fingerprints=pool.account_fingerprints,
        )
