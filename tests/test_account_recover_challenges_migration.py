"""0105 adds the durable cross-worker account_recover_challenges table."""
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402


def _cfg():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return cfg


def test_alembic_single_head():
    heads = ScriptDirectory.from_config(_cfg()).get_heads()
    assert len(heads) == 1, f"expected one Alembic head, got {list(heads)}"
    assert heads[0] == "0105_account_recover_challenges"


def test_0105_upgrade_creates_table_and_indexes():
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT to_regclass('account_recover_challenges')"
        ).fetchone()[0] is not None
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s",
                ("account_recover_challenges",),
            ).fetchall()
        }
        assert "ix_account_recover_challenges_user_id" in idx
        assert "ix_account_recover_challenges_expires_at" in idx


def test_0105_downgrade_only_drops_its_own_table():
    cfg = _cfg()
    with db.get_pool().connection() as conn:
        other_tables_before = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ).fetchall()
        }

    try:
        command.downgrade(cfg, "0104_distill_artifact_ledger")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == ("0104_distill_artifact_ledger",)
            assert conn.execute(
                "SELECT to_regclass('account_recover_challenges')"
            ).fetchone() == (None,)
            other_tables_after = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ).fetchall()
            }
        assert other_tables_before - {"account_recover_challenges"} == other_tables_after

        command.upgrade(cfg, "0105_account_recover_challenges")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == ("0105_account_recover_challenges",)
            assert conn.execute(
                "SELECT to_regclass('account_recover_challenges')"
            ).fetchone()[0] is not None

        command.downgrade(cfg, "0104_distill_artifact_ledger")
        command.upgrade(cfg, "0105_account_recover_challenges")
        with db.get_pool().connection() as conn:
            assert conn.execute(
                "SELECT to_regclass('account_recover_challenges')"
            ).fetchone()[0] is not None
    finally:
        command.upgrade(cfg, "head")


def test_0105_challenge_row_cascades_on_user_delete():
    uid = "usr_migration_cascade_probe"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s, '', '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
        conn.execute(
            "INSERT INTO account_recover_challenges "
            "(challenge_id, user_id, public_key, answer_sha256, created_at, expires_at) "
            "VALUES (%s, %s, 'pk', 'hash', 0, 0)",
            ("chal_migration_probe", uid),
        )
        conn.execute("DELETE FROM users WHERE user_id = %s", (uid,))
        assert conn.execute(
            "SELECT 1 FROM account_recover_challenges WHERE challenge_id = %s",
            ("chal_migration_probe",),
        ).fetchone() is None
