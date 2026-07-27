#!/usr/bin/env python3
"""从 RDS 实库派生 TEE 侧 DDL 草稿。

**产出是草稿，必须人工审校后再落进 alembic_tee revision。** 脚本做不了的判断：
- 信封列在 TEE 侧是明文（CIPHERTEXT lane）还是原样保留，以及对应的 CHECK 约束
  要不要重写——解密后 payload_envelope 变成明文 doc，原 CHECK 必然失效。
- per-user FK 要指向 TEE 自己的 users 表。
- 哪些索引值得带过去（TEE 是副本，读模式与主库不同）。

用法：
    python scripts/tee/derive_tee_ddl.py --dsn "$PROD_DATABASE_URL" > /tmp/ddl.sql
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from tee_shadow import table_registry as reg  # noqa: E402


def _columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default,
               is_identity, identity_generation
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    out = []
    for name, dtype, nullable, default, is_identity, id_gen in rows:
        frag = f"    {name} {dtype.upper()}"
        if is_identity == "YES":
            frag += f" GENERATED {id_gen} AS IDENTITY"
        elif default is not None:
            frag += f" DEFAULT {default}"
        if nullable == "NO":
            frag += " NOT NULL"
        out.append(frag)
    return out


def _pk(conn, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()

    want = [t for t in reg.synced_tables()]
    with psycopg.connect(args.dsn) as conn:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        for table in want:
            if table not in existing:
                print(f"-- SKIP {table}: 不在该 RDS 实库中", file=sys.stderr)
                continue
            cols = _columns(conn, table)
            pk = _pk(conn, table)
            print(f"\nCREATE TABLE IF NOT EXISTS {table} (")
            body = list(cols)
            if pk:
                body.append(f"    PRIMARY KEY ({', '.join(pk)})")
            has_user = any(c.strip().startswith("user_id ") for c in cols)
            if has_user:
                body.append(
                    "    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE")
            print(",\n".join(body))
            print(");")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
