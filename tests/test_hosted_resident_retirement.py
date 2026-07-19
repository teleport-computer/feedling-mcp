"""Fail-closed guards for fleet-wide hosted resident-process retirement."""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import admin_core  # noqa: E402
from core import store as core_store  # noqa: E402
import db  # noqa: E402


ROOT = Path(__file__).parent.parent
V2_COMMAND = [
    "python",
    "-u",
    "backend/model_api_runtime/v2/serve_worker.py",
]


def test_every_hosted_backend_compose_is_literal_v2_only():
    for relative in (
        "deploy/docker-compose.yaml",
        "deploy/docker-compose.ci.yml",
        "deploy/docker-compose.memory-sandbox.yaml",
        "deploy/docker-compose.phala.yaml",
        "deploy/docker-compose.phala.test.yaml",
        "deploy/docker-compose.phala.pre.yaml",
    ):
        compose = yaml.safe_load((ROOT / relative).read_text())
        assert (
            compose["services"]["backend"]["environment"][
                "FEEDLING_HOSTED_RUNTIME_POLICY"
            ]
            == "v2_only"
        ), relative


def test_every_hosted_worker_compose_can_launch_only_the_v2_pool():
    for relative in (
        "deploy/docker-compose.agent-runner.yaml",
        "deploy/docker-compose.phala.runner.yaml",
        "deploy/docker-compose.phala.pre.runner.yaml",
        "deploy/docker-compose.phala.prod.runner.yaml",
    ):
        source = (ROOT / relative).read_text()
        compose = yaml.safe_load(source)
        assert set(compose["services"]) == {"serve-worker"}, relative
        assert compose["services"]["serve-worker"]["command"] == V2_COMMAND
        assert "volumes" not in compose
        for retired in (
            "backend/agent_runtime/supervisor.py",
            "AGENT_RUNTIME_USERS",
            "AGENT_RUNTIME_AUTODISCOVER",
            "AGENT_RUNTIME_MAX_CHILDREN",
            "FEEDLING_HOST_ALL",
        ):
            assert retired not in source, (relative, retired)


def test_every_hosted_worker_compose_wires_fail_closed_sandbox_configuration():
    for relative in (
        "deploy/docker-compose.agent-runner.yaml",
        "deploy/docker-compose.phala.runner.yaml",
        "deploy/docker-compose.phala.pre.runner.yaml",
        "deploy/docker-compose.phala.prod.runner.yaml",
    ):
        compose = yaml.safe_load((ROOT / relative).read_text())
        environment = compose["services"]["serve-worker"]["environment"]
        assert environment["FEEDLING_V2_SANDBOX_PROVIDER"].endswith(":-disabled}"), (
            relative
        )
        assert "E2B_API_KEY" in environment, relative
        assert "FEEDLING_V2_E2B_TEMPLATE" in environment, relative
        assert environment["FEEDLING_V2_E2B_ALLOW_INTERNET"].endswith(":-0}"), relative


def test_every_hosted_worker_compose_wires_fail_closed_review_configuration():
    for relative in (
        "deploy/docker-compose.agent-runner.yaml",
        "deploy/docker-compose.phala.runner.yaml",
        "deploy/docker-compose.phala.pre.runner.yaml",
        "deploy/docker-compose.phala.prod.runner.yaml",
    ):
        compose = yaml.safe_load((ROOT / relative).read_text())
        environment = compose["services"]["serve-worker"]["environment"]
        assert environment["FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED"].endswith(":-0}"), (
            relative
        )
        assert environment["FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE"].endswith(
            ":-64}"
        ), relative


def test_worker_image_contains_no_resident_runtime_or_cli_toolchain():
    source = (ROOT / "deploy/Dockerfile.agent-runner").read_text()
    instructions = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert (
        'CMD ["python", "-u", "backend/model_api_runtime/v2/serve_worker.py"]' in source
    )
    for retired in (
        "npm install",
        "backend/agent_runtime/supervisor.py",
        "tools/chat_resident_consumer",
        "/agent-data",
    ):
        assert retired not in instructions, retired


def test_ci_has_mandatory_worker_jobs_and_no_resident_rollback_selector():
    source = (ROOT / ".github/workflows/ci.yml").read_text()
    for job in (
        "deploy-test-runner-cvm:",
        "deploy-pre-runner-cvm:",
        "deploy-prod-runner-cvm:",
    ):
        assert job in source
    for retired in (
        "DEPLOY_TEST_RUNNER_CVM",
        "DEPLOY_PRE_RUNNER_CVM",
        "DEPLOY_PROD_RUNNER_CVM",
        "AGENT_RUNTIME_USERS",
        "AGENT_RUNTIME_AUTODISCOVER",
        "AGENT_RUNTIME_MAX_CHILDREN",
        "FEEDLING_HOST_ALL",
        "resident_only",
    ):
        assert retired not in source, retired


def test_ci_passes_e2b_configuration_only_through_worker_deploys_and_preflight():
    source = (ROOT / ".github/workflows/ci.yml").read_text()
    for environment in ("TEST", "PRE", "PROD"):
        assert f"{environment}_FEEDLING_V2_SANDBOX_PROVIDER" in source
        assert f"{environment}_FEEDLING_V2_E2B_TEMPLATE" in source
        assert f"{environment}_FEEDLING_V2_E2B_ALLOW_INTERNET" in source
    assert "secrets.TEST_E2B_API_KEY" in source
    assert "secrets.PRE_E2B_API_KEY" in source
    assert "secrets.E2B_API_KEY" in source
    # One guard per test/pre/prod worker deploy plus one preflight guard for
    # each release unit.
    assert source.count('FEEDLING_V2_SANDBOX_PROVIDER" = "e2b"') == 6


def test_ci_passes_opt_in_review_configuration_to_every_worker_deploy():
    source = (ROOT / ".github/workflows/ci.yml").read_text()
    for environment in ("TEST", "PRE", "PROD"):
        assert (
            f"vars.{environment}_FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED || '0'" in source
        )
        assert (
            f"vars.{environment}_FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE || '64'"
            in source
        )
    assert (
        source.count(
            '-e "FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED='
            '$FEEDLING_V2_TRAJECTORY_REVIEW_ENABLED"'
        )
        == 3
    )
    assert (
        source.count(
            '-e "FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE='
            '$FEEDLING_V2_TRAJECTORY_REVIEW_MAX_ACTIVE"'
        )
        == 3
    )


def test_managed_worker_identity_is_fail_closed_and_gated_per_inventory_cvm():
    for relative in (
        "deploy/docker-compose.phala.runner.yaml",
        "deploy/docker-compose.phala.pre.runner.yaml",
        "deploy/docker-compose.phala.prod.runner.yaml",
    ):
        compose = yaml.safe_load((ROOT / relative).read_text())
        environment = compose["services"]["serve-worker"]["environment"]
        assert environment["FEEDLING_V2_FLEET_IDENTITY_REQUIRED"] == "1"
        assert "FEEDLING_V2_RUNNER_CVM_ID" in environment
        assert "FEEDLING_V2_DEPLOYED_BUILD" in environment

    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert '-e "FEEDLING_V2_RUNNER_CVM_ID=$CVM_ID"' in workflow
    assert '-e "FEEDLING_V2_DEPLOYED_BUILD=$DEPLOYED_BUILD"' in workflow
    assert "Post-deploy Runtime V2 fleet identity gate (prod)" in workflow
    assert "python3 deploy/check-v2-runner-fleet.py" in workflow
    assert '--inventory deploy/prod-runner-cvm-ids.txt' in workflow


def test_production_worker_topology_requires_two_distinct_cvm_ids(tmp_path):
    script = ROOT / "deploy/check-prod-runner-topology.sh"

    def run(lines: str):
        ids = tmp_path / "ids.txt"
        ids.write_text(lines)
        return subprocess.run(
            [str(script), str(ids)],
            text=True,
            capture_output=True,
            check=False,
        )

    assert run("worker-a\n").returncode != 0
    assert run("worker-a\nworker-a\n").returncode != 0
    passed = run("worker-a\nworker-b\n")
    assert passed.returncode == 0, passed.stderr + passed.stdout


def test_admin_resident_selector_fails_before_hydration(monkeypatch):
    monkeypatch.setattr(
        core_store,
        "get_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resident selector reached runtime side effects")
        ),
    )
    assert admin_core.set_runtime_mode("unused", "resident_cli") == (
        {"error": "hosted resident runtime is retired"},
        400,
    )
    source = inspect.getsource(admin_core.set_runtime_mode)
    assert source.index("mode !=") < source.index("get_store")


def test_ops_cli_exposes_v2_repair_not_a_runtime_selector():
    source = (ROOT / "tools/io_cli.py").read_text()
    assert '"repair-runtime-v2"' in source
    assert '"runtime-v2-status"' in source
    assert 'payload={"user_id": args.user_id, "mode": "db_action_v2"}' in source
    assert '"set-runtime-mode"' not in source
    assert '"list-runtime-mode"' not in source


def test_hosted_resident_implementation_is_absent():
    for relative in (
        "backend/agent_runtime/supervisor.py",
        "backend/agent_runtime/spawners.py",
        "backend/agent_runtime/leases.py",
        "backend/agent_runtime/tokens.py",
    ):
        assert not (ROOT / relative).exists(), relative


def test_external_resident_tools_have_api_key_auth_only():
    for relative in (
        "tools/chat_resident_consumer.py",
        "tools/io_cli.py",
    ):
        source = (ROOT / relative).read_text()
        for retired in (
            "FEEDLING_RUNTIME_TOKEN_FILE",
            "X-Feedling-Runtime-Token",
            "_HOSTED",
            "host-all",
        ):
            assert retired not in source, (relative, retired)
        assert "FEEDLING_API_KEY" in source, relative
        assert "X-API-Key" in source, relative


def test_retired_supervisor_db_surface_and_tables_are_absent():
    for name in (
        "set_supervisor_heartbeat",
        "read_supervisor_heartbeat",
        "set_supervisor_instance_heartbeat",
        "list_supervisor_instance_heartbeats",
        "prune_supervisor_instance_heartbeats",
        "list_agent_runtime_enabled_users",
    ):
        assert not hasattr(db, name), name

    with db.get_pool().connection() as conn:
        for table in (
            "agent_runtime_instances",
            "agent_runtime_supervisor_heartbeats",
        ):
            assert conn.execute(
                "SELECT to_regclass(%s)", (f"public.{table}",)
            ).fetchone()[0] is None, table
        assert conn.execute(
            "SELECT 1 FROM server_config "
            "WHERE key='agent_runtime_supervisor_heartbeat'"
        ).fetchone() is None


def test_active_e2e_utilities_do_not_reintroduce_managed_resident_semantics():
    for relative in (
        "tools/e2e/hosted.py",
        "tools/e2e/unlock.py",
        "tools/genesis_e2e.py",
        "tools/gen_url_map.py",
    ):
        source = (ROOT / relative).read_text()
        for retired in (
            "FEEDLING_RUNTIME_TOKEN_FILE",
            "FEEDLING_HOST_ALL",
            "host-all",
            "agent_runtime_instances",
            "agent_runtime/supervisor.py",
        ):
            assert retired not in source, (relative, retired)


def test_historical_supervisor_docs_are_prominently_non_operational():
    retired_pattern = re.compile(
        r"FEEDLING_HOST_ALL|agent_runtime_instances|"
        r"agent_runtime_supervisor_heartbeats|backend/agent_runtime/supervisor\.py|"
        r"host-all|host_all|resident supervisor|FEEDLING_RUNTIME_TOKEN_FILE",
        re.IGNORECASE,
    )
    historical_roots = (
        ROOT / "docs" / "superpowers" / "plans",
        ROOT / "docs" / "superpowers" / "specs",
    )
    for root in historical_roots:
        for path in root.glob("*.md"):
            source = path.read_text()
            if retired_pattern.search(source):
                preamble = "\n".join(source.splitlines()[:12])
                assert "RETIRED / DO NOT DEPLOY" in preamble, path
