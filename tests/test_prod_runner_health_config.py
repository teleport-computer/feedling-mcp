from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


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


def test_prod_deploy_derives_and_injects_expected_runner_count():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = workflow.split("\n  deploy-cvm:\n", 1)[1].split("\n  deploy-prod-runner-cvm:\n", 1)[0]
    assert "deploy/prod-runner-cvm-ids.txt" in deploy
    assert "FEEDLING_EXPECTED_RUNNER_COUNT" in deploy
    assert '-e "FEEDLING_EXPECTED_RUNNER_COUNT=$FEEDLING_EXPECTED_RUNNER_COUNT"' in deploy


def test_prod_deploy_rejects_zero_expected_runner_count_before_phala_deploy():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = workflow.split("\n  deploy-cvm:\n", 1)[1].split("\n  deploy-prod-runner-cvm:\n", 1)[0]
    zero_count_guard = 'if [ "$FEEDLING_EXPECTED_RUNNER_COUNT" -lt 1 ]; then'
    assert zero_count_guard in deploy
    assert deploy.index(zero_count_guard) < deploy.index("\n          phala deploy \\")
