"""0014 迁移落地：四张 V2 表 + single-flight 唯一索引真的存在且生效。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import psycopg


def _seed_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_v2_tables_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN "
            "('agent_jobs','agent_action_queue','agent_status_events','runtime_state')"
        ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"agent_jobs", "agent_action_queue", "agent_status_events", "runtime_state"}


def test_singleflight_unique_index_enforced():
    _seed_user("u_mig_1")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status) VALUES ('u_mig_1','chat','pending')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO agent_jobs (user_id, lane, status) VALUES ('u_mig_1','chat','pending')"
            )
    # cleanup so the shared session DB stays clean for later modules
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id='u_mig_1'")
