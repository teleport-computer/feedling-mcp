from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_test_backend_declares_its_single_expected_runner():
    compose = (ROOT / "deploy/docker-compose.phala.test.yaml").read_text()
    backend = compose.split("\n  backend:\n", 1)[1]

    assert 'FEEDLING_EXPECTED_RUNNER_COUNT: "1"' in backend
