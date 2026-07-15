#!/usr/bin/env python3
"""Prepare and clean a trusted persona-memory regression account pool.

The command deliberately sits outside the conversation runner.  It may use the
account manifest and cleanup authority, while the runner only receives an
owner-controlled, content-free readiness receipt.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.atomic_private_file import (  # noqa: E402
    AtomicPrivateFileError,
    create_private_file,
)
from qa.provision_profiles import (  # noqa: E402
    ProvisionError,
    cleanup_manifest_snapshot,
    unlink_manifest_snapshot,
)
from qa.regression.contracts import canonical_json_bytes  # noqa: E402
from qa.regression.live_accounts import (  # noqa: E402
    ACCOUNT_RUN_LEASE_REMAINING_SECONDS,
    CLEANUP_KIND,
    READINESS_KIND,
    LiveAccountContractError,
    load_account_pool,
)
from qa.regression.scenario_loader import (  # noqa: E402
    LoaderError,
    load_golden_persona,
    load_verified_source_fixture,
)
from qa.run_persona_memory_regression import (  # noqa: E402
    CommandError,
    _deployment_receipt,
)
from qa.verify_deployment import (  # noqa: E402
    DeploymentVerificationError,
    verify_deployment,
)
from tools import genesis_e2e  # noqa: E402
from tools.provider_smoke.client import SmokeClient  # noqa: E402


LOCKED_BASE_URL = "https://test-api.feedling.app"
READINESS_TTL_SECONDS = 1800
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_OUTCOME_KIND = "persona_memory_account_cleanup_outcome"
_CLEANUP_OUTCOME_KEYS = {
    "schema_version",
    "kind",
    "created_at",
    "base_url",
    "pool_manifest_sha256",
    "route_sha256",
    "account_count",
    "account_fingerprints",
    "attempted",
    "cleaned",
    "failed_count",
    "complete",
    "accounts",
}
_CLEANUP_ACCOUNT_KEYS = {
    "pool_index",
    "account_fingerprint",
    "profile_id",
    "attempted",
    "reset_response_accepted",
    "provider_config_preexisted",
    "provider_config_live_predelete_observed",
    "provider_config_deleted",
    "key_envelope_deleted",
    "provider_config_deletion_source",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
    "status",
}
_CLEANUP_ACCOUNT_BOOL_KEYS = {
    "attempted",
    "reset_response_accepted",
    "provider_config_preexisted",
    "provider_config_live_predelete_observed",
    "provider_config_deleted",
    "key_envelope_deleted",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
}
_CLEANUP_ACCOUNT_REQUIRED_TRUE = {
    "attempted",
    "reset_response_accepted",
    "provider_config_deleted",
    "key_envelope_deleted",
    "account_reset",
    "old_credential_rejected",
    "user_absence_verified",
}


class PreparationError(RuntimeError):
    """A bounded, credential-free account-preparation failure."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _owner_directory(path: Path, *, empty: bool = False) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise PreparationError("private directory path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise PreparationError("private directory is unavailable") from None
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PreparationError("private directory must be owner-controlled mode 0700")
    if empty:
        try:
            if any(resolved.iterdir()):
                raise PreparationError("private work directory must be empty")
        except OSError:
            raise PreparationError("private directory is unreadable") from None
    return resolved


def _private_output(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.exists() or candidate.is_symlink():
        raise PreparationError("private receipt path must be new and absolute")
    parent = _owner_directory(candidate.parent)
    return parent / candidate.name


def _outside_artifacts(path: Path, artifact_dir: Path) -> None:
    try:
        private_path = path.resolve(strict=False)
        public_root = artifact_dir.expanduser().resolve(strict=True)
        inside = os.path.commonpath([str(private_path), str(public_root)]) == str(
            public_root
        )
    except (OSError, RuntimeError, ValueError):
        raise PreparationError("artifact boundary is unavailable") from None
    if inside:
        raise PreparationError("private preparation output must be outside artifacts")


def _private_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(payload) + b"\n"


def _account_fingerprint(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _post_import_reset(client: SmokeClient, session: Any) -> dict[str, bool]:
    status, body = client._req(
        "DELETE",
        "/v1/chat/history",
        api_key=session.api_key,
        body={"confirm": "clear-chat-history"},
        attempts=1,
    )
    if status != 200 or not isinstance(body, Mapping) or body.get("cleared") is not True:
        raise PreparationError("post-import transcript clearing failed")
    history_status, history = client._req(
        "GET", "/v1/chat/history?limit=1", api_key=session.api_key, attempts=1
    )
    identity_status, identity = client._req(
        "GET", "/v1/identity/get", api_key=session.api_key, attempts=1
    )
    memory_status, memory = client._req(
        "GET", "/v1/memory/list?limit=1", api_key=session.api_key, attempts=1
    )
    client.clear_trace(session)
    history_empty = (
        history_status == 200
        and isinstance(history, Mapping)
        and history.get("messages") == []
    )
    identity_present = (
        identity_status == 200
        and isinstance(identity, Mapping)
        and isinstance(identity.get("identity"), Mapping)
        and bool(identity["identity"])
    )
    memory_present = (
        memory_status == 200
        and isinstance(memory, Mapping)
        and isinstance(memory.get("moments"), list)
        and bool(memory["moments"])
    )
    if not history_empty or not identity_present or not memory_present:
        raise PreparationError("post-import account state is not ready")
    return {
        "post_import_chat_empty": True,
        "post_import_identity_present": True,
        "post_import_memory_present": True,
        "trace_cleared": True,
    }


def _prepare_one(
    *,
    profile: Mapping[str, Any],
    session: Any,
    fixture: dict[str, Any],
    work_dir: Path,
    artifact_dir: Path,
    timeout: float,
    poll: float,
    intro_timeout: float,
    memory_limit: int,
) -> dict[str, Any]:
    fingerprint = _account_fingerprint(str(session.user_id))
    evidence_path = work_dir / f"evidence-{fingerprint}.json"
    capture = genesis_e2e.capture_existing_session_distill_evidence(
        api_url=LOCKED_BASE_URL,
        api_key=str(session.api_key),
        user_id=str(session.user_id),
        content_private_key=session.sk,
        fixture=fixture,
        private_evidence_path=str(evidence_path),
        artifact_dir=str(artifact_dir),
        timeout=timeout,
        poll=poll,
        intro_timeout=intro_timeout,
        memory_limit=memory_limit,
    )
    readiness = genesis_e2e.finalize_existing_session_import_readiness(
        private_evidence_path=str(evidence_path),
        fixture=fixture,
        artifact_dir=str(artifact_dir),
    )
    if readiness.get("ok") is not True:
        raise PreparationError("deterministic persona import acceptance failed")
    checks = readiness.get("checks")
    if not isinstance(checks, Mapping):
        raise PreparationError("deterministic persona import receipt is invalid")
    source_verified = all(
        checks.get(name) is True
        for name in (
            "archive_receipts_verified",
            "genesis_upload_metadata_verified",
        )
    )
    surfaces_decryptable = all(
        checks.get(name) is True
        for name in (
            "identity_envelope_decrypted",
            "persona_envelope_decrypted",
            "memory_envelopes_decrypted",
            "chat_envelopes_decrypted",
        )
    )
    if not source_verified or not surfaces_decryptable:
        raise PreparationError("persona import transport evidence is incomplete")
    post_import = _post_import_reset(SmokeClient(LOCKED_BASE_URL), session)
    if capture.get("evidence_sha256") != readiness.get("evidence_sha256"):
        raise PreparationError("persona import evidence binding is invalid")
    return {
        "account_fingerprint": fingerprint,
        "evidence_sha256": readiness["evidence_sha256"],
        "fixture_sha256": readiness["fixture_sha256"],
        "source_materials_verified": True,
        "surfaces_decryptable": True,
        "deterministic_acceptance": True,
        **post_import,
    }


def _best_effort_cleanup(pool: Any) -> bool:
    try:
        result = cleanup_manifest_snapshot(
            pool.manifest,
            pool.path,
            pool.file_identity,
            env=os.environ,
            delete_manifest=True,
        )
    except Exception:
        return False
    return bool(result.get("manifest_deleted") and not result.get("failed_profile_ids"))


def _best_effort_delete_evidence(work_dir: Path) -> bool:
    try:
        evidence_paths = list(work_dir.glob("evidence-*.json"))
    except OSError:
        return False
    complete = True
    for evidence in evidence_paths:
        try:
            evidence.unlink()
        except OSError:
            complete = False
    try:
        return complete and not any(work_dir.glob("evidence-*.json"))
    except OSError:
        return False


def _cleanup_recovery_paths(pool_path: Path) -> tuple[Path, Path]:
    parent = _owner_directory(pool_path.expanduser().parent)
    pending = parent / f".{pool_path.name}.cleanup-pending"
    outcome = parent / f".{pool_path.name}.cleanup-outcome.json"
    return pending, outcome


def _verified_cleanup_accounts(
    value: Any,
    *,
    count: int,
    expected_fingerprints_by_index: Sequence[str] | None = None,
    require_pass: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != count:
        raise PreparationError("per-account cleanup evidence is incomplete")
    rows: list[dict[str, Any]] = []
    observed_fingerprints: list[str] = []
    expected = tuple(expected_fingerprints_by_index or ())
    if expected and len(expected) != count:
        raise PreparationError("per-account cleanup evidence binding is invalid")
    for expected_index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping) or set(raw) != _CLEANUP_ACCOUNT_KEYS:
            raise PreparationError("per-account cleanup evidence is invalid")
        row = dict(raw)
        fingerprint = row.get("account_fingerprint")
        if (
            row.get("pool_index") != expected_index
            or not isinstance(fingerprint, str)
            or _SHA256_RE.fullmatch(fingerprint) is None
            or not isinstance(row.get("profile_id"), str)
            or not row["profile_id"]
            or any(type(row.get(key)) is not bool for key in _CLEANUP_ACCOUNT_BOOL_KEYS)
            or row.get("provider_config_deletion_source")
            not in {"explicit_api", "account_cascade"}
            or row.get("status") not in {"PASS", "FAIL"}
            or (
                require_pass
                and (
                    row.get("status") != "PASS"
                    or any(
                        row.get(key) is not True
                        for key in _CLEANUP_ACCOUNT_REQUIRED_TRUE
                    )
                )
            )
            or (expected and fingerprint != expected[expected_index - 1])
        ):
            raise PreparationError("per-account cleanup evidence is invalid")
        observed_fingerprints.append(fingerprint)
        rows.append(row)
    if len(set(observed_fingerprints)) != count:
        raise PreparationError("per-account cleanup evidence is not unique")
    return rows


def _cleanup_outcome(pool: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    count = len(pool.account_fingerprints)
    failed_count = len(result.get("failed_profile_ids") or [])
    attempted = int(result.get("attempted") or 0)
    cleaned = int(result.get("cleaned") or 0)
    expected_by_index = [
        _account_fingerprint(str(session.user_id))
        for _profile, session in pool.rows
    ]
    accounts = _verified_cleanup_accounts(
        result.get("cleanup_accounts"),
        count=count,
        expected_fingerprints_by_index=expected_by_index,
        require_pass=False,
    )
    complete = bool(
        failed_count == 0
        and result.get("manifest_retained") is True
        and attempted == count
        and cleaned == count
        and all(row["status"] == "PASS" for row in accounts)
    )
    if not complete:
        raise PreparationError(
            "account cleanup is incomplete; retained recovery manifest must be retried"
        )
    return {
        "schema_version": 1,
        "kind": _CLEANUP_OUTCOME_KIND,
        "created_at": _timestamp(_utc_now()),
        "base_url": LOCKED_BASE_URL,
        "pool_manifest_sha256": pool.manifest_sha256,
        "route_sha256": pool.route_sha256,
        "account_count": count,
        "account_fingerprints": list(sorted(pool.account_fingerprints)),
        "attempted": attempted,
        "cleaned": cleaned,
        "failed_count": 0,
        "complete": True,
        "accounts": accounts,
    }


def _verified_cleanup_outcome(path: Path) -> dict[str, Any]:
    from qa.regression.live_accounts import read_private_json

    document, _digest = read_private_json(path, label="cleanup outcome journal")
    fingerprints = document.get("account_fingerprints")
    count = document.get("account_count")
    if (
        set(document) != _CLEANUP_OUTCOME_KEYS
        or document.get("schema_version") != 1
        or document.get("kind") != _CLEANUP_OUTCOME_KIND
        or document.get("base_url") != LOCKED_BASE_URL
        or not isinstance(document.get("created_at"), str)
        or not isinstance(document.get("pool_manifest_sha256"), str)
        or _SHA256_RE.fullmatch(document["pool_manifest_sha256"]) is None
        or not isinstance(document.get("route_sha256"), str)
        or _SHA256_RE.fullmatch(document["route_sha256"]) is None
        or type(count) is not int
        or count < 1
        or not isinstance(fingerprints, list)
        or len(fingerprints) != count
        or fingerprints != sorted(fingerprints)
        or len(fingerprints) != len(set(fingerprints))
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in fingerprints
        )
        or type(document.get("attempted")) is not int
        or document.get("attempted") != count
        or type(document.get("cleaned")) is not int
        or document.get("cleaned") != count
        or type(document.get("failed_count")) is not int
        or document.get("failed_count") != 0
        or document.get("complete") is not True
    ):
        raise PreparationError("cleanup outcome journal is invalid")
    accounts = _verified_cleanup_accounts(document.get("accounts"), count=count)
    if sorted(row["account_fingerprint"] for row in accounts) != fingerprints:
        raise PreparationError("cleanup outcome account evidence is mismatched")
    try:
        created = datetime.fromisoformat(
            document["created_at"].replace("Z", "+00:00")
        )
        if created.tzinfo is None:
            raise ValueError
    except ValueError:
        raise PreparationError("cleanup outcome timestamp is invalid") from None
    return document


def _final_cleanup_receipt(outcome: Mapping[str, Any]) -> dict[str, Any]:
    receipt = dict(outcome)
    receipt["kind"] = CLEANUP_KIND
    receipt["manifest_deleted"] = True
    return receipt


def _cmd_prepare(args: argparse.Namespace) -> int:
    output = _private_output(args.readiness_receipt)
    post_deployment_output = _private_output(args.post_deployment_receipt)
    if output == post_deployment_output:
        raise PreparationError("readiness and post-deployment receipts must differ")
    work_dir = _owner_directory(args.work_dir, empty=True)
    artifact_dir = args.artifact_dir.expanduser()
    if not artifact_dir.is_absolute() or not artifact_dir.is_dir():
        raise PreparationError("artifact directory must be an existing absolute path")
    _outside_artifacts(work_dir, artifact_dir)
    _outside_artifacts(output, artifact_dir)
    _outside_artifacts(post_deployment_output, artifact_dir)

    pool = load_account_pool(args.account_pool)
    if pool.deployment_runtime != "hosted_resident":
        raise PreparationError(
            "formal persona-memory preparation requires per-account Hosted Runtime V2 readback"
        )
    persona = load_golden_persona(args.persona)
    fixture, import_fixture_sha256 = load_verified_source_fixture(
        persona, args.source_fixture
    )
    if _BUILD_SHA_RE.fullmatch(str(args.build_sha or "")) is None:
        raise PreparationError("build SHA must be a full lowercase digest")
    _deployment, deployment_pre_sha256 = _deployment_receipt(
        args.deployment_receipt,
        expected_sha=args.build_sha,
        expected_runtime=pool.deployment_runtime,
    )
    import_started_at = _utc_now()

    try:
        accounts: list[dict[str, Any]] = []
        executor = ThreadPoolExecutor(max_workers=args.concurrency)
        futures: list[Future[dict[str, Any]]] = []
        try:
            futures = [
                executor.submit(
                    _prepare_one,
                    profile=profile,
                    session=session,
                    fixture=copy.deepcopy(fixture),
                    work_dir=work_dir,
                    artifact_dir=artifact_dir,
                    timeout=args.timeout,
                    poll=args.poll,
                    intro_timeout=args.intro_timeout,
                    memory_limit=args.memory_limit,
                )
                for profile, session in pool.rows
            ]
            for future in as_completed(futures):
                accounts.append(future.result())
        except Exception:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        accounts.sort(key=lambda item: item["account_fingerprint"])
        observed_fingerprints = tuple(
            item["account_fingerprint"] for item in accounts
        )
        if observed_fingerprints != tuple(sorted(pool.account_fingerprints)):
            raise PreparationError("prepared account set does not match the pool")
        fixture_hashes = {item["fixture_sha256"] for item in accounts}
        if fixture_hashes != {import_fixture_sha256}:
            raise PreparationError("persona import fixture evidence is inconsistent")
        evidence_hashes = {item["evidence_sha256"] for item in accounts}
        if len(evidence_hashes) != len(accounts):
            raise PreparationError("persona import account evidence is not unique")

        import_finished_at = _utc_now()
        post_deployment = verify_deployment(
            args.build_sha,
            post_deployment_output,
            env=os.environ,
            expected_runtime=pool.deployment_runtime,
        )
        deployment_post_sha256 = hashlib.sha256(
            canonical_json_bytes(post_deployment)
        ).hexdigest()
        if deployment_post_sha256 == deployment_pre_sha256:
            raise PreparationError("import deployment receipts must be distinct")

        created_at = _utc_now()
        minimum_lease_expiry = created_at + timedelta(
            seconds=ACCOUNT_RUN_LEASE_REMAINING_SECONDS
        )
        if any(
            datetime.fromtimestamp(
                profile["synthetic_account_lease"]["expires_at_epoch"],
                tz=timezone.utc,
            )
            < minimum_lease_expiry
            for profile, _session in pool.rows
        ):
            raise PreparationError(
                "account leases have less than the required run lifetime remaining"
            )
        receipt = {
            "schema_version": 1,
            "kind": READINESS_KIND,
            "created_at": _timestamp(created_at),
            "expires_at": _timestamp(
                created_at + timedelta(seconds=READINESS_TTL_SECONDS)
            ),
            "base_url": LOCKED_BASE_URL,
            "build_sha": args.build_sha,
            "deployment_receipt_pre_sha256": deployment_pre_sha256,
            "deployment_receipt_post_sha256": deployment_post_sha256,
            "import_started_at": _timestamp(import_started_at),
            "import_finished_at": _timestamp(import_finished_at),
            "pool_manifest_sha256": pool.manifest_sha256,
            "route_sha256": pool.route_sha256,
            "persona_fixture_sha256": persona.fixture_sha256,
            "source_bundle_sha256": persona.source_fixture_sha256,
            "import_fixture_sha256": import_fixture_sha256,
            "account_count": len(accounts),
            "account_fingerprints": list(observed_fingerprints),
            "accounts": accounts,
            "all_ready": True,
        }
        create_private_file(output, _private_json_bytes(receipt))
    except Exception:
        evidence_deleted = _best_effort_delete_evidence(work_dir)
        accounts_cleaned = _best_effort_cleanup(pool)
        if accounts_cleaned and evidence_deleted:
            cleanup_status = "full cleanup completed"
        elif accounts_cleaned:
            cleanup_status = "account cleanup completed; private evidence retained"
        elif evidence_deleted:
            cleanup_status = "account cleanup requires retry"
        else:
            cleanup_status = "account cleanup requires retry; private evidence retained"
        raise PreparationError(
            "account preparation failed; " + cleanup_status
        ) from None

    print(
        json.dumps(
            {
                "ok": True,
                "account_count": len(accounts),
                "profile_id": pool.profile_id,
                "readiness_receipt": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    output = _private_output(args.receipt)
    pool_path = args.account_pool.expanduser()
    pending_path, outcome_path = _cleanup_recovery_paths(pool_path)
    if output in {pending_path, outcome_path}:
        raise PreparationError("cleanup receipt conflicts with a recovery path")

    if outcome_path.exists():
        if pool_path.exists():
            raise PreparationError("cleanup recovery state is ambiguous")
        outcome = _verified_cleanup_outcome(outcome_path)
        if pending_path.exists():
            pool = load_account_pool(pending_path, allow_expired_lease=True)
            if (
                pool.manifest_sha256 != outcome["pool_manifest_sha256"]
                or pool.route_sha256 != outcome["route_sha256"]
                or list(pool.account_fingerprints)
                != outcome["account_fingerprints"]
            ):
                raise PreparationError("cleanup recovery manifest does not match outcome")
            delete_failure = unlink_manifest_snapshot(
                pending_path, pool.file_identity
            )
            if delete_failure is not None:
                raise PreparationError(
                    "cleanup recovery manifest could not be removed"
                )
    else:
        if pending_path.exists():
            if pool_path.exists():
                raise PreparationError("cleanup recovery state is ambiguous")
            pool = load_account_pool(pending_path, allow_expired_lease=True)
        else:
            pool = load_account_pool(pool_path, allow_expired_lease=True)
            try:
                os.rename(pool_path, pending_path)
                renamed = pending_path.lstat()
            except OSError:
                raise PreparationError("account pool could not enter cleanup recovery") from None
            if (renamed.st_dev, renamed.st_ino) != pool.file_identity:
                raise PreparationError("cleanup recovery manifest identity changed")
        result = cleanup_manifest_snapshot(
            pool.manifest,
            pending_path,
            pool.file_identity,
            env=os.environ,
            delete_manifest=False,
        )
        outcome = _cleanup_outcome(pool, result)
        create_private_file(outcome_path, _private_json_bytes(outcome))
        delete_failure = unlink_manifest_snapshot(pending_path, pool.file_identity)
        if delete_failure is not None:
            raise PreparationError("cleanup recovery manifest could not be removed")

    receipt = _final_cleanup_receipt(outcome)
    create_private_file(output, _private_json_bytes(receipt))
    try:
        outcome_path.unlink()
    except OSError:
        pass
    print(
        json.dumps(
            {
                "ok": True,
                "attempted": receipt["attempted"],
                "cleaned": receipt["cleaned"],
                "failed_count": 0,
                "cleanup_receipt": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare", help="import and verify the fixed persona across an account pool"
    )
    prepare.add_argument("--account-pool", type=Path, required=True)
    prepare.add_argument("--build-sha", required=True)
    prepare.add_argument("--deployment-receipt", type=Path, required=True)
    prepare.add_argument(
        "--post-deployment-receipt",
        type=Path,
        required=True,
        help="new owner-only receipt path verified immediately after all imports",
    )
    prepare.add_argument(
        "--persona",
        type=Path,
        default=Path("qa/regression/fixtures/golden-persona-mira-v1.json"),
    )
    prepare.add_argument(
        "--source-fixture",
        type=Path,
        default=Path("qa/fixtures/persona-import-v1.json"),
    )
    prepare.add_argument("--work-dir", type=Path, required=True)
    prepare.add_argument("--artifact-dir", type=Path, required=True)
    prepare.add_argument("--readiness-receipt", type=Path, required=True)
    prepare.add_argument("--concurrency", type=int, default=3)
    prepare.add_argument("--timeout", type=float, default=900.0)
    prepare.add_argument("--poll", type=float, default=10.0)
    prepare.add_argument("--intro-timeout", type=float, default=180.0)
    prepare.add_argument("--memory-limit", type=int, default=100)
    prepare.set_defaults(handler=_cmd_prepare)

    remove = commands.add_parser(
        "cleanup", help="reset every account and emit a hash-bound cleanup receipt"
    )
    remove.add_argument("--account-pool", type=Path, required=True)
    remove.add_argument("--receipt", type=Path, required=True)
    remove.set_defaults(handler=_cmd_cleanup)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "concurrency", 1) < 1:
            raise PreparationError("concurrency must be positive")
        if getattr(args, "timeout", 1.0) <= 0 or getattr(args, "poll", 0.0) < 0:
            raise PreparationError("timeout and poll values are invalid")
        return int(args.handler(args))
    except (
        AtomicPrivateFileError,
        CommandError,
        DeploymentVerificationError,
        genesis_e2e.ExistingSessionDistillError,
        LiveAccountContractError,
        LoaderError,
        PreparationError,
        ProvisionError,
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
