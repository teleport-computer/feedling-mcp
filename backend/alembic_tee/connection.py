"""Pure connection selection for the independent TEE migration chain."""
from __future__ import annotations

import os


def migration_database_url() -> str:
    """Return the owner DSN for Alembic without ever falling back to primary."""
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
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url

