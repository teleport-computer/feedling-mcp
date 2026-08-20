"""Configuration boundary for the TEE-primary plaintext shadow.

This module deliberately owns the new variable names.  The legacy
``TEE_DATABASE_URL`` path describes the opposite replication direction and is
not a fallback here.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Literal

from psycopg.conninfo import conninfo_to_dict


@dataclass(frozen=True)
class TargetPolicy:
    """One independently configured, fully decrypted shadow target."""

    dsn: str
    mode: Literal["plaintext_all"] = "plaintext_all"


def _gate_enabled() -> bool:
    raw = os.environ.get("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0")
    if raw not in {"0", "1"}:
        raise RuntimeError(
            "FEEDLING_PLAINTEXT_SHADOW_ENABLED must be exactly '0' or '1'"
        )
    return raw == "1"


def load_target() -> TargetPolicy | None:
    """Return the enabled target, failing closed on incomplete configuration."""
    if not _gate_enabled():
        return None
    dsn = os.environ.get("PLAINTEXT_SHADOW_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "PLAINTEXT_SHADOW_DATABASE_URL is required when the plaintext shadow is enabled"
        )
    return TargetPolicy(dsn=dsn)


def require_target() -> TargetPolicy:
    """Return the enabled target or fail with a credential-free message."""
    target = load_target()
    if target is None:
        raise RuntimeError("plaintext shadow is disabled")
    validate_startup()
    return target


def _database_identity(dsn: str) -> tuple[str, str, str]:
    try:
        parsed = conninfo_to_dict(dsn)
    except Exception as exc:  # psycopg can raise several parsing error classes
        raise RuntimeError("database DSN is invalid") from exc

    host = parsed.get("hostaddr") or parsed.get("host") or ""
    host = host.rstrip(".").lower()
    port = parsed.get("port") or "5432"
    dbname = parsed.get("dbname") or parsed.get("user") or ""
    return host, str(port), dbname


def same_database(a: str, b: str) -> bool:
    """Compare endpoint/database identity without credentials or options."""
    return _database_identity(a) == _database_identity(b)


def validate_startup() -> None:
    """Fail startup on an incomplete, reversed, or self-writing topology."""
    target = load_target()
    if target is None:
        return

    schema = os.environ.get("FEEDLING_DATABASE_SCHEMA", "rds")
    if schema != "tee":
        raise RuntimeError(
            "plaintext shadow requires FEEDLING_DATABASE_SCHEMA=tee"
        )

    primary_dsn = os.environ.get("DATABASE_URL", "").strip()
    if not primary_dsn:
        raise RuntimeError("DATABASE_URL is required when the plaintext shadow is enabled")
    if same_database(primary_dsn, target.dsn):
        raise RuntimeError(
            "DATABASE_URL and PLAINTEXT_SHADOW_DATABASE_URL must identify different PostgreSQL databases"
        )


def validate_live_topology(primary, shadow) -> None:
    """Prove two live connections do not address the same PostgreSQL database.

    Conninfo comparison is only an early typo guard: DNS aliases and database
    proxies can expose the same database through different client strings. A
    session advisory lock is scoped to a live database, so a second connection
    to that same database cannot acquire the challenge while the source holds
    it. Different databases, including databases on one cluster, have separate
    advisory-lock namespaces.
    """
    namespace = 0x504C5348  # "PLSH", signed-int32 safe and content-free.
    challenge = secrets.randbelow(2**31 - 1)
    source_locked = False
    shadow_locked = False
    try:
        primary.execute("SELECT pg_advisory_lock(%s, %s)", (namespace, challenge))
        source_locked = True
        row = shadow.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (namespace, challenge)
        ).fetchone()
        shadow_locked = bool(row and row[0])
        if not shadow_locked:
            raise RuntimeError(
                "primary and plaintext shadow resolve to the same live PostgreSQL database"
            )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("live PostgreSQL identity check failed") from exc
    finally:
        if shadow_locked:
            try:
                shadow.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", (namespace, challenge)
                )
            except Exception:
                pass
        if source_locked:
            try:
                primary.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", (namespace, challenge)
                )
            except Exception:
                pass


def validate_live_startup() -> None:
    """Run static and live topology checks before an enabled process starts."""
    validate_startup()
    target = load_target()
    if target is None:
        return
    import psycopg

    primary_dsn = os.environ["DATABASE_URL"].strip()
    try:
        with psycopg.connect(primary_dsn, connect_timeout=10) as primary, psycopg.connect(
            target.dsn, connect_timeout=10
        ) as shadow:
            validate_live_topology(primary, shadow)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("live PostgreSQL identity check failed") from exc
