import os
from pathlib import Path
import subprocess
from textwrap import dedent

import pytest
import yaml

from tools.strict_yaml import load_yaml_strict


ROOT = Path(__file__).resolve().parents[1]
INGRESS_COMPOSES = (
    ROOT / "deploy" / "docker-compose.phala.yaml",
    ROOT / "deploy" / "docker-compose.phala.test.yaml",
    ROOT / "deploy" / "docker-compose.phala.pre.yaml",
)
PG_BACKUP_HEALTH = ROOT / "deploy" / "postgres" / "check-backup-health.sh"


def _run_pg_backup_health(mode: str, **values: str) -> subprocess.CompletedProcess[str]:
    assert PG_BACKUP_HEALTH.exists(), "PG backup health evaluator is missing"
    return subprocess.run(
        ["bash", str(PG_BACKUP_HEALTH), mode],
        capture_output=True,
        text=True,
        env={**os.environ, **values},
    )


def test_pg_backup_monitor_accepts_idle_archiver_when_r2_matches():
    archiver = _run_pg_backup_health(
        "archiver",
        LAST_ARCHIVED_WAL="00000001000002A8000000B1",
        CURRENT_WAL="00000001000002A8000000B2",
        UNRESOLVED_ARCHIVE_FAILURE="0",
        READY_COUNT="0",
        OLDEST_READY_AGE_SEC="0",
        WAL_MB="96",
    )
    assert archiver.returncode == 0, archiver.stderr

    r2 = _run_pg_backup_health(
        "r2",
        LAST_ARCHIVED_WAL="00000001000002A8000000B1",
        NEWEST_WAL_KEY="prod/wal-g/wal_005/00000001000002A8000000B1.lz4",
        BASE_AGE_SEC="3600",
    )
    assert r2.returncode == 0, r2.stderr


@pytest.mark.parametrize(
    ("mode", "values", "error_slug"),
    [
        (
            "archiver",
            {
                "LAST_ARCHIVED_WAL": "00000001000002A8000000B1",
                "CURRENT_WAL": "00000001000002A8000000B2",
                "UNRESOLVED_ARCHIVE_FAILURE": "1",
                "READY_COUNT": "0",
                "OLDEST_READY_AGE_SEC": "0",
                "WAL_MB": "96",
            },
            "ARCHIVE_FAILURE_UNRESOLVED",
        ),
        (
            "archiver",
            {
                "LAST_ARCHIVED_WAL": "00000001000002A8000000B1",
                "CURRENT_WAL": "00000001000002A8000000B2",
                "UNRESOLVED_ARCHIVE_FAILURE": "0",
                "READY_COUNT": "1",
                "OLDEST_READY_AGE_SEC": "600",
                "WAL_MB": "96",
            },
            "ARCHIVE_READY_STALE",
        ),
        (
            "archiver",
            {
                "LAST_ARCHIVED_WAL": "00000001000002A8000000B1",
                "CURRENT_WAL": "00000001000002A8000000B2",
                "UNRESOLVED_ARCHIVE_FAILURE": "0",
                "READY_COUNT": "0",
                "OLDEST_READY_AGE_SEC": "0",
                "WAL_MB": "4096",
            },
            "WAL_DIR_TOO_LARGE",
        ),
        (
            "r2",
            {
                "LAST_ARCHIVED_WAL": "00000001000002A8000000B1",
                "NEWEST_WAL_KEY": "prod/wal-g/wal_005/00000001000002A8000000B0.lz4",
                "BASE_AGE_SEC": "3600",
            },
            "R2_WAL_MISMATCH",
        ),
        (
            "r2",
            {
                "LAST_ARCHIVED_WAL": "00000001000002A8000000B1",
                "NEWEST_WAL_KEY": "prod/wal-g/wal_005/00000001000002A8000000B1.lz4",
                "BASE_AGE_SEC": "93600",
            },
            "BASE_BACKUP_STALE",
        ),
    ],
)
def test_pg_backup_monitor_rejects_real_backup_failures(mode, values, error_slug):
    result = _run_pg_backup_health(mode, **values)
    assert result.returncode != 0
    assert error_slug in result.stderr


def test_pg_backup_workflow_reconciles_database_wal_with_r2():
    path = ROOT / ".github" / "workflows" / "pg-monitor.yml"
    workflow = path.read_text()
    load_yaml_strict(workflow, source_name=str(path.relative_to(ROOT)))

    assert "archiver stale" not in workflow
    assert "pg_ls_archive_statusdir()" in workflow
    assert "LAST_ARCHIVED_WAL" in workflow
    assert "NEWEST_WAL_KEY" in workflow
    assert "check-backup-health.sh archiver" in workflow
    assert "check-backup-health.sh r2" in workflow


def test_strict_loader_rejects_duplicate_mapping_keys():
    source = dedent(
        """\
        services:
          worker:
            environment:
              FEATURE_FLAG: "0"
              FEATURE_FLAG: "1"
        """
    )

    with pytest.raises(yaml.constructor.ConstructorError) as exc_info:
        load_yaml_strict(source, source_name="duplicate.yaml")

    message = str(exc_info.value)
    assert "duplicate mapping key 'FEATURE_FLAG'" in message
    assert 'in "duplicate.yaml", line 5, column 7' in message
    assert 'in "duplicate.yaml", line 4, column 7' in message


def test_all_deploy_yaml_files_have_unique_mapping_keys():
    paths = sorted((ROOT / "deploy").glob("*.yaml"))
    paths += sorted((ROOT / "deploy").glob("*.yml"))
    assert paths

    for path in paths:
        load_yaml_strict(path.read_text(), source_name=str(path.relative_to(ROOT)))


def test_ci_runs_the_strict_deploy_yaml_gate():
    path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = load_yaml_strict(
        path.read_text(),
        source_name=str(path.relative_to(ROOT)),
    )
    steps = workflow["jobs"]["python-tests"]["steps"]

    assert any(
        "tests/test_deploy_yaml_strict.py" in step.get("run", "")
        for step in steps
    )


def test_test_environment_uses_literal_three_pool_runtime_values():
    path = ROOT / "deploy" / "docker-compose.phala.test.yaml"
    compose = load_yaml_strict(
        path.read_text(),
        source_name=str(path.relative_to(ROOT)),
    )
    environment = compose["services"]["serve-worker"]["environment"]

    assert environment | {
        "FEEDLING_V2_FOREGROUND_SLOTS": "2",
        "FEEDLING_V2_WAKE_SLOTS": "1",
        "FEEDLING_V2_HEAVY_SLOTS": "1",
        "FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY": "1",
        "FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY": "4",
    } == environment
    for key in (
        "FEEDLING_V2_FOREGROUND_SLOTS",
        "FEEDLING_V2_WAKE_SLOTS",
        "FEEDLING_V2_HEAVY_SLOTS",
        "FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY",
        "FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY",
    ):
        assert "${" not in environment[key]
    for retired in (
        "FEEDLING_V2_POOL_MODE",
        "FEEDLING_V2_MAX_WORKERS",
        "FEEDLING_V2_CHAT_PREEMPTION_ENABLED",
        "FEEDLING_V2_SLOT_PROCESS_ISOLATION",
    ):
        assert retired not in environment


def test_test_environment_attests_incremental_chat_sync_with_256_row_hot_cache():
    """Removing either literal would silently restore legacy 5k reloads."""
    path = ROOT / "deploy" / "docker-compose.phala.test.yaml"
    compose = load_yaml_strict(
        path.read_text(),
        source_name=str(path.relative_to(ROOT)),
    )

    for service_name in ("backend", "serve-worker"):
        environment = compose["services"][service_name]["environment"]
        assert environment["FEEDLING_CHAT_SYNC_MODE"] == "incremental"
        assert environment["FEEDLING_CHAT_HOT_CACHE_LIMIT"] == "256"
        assert environment["FEEDLING_STORE_LOAD_MODE"] == "legacy"
        assert "${" not in environment["FEEDLING_CHAT_SYNC_MODE"]
        assert "${" not in environment["FEEDLING_CHAT_HOT_CACHE_LIMIT"]
        assert "${" not in environment["FEEDLING_STORE_LOAD_MODE"]


def test_prod_environment_attests_incremental_chat_sync_with_256_row_hot_cache():
    """PROD must not silently fall back to legacy 5k chat reloads."""
    path = ROOT / "deploy" / "docker-compose.phala.yaml"
    compose = load_yaml_strict(
        path.read_text(),
        source_name=str(path.relative_to(ROOT)),
    )

    for service_name in ("backend", "serve-worker"):
        environment = compose["services"][service_name]["environment"]
        assert environment["FEEDLING_CHAT_SYNC_MODE"] == "incremental"
        assert environment["FEEDLING_CHAT_HOT_CACHE_LIMIT"] == "256"
        assert environment["FEEDLING_STORE_LOAD_MODE"] == "legacy"
        assert "${" not in environment["FEEDLING_CHAT_SYNC_MODE"]
        assert "${" not in environment["FEEDLING_CHAT_HOT_CACHE_LIMIT"]
        assert "${" not in environment["FEEDLING_STORE_LOAD_MODE"]


def _ingress_entrypoint(path: Path) -> str:
    compose = load_yaml_strict(
        path.read_text(), source_name=str(path.relative_to(ROOT))
    )
    entrypoint = compose["services"]["ingress"]["entrypoint"]
    assert entrypoint[:2] == ["/bin/bash", "-euo"]
    assert entrypoint[2] == "pipefail"
    assert entrypoint[3:5] == ["-c", entrypoint[4]]
    # Compose escapes a literal container-side dollar as ``$$``.
    return entrypoint[4].replace("$$", "$")


def _make_certificate(tmp_path: Path, *, stem: str, days: int = 30) -> tuple[Path, Path]:
    cert = tmp_path / f"{stem}.pem"
    key = tmp_path / f"{stem}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={stem}.example.test",
            "-days",
            str(days),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert, key


def _run_ingress_entrypoint(
    tmp_path: Path,
    *,
    certificate: Path,
    private_key: Path,
    min_validity_sec: int,
) -> subprocess.CompletedProcess[str]:
    domain = "api.example.test"
    live = tmp_path / "letsencrypt" / "live" / domain
    live.mkdir(parents=True, exist_ok=True)
    (live / "fullchain.pem").unlink(missing_ok=True)
    (live / "privkey.pem").unlink(missing_ok=True)
    (live / "fullchain.pem").symlink_to(certificate)
    (live / "privkey.pem").symlink_to(private_key)
    upstream = tmp_path / "upstream.sh"
    upstream.write_text("#!/bin/sh\nprintf 'upstream:%s\\n' \"$*\"\n")
    upstream.chmod(0o755)
    env = {
        **os.environ,
        "DOMAINS": domain,
        "INGRESS_CERT_ROOT": str(tmp_path / "letsencrypt"),
        "INGRESS_BOOTSTRAP_MARKER": str(tmp_path / "bootstrapped"),
        "INGRESS_VENV_MARKER": str(tmp_path / "venv_bootstrapped"),
        "INGRESS_CERTBOT_BIN": str(upstream),
        "INGRESS_UPSTREAM_ENTRYPOINT": str(upstream),
        "INGRESS_CERT_MIN_VALIDITY_SEC": str(min_validity_sec),
    }
    script = _ingress_entrypoint(INGRESS_COMPOSES[0])
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script, "--", "haproxy", "-W"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def test_all_ingress_services_reuse_only_valid_matching_persisted_certificates(
    tmp_path,
):
    scripts = [_ingress_entrypoint(path) for path in INGRESS_COMPOSES]
    assert len(set(scripts)) == 1

    cert, key = _make_certificate(tmp_path, stem="valid", days=30)
    result = _run_ingress_entrypoint(
        tmp_path,
        certificate=cert,
        private_key=key,
        min_validity_sec=7 * 24 * 60 * 60,
    )
    assert result.stdout.splitlines() == [
        "Reusing persisted ingress certificates; renewal remains background-managed",
        "upstream:haproxy -W",
    ]
    assert (tmp_path / "bootstrapped").exists()
    assert (tmp_path / "venv_bootstrapped").exists()


def test_ingress_does_not_skip_bootstrap_for_expiring_or_mismatched_certificates(
    tmp_path,
):
    expiring_cert, expiring_key = _make_certificate(
        tmp_path, stem="expiring", days=1
    )
    _run_ingress_entrypoint(
        tmp_path,
        certificate=expiring_cert,
        private_key=expiring_key,
        min_validity_sec=7 * 24 * 60 * 60,
    )
    assert not (tmp_path / "bootstrapped").exists()

    valid_cert, _valid_key = _make_certificate(tmp_path, stem="valid", days=30)
    _other_cert, other_key = _make_certificate(tmp_path, stem="other", days=30)
    _run_ingress_entrypoint(
        tmp_path,
        certificate=valid_cert,
        private_key=other_key,
        min_validity_sec=7 * 24 * 60 * 60,
    )
    assert not (tmp_path / "bootstrapped").exists()
