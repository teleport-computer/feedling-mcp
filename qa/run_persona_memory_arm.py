#!/usr/bin/env python3
"""Run one formal live persona-memory arm with unconditional cleanup.

This trusted supervisor keeps provisioning/admin credentials out of the
conversation-runner subprocess, preserves a product-regression exit code, and
still performs post-deployment verification, recoverable pool cleanup, and arm
finalization when the live evaluation returns non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.regression.live_accounts import (  # noqa: E402
    LiveAccountContractError,
    load_account_pool,
    read_private_json,
    verify_cleanup_receipt,
)
from qa.provision_profiles import (  # noqa: E402
    ProvisionError,
    SYNTHETIC_CLEANUP_RUN_KIND,
    SYNTHETIC_LABEL_PREFIX,
    normalize_synthetic_run_id,
)


_QA_ROOT = _REPO_ROOT / "qa"
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROVIDER_SECRET_ENVS = {
    "QA_ANTHROPIC_API_KEY",
    "QA_DEEPSEEK_API_KEY",
    "QA_GEMINI_API_KEY",
    "QA_KONGBEIQIE_API_KEY",
    "QA_OPENAI_PROVIDER_API_KEY",
    "QA_OPENROUTER_API_KEY",
}


class ArmSupervisorError(RuntimeError):
    """A bounded local orchestration error safe to print."""


class _ArmTermination(BaseException):
    """First SIGINT/SIGTERM converted into a controlled cleanup unwind."""

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        super().__init__(f"termination signal {self.signum}")


@dataclass(frozen=True, slots=True)
class ArmPaths:
    pool: Path
    import_pre: Path
    import_post: Path
    readiness: Path
    run_pre: Path
    result: Path
    run_post: Path
    cleanup: Path
    run_cleanup: Path
    arm: Path


@dataclass(slots=True)
class _ArmRunState:
    paths: ArmPaths | None = None
    provision_started: bool = False
    pool_created: bool = False
    pool_manifest_sha256: str = ""
    route_sha256: str = ""
    account_fingerprints: tuple[str, ...] = ()
    run_id: str = ""


def _owner_directory(path: Path, *, empty: bool) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ArmSupervisorError("private directories must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise ArmSupervisorError("private directory is unavailable") from None
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ArmSupervisorError("private directory must be owner-controlled mode 0700")
    if empty:
        try:
            if any(resolved.iterdir()):
                raise ArmSupervisorError("private directory must start empty")
        except OSError:
            raise ArmSupervisorError("private directory is unreadable") from None
    return resolved


def _artifact_directory(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ArmSupervisorError("artifact scratch directory must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ArmSupervisorError("artifact scratch directory is unavailable") from None
    if resolved != candidate or not resolved.is_dir():
        raise ArmSupervisorError("artifact scratch directory is invalid")
    return resolved


def _paths(root: Path) -> ArmPaths:
    return ArmPaths(
        pool=root / "account-pool.json",
        import_pre=root / "import-deployment-pre.json",
        import_post=root / "import-deployment-post.json",
        readiness=root / "account-readiness.json",
        run_pre=root / "deployment-pre.json",
        result=root / "result.json",
        run_post=root / "deployment-post.json",
        cleanup=root / "account-cleanup.json",
        run_cleanup=root / "account-run-cleanup.json",
        arm=root / "arm-receipt.json",
    )


@contextmanager
def _controlled_termination():
    """Make the first termination signal unwind; make the second hard-stop.

    Signal handlers can only be installed by Python's main thread.  The formal
    CLI always runs there; direct library callers in worker threads retain the
    caller's signal policy.
    """

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in watched}

    def terminate(signum, _frame):  # noqa: ANN001
        # Switching both signals to their OS defaults before unwinding means a
        # second interrupt cannot be swallowed by cleanup or falsely reported.
        for watched_signum in watched:
            signal.signal(watched_signum, signal.SIG_DFL)
        raise _ArmTermination(int(signum))

    for signum in watched:
        signal.signal(signum, terminate)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_step(command: Sequence[str], *, env: Mapping[str, str]) -> int:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=_REPO_ROOT,
            env=dict(env),
            start_new_session=True,
        )
    except OSError:
        raise ArmSupervisorError("an orchestration subprocess could not start") from None
    try:
        return int(process.wait())
    except BaseException:
        # The child owns account mutation.  It must be dead before the parent
        # starts recovery cleanup or it could recreate users/manifest state
        # after cleanup was reported complete.
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - formal runners are Linux.
                    process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise


def _attempt_step(
    step_runner: Callable[..., int],
    command: Sequence[str],
    *,
    env: Mapping[str, str],
) -> int | None:
    """Keep one subprocess-launch failure from bypassing later cleanup steps."""
    try:
        return int(step_runner(command, env=env))
    except Exception:
        return None


def _private_result_status(path: Path) -> str:
    try:
        document, _digest = read_private_json(
            path,
            label="private experiment result",
            max_bytes=64 * 1024 * 1024,
        )
    except LiveAccountContractError:
        raise ArmSupervisorError("private experiment result is invalid") from None
    status = document.get("status")
    if status not in {"PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR"}:
        raise ArmSupervisorError("private experiment result status is invalid")
    return str(status)


def _private_arm_status(path: Path) -> str:
    try:
        document, _digest = read_private_json(path, label="arm run receipt")
    except LiveAccountContractError:
        raise ArmSupervisorError("arm run receipt is invalid") from None
    status = document.get("result_status")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "persona_memory_arm_run"
        or status not in {"PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR"}
    ):
        raise ArmSupervisorError("arm run receipt status is invalid")
    return str(status)


def _discard_arm_receipt(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _result_exit_code(status: str) -> int:
    if status == "PASS":
        return 0
    if status == "FAIL":
        return 1
    return 2


def _script(name: str, *arguments: str) -> list[str]:
    return [sys.executable, str(_QA_ROOT / name), *arguments]


def _runner_environment(env: Mapping[str, str]) -> dict[str, str]:
    sanitized = dict(env)
    sanitized.pop("QA_TEST_ADMIN_TOKEN", None)
    for name in _PROVIDER_SECRET_ENVS:
        sanitized.pop(name, None)
    return sanitized


def _run_arm_impl(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] | None = None,
    step_runner=_run_step,
    run_state: _ArmRunState,
) -> int:
    active_env = dict(os.environ if env is None else env)
    try:
        run_id = normalize_synthetic_run_id(active_env.get("QA_RUN_ID") or "local")
    except ProvisionError:
        raise ArmSupervisorError("QA run ID is invalid") from None
    active_env["QA_RUN_ID"] = run_id
    run_state.run_id = run_id
    if _BUILD_SHA_RE.fullmatch(str(args.build_sha or "")) is None:
        raise ArmSupervisorError("build SHA must be a full lowercase digest")
    if not args.allow_private_judge_egress:
        raise ArmSupervisorError("semantic judge plaintext egress was not authorized")
    private_root = _owner_directory(args.private_root, empty=True)
    work_dir = _owner_directory(args.work_dir, empty=True)
    artifact_dir = _artifact_directory(args.artifact_dir)
    if private_root == work_dir:
        raise ArmSupervisorError("private result and import work directories must differ")
    try:
        if any(
            os.path.commonpath([str(path), str(artifact_dir)]) == str(artifact_dir)
            for path in (private_root, work_dir)
        ):
            raise ArmSupervisorError(
                "private result and work directories must be outside artifacts"
            )
    except ValueError:
        raise ArmSupervisorError("private/artifact path boundary is invalid") from None
    paths = _paths(private_root)
    run_state.paths = paths
    target_id = args.target_id or f"{args.target_label}-{args.build_sha}"
    account_count = args.repetitions * 8
    common_verify = [
        "--expected-sha",
        args.build_sha,
        "--expected-runtime",
        "hosted_resident",
    ]

    pool_created = False
    preparation_started = False
    run_code = 2
    result_status: str | None = None
    run_exit_consistent = False
    arm_finalized = False
    operational_failure = False

    provision = _script(
        "provision_profiles.py",
        "provision-pool",
        "--profile",
        args.profile,
        "--count",
        str(account_count),
        "--require-runtime-v2",
        "--manifest",
        str(paths.pool),
    )
    run_state.provision_started = True
    provision_code = _attempt_step(step_runner, provision, env=active_env)
    if provision_code != 0 or not paths.pool.is_file():
        manifest_recovery_ok = not paths.pool.exists()
        if paths.pool.exists():
            recover_partial_pool = _script(
                "provision_profiles.py",
                "cleanup",
                "--manifest",
                str(paths.pool),
            )
            manifest_recovery_ok = (
                _attempt_step(step_runner, recover_partial_pool, env=active_env) == 0
                and not paths.pool.exists()
            )
        run_cleanup_ok = _attempt_run_cleanup(
            paths,
            run_id,
            active_env=active_env,
            step_runner=step_runner,
        )
        recovery_ok = manifest_recovery_ok and run_cleanup_ok
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "BLOCKED",
                    "run_exit_code": 2,
                    "arm_finalized": False,
                    "provision_cleanup_complete": recovery_ok,
                    "provision_run_cleanup_complete": run_cleanup_ok,
                    "provision_reaper_pending": not run_cleanup_ok,
                    "private_root": str(private_root),
                    "result": str(paths.result),
                    "arm_receipt": str(paths.arm),
                },
                sort_keys=True,
            )
        )
        return 2
    pool_created = True
    run_state.pool_created = True
    try:
        bound_pool = load_account_pool(paths.pool, allow_expired_lease=True)
    except LiveAccountContractError:
        pass
    else:
        run_state.pool_manifest_sha256 = bound_pool.manifest_sha256
        run_state.route_sha256 = bound_pool.route_sha256
        run_state.account_fingerprints = tuple(bound_pool.account_fingerprints)

    try:
        import_pre = _script(
            "verify_deployment.py",
            *common_verify,
            "--receipt",
            str(paths.import_pre),
        )
        if (
            _attempt_step(step_runner, import_pre, env=active_env) != 0
            or not paths.import_pre.is_file()
        ):
            operational_failure = True
        else:
            preparation_started = True
            prepare = _script(
                "prepare_persona_memory_accounts.py",
                "prepare",
                "--account-pool",
                str(paths.pool),
                "--build-sha",
                args.build_sha,
                "--deployment-receipt",
                str(paths.import_pre),
                "--post-deployment-receipt",
                str(paths.import_post),
                "--work-dir",
                str(work_dir),
                "--artifact-dir",
                str(artifact_dir),
                "--readiness-receipt",
                str(paths.readiness),
                "--concurrency",
                str(args.concurrency),
            )
            if (
                _attempt_step(step_runner, prepare, env=active_env) != 0
                or not paths.readiness.is_file()
                or not paths.import_post.is_file()
                or not paths.pool.is_file()
            ):
                operational_failure = True

        if not operational_failure:
            run_pre = _script(
                "verify_deployment.py",
                *common_verify,
                "--receipt",
                str(paths.run_pre),
            )
            if (
                _attempt_step(step_runner, run_pre, env=active_env) != 0
                or not paths.run_pre.is_file()
            ):
                operational_failure = True

        if not operational_failure:
            run = _script(
                "run_persona_memory_regression.py",
                "run-live",
                "--target-id",
                target_id,
                "--target-label",
                args.target_label,
                "--build-sha",
                args.build_sha,
                "--deployment-receipt",
                str(paths.run_pre),
                "--account-pool",
                str(paths.pool),
                "--readiness-receipt",
                str(paths.readiness),
                "--external-cleanup-guaranteed",
                "--repetitions",
                str(args.repetitions),
                "--concurrency",
                str(args.concurrency),
                "--codex-bin",
                str(args.codex_bin),
                "--codex-home",
                str(args.codex_home),
                "--judge-work-root",
                str(private_root / "codex-judge"),
                "--judge-model",
                args.judge_model,
                "--judge-id",
                args.judge_id,
                "--judge-configuration-id",
                args.judge_configuration_id,
                "--judge-codex-profile",
                args.judge_codex_profile,
                "--judge-permission-profile",
                args.judge_permission_profile,
                "--judge-reasoning-effort",
                args.judge_reasoning_effort,
                "--judge-timeout",
                str(args.judge_timeout),
                "--allow-private-judge-egress",
                "--output",
                str(paths.result),
            )
            observed_run_code = _attempt_step(
                step_runner, run, env=_runner_environment(active_env)
            )
            run_code = observed_run_code if observed_run_code is not None else 2
            if not paths.result.is_file():
                operational_failure = True
            else:
                try:
                    result_status = _private_result_status(paths.result)
                except ArmSupervisorError:
                    operational_failure = True
                else:
                    run_exit_consistent = (
                        run_code in {0, 1, 2}
                        and _result_exit_code(result_status) == run_code
                    )
                    if not run_exit_consistent or run_code == 2:
                        operational_failure = True
    finally:
        post_ok = False
        cleanup_ok = False
        run_cleanup_ok = False
        if pool_created and preparation_started:
            run_post = _script(
                "verify_deployment.py",
                *common_verify,
                "--receipt",
                str(paths.run_post),
            )
            post_ok = (
                _attempt_step(step_runner, run_post, env=active_env) == 0
                and paths.run_post.is_file()
            )
            if not post_ok:
                operational_failure = True

        if pool_created and paths.pool.exists():
            cleanup = _script(
                "prepare_persona_memory_accounts.py",
                "cleanup",
                "--account-pool",
                str(paths.pool),
                "--receipt",
                str(paths.cleanup),
            )
            cleanup_ok = (
                _attempt_step(step_runner, cleanup, env=active_env) == 0
                and paths.cleanup.is_file()
                and not paths.pool.exists()
            )
            if not cleanup_ok:
                operational_failure = True
        elif paths.cleanup.is_file():
            cleanup_ok = True
        elif pool_created:
            operational_failure = True

        run_cleanup_ok = _attempt_run_cleanup(
            paths,
            run_id,
            active_env=active_env,
            step_runner=step_runner,
        )
        if not run_cleanup_ok:
            operational_failure = True

        required = (
            paths.result,
            paths.readiness,
            paths.import_pre,
            paths.import_post,
            paths.run_pre,
            paths.run_post,
            paths.cleanup,
            paths.run_cleanup,
        )
        if (
            run_exit_consistent
            and post_ok
            and cleanup_ok
            and run_cleanup_ok
            and all(path.is_file() for path in required)
        ):
            finalize = _script(
                "run_persona_memory_regression.py",
                "finalize-arm",
                "--result",
                str(paths.result),
                "--readiness-receipt",
                str(paths.readiness),
                "--import-pre-deployment-receipt",
                str(paths.import_pre),
                "--import-post-deployment-receipt",
                str(paths.import_post),
                "--pre-deployment-receipt",
                str(paths.run_pre),
                "--post-deployment-receipt",
                str(paths.run_post),
                "--cleanup-receipt",
                str(paths.cleanup),
                "--output",
                str(paths.arm),
            )
            finalize_code = _attempt_step(step_runner, finalize, env=active_env)
            if finalize_code != 0 or not paths.arm.is_file():
                operational_failure = True
                _discard_arm_receipt(paths.arm)
            else:
                try:
                    arm_status = _private_arm_status(paths.arm)
                except ArmSupervisorError:
                    operational_failure = True
                    _discard_arm_receipt(paths.arm)
                else:
                    arm_finalized = arm_status == result_status
                    if not arm_finalized:
                        operational_failure = True
                        _discard_arm_receipt(paths.arm)
        elif paths.result.exists():
            operational_failure = True

    status = "BLOCKED" if operational_failure else ("PASS" if run_code == 0 else "FAIL")
    print(
        json.dumps(
            {
                "ok": status == "PASS",
                "status": status,
                "run_exit_code": run_code,
                "arm_finalized": arm_finalized,
                "run_cleanup_complete": run_cleanup_ok,
                "private_root": str(private_root),
                "result": str(paths.result),
                "arm_receipt": str(paths.arm),
            },
            sort_keys=True,
        )
    )
    return 2 if operational_failure else run_code


def _cleanup_receipt_complete(paths: ArmPaths, state: _ArmRunState) -> bool:
    if (
        not state.pool_manifest_sha256
        or not state.route_sha256
        or not state.account_fingerprints
    ):
        return False
    try:
        verify_cleanup_receipt(
            paths.cleanup,
            expected_pool_manifest_sha256=state.pool_manifest_sha256,
            expected_route_sha256=state.route_sha256,
            expected_account_fingerprints=state.account_fingerprints,
        )
    except LiveAccountContractError:
        return False
    return True


def _run_cleanup_receipt_complete(path: Path, run_id: str) -> bool:
    try:
        document, _digest = read_private_json(
            path,
            label="authoritative run cleanup receipt",
            max_bytes=64 * 1024,
        )
    except LiveAccountContractError:
        return False
    count_keys = {
        "matched_count",
        "deleted_count",
        "already_absent_count",
        "operation_failure_count",
        "remaining_count",
    }
    expected_keys = {
        "schema_version",
        "kind",
        "run_id_sha256",
        "label_prefix_sha256",
        "database_authoritative",
        *count_keys,
        "complete",
    }
    expected_run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    expected_label_hash = hashlib.sha256(
        f"{SYNTHETIC_LABEL_PREFIX}{run_id}-".encode("utf-8")
    ).hexdigest()
    return bool(
        set(document) == expected_keys
        and document.get("schema_version") == 1
        and document.get("kind") == SYNTHETIC_CLEANUP_RUN_KIND
        and document.get("run_id_sha256") == expected_run_hash
        and document.get("label_prefix_sha256") == expected_label_hash
        and document.get("database_authoritative") is True
        and all(
            type(document.get(key)) is int and document[key] >= 0
            for key in count_keys
        )
        and document.get("matched_count")
        == document.get("deleted_count")
        + document.get("already_absent_count")
        + document.get("operation_failure_count")
        and document.get("operation_failure_count") == 0
        and document.get("remaining_count") == 0
        and document.get("complete") is True
    )


def _attempt_run_cleanup(
    paths: ArmPaths,
    run_id: str,
    *,
    active_env: Mapping[str, str],
    step_runner: Callable[..., int],
) -> bool:
    if _run_cleanup_receipt_complete(paths.run_cleanup, run_id):
        return True
    if paths.run_cleanup.exists() or paths.run_cleanup.is_symlink():
        return False
    command = _script(
        "provision_profiles.py",
        "cleanup-run",
        "--run-id",
        run_id,
        "--receipt",
        str(paths.run_cleanup),
    )
    return bool(
        _attempt_step(step_runner, command, env=active_env) == 0
        and _run_cleanup_receipt_complete(paths.run_cleanup, run_id)
    )


def _termination_result(
    termination: _ArmTermination,
    *,
    state: _ArmRunState,
    active_env: Mapping[str, str],
    step_runner: Callable[..., int],
) -> int:
    paths = state.paths
    cleanup_attempted = False
    manifest_cleanup_complete = False
    run_cleanup_complete = False
    if paths is not None and state.provision_started:
        if state.pool_created:
            manifest_cleanup_complete = _cleanup_receipt_complete(paths, state)
            if not manifest_cleanup_complete:
                # Always call the recovery-aware cleanup command.  It resumes
                # the hidden pending/outcome journal even after the public pool
                # path has already been renamed away.
                cleanup_attempted = True
                cleanup = _script(
                    "prepare_persona_memory_accounts.py",
                    "cleanup",
                    "--account-pool",
                    str(paths.pool),
                    "--receipt",
                    str(paths.cleanup),
                )
                _attempt_step(step_runner, cleanup, env=active_env)
                manifest_cleanup_complete = _cleanup_receipt_complete(paths, state)
        elif paths.pool.exists():
            cleanup_attempted = True
            cleanup = _script(
                "provision_profiles.py",
                "cleanup",
                "--manifest",
                str(paths.pool),
            )
            manifest_cleanup_complete = (
                _attempt_step(step_runner, cleanup, env=active_env) == 0
                and not paths.pool.exists()
            )
        else:
            manifest_cleanup_complete = True
        if not _run_cleanup_receipt_complete(paths.run_cleanup, state.run_id):
            cleanup_attempted = True
        run_cleanup_complete = _attempt_run_cleanup(
            paths,
            state.run_id,
            active_env=active_env,
            step_runner=step_runner,
        )
    cleanup_complete = manifest_cleanup_complete and run_cleanup_complete
    try:
        signal_name = signal.Signals(termination.signum).name
    except ValueError:
        signal_name = "UNKNOWN"
    print(
        json.dumps(
            {
                "ok": False,
                "status": "BLOCKED",
                "run_exit_code": 2,
                "arm_finalized": False,
                "termination_signal": signal_name,
                "termination_cleanup_attempted": cleanup_attempted,
                "termination_cleanup_complete": cleanup_complete,
                "termination_run_cleanup_complete": run_cleanup_complete,
                "provision_reaper_pending": state.provision_started
                and not run_cleanup_complete,
            },
            sort_keys=True,
        )
    )
    return 2


def run_arm(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] | None = None,
    step_runner=_run_step,
) -> int:
    active_env = dict(os.environ if env is None else env)
    state = _ArmRunState()
    with _controlled_termination():
        try:
            return _run_arm_impl(
                args,
                env=active_env,
                step_runner=step_runner,
                run_state=state,
            )
        except _ArmTermination as termination:
            return _termination_result(
                termination,
                state=state,
                active_env=active_env,
                step_runner=step_runner,
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-label", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--target-id", default="")
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--profile", default="official-openai")
    parser.add_argument("--repetitions", type=int, choices=(1, 3), default=3)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--judge-id", required=True)
    parser.add_argument("--judge-configuration-id", required=True)
    parser.add_argument("--judge-codex-profile", default="persona_memory_judge")
    parser.add_argument(
        "--judge-permission-profile",
        default="feedling-e2e-persona-memory-judge",
    )
    parser.add_argument(
        "--judge-reasoning-effort",
        choices=("low", "medium", "high", "xhigh"),
        default="medium",
    )
    parser.add_argument("--judge-timeout", type=float, default=180.0)
    parser.add_argument(
        "--allow-private-judge-egress",
        action="store_true",
        required=True,
        help="authorize plaintext trajectory egress to the configured semantic judge",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.concurrency < 1:
            raise ArmSupervisorError("concurrency must be positive")
        return run_arm(args)
    except ArmSupervisorError as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
