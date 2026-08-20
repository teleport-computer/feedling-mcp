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
