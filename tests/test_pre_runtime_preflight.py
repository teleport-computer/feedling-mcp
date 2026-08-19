from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEE_MIGRATE_WORKFLOW = ROOT / ".github" / "workflows" / "tee-migrate.yml"
TEST_COMPOSE = ROOT / "deploy" / "docker-compose.phala.test.yaml"
TEST_RUNNER_COMPOSE = ROOT / "deploy" / "docker-compose.phala.runner.yaml"
PROD_COMPOSE = ROOT / "deploy" / "docker-compose.phala.yaml"
PROD_RUNNER_COMPOSE = ROOT / "deploy" / "docker-compose.phala.prod.runner.yaml"


def test_tee_migrate_has_one_head_after_runtime_v2_alignment():
    cfg = Config(str(ROOT / "backend" / "alembic_tee" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "alembic_tee"))
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0026_plaintext_shadow_control"]
    assert (
        script.get_revision("0026_plaintext_shadow_control").down_revision
        == "0025_lane_rollup_voice"
    )
    assert (
        script.get_revision("0025_lane_rollup_voice").down_revision
        == "0024_lane_rollup_safe_ts"
    )
    assert (
        script.get_revision("0024_lane_rollup_safe_ts").down_revision
        == "0023_lane_daily_rollup"
    )
    assert (
        script.get_revision("0023_lane_daily_rollup").down_revision
        == "0022_v2_wake_outcomes"
    )
    assert (
        script.get_revision("0022_v2_wake_outcomes").down_revision
        == "0021_agent_jobs_available_at"
    )
    assert (
        script.get_revision("0021_agent_jobs_available_at").down_revision
        == "0020_v2_first_chat_activation"
    )
    assert (
        script.get_revision("0020_v2_first_chat_activation").down_revision
        == "0019_v2_worker_pool_heartbeats"
    )
    assert (
        script.get_revision("0019_v2_worker_pool_heartbeats").down_revision
        == "0018_v2_wake_shadow_decisions"
    )
    assert (
        script.get_revision("0018_v2_wake_shadow_decisions").down_revision
        == "0017_voice_primary_alignment"
    )
    # The prepared-head pin must name whichever revision is CURRENTLY head —
    # a cutover that replays a stale pin re-arms the old head and the preflight
    # then waves through a database that is one migration behind. Derive it
    # from get_heads() so adding a revision without advancing its pin fails
    # here instead of at cutover time.
    (head,) = script.get_heads()
    migration = script.get_revision(head).module
    assert f"'[\"{head}\"]'::jsonb" in migration._UPDATE_PREPARED_HEAD


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


def test_preflight_blocks_tee_primary_deploy_before_mutating_a_cvm():
    source = WORKFLOW.read_text()
    preflight = _job(
        source,
        "validate-pre-runtime-prerequisites",
        "deploy-cvm",
    )

    for required in (
        "PRE_FEEDLING_DATABASE_SCHEMA == 'tee'",
        "PRE_TEE_MIGRATION_DSN",
        "PRE_TEE_PG_CA_PEM",
        "backend/alembic_tee/alembic.ini",
        "SELECT version_num FROM alembic_tee_version",
        "PRE TEE schema migration required",
        "run the TEE migrate workflow for pre",
        "No PRE CVM was changed",
    ):
        assert required in preflight

    schema_gate = preflight.index(
        "Require PRE TEE schema at release head before mutating either CVM"
    )
    image_gate = preflight.index(
        "Require both Runtime V2 images before mutating either CVM"
    )
    assert schema_gate < image_gate


def test_pre_release_gates_run_the_application_startup_contract():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-pre-runtime-prerequisites",
        "deploy-cvm",
    )
    tee_migrate = TEE_MIGRATE_WORKFLOW.read_text()

    for source in (preflight, tee_migrate):
        assert 'os.environ["FEEDLING_DATABASE_SCHEMA"] = "tee"' in source
        assert 'os.environ["DATABASE_URL"] = os.environ["TEE_MIGRATION_DATABASE_URL"]' in source
        assert "db.init_schema()" in source

    assert "Assert PRE application startup contract" in tee_migrate


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


def test_test_stage_a_keeps_rds_primary_and_tee_shadow_wiring():
    source = WORKFLOW.read_text()
    main = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    runner = _job(source, "deploy-test-runner-cvm", "deploy-pre-cvm")

    assert "${{ secrets.TEST_DATABASE_URL }}" in main
    assert "${{ secrets.TEST_TEE_DATABASE_URL }}" in main
    assert "${{ secrets.TEST_FEEDLING_TEE_DUAL_WRITE }}" in main
    assert "${{ secrets.TEST_DATABASE_URL }}" in runner
    assert "PRE_DATABASE_URL" not in main
    assert "PRE_DATABASE_URL" not in runner


def test_test_deploys_forward_one_database_schema_selector_to_both_cvms():
    source = WORKFLOW.read_text()
    main = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    runner = _job(source, "deploy-test-runner-cvm", "deploy-pre-cvm")

    selector = "${{ vars.TEST_FEEDLING_DATABASE_SCHEMA || 'rds' }}"
    for deploy in (main, runner):
        assert "FEEDLING_DATABASE_SCHEMA:" in deploy
        assert selector in deploy
        assert '-e "FEEDLING_DATABASE_SCHEMA=$FEEDLING_DATABASE_SCHEMA"' in deploy


def test_test_compose_forwards_database_schema_to_every_database_client():
    main = TEST_COMPOSE.read_text()
    backend = main.split("\n  backend:\n", 1)[1].split("\n  serve-worker:\n", 1)[0]
    worker = main.split("\n  serve-worker:\n", 1)[1]
    runner = TEST_RUNNER_COMPOSE.read_text()
    selector = 'FEEDLING_DATABASE_SCHEMA: "${FEEDLING_DATABASE_SCHEMA:-rds}"'

    assert selector in backend
    assert selector in worker
    assert selector in runner


def test_test_preflight_blocks_tee_primary_before_mutating_either_cvm():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-test-runtime-prerequisites",
        "validate-prod-runner-topology",
    )

    for required in (
        "TEST_FEEDLING_DATABASE_SCHEMA == 'tee'",
        "TEST_TEE_MIGRATION_DSN",
        "TEST_TEE_PG_CA_PEM",
        "APP_DATABASE_URL",
        "backend/alembic_tee/alembic.ini",
        "SELECT version_num FROM alembic_tee_version",
        "owner_fingerprint != app_fingerprint",
        "TEST TEE schema migration required",
        "run the TEE migrate workflow for test",
        "No TEST CVM was changed",
        'os.environ["FEEDLING_DATABASE_SCHEMA"] = "tee"',
        'os.environ["DATABASE_URL"] = os.environ["APP_DATABASE_URL"]',
        "db.init_schema()",
    ):
        assert required in preflight

    schema_gate = preflight.index(
        "Require TEST TEE schema at release head before mutating either CVM"
    )
    image_gate = preflight.index(
        "Require both Runtime V2 images before mutating either CVM"
    )
    assert schema_gate < image_gate


def test_test_preflight_rejects_noncanonical_database_schema_selector():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-test-runtime-prerequisites",
        "validate-prod-runner-topology",
    )
    complete_config = preflight.split(
        "- name: Require complete Runtime V2 configuration", 1
    )[1].split("\n      - name:", 1)[0]

    assert "FEEDLING_DATABASE_SCHEMA:" in complete_config
    assert "${{ vars.TEST_FEEDLING_DATABASE_SCHEMA || 'rds' }}" in complete_config
    assert 'case "$FEEDLING_DATABASE_SCHEMA" in' in complete_config
    assert "rds|tee)" in complete_config
    assert "must be exactly rds or tee" in complete_config


def test_prod_deploys_forward_one_database_schema_selector_to_every_database_client():
    source = WORKFLOW.read_text()
    main = _job(source, "deploy-cvm", "deploy-test-cvm")
    runner = _job(source, "deploy-prod-runner-cvm", "notify-lark-prod-deploy")

    selector = "${{ vars.PROD_FEEDLING_DATABASE_SCHEMA || 'rds' }}"
    for deploy in (main, runner):
        assert "FEEDLING_DATABASE_SCHEMA:" in deploy
        assert selector in deploy
        assert '-e "FEEDLING_DATABASE_SCHEMA=$FEEDLING_DATABASE_SCHEMA"' in deploy


def test_prod_compose_forwards_database_schema_to_every_database_client():
    main = PROD_COMPOSE.read_text()
    backend = main.split("\n  backend:\n", 1)[1].split("\n  serve-worker:\n", 1)[0]
    worker = main.split("\n  serve-worker:\n", 1)[1]
    runner = PROD_RUNNER_COMPOSE.read_text()
    selector = 'FEEDLING_DATABASE_SCHEMA: "${FEEDLING_DATABASE_SCHEMA:-rds}"'

    assert selector in backend
    assert selector in worker
    assert selector in runner


def test_prod_preflight_blocks_unready_tee_primary_before_mutating_any_cvm():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-prod-runner-topology",
        "detect-cvm-changes-pre",
    )

    for required in (
        "PROD_FEEDLING_DATABASE_SCHEMA == 'tee'",
        "PROD_TEE_MIGRATION_DSN",
        "PROD_TEE_PG_CA_PEM",
        "APP_DATABASE_URL",
        "backend/alembic_tee/alembic.ini",
        "SELECT version_num FROM alembic_tee_version",
        "owner_fingerprint != app_fingerprint",
        'owner_user != "feedling_owner"',
        "current_user",
        "PROD TEE schema migration required",
        "run the TEE migrate workflow for prod",
        "No production CVM was changed",
        'os.environ["FEEDLING_DATABASE_SCHEMA"] = "tee"',
        'os.environ["DATABASE_URL"] = os.environ["APP_DATABASE_URL"]',
        "db.init_schema()",
    ):
        assert required in preflight

    schema_gate = preflight.index(
        "Require PROD TEE schema at release head before mutating any CVM"
    )
    image_gate = preflight.index(
        "Require both production images before mutating either CVM"
    )
    assert schema_gate < image_gate


def test_prod_preflight_reads_owner_role_before_enforcing_it():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-prod-runner-topology",
        "detect-cvm-changes-pre",
    )
    schema_step = preflight.split(
        "- name: Require PROD TEE schema at release head before mutating any CVM",
        1,
    )[1].split("\n      - name:", 1)[0]

    assignment = (
        'owner_user = str(conn.execute("SELECT current_user").fetchone()[0])'
    )
    enforcement = 'if owner_user != "feedling_owner":'
    assert assignment in schema_step
    assert schema_step.index(assignment) < schema_step.index(enforcement)


def test_prod_preflight_rejects_invalid_selector_and_stale_shadow_wiring():
    preflight = _job(
        WORKFLOW.read_text(),
        "validate-prod-runner-topology",
        "detect-cvm-changes-pre",
    )
    complete_config = preflight.split(
        "- name: Require complete production Runtime V2 configuration", 1
    )[1].split("\n      - name:", 1)[0]

    assert "FEEDLING_DATABASE_SCHEMA:" in complete_config
    assert "${{ vars.PROD_FEEDLING_DATABASE_SCHEMA || 'rds' }}" in complete_config
    assert 'case "$FEEDLING_DATABASE_SCHEMA" in' in complete_config
    assert "rds|tee)" in complete_config
    assert "PROD_FEEDLING_DATABASE_SCHEMA must be exactly rds or tee" in complete_config
    assert "PROD_TEE_DATABASE_URL must be empty for TEE primary" in complete_config
    assert "PROD_FEEDLING_TEE_DUAL_WRITE must be empty for TEE primary" in complete_config
