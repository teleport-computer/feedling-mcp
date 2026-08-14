from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).parent.parent


def _scripts(tree: str) -> ScriptDirectory:
    ini = "alembic.ini" if tree == "alembic" else "alembic_tee/alembic.ini"
    cfg = Config(str(ROOT / "backend" / ini))
    cfg.set_main_option("script_location", str(ROOT / "backend" / tree))
    return ScriptDirectory.from_config(cfg)


def test_rds_pre_and_test_heads_converge():
    script = _scripts("alembic")
    assert script.get_heads() == ["0088_merge_pre_test_heads"]
    assert set(script.get_revision("0088_merge_pre_test_heads").down_revision) == {
        "0086_merge_voice_wake",
        "0087_v2_first_chat_activation",
    }


def test_tee_chain_carries_test_runtime_schema():
    script = _scripts("alembic_tee")
    assert script.get_heads() == ["0020_v2_first_chat_activation"]
    assert (
        script.get_revision("0020_v2_first_chat_activation").down_revision
        == "0019_v2_worker_pool_heartbeats"
    )
    assert (
        script.get_revision("0019_v2_worker_pool_heartbeats").down_revision
        == "0018_v2_wake_shadow_decisions"
    )
    assert (
        script.get_revision("0018_v2_wake_shadow_decisions").down_revision
        == "0017_voice_primary_alignment"
    )


def test_tee_migrations_reuse_the_rds_contract_sql():
    rds = _scripts("alembic")
    tee = _scripts("alembic_tee")
    assert (
        tee.get_revision("0018_v2_wake_shadow_decisions").module._SCHEMA_UP
        == rds.get_revision("0085_v2_wake_shadow_decisions").module._SCHEMA_UP
    )
    assert (
        tee.get_revision("0019_v2_worker_pool_heartbeats").module._UP
        == rds.get_revision("0086_v2_worker_pool_heartbeats").module._UP
    )
    assert (
        tee.get_revision("0020_v2_first_chat_activation").module._BACKFILL_SQL
        == rds.get_revision("0087_v2_first_chat_activation").module._BACKFILL_SQL
    )
