#!/usr/bin/env python3
"""Validate, run, and compare Feedling persona/memory regression experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.atomic_private_file import AtomicPrivateFileError, create_private_file  # noqa: E402
from qa.live_scenario_probe import (  # noqa: E402
    LOCKED_BASE_URL,
    LiveScenarioProbeError,
    load_profile,
)
from qa.regression.compare import ComparisonError, compare_results  # noqa: E402
from qa.regression.contracts import (  # noqa: E402
    ContractError,
    ExperimentResult,
    ExperimentTarget,
    canonical_json_sha256,
)
from qa.regression.engine import run_experiment, validate_suite_contract  # noqa: E402
from qa.regression.feedling_rotation import (  # noqa: E402
    RotationEvidenceError,
    prove_codex_runtime_session_rotation,
)
from qa.regression.codex_judge import (  # noqa: E402
    DEFAULT_CODEX_PROFILE,
    DEFAULT_PERMISSION_PROFILE,
    CodexExecJudge,
)
from qa.regression.judge import JudgeError  # noqa: E402
from qa.regression.live_accounts import (  # noqa: E402
    LiveAccountContractError,
    load_account_pool,
    read_private_json,
    verify_cleanup_receipt,
    verify_readiness_receipt,
)
from qa.regression.report import ReportError, write_reports  # noqa: E402
from qa.regression.scenario_loader import (  # noqa: E402
    LoaderError,
    RegressionSuite,
    load_suite,
    load_suite_directory,
    load_verified_source_fixture,
    read_contract_json,
)
from qa.regression.target import (  # noqa: E402
    BLOCKED_EVIDENCE,
    FeedlingTarget,
    TargetContext,
    TargetError,
)


DEFAULT_PERSONA = Path("qa/regression/fixtures/golden-persona-mira-v1.json")
DEFAULT_SCENARIO_ROOT = Path("qa/regression/scenarios")
DEFAULT_SOURCE_FIXTURE = Path("qa/fixtures/persona-import-v1.json")
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROTECTED_DEBUG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")
_ARM_RECEIPT_KIND = "persona_memory_arm_run"
_ARM_RECEIPT_KEYS = {
    "schema_version",
    "kind",
    "created_at",
    "target_id",
    "target_label",
    "build_sha",
    "runtime_mode",
    "result_status",
    "result_sha256",
    "persona_fixture_sha256",
    "source_bundle_sha256",
    "import_fixture_sha256",
    "pool_manifest_sha256",
    "route_sha256",
    "account_fingerprints",
    "readiness_receipt_sha256",
    "import_deployment_receipt_pre_sha256",
    "import_deployment_receipt_post_sha256",
    "deployment_receipt_pre_sha256",
    "deployment_receipt_post_sha256",
    "cleanup_receipt_sha256",
    "import_started_at",
    "import_finished_at",
    "import_pre_verified_at",
    "import_post_verified_at",
    "pre_verified_at",
    "post_verified_at",
    "cleanup_verified_at",
    "deployment_bracket_verified",
    "cleanup_verified",
}


class CommandError(RuntimeError):
    """A bounded operator error that contains no account credentials."""


def _gate_exit_code(status: str) -> int:
    """Map product outcomes separately from unusable evaluation evidence."""
    if status == "PASS":
        return 0
    if status == "FAIL":
        return 1
    if status in {"BLOCKED_EVIDENCE", "INFRA_ERROR"}:
        return 2
    raise CommandError("evaluation produced an unknown status")


class _SessionPool:
    def __init__(self, rows: list[tuple[dict[str, Any], Any]]) -> None:
        self._rows = list(rows)
        self._lock = threading.Lock()

    def checkout(self, _context: TargetContext) -> Any:
        with self._lock:
            if not self._rows:
                raise CommandError("isolated session pool is exhausted")
            _profile, session = self._rows.pop(0)
            return session


def _private_output_parent(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if candidate.exists() or candidate.is_symlink():
        raise CommandError("private output path must not already exist")
    parent = candidate.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = parent.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise CommandError("private output parent is unavailable") from None
    if (
        resolved != parent
        or metadata.st_uid != os.geteuid()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CommandError("private output parent must be owner-controlled mode 0700")
    return resolved / candidate.name


def _manifest_spec(value: str) -> tuple[str, Path]:
    profile_id, separator, raw_path = value.partition("=")
    if not separator or not profile_id or not raw_path:
        raise argparse.ArgumentTypeError("expected PROFILE_ID=/absolute/manifest.json")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("profile manifest path must be absolute")
    return profile_id, path


def _load_manifest_sessions(
    specs: Sequence[tuple[str, Path]],
) -> list[tuple[dict[str, Any], Any]]:
    rows = [load_profile(path, profile_id) for profile_id, path in specs]
    users = [session.user_id for _profile, session in rows]
    if len(users) != len(set(users)):
        raise CommandError("profile manifests reuse a synthetic account")
    routes = {
        (
            str(profile.get("provider") or ""),
            str(profile.get("configured_model") or ""),
            str(profile.get("configured_base_url") or ""),
            str(profile.get("runtime_mode") or ""),
            str(profile.get("runtime_version") or ""),
            str(profile.get("reasoning_effort") or ""),
            str(profile.get("trace_enabled") or ""),
        )
        for profile, _session in rows
    }
    if len(routes) != 1 or any(not value for value in next(iter(routes), ())):
        raise CommandError("profile manifests must use one fully identified target route")
    return rows


def _deployment_receipt(
    path: Path,
    *,
    expected_sha: str,
    expected_runtime: str,
    reference_time: datetime | None = None,
    max_age_seconds: float = 1800.0,
) -> tuple[dict[str, Any], str]:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise CommandError("deployment receipt path must be an absolute regular file")
    try:
        metadata = candidate.stat(follow_symlinks=False)
    except OSError:
        raise CommandError("deployment receipt is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or metadata.st_nlink != 1
    ):
        raise CommandError("deployment receipt is not owner-controlled")
    receipt = read_contract_json(candidate, max_bytes=64 * 1024)
    if not isinstance(receipt, dict):
        raise CommandError("deployment receipt is invalid")
    sha = expected_sha.lower()
    worker_sha = receipt.get("observed_worker_sha")
    expected_runtime_evidence_source = (
        "per_profile_runtime_readback_and_live_scenarios"
        if expected_runtime == "hosted_resident"
        else "deployed_runtime_readback"
    )
    try:
        verified_at = datetime.fromisoformat(
            str(receipt.get("verified_at") or "").replace("Z", "+00:00")
        )
        reference = reference_time or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            raise ValueError
        receipt_age = (
            reference.astimezone(timezone.utc) - verified_at.astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError):
        receipt_age = -1
    if (
        _BUILD_SHA_RE.fullmatch(sha) is None
        or receipt.get("schema_version") != 1
        or receipt.get("environment") != "test"
        or receipt.get("base_url") != LOCKED_BASE_URL
        or receipt.get("expected_runtime") != expected_runtime
        or receipt.get("expected_deployment_sha") != sha
        or receipt.get("observed_backend_sha") != sha
        or receipt.get("observed_deployment_sha") != sha
        or worker_sha is not None
        or receipt.get("live_worker_count") is not None
        or receipt.get("runtime_evidence_source")
        != expected_runtime_evidence_source
        or receipt.get("liveness_verified") is not True
        or receipt.get("deployment_identity_verified") is not True
        or not 0 <= receipt_age <= max_age_seconds
    ):
        raise CommandError("deployment receipt does not prove the requested build")
    return receipt, canonical_json_sha256(receipt)


def _required_sessions(suite: RegressionSuite, repetitions: int) -> int:
    per_repeat = 0
    for scenario in suite.scenarios:
        per_repeat += len({turn.session_key for turn in scenario.turns})
    return per_repeat * repetitions


def formal_sessions_per_repetition() -> int:
    """Return the checked-in formal suite's exact isolated-account count."""

    suite = load_suite_directory(
        _REPO_ROOT / DEFAULT_PERSONA,
        _REPO_ROOT / DEFAULT_SCENARIO_ROOT,
    )
    validate_suite_contract(suite)
    return _required_sessions(suite, 1)


def _selected_suite(args: argparse.Namespace) -> RegressionSuite:
    if args.scenario:
        suite = load_suite(args.persona, list(args.scenario))
    else:
        all_scenarios = load_suite_directory(args.persona, args.scenario_root)
        selected = tuple(
            scenario
            for scenario in all_scenarios.scenarios
            if args.include_nightly or scenario.metadata.get("lane") == "release"
        )
        if not selected:
            raise CommandError("no scenarios selected")
        suite = RegressionSuite(persona=all_scenarios.persona, scenarios=selected)
    validate_suite_contract(suite)
    return suite


def _judge(args: argparse.Namespace) -> CodexExecJudge | None:
    codex_fields = (
        args.codex_bin,
        args.codex_home,
        args.judge_work_root,
        args.judge_model,
        args.judge_id,
        args.judge_configuration_id,
    )
    if not any(codex_fields):
        return None
    if not all(codex_fields):
        raise CommandError(
            "Codex judge requires executable, CODEX_HOME, work root, model, judge id, and configuration id"
        )
    if not args.allow_private_judge_egress:
        raise CommandError(
            "Codex judge requires --allow-private-judge-egress because requests contain plaintext trajectories"
        )
    return CodexExecJudge(
        judge_id=args.judge_id,
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
        work_root=args.judge_work_root,
        model=args.judge_model,
        reasoning_effort=args.judge_reasoning_effort,
        timeout_seconds=args.judge_timeout,
        codex_profile=args.judge_codex_profile,
        permission_profile=args.judge_permission_profile,
        configuration_id=args.judge_configuration_id,
    )


def _cmd_validate(args: argparse.Namespace) -> int:
    suite = _selected_suite(args)
    _fixture, import_fixture_sha256 = load_verified_source_fixture(
        suite.persona, args.source_fixture
    )
    print(
        json.dumps(
            {
                "ok": True,
                "persona_id": suite.persona.persona_id,
                "persona_version": suite.persona.persona_version,
                "persona_fixture_sha256": suite.persona.fixture_sha256,
                "rubric_sha256": suite.rubric_sha256,
                "evaluation_contract_sha256": suite.evaluation_contract_sha256,
                "import_fixture_sha256": import_fixture_sha256,
                "scenario_fingerprints": suite.scenario_fingerprints,
                "sessions_per_repetition": _required_sessions(suite, 1),
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    suite = _selected_suite(args)
    _fixture, import_fixture_sha256 = load_verified_source_fixture(
        suite.persona, args.source_fixture
    )
    output = _private_output_parent(args.output)
    strong_rotation_required = any(
        "persistent_memory_strong" in item.requirements for item in suite.scenarios
    )
    semantic_judge = _judge(args)
    required = _required_sessions(suite, args.repetitions)
    live_metadata: dict[str, Any]
    deployment_runtime: str | None = None
    if args.account_pool is not None:
        if args.readiness_receipt is None:
            raise CommandError("--readiness-receipt is required with --account-pool")
        if args.accounts_ready:
            raise CommandError(
                "--accounts-ready cannot replace a readiness receipt for an account pool"
            )
        if not args.external_cleanup_guaranteed or args.cleanup_account:
            raise CommandError(
                "account pools require --external-cleanup-guaranteed and trusted batch cleanup"
            )
        account_pool = load_account_pool(args.account_pool)
        deployment_runtime = account_pool.deployment_runtime
        if deployment_runtime != "hosted_resident":
            raise CommandError(
                "formal account-pool runs require per-account Hosted Runtime V2 readback"
            )
        sessions = list(account_pool.rows)
        if len(sessions) != required:
            raise CommandError(
                f"account pool size must exactly match the run: required={required} available={len(sessions)}"
            )
        _readiness, readiness_sha256 = verify_readiness_receipt(
            args.readiness_receipt,
            pool=account_pool,
            expected_build_sha=args.build_sha,
            expected_persona_fixture_sha256=suite.persona.fixture_sha256,
            expected_source_bundle_sha256=suite.persona.source_fixture_sha256,
            expected_import_fixture_sha256=import_fixture_sha256,
        )
        account_fingerprints = list(account_pool.account_fingerprints)
        live_metadata = {
            "account_readiness": "receipt_verified",
            "account_pool_manifest_sha256": account_pool.manifest_sha256,
            "account_readiness_receipt_sha256": readiness_sha256,
            "account_route_sha256": account_pool.route_sha256,
            "import_deployment_receipt_pre_sha256": _readiness[
                "deployment_receipt_pre_sha256"
            ],
            "import_deployment_receipt_post_sha256": _readiness[
                "deployment_receipt_post_sha256"
            ],
            "import_fixture_sha256": import_fixture_sha256,
        }
    else:
        if args.readiness_receipt is not None:
            raise CommandError(
                "--readiness-receipt requires the strict --account-pool path"
            )
        if not args.accounts_ready:
            raise CommandError("--accounts-ready is required for legacy live mutation")
        sessions = _load_manifest_sessions(args.manifest_profile)
        if len(sessions) < required:
            raise CommandError(
                f"isolated session pool is too small: required={required} available={len(sessions)}"
            )
        if args.cleanup_account and len(sessions) != required:
            raise CommandError(
                "--cleanup-account requires exactly the accounts consumed by this run"
            )
        account_fingerprints = sorted(
            hashlib.sha256(str(session.user_id).encode("utf-8")).hexdigest()
            for _profile, session in sessions[:required]
        )
        live_metadata = {"account_readiness": "operator_attested"}
    first_profile = sessions[0][0]
    observed_account_runtime = str(first_profile.get("runtime_mode") or "")
    expected_runtime = deployment_runtime or observed_account_runtime
    if strong_rotation_required and (
        expected_runtime != "hosted_resident"
        or first_profile.get("provider") != "openai"
        or first_profile.get("configured_base_url") != "https://api.openai.com/v1"
        or first_profile.get("trace_enabled") is not True
    ):
        raise CommandError(
            "strong runtime rotation requires a verbose-traced official OpenAI "
            "Hosted Runtime V2 account pool"
        )
    _receipt, receipt_sha256 = _deployment_receipt(
        args.deployment_receipt,
        expected_sha=args.build_sha,
        expected_runtime=expected_runtime,
    )
    pool = _SessionPool(sessions)

    def cleanup(client: Any, session: Any) -> None:
        client.reset_account(session)

    def rotate_runtime_session(
        client: Any,
        session: Any,
        prior_turn: dict[str, str],
    ) -> dict[str, Any]:
        try:
            return dict(
                prove_codex_runtime_session_rotation(client, session, prior_turn)
            )
        except RotationEvidenceError as exc:
            raise TargetError(
                "SESSION_BOUNDARY_UNPROVEN",
                status=BLOCKED_EVIDENCE,
                detail=(
                    "Exact Codex runtime-session rotation evidence was unavailable "
                    f"({exc.code})"
                ),
                protected_debug_identifiers=exc.protected_debug_identifiers,
            ) from None

    target_adapter = FeedlingTarget(
        target_id=args.target_id,
        base_url=LOCKED_BASE_URL,
        session_factory=pool.checkout,
        runtime_session_rotator=(
            rotate_runtime_session if strong_rotation_required else None
        ),
        session_closer=cleanup if args.cleanup_account else None,
        external_cleanup_guaranteed=args.external_cleanup_guaranteed,
    )
    target = ExperimentTarget(
        target_id=args.target_id,
        label=args.target_label,
        base_url=LOCKED_BASE_URL,
        build_sha=args.build_sha,
        runtime_mode=expected_runtime or "unknown",
        provider=str(first_profile.get("provider") or "unknown"),
        model=str(first_profile.get("configured_model") or "unknown"),
        configuration={
            "configured_base_url": str(
                first_profile.get("configured_base_url") or ""
            ),
            "reasoning_effort": str(first_profile.get("reasoning_effort") or ""),
            "runtime_version": first_profile.get("runtime_version"),
            "observed_account_runtime_mode": observed_account_runtime,
            "trace_enabled": first_profile.get("trace_enabled") is True,
        },
    )
    experiment_id = args.experiment_id or f"persona-memory-{uuid.uuid4().hex}"
    protected_debug_accounts = []
    for _profile, session in sessions[:required]:
        user_id = str(session.user_id)
        if _PROTECTED_DEBUG_ID_RE.fullmatch(user_id) is None:
            raise CommandError("synthetic account identifier is invalid")
        protected_debug_accounts.append(
            {
                "account_fingerprint": hashlib.sha256(
                    user_id.encode("utf-8")
                ).hexdigest(),
                "user_id": user_id,
            }
        )
    if (
        len(protected_debug_accounts) != len(account_fingerprints)
        or len({row["user_id"] for row in protected_debug_accounts})
        != len(protected_debug_accounts)
        or sorted(row["account_fingerprint"] for row in protected_debug_accounts)
        != account_fingerprints
    ):
        raise CommandError("synthetic debug account binding is invalid")
    result = run_experiment(
        suite=suite,
        target_adapter=target_adapter,
        target=target,
        experiment_id=experiment_id,
        repetitions=args.repetitions,
        concurrency=args.concurrency,
        judge=semantic_judge,
        metadata={
            "deployment_receipt_sha256": receipt_sha256,
            "account_fingerprints": account_fingerprints,
            "protected_debug_accounts": protected_debug_accounts,
            "source_bundle_sha256": suite.persona.source_fixture_sha256,
            **live_metadata,
        },
    )
    content = (
        json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    create_private_file(output, content)
    print(
        json.dumps(
            {
                "ok": result.status == "PASS",
                "status": result.status,
                "experiment_id": result.experiment_id,
                "trajectory_count": len(result.trajectories),
                "metric_count": len(result.metric_results),
                "private_result": str(output),
            },
            sort_keys=True,
        )
    )
    return _gate_exit_code(result.status)


def _as_utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        raise CommandError(f"{label} timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise CommandError(f"{label} timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _single_target(result: ExperimentResult) -> ExperimentTarget:
    if len(result.targets) != 1:
        raise CommandError("a finalized arm must contain exactly one target")
    return result.targets[0]


def _load_private_result(path: Path) -> tuple[ExperimentResult, str]:
    document, digest = read_private_json(
        path, label="private experiment result", max_bytes=64 * 1024 * 1024
    )
    try:
        result = ExperimentResult.from_dict(document)
    except ContractError:
        raise CommandError("private experiment result contract is invalid") from None
    return result, digest


def _cmd_finalize_arm(args: argparse.Namespace) -> int:
    output = _private_output_parent(args.output)
    result, result_sha256 = _load_private_result(args.result)
    target = _single_target(result)
    metadata = result.metadata
    account_fingerprints = metadata.get("account_fingerprints")
    pool_sha256 = metadata.get("account_pool_manifest_sha256")
    route_sha256 = metadata.get("account_route_sha256")
    readiness_expected_sha256 = metadata.get("account_readiness_receipt_sha256")
    source_bundle_sha256 = metadata.get("source_bundle_sha256")
    import_fixture_sha256 = metadata.get("import_fixture_sha256")
    import_pre_expected_sha256 = metadata.get(
        "import_deployment_receipt_pre_sha256"
    )
    import_post_expected_sha256 = metadata.get(
        "import_deployment_receipt_post_sha256"
    )
    if (
        metadata.get("account_readiness") != "receipt_verified"
        or not isinstance(account_fingerprints, list)
        or not account_fingerprints
        or account_fingerprints != sorted(account_fingerprints)
        or len(account_fingerprints) != len(set(account_fingerprints))
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in account_fingerprints
        )
        or not isinstance(pool_sha256, str)
        or _SHA256_RE.fullmatch(pool_sha256) is None
        or not isinstance(route_sha256, str)
        or _SHA256_RE.fullmatch(route_sha256) is None
        or not isinstance(readiness_expected_sha256, str)
        or _SHA256_RE.fullmatch(readiness_expected_sha256) is None
        or not isinstance(source_bundle_sha256, str)
        or _SHA256_RE.fullmatch(source_bundle_sha256) is None
        or not isinstance(import_fixture_sha256, str)
        or _SHA256_RE.fullmatch(import_fixture_sha256) is None
        or not isinstance(import_pre_expected_sha256, str)
        or _SHA256_RE.fullmatch(import_pre_expected_sha256) is None
        or not isinstance(import_post_expected_sha256, str)
        or _SHA256_RE.fullmatch(import_post_expected_sha256) is None
        or target.runtime_mode != "hosted_resident"
    ):
        raise CommandError("experiment result has no verified account-pool binding")
    started_at = _as_utc(result.started_at, "result start")
    finished_at = _as_utc(result.finished_at, "result finish")
    readiness, readiness_sha256 = verify_readiness_receipt(
        args.readiness_receipt,
        expected_build_sha=target.build_sha,
        expected_persona_fixture_sha256=result.persona_fixture_sha256,
        expected_source_bundle_sha256=source_bundle_sha256,
        expected_import_fixture_sha256=import_fixture_sha256,
        expected_account_fingerprints=account_fingerprints,
        expected_pool_manifest_sha256=pool_sha256,
        expected_route_sha256=route_sha256,
        at_time=started_at,
    )
    if (
        readiness_sha256 != readiness_expected_sha256
        or readiness.get("deployment_receipt_pre_sha256")
        != import_pre_expected_sha256
        or readiness.get("deployment_receipt_post_sha256")
        != import_post_expected_sha256
    ):
        raise CommandError("readiness receipt does not match the experiment result")

    import_started_at = _as_utc(
        str(readiness.get("import_started_at") or ""), "import start"
    )
    import_finished_at = _as_utc(
        str(readiness.get("import_finished_at") or ""), "import finish"
    )
    readiness_created_at = _as_utc(
        str(readiness.get("created_at") or ""), "readiness created_at"
    )
    import_pre, import_pre_sha256 = _deployment_receipt(
        args.import_pre_deployment_receipt,
        expected_sha=target.build_sha,
        expected_runtime=target.runtime_mode,
        reference_time=import_started_at,
    )
    import_post, import_post_sha256 = _deployment_receipt(
        args.import_post_deployment_receipt,
        expected_sha=target.build_sha,
        expected_runtime=target.runtime_mode,
        reference_time=readiness_created_at,
    )
    if (
        import_pre_sha256 != import_pre_expected_sha256
        or import_post_sha256 != import_post_expected_sha256
    ):
        raise CommandError("import deployment receipts do not match readiness")
    import_pre_verified = _as_utc(
        str(import_pre.get("verified_at") or ""), "import pre deployment"
    )
    import_post_verified = _as_utc(
        str(import_post.get("verified_at") or ""), "import post deployment"
    )
    if not (
        import_pre_verified
        <= import_started_at
        <= import_finished_at
        <= import_post_verified
        <= readiness_created_at
    ):
        raise CommandError("deployment receipts do not bracket persona import")

    pre, pre_sha256 = _deployment_receipt(
        args.pre_deployment_receipt,
        expected_sha=target.build_sha,
        expected_runtime=target.runtime_mode,
        reference_time=started_at,
    )
    if pre_sha256 != metadata.get("deployment_receipt_sha256"):
        raise CommandError("pre-deployment receipt does not match the experiment result")
    pre_verified = _as_utc(str(pre.get("verified_at") or ""), "pre deployment")

    cleanup_receipt, cleanup_sha256 = verify_cleanup_receipt(
        args.cleanup_receipt,
        expected_pool_manifest_sha256=pool_sha256,
        expected_route_sha256=route_sha256,
        expected_account_fingerprints=account_fingerprints,
    )
    cleanup_verified = _as_utc(
        str(cleanup_receipt.get("created_at") or ""), "cleanup"
    )
    post, post_sha256 = _deployment_receipt(
        args.post_deployment_receipt,
        expected_sha=target.build_sha,
        expected_runtime=target.runtime_mode,
        reference_time=cleanup_verified,
        max_age_seconds=float("inf"),
    )
    post_verified = _as_utc(str(post.get("verified_at") or ""), "post deployment")
    receipt_digests = {
        import_pre_sha256,
        import_post_sha256,
        pre_sha256,
        post_sha256,
    }
    if (
        len(receipt_digests) != 4
        or not readiness_created_at <= pre_verified <= started_at
        or post_verified < finished_at
    ):
        raise CommandError("deployment receipts do not bracket the experiment")
    if cleanup_verified < post_verified:
        raise CommandError("account cleanup did not follow post-deployment verification")

    created_at = datetime.now(timezone.utc)
    if cleanup_verified > created_at:
        raise CommandError("account cleanup receipt is dated in the future")
    arm_receipt = {
        "schema_version": 1,
        "kind": _ARM_RECEIPT_KIND,
        "created_at": created_at.isoformat(),
        "target_id": target.target_id,
        "target_label": target.label,
        "build_sha": target.build_sha,
        "runtime_mode": target.runtime_mode,
        "result_status": result.status,
        "result_sha256": result_sha256,
        "persona_fixture_sha256": result.persona_fixture_sha256,
        "source_bundle_sha256": source_bundle_sha256,
        "import_fixture_sha256": import_fixture_sha256,
        "pool_manifest_sha256": pool_sha256,
        "route_sha256": route_sha256,
        "account_fingerprints": account_fingerprints,
        "readiness_receipt_sha256": readiness_sha256,
        "import_deployment_receipt_pre_sha256": import_pre_sha256,
        "import_deployment_receipt_post_sha256": import_post_sha256,
        "deployment_receipt_pre_sha256": pre_sha256,
        "deployment_receipt_post_sha256": post_sha256,
        "cleanup_receipt_sha256": cleanup_sha256,
        "import_started_at": readiness["import_started_at"],
        "import_finished_at": readiness["import_finished_at"],
        "import_pre_verified_at": import_pre["verified_at"],
        "import_post_verified_at": import_post["verified_at"],
        "pre_verified_at": pre["verified_at"],
        "post_verified_at": post["verified_at"],
        "cleanup_verified_at": cleanup_receipt["created_at"],
        "deployment_bracket_verified": True,
        "cleanup_verified": True,
    }
    content = (
        json.dumps(
            arm_receipt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    create_private_file(output, content)
    print(
        json.dumps(
            {
                "ok": True,
                "target_id": target.target_id,
                "result_status": result.status,
                "arm_receipt": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


def _verify_arm_receipt(
    path: Path, result: ExperimentResult, result_sha256: str
) -> tuple[dict[str, Any], str]:
    receipt, digest = read_private_json(path, label="arm run receipt")
    target = _single_target(result)
    metadata = result.metadata
    accounts = metadata.get("account_fingerprints")
    if (
        set(receipt) != _ARM_RECEIPT_KEYS
        or receipt.get("schema_version") != 1
        or receipt.get("kind") != _ARM_RECEIPT_KIND
        or receipt.get("target_id") != target.target_id
        or receipt.get("target_label") != target.label
        or receipt.get("build_sha") != target.build_sha
        or receipt.get("runtime_mode") != target.runtime_mode
        or receipt.get("result_status") != result.status
        or receipt.get("result_sha256") != result_sha256
        or receipt.get("persona_fixture_sha256") != result.persona_fixture_sha256
        or receipt.get("source_bundle_sha256")
        != metadata.get("source_bundle_sha256")
        or receipt.get("import_fixture_sha256")
        != metadata.get("import_fixture_sha256")
        or receipt.get("pool_manifest_sha256")
        != metadata.get("account_pool_manifest_sha256")
        or receipt.get("route_sha256") != metadata.get("account_route_sha256")
        or receipt.get("account_fingerprints") != accounts
        or receipt.get("readiness_receipt_sha256")
        != metadata.get("account_readiness_receipt_sha256")
        or receipt.get("import_deployment_receipt_pre_sha256")
        != metadata.get("import_deployment_receipt_pre_sha256")
        or receipt.get("import_deployment_receipt_post_sha256")
        != metadata.get("import_deployment_receipt_post_sha256")
        or receipt.get("deployment_receipt_pre_sha256")
        != metadata.get("deployment_receipt_sha256")
        or receipt.get("deployment_bracket_verified") is not True
        or receipt.get("cleanup_verified") is not True
        or target.runtime_mode != "hosted_resident"
        or not isinstance(accounts, list)
        or not accounts
    ):
        raise CommandError("arm receipt does not match its experiment result")
    for key in (
        "result_sha256",
        "persona_fixture_sha256",
        "source_bundle_sha256",
        "import_fixture_sha256",
        "pool_manifest_sha256",
        "route_sha256",
        "readiness_receipt_sha256",
        "import_deployment_receipt_pre_sha256",
        "import_deployment_receipt_post_sha256",
        "deployment_receipt_pre_sha256",
        "deployment_receipt_post_sha256",
        "cleanup_receipt_sha256",
    ):
        if not isinstance(receipt.get(key), str) or _SHA256_RE.fullmatch(
            receipt[key]
        ) is None:
            raise CommandError("arm receipt contains an invalid digest")
    if (
        accounts != sorted(accounts)
        or len(accounts) != len(set(accounts))
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in accounts
        )
        or len(
            {
                receipt["import_deployment_receipt_pre_sha256"],
                receipt["import_deployment_receipt_post_sha256"],
                receipt["deployment_receipt_pre_sha256"],
                receipt["deployment_receipt_post_sha256"],
            }
        )
        != 4
    ):
        raise CommandError("arm receipt identity evidence is invalid")
    import_started = _as_utc(
        str(receipt.get("import_started_at") or ""), "arm import start"
    )
    import_finished = _as_utc(
        str(receipt.get("import_finished_at") or ""), "arm import finish"
    )
    import_pre = _as_utc(
        str(receipt.get("import_pre_verified_at") or ""), "arm import pre deployment"
    )
    import_post = _as_utc(
        str(receipt.get("import_post_verified_at") or ""), "arm import post deployment"
    )
    pre = _as_utc(str(receipt.get("pre_verified_at") or ""), "arm pre deployment")
    post = _as_utc(
        str(receipt.get("post_verified_at") or ""), "arm post deployment"
    )
    cleaned = _as_utc(str(receipt.get("cleanup_verified_at") or ""), "arm cleanup")
    created = _as_utc(str(receipt.get("created_at") or ""), "arm created_at")
    started = _as_utc(result.started_at, "result start")
    finished = _as_utc(result.finished_at, "result finish")
    if not (
        import_pre
        <= import_started
        <= import_finished
        <= import_post
        <= pre
        <= started
        <= finished
        <= post
        <= cleaned
        <= created
        <= datetime.now(timezone.utc)
    ):
        raise CommandError("arm receipt does not prove ordered run boundaries")
    return receipt, digest


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline, baseline_result_sha256 = _load_private_result(args.baseline)
    candidate, candidate_result_sha256 = _load_private_result(args.candidate)
    baseline_arm, _baseline_arm_sha256 = _verify_arm_receipt(
        args.baseline_arm_receipt, baseline, baseline_result_sha256
    )
    candidate_arm, _candidate_arm_sha256 = _verify_arm_receipt(
        args.candidate_arm_receipt, candidate, candidate_result_sha256
    )
    if baseline_arm["target_label"] != "baseline" or candidate_arm[
        "target_label"
    ] != "candidate":
        raise CommandError("arm receipts must identify baseline and candidate")
    if baseline_arm["route_sha256"] != candidate_arm["route_sha256"]:
        raise CommandError("baseline and candidate arm routes do not match")
    if (
        baseline_arm["persona_fixture_sha256"]
        != candidate_arm["persona_fixture_sha256"]
        or baseline_arm["source_bundle_sha256"]
        != candidate_arm["source_bundle_sha256"]
        or baseline_arm["import_fixture_sha256"]
        != candidate_arm["import_fixture_sha256"]
    ):
        raise CommandError("baseline and candidate persona sources do not match")
    for key in (
        "pool_manifest_sha256",
        "readiness_receipt_sha256",
        "import_deployment_receipt_pre_sha256",
        "import_deployment_receipt_post_sha256",
        "deployment_receipt_pre_sha256",
        "deployment_receipt_post_sha256",
        "cleanup_receipt_sha256",
    ):
        if baseline_arm[key] == candidate_arm[key]:
            raise CommandError(f"baseline and candidate reused {key}")
    if set(baseline_arm["account_fingerprints"]) & set(
        candidate_arm["account_fingerprints"]
    ):
        raise CommandError("baseline and candidate account pools overlap")
    comparison = compare_results(
        baseline,
        candidate,
        baseline_target_id=args.baseline_target_id,
        candidate_target_id=args.candidate_target_id,
        required_pass_rate=args.required_pass_rate,
        max_score_drop=args.max_score_drop,
    )
    write_reports(comparison, args.output_dir)
    print(
        json.dumps(
            {
                "ok": comparison["status"] == "PASS",
                "status": comparison["status"],
                "summary": comparison["summary"],
                "artifact_dir": str(args.output_dir),
            },
            sort_keys=True,
        )
    )
    return _gate_exit_code(str(comparison["status"]))


def _suite_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--persona", type=Path, default=DEFAULT_PERSONA)
    parser.add_argument("--source-fixture", type=Path, default=DEFAULT_SOURCE_FIXTURE)
    parser.add_argument("--scenario-root", type=Path, default=DEFAULT_SCENARIO_ROOT)
    parser.add_argument("--scenario", type=Path, action="append")
    parser.add_argument(
        "--include-nightly",
        action="store_true",
        help="include strong-boundary/nightly scenarios when --scenario is omitted",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate and fingerprint fixtures")
    _suite_arguments(validate)
    validate.set_defaults(handler=_cmd_validate)

    run = commands.add_parser(
        "run-live",
        help="run one deployed arm against a prepared isolated account batch",
    )
    _suite_arguments(run)
    account_input = run.add_mutually_exclusive_group(required=True)
    account_input.add_argument(
        "--manifest-profile",
        action="append",
        type=_manifest_spec,
        metavar="PROFILE_ID=/ABSOLUTE/MANIFEST.json",
        help="legacy diagnostic one-profile manifest (repeatable)",
    )
    account_input.add_argument(
        "--account-pool",
        type=Path,
        help="strict aggregate pool from provision-pool",
    )
    run.add_argument(
        "--readiness-receipt",
        type=Path,
        help="machine-verified persona import receipt for --account-pool",
    )
    run.add_argument("--target-id", required=True)
    run.add_argument("--target-label", choices=("baseline", "candidate"), required=True)
    run.add_argument("--build-sha", required=True)
    run.add_argument("--deployment-receipt", type=Path, required=True)
    run.add_argument("--experiment-id", default="")
    run.add_argument("--repetitions", type=int, default=3)
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--accounts-ready",
        action="store_true",
        help="legacy diagnostic operator attestation; invalid with --account-pool",
    )
    cleanup = run.add_mutually_exclusive_group(required=True)
    cleanup.add_argument("--cleanup-account", action="store_true")
    cleanup.add_argument("--external-cleanup-guaranteed", action="store_true")
    run.add_argument("--codex-bin", type=Path)
    run.add_argument("--codex-home", type=Path)
    run.add_argument("--judge-work-root", type=Path)
    run.add_argument("--judge-model", default="")
    run.add_argument("--judge-id", default="")
    run.add_argument("--judge-configuration-id", default="")
    run.add_argument("--judge-codex-profile", default=DEFAULT_CODEX_PROFILE)
    run.add_argument(
        "--judge-permission-profile", default=DEFAULT_PERMISSION_PROFILE
    )
    run.add_argument(
        "--judge-reasoning-effort",
        choices=("low", "medium", "high", "xhigh"),
        default="medium",
    )
    run.add_argument("--judge-timeout", type=float, default=180.0)
    run.add_argument("--allow-private-judge-egress", action="store_true")
    run.set_defaults(handler=_cmd_run)

    finalize = commands.add_parser(
        "finalize-arm",
        help="bind a private arm result to pre/post deployment and full cleanup proof",
    )
    finalize.add_argument("--result", type=Path, required=True)
    finalize.add_argument("--readiness-receipt", type=Path, required=True)
    finalize.add_argument(
        "--import-pre-deployment-receipt", type=Path, required=True
    )
    finalize.add_argument(
        "--import-post-deployment-receipt", type=Path, required=True
    )
    finalize.add_argument("--pre-deployment-receipt", type=Path, required=True)
    finalize.add_argument("--post-deployment-receipt", type=Path, required=True)
    finalize.add_argument("--cleanup-receipt", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(handler=_cmd_finalize_arm)

    compare = commands.add_parser(
        "compare", help="compare no-overwrite private baseline and candidate results"
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--baseline-arm-receipt", type=Path, required=True)
    compare.add_argument("--candidate-arm-receipt", type=Path, required=True)
    compare.add_argument("--baseline-target-id")
    compare.add_argument("--candidate-target-id")
    compare.add_argument("--required-pass-rate", type=float, default=1.0)
    compare.add_argument("--max-score-drop", type=float, default=0.0)
    compare.add_argument("--output-dir", type=Path, required=True)
    compare.set_defaults(handler=_cmd_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "repetitions", 1) < 1 or getattr(args, "concurrency", 1) < 1:
            raise CommandError("repetitions and concurrency must be positive")
        return int(args.handler(args))
    except (
        AtomicPrivateFileError,
        CommandError,
        ComparisonError,
        ContractError,
        JudgeError,
        LiveAccountContractError,
        LiveScenarioProbeError,
        LoaderError,
        ReportError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:500],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
