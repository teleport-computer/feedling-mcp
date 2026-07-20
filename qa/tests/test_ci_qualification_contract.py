from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PG_DEPLOY = (ROOT / ".github" / "workflows" / "pg-deploy.yml").read_text(
    encoding="utf-8"
)
E2E = (ROOT / ".github" / "workflows" / "api-key-e2e.yml").read_text(encoding="utf-8")
CONTROL = (ROOT / ".github" / "workflows" / "io-e2e-control.yml").read_text(
    encoding="utf-8"
)
REAPER = (ROOT / ".github" / "workflows" / "api-key-e2e-runner-reaper.yml").read_text(
    encoding="utf-8"
)
TEST_COMPOSE = (ROOT / "deploy" / "docker-compose.phala.test.yaml").read_text(
    encoding="utf-8"
)


def _job(name: str, next_name: str) -> str:
    start = CI.index(f"  {name}:\n")
    end = CI.index(f"  {next_name}:\n", start)
    return CI[start:end]


def test_deterministic_qualification_contracts_gate_test_and_production_deploys():
    qa_job = _job("qa-contract-tests", "docker-build")
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert "python -m pytest -q" in qa_job
    assert '--basetemp "${RUNNER_TEMP}/feedling-qa-pytest-' in qa_job
    assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in qa_job
    assert "qa/tests" in qa_job
    assert "tools/provider_smoke/tests" in qa_job
    assert "tests/test_genesis_distill_acceptance.py" in qa_job
    assert "python -m pip install --require-hashes -r qa/requirements.lock" in qa_job
    assert "qa-contract-tests" in test_deploy.split("steps:", 1)[0]
    assert "qa-contract-tests" in production_deploy.split("steps:", 1)[0]


def test_dedicated_control_workflow_dispatches_trusted_e2e_without_inheriting_secrets():
    ci_trigger = CI[CI.index("on:\n") : CI.index("concurrency:\n")]
    control_trigger = CONTROL[CONTROL.index("on:\n") : CONTROL.index("permissions:\n")]
    qualify = CONTROL[CONTROL.index("  qualify:\n") :]

    assert "workflow_dispatch:" not in ci_trigger
    assert "workflow_dispatch:" in control_trigger
    assert "request_id:" in control_trigger
    assert "target_ref:" in control_trigger
    assert "target_sha:" in control_trigger
    assert "lane:" in control_trigger
    assert "suite:" in control_trigger
    assert "runtime_target:" in control_trigger
    assert "persona_repetitions:" in control_trigger
    assert "uses: ./.github/workflows/api-key-e2e.yml" in qualify
    assert "runtime_target: ${{ inputs.runtime_target }}" in qualify
    assert "persona_repetitions: ${{ inputs.persona_repetitions }}" in qualify
    assert "permissions:" in qualify
    assert "contents: read" in qualify
    assert "id-token: write" in qualify
    assert "secrets: inherit" not in qualify
    assert "secrets:" not in qualify
    assert "IO_E2E_ADMIN_TOKEN" not in qualify
    for forbidden_secret in (
        "QA_CODEX_AUTH_JSON_B64",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_RUNNER_GITHUB_APP_PRIVATE_KEY",
    ):
        assert forbidden_secret not in qualify
    assert "workflow_dispatch:" not in E2E
    assert "workflow_call:" in E2E
    workflow_call = E2E[E2E.index("  workflow_call:\n") : E2E.index("permissions:\n")]
    assert "secrets:" not in workflow_call
    assert "group: ci-${{ github.event_name }}-${{ github.ref }}" in CI
    assert "cancel-in-progress: true" in CI


def test_manual_dispatch_skips_ordinary_ci_jobs_and_runs_only_qualification():
    ordinary_jobs = (
        ("forge-test", "python-tests"),
        ("python-tests", "qa-contract-tests"),
        ("qa-contract-tests", "docker-build"),
        ("docker-build", "lint"),
        ("lint", "dcap-python"),
        ("dcap-python", "detect-cvm-changes"),
    )

    for job_name, next_job_name in ordinary_jobs:
        header = _job(job_name, next_job_name).split("    steps:\n", 1)[0]
        assert "if: github.event_name != 'workflow_dispatch'" in header, job_name

    # Deployment roots already require push events, and the always() notifier
    # remains transitively suppressed when deploy-cvm is skipped. Keep those
    # graph constraints explicit so a manual qualification cannot mutate a CVM
    # or send a misleading production-deploy notification.
    for job_name, next_job_name in (
        ("detect-cvm-changes", "detect-cvm-changes-test"),
        ("detect-cvm-changes-test", "validate-prod-runner-topology"),
        ("validate-prod-runner-topology", "deploy-cvm"),
        ("deploy-cvm", "deploy-test-cvm"),
        ("deploy-test-cvm", "deploy-test-runner-cvm"),
        ("deploy-test-runner-cvm", "deploy-prod-runner-cvm"),
        ("deploy-prod-runner-cvm", "notify-lark-prod-deploy"),
    ):
        header = _job(job_name, next_job_name).split("    steps:\n", 1)[0]
        assert "github.event_name == 'push'" in header, job_name

    notifier = CI[CI.index("  notify-lark-prod-deploy:\n") :]
    notifier_header = notifier.split("    steps:\n", 1)[0]
    assert "needs: [deploy-cvm, deploy-prod-runner-cvm]" in notifier_header
    assert "needs.deploy-cvm.result != 'skipped'" in notifier_header


def test_no_repository_workflow_implicitly_inherits_every_available_secret():
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        assert "secrets: inherit" not in path.read_text(encoding="utf-8"), path


def test_e2e_pins_secret_bearing_code_and_treats_deployment_sha_as_metadata():
    trigger = E2E[E2E.index("on:\n") : E2E.index("permissions:\n")]
    resolver = E2E[
        E2E.index("  resolve-test-deployment:\n") : E2E.index(
            "  provision-aws-runner:\n"
        )
    ]
    qualify = E2E[E2E.index("  qualify-api-key-runtime:\n") :]

    assert "expected_test_head_sha:" in trigger
    assert "expected_deployment_sha:" in trigger
    assert trigger.count("required: true") == 3
    assert "group: io-e2e-agent-driven-test" in E2E
    assert 'if [ "$DISPATCH_REF" != "refs/heads/main" ]' in E2E
    assert "runs-on: ubuntu-24.04" in resolver
    assert "environment:" not in resolver
    assert "self-hosted" not in resolver
    assert "secrets." not in resolver
    assert "ref: test" in resolver
    assert "Resolve current serialized test deployment target" in resolver
    assert 'if [ "${#images[@]}" -ne 2 ]' in resolver
    assert "git merge-base --is-ancestor" in resolver
    assert "EXPECTED_TEST_HEAD_SHA: ${{ inputs.expected_test_head_sha }}" in resolver
    assert "EXPECTED_DEPLOYMENT_SHA: ${{ inputs.expected_deployment_sha }}" in resolver
    assert 'if [ "$test_head" != "$EXPECTED_TEST_HEAD_SHA" ]' in resolver
    assert 'if [ "$full_sha" != "$EXPECTED_DEPLOYMENT_SHA" ]' in resolver
    assert (
        "needs: [validate-dispatch, resolve-test-deployment, provision-aws-runner]"
        in qualify
    )
    assert "ref: ${{ github.sha }}" in qualify
    assert "fetch-depth: 1" in qualify
    assert 'checked_out_sha="$(git rev-parse --verify HEAD)"' in qualify
    assert 'if [ "$checked_out_sha" != "$CONTROLLER_SHA" ]' in qualify
    assert (
        "EXPECTED_DEPLOYMENT_SHA: ${{ needs.resolve-test-deployment.outputs.sha }}"
        in qualify
    )
    assert 'echo "expected_sha=$EXPECTED_DEPLOYMENT_SHA"' in qualify


def test_e2e_uses_pinned_jit_app_and_oidc_actions_with_hosted_cleanup():
    provision = E2E[
        E2E.index("  provision-aws-runner:\n") : E2E.index(
            "  qualify-api-key-runtime:\n"
        )
    ]
    qualify_header = E2E[
        E2E.index("  qualify-api-key-runtime:\n") : E2E.index(
            "    steps:\n", E2E.index("  qualify-api-key-runtime:\n")
        )
    ]
    cleanup = E2E[E2E.index("  cleanup-aws-runner:\n") :]
    account_cleanup = E2E[
        E2E.index("  cleanup-synthetic-accounts:\n") : E2E.index(
            "  cleanup-aws-runner:\n"
        )
    ]

    assert "runs-on: ubuntu-24.04" in provision
    assert "id-token: write" in provision
    assert (
        "actions/create-github-app-token@"
        "fee1f7d63c2ff003460e3d139729b119787bc349" in provision
    )
    oidc_pin = (
        "aws-actions/configure-aws-credentials@61815dcd50bd041e203e49132bacad1fd04d2708"
    )
    assert oidc_pin in provision
    assert "/actions/runners/generate-jitconfig" in provision
    assert "python3 qa/aws/launch.py" in provision
    assert "needs: [validate-dispatch, resolve-test-deployment]" in provision
    assert "TARGET_SHA: ${{ needs.resolve-test-deployment.outputs.sha }}" in provision
    assert '--target-sha "$TARGET_SHA"' in provision
    assert '--controller-sha "$CONTROLLER_SHA"' in provision
    assert "Wait for exact JIT evaluator to become ready" in provision
    assert 'runner.get("status") == "online"' in provision
    assert 'runner.get("busy") is False' in provision
    assert "provision-aws-runner" in qualify_header
    assert "needs.provision-aws-runner.outputs.runner_group" in qualify_header
    assert "needs.provision-aws-runner.outputs.runner_label" in qualify_header
    assert "QA_RUNNER_GROUP_NAME" in provision
    assert "/actions/runner-groups/${RUNNER_GROUP_ID}" in provision
    assert "runner group id and name do not identify the same group" in provision
    assert "allows_public_repositories" in provision
    assert "restricted_to_workflows" in provision
    assert "selected_workflows != [expected_workflow]" in provision
    assert '"io-e2e-control.yml@refs/heads/main"' in provision
    assert 'runner.get("runner_group_id")' not in provision
    assert (
        "/actions/runner-groups/${RUNNER_GROUP_ID}/runners?per_page=100&page=1"
        in provision
    )
    assert (
        "/actions/runner-groups/${RUNNER_GROUP_ID}/runners?per_page=100&page=${page}"
        in provision
    )
    assert provision.index(
        'echo "runner_id=$runner_id" >> "$GITHUB_OUTPUT"'
    ) < provision.index("runner_bound=false")
    assert "membership_attempts=12" in provision
    assert "page_count = max(1, (total_count + 99) // 100)" in provision
    assert "len(all_runners) != expected_total" in provision
    assert "total_count > 100" not in provision
    assert "--connect-timeout 5" in provision
    assert "--max-time 20" in provision
    assert "GitHub JIT runner was not bound to the dedicated runner group" in provision
    assert "runs-on: ubuntu-24.04" in account_cleanup
    assert "qa/provision_profiles.py cleanup-run" in account_cleanup
    assert "secrets.IO_E2E_ADMIN_TOKEN" in account_cleanup
    assert "cleanup-synthetic-accounts" in cleanup.split("    steps:\n", 1)[0]
    assert (
        "if: ${{ always() && needs.validate-dispatch.result == 'success' && "
        "needs.provision-aws-runner.result != 'skipped' }}" in cleanup
    )
    assert "runs-on: ubuntu-24.04" in cleanup
    assert oidc_pin in cleanup
    assert "python3 qa/aws/terminate.py" in cleanup
    assert "Mint fresh GitHub App token for registration cleanup" in cleanup
    assert "Delete exact stale JIT runner registration" in cleanup
    assert (
        "if: ${{ always() && steps.github_cleanup_app.outcome == 'success' }}"
        in cleanup
    )
    assert 'runner_id="$EXPECTED_RUNNER_ID"' in cleanup
    assert cleanup.index('runner_id="$EXPECTED_RUNNER_ID"') < cleanup.index(
        "/actions/runners/${runner_id}"
    )
    assert "/actions/runners/${runner_id}" in cleanup


def test_expired_runner_reaper_is_hourly_hosted_oidc_and_protected_main_only():
    trigger = REAPER[REAPER.index("on:\n") : REAPER.index("permissions:\n")]

    assert "schedule:" in trigger
    assert 'cron: "37 * * * *"' in trigger
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "workflow_call:" not in trigger
    assert "github.repository == 'teleport-computer/feedling-mcp'" in REAPER
    assert "github.ref == 'refs/heads/main'" in REAPER
    assert "environment: io-e2e-agent-driven-test" in REAPER
    assert "runs-on: ubuntu-24.04" in REAPER
    assert "self-hosted" not in REAPER
    assert "id-token: write" in REAPER
    assert "ref: ${{ github.sha }}" in REAPER
    assert "persist-credentials: false" in REAPER
    assert (
        "aws-actions/configure-aws-credentials@"
        "61815dcd50bd041e203e49132bacad1fd04d2708" in REAPER
    )
    assert "python3 qa/aws/reap.py" in REAPER
    assert '--region "$AWS_REGION"' in REAPER
    assert '--repository "$GITHUB_REPOSITORY"' in REAPER
    assert "secrets." not in REAPER
    for forbidden_secret in (
        "IO_E2E_ADMIN_TOKEN",
        "QA_CODEX_AUTH_JSON_B64",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
    ):
        assert forbidden_secret not in REAPER


def test_backend_qualification_regressions_run_with_postgres_dependencies():
    backend_job = _job("python-tests", "qa-contract-tests")

    assert "tests/test_memory_contract_backend.py" in backend_job
    assert "tests/test_qa_build_identity.py" in backend_job
    assert "tests/test_qa_synthetic_accounts.py" in backend_job
    assert "FEEDLING_TEST_PG" in backend_job
    assert "backend/requirements.lock" in backend_job


def test_io_e2e_admin_credential_is_narrow_and_not_deployed_to_production():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert "environment: io-test-deploy" in test_deploy
    assert "secrets.IO_E2E_ADMIN_TOKEN" in test_deploy
    assert "secrets.FEEDLING_ADMIN_TOKEN" in test_deploy
    assert "secrets.FEEDLING_ADMIN_TOKEN" in production_deploy
    assert "secrets.IO_E2E_ADMIN_TOKEN" not in production_deploy
    assert 'IO_E2E_ADMIN_TOKEN: "${IO_E2E_ADMIN_TOKEN:-}"' in TEST_COMPOSE
    assert 'FEEDLING_ADMIN_TOKEN: "${FEEDLING_ADMIN_TOKEN:-}"' in TEST_COMPOSE


def test_test_deploy_enables_bounded_synthetic_account_reaper():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert 'IO_E2E_SYNTHETIC_ACCOUNTS_ENABLED: "true"' in test_deploy
    assert 'IO_E2E_SYNTHETIC_ACCOUNT_MAX_TTL_SECONDS: "14400"' in test_deploy
    assert 'IO_E2E_SYNTHETIC_REAPER_INTERVAL_SECONDS: "60"' in test_deploy
    for variable in (
        "IO_E2E_SYNTHETIC_ACCOUNTS_ENABLED",
        "IO_E2E_SYNTHETIC_ACCOUNT_MAX_TTL_SECONDS",
        "IO_E2E_SYNTHETIC_REAPER_INTERVAL_SECONDS",
    ):
        assert f'-e "{variable}=${variable}"' in test_deploy
        assert f'{variable}: "${{{variable}:-' in TEST_COMPOSE
        assert variable not in production_deploy
    assert 'IO_E2E_TEST_DEPLOY_SHA: "${IO_E2E_TEST_DEPLOY_SHA:-}"' in TEST_COMPOSE
    assert 'if [ -z "$IO_E2E_ADMIN_TOKEN" ]' in test_deploy
    assert "IO_E2E_ADMIN_TOKEN secret is not set" in test_deploy


def test_every_test_environment_mutator_uses_the_qualification_lock():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    test_runner_deploy = _job("deploy-test-runner-cvm", "deploy-prod-runner-cvm")

    for source in (test_deploy, test_runner_deploy, PG_DEPLOY, E2E):
        assert "io-e2e-agent-driven-test" in source
    assert "options: [test]" in PG_DEPLOY


def test_manual_qualification_binds_agent_output_to_trusted_inputs():
    assert "on:\n  workflow_dispatch:" in CONTROL
    assert "workflow_call:" in E2E
    assert '--provisioning-manifest "${{ steps.context.outputs.manifest }}"' in E2E
    assert "per-turn five-stage latency" in E2E
    assert "qualification-agent semantic judgment" in E2E
    assert "steps.deployment_pre.outcome" in E2E
    assert "steps.deployment_post.outcome" in E2E
