"""Both migration chains carry the identical derived-vector schema."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import psycopg
import db
from alembic.config import Config
from alembic.script import ScriptDirectory
from tee_shadow import table_registry


def test_vectors_are_primary_local_but_require_both_schemas():
    entry = table_registry.REGISTRY["memory_vectors"]
    assert entry.lane == table_registry.SKIP
    assert entry.tee_required


def test_both_chains_have_one_head_and_vectors_revision():
    root = Path(__file__).parent.parent / "backend"
    for chain, revision in [("alembic", "0111_memory_vectors"), ("alembic_tee", "0046_memory_vectors")]:
        cfg = Config()
        cfg.set_main_option("script_location", str(root / chain))
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1
        assert script.get_revision(revision) is not None


def test_vector_schema_on_primary_and_tee():
    schemas = []
    for url in [db._database_url(), os.environ["TEE_DATABASE_URL"]]:
        with psycopg.connect(url) as conn:
            cols = conn.execute("SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                "WHERE table_name='memory_vectors' ORDER BY ordinal_position").fetchall()
            assert [r[0] for r in cols] == ['user_id','moment_id','model_id','projection_hash','dim','vector','created_at']
            indexes = conn.execute("SELECT indexname FROM pg_indexes WHERE tablename='memory_vectors'").fetchall()
            assert ('memory_vectors_user_model_idx',) in indexes
            schemas.append(cols)
    assert schemas[0] == schemas[1]
