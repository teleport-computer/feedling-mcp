"""jobs_store.live_genesis_worker_ids returns only fresh kind='genesis' workers."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


def test_live_genesis_worker_ids_only_fresh():
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM v2_worker_heartbeats WHERE worker_id LIKE 'wtest-%'")
    jobs_store.record_worker_heartbeat("wtest-fresh:genesis", kind="genesis", capacity=0)
    with db.get_pool().connection() as c:
        c.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind, capacity) "
            "VALUES ('wtest-stale:genesis', now() - interval '600 seconds', 'genesis', 0) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = EXCLUDED.beat_at, kind='genesis'")
    # a turn worker must never appear — genesis reclaim only cares about genesis
    jobs_store.record_worker_heartbeat("wtest-turn", kind="turn", capacity=4)

    ids = jobs_store.live_genesis_worker_ids(within_sec=120)
    assert "wtest-fresh:genesis" in ids
    assert "wtest-stale:genesis" not in ids
    assert "wtest-turn" not in ids
