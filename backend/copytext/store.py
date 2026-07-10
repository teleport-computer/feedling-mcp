"""Self-contained DB access for server-managed UI copy.

Keeps all copytext SQL inside the feature module (db.py is untouched). Reuses
the shared connection pool from db.get_pool() (autocommit). Two tables, added
by migration 0006:

  copytext_strings — one row per (key, lang).
  copytext_meta    — single-row revision counter, bumped on every write.
"""
from __future__ import annotations

import logging
import time

from db import get_pool

log = logging.getLogger("copytext.store")

LANGS = ("en", "zh-Hans")


def get_revision() -> int:
    """Current bundle revision (0 if the meta row is somehow absent)."""
    try:
        with get_pool().connection() as conn:
            row = conn.execute(
                "SELECT revision FROM copytext_meta WHERE id = TRUE"
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001
        log.error("get_revision failed: %s", e)
        return 0


def get_all() -> dict[str, dict[str, str]]:
    """Return every managed string as {key: {lang: value}}."""
    try:
        with get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT key, lang, value FROM copytext_strings"
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.error("get_all failed: %s", e)
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, lang, value in rows:
        out.setdefault(key, {})[lang] = value
    return out


def apply_edits(
    strings: dict[str, dict[str, str]] | None,
    delete: list[str] | None,
) -> tuple[int, int, int]:
    """Upsert the given (key, lang) values and delete the listed keys, then bump
    the revision. Runs in one transaction so the revision and the data move
    together. Returns (new_revision, upserted_rows, deleted_keys).

    Callers are responsible for validation (see service.apply_edits).
    """
    strings = strings or {}
    delete = delete or []
    now = time.time()
    upserted = 0
    deleted = 0
    mirror_group: list[tuple[str, tuple]] = []

    upsert_sql = ("INSERT INTO copytext_strings (key, lang, value, updated_at) "
                  "VALUES (%s, %s, %s, %s) "
                  "ON CONFLICT (key, lang) DO UPDATE "
                  "SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at")
    delete_sql = "DELETE FROM copytext_strings WHERE key = %s"
    bump_sql = "UPDATE copytext_meta SET revision = revision + 1 WHERE id = TRUE RETURNING revision"

    # One explicit transaction (the pool is autocommit by default, so open a
    # transaction block to keep writes + revision bump atomic).
    with get_pool().connection() as conn:
        with conn.transaction():
            for key, by_lang in strings.items():
                for lang, value in by_lang.items():
                    conn.execute(upsert_sql, (key, lang, value, now))
                    mirror_group.append((upsert_sql, (key, lang, value, now)))
                    upserted += 1
            for key in delete:
                cur = conn.execute(delete_sql, (key,))
                mirror_group.append((delete_sql, (key,)))
                # Count keys removed (not rows): a key spans up to len(LANGS) rows.
                if (cur.rowcount or 0) > 0:
                    deleted += 1
            row = conn.execute(bump_sql).fetchone()
            mirror_group.append((bump_sql, ()))
    new_rev = int(row[0]) if row else get_revision()
    from tee_shadow import mirror
    mirror.execute_many(mirror_group)
    return new_rev, upserted, deleted
