"""Alembic migration environment for the TEE shadow (plaintext) database.

Independent chain from backend/alembic/env.py: reads the connection string
from TEE_MIGRATION_DATABASE_URL (falling back to TEE_DATABASE_URL) instead of
DATABASE_URL, and stamps its own version table (alembic_tee_version) so this
chain's bookkeeping never collides with the RDS ciphertext-envelope chain's
alembic_version table — the two run against different databases in
production, but tests/conftest.py mirrors the isolation with a dedicated
version_table anyway for defense in depth. Migrations are hand-written (no ORM
models), so target_metadata is None and autogenerate is not used.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

# No ORM model metadata — migrations are authored by hand.
target_metadata = None

VERSION_TABLE = "alembic_tee_version"


def _database_url() -> str:
    url = (
        os.environ.get("TEE_MIGRATION_DATABASE_URL", "").strip()
        or os.environ.get("TEE_DATABASE_URL", "").strip()
    )
    if not url:
        raise RuntimeError(
            "TEE_MIGRATION_DATABASE_URL (or TEE_DATABASE_URL) is not set — "
            "Alembic needs it to connect to the TEE shadow Postgres (must "
            "include sslmode=require for external Postgres)."
        )
    # SQLAlchemy needs an explicit driver; the app uses the bare postgresql://
    # scheme with the psycopg(3) pool. Map both schemes to the psycopg3 dialect.
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
