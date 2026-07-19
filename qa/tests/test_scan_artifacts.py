from __future__ import annotations

import base64
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest

from qa import scan_artifacts as scanner
from qa.tests.test_validate_run import _valid_result


def _env() -> dict[str, str]:
    env = {
        name: f"sensitive-{index}-value"
        for index, name in enumerate(scanner.SECRET_ENV_NAMES)
    }
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "header.payload.signature-for-scanner",
            "access_token": "codex-access-token-for-scanner",
            "refresh_token": "codex-refresh-token-for-scanner",
            "account_id": "qa-account",
        },
    }
    env["QA_CODEX_AUTH_JSON_B64"] = base64.b64encode(
        json.dumps(auth).encode("utf-8")
    ).decode("ascii")
    return env


def _handle(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class ScanInputs:
    artifacts: Path
    manifest: Path
    memory_manifest: Path
    codex_auth: Path
    fixture: Path
    canonical_result: Path
    env: dict[str, str]

    def scan(self) -> list[str]:
        return scanner.scan_artifacts(
            self.artifacts,
            self.manifest,
            self.memory_manifest,
            self.codex_auth,
            self.fixture,
            self.canonical_result,
            env=self.env,
        )

    def canonical(self) -> dict:
        return json.loads(self.canonical_result.read_text(encoding="utf-8"))

    def write_canonical(self, document: dict) -> None:
        self.canonical_result.write_text(json.dumps(document), encoding="utf-8")


def _write_inputs(tmp_path: Path) -> ScanInputs:
    canonical = _valid_result()
    staging = tmp_path / "staging"
    staging.mkdir()
    canonical_result = staging / "run-result.json"
    canonical_result.write_text(json.dumps(canonical), encoding="utf-8")

    artifacts = tmp_path / "team-safe"
    artifacts.mkdir()
    profile_handles = [_handle(profile["user_id"]) for profile in canonical["profiles"]]
    (artifacts / "run-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_handle": _handle(canonical["run_id"]),
                "profile_handles": profile_handles,
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "failure-index.json").write_text(
        json.dumps({"schema_version": 1, "failure_count": 0, "failures": []}),
        encoding="utf-8",
    )
    (artifacts / "team-summary.md").write_text(
        "# Team-safe qualification summary\n", encoding="utf-8"
    )
    (artifacts / "cleanup-receipt.json").write_text(
        json.dumps({"kind": "deterministic_cleanup_receipt", "status": "PASS"}),
        encoding="utf-8",
    )
    (artifacts / "memory-contract.json").write_text(
        json.dumps({"status": "PASS"}), encoding="utf-8"
    )
    (artifacts / "persona-memory-summary.json").write_text(
        json.dumps(
            {
                "kind": "persona_memory_qualification_summary",
                "status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "persona-memory-matrix.md").write_text(
        "persona memory matrix\n", encoding="utf-8"
    )
    (artifacts / "matrix.md").write_text(
        "\n".join(("qualification matrix", *scanner.PROFILE_IDS)) + "\n",
        encoding="utf-8",
    )
    (artifacts / "latency.csv").write_text(
        "profile,status\nofficial-openai,PASS\n", encoding="utf-8"
    )
    (artifacts / "junit.xml").write_text("<testsuite/>\n", encoding="utf-8")

    profiles = [
        {
            "profile_id": profile_id,
            "api_key": f"feedling-sensitive-account-{index}",
            "secret_key_b64": base64.b64encode(bytes([index + 1]) * 32).decode(),
            "synthetic_account_lease": {"absence_token": f"{index + 1:064x}"},
        }
        for index, profile_id in enumerate(scanner.PROFILE_IDS)
    ]
    memory_profile = {
        "profile_id": scanner.MEMORY_CONTRACT_PROFILE_ID,
        "api_key": "feedling-sensitive-memory-account",
        "secret_key_b64": base64.b64encode(bytes([99]) * 32).decode(),
        "synthetic_account_lease": {"absence_token": f"{99:064x}"},
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": profiles,
                "auxiliary_accounts": [memory_profile],
            }
        ),
        encoding="utf-8",
    )
    memory_manifest = tmp_path / "memory-manifest.json"
    memory_manifest.write_text(
        json.dumps({"schema_version": 1, "profiles": [memory_profile]}),
        encoding="utf-8",
    )
    env = _env()
    codex_auth = tmp_path / "auth.json"
    codex_auth.write_bytes(base64.b64decode(env["QA_CODEX_AUTH_JSON_B64"]))
    fixture = tmp_path / "persona-fixture.json"
    fixture.write_text(
        json.dumps(
            {
                "privacy": {
                    "forbidden_in_agent_identity_or_persona": ["QA-PRIVATE-CANARY-7F3A"]
                }
            }
        ),
        encoding="utf-8",
    )
    return ScanInputs(
        artifacts=artifacts,
        manifest=manifest,
        memory_manifest=memory_manifest,
        codex_auth=codex_auth,
        fixture=fixture,
        canonical_result=canonical_result,
        env=env,
    )


def _identifier_samples(document: dict) -> dict[str, str]:
    profile = document["profiles"][0]
    turn = profile["turns"][0]
    persona = next(
        row["persona_finalizer"]
        for row in profile["scenarios"]
        if row["scenario_id"] == "P0-06"
    )
    return {
        "user": profile["user_id"],
        "request": turn["request_id"],
        "turn": turn["turn_id"],
        "trace": turn["trace_id"],
        "job": persona["job_id"],
    }


def test_realistic_canonical_result_and_hashed_team_handles_pass(tmp_path):
    inputs = _write_inputs(tmp_path)
    canonical_text = inputs.canonical_result.read_text(encoding="utf-8")
    team_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(inputs.artifacts.iterdir())
    )

    assert _identifier_samples(inputs.canonical())["user"] in canonical_text
    assert _identifier_samples(inputs.canonical())["user"] not in team_text
    assert _handle(_identifier_samples(inputs.canonical())["user"]) in team_text
    assert inputs.scan() == []


def test_team_report_tree_is_exact_and_contains_no_canonical_result_or_profiles(
    tmp_path,
):
    inputs = _write_inputs(tmp_path)

    assert {path.name for path in inputs.artifacts.iterdir()} == (
        scanner.EXPECTED_PUBLIC_FILES
    )
    assert not (inputs.artifacts / "run-result.json").exists()
    assert not (inputs.artifacts / "profiles").exists()
    assert inputs.scan() == []


@pytest.mark.parametrize("kind", ("user", "request", "turn", "trace", "job"))
def test_raw_canonical_identifier_leak_fails_without_echoing_value(tmp_path, kind):
    inputs = _write_inputs(tmp_path)
    identifier = _identifier_samples(inputs.canonical())[kind]
    (inputs.artifacts / "failure-index.json").write_text(
        json.dumps({"schema_version": 1, "leaked_value": identifier}),
        encoding="utf-8",
    )

    findings = inputs.scan()

    assert findings == [
        "public artifact contains forbidden synthetic identifier material"
    ]
    assert identifier not in "\n".join(findings)


@pytest.mark.parametrize("representation", ("base64", "urlsafe_base64", "hex"))
def test_encoded_canonical_identifier_leak_fails(tmp_path, representation):
    inputs = _write_inputs(tmp_path)
    identifier = _identifier_samples(inputs.canonical())["request"]
    raw = identifier.encode("utf-8")
    encoded = {
        "base64": base64.b64encode(raw),
        "urlsafe_base64": base64.urlsafe_b64encode(raw),
        "hex": raw.hex().encode("ascii"),
    }[representation]
    (inputs.artifacts / "team-summary.md").write_bytes(encoded)

    assert inputs.scan() == [
        "public artifact contains forbidden synthetic identifier material"
    ]


def test_canonical_identifier_split_across_json_strings_fails(tmp_path):
    inputs = _write_inputs(tmp_path)
    identifier = _identifier_samples(inputs.canonical())["request"]
    split = len(identifier) // 2
    document = {
        "first": f"safe-prefix::{identifier[:split]}::safe-suffix",
        "nested": [{"second": f"safe-prefix::{identifier[split:]}::safe-suffix"}],
    }
    serialized = json.dumps(document)
    assert identifier not in serialized
    (inputs.artifacts / "failure-index.json").write_text(serialized, encoding="utf-8")

    assert inputs.scan() == [
        "public artifact contains forbidden synthetic identifier material"
    ]


@pytest.mark.parametrize(
    "field",
    (
        "user_id",
        "request_id",
        "turn_id",
        "trace_id",
        "job_id",
        "session_id",
        "prompt",
        "response",
        "rationale",
        "raw_reply",
        "raw_private_reasoning",
    ),
)
def test_private_data_keys_are_globally_forbidden(tmp_path, field):
    inputs = _write_inputs(tmp_path)
    (inputs.artifacts / "failure-index.json").write_text(
        json.dumps({field: "safe-noncanonical-value"}), encoding="utf-8"
    )

    assert inputs.scan() == ["public artifact contains a forbidden private-data field"]


def test_allowlisted_raw_attestations_are_safe_only_at_their_locked_values(tmp_path):
    inputs = _write_inputs(tmp_path)
    path = inputs.artifacts / "failure-index.json"
    path.write_text(
        json.dumps(
            {
                "raw_chat_omitted": True,
                "raw_trace_stored": False,
                "raw_responses_persisted": False,
            }
        ),
        encoding="utf-8",
    )
    assert inputs.scan() == []

    path.write_text(json.dumps({"raw_chat_omitted": False}), encoding="utf-8")
    assert inputs.scan() == ["public artifact contains a forbidden private-data field"]


def _credential_sample(inputs: ScanInputs, source: str) -> bytes:
    manifest = json.loads(inputs.manifest.read_text(encoding="utf-8"))
    memory = json.loads(inputs.memory_manifest.read_text(encoding="utf-8"))["profiles"][
        0
    ]
    auth = json.loads(inputs.codex_auth.read_text(encoding="utf-8"))
    if source == "provider":
        return inputs.env["QA_OPENROUTER_API_KEY"].encode("utf-8")
    if source == "account":
        return manifest["profiles"][0]["api_key"].encode("utf-8")
    if source == "content_private":
        return base64.b64decode(manifest["profiles"][0]["secret_key_b64"])
    if source == "memory_account":
        return memory["api_key"].encode("utf-8")
    if source == "memory_private":
        return base64.b64decode(memory["secret_key_b64"])
    if source == "oauth":
        return auth["tokens"]["access_token"].encode("utf-8")
    if source == "absence":
        return manifest["profiles"][0]["synthetic_account_lease"][
            "absence_token"
        ].encode("utf-8")
    if source == "memory_absence":
        return memory["synthetic_account_lease"]["absence_token"].encode("utf-8")
    raise AssertionError(source)


@pytest.mark.parametrize(
    "source",
    (
        "provider",
        "account",
        "content_private",
        "memory_account",
        "memory_private",
        "oauth",
        "absence",
        "memory_absence",
    ),
)
def test_exact_credentials_and_attestations_are_scanned(tmp_path, source):
    inputs = _write_inputs(tmp_path)
    secret = _credential_sample(inputs, source)
    (inputs.artifacts / "team-summary.md").write_bytes(secret)

    assert inputs.scan() == ["public artifact contains exact credential material"]


@pytest.mark.parametrize("representation", ("base64", "urlsafe_base64", "hex"))
def test_encoded_provider_secret_fails(tmp_path, representation):
    inputs = _write_inputs(tmp_path)
    secret = inputs.env["QA_DEEPSEEK_API_KEY"].encode("utf-8")
    payload = {
        "base64": base64.b64encode(secret),
        "urlsafe_base64": base64.urlsafe_b64encode(secret),
        "hex": secret.hex().encode("ascii"),
    }[representation]
    (inputs.artifacts / "latency.csv").write_bytes(payload)

    assert inputs.scan() == ["public artifact contains exact credential material"]


def test_provider_secret_split_across_padded_json_strings_fails(tmp_path):
    inputs = _write_inputs(tmp_path)
    secret = "fedcba9876543210fedcba9876543210"
    inputs.env["QA_OPENROUTER_API_KEY"] = secret
    first = len(secret) // 3
    second = first * 2
    document = {
        "first": f"safe-prefix::{secret[:first]}::safe-suffix",
        "nested": [
            f"safe-prefix::{secret[first:second]}::safe-suffix",
            {"third": f"safe-prefix::{secret[second:]}::safe-suffix"},
        ],
    }
    serialized = json.dumps(document)
    assert secret not in serialized
    (inputs.artifacts / "failure-index.json").write_text(serialized, encoding="utf-8")

    assert inputs.scan() == ["public artifact contains exact credential material"]


def test_reordered_secret_fragments_do_not_false_positive(tmp_path):
    inputs = _write_inputs(tmp_path)
    secret = inputs.env["QA_OPENROUTER_API_KEY"]
    split = len(secret) // 2
    (inputs.artifacts / "failure-index.json").write_text(
        json.dumps(
            {
                "second": f"prefix::{secret[split:]}::suffix",
                "first": f"prefix::{secret[:split]}::suffix",
            }
        ),
        encoding="utf-8",
    )

    assert inputs.scan() == []


@pytest.mark.parametrize("representation", ("raw", "base64", "urlsafe", "hex"))
def test_forbidden_persona_canary_fails_in_encoded_forms(tmp_path, representation):
    inputs = _write_inputs(tmp_path)
    marker = "fixture-private-ÿ-marker"
    inputs.fixture.write_text(
        json.dumps({"privacy": {"forbidden_in_agent_identity_or_persona": [marker]}}),
        encoding="utf-8",
    )
    raw = marker.encode("utf-8")
    payload = {
        "raw": raw,
        "base64": base64.b64encode(raw),
        "urlsafe": base64.urlsafe_b64encode(raw),
        "hex": raw.hex().encode("ascii"),
    }[representation]
    (inputs.artifacts / "team-summary.md").write_bytes(payload)

    assert inputs.scan() == [
        "public artifact contains forbidden persona fixture material"
    ]


def test_forbidden_persona_canary_split_across_json_fails(tmp_path):
    inputs = _write_inputs(tmp_path)
    marker = "QA-PRIVATE-CANARY-7F3A"
    document = {"first": marker[:12], "nested": [{"second": marker[12:]}]}
    serialized = json.dumps(document)
    assert marker not in serialized
    (inputs.artifacts / "failure-index.json").write_text(serialized, encoding="utf-8")

    assert inputs.scan() == [
        "public artifact contains forbidden persona fixture material"
    ]


def test_credential_shaped_token_fails(tmp_path):
    inputs = _write_inputs(tmp_path)
    (inputs.artifacts / "matrix.md").write_text(
        "accidental sk-ant-this-looks-like-a-token", encoding="utf-8"
    )

    assert inputs.scan() == ["public artifact contains a credential-shaped token"]


def test_missing_scan_secret_fails_closed(tmp_path):
    inputs = _write_inputs(tmp_path)
    del inputs.env["IO_E2E_ADMIN_TOKEN"]

    with pytest.raises(scanner.ArtifactScanError, match="inputs are incomplete"):
        inputs.scan()


def test_memory_manifest_must_match_provisioning_auxiliary_account(tmp_path):
    inputs = _write_inputs(tmp_path)
    memory = json.loads(inputs.memory_manifest.read_text(encoding="utf-8"))
    memory["profiles"][0]["api_key"] = "different-memory-account-key"
    inputs.memory_manifest.write_text(json.dumps(memory), encoding="utf-8")

    with pytest.raises(scanner.ArtifactScanError, match="shape is invalid"):
        inputs.scan()


@pytest.mark.parametrize("source", ("provider", "memory"))
def test_missing_synthetic_absence_attestation_fails_closed(tmp_path, source):
    inputs = _write_inputs(tmp_path)
    provisioning = json.loads(inputs.manifest.read_text(encoding="utf-8"))
    if source == "provider":
        del provisioning["profiles"][0]["synthetic_account_lease"]["absence_token"]
        inputs.manifest.write_text(json.dumps(provisioning), encoding="utf-8")
    else:
        memory = json.loads(inputs.memory_manifest.read_text(encoding="utf-8"))
        del memory["profiles"][0]["synthetic_account_lease"]["absence_token"]
        provisioning["auxiliary_accounts"] = memory["profiles"]
        inputs.manifest.write_text(json.dumps(provisioning), encoding="utf-8")
        inputs.memory_manifest.write_text(json.dumps(memory), encoding="utf-8")

    with pytest.raises(scanner.ArtifactScanError, match="attestation is incomplete"):
        inputs.scan()


def test_symlink_fails_without_reading_target(tmp_path):
    inputs = _write_inputs(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(inputs.env["QA_DEEPSEEK_API_KEY"], encoding="utf-8")
    try:
        os.symlink(outside, inputs.artifacts / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    assert inputs.scan() == ["public artifact tree contains a symbolic link"]


def test_extra_and_missing_artifact_fail_closed(tmp_path):
    inputs = _write_inputs(tmp_path)
    (inputs.artifacts / "notes.txt").write_text("extra", encoding="utf-8")
    (inputs.artifacts / "junit.xml").unlink()

    assert inputs.scan() == [
        "public artifact tree contains an unexpected file",
        "public artifact tree is missing required files",
    ]


def test_unexpected_directory_fails_closed(tmp_path):
    inputs = _write_inputs(tmp_path)
    (inputs.artifacts / "profiles").mkdir()

    assert inputs.scan() == ["public artifact tree contains an unexpected directory"]


def test_oversized_artifact_fails_closed(tmp_path, monkeypatch):
    inputs = _write_inputs(tmp_path)
    monkeypatch.setattr(scanner, "MAX_FILE_BYTES", 8)

    assert inputs.scan() == ["public artifact per-file size limit exceeded"]


def test_canonical_result_must_be_external_regular_validated_staging_file(tmp_path):
    inputs = _write_inputs(tmp_path)
    inside = inputs.artifacts / "run-result.json"
    inside.write_text(
        inputs.canonical_result.read_text(encoding="utf-8"), encoding="utf-8"
    )
    inputs.canonical_result = inside

    with pytest.raises(scanner.ArtifactScanError, match="must remain outside"):
        inputs.scan()


def test_canonical_result_symlink_fails_closed(tmp_path):
    inputs = _write_inputs(tmp_path)
    target = tmp_path / "target" / "run-result.json"
    target.parent.mkdir()
    target.write_text(
        inputs.canonical_result.read_text(encoding="utf-8"), encoding="utf-8"
    )
    inputs.canonical_result.unlink()
    try:
        os.symlink(target, inputs.canonical_result)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(scanner.ArtifactScanError, match="missing or unreadable"):
        inputs.scan()


def test_canonical_result_rejects_non_synthetic_profiles(tmp_path):
    inputs = _write_inputs(tmp_path)
    canonical = inputs.canonical()
    canonical["profiles"][0]["redaction"]["synthetic_users_only"] = False
    inputs.write_canonical(canonical)

    with pytest.raises(scanner.ArtifactScanError, match="shape is invalid"):
        inputs.scan()


def test_canonical_result_rejects_invalid_identifier_shape(tmp_path):
    inputs = _write_inputs(tmp_path)
    canonical = inputs.canonical()
    canonical["profiles"][0]["user_id"] = "short"
    inputs.write_canonical(canonical)

    with pytest.raises(scanner.ArtifactScanError, match="identifier shape is invalid"):
        inputs.scan()


def test_cli_requires_canonical_result_and_never_prints_detected_material(
    tmp_path, monkeypatch, capsys
):
    inputs = _write_inputs(tmp_path)
    identifier = _identifier_samples(inputs.canonical())["trace"]
    (inputs.artifacts / "matrix.md").write_text(identifier, encoding="utf-8")
    for name, value in inputs.env.items():
        monkeypatch.setenv(name, value)

    rc = scanner.main(
        [
            "--artifacts",
            str(inputs.artifacts),
            "--manifest",
            str(inputs.manifest),
            "--memory-manifest",
            str(inputs.memory_manifest),
            "--codex-auth",
            str(inputs.codex_auth),
            "--fixture",
            str(inputs.fixture),
            "--canonical-result",
            str(inputs.canonical_result),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert identifier not in captured.out + captured.err


def test_realistic_fixture_detects_every_canonical_identifier_location(tmp_path):
    inputs = _write_inputs(tmp_path)
    canonical = deepcopy(inputs.canonical())
    profile = canonical["profiles"][0]
    profile["user_id"] = "user-unique-location-0001"
    scenario = next(
        row for row in profile["scenarios"] if row["scenario_id"] == "P0-08"
    )
    scenario["request_ids"] = ["scenario-request-unique-0001"]
    scenario["turn_ids"] = ["scenario-turn-unique-0001"]
    scenario["trace_ids"] = ["scenario-trace-unique-0001"]
    turn = profile["turns"][0]
    turn["request_id"] = "turn-request-unique-0001"
    turn["turn_id"] = "turn-id-unique-location-0001"
    turn["trace_id"] = "turn-trace-unique-0001"
    profile["reasoning"]["request_id"] = "reason-request-unique-0001"
    profile["reasoning"]["turn_id"] = "reason-turn-unique-0001"
    profile["reasoning"]["trace_id"] = "reason-trace-unique-0001"
    persona = next(
        row["persona_finalizer"]
        for row in profile["scenarios"]
        if row["scenario_id"] == "P0-06"
    )
    persona["request_id"] = "persona-request-unique-0001"
    persona["job_id"] = "persona-job-unique-0001"
    expected = {
        profile["user_id"],
        *scenario["request_ids"],
        *scenario["turn_ids"],
        *scenario["trace_ids"],
        turn["request_id"],
        turn["turn_id"],
        turn["trace_id"],
        profile["reasoning"]["request_id"],
        profile["reasoning"]["turn_id"],
        profile["reasoning"]["trace_id"],
        persona["request_id"],
        persona["job_id"],
    }
    variants = scanner._canonical_identifier_values(canonical)

    for identifier in expected:
        assert identifier.encode("utf-8") in variants
        assert base64.b64encode(identifier.encode("utf-8")) in variants
