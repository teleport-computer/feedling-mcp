"""TEE 明文库独立 Alembic 链验证：表集齐全 + 版本表隔离（P2T1 / spec §4）。"""

import os

import psycopg


def _tee_conn():
    return psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True)


def test_tee_schema_has_all_tables():
    # agent_runtime_instances / agent_runtime_supervisor_heartbeats: 0002_drop_retired_
    # hosted_supervisor 曾按"V1 supervisor 已退役"把这两张表从这里的 want 集合里撤下
    # （当时还专门加了 "not in" 断言防止它们被误建回来）。0004_full_table_alignment
    # 按 2026-07-27 用户的决定把它们重新建回——实测 V1 在 RDS 侧仍然活着（prod 220 行
    # agent_runtime_instances + 1 行心跳，backend db.py:335 / agent_runtime/leases.py:51
    # 仍在写），TEE 全量对齐目标下不能留这个缺口。所以它们现在应当存在，"not in" 断言
    # 已删除，改为并入 want。
    want = {"server_config","global_blobs","users","user_blobs","user_logs",
            "perception_items","perception_daily","copytext_strings","copytext_meta",
            "genesis_import_jobs","genesis_import_outputs","chat_messages","memory_moments",
            "world_book_entries","frames","tee_replication_cursors",
            "tee_pending_device_migration","notify_relay_configs","notify_relay_logs",
            "agent_runtime_instances","agent_runtime_supervisor_heartbeats"}
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
    assert want <= {r[0] for r in rows}


def test_tee_version_table_is_isolated():
    with _tee_conn() as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE 'alembic%'").fetchall()
    assert {r[0] for r in rows} == {"alembic_tee_version"}
