"""Pure connection selection for the independent TEE migration chain."""
from __future__ import annotations

import os
import re
from urllib.parse import quote, urlencode

# Anything that *starts* with a URL scheme is URL-form; a '://' appearing
# later (e.g. inside a libpq-quoted password) must not suppress conversion.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _keyword_dsn_to_url(dsn: str) -> str:
    """Convert a libpq keyword/value DSN into a SQLAlchemy psycopg URL.

    Production supplies the enclave DSN in libpq keyword form
    (``user=... password=... host=... sslmode=...``); SQLAlchemy's
    ``make_url`` only accepts URL syntax, so the boot-time migration
    crash-looped backend and serve-worker on 2026-09-02. Parse with
    psycopg's own conninfo parser — never hand-split — and fail closed
    on anything it rejects.
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = {
            k: str(v)
            for k, v in conninfo_to_dict(dsn).items()
            if v is not None
        }
    except Exception as exc:
        raise RuntimeError(
            "TEE migration DSN is in libpq keyword form but could not be "
            f"parsed: {exc}"
        ) from exc

    host = params.pop("host", "")
    port = params.pop("port", "")
    dbname = params.pop("dbname", "")
    user = params.pop("user", "")
    password = params.pop("password", "")

    auth = ""
    if user:
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    netloc = auth + host + (f":{port}" if port else "")
    query = urlencode(sorted(params.items()))
    url = (
        f"postgresql+psycopg://{netloc}/{quote(dbname, safe='')}"
        + (f"?{query}" if query else "")
    )

    # Fail closed on any shape the URL syntax cannot express losslessly
    # (unix-socket path hosts, bare IPv6, password without user, ...):
    # round-trip through SQLAlchemy's own parser and require field-exact
    # equality with what libpq gave us. A silent misroute is worse than a
    # loud refusal.
    from sqlalchemy.engine.url import make_url

    try:
        parsed = make_url(url)
    except Exception as exc:
        raise RuntimeError(
            "TEE migration DSN keyword form produced a URL SQLAlchemy "
            f"cannot parse (shape not expressible): {exc}"
        ) from exc
    mismatches = [
        name
        for name, got, want in (
            ("user", parsed.username or "", user),
            ("password", parsed.password or "", password),
            ("host", parsed.host or "", host),
            ("port", str(parsed.port) if parsed.port else "", port),
            ("dbname", parsed.database or "", dbname),
        )
        if got != want
    ]
    if mismatches:
        raise RuntimeError(
            "TEE migration DSN keyword form cannot be losslessly expressed "
            f"as a URL (fields {mismatches} would change meaning); refusing"
        )
    return url


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if "=" in url and not _URL_SCHEME_RE.match(url):
        return _keyword_dsn_to_url(url)
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
