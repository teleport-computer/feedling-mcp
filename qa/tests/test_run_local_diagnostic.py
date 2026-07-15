from __future__ import annotations

import base64
import hashlib
import json
import shlex
import stat
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from qa import run_local_diagnostic as local
from qa import run_codex_profile_workers as workers
from qa import verify_codex_orchestration as orchestration
from qa import diagnostic_results
from qa import validate_diagnostic_attempts as diagnostic_attempts


def _write_private(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _qualification_runtime(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "qualification-runtime"
    binary_dir = runtime / "bin"
    binary_dir.mkdir(parents=True, mode=0o755)
    runtime.chmod(0o755)
    binary_dir.chmod(0o755)
    executable = binary_dir / "python3"
    executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
    )
    executable.chmod(0o700)
    return runtime, executable


def _auth_document() -> dict:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "header.payload.signature-for-local-diagnostic",
            "access_token": "access-token-for-local-diagnostic-account",
            "refresh_token": "refresh-token-for-local-diagnostic-account",
            "account_id": "account-local-diagnostic",
        },
        "last_refresh": "2026-07-14T00:00:00Z",
        "agent_identity": None,
        "personal_access_token": None,
        "bedrock_api_key": None,
    }


def _parent_cleanup_deferred_profile(profile_id: str) -> dict:
    scenarios = [
        {"scenario_id": f"P0-{index:02d}", "status": "PASS"}
        for index in range(1, 13)
    ]
    deferred_failure = dict(diagnostic_attempts.PARENT_CLEANUP_DEFERRED_FAILURE)
    scenarios.append(
        {
            "scenario_id": "P0-13",
            "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
            "attempts": 1,
            "attempt_results": [
                {
                    "attempt": 1,
                    "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
                    "failure": dict(deferred_failure),
                }
            ],
            "assertions": {
                "trace_stages_complete": True,
                "trace_correlation_confirmed": True,
                "latency_attributed": True,
                "cleanup_confirmed": False,
            },
            "evidence_codes": [
                "TRACE_CORRELATION_CONFIRMED",
                "LATENCY_ATTRIBUTED",
            ],
            "failure": deferred_failure,
        }
    )
    return {
        "profile_id": profile_id,
        "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
        "scenarios": scenarios,
        "cleanup": {
            "attempted": False,
            "provider_config_deleted": False,
            "account_reset": False,
            "old_credential_rejected": False,
            "status": diagnostic_attempts.PARENT_CLEANUP_DEFERRED_STATUS,
        },
        "diagnostic_codes": ["CLEANUP_FALLBACK_USED"],
    }


def _schema_instance(node: dict, definitions: dict) -> object:
    if "$ref" in node:
        return _schema_instance(
            definitions[node["$ref"].removeprefix("#/$defs/")], definitions
        )
    if "enum" in node:
        return node["enum"][0]
    if "anyOf" in node:
        return _schema_instance(node["anyOf"][0], definitions)
    node_type = node.get("type")
    if isinstance(node_type, list):
        node_type = next(value for value in node_type if value != "null")
    if node_type == "object":
        return {
            name: _schema_instance(child, definitions)
            for name, child in node.get("properties", {}).items()
        }
    if node_type == "array":
        return []
    if node_type == "string":
        return "safe"
    if node_type in ("integer", "number"):
        return 0
    if node_type == "boolean":
        return True
    if node_type == "null":
        return None
    raise AssertionError(node)


def _options(
    tmp_path: Path,
    *,
    preflight_only: bool,
    env_text: str = (
        "QA_GEMINI_API_KEY=gemini-sensitive-value\n"
        "FEEDLING_ADMIN_TOKEN=test-admin-token\n"
    ),
) -> local.DiagnosticOptions:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "qa").mkdir()
    (source / "qa" / "fixtures").mkdir()
    (source / "qa" / "fixtures" / "persona-import-v1.json").write_text(
        json.dumps(
            {
                "privacy": {
                    "forbidden_in_agent_identity_or_persona": [
                        "QA-PRIVATE-CANARY-7F3A"
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "qa" / "coverage-lock.json").write_text("{}\n", encoding="utf-8")
    (source / "tools" / "provider_smoke").mkdir(parents=True)
    (source / "tools" / "genesis_e2e.py").write_text(
        "# synthetic genesis driver\n", encoding="utf-8"
    )
    (source / "backend").mkdir()
    (source / "backend" / "content_encryption.py").write_text(
        "# synthetic content encryption helper\n", encoding="utf-8"
    )
    env_file = _write_private(tmp_path / "external.env", env_text)
    auth_file = _write_private(
        tmp_path / "auth.json", json.dumps(_auth_document()) + "\n"
    )
    runtime_root, worker_python = _qualification_runtime(tmp_path)
    return local.DiagnosticOptions(
        env_file=env_file,
        candidate_sha="a" * 40,
        codex_model="gpt-5.4",
        profile_ids=("official-gemini",),
        preflight_only=preflight_only,
        source_root=source,
        auth_file=auth_file,
        private_base=tmp_path / "private-runs",
        codex_bin=Path("/usr/bin/true"),
        worker_python=worker_python,
        worker_runtime_roots=(runtime_root,),
    )


def _codex_preflight_receipt(
    options: local.DiagnosticOptions, paths: local.RunPaths
) -> dict:
    assert (paths.codex_home / "auth.json").exists()
    return {
        "headless_exec_completed": True,
        "structured_output_valid": True,
        "event_stream_valid": True,
        "tool_calls_observed": False,
        "profile_id": options.profile_ids[0],
        "model": options.codex_model,
    }


def _harness_provenance_receipt(_source_root: Path) -> dict:
    return {
        "git_head": "b" * 40,
        "dirty": True,
        "source_sha256": "c" * 64,
        "worker_source_sha256": local.harness_provenance.snapshot_digest(
            _source_root
        ),
    }


def _deployment_verification(
    expected_sha,
    receipt_path,
    *,
    env,
    expected_runtime,
) -> dict:
    assert env["QA_FEEDLING_BASE_URL"] == "https://test-api.feedling.app"
    assert env["QA_TEST_ADMIN_TOKEN"]
    sha = expected_sha or "a" * 40
    receipt = {
        "schema_version": 1,
        "environment": "test",
        "base_url": "https://test-api.feedling.app",
        "expected_runtime": expected_runtime,
        "expected_deployment_sha": sha,
        "observed_backend_sha": sha,
        "observed_deployment_sha": sha,
        "observed_worker_sha": None,
        "live_worker_count": None,
        "runtime_evidence_source": (
            "per_profile_runtime_readback_and_live_scenarios"
            if expected_runtime == "hosted_resident"
            else "deployed_runtime_readback"
        ),
        "liveness_verified": True,
        "deployment_identity_verified": True,
        "verified_at": "2026-07-14T00:00:00+00:00",
    }
    local._write_private_json(receipt_path, receipt)
    receipt_path.chmod(0o400)
    return receipt


def _fake_codex_package(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    machine = local.platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    package_dir = "codex-test-platform"
    target = "test-target"
    package_root = (
        tmp_path
        / "install"
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / package_dir
    )
    files = {
        Path("README.md"): b"official fixture\n",
        Path("package.json"): json.dumps(
            {"name": "@openai/codex", "version": "0.144.3-test-platform"}
        ).encode(),
        Path("vendor", target, "bin", "codex"): b"native-codex-fixture\n",
        Path("vendor", target, "bin", "codex-code-mode-host"): b"host\n",
        Path("vendor", target, "codex-package.json"): b"{}\n",
        Path("vendor", target, "codex-path", "rg"): b"rg\n",
        Path("vendor", target, "codex-resources", "zsh", "bin", "zsh"): (
            b"zsh\n"
        ),
    }
    tree_digest = hashlib.sha256()
    for relative in sorted(files, key=lambda item: item.as_posix()):
        destination = package_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(files[relative])
        encoded = relative.as_posix().encode("utf-8")
        tree_digest.update(len(encoded).to_bytes(8, "big"))
        tree_digest.update(encoded)
        tree_digest.update(len(files[relative]).to_bytes(8, "big"))
        tree_digest.update(files[relative])
    executable = package_root / "vendor" / target / "bin" / "codex"
    executable.chmod(0o700)
    monkeypatch.setattr(
        local,
        "_TRUSTED_LOCAL_CODEX",
        {
            (local.sys.platform, machine): {
                "package_dir": package_dir,
                "package_version": "0.144.3-test-platform",
                "target": target,
                "tree_sha256": tree_digest.hexdigest(),
            }
        },
    )
    return package_root, executable


def test_codex_provenance_pins_entire_native_package_tree(tmp_path, monkeypatch):
    package_root, executable = _fake_codex_package(tmp_path, monkeypatch)

    local._verify_local_codex_provenance(executable)
    (package_root / "vendor" / "test-target" / "codex-path" / "rg").write_text(
        "tampered\n", encoding="utf-8"
    )

    with pytest.raises(local.LocalDiagnosticError, match="package digest"):
        local._verify_local_codex_provenance(executable)


def test_codex_locator_never_executes_arbitrary_path_binary(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\necho 'codex-cli 0.144.3'\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setattr(local.shutil, "which", lambda _name: str(fake))
    machine = local.platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    monkeypatch.setattr(
        local,
        "_TRUSTED_LOCAL_CODEX",
        {
            (local.sys.platform, machine): {
                "package_dir": "codex-test-platform",
                "package_version": "0.144.3-test-platform",
                "target": "test-target",
                "tree_sha256": "0" * 64,
            }
        },
    )

    with pytest.raises(local.LocalDiagnosticError, match="official npm"):
        local._resolve_trusted_codex_binary()

    assert not marker.exists()


def test_codex_locator_fails_closed_before_executing_on_unsupported_platform(
    tmp_path, monkeypatch
):
    marker = tmp_path / "executed"
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\necho 'codex-cli 0.144.3'\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setattr(local.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(local, "_TRUSTED_LOCAL_CODEX", {})

    with pytest.raises(local.LocalDiagnosticError, match="no pinned Codex artifact"):
        local._resolve_trusted_codex_binary()

    assert not marker.exists()


def test_cli_wires_explicit_codex_binary_into_trusted_resolver(
    tmp_path, monkeypatch
):
    requested = tmp_path / "official-codex-wrapper"
    native = tmp_path / "verified-native-codex"
    observed: list[Path | None] = []

    def resolve(explicit):
        observed.append(explicit)
        return native

    monkeypatch.setattr(local, "_resolve_trusted_codex_binary", resolve)
    args = local._parser().parse_args(
        [
            "--candidate-sha",
            "a" * 40,
            "--codex-bin",
            str(requested),
            "--preflight-only",
        ]
    )

    options = local._options(args)

    assert observed == [requested]
    assert options.codex_bin == native
    assert options.runtime_requirement == "deployed_current"


def test_cli_runtime_v2_requirement_is_opt_in(tmp_path, monkeypatch):
    native = tmp_path / "verified-native-codex"
    monkeypatch.setattr(
        local, "_resolve_trusted_codex_binary", lambda _explicit: native
    )
    args = local._parser().parse_args(
        ["--candidate-sha", "a" * 40, "--require-runtime-v2"]
    )

    assert local._options(args).runtime_requirement == "hosted_resident"


def test_cli_candidate_sha_is_optional_for_authoritative_discovery(
    tmp_path, monkeypatch
):
    native = tmp_path / "verified-native-codex"
    monkeypatch.setattr(
        local, "_resolve_trusted_codex_binary", lambda _explicit: native
    )

    args = local._parser().parse_args(["--preflight-only"])

    assert local._options(args).candidate_sha == ""


def test_env_loader_requires_owner_only_mode_and_never_echoes_value(tmp_path):
    secret = "provider-super-sensitive-value"
    env_file = tmp_path / "external.env"
    env_file.write_text(f"QA_GEMINI_API_KEY={secret}\n", encoding="utf-8")
    env_file.chmod(0o644)

    with pytest.raises(local.LocalDiagnosticError) as exc:
        local.load_env_file(env_file)

    assert secret not in str(exc.value)
    assert "owner-only" in str(exc.value)


def test_env_loader_does_not_evaluate_shell_syntax(tmp_path):
    marker = tmp_path / "must-not-exist"
    env_file = _write_private(
        tmp_path / "external.env",
        f"QA_GEMINI_API_KEY='$(touch {marker})'\nQA_GEMINI_MODEL=gemini-2.5-flash\n",
    )

    loaded = local.load_env_file(env_file)

    assert loaded["QA_GEMINI_API_KEY"].startswith("$(touch ")
    assert not marker.exists()


def test_repo_local_dotenv_is_excluded_from_worker_source_snapshot(tmp_path):
    options = _options(tmp_path, preflight_only=True)
    source = options.source_root
    (source / "qa" / "SOP.md").write_text("safe qualification instructions\n")
    (source / "qa" / ".env.worker").write_text(
        "QA_GEMINI_API_KEY=inside-allowlist-secret\n", encoding="utf-8"
    )
    (source / ".git").write_text("gitdir: elsewhere\n")
    (source / "secrets.json").write_text(
        '{"ignored-looking":"must-not-be-copied"}\n', encoding="utf-8"
    )
    repo_env = _write_private(
        source / ".env.test",
        "QA_GEMINI_API_KEY=repo-local-provider-secret\n"
        "FEEDLING_ADMIN_TOKEN=test-admin-token\n",
    )
    (source / ".env.example").write_text(
        "QA_GEMINI_API_KEY=example-only\n", encoding="utf-8"
    )
    options = replace(options, env_file=repo_env)

    def verify_exec(
        received_options: local.DiagnosticOptions, paths: local.RunPaths
    ) -> dict:
        snapshot = paths.worker_source_root
        assert snapshot != received_options.source_root
        assert (snapshot / "qa" / "SOP.md").read_text() == (
            "safe qualification instructions\n"
        )
        assert not (snapshot / ".env.test").exists()
        assert not (snapshot / ".env.example").exists()
        assert not (snapshot / ".git").exists()
        assert not (snapshot / "qa" / ".env.worker").exists()
        assert not (snapshot / "secrets.json").exists()
        assert (snapshot / "backend" / "content_encryption.py").exists()
        assert (snapshot / "tools" / "genesis_e2e.py").exists()
        document = tomllib.loads((paths.codex_home / "config.toml").read_text())
        filesystem = document["permissions"][
            local.write_codex_config.SUPERVISOR_PERMISSION_PROFILE
        ]["filesystem"]
        assert filesystem[str(snapshot.resolve())] == "read"
        assert str(received_options.source_root.resolve()) not in filesystem
        return _codex_preflight_receipt(received_options, paths)

    dependencies = local.DiagnosticDependencies(
        provision=lambda *_args, **_kwargs: pytest.fail("unexpected provision"),
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=lambda *_args, **_kwargs: pytest.fail("unexpected launch"),
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=verify_exec,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    summary, _summary_path = local.execute(options, dependencies=dependencies)

    assert summary["status"] == "PREFLIGHT_PASS"
    assert not any(options.private_base.iterdir())


def test_subset_run_scans_every_loaded_provider_credential(tmp_path, monkeypatch):
    unselected_secret = "unselected-openai-provider-secret"
    admin_secret = "local-test-admin-secret"
    expected_model = "gemini-2.5-flash"
    expected_base_url = "https://api.kongbeiqie.com"
    options = _options(
        tmp_path,
        preflight_only=True,
        env_text=(
            "QA_GEMINI_API_KEY=selected-gemini-provider-secret\n"
            f"QA_OPENAI_PROVIDER_API_KEY={unselected_secret}\n"
            f"QA_TEST_ADMIN_TOKEN={admin_secret}\n"
            f"QA_GEMINI_MODEL={expected_model}\n"
            f"QA_KONGBEIQIE_BASE_URL={expected_base_url}\n"
        ),
    )
    observed: list[tuple[str, ...]] = []

    def capture_scan(_root, secret_values, _profile_ids):
        observed.append(tuple(secret_values))

    monkeypatch.setattr(local, "_scan_public_artifacts", capture_scan)
    dependencies = local.DiagnosticDependencies(
        provision=lambda *_args, **_kwargs: pytest.fail("unexpected provision"),
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=lambda *_args, **_kwargs: pytest.fail("unexpected launch"),
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    local.execute(options, dependencies=dependencies)

    assert observed
    assert all(unselected_secret in secrets for secrets in observed)
    assert all(admin_secret in secrets for secrets in observed)
    assert all(expected_model not in secrets for secrets in observed)
    assert all(expected_base_url not in secrets for secrets in observed)


def test_private_finalization_failure_removes_entire_raw_run_root(
    tmp_path, monkeypatch
):
    options = _options(tmp_path, preflight_only=True)
    dependencies = local.DiagnosticDependencies(
        provision=lambda *_args, **_kwargs: pytest.fail("unexpected provision"),
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=lambda *_args, **_kwargs: pytest.fail("unexpected launch"),
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )
    real_rmtree = local.shutil.rmtree
    failed_once = False

    def flaky_rmtree(path, *args, **kwargs):
        nonlocal failed_once
        candidate = Path(path)
        if (
            not failed_once
            and candidate.parent == options.private_base
            and not kwargs.get("ignore_errors", False)
        ):
            failed_once = True
            raise OSError("synthetic finalization failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(local.shutil, "rmtree", flaky_rmtree)

    with pytest.raises(local.LocalDiagnosticError, match="finalization failed"):
        local.execute(options, dependencies=dependencies)

    assert failed_once is True
    assert not options.private_base.exists() or not any(options.private_base.iterdir())
    summaries = list(
        options.source_root.glob("qualification-artifacts/*/diagnostic-summary.json")
    )
    assert len(summaries) == 1
    assert json.loads(summaries[0].read_text())["private_scratch_remains"] is False


def test_failed_public_artifact_scan_quarantines_original_files(
    tmp_path, monkeypatch
):
    provider_secret = "provider-secret-that-must-be-quarantined"
    options = _options(
        tmp_path,
        preflight_only=True,
        env_text=(
            f"QA_GEMINI_API_KEY={provider_secret}\n"
            "FEEDLING_ADMIN_TOKEN=test-admin-token\n"
        ),
    )
    dependencies = local.DiagnosticDependencies(
        provision=lambda *_args, **_kwargs: pytest.fail("unexpected provision"),
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=lambda *_args, **_kwargs: pytest.fail("unexpected launch"),
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )
    original_scan = local._scan_public_artifacts
    injected = False

    def inject_then_scan(artifact_root, secret_values, profile_ids):
        nonlocal injected
        if not injected:
            injected = True
            matrix = artifact_root / "matrix.md"
            matrix.write_text(provider_secret, encoding="utf-8")
            matrix.chmod(0o600)
        original_scan(artifact_root, secret_values, profile_ids)

    monkeypatch.setattr(local, "_scan_public_artifacts", inject_then_scan)

    with pytest.raises(
        local.LocalDiagnosticError,
        match="artifacts could not be produced safely",
    ):
        local.execute(options, dependencies=dependencies)

    artifact_roots = list(
        options.source_root.glob("qualification-artifacts/local-*")
    )
    assert len(artifact_roots) == 1
    artifact_root = artifact_roots[0]
    summary_path = artifact_root / "diagnostic-summary.json"
    assert set(artifact_root.iterdir()) == {summary_path}
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "SECURITY_FAIL"
    assert summary["artifacts_quarantined"] is True
    assert provider_secret not in summary_path.read_text()


def test_unselected_key_leak_is_rejected_but_model_and_base_url_are_allowed(
    tmp_path,
):
    unselected_secret = "unselected-provider-key-in-artifact"
    admin_secret = "admin-token-in-artifact"
    expected_model = "gemini-2.5-flash"
    expected_base_url = "https://api.kongbeiqie.com"
    env_file = _write_private(
        tmp_path / "all-credentials.env",
        (
            "QA_GEMINI_API_KEY=selected-provider-key\n"
            f"QA_OPENAI_PROVIDER_API_KEY={unselected_secret}\n"
            f"QA_TEST_ADMIN_TOKEN={admin_secret}\n"
            f"QA_GEMINI_MODEL={expected_model}\n"
            f"QA_KONGBEIQIE_BASE_URL={expected_base_url}\n"
        ),
    )
    loaded = local.load_env_file(env_file)
    secret_values = local._loaded_credential_values(loaded)
    run_root = tmp_path / "run"
    run_root.mkdir()
    options = _options(run_root, preflight_only=True)
    paths = local.create_run_paths(options, "all-secret-scan")
    matrix = paths.artifact_root / "matrix.md"
    matrix.write_text(unselected_secret, encoding="utf-8")
    matrix.chmod(0o600)

    with pytest.raises(local.LocalDiagnosticError, match="secret material"):
        local._scan_public_artifacts(
            paths.artifact_root, secret_values, ("official-gemini",)
        )

    matrix.write_text(
        f"model={expected_model}\nbase_url={expected_base_url}\n",
        encoding="utf-8",
    )
    matrix.chmod(0o600)
    local._scan_public_artifacts(
        paths.artifact_root, secret_values, ("official-gemini",)
    )


@pytest.mark.parametrize(
    "relative",
    (
        "diagnostic-summary.json",
        "matrix.md",
        "latency.csv",
        "junit.xml",
        "profiles/official-gemini.json",
    ),
)
def test_public_artifact_scan_covers_every_operator_artifact(tmp_path, relative):
    options = _options(tmp_path, preflight_only=True)
    paths = local.create_run_paths(options, f"scan-{relative.replace('/', '-')}")
    secret = "provider-secret-for-artifact-scan"
    destination = paths.artifact_root / relative
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_bytes(base64.b64encode(secret.encode("utf-8")))
    destination.chmod(0o600)

    with pytest.raises(local.LocalDiagnosticError):
        local._scan_public_artifacts(
            paths.artifact_root, [secret], ("official-gemini",)
        )


def test_public_artifact_scan_reconstructs_split_json_secret(tmp_path):
    options = _options(tmp_path, preflight_only=True)
    paths = local.create_run_paths(options, "scan-split-json")
    secret = "provider-secret-split-across-fields"
    destination = paths.profile_artifacts / "official-gemini.json"
    local._write_private_json(
        destination,
        {"first": secret[:16], "second": secret[16:]},
    )

    with pytest.raises(local.LocalDiagnosticError):
        local._scan_public_artifacts(
            paths.artifact_root, [secret], ("official-gemini",)
        )


def test_public_artifact_scan_reconstructs_padded_split_json_secret(tmp_path):
    options = _options(tmp_path, preflight_only=True)
    paths = local.create_run_paths(options, "scan-padded-split-json")
    secret = "provider-secret-padded-across-fields"
    destination = paths.profile_artifacts / "official-gemini.json"
    local._write_private_json(
        destination,
        {
            "first": f"prefix::{secret[:17]}::suffix",
            "ignored": "fixed-safe-interleaved-value",
            "second": f"prefix::{secret[17:]}::suffix",
        },
    )

    with pytest.raises(local.LocalDiagnosticError):
        local._scan_public_artifacts(
            paths.artifact_root, [secret], ("official-gemini",)
        )


def test_public_artifact_scan_does_not_reorder_padded_fragments(tmp_path):
    options = _options(tmp_path, preflight_only=True)
    paths = local.create_run_paths(options, "scan-reordered-json")
    secret = "provider-secret-reordered-near-miss"
    split = len(secret) // 2
    destination = paths.profile_artifacts / "official-gemini.json"
    local._write_private_json(
        destination,
        {
            # The private JSON writer sorts keys, so these names preserve the
            # deliberately reversed fragment order on disk.
            "a_second": f"prefix::{secret[split:]}::suffix",
            "z_first": f"prefix::{secret[:split]}::suffix",
        },
    )

    local._scan_public_artifacts(
        paths.artifact_root, [secret], ("official-gemini",)
    )


def test_private_fallback_scrub_reconstructs_split_json_secret(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    secret = "provider-secret-split-across-private-fields"
    destination = root / "split.json"
    local._write_private_json(
        destination,
        {"first": secret[:20], "second": secret[20:]},
    )

    local._scrub_sensitive_material(root, [secret])

    assert not destination.exists()


def test_private_fallback_scrub_reconstructs_padded_split_json_secret(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    secret = "provider-secret-padded-across-private-fields"
    destination = root / "split.json"
    local._write_private_json(
        destination,
        {
            "first": f"prefix::{secret[:21]}::suffix",
            "second": f"prefix::{secret[21:]}::suffix",
        },
    )

    local._scrub_sensitive_material(root, [secret])

    assert not destination.exists()


@pytest.mark.parametrize(
    "relative",
    (
        ".venv/bin/python",
        "lib/site-packages/pkg/module.py",
        "src/__pycache__/module.cpython-311.pyc",
        "worker.pyc",
        "worker.PYO",
    ),
)
def test_debug_quarantine_classifies_runtime_and_bytecode_noise(relative):
    assert local._is_debug_quarantine_noise(Path(relative)) is True


@pytest.mark.parametrize(
    "relative", ("events.jsonl", "stderr.log", "src/worker.py", "session/rollout.jsonl")
)
def test_debug_quarantine_keeps_actionable_evidence(relative):
    assert local._is_debug_quarantine_noise(Path(relative)) is False


def test_prepare_environment_validates_only_selected_key_names_and_adds_defaults():
    env, names = local.prepare_environment(
        {
            "QA_GEMINI_API_KEY": "gemini-sensitive-value",
            "QA_DEEPSEEK_API_KEY": "unused-deepseek-secret",
            "QA_DEEPSEEK_MODEL": "unused-model",
            "QA_CODEX_MODEL": "unused-codex-model",
        },
        ("official-gemini",),
        "local-unit",
    )

    assert names == ("QA_GEMINI_API_KEY",)
    assert env["QA_GEMINI_MODEL"] == "gemini-2.5-flash"
    assert env["QA_FEEDLING_BASE_URL"] == local.LOCKED_BASE_URL
    assert "QA_TEST_ADMIN_TOKEN" not in env
    assert "QA_DEEPSEEK_API_KEY" not in env
    assert "QA_DEEPSEEK_MODEL" not in env
    assert "QA_CODEX_MODEL" not in env


def test_loaded_credentials_reject_short_unscannable_values():
    with pytest.raises(local.LocalDiagnosticError, match="QA_DEEPSEEK_API_KEY"):
        local._loaded_credential_values({"QA_DEEPSEEK_API_KEY": "abc1234"})


def test_local_artifact_scan_rejects_fixture_privacy_canary_variants(tmp_path):
    options = _options(tmp_path, preflight_only=True)
    values = local._fixture_privacy_values(options.source_root)
    canary = values[0]

    assert local._public_data_is_unsafe(canary.encode(), values) is True
    assert (
        local._public_data_is_unsafe(
            base64.b64encode(canary.encode("utf-8")), values
        )
        is True
    )
    assert (
        local._public_data_is_unsafe(
            json.dumps([canary[:10], canary[10:]]).encode("utf-8"), values
        )
        is True
    )


def test_cot_projection_preserves_fallback_as_not_run():
    projected = local._cot_delivery_projection(
        {
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "deterministic_fallback",
                    "fallback_reason": "COT_RECEIPT_MISSING",
                }
            ]
        },
        ("official-gemini",),
    )

    assert projected["official-gemini"] == {
        "status": "NOT_RUN",
        "failure_code": "COT_RECEIPT_MISSING",
        "receipt_status": None,
        "receipt_failure_code": None,
        "delivery_qualified": False,
        "final_answer_correct": None,
        "reasoning_event_count": None,
        "metadata_present": None,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
        "user_visible_disclosure_present": None,
        "receipt_sha256": None,
    }
    assert local._cot_delivery_passes(projected, ("official-gemini",)) is False


def test_cot_projection_preserves_trusted_observation_when_agent_falls_back():
    projected = local._cot_delivery_projection(
        {
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "deterministic_fallback",
                    "fallback_reason": "WORKER_RESULT_INVALID",
                    "cot_receipt_sha256": "a" * 64,
                    "cot_delivery_status": "PASS",
                    "cot_failure_code": "NONE",
                    "cot_delivery_qualified": True,
                    "cot_final_answer_correct": True,
                    "cot_reasoning_event_count": 1,
                    "cot_metadata_present": True,
                    "cot_token_metadata_status": "PRESENT",
                    "cot_reasoning_token_count": 192,
                    "cot_user_visible_disclosure_present": True,
                }
            ]
        },
        ("official-gemini",),
    )

    row = projected["official-gemini"]
    assert row["status"] == "NOT_RUN"
    assert row["failure_code"] == "WORKER_RESULT_INVALID"
    assert row["receipt_status"] == "PASS"
    assert row["reasoning_token_count"] == 192
    assert local._cot_delivery_passes(projected, ("official-gemini",)) is False


def test_cot_projection_keeps_agent_result_separate_from_missing_receipt():
    projected = local._cot_delivery_projection(
        {
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "codex_worker",
                    "fallback_reason": None,
                    "cot_evidence_failure": "COT_RECEIPT_MISSING",
                }
            ]
        },
        ("official-gemini",),
    )

    assert projected["official-gemini"]["status"] == "NOT_RUN"
    assert projected["official-gemini"]["failure_code"] == "COT_RECEIPT_MISSING"
    assert local._cot_delivery_passes(projected, ("official-gemini",)) is False


def test_cot_projection_marks_invalid_receipt_as_evidence_failure():
    projected = local._cot_delivery_projection(
        {
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "codex_worker",
                    "fallback_reason": None,
                    "cot_evidence_failure": "COT_RECEIPT_INVALID",
                }
            ]
        },
        ("official-gemini",),
    )

    evidence = projected["official-gemini"]
    assert evidence["status"] == "FAIL"
    assert evidence["failure_code"] == "COT_RECEIPT_INVALID"
    assert evidence["receipt_sha256"] is None
    assert local._cot_delivery_passes(projected, ("official-gemini",)) is False


def test_cot_projection_binding_failure_keeps_trusted_receipt_facts():
    projected = local._cot_delivery_projection(
        {
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "codex_worker",
                    "fallback_reason": None,
                    "cot_evidence_failure": "COT_RESULT_BINDING_MISMATCH",
                    "cot_receipt_sha256": "a" * 64,
                    "cot_delivery_status": "PASS",
                    "cot_failure_code": "NONE",
                    "cot_delivery_qualified": True,
                    "cot_final_answer_correct": True,
                    "cot_reasoning_event_count": 1,
                    "cot_metadata_present": True,
                    "cot_token_metadata_status": "UNVERIFIED",
                    "cot_reasoning_token_count": None,
                    "cot_user_visible_disclosure_present": True,
                }
            ]
        },
        ("official-gemini",),
    )

    assert projected["official-gemini"] == {
        "status": "FAIL",
        "failure_code": "COT_RESULT_BINDING_MISMATCH",
        "receipt_status": "PASS",
        "receipt_failure_code": "NONE",
        "delivery_qualified": True,
        "final_answer_correct": True,
        "reasoning_event_count": 1,
        "metadata_present": True,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
        "user_visible_disclosure_present": True,
        "receipt_sha256": "a" * 64,
    }
    assert local._cot_delivery_passes(projected, ("official-gemini",)) is False


def test_cot_projection_fails_gate_when_receipt_lacks_reasoning_tokens():
    projected = local._cot_delivery_projection(
        {
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "codex_worker",
                    "fallback_reason": None,
                    "cot_evidence_failure": None,
                    "cot_receipt_sha256": "a" * 64,
                    "cot_delivery_status": "PASS",
                    "cot_failure_code": "NONE",
                    "cot_delivery_qualified": True,
                    "cot_final_answer_correct": True,
                    "cot_reasoning_event_count": 1,
                    "cot_metadata_present": True,
                    "cot_token_metadata_status": "UNVERIFIED",
                    "cot_reasoning_token_count": None,
                    "cot_user_visible_disclosure_present": True,
                }
            ]
        },
        ("official-gemini",),
    )

    row = projected["official-gemini"]
    assert row["status"] == "FAIL"
    assert row["failure_code"] == "REASONING_TOKENS_MISSING"
    assert row["receipt_status"] == "PASS"
    assert row["receipt_failure_code"] == "NONE"
    assert local._cot_delivery_passes(projected, ("official-gemini",)) is False


def test_cot_projection_rejects_unknown_evidence_failure():
    with pytest.raises(local.LocalDiagnosticError, match="failure code"):
        local._cot_delivery_projection(
            {
                "workers": [
                    {
                        "profile_id": "official-gemini",
                        "result_source": "codex_worker",
                        "fallback_reason": None,
                        "cot_evidence_failure": "UNTRUSTED_UNKNOWN_CODE",
                    }
                ]
            },
            ("official-gemini",),
        )


@pytest.mark.parametrize(
    ("override", "expected"),
    (
        ({}, True),
        ({"status": "FAIL"}, False),
        ({"failure_code": "FINAL_ANSWER_WRONG"}, False),
        ({"delivery_qualified": False}, False),
        ({"final_answer_correct": False}, False),
        ({"reasoning_event_count": 0}, False),
        ({"metadata_present": False}, False),
        ({"token_metadata_status": "UNVERIFIED"}, False),
        ({"reasoning_token_count": None}, False),
        ({"reasoning_token_count": 0}, False),
        ({"user_visible_disclosure_present": False}, False),
    ),
)
def test_local_pass_requires_trusted_cot_delivery(override, expected):
    evidence = {
        "status": "PASS",
        "failure_code": "NONE",
        "receipt_status": "PASS",
        "receipt_failure_code": "NONE",
        "delivery_qualified": True,
        "final_answer_correct": True,
        "reasoning_event_count": 1,
        "metadata_present": True,
        "token_metadata_status": "PRESENT",
        "reasoning_token_count": 192,
        "user_visible_disclosure_present": True,
        "receipt_sha256": "a" * 64,
    }
    evidence.update(override)

    assert (
        local._cot_delivery_passes({"official-gemini": evidence}, ("official-gemini",))
        is expected
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("attempted", 0),
        ("cleaned", 0),
        ("failed_profile_ids", ["official-gemini"]),
        ("manifest_deleted", False),
        ("manifest_missing", True),
    ),
)
def test_parent_cleanup_projection_rejects_inexact_receipt(field, value):
    receipt = {
        "attempted": 1,
        "cleaned": 1,
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_missing": False,
    }
    receipt[field] = value

    projection = local._parent_cleanup_projection(
        receipt,
        ("official-gemini",),
        required=True,
        manifest_profiles_validated=True,
    )

    assert projection["status"] == "FAIL"


def test_parent_finalization_rejects_arbitrary_nonpass_or_agent_side_cleanup():
    profile = _parent_cleanup_deferred_profile("official-gemini")
    parent_cleanup = local._parent_cleanup_projection(
        {
            "attempted": 1,
            "cleaned": 1,
            "failed_profile_ids": [],
            "manifest_deleted": True,
            "manifest_missing": False,
        },
        ("official-gemini",),
        required=True,
        manifest_profiles_validated=True,
    )
    assert local._diagnostic_profile_projection(
        {"official-gemini": profile}, ("official-gemini",), parent_cleanup
    ) == {"official-gemini": "PASS"}

    profile["scenarios"][-1]["failure"]["failure_code"] = "CLEANUP_FAILED"
    assert local._diagnostic_profile_projection(
        {"official-gemini": profile}, ("official-gemini",), parent_cleanup
    ) == {"official-gemini": "FAIL"}

    profile = _parent_cleanup_deferred_profile("official-gemini")
    profile["cleanup"]["attempted"] = True
    assert local._diagnostic_profile_projection(
        {"official-gemini": profile}, ("official-gemini",), parent_cleanup
    ) == {"official-gemini": "FAIL"}


def test_local_auth_is_validated_and_copied_privately(tmp_path):
    source = _write_private(
        tmp_path / "source-auth.json", json.dumps(_auth_document()) + "\n"
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)

    local.install_local_codex_auth(source, codex_home)

    installed = codex_home / "auth.json"
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert json.loads(installed.read_text())["auth_mode"] == "chatgpt"


def test_headless_preflight_uses_isolated_oauth_model_and_forbids_tools(
    tmp_path, monkeypatch
):
    options = local._resolve_runtime_options(_options(tmp_path, preflight_only=True))
    paths = local.create_run_paths(options, "local-preflight-unit")
    local._build_codex_config(options, paths)
    observed: dict = {"sandbox_commands": [], "sandbox_environments": []}
    real_run = local.subprocess.run

    def run(command, **kwargs):
        if command[1] == "-c":
            return real_run(command, **kwargs)
        if command[1] == "sandbox":
            observed["sandbox_commands"].append(tuple(command))
            observed["sandbox_environments"].append(dict(kwargs["env"]))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        observed["command"] = tuple(command)
        observed["environment"] = dict(kwargs["env"])
        observed["prompt"] = kwargs["input"]
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(
            json.dumps({"ok": True, "profile_id": "official-gemini"}) + "\n",
            encoding="utf-8",
        )
        events = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "30000000-0000-4000-8000-000000000001",
                }
            )
            + "\n"
            + json.dumps({"type": "turn.started"})
            + "\n"
            + json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message"},
                }
            )
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {}})
            + "\n"
        )
        kwargs["stdout"].write(events.encode("utf-8"))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(local.subprocess, "run", run)

    receipt = local._verify_codex_exec(options, paths)

    command = observed["command"]
    assert command[:4] == (
        "/usr/bin/true",
        "exec",
        "-p",
        "profile_official_gemini",
    )
    assert command[command.index("--disable") + 1] == "network_proxy"
    assert command[-1] == "-"
    assert "Do not call any tool" in observed["prompt"]
    assert observed["environment"]["CODEX_HOME"] == str(paths.codex_home)
    assert "QA_GEMINI_API_KEY" not in observed["environment"]
    assert "QA_FEEDLING_BASE_URL" not in observed["environment"]
    assert receipt["headless_exec_completed"] is True
    assert receipt["tool_calls_observed"] is False
    assert receipt["worker_runtime_valid"] is True
    assert receipt["worker_network_valid"] is True
    assert len(observed["sandbox_commands"]) == 2
    sandbox_command = observed["sandbox_commands"][0]
    assert sandbox_command[1:6] == (
        "sandbox",
        "-p",
        "profile_official_gemini",
        "-P",
        "feedling-e2e-official-gemini",
    )
    assert sandbox_command[-1].endswith("/qa/cot_delivery_probe.py")
    assert 'exec "$QA_PYTHON_BIN" -I -B "$2" --help' in sandbox_command[-4]
    runtime_environment = observed["sandbox_environments"][0]
    assert runtime_environment["QA_PYTHON_BIN"] == sandbox_command[-2]
    assert runtime_environment["QA_QUALIFICATION_MODE"] == "diagnostic"
    network_command = observed["sandbox_commands"][1]
    assert network_command[1:6] == (
        "sandbox",
        "-p",
        "profile_official_gemini",
        "-P",
        "feedling-e2e-official-gemini",
    )
    assert network_command[-1] == "https://test-api.feedling.app/healthz"
    assert "urllib.request.urlopen" in network_command[-2]


def test_benchmark_fake_ip_detection_is_narrow(monkeypatch):
    def rows(addresses):
        return [
            (local.socket.AF_INET, local.socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]

    monkeypatch.setattr(
        local.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: rows(["198.18.0.137", "198.19.255.254"]),
    )
    assert local._uses_benchmark_fake_ip("test-api.feedling.app") is True

    monkeypatch.setattr(
        local.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: rows(["198.18.0.137", "203.0.113.7"]),
    )
    assert local._uses_benchmark_fake_ip("test-api.feedling.app") is False

    monkeypatch.setattr(
        local.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: rows(["10.0.0.7"]),
    )
    assert local._uses_benchmark_fake_ip("test-api.feedling.app") is False


def test_preflight_creates_sanitized_non_release_artifact_and_calls_cleanup(
    tmp_path, monkeypatch
):
    options = _options(tmp_path, preflight_only=True)
    cleanup_calls: list[Path] = []
    trust_order: list[str] = []
    real_install_auth = local.install_local_codex_auth

    def unexpected(*_args, **_kwargs):
        raise AssertionError("network phase must not run during preflight")

    def cleanup(path, **_kwargs):
        cleanup_calls.append(path)
        return {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        }

    def install_auth(source, destination):
        trust_order.append("oauth_install")
        return real_install_auth(source, destination)

    monkeypatch.setattr(local, "install_local_codex_auth", install_auth)

    dependencies = local.DiagnosticDependencies(
        provision=unexpected,
        cleanup=cleanup,
        launch=unexpected,
        verify_deployment=lambda *args, **kwargs: (
            trust_order.append("deployment")
            or _deployment_verification(*args, **kwargs)
        ),
        verify_codex_provenance=lambda _path: trust_order.append("provenance"),
        verify_codex_version=lambda _path: trust_order.append("version"),
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    summary, summary_path = local.execute(options, dependencies=dependencies)

    assert summary["status"] == "PREFLIGHT_PASS"
    assert trust_order == ["deployment", "provenance", "version", "oauth_install"]
    assert summary["release_qualified"] is False
    assert summary["requested_profile_ids"] == ["official-gemini"]
    expected_harness = _harness_provenance_receipt(options.source_root)
    assert summary["qualification_harness"] == {
        **expected_harness,
        "worker_snapshot_sha256": expected_harness["worker_source_sha256"],
    }
    assert summary["validated_credential_names"] == ["QA_GEMINI_API_KEY"]
    assert summary["missing_strict_evidence"]
    assert summary["codex_preflight"]["headless_exec_completed"] is True
    assert cleanup_calls
    assert stat.S_IMODE(summary_path.stat().st_mode) == 0o600
    artifacts = summary_path.parent
    assert {path.name for path in artifacts.iterdir()} == {
        "diagnostic-summary.json",
        "matrix.md",
        "latency.csv",
        "junit.xml",
        "profiles",
    }
    assert "NOT_RUN" in (artifacts / "matrix.md").read_text()
    junit = ElementTree.parse(artifacts / "junit.xml").getroot()
    assert junit.attrib["skipped"] == "13"
    assert junit.attrib["release_qualified"] == "false"
    assert not any(options.private_base.iterdir())
    assert "gemini-sensitive-value" not in summary_path.read_text()


def test_preflight_discovers_authoritative_deployment_sha_before_codex(tmp_path):
    discovered_sha = "d" * 40
    options = replace(
        _options(tmp_path, preflight_only=True),
        candidate_sha="",
    )
    order: list[str] = []

    def verify_deployment(expected_sha, receipt_path, *, env, expected_runtime):
        order.append("deployment")
        assert expected_sha is None
        assert env == {
            "QA_FEEDLING_BASE_URL": "https://test-api.feedling.app",
            "QA_TEST_ADMIN_TOKEN": "test-admin-token",
        }
        receipt = {
            "schema_version": 1,
            "environment": "test",
            "base_url": "https://test-api.feedling.app",
            "expected_runtime": expected_runtime,
            "expected_deployment_sha": discovered_sha,
            "observed_backend_sha": discovered_sha,
            "observed_deployment_sha": discovered_sha,
            "observed_worker_sha": None,
            "live_worker_count": None,
            "runtime_evidence_source": (
                "per_profile_runtime_readback_and_live_scenarios"
                if expected_runtime == "hosted_resident"
                else "deployed_runtime_readback"
            ),
            "liveness_verified": True,
            "deployment_identity_verified": True,
            "verified_at": "2026-07-14T00:00:00+00:00",
        }
        local._write_private_json(receipt_path, receipt)
        receipt_path.chmod(0o400)
        return receipt

    dependencies = local.DiagnosticDependencies(
        provision=lambda *_args, **_kwargs: pytest.fail("unexpected provision"),
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=lambda *_args, **_kwargs: pytest.fail("unexpected launch"),
        verify_deployment=verify_deployment,
        verify_codex_provenance=lambda _path: order.append("codex"),
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    summary, _summary_path = local.execute(options, dependencies=dependencies)

    assert order[:2] == ["deployment", "codex"]
    assert summary["candidate_sha"] == discovered_sha
    assert summary["deployment_identity"] == {
        "expected_deployment_sha": discovered_sha,
        "observed_backend_sha": discovered_sha,
        "observed_deployment_sha": discovered_sha,
        "observed_worker_sha": None,
        "identity_verified": True,
    }


def test_candidate_sha_mismatch_aborts_before_codex_or_provisioning(tmp_path):
    options = _options(tmp_path, preflight_only=False)
    downstream_called = False

    def unexpected(*_args, **_kwargs):
        nonlocal downstream_called
        downstream_called = True
        raise AssertionError("deployment mismatch must stop the run")

    def reject_mismatch(expected_sha, _receipt_path, *, env, expected_runtime):
        assert expected_sha == "a" * 40
        assert env["QA_TEST_ADMIN_TOKEN"] == "test-admin-token"
        assert expected_runtime == "deployed_current"
        raise local.deployment_verifier.DeploymentVerificationError(
            "deployed backend build does not match the candidate"
        )

    dependencies = local.DiagnosticDependencies(
        provision=unexpected,
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=unexpected,
        verify_deployment=reject_mismatch,
        verify_codex_provenance=unexpected,
        verify_codex_version=unexpected,
        verify_login=unexpected,
        verify_codex_exec=unexpected,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    with pytest.raises(local.LocalDiagnosticError, match="does not match"):
        local.execute(options, dependencies=dependencies)

    assert downstream_called is False


def test_harness_provenance_failure_aborts_before_provisioning(tmp_path):
    options = _options(tmp_path, preflight_only=False)
    provision_called = False

    def provision(*_args, **_kwargs):
        nonlocal provision_called
        provision_called = True
        raise AssertionError("provisioning must not start")

    def fail_provenance(_source_root):
        raise local.harness_provenance.HarnessProvenanceError(
            "qualification harness Git HEAD is unavailable"
        )

    dependencies = local.DiagnosticDependencies(
        provision=provision,
        cleanup=lambda *_args, **_kwargs: {
            "attempted": 0,
            "cleaned": 0,
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": True,
        },
        launch=lambda *_args, **_kwargs: pytest.fail("unexpected launch"),
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: pytest.fail(
            "unexpected Codex provenance check"
        ),
        verify_codex_version=lambda _path: pytest.fail("unexpected Codex check"),
        verify_login=lambda *_args: pytest.fail("unexpected login check"),
        verify_codex_exec=lambda *_args: pytest.fail("unexpected exec check"),
        collect_harness_provenance=fail_provenance,
    )

    with pytest.raises(
        local.LocalDiagnosticError,
        match="qualification harness Git HEAD is unavailable",
    ):
        local.execute(options, dependencies=dependencies)

    assert provision_called is False


def test_selected_profile_run_provisions_launches_copies_result_and_cleans(tmp_path):
    options = local._resolve_runtime_options(_options(tmp_path, preflight_only=False))
    calls: list[str] = []

    def provision(
        _coverage,
        manifest_path,
        *,
        env,
        diagnostic,
        profile_ids,
        runtime_requirement,
    ):
        calls.append("provision")
        assert "source-snapshot" in _coverage.parts
        assert options.source_root not in _coverage.parents
        assert _coverage.read_text() == "{}\n"
        assert diagnostic is True
        assert profile_ids == ("official-gemini",)
        assert runtime_requirement == "deployed_current"
        assert env["QA_GEMINI_API_KEY"] == "gemini-sensitive-value"
        assert "QA_TEST_ADMIN_TOKEN" not in env
        assert "FEEDLING_ADMIN_TOKEN" not in env
        manifest = {
            "schema_version": 1,
            "qualification_mode": "diagnostic",
            "selected_profile_ids": ["official-gemini"],
            "runtime_mode": "deployed_current",
            "runtime_requirement": "deployed_current",
            "profiles": [
                {
                    "profile_id": "official-gemini",
                    "runtime_mode": "hosted_resident",
                    "runtime_version": 2,
                    "runtime_mode_readback_verified": True,
                }
            ],
        }
        local._write_private_json(manifest_path, manifest)
        return manifest

    def launch(**kwargs):
        calls.append("launch")
        assert kwargs["diagnostic"] is True
        assert kwargs["profile_ids"] == ("official-gemini",)
        assert kwargs["expected_runtime"] == "deployed_current"
        isolated = json.loads(
            (kwargs["profile_manifest_dir"] / "official-gemini.json").read_text()
        )
        assert isolated["selected_profile_ids"] == ["official-gemini"]
        assert len(isolated["profiles"]) == 1
        profile = _parent_cleanup_deferred_profile("official-gemini")
        profile.update(
            {
                "observed_runtime": "hosted_resident",
                "observed_runtime_version": 2,
                "latency": {
                    "sample_count": 3,
                    "ack_p50_ms": 12,
                    "reply_p50_ms": 345,
                    "reply_p95_ms": 678,
                    "stage_p50_ms": {
                        "routing": 1,
                        "queue": 2,
                        "provider": 300,
                        "persistence": 20,
                        "delivery": 22,
                    },
                    "missing_stages": [],
                },
                "reasoning": {
                    "reasoning_event_count": 1,
                    "metadata_present": True,
                    "token_metadata_present": True,
                    "reasoning_token_count": 192,
                    "user_visible_disclosure_present": True,
                },
            }
        )
        local._write_private_json(
            kwargs["aggregation_input_root"] / "official-gemini.json",
            profile,
        )
        return {
            "launch_attempts": 1,
            "max_observed_profile_concurrency": 1,
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "codex_worker",
                    "fallback_reason": None,
                    "completed_command_execution_count": 13,
                    "completed_scenario_command_ids": list(
                        workers.AGENT_LIVE_SCENARIO_IDS
                    ),
                    "completed_scenario_command_counts": (
                        orchestration.MIN_SCENARIO_COMMAND_COUNTS
                    ),
                    "p0_06_command_phases": list(
                        orchestration.P0_06_COMMAND_PHASES
                    ),
                    "cot_receipt_sha256": "a" * 64,
                    "cot_delivery_status": "PASS",
                    "cot_failure_code": "NONE",
                    "cot_delivery_qualified": True,
                    "cot_final_answer_correct": True,
                    "cot_reasoning_event_count": 1,
                    "cot_metadata_present": True,
                    "cot_token_metadata_status": "PRESENT",
                    "cot_reasoning_token_count": 192,
                    "cot_user_visible_disclosure_present": True,
                }
            ],
        }

    def cleanup(path, **_kwargs):
        calls.append("cleanup")
        path.unlink()
        return {
            "attempted": 1,
            "cleaned": 1,
            "failed_profile_ids": [],
            "manifest_deleted": True,
            "manifest_missing": False,
        }

    dependencies = local.DiagnosticDependencies(
        provision=provision,
        cleanup=cleanup,
        launch=launch,
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    summary, summary_path = local.execute(options, dependencies=dependencies)

    assert calls == ["provision", "launch", "cleanup"]
    assert summary["status"] == "DIAGNOSTIC_PASS"
    assert summary["release_qualified"] is False
    assert summary["deployment_identity"] == {
        "expected_deployment_sha": "a" * 40,
        "observed_backend_sha": "a" * 40,
        "observed_deployment_sha": "a" * 40,
        "observed_worker_sha": None,
        "identity_verified": True,
    }
    assert summary["profile_statuses"] == {
        "official-gemini": "BLOCKED_EVIDENCE"
    }
    assert summary["diagnostic_profile_statuses"] == {"official-gemini": "PASS"}
    assert summary["parent_cleanup_verification"] == {
        "owner": "deterministic_parent",
        "status": "PASS",
        "expected_profile_ids": ["official-gemini"],
        "manifest_profiles_validated": True,
        "attempted": 1,
        "cleaned": 1,
        "failed_profile_ids": [],
        "manifest_deleted": True,
        "manifest_missing": False,
    }
    assert summary["orchestration"]["completed_command_execution_counts"] == {
        "official-gemini": 13
    }
    assert summary["orchestration"]["completed_scenario_command_ids"] == {
        "official-gemini": list(workers.AGENT_LIVE_SCENARIO_IDS)
    }
    assert summary["orchestration"]["completed_scenario_command_counts"] == {
        "official-gemini": orchestration.MIN_SCENARIO_COMMAND_COUNTS
    }
    assert summary["orchestration"]["p0_06_command_phases"] == {
        "official-gemini": list(orchestration.P0_06_COMMAND_PHASES)
    }
    assert summary["cot_delivery"]["official-gemini"] == {
        "status": "PASS",
        "failure_code": "NONE",
        "receipt_status": "PASS",
        "receipt_failure_code": "NONE",
        "delivery_qualified": True,
        "final_answer_correct": True,
        "reasoning_event_count": 1,
        "metadata_present": True,
        "token_metadata_status": "PRESENT",
        "reasoning_token_count": 192,
        "user_visible_disclosure_present": True,
        "receipt_sha256": "a" * 64,
    }
    profile = summary_path.parent / "profiles" / "official-gemini.json"
    stored_profile = json.loads(profile.read_text())
    assert stored_profile["status"] == "BLOCKED_EVIDENCE"
    assert stored_profile["scenarios"][-1]["status"] == "BLOCKED_EVIDENCE"
    assert stored_profile["scenarios"][-1]["assertions"]["cleanup_confirmed"] is False
    assert "official-gemini" in (summary_path.parent / "matrix.md").read_text()
    assert "release_qualified" in (summary_path.parent / "latency.csv").read_text()
    latency = (summary_path.parent / "latency.csv").read_text()
    assert (
        "official-gemini,BLOCKED_EVIDENCE,3,12,345,678,1,2,300,20,22,,false"
        in latency
    )
    matrix = (summary_path.parent / "matrix.md").read_text()
    assert "Deterministic parent cleanup: `PASS`" in matrix
    assert "Parent-finalized profiles: `1/1`" in matrix
    assert (
        "PASS | NONE | PASS | NONE | OBSERVED | PRESENT | PRESENT | PRESENT"
        in matrix
    )
    junit = ElementTree.parse(summary_path.parent / "junit.xml").getroot()
    assert junit.attrib == {
        "name": "feedling-local-api-key-diagnostic",
        "tests": "13",
        "failures": "0",
        "errors": "0",
        "skipped": "0",
        "release_qualified": "false",
    }
    for artifact in summary_path.parent.rglob("*"):
        if artifact.is_file():
            assert "gemini-sensitive-value" not in artifact.read_text()
    assert not any(options.private_base.iterdir())


def test_nonpassing_worker_retains_only_credential_scrubbed_debug_evidence(tmp_path):
    options = _options(tmp_path, preflight_only=False)
    synthetic_api_key = "synthetic-user-api-key-for-debug-test"
    content_key = "synthetic-content-key-for-debug-test"

    def provision(
        _coverage,
        manifest_path,
        *,
        env,
        diagnostic,
        profile_ids,
        runtime_requirement,
    ):
        assert diagnostic is True
        assert profile_ids == ("official-gemini",)
        assert runtime_requirement == "deployed_current"
        manifest = {
            "schema_version": 1,
            "qualification_mode": "diagnostic",
            "selected_profile_ids": ["official-gemini"],
            "runtime_mode": "deployed_current",
            "runtime_requirement": "deployed_current",
            "profiles": [
                {
                    "profile_id": "official-gemini",
                    "configured_model": "gemini-2.5-flash",
                    "runtime_mode": "hosted_resident",
                    "runtime_version": 2,
                    "runtime_mode_readback_verified": True,
                    "trace_enabled": True,
                    "user_id": "synthetic-debug-user",
                    "api_key": synthetic_api_key,
                    "secret_key_b64": content_key,
                }
            ],
        }
        local._write_private_json(manifest_path, manifest)
        return manifest

    def launch(**kwargs):
        output = kwargs["worker_output_root"] / "official-gemini"
        output.mkdir(mode=0o700)
        local._write_private_bytes(
            output / "events.jsonl",
            b'{"type":"turn.completed","usage":{}}\n',
        )
        local._write_private_bytes(
            output / "oauth.txt",
            b"access-token-for-local-diagnostic-account",
        )
        local._write_private_bytes(
            output / "provider.txt",
            b"gemini-sensitive-value",
        )
        local._write_private_bytes(output / "useful-debug.log", b"useful evidence\n")
        for relative in (
            ".venv/lib/site-packages/pkg/module.py",
            "site-packages/pkg/metadata.txt",
            "__pycache__/module.cpython-311.pyc",
            "compiled.pyc",
            "compiled.pyo",
        ):
            noise = output / relative
            noise.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            local._write_private_bytes(noise, b"runtime noise\n")
        local._write_private_bytes(output / "user-key.txt", synthetic_api_key.encode())
        local._write_private_json(
            output / "split-provider-key.json",
            {
                "first": "gemini-sensitive-",
                "second": "value",
            },
        )
        local._write_private_json(
            kwargs["aggregation_input_root"] / "official-gemini.json",
            diagnostic_results.agent_error_profile(
                {
                    "profile_id": "official-gemini",
                    "configured_model": "gemini-2.5-flash",
                    "runtime_mode": "hosted_resident",
                    "trace_enabled": True,
                    "user_id": "synthetic-debug-user",
                },
                profile_id="official-gemini",
                expected_runtime="hosted_resident",
            ),
        )
        return {
            "launch_attempts": 1,
            "max_observed_profile_concurrency": 1,
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "deterministic_fallback",
                    "fallback_reason": "COT_RECEIPT_MISSING",
                }
            ],
        }

    def cleanup(path, **_kwargs):
        path.unlink()
        return {
            "attempted": 1,
            "cleaned": 1,
            "failed_profile_ids": [],
            "manifest_deleted": True,
            "manifest_missing": False,
        }

    dependencies = local.DiagnosticDependencies(
        provision=provision,
        cleanup=cleanup,
        launch=launch,
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    summary, _summary_path = local.execute(options, dependencies=dependencies)

    assert summary["status"] == "DIAGNOSTIC_FAIL"
    assert summary["private_debug_retained"] is True
    assert summary["private_debug_run_id"] == summary["run_id"]
    assert summary["private_cleanup_retry_retained"] is False
    assert not any(options.private_base.iterdir())

    debug_root = options.private_base.parent / "feedling-e2e-debug" / summary["run_id"]
    debug_manifest = json.loads((debug_root / "debug-manifest.json").read_text())
    assert debug_manifest["oauth_material_retained"] is False
    assert debug_manifest["credential_material_retained"] is False
    assert debug_manifest["provisioning_manifest_retained"] is False
    assert (debug_root / "worker-outputs" / "official-gemini" / "events.jsonl").exists()
    assert (
        debug_root / "worker-outputs" / "official-gemini" / "useful-debug.log"
    ).exists()
    assert not any(
        path.suffix.lower() in {".pyc", ".pyo"}
        or any(
            part in {".venv", "site-packages", "__pycache__"}
            for part in path.relative_to(debug_root).parts
        )
        for path in debug_root.rglob("*")
    )
    assert not (
        debug_root / "worker-outputs" / "official-gemini" / "oauth.txt"
    ).exists()
    assert not (
        debug_root / "worker-outputs" / "official-gemini" / "provider.txt"
    ).exists()
    assert not (
        debug_root / "worker-outputs" / "official-gemini" / "user-key.txt"
    ).exists()
    assert not (
        debug_root / "worker-outputs" / "official-gemini" / "split-provider-key.json"
    ).exists()
    assert not list(debug_root.rglob("provisioning-manifest.json"))
    for path in debug_root.rglob("*"):
        expected_mode = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
        if path.is_file():
            data = path.read_bytes()
            assert b"access-token-for-local-diagnostic-account" not in data
            assert b"gemini-sensitive-value" not in data
            assert synthetic_api_key.encode() not in data
            assert content_key.encode() not in data


def test_cleanup_failure_retains_retry_manifest_but_removes_oauth_material(tmp_path):
    options = _options(tmp_path, preflight_only=False)
    synthetic_api_key = "synthetic-user-api-key-for-cleanup-retry"

    def provision(_coverage, manifest_path, **_kwargs):
        manifest = {
            "schema_version": 1,
            "qualification_mode": "diagnostic",
            "selected_profile_ids": ["official-gemini"],
            "runtime_mode": "hosted_resident",
            "runtime_version": 2,
            "profiles": [
                {
                    "profile_id": "official-gemini",
                    "configured_model": "gemini-2.5-flash",
                    "runtime_mode": "hosted_resident",
                    "runtime_version": 2,
                    "trace_enabled": True,
                    "user_id": "synthetic-cleanup-retry-user",
                    "api_key": synthetic_api_key,
                    "secret_key_b64": "synthetic-content-key-for-cleanup-retry",
                }
            ],
        }
        local._write_private_json(manifest_path, manifest)
        return manifest

    def launch(**kwargs):
        local._write_private_json(
            kwargs["aggregation_input_root"] / "official-gemini.json",
            diagnostic_results.agent_error_profile(
                {
                    "profile_id": "official-gemini",
                    "configured_model": "gemini-2.5-flash",
                    "runtime_mode": "hosted_resident",
                    "trace_enabled": True,
                    "user_id": "synthetic-cleanup-retry-user",
                },
                profile_id="official-gemini",
                expected_runtime="hosted_resident",
            ),
        )
        return {
            "launch_attempts": 1,
            "max_observed_profile_concurrency": 1,
            "workers": [
                {
                    "profile_id": "official-gemini",
                    "result_source": "deterministic_fallback",
                    "fallback_reason": "COT_RECEIPT_MISSING",
                }
            ],
        }

    def cleanup(_path, **_kwargs):
        return {
            "attempted": 1,
            "cleaned": 0,
            "failed_profile_ids": ["official-gemini"],
            "manifest_deleted": False,
            "manifest_missing": False,
        }

    dependencies = local.DiagnosticDependencies(
        provision=provision,
        cleanup=cleanup,
        launch=launch,
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    with pytest.raises(local.LocalDiagnosticError):
        local.execute(options, dependencies=dependencies)

    summaries = list(
        options.source_root.glob("qualification-artifacts/*/diagnostic-summary.json")
    )
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["status"] == "CLEANUP_FAIL"
    assert summary["private_debug_retained"] is False
    assert summary["private_cleanup_retry_retained"] is True

    private_root = options.private_base / summary["run_id"]
    manifest = private_root / "provisioning-manifest.json"
    assert set(private_root.iterdir()) == {manifest}
    assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert manifest.exists()
    assert synthetic_api_key in manifest.read_text()
    assert not (private_root / "codex-home" / "auth.json").exists()
    for path in private_root.rglob("*"):
        if path.is_file():
            assert b"access-token-for-local-diagnostic-account" not in path.read_bytes()


def test_worker_launcher_diagnostic_mode_runs_only_selected_profile(tmp_path):
    options = local._resolve_runtime_options(_options(tmp_path, preflight_only=False))
    paths = local.create_run_paths(options, "local-worker-unit")
    local._build_codex_config(options, paths)
    local._write_private_json(
        paths.profile_manifests / "official-gemini.json",
        {
            "schema_version": 1,
            "profiles": [{"profile_id": "official-gemini"}],
        },
    )
    captured: list[workers.WorkerSpec] = []

    def run(spec: workers.WorkerSpec, timeout: int) -> int:
        captured.append(spec)
        assert timeout == 600
        schema = json.loads(spec.schema_path.read_text())
        result = _schema_instance(schema, schema["$defs"])
        spec.result_path.write_text(json.dumps(result) + "\n")
        spec.events_path.write_text(
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "30000000-0000-4000-8000-000000000001",
                    "session_id": "40000000-0000-4000-8000-000000000001",
                }
            )
            + "\n"
            + json.dumps({"type": "turn.started"})
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {}})
            + "\n"
        )
        return 0

    receipt = workers.launch(
        codex_bin=options.codex_bin,
        codex_home=paths.codex_home,
        source_root=paths.worker_source_root,
        artifact_root=paths.artifact_root,
        profile_manifest_dir=paths.profile_manifests,
        worker_root=paths.worker_root,
        worker_output_root=paths.worker_outputs,
        aggregation_input_root=paths.aggregation_inputs,
        authoring_schema_path=(
            Path(local.__file__).parent / "schemas" / "codex-run-result.schema.json"
        ),
        receipt_path=paths.orchestration_receipt,
        run_id="local-worker-unit",
        base_url=local.LOCKED_BASE_URL,
        expected_sha="a" * 40,
        timeout_seconds=600,
        process_runner=run,
        diagnostic=True,
        profile_ids=("official-gemini",),
        expected_runtime="hosted_resident",
        worker_python=options.worker_python,
    )

    assert len(captured) == 1
    assert captured[0].profile_id == "official-gemini"
    assert captured[0].environment["QA_SOURCE_ROOT"] == str(
        paths.worker_source_root.resolve()
    )
    assert captured[0].environment["QA_EXPECTED_RUNTIME"] == "hosted_resident"
    assert captured[0].environment["QA_QUALIFICATION_MODE"] == "diagnostic"
    assert receipt["qualification_mode"] == "diagnostic"
    assert receipt["release_qualified"] is False
    assert receipt["requested_profile_ids"] == ["official-gemini"]
    assert receipt["launch_attempts"] == 1
    assert {path.name for path in paths.worker_outputs.iterdir()} == {"official-gemini"}


def test_cleanup_runs_after_provisioning_failure_and_failure_stays_sanitized(tmp_path):
    options = _options(tmp_path, preflight_only=False)
    cleaned: list[bool] = []

    def provision(_coverage, manifest_path, **_kwargs):
        local._write_private_json(
            manifest_path,
            {
                "schema_version": 1,
                "profiles": [{"profile_id": "official-gemini"}],
            },
        )
        raise local.provision_profiles.ProvisionError(
            "account registration failed for profile: official-gemini"
        )

    def cleanup(path, **_kwargs):
        cleaned.append(True)
        path.unlink()
        return {
            "attempted": 1,
            "cleaned": 1,
            "failed_profile_ids": [],
            "manifest_deleted": True,
            "manifest_missing": False,
        }

    dependencies = local.DiagnosticDependencies(
        provision=provision,
        cleanup=cleanup,
        launch=lambda **_kwargs: {},
        verify_deployment=_deployment_verification,
        verify_codex_provenance=lambda _path: None,
        verify_codex_version=lambda _path: None,
        verify_login=lambda *_args: None,
        verify_codex_exec=_codex_preflight_receipt,
        collect_harness_provenance=_harness_provenance_receipt,
    )

    with pytest.raises(local.LocalDiagnosticError) as exc:
        local.execute(options, dependencies=dependencies)

    assert cleaned == [True]
    assert "gemini-sensitive-value" not in str(exc.value)
    summaries = list(
        options.source_root.glob("qualification-artifacts/*/diagnostic-summary.json")
    )
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text())
    assert summary["status"] == "DIAGNOSTIC_ERROR"
    assert summary["release_qualified"] is False
    assert summary["cleanup"]["cleaned"] == 1
