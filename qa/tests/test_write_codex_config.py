from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from qa import write_codex_config as writer
from qa.orchestration_contract import PROFILE_AGENT_TYPES


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def _paths(tmp_path: Path) -> dict[str, Path | str | list[Path]]:
    source = tmp_path / "checkout"
    artifacts = source / "qualification-artifacts" / "run"
    artifacts.mkdir(parents=True)
    (source / "qa").mkdir()
    private = tmp_path / "private"
    _mkdir_private(private)
    codex_home = private / "codex-home"
    supervisor_home = private / "supervisor-home"
    supervisor_tmp = private / "supervisor-tmp"
    supervisor_work = private / "supervisor-work"
    manifests = private / "profile-manifests"
    worker_root = private / "workers"
    worker_outputs = private / "worker-outputs"
    aggregation_inputs = private / "aggregation-inputs"
    persona_judge_root = private / "persona-judge"
    persona_judge_scratch_root = private / "persona-judge-scratch"
    for directory in (
        codex_home,
        supervisor_home,
        supervisor_tmp,
        supervisor_work,
        manifests,
        worker_root,
        worker_outputs,
        aggregation_inputs,
        persona_judge_root,
        persona_judge_scratch_root,
    ):
        _mkdir_private(directory)
    for leaf in ("home", "tmp", "work"):
        _mkdir_private(persona_judge_root / leaf)
    for _, agent_type in PROFILE_AGENT_TYPES:
        agent_root = worker_root / agent_type
        _mkdir_private(agent_root)
        _mkdir_private(agent_root / "home")
        _mkdir_private(agent_root / "tmp")
        _mkdir_private(agent_root / "work")
    python_runtime = tmp_path / "trusted-python"
    python_bin = python_runtime / "bin"
    python_bin.mkdir(parents=True)
    worker_python = python_bin / "python3"
    worker_python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    worker_python.chmod(0o700)
    return {
        "output": codex_home / "config.toml",
        "source_root": source,
        "artifact_root": artifacts,
        "full_manifest": private / "full-provisioning-manifest.json",
        "profile_manifest_dir": manifests,
        "supervisor_home": supervisor_home,
        "supervisor_tmp": supervisor_tmp,
        "supervisor_work": supervisor_work,
        "worker_root": worker_root,
        "worker_output_root": worker_outputs,
        "aggregation_input_root": aggregation_inputs,
        "orchestration_receipt": private / "orchestration-receipt.json",
        "persona_judge_root": persona_judge_root,
        "persona_judge_scratch_root": persona_judge_scratch_root,
        "codex_model": "gpt-5.4",
        "allowed_host": "test-api.feedling.app",
        "worker_python": worker_python,
        "qualification_mode": "release",
        "runtime_read_roots": [python_runtime],
    }


def test_bundle_isolates_workers_aggregation_and_persona_judge(tmp_path):
    values = _paths(tmp_path)
    bundle = writer.build_config_bundle(**values)
    document = tomllib.loads(bundle.main)
    supervisor = document["permissions"][writer.SUPERVISOR_PERMISSION_PROFILE]
    filesystem = supervisor["filesystem"]

    assert document["default_permissions"] == writer.SUPERVISOR_PERMISSION_PROFILE
    assert document["model"] == "gpt-5.4"
    assert document["approval_policy"] == "never"
    assert document["features"]["multi_agent"] is False
    assert document["features"]["hooks"] is False
    assert supervisor["network"]["allow_local_binding"] is False
    assert "agents" not in document
    assert filesystem[":minimal"] == "read"
    assert filesystem[str(Path(values["source_root"]).resolve())] == "read"
    assert filesystem[str(Path(values["artifact_root"]).resolve())] == "deny"
    assert filesystem[str(Path(values["full_manifest"]).resolve())] == "deny"
    assert filesystem[str(Path(values["profile_manifest_dir"]).resolve())] == "deny"
    assert filesystem[str(Path(values["worker_output_root"]).resolve())] == "deny"
    assert filesystem[str(Path(values["aggregation_input_root"]).resolve())] == "read"
    assert filesystem[str(Path(values["orchestration_receipt"]).resolve())] == "read"
    assert document["shell_environment_policy"]["set"][
        "QA_AGGREGATION_INPUT_ROOT"
    ] == str(Path(values["aggregation_input_root"]).resolve())
    assert document["shell_environment_policy"]["set"][
        "QA_ORCHESTRATION_RECEIPT"
    ] == str(Path(values["orchestration_receipt"]).resolve())
    assert (
        "QA_PRIVATE_MANIFEST"
        not in document["shell_environment_policy"]["include_only"]
    )

    assert len(bundle.profiles) == len(PROFILE_AGENT_TYPES) + 1 == 9
    manifests = Path(values["profile_manifest_dir"])
    worker_root = Path(values["worker_root"])
    permissions = document["permissions"]
    for profile_id, agent_type in PROFILE_AGENT_TYPES:
        profile_path = Path(values["output"]).parent / f"{agent_type}.config.toml"
        profile = tomllib.loads(bundle.profiles[profile_path])
        permission_name = writer.worker_permission_profile(profile_id)
        policy = permissions[permission_name]["filesystem"]
        own_manifest = str((manifests / f"{profile_id}.json").resolve())
        memory_manifest = str(
            (manifests / f"{writer.MEMORY_CONTRACT_PROFILE_ID}.json").resolve()
        )
        assert profile["default_permissions"] == permission_name
        assert policy[memory_manifest] == "deny"
        assert profile["model"] == "gpt-5.4"
        assert "agents" not in profile
        assert "permissions" not in profile
        assert (
            profile["shell_environment_policy"]["set"]["QA_PRIVATE_MANIFEST"]
            == own_manifest
        )
        assert profile["shell_environment_policy"]["set"]["QA_PROFILE_ID"] == profile_id
        assert profile["shell_environment_policy"]["set"]["QA_AGENT_TYPE"] == agent_type
        assert profile["shell_environment_policy"]["set"]["QA_ARTIFACT_DIR"] == str(
            Path(values["artifact_root"]).resolve()
        )
        assert profile["shell_environment_policy"]["set"]["QA_PYTHON_BIN"] == str(
            Path(values["worker_python"]).resolve()
        )
        assert (
            profile["shell_environment_policy"]["set"]["QA_QUALIFICATION_MODE"]
            == "release"
        )
        assert "QA_PYTHON_BIN" in profile["shell_environment_policy"]["include_only"]
        assert (
            "QA_QUALIFICATION_MODE"
            in profile["shell_environment_policy"]["include_only"]
        )
        assert policy[own_manifest] == "read"
        for other_profile, other_agent in PROFILE_AGENT_TYPES:
            if other_profile != profile_id:
                assert (
                    policy[str((manifests / f"{other_profile}.json").resolve())]
                    == "deny"
                )
                assert policy[str((worker_root / other_agent).resolve())] == "deny"
        assert policy[str(Path(values["artifact_root"]).resolve())] == "deny"
        assert policy[str(Path(values["full_manifest"]).resolve())] == "deny"
        assert policy[str(Path(values["worker_output_root"]).resolve())] == "deny"
        assert policy[str(Path(values["aggregation_input_root"]).resolve())] == "deny"
        assert policy[str(Path(values["orchestration_receipt"]).resolve())] == "deny"
        for leaf in ("home", "tmp", "work"):
            assert policy[str((worker_root / agent_type / leaf).resolve())] == "write"

    judge_root = Path(values["persona_judge_root"])
    judge_path = (
        Path(values["output"]).parent
        / f"{writer.PERSONA_MEMORY_JUDGE_PROFILE_NAME}.config.toml"
    )
    judge_profile = tomllib.loads(bundle.profiles[judge_path])
    judge_permission = permissions[writer.PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE]
    judge_policy = judge_permission["filesystem"]

    assert (
        judge_profile["default_permissions"]
        == writer.PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE
    )
    assert judge_profile["model"] == "gpt-5.4"
    assert judge_profile["approval_policy"] == "never"
    assert set(judge_profile["shell_environment_policy"]["include_only"]) == {
        "HOME",
        "PATH",
        "LANG",
        "TMPDIR",
        "PYTHONDONTWRITEBYTECODE",
        "QA_RUN_ID",
        "QA_WORK_ROOT",
    }
    assert "QA_FEEDLING_BASE_URL" not in bundle.profiles[judge_path]
    assert "QA_TEST_ADMIN_TOKEN" not in bundle.profiles[judge_path]
    assert "QA_PRIVATE_MANIFEST" not in bundle.profiles[judge_path]
    assert judge_permission["network"]["enabled"] is False
    assert "domains" not in judge_permission["network"]
    for denied in (
        values["source_root"],
        values["artifact_root"],
        values["full_manifest"],
        values["profile_manifest_dir"],
        values["worker_root"],
        values["worker_output_root"],
        values["aggregation_input_root"],
        values["orchestration_receipt"],
        Path(values["output"]).parent,
        values["persona_judge_scratch_root"],
        *values["runtime_read_roots"],
    ):
        assert judge_policy[str(Path(denied).resolve())] == "deny"
    assert judge_policy[str(judge_root.resolve())] == "write"
    for leaf in ("home", "tmp", "work"):
        assert judge_policy[str((judge_root / leaf).resolve())] == "write"


def test_local_binding_is_explicit_and_applies_to_every_locked_profile(tmp_path):
    values = _paths(tmp_path)
    values["allow_local_binding"] = True

    document = tomllib.loads(writer.build_config_bundle(**values).main)

    for name, permission in document["permissions"].items():
        if name == writer.PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE:
            assert permission["network"]["enabled"] is False
            assert permission["network"]["allow_local_binding"] is False
            assert "domains" not in permission["network"]
            continue
        assert permission["network"]["allow_local_binding"] is True
        assert permission["network"]["domains"] == {"test-api.feedling.app": "allow"}


def test_writer_creates_private_bundle_once(tmp_path):
    values = _paths(tmp_path)
    output = Path(values["output"])
    bundle = writer.build_config_bundle(**values)
    writer.write_bundle(output, bundle)
    paths = [output, *bundle.profiles]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    with pytest.raises(writer.CodexConfigError, match="unable to create"):
        writer.write_bundle(output, bundle)


def test_pinned_codex_applies_each_top_level_profile_permission(tmp_path):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("pinned Codex CLI is not installed")
    version = subprocess.run(
        [codex, "--version"], capture_output=True, text=True, check=False
    )
    if version.returncode != 0 or version.stdout.strip() != "codex-cli 0.144.3":
        pytest.skip("Codex CLI is not the qualification-pinned 0.144.3")

    values = _paths(tmp_path)
    output = Path(values["output"])
    writer.write_bundle(output, writer.build_config_bundle(**values))
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(output.parent)
    result = subprocess.run(
        [codex, "--strict-config", "doctor", "--summary", "--no-color"],
        cwd=values["source_root"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "config could not be loaded" not in combined, combined
    assert "Configuration" in combined, combined

    manifests = Path(values["profile_manifest_dir"])
    for profile_id, _ in PROFILE_AGENT_TYPES:
        manifest = manifests / f"{profile_id}.json"
        manifest.write_text('{"schema_version":1,"profiles":[]}\n')
        manifest.chmod(0o600)
    full_manifest = Path(values["full_manifest"])
    full_manifest.write_text('{"schema_version":1,"profiles":[]}\n')
    full_manifest.chmod(0o600)
    receipt = Path(values["orchestration_receipt"])
    receipt.write_text("{}\n")
    receipt.chmod(0o600)
    raw_result = Path(values["worker_output_root"]) / "raw.json"
    raw_result.write_text("{}\n")
    raw_result.chmod(0o600)
    canonical = Path(values["aggregation_input_root"]) / "official-deepseek.json"
    canonical.write_text("{}\n")
    canonical.chmod(0o600)
    public_artifact = Path(values["artifact_root"]) / "result.json"
    public_artifact.write_text("{}\n")
    source_private = Path(values["source_root"]) / "qa" / "private.json"
    source_private.write_text("{}\n")

    profile_id, agent_type = PROFILE_AGENT_TYPES[0]
    own_manifest = manifests / f"{profile_id}.json"
    other_manifest = manifests / f"{PROFILE_AGENT_TYPES[1][0]}.json"

    def sandbox_cat(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                codex,
                "sandbox",
                "-p",
                agent_type,
                "-P",
                writer.worker_permission_profile(profile_id),
                "-C",
                str(values["source_root"]),
                "/bin/cat",
                str(path),
            ],
            cwd=values["source_root"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert sandbox_cat(own_manifest).returncode == 0
    for denied in (
        other_manifest,
        full_manifest,
        receipt,
        raw_result,
        canonical,
        public_artifact,
    ):
        assert sandbox_cat(denied).returncode != 0, denied

    def supervisor_cat(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                codex,
                "sandbox",
                "-P",
                writer.SUPERVISOR_PERMISSION_PROFILE,
                "-C",
                str(values["source_root"]),
                "/bin/cat",
                str(path),
            ],
            cwd=values["source_root"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert supervisor_cat(canonical).returncode == 0
    assert supervisor_cat(receipt).returncode == 0
    for denied in (own_manifest, full_manifest, raw_result, public_artifact):
        assert supervisor_cat(denied).returncode != 0, denied

    judge_root = Path(values["persona_judge_root"])
    judge_work = judge_root / "work"
    judge_scratch = judge_work / "scratch.json"
    judge_scratch.write_text("{}\n")
    judge_scratch.chmod(0o600)

    def judge_cat(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                codex,
                "sandbox",
                "-p",
                writer.PERSONA_MEMORY_JUDGE_PROFILE_NAME,
                "-P",
                writer.PERSONA_MEMORY_JUDGE_PERMISSION_PROFILE,
                "-C",
                str(judge_work),
                "/bin/cat",
                str(path),
            ],
            cwd=judge_work,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert judge_cat(judge_scratch).returncode == 0
    for denied in (
        source_private,
        own_manifest,
        full_manifest,
        receipt,
        raw_result,
        canonical,
        public_artifact,
        output,
    ):
        assert judge_cat(denied).returncode != 0, denied


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("source_root", Path("relative")),
        ("artifact_root", Path("relative")),
        ("profile_manifest_dir", Path("relative")),
        ("codex_model", "gpt model"),
        ("allowed_host", "https://test-api.feedling.app"),
        ("allowed_host", "*.feedling.app"),
        ("allowed_host", "test-api.feedling.app:443"),
    ),
)
def test_rejects_unsafe_paths_and_hosts(tmp_path, field, replacement):
    values = _paths(tmp_path)
    values[field] = replacement
    with pytest.raises(writer.CodexConfigError):
        writer.build_config_bundle(**values)


def test_private_directories_must_be_owner_only_and_empty(tmp_path):
    values = _paths(tmp_path)
    Path(values["profile_manifest_dir"]).chmod(0o755)
    with pytest.raises(writer.CodexConfigError, match="owner-only"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "second")
    stale = Path(values["worker_output_root"]) / "stale.json"
    stale.write_text("{}\n")
    with pytest.raises(writer.CodexConfigError, match="must be empty"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "judge-stale")
    (Path(values["persona_judge_root"]) / "stale.json").write_text("{}\n")
    with pytest.raises(writer.CodexConfigError, match="only clean roots"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "judge-permissions")
    (Path(values["persona_judge_root"]) / "work").chmod(0o755)
    with pytest.raises(writer.CodexConfigError, match="persona judge work.*owner-only"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "judge-scratch")
    (Path(values["persona_judge_scratch_root"]) / "stale.json").write_text("{}\n")
    with pytest.raises(writer.CodexConfigError, match="scratch root.*empty"):
        writer.build_config_bundle(**values)


def test_persona_judge_profile_and_scratch_roots_are_atomic_configuration(tmp_path):
    values = _paths(tmp_path)
    values["persona_judge_scratch_root"] = None
    with pytest.raises(writer.CodexConfigError, match="configured together"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "second")
    values["persona_judge_root"] = None
    with pytest.raises(writer.CodexConfigError, match="configured together"):
        writer.build_config_bundle(**values)


def test_runtime_root_cannot_expose_private_run_data(tmp_path):
    values = _paths(tmp_path)
    values["runtime_read_roots"] = [Path(values["supervisor_home"])]
    with pytest.raises(writer.CodexConfigError, match="runtime read roots"):
        writer.build_config_bundle(**values)


def test_only_diagnostic_mode_allows_artifacts_outside_worker_source(tmp_path):
    values = _paths(tmp_path)
    external_artifacts = tmp_path / "operator-artifacts"
    external_artifacts.mkdir(mode=0o700)
    values["artifact_root"] = external_artifacts

    with pytest.raises(writer.CodexConfigError, match="inside the source"):
        writer.build_config_bundle(**values)

    values["qualification_mode"] = "diagnostic"
    bundle = writer.build_config_bundle(**values)

    assert str(external_artifacts.resolve()) in bundle.main


def test_rejects_broad_or_writable_worker_runtime(tmp_path):
    values = _paths(tmp_path)
    values["runtime_read_roots"] = [Path.home()]
    with pytest.raises(writer.CodexConfigError, match="too broad or private"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "writable-root")
    runtime = Path(values["runtime_read_roots"][0])
    runtime.chmod(0o777)
    with pytest.raises(writer.CodexConfigError, match="group/world writable"):
        writer.build_config_bundle(**values)

    values = _paths(tmp_path / "writable-bin")
    Path(values["worker_python"]).parent.chmod(0o777)
    with pytest.raises(writer.CodexConfigError, match="runtime bin"):
        writer.build_config_bundle(**values)


def test_worker_python_must_be_bound_to_declared_runtime(tmp_path):
    values = _paths(tmp_path)
    outside = tmp_path / "outside-python"
    outside.write_text("#!/bin/sh\nexit 0\n")
    outside.chmod(0o700)
    values["worker_python"] = outside

    with pytest.raises(writer.CodexConfigError, match="runtime bin"):
        writer.build_config_bundle(**values)


def test_rejects_denied_data_beneath_an_ambient_read_root(tmp_path, monkeypatch):
    values = _paths(tmp_path)
    monkeypatch.setattr(writer, "_AMBIENT_READ_ROOTS", (tmp_path.resolve(),))
    with pytest.raises(writer.CodexConfigError, match="ambient readable"):
        writer.build_config_bundle(**values)
