from __future__ import annotations

import pytest

from plaintext_shadow import config


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DATABASE_URL",
        "FEEDLING_DATABASE_SCHEMA",
        "PLAINTEXT_SHADOW_DATABASE_URL",
        "FEEDLING_PLAINTEXT_SHADOW_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_shadow_has_no_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/zero gate must never open a shadow merely because a DSN exists."""
    _clear(monkeypatch)
    monkeypatch.setenv(
        "PLAINTEXT_SHADOW_DATABASE_URL",
        "postgresql://shadow_app:synthetic@shadow.example/feedling",
    )

    assert config.load_target() is None

    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0")
    assert config.load_target() is None


def test_enabled_shadow_requires_a_target_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate typo must fail startup instead of producing a half-enabled topology."""
    _clear(monkeypatch)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")

    with pytest.raises(RuntimeError, match="PLAINTEXT_SHADOW_DATABASE_URL is required"):
        config.validate_startup()


@pytest.mark.parametrize("raw", ["true", "yes", "2", " 1 "])
def test_shadow_gate_accepts_only_literal_zero_or_one(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """Loose truthiness can accidentally activate a plaintext recipient."""
    _clear(monkeypatch)
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", raw)

    with pytest.raises(RuntimeError, match="must be exactly '0' or '1'"):
        config.validate_startup()


def test_enabled_shadow_requires_tee_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new direction must not run on the legacy managed-primary topology."""
    _clear(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://primary_app:a@primary.example/feedling")
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", "postgresql://shadow_app:b@shadow.example/feedling")
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")

    with pytest.raises(RuntimeError, match="requires FEEDLING_DATABASE_SCHEMA=tee"):
        config.validate_startup()


def test_primary_shadow_alias_ignores_credentials_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different roles/options do not make two DSNs independent databases."""
    _clear(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://primary_app:a@db.example:5432/feedling?sslmode=require",
    )
    monkeypatch.setenv(
        "PLAINTEXT_SHADOW_DATABASE_URL",
        "postgresql://shadow_app:b@db.example:5432/feedling?application_name=shadow",
    )
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")

    with pytest.raises(RuntimeError, match="different PostgreSQL databases") as caught:
        config.validate_startup()

    message = str(caught.value)
    assert "primary_app" not in message
    assert "shadow_app" not in message
    assert "db.example" not in message


def test_distinct_tee_shadow_returns_plaintext_all_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid topology exposes exactly the fully-decrypted target policy."""
    _clear(monkeypatch)
    shadow_dsn = "postgresql://shadow_app:b@shadow.example:443/feedling?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", "postgresql://primary_app:a@primary.example/feedling")
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", shadow_dsn)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")

    config.validate_startup()
    target = config.load_target()

    assert target == config.TargetPolicy(dsn=shadow_dsn, mode="plaintext_all")

