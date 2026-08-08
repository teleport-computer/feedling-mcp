from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _job(source: str, name: str, next_name: str) -> str:
    return source.split(f"\n  {name}:\n", 1)[1].split(f"\n  {next_name}:\n", 1)[0]


def test_pre_main_deploy_waits_for_complete_runtime_preflight():
    source = WORKFLOW.read_text()
    deploy = _job(source, "deploy-pre-cvm", "deploy-pre-runner-cvm")
    header = "\n".join(deploy.splitlines()[:8])
    assert "validate-pre-runtime-prerequisites" in header


def test_deployment_branch_pushes_cannot_cancel_a_partial_release_unit():
    source = WORKFLOW.read_text()
    concurrency = source.split("\njobs:\n", 1)[0]
    assert "github.event_name == 'pull_request'" in concurrency
    assert "cancel-in-progress: true" not in concurrency


def test_release_preflights_reject_unnormalized_sandbox_provider_values():
    source = WORKFLOW.read_text()
    assert source.count("sandbox provider must be exactly disabled or e2b") == 3


def test_preflight_validates_entire_two_cvm_release_before_mutation():
    source = WORKFLOW.read_text()
    preflight = _job(
        source,
        "validate-pre-runtime-prerequisites",
        "deploy-cvm",
    )
    for required in (
        "deploy/pre-cvm-id.txt",
        "deploy/pre-runner-cvm-id.txt",
        'if [ "$MAIN_CVM_ID" = "$RUNNER_CVM_ID" ]',
        "PRE_DATABASE_URL",
        "TEST_FEEDLING_RUNTIME_TOKEN_SECRET",
        "PRE_MAIN_API_URL",
        "PRE_MAIN_ENCLAVE_URL",
        "PRE_E2B_API_KEY",
        "PRE_FEEDLING_V2_E2B_TEMPLATE",
        'phala cvms get "$CVM_ID"',
        "Build and verify the content-addressed E2B template",
        "deploy/e2b/runtime-v2/template-tag.txt",
        "feedling feedling-agent-runner",
        "docker manifest inspect",
        "no pre CVM was changed",
    ):
        assert required in preflight


def test_preflight_is_triggered_by_both_cvm_inventory_files():
    source = WORKFLOW.read_text()
    detection = _job(
        source,
        "detect-cvm-changes-pre",
        "validate-pre-runtime-prerequisites",
    )
    assert "deploy/pre-cvm-id.txt" in detection
    assert "deploy/pre-runner-cvm-id.txt" in detection


def test_test_main_deploy_waits_for_the_same_release_unit_preflight():
    source = WORKFLOW.read_text()
    deploy = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    assert "validate-test-runtime-prerequisites" in "\n".join(
        deploy.splitlines()[:8]
    )
    preflight = _job(
        source,
        "validate-test-runtime-prerequisites",
        "validate-prod-runner-topology",
    )
    for required in (
        "deploy/test-cvm-id.txt",
        "deploy/test-runner-cvm-id.txt",
        "feedling feedling-agent-runner",
        "no test CVM was changed",
        'phala cvms get "$CVM_ID"',
        "Build and verify the test E2B template",
    ):
        assert required in preflight


def test_test_deploys_when_the_hosted_v1_consumer_changes():
    source = WORKFLOW.read_text()
    detection = _job(
        source,
        "detect-cvm-changes-test",
        "validate-test-runtime-prerequisites",
    )
    assert "tools/chat_resident_consumer.py" in detection
