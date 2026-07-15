from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from qa import scan_artifacts as scanner


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


def _write_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, dict[str, str]]:
    artifacts = tmp_path / "public"
    artifacts.mkdir()
    (artifacts / "run-result.json").write_text(
        json.dumps({"redaction": {"provider_keys_omitted": True}}),
        encoding="utf-8",
    )
    (artifacts / "cleanup-receipt.json").write_text(
        json.dumps({"kind": "deterministic_cleanup_receipt"}),
        encoding="utf-8",
    )
    (artifacts / "memory-contract.json").write_text("{}\n", encoding="utf-8")
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
    (artifacts / "matrix.md").write_text("matrix\n", encoding="utf-8")
    (artifacts / "latency.csv").write_text("latency_ms\n", encoding="utf-8")
    (artifacts / "junit.xml").write_text("<testsuite/>\n", encoding="utf-8")
    public_profiles = artifacts / "profiles"
    public_profiles.mkdir()
    for profile_id in scanner.PROFILE_IDS:
        (public_profiles / f"{profile_id}.json").write_text("{}\n", encoding="utf-8")
    profiles = [
        {
            "profile_id": f"profile-{index}",
            "api_key": f"feedling-sensitive-account-{index}",
            "secret_key_b64": base64.b64encode(bytes([index + 1]) * 32).decode(),
            "synthetic_account_lease": {
                "absence_token": f"{index + 1:064x}",
            },
        }
        for index in range(len(scanner.PROFILE_IDS))
    ]
    memory_profile = {
        "profile_id": scanner.MEMORY_CONTRACT_PROFILE_ID,
        "api_key": "feedling-sensitive-memory-account",
        "secret_key_b64": base64.b64encode(bytes([99]) * 32).decode(),
        "synthetic_account_lease": {
            "absence_token": f"{99:064x}",
        },
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
    return artifacts, manifest, memory_manifest, codex_auth, fixture, env


def test_clean_public_boundary_passes(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    assert (
        scanner.scan_artifacts(
            artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
        )
        == []
    )


@pytest.mark.parametrize("source", ("provider", "account", "content_private"))
def test_exact_secret_material_fails_without_returning_value(tmp_path, source):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    doc = json.loads(manifest.read_text())
    if source == "provider":
        secret = env["QA_OPENROUTER_API_KEY"]
    elif source == "account":
        secret = doc["profiles"][0]["api_key"]
    else:
        secret = doc["profiles"][0]["secret_key_b64"]
    (artifacts / "matrix.md").write_text(f"leak={secret}", encoding="utf-8")

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains exact credential material"]
    assert secret not in "\n".join(findings)


@pytest.mark.parametrize("secret_name", ("QA_GEMINI_API_KEY", "QA_KONGBEIQIE_API_KEY"))
def test_new_profile_provider_secrets_are_scanned(tmp_path, secret_name):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    secret = env[secret_name]
    (artifacts / "matrix.md").write_text(secret, encoding="utf-8")

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains exact credential material"]
    assert secret not in "\n".join(findings)


def test_forbidden_raw_field_fails(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    (artifacts / "profiles" / "official-deepseek.json").write_text(
        json.dumps({"body_ct": "not-even-a-real-ciphertext"}), encoding="utf-8"
    )
    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == ["public artifact contains a forbidden private-data field"]


@pytest.mark.parametrize(
    "field",
    (
        "prompt",
        "response",
        "rationale",
        "evidence_turn_ids",
        "account_fingerprints",
        "turn_id",
        "session_id",
        "request_id",
        "response_id",
        "trace_id",
    ),
)
def test_persona_summary_rejects_private_evidence_fields(tmp_path, field):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    (artifacts / "persona-memory-summary.json").write_text(
        json.dumps({field: "private-value"}), encoding="utf-8"
    )

    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == ["public artifact contains a forbidden private-data field"]


def test_encoded_provider_secret_and_decoded_private_key_fail(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    profiles = json.loads(manifest.read_text())["profiles"]
    encoded_provider = base64.b64encode(env["QA_DEEPSEEK_API_KEY"].encode())
    decoded_private = base64.b64decode(profiles[0]["secret_key_b64"])
    (artifacts / "latency.csv").write_bytes(encoded_provider + b"\n" + decoded_private)
    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == ["public artifact contains exact credential material"]


@pytest.mark.parametrize("representation", ("urlsafe_base64", "hex"))
def test_encoded_decoded_private_key_fails(tmp_path, representation):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    document = json.loads(manifest.read_text())
    decoded_private = bytes((0xFB, 0xFF)) * 16
    document["profiles"][0]["secret_key_b64"] = base64.b64encode(
        decoded_private
    ).decode("ascii")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    encoded = {
        "urlsafe_base64": base64.urlsafe_b64encode(decoded_private),
        "hex": decoded_private.hex().encode("ascii"),
    }[representation]
    (artifacts / "latency.csv").write_bytes(encoded)

    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == ["public artifact contains exact credential material"]


def test_provider_secret_split_across_ordered_json_string_fields_fails(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    secret = "0123456789abcdef0123456789abcdef"
    env["QA_OPENROUTER_API_KEY"] = secret
    first = len(secret) // 3
    second = first * 2
    document = {
        "first_fragment": secret[:first],
        "nested_fragments": [
            {"second_fragment": secret[first:second]},
            {"unrelated": "fixed-safe-interleaved-value"},
            {"third_fragment": secret[second:]},
        ],
    }
    serialized = json.dumps(document)
    assert secret not in serialized
    (artifacts / "profiles" / "openrouter-claude.json").write_text(
        serialized,
        encoding="utf-8",
    )

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains exact credential material"]
    assert secret not in "\n".join(findings)


def test_provider_secret_split_across_padded_json_strings_fails(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    secret = "fedcba9876543210fedcba9876543210"
    env["QA_OPENROUTER_API_KEY"] = secret
    first = len(secret) // 3
    second = first * 2
    document = {
        "first": f"safe-prefix::{secret[:first]}::safe-suffix",
        "ignored": "fixed-safe-interleaved-value",
        "nested": [
            f"another-prefix::{secret[first:second]}::another-suffix",
            {"third": f"last-prefix::{secret[second:]}::last-suffix"},
        ],
    }
    serialized = json.dumps(document)
    assert secret not in serialized
    (artifacts / "profiles" / "openrouter-claude.json").write_text(
        serialized,
        encoding="utf-8",
    )

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains exact credential material"]


def test_reordered_padded_json_secret_fragments_do_not_false_positive(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    secret = env["QA_OPENROUTER_API_KEY"]
    split = len(secret) // 2
    serialized = json.dumps(
        {
            "second": f"prefix::{secret[split:]}::suffix",
            "first": f"prefix::{secret[:split]}::suffix",
        }
    )
    assert secret not in serialized
    (artifacts / "profiles" / "openrouter-claude.json").write_text(
        serialized,
        encoding="utf-8",
    )

    assert (
        scanner.scan_artifacts(
            artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
        )
        == []
    )


def test_credential_shaped_token_fails(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    (artifacts / "matrix.md").write_text(
        "accidental sk-ant-this-looks-like-a-token", encoding="utf-8"
    )
    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == ["public artifact contains a credential-shaped token"]


def test_missing_scan_secret_fails_closed(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    del env["QA_TEST_ADMIN_TOKEN"]
    with pytest.raises(scanner.ArtifactScanError, match="inputs are incomplete"):
        scanner.scan_artifacts(
            artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
        )


def test_symlink_fails_without_reading_target(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    outside = tmp_path / "outside.txt"
    outside.write_text(env["QA_DEEPSEEK_API_KEY"], encoding="utf-8")
    try:
        os.symlink(outside, artifacts / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == ["public artifact tree contains a symbolic link"]


def test_extra_or_missing_artifact_fails_closed(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    (artifacts / "notes.txt").write_text("extra", encoding="utf-8")
    (artifacts / "junit.xml").unlink()
    assert scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    ) == [
        "public artifact tree contains an unexpected file",
        "public artifact tree is missing required files",
    ]


def test_cli_does_not_print_detected_secret(tmp_path, monkeypatch, capsys):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    secret = env["QA_ANTHROPIC_API_KEY"]
    (artifacts / "matrix.md").write_text(secret, encoding="utf-8")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    rc = scanner.main(
        [
            "--artifacts",
            str(artifacts),
            "--manifest",
            str(manifest),
            "--memory-manifest",
            str(memory_manifest),
            "--codex-auth",
            str(codex_auth),
            "--fixture",
            str(fixture),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize("field", ("api_key", "secret_key_b64"))
def test_memory_contract_account_secrets_are_scanned(tmp_path, field):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    memory = json.loads(memory_manifest.read_text())["profiles"][0]
    secret = memory[field]
    payload = (
        base64.b64decode(secret)
        if field == "secret_key_b64"
        else secret.encode("utf-8")
    )
    (artifacts / "matrix.md").write_bytes(payload)

    findings = scanner.scan_artifacts(
        artifacts,
        manifest,
        memory_manifest,
        codex_auth,
        fixture,
        env=env,
    )

    assert findings == ["public artifact contains exact credential material"]
    assert secret not in "\n".join(findings)


@pytest.mark.parametrize("source", ("provider", "memory"))
def test_synthetic_absence_attestations_are_scanned(tmp_path, source):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    provisioning = json.loads(manifest.read_text())
    if source == "provider":
        secret = provisioning["profiles"][0]["synthetic_account_lease"]["absence_token"]
    else:
        secret = json.loads(memory_manifest.read_text())["profiles"][0][
            "synthetic_account_lease"
        ]["absence_token"]
    (artifacts / "matrix.md").write_text(secret, encoding="utf-8")

    findings = scanner.scan_artifacts(
        artifacts,
        manifest,
        memory_manifest,
        codex_auth,
        fixture,
        env=env,
    )

    assert findings == ["public artifact contains exact credential material"]
    assert secret not in "\n".join(findings)


@pytest.mark.parametrize("source", ("provider", "memory"))
def test_missing_synthetic_absence_attestation_fails_closed(tmp_path, source):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    provisioning = json.loads(manifest.read_text())
    if source == "provider":
        del provisioning["profiles"][0]["synthetic_account_lease"]["absence_token"]
        manifest.write_text(json.dumps(provisioning), encoding="utf-8")
    else:
        memory = json.loads(memory_manifest.read_text())
        del memory["profiles"][0]["synthetic_account_lease"]["absence_token"]
        provisioning["auxiliary_accounts"] = memory["profiles"]
        manifest.write_text(json.dumps(provisioning), encoding="utf-8")
        memory_manifest.write_text(json.dumps(memory), encoding="utf-8")

    with pytest.raises(scanner.ArtifactScanError, match="attestation is incomplete"):
        scanner.scan_artifacts(
            artifacts,
            manifest,
            memory_manifest,
            codex_auth,
            fixture,
            env=env,
        )


def test_memory_manifest_must_match_provisioning_auxiliary_account(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    memory = json.loads(memory_manifest.read_text())
    memory["profiles"][0]["api_key"] = "different-memory-account-key"
    memory_manifest.write_text(json.dumps(memory), encoding="utf-8")

    with pytest.raises(scanner.ArtifactScanError, match="shape is invalid"):
        scanner.scan_artifacts(
            artifacts,
            manifest,
            memory_manifest,
            codex_auth,
            fixture,
            env=env,
        )


def test_nonobject_memory_manifest_fails_with_sanitized_error(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    memory_manifest.write_text("[]", encoding="utf-8")

    with pytest.raises(scanner.ArtifactScanError, match="shape is invalid"):
        scanner.scan_artifacts(
            artifacts,
            manifest,
            memory_manifest,
            codex_auth,
            fixture,
            env=env,
        )


@pytest.mark.parametrize("field", ("id_token", "access_token", "refresh_token"))
def test_decoded_codex_oauth_material_fails(tmp_path, field):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    auth = json.loads(codex_auth.read_text(encoding="utf-8"))
    secret = auth["tokens"][field]
    (artifacts / "matrix.md").write_text(secret, encoding="utf-8")

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains exact credential material"]
    assert secret not in "\n".join(findings)


@pytest.mark.parametrize(
    "representation",
    ("raw", "base64", "urlsafe_base64", "hex"),
)
def test_forbidden_persona_fixture_material_fails_in_encoded_forms(
    tmp_path, representation
):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    marker = "fixture-private-ÿ-marker"
    fixture.write_text(
        json.dumps({"privacy": {"forbidden_in_agent_identity_or_persona": [marker]}}),
        encoding="utf-8",
    )
    raw = marker.encode("utf-8")
    payloads = {
        "raw": raw,
        "base64": base64.b64encode(raw),
        "urlsafe_base64": base64.urlsafe_b64encode(raw),
        "hex": raw.hex().encode("ascii"),
    }
    (artifacts / "matrix.md").write_bytes(payloads[representation])

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains forbidden persona fixture material"]
    assert marker not in "\n".join(findings)


def test_forbidden_persona_fixture_material_split_across_json_fails(tmp_path):
    artifacts, manifest, memory_manifest, codex_auth, fixture, env = _write_inputs(
        tmp_path
    )
    marker = "QA-PRIVATE-CANARY-7F3A"
    document = {"a": marker[:8], "nested": [{"b": marker[8:17]}, marker[17:]]}
    serialized = json.dumps(document)
    assert marker not in serialized
    (artifacts / "profiles" / "official-openai.json").write_text(
        serialized,
        encoding="utf-8",
    )

    findings = scanner.scan_artifacts(
        artifacts, manifest, memory_manifest, codex_auth, fixture, env=env
    )

    assert findings == ["public artifact contains forbidden persona fixture material"]
