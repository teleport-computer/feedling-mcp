#!/usr/bin/env python3
"""Fail-closed, credential-free database topology deployment gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import NamedTuple

from psycopg.conninfo import conninfo_to_dict


class Result(NamedTuple):
    ok: bool
    slug: str
    primary_identity: str | None = None
    shadow_identity: str | None = None


def _identity(dsn: str) -> tuple[str, str, str]:
    try:
        parsed = conninfo_to_dict(dsn)
    except Exception as exc:
        raise ValueError("invalid database dsn") from exc
    host = (parsed.get("hostaddr") or parsed.get("host") or "").rstrip(".").lower()
    port = str(parsed.get("port") or "5432")
    database = parsed.get("dbname") or parsed.get("user") or ""
    return host, port, database


def _fingerprint(identity: tuple[str, str, str]) -> str:
    value = f"{identity[0]}:{identity[1]}/{identity[2]}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def check(
    *,
    schema: str,
    primary: str,
    shadow: str,
    enabled: str,
    tee_database_url: str = "",
    tee_dual_write: str = "",
) -> Result:
    if schema not in {"rds", "tee"}:
        return Result(False, "invalid_database_schema")
    if enabled not in {"0", "1"}:
        return Result(False, "invalid_plaintext_shadow_gate")
    if not primary.strip():
        return Result(False, "primary_database_missing")
    if schema == "tee" and (tee_database_url.strip() or tee_dual_write.strip()):
        return Result(False, "stale_legacy_shadow_config")
    if enabled == "0":
        return Result(True, "ok")
    if schema != "tee":
        return Result(False, "plaintext_shadow_requires_tee")
    if not shadow.strip():
        return Result(False, "plaintext_shadow_database_missing")
    try:
        primary_identity = _identity(primary)
        shadow_identity = _identity(shadow)
    except ValueError:
        return Result(False, "invalid_database_dsn")
    if primary_identity == shadow_identity:
        return Result(
            False,
            "primary_shadow_alias",
            _fingerprint(primary_identity),
            _fingerprint(shadow_identity),
        )
    return Result(
        True,
        "ok",
        _fingerprint(primary_identity),
        _fingerprint(shadow_identity),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default=os.environ.get("FEEDLING_DATABASE_SCHEMA", "rds"))
    parser.add_argument("--primary", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument(
        "--shadow", default=os.environ.get("PLAINTEXT_SHADOW_DATABASE_URL", "")
    )
    parser.add_argument(
        "--enabled",
        default=os.environ.get("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0"),
    )
    parser.add_argument("--tee-database-url", default=os.environ.get("TEE_DATABASE_URL", ""))
    parser.add_argument(
        "--tee-dual-write", default=os.environ.get("FEEDLING_TEE_DUAL_WRITE", "")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = check(
        schema=args.schema,
        primary=args.primary,
        shadow=args.shadow,
        enabled=args.enabled,
        tee_database_url=args.tee_database_url,
        tee_dual_write=args.tee_dual_write,
    )
    print(
        json.dumps(
            {
                "ok": result.ok,
                "slug": result.slug,
                "primary_identity": result.primary_identity,
                "shadow_identity": result.shadow_identity,
            },
            sort_keys=True,
        )
    )
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
