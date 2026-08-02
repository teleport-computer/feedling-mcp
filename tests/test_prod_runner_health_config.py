from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parent.parent
INVENTORY = ROOT / "deploy/list-prod-runner-cvm-ids.sh"


@pytest.fixture(autouse=True)
def _disable_setup_auto_vision_probe():
    """Keep this static deployment-wiring test independent of backend imports."""


@pytest.fixture(autouse=True)
def _reset_enclave_http_client():
    """Keep this static deployment-wiring test independent of backend imports."""


def test_prod_backend_declares_expected_runner_count():
    compose = (ROOT / "deploy/docker-compose.phala.yaml").read_text()
    backend = compose.split("\n  backend:\n", 1)[1]
    assert 'FEEDLING_EXPECTED_RUNNER_COUNT: "${FEEDLING_EXPECTED_RUNNER_COUNT:-}"' in backend


def test_prod_runner_declares_stable_cvm_heartbeat_identity():
    compose = (ROOT / "deploy/docker-compose.phala.prod.runner.yaml").read_text()
    assert 'FEEDLING_RUNNER_CVM_ID: "${FEEDLING_RUNNER_CVM_ID:-}"' in compose


def test_prod_deploy_derives_and_injects_expected_runner_count():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = workflow.split("\n  deploy-cvm:\n", 1)[1].split("\n  deploy-prod-runner-cvm:\n", 1)[0]
    derivation = (
        "FEEDLING_EXPECTED_RUNNER_COUNT=$("
        "deploy/list-prod-runner-cvm-ids.sh deploy/prod-runner-cvm-ids.txt "
        "| wc -l | tr -d '[:space:]')"
    )
    assert derivation in deploy
    assert '-e "FEEDLING_EXPECTED_RUNNER_COUNT=$FEEDLING_EXPECTED_RUNNER_COUNT"' in deploy


def test_prod_deploy_rejects_zero_expected_runner_count_before_phala_deploy():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = workflow.split("\n  deploy-cvm:\n", 1)[1].split("\n  deploy-prod-runner-cvm:\n", 1)[0]
    zero_count_guard = 'if [ "$FEEDLING_EXPECTED_RUNNER_COUNT" -lt 1 ]; then'
    assert zero_count_guard in deploy
    assert deploy.index(zero_count_guard) < deploy.index("\n          phala deploy \\")


def test_prod_runner_deploy_iterates_normalized_inventory_and_injects_identity():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = workflow.split("\n  deploy-prod-runner-cvm:\n", 1)[1].split(
        "\n  notify-lark-prod-deploy:\n", 1
    )[0]
    assert "IDS=$(deploy/list-prod-runner-cvm-ids.sh deploy/prod-runner-cvm-ids.txt)" in deploy
    assert '-e "FEEDLING_RUNNER_CVM_ID=$CVM_ID"' in deploy


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("\n  # note\n cvm-b \n\tcvm-a\t\n", ["cvm-a", "cvm-b"]),
        ("cvm-a\n cvm-a \n\tcvm-b\n", ["cvm-a", "cvm-b"]),
        ("\n # only comments\n\t\n", []),
    ],
)
def test_prod_runner_inventory_normalizes_comments_whitespace_duplicates_and_zero(
    tmp_path, contents, expected,
):
    inventory = tmp_path / "runner-ids.txt"
    inventory.write_text(contents)

    result = subprocess.run(
        ["bash", str(INVENTORY), str(inventory)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == expected
