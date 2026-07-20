from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROL = (ROOT / ".github" / "workflows" / "io-e2e-control.yml").read_text(
    encoding="utf-8"
)
EVALUATOR = (ROOT / ".github" / "workflows" / "api-key-e2e.yml").read_text(
    encoding="utf-8"
)
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _job(name: str, next_name: str | None = None) -> str:
    start = CONTROL.index(f"  {name}:\n")
    if next_name is None:
        return CONTROL[start:]
    return CONTROL[start : CONTROL.index(f"  {next_name}:\n", start)]


def test_control_is_manual_only_while_ordinary_ci_has_no_e2e_dispatch() -> None:
    trigger = CONTROL[CONTROL.index("on:\n") : CONTROL.index("permissions:\n")]
    ci_trigger = CI[CI.index("on:\n") : CI.index("concurrency:\n")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "workflow_dispatch:" not in ci_trigger
    assert "uses: ./.github/workflows/api-key-e2e.yml" not in CI
    assert "concurrency:" not in CONTROL
    assert "group: io-e2e-agent-driven-test" in EVALUATOR


def test_authorization_is_secretless_and_fails_closed_before_evaluator() -> None:
    authorize = _job("authorize-request", "qualify")
    qualify = _job("qualify")

    assert "environment:" not in authorize
    assert "secrets." not in authorize
    assert "refs/heads/main" in authorize
    assert "request_id must be a lowercase UUIDv4" in authorize
    assert "target_sha must be an immutable full Git SHA" in authorize
    assert "ref: test" in authorize
    assert 'if [ "$test_head" != "$TARGET_SHA" ]' in authorize
    assert "test moved after the client resolved it" in authorize
    assert "branch_preview is not enabled" in authorize
    assert "requires target_ref=test" in authorize
    assert "Require protected controller and deployment branches" in authorize
    assert 'for branch in main test' in authorize
    assert 'branches/${branch}' in authorize
    assert 'rules/branches/${branch}' in authorize
    assert '"deletion", "non_fast_forward"' in authorize
    assert 'required_approving_review_count' in authorize
    assert 'dismiss_stale_reviews_on_push' in authorize
    assert 'require_last_push_approval' in authorize
    assert 'required_review_thread_resolution' in authorize
    assert "deployments: read" in CONTROL
    assert "Require branch-scoped no-reviewer QA environments" in authorize
    assert "io-e2e-agent-driven-test:main" in authorize
    assert "io-test-deploy:test" in authorize
    assert "deployment-branch-policies" in authorize
    assert 'protection_rules[0].get("type") == "branch_policy"' in authorize
    assert 'python3 -I - "$request_dir/request-manifest.json"' in authorize
    after_target_checkout = authorize[authorize.index("uses: actions/checkout@") :]
    assert "python3 - " not in after_target_checkout
    assert authorize.index("Reject an untrusted controller") < authorize.index(
        "uses: actions/checkout@"
    )
    assert "needs: authorize-request" in qualify


def test_candidate_ref_never_selects_controller_or_evaluator_code() -> None:
    assert "ref: ${{ inputs.target_ref }}" not in CONTROL
    assert "ref: ${{ inputs.target_sha }}" not in CONTROL
    assert "uses: ./.github/workflows/api-key-e2e.yml" in CONTROL
    assert "expected_test_head_sha: ${{ inputs.target_sha }}" in CONTROL
    assert (
        "expected_deployment_sha: ${{ needs.authorize-request.outputs.deployed_sha }}"
        in CONTROL
    )
    assert "inputs.controller_ref" not in CONTROL.lower()
    assert "base_url" not in CONTROL.lower()
    assert "target_url" not in CONTROL.lower()
    assert "workflow_dispatch:" not in EVALUATOR
    assert "workflow_call:" in EVALUATOR


def test_reusable_call_passes_no_secret_and_evaluator_uses_environment() -> None:
    qualify = _job("qualify")

    assert "secrets: inherit" not in qualify
    assert "secrets:" not in qualify
    assert "IO_E2E_ADMIN_TOKEN" not in qualify
    assert "environment: io-e2e-agent-driven-test" in EVALUATOR
    assert "secrets.IO_E2E_ADMIN_TOKEN" in EVALUATOR
    for forbidden in (
        "QA_CODEX_AUTH_JSON_B64",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_RUNNER_GITHUB_APP_PRIVATE_KEY",
    ):
        assert forbidden not in qualify


def test_request_manifest_binds_controller_target_and_deployed_revision() -> None:
    authorize = _job("authorize-request", "qualify")

    for field in (
        '"request_id"',
        '"repository"',
        '"controller_sha"',
        '"target_ref"',
        '"target_sha"',
        '"deployed_sha"',
        '"lane"',
        '"suite"',
        '"runtime_target"',
        '"persona_repetitions"',
    ):
        assert field in authorize
    assert "sort_keys=True" in authorize
    assert (
        "io-e2e-request-${{ inputs.request_id }}-${{ github.run_id }}-"
        "${{ github.run_attempt }}"
    ) in authorize
    assert "retention-days: 30" in authorize
