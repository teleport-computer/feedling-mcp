"""TEE 明文库独立 Alembic 链验证：表集齐全 + 版本表隔离（P2T1 / spec §4）。"""

import os

import psycopg


def _tee_conn():
    return psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True)


def test_tee_schema_has_all_tables():
    want = {"server_config","global_blobs","users","user_blobs","user_logs",
            "perception_items","perception_daily","copytext_strings","copytext_meta",
            "genesis_import_jobs","genesis_import_outputs","agent_runtime_instances",
            "agent_runtime_supervisor_heartbeats","chat_messages","memory_moments",
            "world_book_entries","frames","tee_replication_cursors",
            "tee_pending_device_migration"}
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
    assert want <= {r[0] for r in rows}


def test_tee_version_table_is_isolated():
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'alembic%'").fetchall()
    assert {r[0] for r in rows} == {"alembic_tee_version"}
