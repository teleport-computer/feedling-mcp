from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa import prepare_persona_memory_accounts as prepare
from qa.regression import live_accounts
from tools.provider_smoke.client import Session


BUILD_SHA = "a" * 40
DEPLOYMENT_SHA = "b" * 64
PERSONA_SHA = "c" * 64
SOURCE_SHA = "d" * 64
FIXTURE_SHA = "e" * 64
PRIVATE_MARKER = "QA-PRIVATE-CANARY-PREPARE"


def _private_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _pool(tmp_path: Path, count: int = 2) -> live_accounts.AccountPool:
    rows = []
    for index in range(1, count + 1):
        profile = {
            "profile_id": "official-openai",
            "runtime_mode": "hosted_resident",
            "synthetic_account_lease": {
                "expires_at_epoch": int(
                    (datetime.now(timezone.utc) + timedelta(hours=3)).timestamp()
                )
            },
        }
        session = Session(
            user_id=f"private-user-{index}",
            api_key=f"private-api-key-{index}",
            sk=bytes([index]) * 32,
            pk=bytes([index + 10]) * 32,
        )
        rows.append((profile, session))
    fingerprints = tuple(
        sorted(
            hashlib.sha256(session.user_id.encode("utf-8")).hexdigest()
            for _profile, session in rows
        )
    )
    path = (tmp_path / "private-account-pool.json").resolve()
    path.write_text('{"schema_version":1}\n', encoding="utf-8")
    path.chmod(0o600)
    metadata = path.stat()
    return live_accounts.AccountPool(
        path=path,
        manifest_sha256="f" * 64,
        route_sha256="1" * 64,
        account_fingerprints=fingerprints,
        profile_id="official-openai",
        deployment_runtime="hosted_resident",
        rows=tuple(rows),
        manifest={"schema_version": 1, "base_url": prepare.LOCKED_BASE_URL},
        file_identity=(metadata.st_dev, metadata.st_ino),
    )


def _cleanup_rows(pool: live_accounts.AccountPool, *, complete: bool = True) -> list[dict]:
    rows = []
    for index, (_profile, session) in enumerate(pool.rows, start=1):
        passed = complete or index < len(pool.rows)
        rows.append(
            {
                "pool_index": index,
                "account_fingerprint": hashlib.sha256(
                    session.user_id.encode("utf-8")
                ).hexdigest(),
                "profile_id": pool.profile_id,
                "attempted": True,
                "reset_response_accepted": passed,
                "provider_config_preexisted": True,
                "provider_config_live_predelete_observed": passed,
                "provider_config_deleted": passed,
                "key_envelope_deleted": passed,
                "provider_config_deletion_source": "explicit_api",
                "account_reset": passed,
                "old_credential_rejected": passed,
                "user_absence_verified": passed,
                "status": "PASS" if passed else "FAIL",
            }
        )
    return rows


class _FakeSmokeClient:
    instances: list["_FakeSmokeClient"] = []

    def __init__(self, base_url: str):
        assert base_url == prepare.LOCKED_BASE_URL
        self.calls: list[tuple[str, str, str]] = []
        self.trace_users: list[str] = []
        self.__class__.instances.append(self)

    def _req(self, method, path, *, api_key, **kwargs):
        self.calls.append((method, path, api_key))
        assert kwargs.get("attempts") == 1
        if method == "DELETE" and path == "/v1/chat/history":
            assert kwargs.get("body") == {"confirm": "clear-chat-history"}
            return 200, {"cleared": True}
        if method == "GET" and path == "/v1/chat/history?limit=1":
            return 200, {"messages": []}
        if method == "GET" and path == "/v1/identity/get":
            return 200, {"identity": {"body_ct": "opaque"}}
        if method == "GET" and path == "/v1/memory/list?limit=1":
            return 200, {"moments": [{"body_ct": "opaque"}]}
        raise AssertionError(f"unexpected request: {method} {path}")

    def clear_trace(self, session: Session) -> None:
        self.trace_users.append(session.user_id)


def _patch_prepare_dependencies(monkeypatch, pool, calls):
    _FakeSmokeClient.instances = []
    monkeypatch.setattr(prepare, "load_account_pool", lambda path, **kwargs: pool)
    monkeypatch.setattr(
        prepare,
        "load_golden_persona",
        lambda path: SimpleNamespace(
            fixture_sha256=PERSONA_SHA,
            source_fixture_sha256=SOURCE_SHA,
        ),
    )
    monkeypatch.setattr(
        prepare,
        "load_verified_source_fixture",
        lambda persona, path: ({"private_marker": PRIVATE_MARKER}, FIXTURE_SHA),
    )
    monkeypatch.setattr(
        prepare,
        "_deployment_receipt",
        lambda path, **kwargs: ({"verified": True}, DEPLOYMENT_SHA),
    )

    def verify_deployment(build_sha, receipt_path, **kwargs):
        payload = {
            "schema_version": 1,
            "expected_deployment_sha": build_sha,
            "expected_runtime": kwargs["expected_runtime"],
            "verified_at": "2026-01-01T00:00:00+00:00",
        }
        Path(receipt_path).write_text(json.dumps(payload), encoding="utf-8")
        Path(receipt_path).chmod(0o400)
        return payload

    monkeypatch.setattr(prepare, "verify_deployment", verify_deployment)
    monkeypatch.setattr(prepare, "SmokeClient", _FakeSmokeClient)

    def capture(**kwargs):
        user_id = kwargs["user_id"]
        calls["capture"].append(user_id)
        assert kwargs["fixture"] == {"private_marker": PRIVATE_MARKER}
        evidence_sha = hashlib.sha256(f"evidence:{user_id}".encode()).hexdigest()
        return {"evidence_sha256": evidence_sha}

    def finalize(**kwargs):
        evidence_name = Path(kwargs["private_evidence_path"]).name
        fingerprint = evidence_name.removeprefix("evidence-").removesuffix(".json")
        user_id = next(
            session.user_id
            for _profile, session in pool.rows
            if hashlib.sha256(session.user_id.encode()).hexdigest() == fingerprint
        )
        calls["finalize"].append(user_id)
        return {
            "ok": True,
            "evidence_sha256": hashlib.sha256(
                f"evidence:{user_id}".encode()
            ).hexdigest(),
            "fixture_sha256": FIXTURE_SHA,
            "checks": {
                "archive_receipts_verified": True,
                "genesis_upload_metadata_verified": True,
                "identity_envelope_decrypted": True,
                "persona_envelope_decrypted": True,
                "memory_envelopes_decrypted": True,
                "chat_envelopes_decrypted": True,
            },
        }

    monkeypatch.setattr(
        prepare.genesis_e2e, "capture_existing_session_distill_evidence", capture
    )
    monkeypatch.setattr(
        prepare.genesis_e2e,
        "finalize_existing_session_import_readiness",
        finalize,
    )


def _prepare_argv(
    pool: live_accounts.AccountPool,
    work_dir: Path,
    artifacts: Path,
    output: Path,
) -> list[str]:
    return [
        "prepare",
        "--account-pool",
        str(pool.path),
        "--build-sha",
        BUILD_SHA,
        "--deployment-receipt",
        str(pool.path.parent / "deployment.json"),
        "--post-deployment-receipt",
        str(pool.path.parent / "deployment-post.json"),
        "--persona",
        str(pool.path.parent / "persona.json"),
        "--source-fixture",
        str(pool.path.parent / "fixture.json"),
        "--work-dir",
        str(work_dir),
        "--artifact-dir",
        str(artifacts),
        "--readiness-receipt",
        str(output),
        "--concurrency",
        "1",
        "--poll",
        "0",
    ]


def test_prepare_success_imports_finalizes_and_clears_every_account_without_secrets(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private")
    work_dir = _private_dir(tmp_path, "work")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    output = private / "readiness.json"
    pool = _pool(private)
    calls = {"capture": [], "finalize": []}
    _patch_prepare_dependencies(monkeypatch, pool, calls)

    exit_code = prepare.main(_prepare_argv(pool, work_dir, artifacts, output))

    assert exit_code == 0
    assert sorted(calls["capture"]) == sorted(
        session.user_id for _profile, session in pool.rows
    )
    assert sorted(calls["finalize"]) == sorted(calls["capture"])
    assert len(_FakeSmokeClient.instances) == len(pool.rows)
    assert all(len(client.calls) == 4 for client in _FakeSmokeClient.instances)
    assert sorted(
        user_id
        for client in _FakeSmokeClient.instances
        for user_id in client.trace_users
    ) == sorted(calls["capture"])

    receipt, _digest = live_accounts.read_private_json(
        output, label="account readiness receipt"
    )
    encoded = json.dumps(receipt, sort_keys=True)
    assert receipt["all_ready"] is True
    assert receipt["account_fingerprints"] == list(pool.account_fingerprints)
    assert receipt["account_count"] == len(pool.rows)
    assert PRIVATE_MARKER not in encoded
    for _profile, session in pool.rows:
        assert session.user_id not in encoded
        assert session.api_key not in encoded
        assert session.sk.hex() not in encoded
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True


def test_prepare_account_failure_cleans_entire_pool_and_writes_no_receipt(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private-failure")
    work_dir = _private_dir(tmp_path, "work-failure")
    artifacts = tmp_path / "artifacts-failure"
    artifacts.mkdir()
    output = private / "readiness.json"
    pool = _pool(private)
    calls = {"capture": [], "finalize": [], "cleanup": []}
    _patch_prepare_dependencies(monkeypatch, pool, calls)
    original_capture = prepare.genesis_e2e.capture_existing_session_distill_evidence

    def fail_second(**kwargs):
        if kwargs["user_id"] == pool.rows[1][1].user_id:
            Path(kwargs["private_evidence_path"]).write_text(
                PRIVATE_MARKER, encoding="utf-8"
            )
            raise prepare.genesis_e2e.ExistingSessionDistillError(
                "capture", "injected_failure"
            )
        return original_capture(**kwargs)

    def cleanup_snapshot(manifest, path, identity, *, env, delete_manifest):
        calls["cleanup"].append((manifest, path, identity, env, delete_manifest))
        return {
            "manifest_deleted": True,
            "failed_profile_ids": [],
        }

    monkeypatch.setattr(
        prepare.genesis_e2e, "capture_existing_session_distill_evidence", fail_second
    )
    monkeypatch.setattr(prepare, "cleanup_manifest_snapshot", cleanup_snapshot)

    exit_code = prepare.main(_prepare_argv(pool, work_dir, artifacts, output))

    assert exit_code == 2
    assert len(calls["cleanup"]) == 1
    assert calls["cleanup"][0][:3] == (
        pool.manifest,
        pool.path,
        pool.file_identity,
    )
    assert calls["cleanup"][0][4] is True
    assert output.exists() is False
    assert list(work_dir.iterdir()) == []
    error = json.loads(capsys.readouterr().err)
    assert "full cleanup completed" in error["detail"]
    assert PRIVATE_MARKER not in error["detail"]


def test_prepare_failure_reports_retained_private_evidence(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private-retained-evidence")
    work_dir = _private_dir(tmp_path, "work-retained-evidence")
    artifacts = tmp_path / "artifacts-retained-evidence"
    artifacts.mkdir()
    output = private / "readiness.json"
    pool = _pool(private)
    calls = {"capture": [], "finalize": [], "cleanup": []}
    _patch_prepare_dependencies(monkeypatch, pool, calls)

    def fail_capture(**kwargs):
        Path(kwargs["private_evidence_path"]).write_text(
            PRIVATE_MARKER, encoding="utf-8"
        )
        raise prepare.genesis_e2e.ExistingSessionDistillError(
            "capture", "injected_failure"
        )

    monkeypatch.setattr(
        prepare.genesis_e2e,
        "capture_existing_session_distill_evidence",
        fail_capture,
    )
    monkeypatch.setattr(
        prepare,
        "cleanup_manifest_snapshot",
        lambda manifest, path, identity, *, env, delete_manifest: {
            "manifest_deleted": True,
            "failed_profile_ids": [],
        },
    )
    monkeypatch.setattr(prepare, "_best_effort_delete_evidence", lambda path: False)

    exit_code = prepare.main(_prepare_argv(pool, work_dir, artifacts, output))

    assert exit_code == 2
    error = json.loads(capsys.readouterr().err)
    assert "private evidence retained" in error["detail"]
    assert "full cleanup completed" not in error["detail"]
    assert PRIVATE_MARKER not in error["detail"]
    assert list(work_dir.glob("evidence-*.json"))


def test_prepare_rejects_backend_only_runtime_before_import_mutation(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private-baseline-runtime")
    work_dir = _private_dir(tmp_path, "work-baseline-runtime")
    artifacts = tmp_path / "artifacts-baseline-runtime"
    artifacts.mkdir()
    pool = replace(_pool(private), deployment_runtime="deployed_current")
    monkeypatch.setattr(
        prepare, "load_account_pool", lambda path, **kwargs: pool
    )

    def mutation_must_not_start(*_args, **_kwargs):
        raise AssertionError("source loading or import mutation must not start")

    monkeypatch.setattr(prepare, "load_golden_persona", mutation_must_not_start)
    monkeypatch.setattr(
        prepare.genesis_e2e,
        "capture_existing_session_distill_evidence",
        mutation_must_not_start,
    )

    code = prepare.main(
        _prepare_argv(pool, work_dir, artifacts, private / "readiness.json")
    )

    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert "per-account Hosted Runtime V2 readback" in error["detail"]


def test_cleanup_command_writes_complete_hash_bound_receipt(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private-cleanup")
    output = private / "cleanup.json"
    pool = _pool(private)
    monkeypatch.setattr(prepare, "load_account_pool", lambda path, **kwargs: pool)
    monkeypatch.setattr(
        prepare,
        "cleanup_manifest_snapshot",
        lambda manifest, path, identity, *, env, delete_manifest: {
            "attempted": len(pool.rows),
            "cleaned": len(pool.rows),
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": False,
            "manifest_retained": True,
            "cleanup_accounts": _cleanup_rows(pool),
        },
    )

    exit_code = prepare.main(
        [
            "cleanup",
            "--account-pool",
            str(pool.path),
            "--receipt",
            str(output),
        ]
    )

    assert exit_code == 0
    receipt, _digest = live_accounts.verify_cleanup_receipt(
        output,
        expected_pool_manifest_sha256=pool.manifest_sha256,
        expected_route_sha256=pool.route_sha256,
        expected_account_fingerprints=pool.account_fingerprints,
    )
    assert receipt["complete"] is True
    assert receipt["attempted"] == len(pool.rows)
    assert receipt["cleaned"] == len(pool.rows)
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cleanup_receipt_publication_can_resume_from_durable_outcome(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private-cleanup-recovery")
    output = private / "cleanup.json"
    pool = _pool(private)
    calls = {"cleanup": 0}
    monkeypatch.setattr(
        prepare, "load_account_pool", lambda path, **kwargs: pool
    )

    def cleanup_snapshot(manifest, path, identity, *, env, delete_manifest):
        calls["cleanup"] += 1
        return {
            "attempted": len(pool.rows),
            "cleaned": len(pool.rows),
            "failed_profile_ids": [],
            "manifest_deleted": False,
            "manifest_missing": False,
            "manifest_retained": True,
            "cleanup_accounts": _cleanup_rows(pool),
        }

    monkeypatch.setattr(prepare, "cleanup_manifest_snapshot", cleanup_snapshot)
    real_create = prepare.create_private_file

    def fail_final_receipt(path, content=b""):
        if path == output:
            raise prepare.AtomicPrivateFileError("injected publication failure")
        real_create(path, content)

    monkeypatch.setattr(prepare, "create_private_file", fail_final_receipt)
    argv = [
        "cleanup",
        "--account-pool",
        str(pool.path),
        "--receipt",
        str(output),
    ]

    assert prepare.main(argv) == 2
    capsys.readouterr()
    pending, outcome = prepare._cleanup_recovery_paths(pool.path)
    assert pool.path.exists() is False
    assert pending.exists() is False
    assert outcome.is_file()
    assert output.exists() is False

    monkeypatch.setattr(prepare, "create_private_file", real_create)
    assert prepare.main(argv) == 0
    assert calls["cleanup"] == 1
    assert outcome.exists() is False
    assert output.is_file()
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_incomplete_cleanup_retains_recovery_manifest_for_retry(
    tmp_path: Path, monkeypatch, capsys
):
    private = _private_dir(tmp_path, "private-cleanup-partial")
    output = private / "cleanup.json"
    pool = _pool(private)
    monkeypatch.setattr(
        prepare, "load_account_pool", lambda path, **kwargs: pool
    )
    monkeypatch.setattr(
        prepare,
        "cleanup_manifest_snapshot",
        lambda manifest, path, identity, *, env, delete_manifest: {
            "attempted": len(pool.rows),
            "cleaned": len(pool.rows) - 1,
            "failed_profile_ids": ["official-openai"],
            "manifest_deleted": False,
            "manifest_missing": False,
            "cleanup_accounts": _cleanup_rows(pool, complete=False),
        },
    )

    assert (
        prepare.main(
            [
                "cleanup",
                "--account-pool",
                str(pool.path),
                "--receipt",
                str(output),
            ]
        )
        == 2
    )
    pending, outcome = prepare._cleanup_recovery_paths(pool.path)
    assert pool.path.exists() is False
    assert pending.is_file()
    assert outcome.exists() is False
    assert output.exists() is False
    assert "retained recovery manifest" in json.loads(capsys.readouterr().err)[
        "detail"
    ]


@pytest.mark.parametrize("unsafe", ["output", "workdir"])
def test_unsafe_output_or_workdir_fails_before_account_mutation(
    tmp_path: Path, monkeypatch, capsys, unsafe: str
):
    private = _private_dir(tmp_path, f"private-unsafe-{unsafe}")
    work_dir = _private_dir(tmp_path, f"work-unsafe-{unsafe}")
    artifacts = tmp_path / f"artifacts-unsafe-{unsafe}"
    artifacts.mkdir()
    output = private / "readiness.json"
    pool = _pool(private)
    if unsafe == "output":
        output.write_text("occupied", encoding="utf-8")
    else:
        (work_dir / "occupied").write_text("do not overwrite", encoding="utf-8")

    def mutation_must_not_start(*_args, **_kwargs):
        raise AssertionError("account loading or mutation must not start")

    monkeypatch.setattr(prepare, "load_account_pool", mutation_must_not_start)
    monkeypatch.setattr(
        prepare.genesis_e2e,
        "capture_existing_session_distill_evidence",
        mutation_must_not_start,
    )
    monkeypatch.setattr(
        prepare, "cleanup_manifest_snapshot", mutation_must_not_start
    )

    exit_code = prepare.main(_prepare_argv(pool, work_dir, artifacts, output))

    assert exit_code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "PreparationError"
