from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from plaintext_shadow import config


_PATH = Path(__file__).parent.parent / "deploy" / "check-database-topology.py"
_SPEC = importlib.util.spec_from_file_location("check_database_topology", _PATH)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

PRIMARY = "postgresql://app:primary-secret@primary.invalid:5432/feedling"
SHADOW = "postgresql://shadow:shadow-secret@shadow.invalid:5432/feedling_plaintext"
OLD = "postgresql://legacy:legacy-secret@legacy.invalid:5432/feedling"


def test_gate_accepts_tee_primary_with_disabled_shadow():
    assert gate.check(schema="tee", primary=PRIMARY, shadow="", enabled="0").ok


def test_gate_accepts_enabled_independent_plaintext_shadow():
    assert gate.check(
        schema="tee", primary=PRIMARY, shadow=SHADOW, enabled="1"
    ).ok


def test_gate_rejects_enabled_alias():
    result = gate.check(
        schema="tee", primary=PRIMARY, shadow=PRIMARY, enabled="1"
    )
    assert result.slug == "primary_shadow_alias"


def test_gate_rejects_legacy_shadow_in_tee_mode():
    result = gate.check(
        schema="tee",
        primary=PRIMARY,
        shadow="",
        enabled="0",
        tee_database_url=OLD,
        tee_dual_write="1",
    )
    assert result.slug == "stale_legacy_shadow_config"


@pytest.mark.parametrize("enabled", ["", "true", "false", "yes", "2"])
def test_gate_requires_literal_enable_value(enabled):
    assert gate.check(
        schema="tee", primary=PRIMARY, shadow=SHADOW, enabled=enabled
    ).slug == "invalid_plaintext_shadow_gate"


def test_gate_output_never_contains_credentials(capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PRIMARY)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", PRIMARY)
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")

    assert gate.main([]) == 2
    output = capsys.readouterr().out
    assert "primary-secret" not in output
    assert "shadow-secret" not in output
    assert "primary.invalid" not in output


@pytest.mark.parametrize(
    ("schema", "primary", "shadow", "enabled"),
    [
        ("tee", PRIMARY, "", "0"),
        ("tee", PRIMARY, SHADOW, "1"),
        ("rds", PRIMARY, "", "0"),
        ("tee", PRIMARY, PRIMARY, "1"),
        ("rds", PRIMARY, SHADOW, "1"),
    ],
)
def test_gate_and_runtime_configuration_rules_do_not_drift(
    monkeypatch, schema, primary, shadow, enabled
):
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", schema)
    monkeypatch.setenv("DATABASE_URL", primary)
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", shadow)
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", enabled)
    gate_ok = gate.check(
        schema=schema, primary=primary, shadow=shadow, enabled=enabled
    ).ok
    try:
        config.validate_startup()
        runtime_ok = True
    except RuntimeError:
        runtime_ok = False
    assert gate_ok == runtime_ok


def test_all_main_composes_expose_gate_and_all_runners_force_it_off():
    root = Path(__file__).parent.parent
    for name in (
        "docker-compose.phala.yaml",
        "docker-compose.phala.test.yaml",
        "docker-compose.phala.pre.yaml",
    ):
        text = (root / "deploy" / name).read_text()
        assert 'PLAINTEXT_SHADOW_DATABASE_URL: "${PLAINTEXT_SHADOW_DATABASE_URL:-}"' in text
        assert "FEEDLING_PLAINTEXT_SHADOW_ENABLED" in text
        assert "FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY" in text
    for name in (
        "docker-compose.phala.runner.yaml",
        "docker-compose.phala.pre.runner.yaml",
        "docker-compose.phala.prod.runner.yaml",
    ):
        text = (root / "deploy" / name).read_text()
        assert 'FEEDLING_PLAINTEXT_SHADOW_ENABLED: "0"' in text
        assert "PLAINTEXT_SHADOW_DATABASE_URL:" not in text


def test_prod_gate_two_is_protected_and_required_by_deploy_job():
    workflow = (
        Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
    ).read_text()
    assert "prod-plaintext-shadow-gate2:" in workflow
    assert "environment: prod-plaintext-shadow-gate2" in workflow
    assert "verify --require-green" in workflow
    assert "prod-plaintext-shadow-gate2-${{ github.sha }}" in workflow
    assert "prod-plaintext-shadow-gate2]" in workflow
