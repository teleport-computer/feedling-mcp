"""TEE plaintext database migration-chain helpers."""

from __future__ import annotations

from pathlib import Path


class MigrationHeadError(RuntimeError):
    """The checked-out TEE migration graph has no sole authoritative head."""


def _config():
    from alembic.config import Config

    here = Path(__file__).resolve().parent
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here))
    return cfg


def current_heads() -> tuple[str, ...]:
    """Return the heads declared by the migration files in this checkout."""
    from alembic.script import ScriptDirectory

    return tuple(ScriptDirectory.from_config(_config()).get_heads())


def current_head() -> str:
    """Return the sole TEE head, failing closed on a missing or split chain."""
    heads = current_heads()
    if len(heads) != 1:
        raise MigrationHeadError(
            "alembic_tee migration chain must have exactly one head: "
            f"found={list(heads)}"
        )
    return heads[0]


def upgrade_head() -> None:
    from alembic import command

    command.upgrade(_config(), "head")
