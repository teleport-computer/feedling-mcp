from __future__ import annotations

from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "api-key-e2e.yml"
).read_text(encoding="utf-8")


def _step(name: str, next_name: str) -> str:
    start = WORKFLOW.index(f"      - name: {name}")
    end = WORKFLOW.index(f"      - name: {next_name}", start)
    return WORKFLOW[start:end]


def test_workflow_is_manual_only_and_uses_protected_jit_runner():
    trigger = WORKFLOW[WORKFLOW.index("on:\n") : WORKFLOW.index("permissions:\n")]
    assert "workflow_dispatch:" in trigger
    assert "workflow_call:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "  deployment:" not in trigger
    assert "validate-dispatch:" in WORKFLOW
    assert 'if [ "$DISPATCH_REF" != "refs/heads/main" ]' in WORKFLOW
    assert (
        "needs: [validate-dispatch, resolve-test-deployment, provision-aws-runner]"
        in WORKFLOW
    )
    assert "environment: feedling-e2e-test" in WORKFLOW
    assert "runs-on:\n      group: ${{ needs.provision-aws-runner.outputs.runner_group }}" in WORKFLOW
    assert "labels: ${{ needs.provision-aws-runner.outputs.runner_label }}" in WORKFLOW
    assert "timeout-minutes: 240" in WORKFLOW
    assert "group: feedling-test-environment" in WORKFLOW
    assert "persist-credentials: false" in WORKFLOW
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in WORKFLOW
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in WORKFLOW
    assert (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in WORKFLOW
    )


def test_github_hosted_controller_provisions_private_jit_aws_runner():
    provision = WORKFLOW[
        WORKFLOW.index("  provision-aws-runner:\n") : WORKFLOW.index(
            "  qualify-api-key-runtime:\n"
        )
    ]

    assert "needs: [validate-dispatch, resolve-test-deployment]" in provision
    assert "runs-on: ubuntu-24.04" in provision
    assert "environment: feedling-e2e-test" in provision
    assert "id-token: write" in provision
    assert "ref: ${{ github.sha }}" in provision
    assert "persist-credentials: false" in provision
    assert (
        "actions/create-github-app-token@"
        "fee1f7d63c2ff003460e3d139729b119787bc349" in provision
    )
    assert (
        "aws-actions/configure-aws-credentials@"
        "61815dcd50bd041e203e49132bacad1fd04d2708" in provision
    )
    assert "/actions/runners/generate-jitconfig" in provision
    assert "RUNNER_GROUP_NAME: ${{ vars.QA_RUNNER_GROUP_NAME }}" in provision
    assert 'echo "group=$RUNNER_GROUP_NAME" >> "$GITHUB_OUTPUT"' in provision
    assert "runner group name is invalid" in provision
    assert (
        "/orgs/${GITHUB_REPOSITORY_OWNER}/actions/runner-groups/"
        "${RUNNER_GROUP_ID}" in provision
    )
    assert "runner group id and name do not identify the same group" in provision
    assert "qualification runners require a dedicated non-default group" in provision
    assert 'group.get("allows_public_repositories") is not False' in provision
    assert 'group.get("restricted_to_workflows") is not True' in provision
    assert 'selected_workflows = group.get("selected_workflows")' in provision
    assert 'selected_workflows != [expected_workflow]' in provision
    assert '"api-key-e2e.yml@refs/heads/main"' in provision
    assert (
        "qualification runner group must select only the protected main workflow"
        in provision
    )
    assert 'runner_identity="feedling-e2e-${run_key}"' in provision
    assert '"labels": ["self-hosted", "linux", "x64", "feedling-e2e", label]' in provision
    assert 'echo "runner_id=$runner_id" >> "$GITHUB_OUTPUT"' in provision
    assert provision.index(
        'echo "runner_id=$runner_id" >> "$GITHUB_OUTPUT"'
    ) < provision.index("runner_bound=false")
    assert 'runner.get("runner_group_id")' not in provision
    assert (
        "/actions/runner-groups/${RUNNER_GROUP_ID}/runners?per_page=100&page=1"
        in provision
    )
    assert (
        "/actions/runner-groups/${RUNNER_GROUP_ID}/runners?per_page=100&page=${page}"
        in provision
    )
    assert "membership_attempts=12" in provision
    assert "backoff=$((1 << (attempt - 1)))" in provision
    assert 'if [ "$backoff" -gt 10 ]' in provision
    assert "--connect-timeout 5" in provision
    assert "--max-time 20" in provision
    assert "--max-time 30" in provision
    assert "page_count = max(1, (total_count + 99) // 100)" in provision
    assert "len(all_runners) != expected_total" in provision
    assert "len(set(runner_ids)) != len(runner_ids)" in provision
    assert (
        'matches = [item for item in all_runners if item.get("id") == expected_id]'
        in provision
    )
    assert "total_count > 100" not in provision
    assert "GitHub JIT runner was not bound to the dedicated runner group" in provision
    assert 'runner.get("name") != os.environ["RUNNER_NAME"]' in provision
    assert "JIT runner group membership is missing its unique label" in provision
    assert "python3 qa/aws/launch.py" in provision
    assert '--jit-config-file "$JIT_CONFIG_FILE"' in provision
    assert 'rm -f -- "$JIT_CONFIG_FILE"' in provision
    assert '--run-id "$GITHUB_RUN_ID"' in provision
    assert '--run-attempt "$GITHUB_RUN_ATTEMPT"' in provision
    assert "TARGET_SHA: ${{ needs.resolve-test-deployment.outputs.sha }}" in provision
    assert '--target-sha "$TARGET_SHA"' in provision
    assert '--controller-sha "$CONTROLLER_SHA"' in provision
    assert '--github-output "$GITHUB_OUTPUT"' in provision
    assert 'echo "runner-label=' not in provision
    assert 'echo "encoded_jit_config=' not in provision
    assert 'cat "$jit_config_file"' not in provision
    assert 'echo "$GH_APP_TOKEN"' not in provision


def test_qualification_waits_for_exact_online_idle_jit_runner():
    provision = WORKFLOW[
        WORKFLOW.index("  provision-aws-runner:\n") : WORKFLOW.index(
            "  qualify-api-key-runtime:\n"
        )
    ]
    readiness = _step(
        "Wait for exact JIT evaluator to become ready",
        "Remove local JIT configuration",
    )
    qualify_header = WORKFLOW[
        WORKFLOW.index("  qualify-api-key-runtime:\n") : WORKFLOW.index(
            "    steps:\n", WORKFLOW.index("  qualify-api-key-runtime:\n")
        )
    ]

    assert "id: runner_ready" in readiness
    assert "/actions/runners/${RUNNER_ID}" in readiness
    assert 'runner.get("id") != expected_id' in readiness
    assert 'runner.get("name") != expected_name' in readiness
    assert 'os.environ["RUNNER_LABEL"] not in label_names' in readiness
    assert 'runner.get("status") == "online"' in readiness
    assert 'runner.get("busy") is False' in readiness
    assert "for attempt in $(seq 1 60)" in readiness
    assert "python3 qa/aws/terminate.py" in readiness
    assert 'echo "ready=true" >> "$GITHUB_OUTPUT"' in readiness
    assert provision.index("id: launch") < provision.index("id: runner_ready")
    assert "provision-aws-runner" in qualify_header
    assert "needs.provision-aws-runner.outputs.runner_group" in qualify_header
    assert "needs.provision-aws-runner.outputs.runner_label" in qualify_header


def test_failed_provisioning_rolls_back_by_exact_attempt_even_without_instance_output():
    rollback = WORKFLOW[
        WORKFLOW.index("      - name: Roll back failed AWS provisioning attempt\n") :
        WORKFLOW.index("  qualify-api-key-runtime:\n")
    ]

    assert "if: ${{ always()" in rollback
    assert "steps.launch.outcome == 'failure'" in rollback
    assert "steps.runner_ready.outcome == 'failure'" in rollback
    assert "steps.jit_cleanup.outcome == 'failure'" in rollback
    assert "python3 qa/aws/terminate.py" in rollback
    assert '--repository "$GITHUB_REPOSITORY"' in rollback
    assert '--run-id "$GITHUB_RUN_ID"' in rollback
    assert '--run-attempt "$GITHUB_RUN_ATTEMPT"' in rollback
    assert 'if [ -n "$INSTANCE_ID" ]' in rollback


def test_github_hosted_cleanup_always_terminates_exact_managed_attempt():
    cleanup = WORKFLOW[WORKFLOW.index("  cleanup-aws-runner:\n") :]
    cleanup_header = cleanup[: cleanup.index("    steps:\n")]

    assert (
        "if: ${{ always() && needs.validate-dispatch.result == 'success' && "
        "needs.provision-aws-runner.result != 'skipped' }}" in cleanup
    )
    assert "needs.provision-aws-runner.result == 'success'" not in cleanup_header
    assert (
        "needs: [validate-dispatch, provision-aws-runner, qualify-api-key-runtime]"
        in cleanup
    )
    assert "runs-on: ubuntu-24.04" in cleanup
    assert "id-token: write" in cleanup
    assert "ref: ${{ github.sha }}" in cleanup
    assert "persist-credentials: false" in cleanup
    assert (
        "aws-actions/configure-aws-credentials@"
        "61815dcd50bd041e203e49132bacad1fd04d2708" in cleanup
    )
    assert "python3 qa/aws/terminate.py" in cleanup
    assert '--repository "$GITHUB_REPOSITORY"' in cleanup
    assert '--run-id "$GITHUB_RUN_ID"' in cleanup
    assert '--run-attempt "$GITHUB_RUN_ATTEMPT"' in cleanup
    assert 'args+=(--instance-id "$INSTANCE_ID")' in cleanup
    assert (
        "actions/create-github-app-token@"
        "fee1f7d63c2ff003460e3d139729b119787bc349" in cleanup
    )
    assert "Mint fresh GitHub App token for registration cleanup" in cleanup
    assert "Delete exact stale JIT runner registration" in cleanup
    assert "if: ${{ always() && steps.verify_cleanup.outcome == 'success' }}" in cleanup
    assert (
        "if: ${{ always() && steps.github_cleanup_app.outcome == 'success' }}"
        in cleanup
    )
    assert "needs.provision-aws-runner.outputs.runner_id" in cleanup
    assert "needs.provision-aws-runner.outputs.runner_name" in cleanup
    assert "len(matches) > 1" in cleanup
    assert "?name=${derived_name}&per_page=100" in cleanup
    assert cleanup.count("--connect-timeout 5") >= 2
    assert cleanup.count("--max-time 20") >= 2
    assert "runner_id != int(supplied_id)" in cleanup
    assert "/actions/runners/${runner_id}" in cleanup
    assert "204)" in cleanup
    assert "404)" in cleanup
    assert "self-hosted" not in cleanup


def test_deployment_target_is_metadata_and_controller_code_is_immutable():
    trigger = WORKFLOW[WORKFLOW.index("on:\n") : WORKFLOW.index("permissions:\n")]
    resolver = WORKFLOW[
        WORKFLOW.index("  resolve-test-deployment:\n") : WORKFLOW.index(
            "  provision-aws-runner:\n"
        )
    ]
    qualify = WORKFLOW[WORKFLOW.index("  qualify-api-key-runtime:\n") :]
    checkout = _step(
        "Check out immutable trusted controller revision",
        "Verify immutable trusted controller checkout",
    )
    controller_check = _step(
        "Verify immutable trusted controller checkout",
        "Set up Python",
    )
    context = _step(
        "Prepare isolated run directories",
        "Verify deployed endpoint and selected runtime target before qualification",
    )

    assert "expected_deployment_sha" not in trigger
    assert "group: feedling-test-environment" in WORKFLOW
    assert "ref: ${{ github.sha }}" in checkout
    assert "fetch-depth: 1" in checkout
    assert "persist-credentials: false" in checkout
    assert "inputs.expected_deployment_sha" not in WORKFLOW
    assert 'checked_out_sha="$(git rev-parse --verify HEAD)"' in controller_check
    assert 'if [ "$checked_out_sha" != "$CONTROLLER_SHA" ]' in controller_check
    assert "runs-on: ubuntu-24.04" in resolver
    assert "environment:" not in resolver
    assert "self-hosted" not in resolver
    assert "secrets." not in resolver
    assert "ref: test" in resolver
    assert "Resolve current serialized test deployment target" in resolver
    assert 'if [ "${#images[@]}" -ne 2 ]' in resolver
    assert "git merge-base --is-ancestor" in resolver
    assert "ref: test" not in qualify
    assert (
        "needs: [validate-dispatch, resolve-test-deployment, provision-aws-runner]"
        in qualify
    )
    assert (
        "EXPECTED_DEPLOYMENT_SHA: ${{ needs.resolve-test-deployment.outputs.sha }}"
        in context
    )
    assert 'echo "expected_sha=$EXPECTED_DEPLOYMENT_SHA"' in context


def test_codex_preflight_installs_oauth_and_real_top_level_profile_config():
    preflight = _step(
        "Install and verify isolated headless Codex runtime",
        "Provision eight isolated API-key profiles",
    )
    assert "qa/install_codex_auth.py" in preflight
    assert "qa/write_codex_config.py" in preflight
    assert preflight.index("qa/write_codex_config.py") < preflight.index(
        "qa/install_codex_auth.py"
    )
    assert "--full-manifest" in preflight
    assert "--worker-output-root" in preflight
    assert "--aggregation-input-root" in preflight
    assert "--orchestration-receipt" in preflight
    assert "--runtime-read-root" in preflight
    assert '--worker-python "$python_executable"' in preflight
    assert "--qualification-mode release" in preflight
    assert "codex-cli 0.144.3" in preflight
    assert "persistent Codex auth is forbidden" in preflight
    assert "must run as an unprivileged user" in preflight
    assert "secrets.QA_CODEX_AUTH_JSON_B64" in preflight
    assert '"$QA_CODEX_HOME/auth.json"' in preflight
    assert "vars.QA_CODEX_MODEL" in preflight
    assert "unset QA_CODEX_AUTH_JSON_B64" in preflight
    assert "mcp list --json" in preflight
    assert "sandbox -p profile_official_deepseek" in preflight
    assert "-P feedling-e2e-official-deepseek" in preflight
    assert 'test "$QA_PYTHON_BIN" = "$1"' in preflight
    assert 'test "$QA_QUALIFICATION_MODE" = "release"' in preflight
    assert 'exec "$QA_PYTHON_BIN" -I -B "$2" --help' in preflight
    assert "https://test-api.feedling.app/" in preflight
    assert "https://example.com/" in preflight
    assert "https://1.1.1.1/" in preflight
    assert "--noproxy" in preflight
    assert "-p profile_official_deepseek" in preflight
    assert "--strict-config" in preflight
    assert "--output-schema" in preflight
    assert "parse_exec_events" in preflight
    assert "spawn_agent" not in preflight
    assert "record_codex_subagent_hook" not in preflight
    assert "dangerously-bypass-hook-trust" not in preflight


def test_codex_preflight_network_denial_probe_has_balanced_conditionals():
    preflight = _step(
        "Install and verify isolated headless Codex runtime",
        "Provision eight isolated API-key profiles",
    )
    start = preflight.index("https://test-api.feedling.app/")
    end = preflight.index("# Prove the fixed interpreter", start)
    network_probe = preflight[start:end]

    assert network_probe.count("if curl") == 2
    assert sum(line.strip() == "fi" for line in network_probe.splitlines()) == 2


def test_provider_admin_and_oauth_secrets_have_fixed_trust_boundaries():
    provision = _step(
        "Provision eight isolated API-key profiles",
        "Split credentials into isolated one-profile manifests",
    )
    workers = _step(
        "Run eight independent headless Codex profile agents",
        "Verify independent Codex worker lifecycle and canonical inputs",
    )
    supervisor = _step(
        "Run intelligent Codex qualification aggregator",
        "Publish canonical result without following agent-created links",
    )
    scan = _step(
        "Scan public artifacts for secrets and raw evidence",
        "Upload sanitized public qualification artifacts",
    )
    for secret_name in (
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
    ):
        assert f"secrets.{secret_name}" in provision
        assert f"secrets.{secret_name}" in scan
        assert WORKFLOW.count(f"secrets.{secret_name}") == 2
        assert secret_name not in workers
        assert secret_name not in supervisor
    assert WORKFLOW.count("secrets.QA_TEST_ADMIN_TOKEN") == 5
    assert WORKFLOW.count("secrets.QA_CODEX_AUTH_JSON_B64") == 2
    assert "QA_TEST_ADMIN_TOKEN" not in workers
    assert "QA_TEST_ADMIN_TOKEN" not in supervisor
    assert "QA_CODEX_AUTH_JSON_B64" not in workers
    assert "QA_CODEX_AUTH_JSON_B64" not in supervisor
    assert "env -i" in supervisor
    for variable_name in (
        "QA_GEMINI_MODEL",
        "QA_KONGBEIQIE_MODEL",
        "QA_KONGBEIQIE_BASE_URL",
    ):
        assert f"vars.{variable_name}" in provision
        assert variable_name not in workers
        assert variable_name not in supervisor


def test_manifest_isolation_is_probed_for_all_eight_profiles():
    split = _step(
        "Split credentials into isolated one-profile manifests",
        "Verify every profile manifest permission boundary",
    )
    isolation = _step(
        "Verify every profile manifest permission boundary",
        "Run eight independent headless Codex profile agents",
    )
    assert "qa/split_profile_manifests.py" in split
    assert "profiles=(" in isolation
    assert "agent_types=(" in isolation
    assert 'own_manifest="${QA_PROFILE_MANIFEST_DIR}/${profile_id}.json"' in isolation
    assert 'sandbox -p "$agent_type"' in isolation
    assert '-P "feedling-e2e-${profile_id}"' in isolation
    assert "stat -c" in isolation
    assert "os.O_WRONLY | os.O_APPEND" in isolation
    assert "denied_paths=(" in isolation
    assert "QA_PRIVATE_MANIFEST" in isolation
    assert "QA_WORKER_OUTPUT_ROOT" in isolation
    assert "QA_AGGREGATION_INPUT_ROOT" in isolation
    assert "QA_ORCHESTRATION_RECEIPT" in isolation
    assert "QA_MEMORY_MANIFEST" in isolation
    assert '--memory-output "${{ steps.context.outputs.memory_manifest }}"' in split
    assert "source-write-must-fail" in isolation
    for profile_id, agent_type in (
        ("official-deepseek", "profile_official_deepseek"),
        ("official-anthropic", "profile_official_anthropic"),
        ("official-openai", "profile_official_openai"),
        ("official-gemini", "profile_official_gemini"),
        ("openrouter-claude", "profile_openrouter_claude"),
        ("openrouter-openai", "profile_openrouter_openai"),
        ("openrouter-glm", "profile_openrouter_glm"),
        ("relay-kongbeiqie", "profile_relay_kongbeiqie"),
    ):
        assert f"            {profile_id}\n" in isolation
        assert f"            {agent_type}\n" in isolation


def test_deterministic_launcher_runs_exact_independent_profile_matrix():
    workers = _step(
        "Run eight independent headless Codex profile agents",
        "Verify independent Codex worker lifecycle and canonical inputs",
    )
    assert "qa/run_codex_profile_workers.py" in workers
    assert '--codex-home "$QA_CODEX_HOME"' in workers
    assert '--artifact-root "$QA_ARTIFACT_DIR"' in workers
    assert '--profile-manifest-dir "$QA_PROFILE_MANIFEST_DIR"' in workers
    assert '--worker-output-root "$QA_WORKER_OUTPUT_ROOT"' in workers
    assert '--aggregation-input-root "$QA_AGGREGATION_INPUT_ROOT"' in workers
    assert "qa/schemas/codex-run-result.schema.json" in workers
    assert '--receipt "$QA_ORCHESTRATION_RECEIPT"' in workers
    assert "--worker-python" in workers
    assert "--timeout-seconds 2400" in workers
    assert "timeout-minutes: 140" in workers
    assert "spawn_agent" not in workers
    assert "followup_task" not in workers
    assert "hook" not in workers.lower()


def test_real_codex_preflight_binds_the_locked_permission_profile():
    preflight = _step(
        "Install and verify isolated headless Codex runtime",
        "Provision eight isolated API-key profiles",
    )
    assert "-p profile_official_deepseek" in preflight
    assert "-c 'default_permissions=\"feedling-e2e-official-deepseek\"'" in preflight


def test_raw_worker_output_is_verified_but_not_exposed_to_aggregator():
    orchestration = _step(
        "Verify independent Codex worker lifecycle and canonical inputs",
        "Verify deployed endpoint and selected runtime target after profile testing",
    )
    supervisor = _step(
        "Run intelligent Codex qualification aggregator",
        "Publish canonical result without following agent-created links",
    )
    assert "qa/verify_codex_orchestration.py" in orchestration
    assert "--receipt" in orchestration
    assert "--worker-output-root" in orchestration
    assert "--aggregation-input-root" in orchestration
    assert "QA_WORKER_OUTPUT_ROOT" not in supervisor
    assert "raw worker events/stderr" in supervisor
    assert "QA_AGGREGATION_INPUT_ROOT" in supervisor
    assert "QA_ORCHESTRATION_RECEIPT" in supervisor
    assert "--disable multi_agent" in supervisor
    assert "--disable network_proxy" in supervisor
    assert "launch another agent" in supervisor


def test_aggregator_preserves_semantic_and_cot_evidence_and_writes_privately():
    supervisor = _step(
        "Run intelligent Codex qualification aggregator",
        "Publish canonical result without following agent-created links",
    )
    for secret_name in (
        "QA_TEST_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
    ):
        assert secret_name not in supervisor
    assert "persona" in supervisor
    assert "reasoning/COT evidence" in supervisor
    assert "trace correlation" in supervisor
    assert "Copy all eight profile objects exactly" in supervisor
    assert "three fixed" in supervisor
    assert "batches (3+3+2)" in supervisor
    assert "profiles_expected and profiles_completed are both 8" in supervisor
    assert "must sum to eight" in supervisor
    assert "summary counts" in supervisor
    assert "--strict-config" in supervisor
    assert (
        '--output-schema "$GITHUB_WORKSPACE/qa/schemas/codex-run-result.schema.json"'
        in supervisor
    )
    assert '--output-last-message "$QA_PRIVATE_RESULT"' in supervisor
    assert "run-result.json" not in supervisor


def test_selected_runtime_target_is_checked_before_and_after_live_profile_agents():
    deployment_pre = _step(
        "Verify deployed endpoint and selected runtime target before qualification",
        "Install and verify isolated headless Codex runtime",
    )
    deployment_post = _step(
        "Verify deployed endpoint and selected runtime target after profile testing",
        "Run intelligent Codex qualification aggregator",
    )
    validate = _step(
        "Validate complete release result",
        "Scan public artifacts for secrets and raw evidence",
    )
    for deployment in (deployment_pre, deployment_post):
        assert "qa/verify_deployment.py" in deployment
        assert "secrets.QA_TEST_ADMIN_TOKEN" in deployment
        assert "deployment_receipt" in deployment
    assert "steps.orchestration.outcome == 'success'" in deployment_post
    assert "--deployment-receipt" in validate
    assert "--post-deployment-receipt" in validate
    assert "--orchestration-receipt" in validate


def test_manual_dispatch_defaults_to_strict_v2_and_preserves_baseline_option():
    trigger = WORKFLOW[WORKFLOW.index("on:\n") : WORKFLOW.index("permissions:\n")]
    assert "runtime_target:" in trigger
    assert "default: hosted_resident" in trigger
    assert "- deployed_current" in trigger
    assert "- hosted_resident" in trigger

    deployment_pre = _step(
        "Verify deployed endpoint and selected runtime target before qualification",
        "Install and verify isolated headless Codex runtime",
    )
    provision = _step(
        "Provision eight isolated API-key profiles",
        "Split credentials into isolated one-profile manifests",
    )
    workers = _step(
        "Run eight independent headless Codex profile agents",
        "Verify independent Codex worker lifecycle and canonical inputs",
    )
    deployment_post = _step(
        "Verify deployed endpoint and selected runtime target after profile testing",
        "Run intelligent Codex qualification aggregator",
    )
    validate = _step(
        "Validate complete release result",
        "Scan public artifacts for secrets and raw evidence",
    )

    for step in (deployment_pre, provision, workers, deployment_post, validate):
        assert "QA_EXPECTED_RUNTIME: ${{ inputs.runtime_target }}" in step
    for deployment in (deployment_pre, deployment_post):
        assert '--expected-runtime "$QA_EXPECTED_RUNTIME"' in deployment
    assert 'runtime_flag="--baseline-runtime"' in provision
    assert 'if [ "$QA_EXPECTED_RUNTIME" = "hosted_resident" ]' in provision
    assert 'runtime_flag="--require-runtime-v2"' in provision
    assert '"$runtime_flag"' in provision
    assert '--expected-runtime "$QA_EXPECTED_RUNTIME"' in workers
    assert '--expected-runtime "$QA_EXPECTED_RUNTIME"' in validate


def test_agent_result_is_published_and_rendered_only_by_trusted_code():
    publish = _step(
        "Publish canonical result without following agent-created links",
        "Render trusted derived artifacts",
    )
    render = _step(
        "Render trusted derived artifacts",
        "Validate complete release result",
    )
    assert "qa/publish_agent_result.py" in publish
    assert '--source "${{ steps.context.outputs.private_result }}"' in publish
    assert (
        '--destination "${{ steps.context.outputs.artifact_dir }}/run-result.json"'
        in publish
    )
    assert "qa/render_artifacts.py" in render
    assert '--result "$QA_ARTIFACT_DIR/run-result.json"' in render
    assert "--schema qa/schemas/run-result.schema.json" in render


def test_memory_contract_uses_isolated_account_and_deterministic_gate_policy():
    memory = _step(
        "Run deterministic memory contract on isolated synthetic account",
        "Verify deployed endpoint and selected runtime target after profile testing",
    )
    validate = _step(
        "Validate complete release result",
        "Scan public artifacts for secrets and raw evidence",
    )
    enforce = WORKFLOW[WORKFLOW.index("      - name: Enforce fail-closed") :]

    assert "qa/memory_contract_smoke.py" in memory
    assert '--manifest "$QA_MEMORY_MANIFEST"' in memory
    assert '--output "$QA_MEMORY_RECEIPT"' in memory
    assert "continue-on-error: true" in memory
    assert "steps.split_manifests.outcome == 'success'" in memory
    assert "steps.orchestration.outcome == 'success'" in memory
    assert WORKFLOW.index(
        "Verify deployed endpoint and selected runtime target before qualification"
    ) < WORKFLOW.index(
        "Run deterministic memory contract on isolated synthetic account"
    ) < WORKFLOW.index(
        "Verify deployed endpoint and selected runtime target after profile testing"
    )
    for secret_name in (
        "QA_TEST_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_CODEX_AUTH_JSON_B64",
    ):
        assert secret_name not in memory
    assert "qa/validate_run.py" in validate
    assert "MEMORY_CONTRACT" not in enforce


def test_secret_scan_includes_credentials_oauth_and_persona_privacy_fixture():
    scan = _step(
        "Scan public artifacts for secrets and raw evidence",
        "Upload sanitized public qualification artifacts",
    )
    assert "qa/scan_artifacts.py" in scan
    assert "--manifest" in scan
    assert "--memory-manifest" in scan
    assert "--codex-auth" in scan
    assert "--fixture qa/fixtures/persona-import-v1.json" in scan
    for secret_name in (
        "QA_TEST_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_CODEX_AUTH_JSON_B64",
    ):
        assert f"secrets.{secret_name}" in scan


def test_cleanup_diagnostic_upload_and_final_gate_are_fail_closed():
    cleanup = _step(
        "Cleanup every synthetic account with deterministic evidence",
        "Validate deterministic cleanup receipt",
    )
    cleanup_receipt = _step(
        "Validate deterministic cleanup receipt",
        "Scan public artifacts for secrets and raw evidence",
    )
    scan = _step(
        "Scan public artifacts for secrets and raw evidence",
        "Upload sanitized public qualification artifacts",
    )
    upload = _step(
        "Upload sanitized public qualification artifacts",
        "Remove public scratch after upload decision",
    )
    assert "if: always()" in cleanup
    assert "qa/provision_profiles.py cleanup" in cleanup
    assert "--receipt" in cleanup
    assert "--run-id" in cleanup
    assert "--retain-manifest" in cleanup
    assert "qa/validate_cleanup_receipt.py" in cleanup_receipt
    assert "if: always()" in cleanup_receipt
    assert "qa/scan_artifacts.py" in scan
    assert "steps.secret_scan.outcome == 'success'" in upload
    assert "steps.cleanup.outcome == 'success'" in upload
    assert "steps.cleanup_receipt.outcome == 'success'" in upload
    assert "steps.validate.outcome" not in upload
    assert "include-hidden-files: false" in upload
    assert "retention-days: 14" in upload
    assert "Enforce fail-closed qualification outcome" in WORKFLOW
    assert '"profile-workers:$PROFILE_WORKERS"' in WORKFLOW
    assert '"orchestration:$ORCHESTRATION"' in WORKFLOW
    assert '"validate:$VALIDATE"' in WORKFLOW
    assert '"secret-scan:$SECRET_SCAN"' in WORKFLOW
    assert '"cleanup-receipt:$CLEANUP_RECEIPT"' in WORKFLOW
    assert "release qualification: PASS" in WORKFLOW
