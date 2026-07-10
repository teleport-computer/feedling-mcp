"""0014 迁移落地：四张 V2 表 + single-flight 唯一索引真的存在且生效。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory


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


def test_v2_job_liveness_columns_exist():
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_jobs' "
            "AND column_name IN ('input_generation','lease_expires_at','queue_deadline_at')"
        ).fetchall()
    assert {row[0] for row in rows} == {
        "input_generation", "lease_expires_at", "queue_deadline_at",
    }


def test_migration_graph_preserves_deployed_v2_history_and_merges_profiles():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    assert script.get_revision("0014_hosted_runtime_v2").down_revision == (
        "0013_genesis_resident_claim"
    )
    assert set(script.get_revision("0021_merge_v2_profiles").down_revision) == {
        "0020_v2_heartbeat_kind",
        "0014_model_api_profiles",
    }
    assert script.get_current_head() == "0024_v2_worker_capacity"


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
