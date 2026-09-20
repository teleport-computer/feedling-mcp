"""Execute the PerceptKit workflow gate against real pytest exit statuses."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest
import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


def _step():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return next(
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "PerceptKit adapter + conformance suite"
    )


@pytest.mark.parametrize(
    "passed,failure_kind,trailing_line,expected_exit",
    [
        (90, "none", False, 0),
        (90, "error", False, 1),
        (90, "failed", False, 1),
        (89, "none", False, 1),
        (90, "none", True, 0),
    ],
)
def test_perceptkit_gate_preserves_pytest_failure_and_count_floor(
    tmp_path, passed, failure_kind, trailing_line, expected_exit
):
    step = _step()
    suite = tmp_path / "test_sample.py"
    suite.write_text(
        "import pytest\n"
        f"@pytest.mark.parametrize('case', range({passed}))\n"
        "def test_ok(case): pass\n"
        + (
            "@pytest.fixture\n"
            "def broken(): raise RuntimeError('injected setup error')\n"
            "def test_error(broken): pass\n"
            if failure_kind == "error" else ""
        )
        + (
            "def test_fail(): raise RuntimeError('injected test failure')\n"
            if failure_kind == "failed" else ""
        )
    )
    # Keep the actual workflow shell, pipeline and threshold. Only substitute
    # the test selection and isolate its log; DB setup is outside this gate test.
    script = step["run"]
    script = script[script.index("python -m pytest"):]
    script, replacements = re.subn(
        r"python -m pytest\s+(?:tests/\S+\s+\\\s*)+",
        f"{shlex.quote(sys.executable)} -m pytest {shlex.quote(str(suite))} ",
        script.replace("python -m pytest \\\n", "python -m pytest "),
        count=1,
    )
    assert replacements == 1, "workflow test invocation changed; update the harness"
    script = script.replace("/tmp/perceptkit-pytest.log", str(tmp_path / "pytest.log"))
    if trailing_line:
        # Simulate terminal output after pytest's summary, through the same tee.
        script = "{ " + script.replace(
            "-v | tee", "-v; echo trailing-line; } | tee", 1
        )
    # GitHub's implicit Linux shell is bash -e; explicit bash adds pipefail.
    shell = step.get("shell")
    assert shell in (None, "bash"), f"unsupported workflow shell: {shell}"
    argv = ["bash", "--noprofile", "--norc", "-e"]
    if shell == "bash":
        argv += ["-o", "pipefail"]
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [*argv, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert f"{passed} passed" in result.stdout, result.stdout + result.stderr
    if failure_kind == "error":
        assert "1 error" in result.stdout, result.stdout
    elif failure_kind == "failed":
        assert "1 failed" in result.stdout, result.stdout
    if trailing_line:
        assert (tmp_path / "pytest.log").read_text().splitlines()[-1] == "trailing-line"
    assert result.returncode == expected_exit, result.stdout + result.stderr


def test_perceptkit_provisions_backend_database_for_sensitive_suite():
    step = _step()
    assert "tests/test_perceptkit_shadow_sensitive.py" in step["run"]
    # conftest creates isolated DATABASE_URL/TEE_DATABASE_URL from this admin DSN.
    assert step["env"]["FEEDLING_TEST_PG"] == (
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    )
