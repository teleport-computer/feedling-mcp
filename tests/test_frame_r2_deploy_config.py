from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from tools.strict_yaml import load_yaml_strict


ROOT = Path(__file__).resolve().parents[1]
R2_KEYS = (
    "R2_ENDPOINT",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_FRAMES_BUCKET",
)
MAIN_COMPOSES = (
    "docker-compose.phala.yaml",
    "docker-compose.phala.test.yaml",
    "docker-compose.phala.pre.yaml",
)
RUNNER_COMPOSES = (
    "docker-compose.phala.prod.runner.yaml",
    "docker-compose.phala.runner.yaml",
    "docker-compose.phala.pre.runner.yaml",
)


def _load(path: Path):
    return load_yaml_strict(path.read_text(), source_name=str(path.relative_to(ROOT)))


def test_every_frame_reader_receives_r2_configuration():
    expected = {key: f"${{{key}:-}}" for key in R2_KEYS}

    for name in MAIN_COMPOSES:
        compose = _load(ROOT / "deploy" / name)
        for service in ("backend", "serve-worker"):
            environment = compose["services"][service]["environment"]
            assert {key: environment.get(key) for key in R2_KEYS} == expected, (
                name,
                service,
            )

    for name in RUNNER_COMPOSES:
        compose = _load(ROOT / "deploy" / name)
        environment = compose["services"]["agent-runner"]["environment"]
        assert {key: environment.get(key) for key in R2_KEYS} == expected, name


def test_runner_deploy_jobs_forward_r2_secrets():
    workflow = _load(ROOT / ".github" / "workflows" / "ci.yml")
    cases = {
        "deploy-test-runner-cvm": "TEST_",
        "deploy-pre-runner-cvm": "TEST_",
        "deploy-prod-runner-cvm": "",
    }

    for job_name, secret_prefix in cases.items():
        steps = workflow["jobs"][job_name]["steps"]
        deploy_step = next(step for step in steps if "phala deploy" in step.get("run", ""))
        for key in R2_KEYS:
            assert deploy_step["env"][key] == f"${{{{ secrets.{secret_prefix}{key} }}}}"
            assert f'-e "{key}=${key}"' in deploy_step["run"]


def test_export_configuration_reaches_both_test_processes_only():
    expected = {
        "FEEDLING_EXPORT_ENABLED": "${FEEDLING_EXPORT_ENABLED:-0}",
        "FEEDLING_EXPORT_MAILER": "${FEEDLING_EXPORT_MAILER:-log}",
        "R2_EXPORTS_BUCKET": "${R2_EXPORTS_BUCKET:-}",
        "FEEDLING_EXPORT_DAILY_LIMIT": "${FEEDLING_EXPORT_DAILY_LIMIT:-3}",
        "CLOUDFLARE_EMAIL_TOKEN": "${CLOUDFLARE_EMAIL_TOKEN:-}",
        "CLOUDFLARE_EMAIL_ACCOUNT_ID": "${CLOUDFLARE_EMAIL_ACCOUNT_ID:-}",
        "FEEDLING_EXPORT_EMAIL_FROM": "${FEEDLING_EXPORT_EMAIL_FROM:-}",
        "FEEDLING_EXPORT_EMAIL_ALLOWLIST": "${FEEDLING_EXPORT_EMAIL_ALLOWLIST:-}",
    }
    test = _load(ROOT / "deploy/docker-compose.phala.test.yaml")
    for service in ("backend", "serve-worker"):
        env = test["services"][service]["environment"]
        assert {key: env.get(key) for key in expected} == expected
    for name in ("docker-compose.phala.yaml", "docker-compose.phala.pre.yaml"):
        compose = _load(ROOT / "deploy" / name)
        for service in ("backend", "serve-worker"):
            env = compose["services"][service]["environment"]
            assert env.get("FEEDLING_EXPORT_ENABLED", "0") == "0"
            assert not env.get("R2_EXPORTS_BUCKET")


def _execute_test_deploy(tmp_path, **overrides):
    workflow = _load(ROOT / ".github/workflows/ci.yml")
    step = next(s for s in workflow["jobs"]["deploy-test-cvm"]["steps"]
                if "phala deploy" in s.get("run", ""))
    assert step["env"]["R2_EXPORTS_BUCKET"] == "${{ vars.TEST_R2_EXPORTS_BUCKET }}"
    # Execute the actual parsed workflow script; only its external CLI is replaced.
    captured = tmp_path / "deploy-args.json"
    phala = tmp_path / "phala"
    phala.write_text(f"#!{sys.executable}\nimport json,sys\n"
                     f"open({str(captured)!r},'w').write(json.dumps(sys.argv[1:]))\n")
    phala.chmod(0o755)
    (tmp_path / "deploy").mkdir()
    wait = tmp_path / "deploy/wait-cvm-ready.sh"
    wait.write_text("#!/bin/sh\nexit 0\n")
    wait.chmod(0o755)
    env = {key: "" for key in step["env"]}
    env.update(PATH=f"{tmp_path}{os.pathsep}{os.defpath}", DATABASE_URL="local-test",
               GITHUB_SHA="1234567890", R2_EXPORTS_BUCKET="",
               FEEDLING_EXPORT_ENABLED="1", FEEDLING_EXPORT_DAILY_LIMIT="3")
    env.update(overrides)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    return step, result, json.loads(captured.read_text()) if captured.exists() else []


@pytest.mark.parametrize("bucket,enabled", [("", "0"), ("io-test-exports", "1")])
def test_actual_test_deploy_script_fails_closed_without_bucket(tmp_path, bucket, enabled):
    step, result, args = _execute_test_deploy(tmp_path, R2_EXPORTS_BUCKET=bucket)
    assert result.returncode == 0, result.stderr
    forwarded = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-e")
    assert forwarded["R2_EXPORTS_BUCKET"] == bucket
    assert forwarded["FEEDLING_EXPORT_ENABLED"] == enabled
    assert forwarded["FEEDLING_EXPORT_MAILER"] == "log"
    assert forwarded["FEEDLING_EXPORT_DAILY_LIMIT"] == "3"
    assert args[args.index("-c") + 1] == "deploy/docker-compose.phala.test.yaml"
    workflow = _load(ROOT / ".github/workflows/ci.yml")
    for job in ("deploy-cvm", "deploy-pre-cvm"):
        for other in workflow["jobs"][job]["steps"]:
            assert "R2_EXPORTS_BUCKET" not in other.get("env", {})
            assert "FEEDLING_EXPORT_ENABLED" not in other.get("run", "")
            assert "CLOUDFLARE_EMAIL_TOKEN" not in other.get("env", {})


@pytest.mark.parametrize("missing", [None, "CLOUDFLARE_EMAIL_TOKEN", "CLOUDFLARE_EMAIL_ACCOUNT_ID",
                                    "FEEDLING_EXPORT_EMAIL_FROM", "FEEDLING_EXPORT_EMAIL_ALLOWLIST"])
def test_actual_test_mailer_selection_requires_every_setting(tmp_path, missing):
    values = {"CLOUDFLARE_EMAIL_TOKEN": "unit-not-real-token",
              "CLOUDFLARE_EMAIL_ACCOUNT_ID": "a" * 32,
              "FEEDLING_EXPORT_EMAIL_FROM": "welcome@rokku.ai",
              "FEEDLING_EXPORT_EMAIL_ALLOWLIST": "allow@example.com"}
    if missing:
        values[missing] = ""
    step, result, args = _execute_test_deploy(tmp_path, **values)
    assert result.returncode == 0, result.stderr
    assert step["env"]["CLOUDFLARE_EMAIL_TOKEN"] == "${{ secrets.TEST_CLOUDFLARE_EMAIL_TOKEN }}"
    for key, variable in (("CLOUDFLARE_EMAIL_ACCOUNT_ID", "TEST_CLOUDFLARE_EMAIL_ACCOUNT_ID"),
                          ("FEEDLING_EXPORT_EMAIL_FROM", "TEST_EXPORT_EMAIL_FROM"),
                          ("FEEDLING_EXPORT_EMAIL_ALLOWLIST", "TEST_EXPORT_EMAIL_ALLOWLIST")):
        assert step["env"][key] == "${{ vars." + variable + " }}"
    forwarded = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-e")
    assert forwarded["FEEDLING_EXPORT_MAILER"] == ("log" if missing else "cloudflare")
    assert {key: forwarded[key] for key in values} == values
    assert "unit-not-real-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("allowlist", ["*", "allow@example.com,*"])
def test_test_deploy_refuses_wildcard_recipients(tmp_path, allowlist):
    _, result, args = _execute_test_deploy(tmp_path, FEEDLING_EXPORT_EMAIL_ALLOWLIST=allowlist)
    assert result.returncode != 0 and not args
    assert "requires concrete addresses" in result.stdout
