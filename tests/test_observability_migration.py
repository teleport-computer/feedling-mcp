from __future__ import annotations
import importlib.util
from pathlib import Path

MIG = Path(__file__).parent.parent / "backend/alembic/versions/0012_observability.py"

def test_migration_file_exists_and_has_both_tables():
    assert MIG.exists(), "0012_observability.py 不存在"
    src = MIG.read_text()
    assert 'down_revision = "0011_world_book_entries"' in src
    assert "CREATE TABLE IF NOT EXISTS observability_samples" in src
    assert "CREATE TABLE IF NOT EXISTS agent_runtime_resource" in src
    # 关键趋势列必须存在
    for col in ("host_load1", "host_mem_avail_bytes", "backend_mem_bytes",
                "enclave_mem_bytes", "agentrunner_mem_bytes", "live_agents",
                "orphan", "errors", "db_conns", "backend_5xx", "extra"):
        assert col in src, f"缺列 {col}"
