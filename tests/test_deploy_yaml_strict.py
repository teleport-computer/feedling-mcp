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
