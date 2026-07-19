from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from qa import build_team_report as team_report
from qa import protected_debug_bundle as protected
from qa.tests import test_validate_run as validate_fixtures
from qa.tests.test_validate_run import _valid_result


def _failure(category: str, stage: str, code: str) -> dict:
    return {
        "category": category,
        "stage_code": stage,
        "failure_code": code,
        "reproducible": False,
    }


def _mark_failed(profile: dict, scenario_id: str, stage: str, code: str) -> None:
    scenario = next(
        row for row in profile["scenarios"] if row["scenario_id"] == scenario_id
    )
    failure = _failure("PRODUCT_FAIL", stage, code)
    scenario.update(
        status="PRODUCT_FAIL",
        failure=deepcopy(failure),
        attempt_results=[
            {"attempt": 1, "status": "PRODUCT_FAIL", "failure": deepcopy(failure)}
        ],
    )
    first_assertion = next(iter(scenario["assertions"]))
    scenario["assertions"][first_assertion] = False
    if scenario["evidence_codes"]:
        scenario["evidence_codes"] = scenario["evidence_codes"][1:]


def _failed_result() -> dict:
    result = _valid_result()
    profile = result["profiles"][0]
    _mark_failed(
        profile,
        "P0-06",
        "PERSONA_IMPORT",
        "PERSONA_ACCEPTANCE_FAILED",
    )
    _mark_failed(profile, "P0-08", "BASIC_CHAT", "CHAT_TIMEOUT")
    _mark_failed(
        profile,
        "P0-12",
        "REASONING",
        "REASONING_METADATA_MISSING",
    )
    profile["status"] = "PRODUCT_FAIL"
    result["overall_status"] = "PRODUCT_FAIL"
    result["summary"].update({"pass": 8, "product_fail": 1})
    return result


def _coverage() -> dict:
    return json.loads(
        (Path(__file__).resolve().parents[1] / "coverage-lock.json").read_text(
            encoding="utf-8"
        )
    )


def _failure_index(result: dict) -> dict:
    cleanup = {
        "generated_at": result["finished_at"],
        "attempted": 10,
        "cleaned": 10,
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_retained_for_scan": False,
    }
    _, failure_index = team_report._build_indexes(
        result, _coverage(), result["profiles"], cleanup, "unit-test"
    )
    return failure_index


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> Path:
    path.write_text(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def _inputs(tmp_path: Path, *, result: dict | None = None) -> tuple[Path, Path, dict]:
    document = deepcopy(result) if result is not None else _failed_result()
    return (
        _write_json(tmp_path / "run-result.json", document),
        _write_json(tmp_path / "failure-index.json", _failure_index(document)),
        document,
    )


def _trusted_manifest(
    tmp_path: Path, result: dict, *, name: str = "provisioning-manifest.json"
) -> Path:
    default = validate_fixtures._write_provisioning_manifest(tmp_path)
    manifest = json.loads(default.read_text(encoding="utf-8"))
    result_by_id = {row["profile_id"]: row for row in result["profiles"]}
    manifest["runtime_mode"] = result["target"]["expected_runtime"]
    for entry in manifest["profiles"]:
        profile = result_by_id[entry["profile_id"]]
        entry.update(
            label=f"agent-e2e-{result['run_id']}-{entry['profile_id']}",
            provider=profile["provider"],
            route_family=profile["route_family"],
            configured_model=profile["model"],
            user_id=profile["user_id"],
            runtime_mode=profile["observed_runtime"],
            runtime_version=profile["observed_runtime_version"],
        )
        entry["valid_key_receipt"].update(
            provider=profile["provider"], model=profile["model"]
        )
        entry["runtime_readback_receipt"].update(
            runtime_mode=profile["observed_runtime"],
            runtime_version=profile["observed_runtime_version"],
        )
    manifest["auxiliary_accounts"][0]["label"] = (
        f"agent-e2e-{result['run_id']}-memory-contract"
    )
    destination = tmp_path / name
    if destination != default:
        default.unlink()
    return _write_json(destination, manifest)


def _build_bundle(
    *,
    result_path: Path,
    failure_index_path: Path,
    recipients_csv: str,
    output_path: Path,
    provisioning_manifest_path: Path | None = None,
    persona_summary_path: Path | None = None,
    expected_runtime: str | None = None,
    expected_deployment_sha: str | None = None,
) -> None:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    manifest_source = (
        result
        if isinstance(result, dict)
        and isinstance(result.get("target"), dict)
        and isinstance(result.get("profiles"), list)
        else _failed_result()
    )
    manifest = provisioning_manifest_path or _trusted_manifest(
        result_path.parent, manifest_source
    )
    protected.build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        persona_summary_path=persona_summary_path,
        provisioning_manifest_path=manifest,
        expected_runtime=(
            expected_runtime or manifest_source["target"]["expected_runtime"]
        ),
        expected_deployment_sha=(
            expected_deployment_sha
            or manifest_source["target"]["expected_deployment_sha"]
        ),
        recipients_csv=recipients_csv,
        output_path=output_path,
    )


def _decrypt_bundle(
    *,
    identity_path: Path,
    input_path: Path,
    output_path: Path,
    failure_index_path: Path | None = None,
) -> None:
    protected.decrypt_bundle(
        identity_path=identity_path,
        input_path=input_path,
        failure_index_path=(
            failure_index_path or input_path.parent / "failure-index.json"
        ),
        output_path=output_path,
    )


def _identity(tmp_path: Path, name: str = "identity.json") -> tuple[Path, dict]:
    path = tmp_path / name
    public = protected.generate_key(path)
    return path, public


def _round_trip(
    tmp_path: Path,
    *,
    result: dict | None = None,
    identity_name: str = "identity.json",
    envelope_name: str = "bundle.json",
    plaintext_name: str = "payload.json",
) -> tuple[dict, Path, Path, Path, dict]:
    result_path, failure_index_path, source = _inputs(tmp_path, result=result)
    identity_path, public = _identity(tmp_path, identity_name)
    envelope_path = tmp_path / envelope_name
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=envelope_path,
    )
    plaintext_path = tmp_path / plaintext_name
    _decrypt_bundle(
        identity_path=identity_path,
        input_path=envelope_path,
        output_path=plaintext_path,
    )
    return (
        json.loads(plaintext_path.read_text(encoding="utf-8")),
        identity_path,
        envelope_path,
        plaintext_path,
        source,
    )


def test_round_trip_preserves_failure_only_exact_ids_and_metadata(tmp_path):
    payload, _identity_path, _envelope, _plaintext, source = _round_trip(tmp_path)

    assert payload["failure_count"] == 3
    assert (
        payload["failure_index_sha256"]
        == hashlib.sha256(
            (_envelope.parent / "failure-index.json").read_bytes()
        ).hexdigest()
    )
    assert {row["scenario_id"] for row in payload["failures"]} == {
        "P0-06",
        "P0-08",
        "P0-12",
    }
    assert all(row["status"] != "PASS" for row in payload["failures"])
    assert "P0-09" not in {row["scenario_id"] for row in payload["failures"]}

    source_profile = source["profiles"][0]
    basic_source = next(
        row for row in source_profile["scenarios"] if row["scenario_id"] == "P0-08"
    )
    basic = next(row for row in payload["failures"] if row["scenario_id"] == "P0-08")
    assert basic["user_id"] == source_profile["user_id"]
    assert basic["request_ids"] == basic_source["request_ids"]
    assert basic["turn_ids"] == basic_source["turn_ids"]
    assert basic["trace_ids"] == basic_source["trace_ids"]
    assert basic["turns"][0]["request_id"] == source_profile["turns"][0]["request_id"]
    assert basic["turns"][0]["stage_latency_ms"]["provider"] == 1

    persona_source = next(
        row for row in source_profile["scenarios"] if row["scenario_id"] == "P0-06"
    )["persona_finalizer"]
    persona = next(row for row in payload["failures"] if row["scenario_id"] == "P0-06")
    assert persona["persona"]["job_id"] == persona_source["job_id"]
    assert persona["persona"]["request_id"] == persona_source["request_id"]
    assert "private_evidence_deleted" not in persona["persona"]
    assert persona["persona"]["evidence_deleted"] is True

    reasoning = next(
        row for row in payload["failures"] if row["scenario_id"] == "P0-12"
    )["reasoning"]
    assert reasoning["request_id"] == source_profile["reasoning"]["request_id"]
    assert reasoning["reasoning_token_count"] == 1
    assert "raw_private_reasoning_stored" not in reasoning
    assert "raw_trace_stored" not in basic["trace"]


def test_mixed_persona_failure_binds_public_index_but_encrypts_only_exact_ids(
    tmp_path,
):
    result_path, failure_index_path, result = _inputs(tmp_path)
    outcomes = {
        "prepare": "success",
        "live": "failure",
        "cleanup": "success",
        "finalize": "failure",
    }
    persona_summary = team_report._persona_unavailable_summary(result, 1, outcomes)
    persona_path = _write_json(
        tmp_path / "persona-memory-summary.json", persona_summary
    )
    _write_json(
        failure_index_path,
        team_report._build_indexes(
            result,
            _coverage(),
            result["profiles"],
            {
                "generated_at": result["finished_at"],
                "attempted": 10,
                "cleaned": 10,
                "failed_profile_ids": [],
                "manifest_deleted": True,
                "manifest_retained_for_scan": False,
            },
            "unit-test",
            persona_summary,
        )[1],
    )
    identity_path, public = _identity(tmp_path)
    envelope_path = tmp_path / "mixed-bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        persona_summary_path=persona_path,
        recipients_csv=public["public_key_b64"],
        output_path=envelope_path,
    )
    plaintext_path = tmp_path / "mixed-payload.json"
    _decrypt_bundle(
        identity_path=identity_path,
        input_path=envelope_path,
        failure_index_path=failure_index_path,
        output_path=plaintext_path,
    )

    public_index = json.loads(failure_index_path.read_text(encoding="utf-8"))
    payload = json.loads(plaintext_path.read_text(encoding="utf-8"))
    assert public_index["failure_count"] == 4
    assert public_index["persona_memory_failure_count"] == 1
    assert public_index["exact_id_failure_count"] == 3
    assert payload["failure_count"] == 3
    assert all(row["scenario_id"].startswith("P0-") for row in payload["failures"])


def test_multiple_recipients_can_decrypt_the_same_bundle(tmp_path):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    first_identity, first = _identity(tmp_path, "first.json")
    second_identity, second = _identity(tmp_path, "second.json")
    bundle = tmp_path / "bundle.json"

    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=f"{first['public_key_b64']},{second['public_key_b64']}",
        output_path=bundle,
    )
    first_output = tmp_path / "first-payload.json"
    second_output = tmp_path / "second-payload.json"
    _decrypt_bundle(
        identity_path=first_identity, input_path=bundle, output_path=first_output
    )
    _decrypt_bundle(
        identity_path=second_identity, input_path=bundle, output_path=second_output
    )

    assert first_output.read_bytes() == second_output.read_bytes()
    envelope = json.loads(bundle.read_text(encoding="utf-8"))
    assert {row["fingerprint"] for row in envelope["recipients"]} == {
        first["fingerprint"],
        second["fingerprint"],
    }


def test_locked_unicode_relay_model_metadata_round_trips(tmp_path):
    result = _valid_result()
    profile = result["profiles"][-1]
    _mark_failed(profile, "P0-08", "BASIC_CHAT", "CHAT_TIMEOUT")
    profile["status"] = "PRODUCT_FAIL"
    result["overall_status"] = "PRODUCT_FAIL"
    result["summary"].update({"pass": 8, "product_fail": 1})

    payload, _identity_path, _bundle, _plaintext, _source = _round_trip(
        tmp_path, result=result
    )

    assert payload["failures"][0]["profile_id"] == "relay-kongbeiqie"
    assert payload["failures"][0]["model"] == profile["model"]


def test_generate_key_cli_prints_only_public_material(tmp_path, capsys):
    identity = tmp_path / "identity.json"

    assert protected.main(["generate-key", "--identity-out", str(identity)]) == 0

    stdout = capsys.readouterr().out
    public = json.loads(stdout)
    private = json.loads(identity.read_text(encoding="utf-8"))["private_key_b64"]
    assert set(public) == {"public_key_b64", "fingerprint"}
    assert private not in stdout
    assert "private" not in stdout.lower()


def test_identity_bundle_and_plaintext_are_owner_only(tmp_path):
    _payload, identity, bundle, plaintext, _source = _round_trip(tmp_path)

    for path in (identity, bundle, plaintext):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1


def test_wrong_identity_is_rejected(tmp_path):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    _intended_path, intended = _identity(tmp_path, "intended.json")
    wrong_path, _wrong = _identity(tmp_path, "wrong.json")
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=intended["public_key_b64"],
        output_path=bundle,
    )

    with pytest.raises(protected.ProtectedDebugError, match="not a bundle recipient"):
        _decrypt_bundle(
            identity_path=wrong_path,
            input_path=bundle,
            output_path=tmp_path / "payload.json",
        )


@pytest.mark.parametrize("field", ("ciphertext_b64", "payload_sha256", "wrapped"))
def test_tampered_bundle_is_rejected(tmp_path, field):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    identity_path, public = _identity(tmp_path)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )
    envelope = json.loads(bundle.read_text(encoding="utf-8"))
    if field == "payload_sha256":
        envelope[field] = "0" * 64
    elif field == "wrapped":
        decoded = bytearray(
            base64.b64decode(envelope["recipients"][0]["wrapped_key_b64"])
        )
        decoded[-1] ^= 1
        envelope["recipients"][0]["wrapped_key_b64"] = base64.b64encode(
            decoded
        ).decode()
    else:
        decoded = bytearray(base64.b64decode(envelope[field]))
        decoded[-1] ^= 1
        envelope[field] = base64.b64encode(decoded).decode()
    tampered = _write_json(tmp_path / f"tampered-{field}.json", envelope)

    with pytest.raises(protected.ProtectedDebugError):
        _decrypt_bundle(
            identity_path=identity_path,
            input_path=tampered,
            output_path=tmp_path / f"payload-{field}.json",
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda index: index.update(run_id="different-run"),
        lambda index: index["failures"][0].update(status="SECURITY_FAIL"),
        lambda index: index["failures"][0]["correlation_handles"][
            "request_handles"
        ].append("0" * 64),
        lambda index: index["failures"][0].update(unexpected="field"),
    ),
)
def test_public_failure_index_must_bind_exactly_to_canonical_result(tmp_path, mutate):
    result_path, failure_index_path, result = _inputs(tmp_path)
    index = json.loads(failure_index_path.read_text(encoding="utf-8"))
    mutate(index)
    _write_json(failure_index_path, index)
    _identity_path, public = _identity(tmp_path)

    with pytest.raises(
        protected.ProtectedDebugError, match="binding is invalid"
    ) as exc:
        _build_bundle(
            result_path=result_path,
            failure_index_path=failure_index_path,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "bundle.json",
        )
    assert result["run_id"] not in str(exc.value)


def test_consistent_forged_result_and_failure_index_fail_against_unchanged_manifest(
    tmp_path,
):
    trusted = _failed_result()
    trusted_manifest = _trusted_manifest(
        tmp_path, trusted, name="trusted-provisioning-manifest.json"
    )
    forged = deepcopy(trusted)
    forged_user_id = "synthetic-forged-user-not-in-manifest"
    forged["profiles"][0]["user_id"] = forged_user_id
    result_path = _write_json(tmp_path / "forged-run-result.json", forged)
    failure_index_path = _write_json(
        tmp_path / "forged-failure-index.json", _failure_index(forged)
    )
    _identity_path, public = _identity(tmp_path)

    with pytest.raises(
        protected.ProtectedDebugError, match="trusted provisioning binding is invalid"
    ) as exc:
        _build_bundle(
            result_path=result_path,
            failure_index_path=failure_index_path,
            provisioning_manifest_path=trusted_manifest,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "forged-bundle.json",
        )

    assert forged_user_id not in str(exc.value)
    assert not (tmp_path / "forged-bundle.json").exists()


@pytest.mark.parametrize(
    ("expected_runtime", "expected_sha", "message"),
    (
        ("deployed_current", "a" * 40, "deployment binding"),
        ("hosted_resident", "b" * 40, "deployment binding"),
        ("hosted_resident", "a" * 12, "deployment expectation"),
    ),
)
def test_result_target_requires_exact_runtime_and_full_deployment_sha(
    tmp_path, expected_runtime, expected_sha, message
):
    result_path, failure_index_path, result = _inputs(tmp_path)
    manifest = _trusted_manifest(tmp_path, result)
    _identity_path, public = _identity(tmp_path)

    with pytest.raises(protected.ProtectedDebugError, match=message):
        _build_bundle(
            result_path=result_path,
            failure_index_path=failure_index_path,
            provisioning_manifest_path=manifest,
            expected_runtime=expected_runtime,
            expected_deployment_sha=expected_sha,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "mismatched-deployment-bundle.json",
        )


def test_decrypt_rejects_failure_index_from_another_actions_run(tmp_path):
    result_path, failure_index_path, result = _inputs(tmp_path)
    identity_path, public = _identity(tmp_path)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )

    other_result = deepcopy(result)
    other_result["run_id"] = "unit-run-0002"
    other_index = _write_json(
        tmp_path / "other-run-failure-index.json", _failure_index(other_result)
    )
    output = tmp_path / "mixed-run-payload.json"

    with pytest.raises(
        protected.ProtectedDebugError, match="failure index digest is invalid"
    ):
        _decrypt_bundle(
            identity_path=identity_path,
            input_path=bundle,
            failure_index_path=other_index,
            output_path=output,
        )
    assert not output.exists()


def test_decrypt_requires_byte_exact_scanned_failure_index(tmp_path):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    identity_path, public = _identity(tmp_path)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )
    reformatted = tmp_path / "reformatted-failure-index.json"
    reformatted.write_bytes(failure_index_path.read_bytes() + b"\n")
    reformatted.chmod(0o600)

    with pytest.raises(
        protected.ProtectedDebugError, match="failure index digest is invalid"
    ):
        _decrypt_bundle(
            identity_path=identity_path,
            input_path=bundle,
            failure_index_path=reformatted,
            output_path=tmp_path / "reformatted-payload.json",
        )


@pytest.mark.parametrize(
    "field",
    (
        "prompt",
        "response",
        "rationale",
        "raw_trace_payload",
        "raw_persona_payload",
        "private_key",
        "provider_api_key",
    ),
)
def test_forbidden_private_or_raw_fields_are_rejected(tmp_path, field):
    result = _failed_result()
    result["profiles"][0][field] = "FORBIDDEN-SENTINEL-MATERIAL"
    result_path = _write_json(tmp_path / "run-result.json", result)
    failure_index_path = _write_json(
        tmp_path / "failure-index.json", _failure_index(_failed_result())
    )
    _identity_path, public = _identity(tmp_path)

    with pytest.raises(protected.ProtectedDebugError, match="canonical result shape"):
        _build_bundle(
            result_path=result_path,
            failure_index_path=failure_index_path,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "bundle.json",
        )


def test_ciphertext_envelope_contains_no_exact_ids_or_secret_sentinel(tmp_path):
    result = _failed_result()
    result["profiles"][0]["user_id"] = "synthetic-ENCRYPTION-SENTINEL-user"
    payload, _identity_path, envelope_path, _plaintext, source = _round_trip(
        tmp_path, result=result
    )
    envelope_bytes = envelope_path.read_bytes()
    failed_profile = source["profiles"][0]
    persona = next(
        row for row in failed_profile["scenarios"] if row["scenario_id"] == "P0-06"
    )["persona_finalizer"]

    for value in (
        failed_profile["user_id"],
        failed_profile["turns"][0]["request_id"],
        failed_profile["turns"][0]["turn_id"],
        failed_profile["turns"][0]["trace_id"],
        persona["job_id"],
        "ENCRYPTION-SENTINEL",
    ):
        assert value.encode("utf-8") not in envelope_bytes
    assert b"private-account-key-official-deepseek" not in envelope_bytes
    assert b"cHJpdmF0ZS1zZWNyZXQta2V5" not in envelope_bytes
    assert payload["failures"][0]["user_id"] == failed_profile["user_id"]


def test_duplicate_recipient_is_rejected(tmp_path):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    _identity_path, public = _identity(tmp_path)

    with pytest.raises(protected.ProtectedDebugError, match="recipients are invalid"):
        _build_bundle(
            result_path=result_path,
            failure_index_path=failure_index_path,
            recipients_csv=f"{public['public_key_b64']},{public['public_key_b64']}",
            output_path=tmp_path / "bundle.json",
        )


def test_identity_must_be_owner_only(tmp_path):
    identity, public = _identity(tmp_path)
    identity.chmod(0o644)
    result_path, failure_index_path, _result = _inputs(tmp_path)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )

    with pytest.raises(protected.ProtectedDebugError, match="identity.*unsafe"):
        _decrypt_bundle(
            identity_path=identity,
            input_path=bundle,
            output_path=tmp_path / "payload.json",
        )


@pytest.mark.parametrize(
    "source",
    (
        "result",
        "failure-index",
        "manifest",
        "identity",
        "bundle",
        "decrypt-failure-index",
    ),
)
def test_symlink_inputs_are_rejected(tmp_path, source):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    identity_path, public = _identity(tmp_path)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )
    original = {
        "result": result_path,
        "failure-index": failure_index_path,
        "manifest": tmp_path / "provisioning-manifest.json",
        "identity": identity_path,
        "bundle": bundle,
        "decrypt-failure-index": failure_index_path,
    }[source]
    link = tmp_path / f"linked-{source}.json"
    link.symlink_to(original)

    with pytest.raises(protected.ProtectedDebugError, match="unsafe"):
        if source in {"identity", "bundle", "decrypt-failure-index"}:
            _decrypt_bundle(
                identity_path=link if source == "identity" else identity_path,
                input_path=link if source == "bundle" else bundle,
                failure_index_path=(
                    link if source == "decrypt-failure-index" else failure_index_path
                ),
                output_path=tmp_path / "payload.json",
            )
        else:
            _build_bundle(
                result_path=link if source == "result" else result_path,
                failure_index_path=(
                    link if source == "failure-index" else failure_index_path
                ),
                provisioning_manifest_path=(
                    link
                    if source == "manifest"
                    else tmp_path / "provisioning-manifest.json"
                ),
                recipients_csv=public["public_key_b64"],
                output_path=tmp_path / "linked-output.json",
            )


def test_malformed_duplicate_and_oversize_inputs_are_rejected(tmp_path):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    _identity_path, public = _identity(tmp_path)

    malformed = _write_json(tmp_path / "malformed.json", {"not": "a result"})
    with pytest.raises(protected.ProtectedDebugError):
        _build_bundle(
            result_path=malformed,
            failure_index_path=failure_index_path,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "malformed-output.json",
        )

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    duplicate.chmod(0o600)
    with pytest.raises(protected.ProtectedDebugError, match="duplicate keys"):
        _build_bundle(
            result_path=result_path,
            failure_index_path=duplicate,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "duplicate-output.json",
        )

    oversize = tmp_path / "oversize.json"
    with oversize.open("wb") as stream:
        stream.truncate(protected.MAX_FAILURE_INDEX_BYTES + 1)
    oversize.chmod(0o600)
    with pytest.raises(protected.ProtectedDebugError, match="unsafe"):
        _build_bundle(
            result_path=result_path,
            failure_index_path=oversize,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "oversize-output.json",
        )


@pytest.mark.parametrize("operation", ("generate", "build", "decrypt"))
def test_occupied_outputs_are_never_replaced(tmp_path, operation):
    occupied = _write_json(tmp_path / "occupied.json", {"sentinel": True})
    original = occupied.read_bytes()
    result_path, failure_index_path, _result = _inputs(tmp_path)
    identity_path, public = _identity(tmp_path)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )

    with pytest.raises(protected.ProtectedDebugError, match="could not be published"):
        if operation == "generate":
            protected.generate_key(occupied)
        elif operation == "build":
            _build_bundle(
                result_path=result_path,
                failure_index_path=failure_index_path,
                recipients_csv=public["public_key_b64"],
                output_path=occupied,
            )
        else:
            _decrypt_bundle(
                identity_path=identity_path,
                input_path=bundle,
                output_path=occupied,
            )
    assert occupied.read_bytes() == original


def test_envelope_and_identity_reject_extra_fields(tmp_path):
    result_path, failure_index_path, _result = _inputs(tmp_path)
    identity_path, public = _identity(tmp_path)
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["extra"] = "field"
    bad_identity = _write_json(tmp_path / "bad-identity.json", identity)
    bundle = tmp_path / "bundle.json"
    _build_bundle(
        result_path=result_path,
        failure_index_path=failure_index_path,
        recipients_csv=public["public_key_b64"],
        output_path=bundle,
    )

    with pytest.raises(protected.ProtectedDebugError, match="identity shape"):
        _decrypt_bundle(
            identity_path=bad_identity,
            input_path=bundle,
            output_path=tmp_path / "payload-identity.json",
        )

    envelope = json.loads(bundle.read_text(encoding="utf-8"))
    envelope["extra"] = "field"
    bad_envelope = _write_json(tmp_path / "bad-envelope.json", envelope)
    with pytest.raises(protected.ProtectedDebugError, match="bundle shape"):
        _decrypt_bundle(
            identity_path=identity_path,
            input_path=bad_envelope,
            output_path=tmp_path / "payload-envelope.json",
        )


def test_fixed_errors_do_not_echo_sensitive_values(tmp_path):
    sentinel = "SENSITIVE-DO-NOT-ECHO-7F3A"
    result_path, failure_index_path, _result = _inputs(tmp_path)
    index = json.loads(failure_index_path.read_text(encoding="utf-8"))
    index["failures"][0]["unexpected"] = sentinel
    _write_json(failure_index_path, index)
    _identity_path, public = _identity(tmp_path)

    with pytest.raises(protected.ProtectedDebugError) as exc:
        _build_bundle(
            result_path=result_path,
            failure_index_path=failure_index_path,
            recipients_csv=public["public_key_b64"],
            output_path=tmp_path / "bundle.json",
        )
    assert sentinel not in str(exc.value)


def test_generated_identity_public_key_matches_private_key_without_exposing_it(
    tmp_path,
):
    identity_path, public = _identity(tmp_path)
    identity = json.loads(identity_path.read_text(encoding="utf-8"))

    assert identity["public_key_b64"] == public["public_key_b64"]
    assert identity["fingerprint"] == public["fingerprint"]
    assert len(base64.b64decode(identity["private_key_b64"])) == 32
    assert len(base64.b64decode(identity["public_key_b64"])) == 32
    assert os.geteuid() == identity_path.stat().st_uid
