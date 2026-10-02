"""Exercise the test deployment's real shell without credentials or network."""

from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

from tools.strict_yaml import load_yaml_strict


ROOT = Path(__file__).resolve().parents[1]
MODE = "FEEDLING_RESIDENT_PLAINTEXT_RECALL"


def _load(path):
    return load_yaml_strict(path.read_text(), source_name=str(path))


def _workflow():
    return _load(ROOT / ".github/workflows/ci.yml")


def _deploy_step():
    steps = _workflow()["jobs"]["deploy-test-cvm"]["steps"]
    return next(step for step in steps if step.get("name") == "Deploy CVM via phala")


def test_resident_mode_is_bound_only_in_test_backend_deployment():
    matches = []
    for job, config in _workflow()["jobs"].items():
        assert MODE not in config.get("env", {})
        for step in config.get("steps", []):
            if MODE in step.get("env", {}):
                matches.append((job, step["name"], step["env"][MODE]))
    assert matches == [(
        "deploy-test-cvm", "Deploy CVM via phala",
        "${{ vars.TEST_FEEDLING_RESIDENT_PLAINTEXT_RECALL || 'off' }}",
    )]
    assert MODE not in _workflow().get("env", {})

    wired = []
    for path in sorted((ROOT / "deploy").glob("docker-compose.phala*.yaml")):
        for service, config in _load(path).get("services", {}).items():
            environment = config.get("environment", {})
            if MODE in environment:
                wired.append((path.name, service, environment[MODE]))
    assert wired == [(
        "docker-compose.phala.test.yaml", "backend", f"${{{MODE}:-off}}",
    )]


def _run_deploy(tmp_path, mode):
    """Run the complete parsed step, replacing only external side effects."""
    step = _deploy_step()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv = tmp_path / "phala-argv"
    phala = bindir / "phala"
    phala.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$ARGV_FILE"\n')
    phala.chmod(0o755)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    wait = deploy / "wait-cvm-ready.sh"
    wait.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$WAIT_FILE"\n')
    wait.chmod(0o755)
    # Never inherit developer credentials, proxies or a real phala executable.
    env = {key: "fixture" for key in step["env"]}
    env.update(
        PATH=f"{bindir}:/usr/bin:/bin",
        ARGV_FILE=str(argv), WAIT_FILE=str(tmp_path / "wait-argv"),
        GITHUB_SHA="1" * 40, CVM_ID="fixture-cvm",
    )
    if mode is None:
        env.pop(MODE, None)
    else:
        env[MODE] = mode
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    return result, argv


@pytest.mark.parametrize("mode", [None, "", "off", "shadow", "on"])
def test_valid_resident_mode_reaches_phala_and_backend(tmp_path, mode):
    result, argv = _run_deploy(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    args = argv.read_text().splitlines()
    assert args[0] == "deploy"
    assert args[args.index("-c") + 1] == "deploy/docker-compose.phala.test.yaml"
    forwarded = [args[i + 1] for i, arg in enumerate(args) if arg == "-e"]
    values = [value for value in forwarded if value.startswith(f"{MODE}=")]
    assert values == [f"{MODE}={mode or 'off'}"]
    assert (tmp_path / "wait-argv").read_text().splitlines() == ["fixture-cvm", "900"]

    compose = _load(ROOT / "deploy/docker-compose.phala.test.yaml")
    interpolation = compose["services"]["backend"]["environment"][MODE]
    assert interpolation == f"${{{MODE}:-off}}"
    # This supported Compose ${VAR:-default} form uses shell's empty/unset
    # semantics; exercise the parsed value with the exact forwarded env.
    resolved = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f'printf "%s" "{interpolation}"'],
        env={MODE: values[0].split("=", 1)[1]}, capture_output=True, text=True,
        check=True, timeout=10,
    )
    assert resolved.stdout == (mode or "off")


@pytest.mark.parametrize("mode", ["invalid", "ON", " on", "on ", "on\n", "on; exit 0"])
def test_invalid_resident_mode_never_calls_deploy_or_readiness(tmp_path, mode):
    result, argv = _run_deploy(tmp_path, mode)
    assert result.returncode != 0
    assert "must be exactly off, shadow or on" in result.stdout
    assert not argv.exists()
    assert not (tmp_path / "wait-argv").exists()


def test_resident_deployment_guard_is_in_ci():
    steps = _workflow()["jobs"]["python-tests"]["steps"]
    assert any("tests/test_resident_recall_deploy.py" in step.get("run", "") for step in steps)
