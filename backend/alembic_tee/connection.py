"""Pure connection selection for the independent TEE migration chain."""
from __future__ import annotations

import os


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def migration_database_url() -> str:
    """Return the Alembic DSN for the selected database topology."""
    if os.environ.get("FEEDLING_DATABASE_SCHEMA", "rds").strip().lower() == "tee":
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("TEE primary database URL is not set")
        return _normalize_postgres_url(url)

    url = (
        os.environ.get("PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL", "").strip()
        or os.environ.get("TEE_MIGRATION_DATABASE_URL", "").strip()
        or os.environ.get("TEE_DATABASE_URL", "").strip()
    )
    if not url:
        raise RuntimeError(
            "TEE migration database URL is not set; configure the plaintext "
            "or legacy owner migration variable"
        )
    return _normalize_postgres_url(url)
