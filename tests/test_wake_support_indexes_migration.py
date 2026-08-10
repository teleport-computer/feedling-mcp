from __future__ import annotations

import importlib.util
from pathlib import Path

import db


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "backend" / "alembic" / "versions" / "0084_wake_support_indexes.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0084_wake_support_indexes", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wake_support_index_predicates_cover_every_runtime_wake_lane():
    migration = _migration_module()

    from model_api_runtime.v2 import worker as v2_worker

    runtime_lanes = set(v2_worker._WAKE_LANES)
    assert set(migration._WAKE_LANES) == runtime_lanes
    for sql in migration._INDEXES.values():
        assert all(f"'{lane}'" in sql for lane in v2_worker._WAKE_LANES)


def test_wake_support_indexes_are_concurrent_partial_user_first_paths():
    migration = _migration_module()

    assert migration.down_revision == "0083_screen_chat_frames"
    assert set(migration._INDEXES) == {
        "ix_agent_jobs_user_wake_created",
        "ix_agent_jobs_user_wake_failure",
    }

    created = migration._INDEXES["ix_agent_jobs_user_wake_created"]
    assert "CREATE INDEX CONCURRENTLY" in created
    assert "(user_id, created_at DESC)" in created
    assert "WHERE lane IN" in created

    failures = migration._INDEXES["ix_agent_jobs_user_wake_failure"]
    assert "CREATE INDEX CONCURRENTLY" in failures
    assert "(user_id, finished_at DESC, id DESC)" in failures
    assert "WHERE lane IN" in failures
    assert "status IN ('failed','expired')" in failures


def test_wake_support_indexes_are_installed_and_valid():
    migration = _migration_module()

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT cls.relname, idx.indisvalid, pg_get_indexdef(idx.indexrelid) "
            "FROM pg_class AS cls "
            "JOIN pg_index AS idx ON idx.indexrelid=cls.oid "
            "WHERE cls.relname = ANY(%s)",
            (list(migration._INDEXES),),
        ).fetchall()

    installed = {
        str(name): (bool(valid), str(definition))
        for name, valid, definition in rows
    }
    assert set(installed) == set(migration._INDEXES)
    assert all(valid for valid, _definition in installed.values())
    assert "(user_id, created_at DESC)" in installed[
        "ix_agent_jobs_user_wake_created"
    ][1]
    assert "(user_id, finished_at DESC, id DESC)" in installed[
        "ix_agent_jobs_user_wake_failure"
    ][1]
