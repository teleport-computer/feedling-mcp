from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from tools.strict_yaml import load_yaml_strict


ROOT = Path(__file__).resolve().parents[1]


def test_strict_loader_rejects_duplicate_mapping_keys():
    source = dedent(
        """\
        services:
          worker:
            environment:
              FEATURE_FLAG: "0"
              FEATURE_FLAG: "1"
        """
    )

    with pytest.raises(yaml.constructor.ConstructorError) as exc_info:
        load_yaml_strict(source, source_name="duplicate.yaml")

    message = str(exc_info.value)
    assert "duplicate mapping key 'FEATURE_FLAG'" in message
    assert 'in "duplicate.yaml", line 5, column 7' in message
    assert 'in "duplicate.yaml", line 4, column 7' in message


def test_all_deploy_yaml_files_have_unique_mapping_keys():
    paths = sorted((ROOT / "deploy").glob("*.yaml"))
    paths += sorted((ROOT / "deploy").glob("*.yml"))
    assert paths

    for path in paths:
        load_yaml_strict(path.read_text(), source_name=str(path.relative_to(ROOT)))


def test_ci_runs_the_strict_deploy_yaml_gate():
    path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = load_yaml_strict(
        path.read_text(),
        source_name=str(path.relative_to(ROOT)),
    )
    steps = workflow["jobs"]["python-tests"]["steps"]

    assert any(
        "tests/test_deploy_yaml_strict.py" in step.get("run", "")
        for step in steps
    )


def test_test_environment_uses_literal_three_pool_runtime_values():
    path = ROOT / "deploy" / "docker-compose.phala.test.yaml"
    compose = load_yaml_strict(
        path.read_text(),
        source_name=str(path.relative_to(ROOT)),
    )
    environment = compose["services"]["serve-worker"]["environment"]

    assert environment | {
        "FEEDLING_V2_FOREGROUND_SLOTS": "4",
        "FEEDLING_V2_WAKE_SLOTS": "2",
        "FEEDLING_V2_HEAVY_SLOTS": "2",
        "FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY": "1",
        "FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY": "4",
    } == environment
    for key in (
        "FEEDLING_V2_FOREGROUND_SLOTS",
        "FEEDLING_V2_WAKE_SLOTS",
        "FEEDLING_V2_HEAVY_SLOTS",
        "FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY",
        "FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY",
    ):
        assert "${" not in environment[key]
    for retired in (
        "FEEDLING_V2_POOL_MODE",
        "FEEDLING_V2_MAX_WORKERS",
        "FEEDLING_V2_CHAT_PREEMPTION_ENABLED",
        "FEEDLING_V2_SLOT_PROCESS_ISOLATION",
    ):
        assert retired not in environment
