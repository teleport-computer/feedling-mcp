"""Alembic migration environment for the TEE shadow (plaintext) database.

Independent chain from backend/alembic/env.py: reads the connection string
from PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL, then the legacy
TEE_MIGRATION_DATABASE_URL/TEE_DATABASE_URL pair, instead of DATABASE_URL, and
stamps its own version table (alembic_tee_version) so this
chain's bookkeeping never collides with the RDS ciphertext-envelope chain's
alembic_version table — the two run against different databases in
production, but tests/conftest.py mirrors the isolation with a dedicated
version_table anyway for defense in depth. Migrations are hand-written (no ORM
models), so target_metadata is None and autogenerate is not used.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from alembic_tee.connection import migration_database_url

config = context.config

# No ORM model metadata — migrations are authored by hand.
target_metadata = None

VERSION_TABLE = "alembic_tee_version"


def _database_url() -> str:
    return migration_database_url()


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
