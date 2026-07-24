"""0040 adds serve-worker claim-attribution columns to genesis_import_jobs."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


def test_worker_claim_columns_exist():
    with db.get_pool().connection() as c:
        cols = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='genesis_import_jobs'").fetchall()}
    assert "worker_claimed_by" in cols
    assert "worker_claimed_at" in cols


def test_alembic_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    here = Path(__file__).parent.parent / "backend"
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert list(heads) == ["0055_capture_applied_check"]
