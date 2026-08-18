from pathlib import Path

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
