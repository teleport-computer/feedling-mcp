from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa import run_persona_memory_arm as supervisor


BUILD_SHA = "a" * 40


def _run_cleanup_receipt(run_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "qa_synthetic_run_cleanup",
        "run_id_sha256": hashlib.sha256(run_id.encode()).hexdigest(),
        "label_prefix_sha256": hashlib.sha256(
            f"agent-e2e-{run_id}-".encode()
        ).hexdigest(),
        "database_authoritative": True,
        "matched_count": 0,
        "deleted_count": 0,
        "already_absent_count": 0,
        "operation_failure_count": 0,
        "remaining_count": 0,
        "complete": True,
    }


def _private_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _args(tmp_path: Path):
    private = _private_dir(tmp_path, "private")
    work = _private_dir(tmp_path, "work")
    codex_home = _private_dir(tmp_path, "codex-home")
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return SimpleNamespace(
        target_label="candidate",
        target_id="candidate-under-test",
        build_sha=BUILD_SHA,
        profile="official-openai",
        repetitions=3,
        concurrency=3,
        private_root=private,
        work_dir=work,
        artifact_dir=artifacts,
        codex_bin=tmp_path / "codex",
        codex_home=codex_home,
        judge_model="judge-model",
        judge_id="persona-memory-judge-v1",
        judge_configuration_id="rubric-v1",
        judge_codex_profile="persona_memory_judge",
        judge_permission_profile="feedling-e2e-persona-memory-judge",
        judge_reasoning_effort="medium",
        judge_timeout=180.0,
        allow_private_judge_egress=True,
    )


def _fake_runner(
    commands,
    *,
    run_code: int,
    provision_code: int = 0,
    write_provision_manifest: bool = True,
    write_blocked_result: bool = False,
    result_status: str | None = None,
    finalize_code: int = 0,
    run_cleanup_code: int = 0,
    run_cleanup_complete: bool = True,
):
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(command, *, env):
        command = list(command)
        seen.append((command, dict(env)))
        script = Path(command[1]).name
        subcommand = command[2] if len(command) > 2 else ""

        def value(flag: str) -> Path:
            return Path(command[command.index(flag) + 1])

        if script == "provision_profiles.py" and subcommand == "provision-pool":
            if write_provision_manifest:
                value("--manifest").write_text("pool", encoding="utf-8")
            return provision_code
        elif script == "provision_profiles.py" and subcommand == "cleanup":
            value("--manifest").unlink()
        elif script == "provision_profiles.py" and subcommand == "cleanup-run":
            if run_cleanup_code == 0:
                run_id = command[command.index("--run-id") + 1]
                receipt = value("--receipt")
                payload = _run_cleanup_receipt(run_id)
                if not run_cleanup_complete:
                    payload.update(
                        {
                            "matched_count": 1,
                            "operation_failure_count": 1,
                            "remaining_count": 1,
                            "complete": False,
                        }
                    )
                receipt.write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                receipt.chmod(0o600)
            return run_cleanup_code
        elif script == "verify_deployment.py":
            value("--receipt").write_text("deployment", encoding="utf-8")
        elif script == "prepare_persona_memory_accounts.py" and subcommand == "prepare":
            value("--post-deployment-receipt").write_text(
                "import-post", encoding="utf-8"
            )
            value("--readiness-receipt").write_text("ready", encoding="utf-8")
        elif script == "run_persona_memory_regression.py" and subcommand == "run-live":
            if run_code in {0, 1} or write_blocked_result:
                status = result_status or {
                    0: "PASS",
                    1: "FAIL",
                    2: "BLOCKED_EVIDENCE",
                }[run_code]
                output = value("--output")
                output.write_text(json.dumps({"status": status}), encoding="utf-8")
                output.chmod(0o600)
            return run_code
        elif script == "prepare_persona_memory_accounts.py" and subcommand == "cleanup":
            value("--account-pool").unlink()
            value("--receipt").write_text("cleanup", encoding="utf-8")
        elif script == "run_persona_memory_regression.py" and subcommand == "finalize-arm":
            status = result_status or {
                0: "PASS",
                1: "FAIL",
                2: "BLOCKED_EVIDENCE",
            }[run_code]
            output = value("--output")
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "persona_memory_arm_run",
                        "result_status": status,
                    }
                ),
                encoding="utf-8",
            )
            output.chmod(0o600)
            return finalize_code
        return 0

    return run, seen


def test_product_regression_still_post_verifies_cleans_and_finalizes(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=1)
    env = {
        "QA_TEST_ADMIN_TOKEN": "admin-secret",
        "QA_OPENAI_PROVIDER_API_KEY": "provider-secret",
        "CODEX_HOME": str(args.codex_home),
        "QA_CODEX_MODEL": args.judge_model,
    }

    code = supervisor.run_arm(args, env=env, step_runner=runner)

    assert code == 1
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    run_index = scripts.index(("run_persona_memory_regression.py", "run-live"))
    assert scripts[run_index + 1] == ("verify_deployment.py", "--expected-sha")
    assert scripts[run_index + 2] == (
        "prepare_persona_memory_accounts.py",
        "cleanup",
    )
    assert scripts[-1] == ("run_persona_memory_regression.py", "finalize-arm")
    run_env = seen[run_index][1]
    assert "QA_TEST_ADMIN_TOKEN" not in run_env
    assert "QA_OPENAI_PROVIDER_API_KEY" not in run_env
    assert run_env["CODEX_HOME"] == str(args.codex_home)
    assert run_env["QA_CODEX_MODEL"] == args.judge_model
    run_command = seen[run_index][0]
    assert run_command[run_command.index("--codex-home") + 1] == str(args.codex_home)
    assert run_command[run_command.index("--judge-work-root") + 1] == str(
        args.private_root / "codex-judge"
    )
    assert "--judge-api-key-env" not in run_command
    assert (args.private_root / "account-pool.json").exists() is False
    assert (args.private_root / "arm-receipt.json").is_file()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "FAIL"
    assert summary["arm_finalized"] is True


def test_operational_run_failure_still_executes_post_and_cleanup(tmp_path, capsys):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=2)

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("verify_deployment.py", "--expected-sha") in scripts
    assert (
        "prepare_persona_memory_accounts.py",
        "cleanup",
    ) in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") not in scripts
    assert (args.private_root / "account-pool.json").exists() is False
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_blocked_evidence_result_is_cleaned_and_finalized_but_returns_two(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=2, write_blocked_result=True)

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") in scripts
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is True


def test_failed_provisioning_retries_partial_pool_cleanup(tmp_path, capsys):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=0, provision_code=2)

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert scripts == [
        ("provision_profiles.py", "provision-pool"),
        ("provision_profiles.py", "cleanup"),
        ("provision_profiles.py", "cleanup-run"),
    ]
    assert not (args.private_root / "account-pool.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["provision_cleanup_complete"] is True
    assert summary["provision_reaper_pending"] is False


def test_failed_provision_without_manifest_uses_authoritative_run_cleanup(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner(
        [],
        run_code=0,
        provision_code=2,
        write_provision_manifest=False,
    )

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert scripts == [
        ("provision_profiles.py", "provision-pool"),
        ("provision_profiles.py", "cleanup-run"),
    ]
    summary = json.loads(capsys.readouterr().out)
    assert summary["provision_cleanup_complete"] is True
    assert summary["provision_run_cleanup_complete"] is True
    assert summary["provision_reaper_pending"] is False


@pytest.mark.parametrize(
    ("run_code", "result_status"),
    [(0, "FAIL"), (2, "PASS")],
)
def test_run_exit_must_match_private_result_before_finalization(
    tmp_path, capsys, run_code, result_status
):
    args = _args(tmp_path)
    runner, seen = _fake_runner(
        [],
        run_code=run_code,
        write_blocked_result=True,
        result_status=result_status,
    )

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") not in scripts
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_post_verify_launch_error_does_not_bypass_cleanup(tmp_path, capsys):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=0)
    verify_calls = 0

    def fail_run_post(command, *, env):
        nonlocal verify_calls
        if Path(command[1]).name == "verify_deployment.py":
            verify_calls += 1
            if verify_calls == 3:
                raise supervisor.ArmSupervisorError("could not launch post verify")
        return runner(command, env=env)

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=fail_run_post,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    assert not (args.private_root / "account-pool.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_nonzero_finalize_discards_written_arm_receipt(tmp_path, capsys):
    args = _args(tmp_path)
    runner, _seen = _fake_runner([], run_code=0, finalize_code=2)

    code = supervisor.run_arm(
        args,
        env={},
        step_runner=runner,
    )

    assert code == 2
    assert not (args.private_root / "arm-receipt.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


@pytest.mark.parametrize(
    ("run_cleanup_code", "run_cleanup_complete"),
    [(2, True), (0, False)],
)
def test_failed_authoritative_run_sweep_blocks_arm_finalization(
    tmp_path, capsys, run_cleanup_code, run_cleanup_complete
):
    args = _args(tmp_path)
    runner, seen = _fake_runner(
        [],
        run_code=0,
        run_cleanup_code=run_cleanup_code,
        run_cleanup_complete=run_cleanup_complete,
    )

    code = supervisor.run_arm(args, env={}, step_runner=runner)

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("provision_profiles.py", "cleanup-run") in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") not in scripts
    assert not (args.private_root / "arm-receipt.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["run_cleanup_complete"] is False
    assert summary["arm_finalized"] is False


def test_supervisor_rejects_nonempty_private_root_before_subprocess(tmp_path):
    args = _args(tmp_path)
    (args.private_root / "occupied").write_text("do not overwrite", encoding="utf-8")
    calls = []

    def must_not_run(command, *, env):
        calls.append(command)
        return 0

    try:
        supervisor.run_arm(
            args,
            env={},
            step_runner=must_not_run,
        )
    except supervisor.ArmSupervisorError as exc:
        assert "start empty" in str(exc)
    else:
        raise AssertionError("unsafe private root was accepted")
    assert calls == []


def test_supervisor_never_passes_admin_or_provider_keys_to_oauth_judge_runner(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=0)

    assert (
        supervisor.run_arm(
            args,
            env={
                "QA_TEST_ADMIN_TOKEN": "admin-secret",
                "QA_OPENAI_PROVIDER_API_KEY": "provider-secret",
            },
            step_runner=runner,
        )
        == 0
    )
    run_env = next(
        env
        for command, env in seen
        if Path(command[1]).name == "run_persona_memory_regression.py"
        and command[2] == "run-live"
    )
    assert "QA_TEST_ADMIN_TOKEN" not in run_env
    assert "QA_OPENAI_PROVIDER_API_KEY" not in run_env
    assert "QA_EVAL_JUDGE_API_KEY" not in run_env
    capsys.readouterr()


@pytest.mark.parametrize("cleanup_code", (0, 2))
def test_sigterm_during_provision_attempts_bounded_recovery_without_false_claims(
    tmp_path, capsys, cleanup_code
):
    args = _args(tmp_path)
    calls = []

    def interrupting_runner(command, *, env):
        command = list(command)
        calls.append(command)
        script = Path(command[1]).name
        subcommand = command[2]
        if script == "provision_profiles.py" and subcommand == "provision-pool":
            manifest = Path(command[command.index("--manifest") + 1])
            manifest.write_text("partial-pool", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("SIGTERM did not unwind provisioning")
        if script == "provision_profiles.py" and subcommand == "cleanup":
            manifest = Path(command[command.index("--manifest") + 1])
            if cleanup_code == 0:
                manifest.unlink()
            return cleanup_code
        if script == "provision_profiles.py" and subcommand == "cleanup-run":
            run_id = command[command.index("--run-id") + 1]
            receipt = Path(command[command.index("--receipt") + 1])
            receipt.write_text(
                json.dumps(_run_cleanup_receipt(run_id)), encoding="utf-8"
            )
            receipt.chmod(0o600)
            return 0
        raise AssertionError("unexpected command after termination")

    assert supervisor.run_arm(args, env={}, step_runner=interrupting_runner) == 2

    summary = json.loads(capsys.readouterr().out)
    assert summary["termination_signal"] == "SIGTERM"
    assert summary["termination_cleanup_attempted"] is True
    assert summary["termination_cleanup_complete"] is (cleanup_code == 0)
    assert summary["termination_run_cleanup_complete"] is True
    assert summary["provision_reaper_pending"] is False
    assert [Path(command[1]).name for command in calls] == [
        "provision_profiles.py",
        "provision_profiles.py",
        "provision_profiles.py",
    ]


def test_sigint_during_live_run_unwinds_through_full_receipted_cleanup(
    tmp_path, capsys, monkeypatch
):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=0)
    monkeypatch.setattr(
        supervisor,
        "_cleanup_receipt_complete",
        lambda paths, _state: paths.cleanup.is_file(),
    )

    def interrupt_live(command, *, env):
        if (
            Path(command[1]).name == "run_persona_memory_regression.py"
            and command[2] == "run-live"
        ):
            os.kill(os.getpid(), signal.SIGINT)
            raise AssertionError("SIGINT did not unwind live run")
        return runner(command, env=env)

    assert supervisor.run_arm(args, env={}, step_runner=interrupt_live) == 2

    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    summary = json.loads(capsys.readouterr().out)
    assert summary["termination_signal"] == "SIGINT"
    assert summary["termination_cleanup_complete"] is True
    assert summary["provision_reaper_pending"] is False


def test_real_mutating_process_group_is_dead_before_signal_cleanup(tmp_path):
    ready = tmp_path / "child-ready"
    orphan_marker = tmp_path / "orphan-mutation"
    child_code = (
        "import pathlib,sys,time;"
        "time.sleep(0.8);"
        "pathlib.Path(sys.argv[1]).write_text('late mutation',encoding='utf-8')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
        "pathlib.Path(sys.argv[3]).write_text('ready',encoding='utf-8');"
        "time.sleep(30)"
    )
    command = [
        sys.executable,
        "-c",
        parent_code,
        child_code,
        str(orphan_marker),
        str(ready),
    ]

    def terminate_parent():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=terminate_parent, daemon=True)
    sender.start()
    with pytest.raises(supervisor._ArmTermination):
        with supervisor._controlled_termination():
            supervisor._run_step(command, env=os.environ)
    sender.join(timeout=2)
    time.sleep(1)

    assert not sender.is_alive()
    assert ready.is_file()
    assert not orphan_marker.exists()


def test_termination_recovery_resumes_hidden_cleanup_state_and_strictly_verifies(
    tmp_path, capsys, monkeypatch
):
    private = _private_dir(tmp_path, "termination-private")
    paths = supervisor._paths(private)
    pending = private / ".account-pool.json.cleanup-pending"
    pending.write_text("hidden recovery manifest", encoding="utf-8")
    state = supervisor._ArmRunState(
        paths=paths,
        provision_started=True,
        pool_created=True,
        pool_manifest_sha256="a" * 64,
        route_sha256="b" * 64,
        account_fingerprints=("c" * 64,),
        run_id="termination-test",
    )
    observed = []

    def recovery_runner(command, *, env):
        observed.append(list(command))
        script = Path(command[1]).name
        if script == "prepare_persona_memory_accounts.py":
            assert command[2] == "cleanup"
            assert not paths.pool.exists()
            pending.unlink()
            paths.cleanup.write_text("strict receipt", encoding="utf-8")
        else:
            assert script == "provision_profiles.py"
            assert command[2] == "cleanup-run"
            paths.run_cleanup.write_text(
                json.dumps(_run_cleanup_receipt(state.run_id)), encoding="utf-8"
            )
            paths.run_cleanup.chmod(0o600)
        return 0

    monkeypatch.setattr(
        supervisor,
        "_cleanup_receipt_complete",
        lambda candidate_paths, _state: candidate_paths.cleanup.is_file(),
    )

    assert (
        supervisor._termination_result(
            supervisor._ArmTermination(signal.SIGTERM),
            state=state,
            active_env={},
            step_runner=recovery_runner,
        )
        == 2
    )
    assert len(observed) == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["termination_cleanup_attempted"] is True
    assert summary["termination_cleanup_complete"] is True
