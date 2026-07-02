from __future__ import annotations
import sys, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import pytest  # noqa: E402
import db  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from observability import store  # noqa: E402


def test_insert_and_latest():
    store.insert_sample({
        "ts": "2026-07-02T10:00:00+00:00",
        "host_load1": 1.5, "live_agents": 90, "orphan": 3,
        "backend_5xx": 2, "top_tables": [{"t": "x"}],  # 未知键 → extra
    })
    latest = store.latest_sample()
    assert latest["live_agents"] == 90
    assert latest["orphan"] == 3
    assert latest["top_tables"] == [{"t": "x"}]  # 从 extra 展开


def test_recent_and_retention():
    old_ts = "2000-01-01T00:00:00+00:00"
    store.insert_sample({"ts": old_ts, "live_agents": 1})
    n = store.delete_old_samples(retention_hours=48)
    assert n >= 1
    tss = [r["ts"] for r in store.recent_samples(hours=1)]
    assert all(str(t) != old_ts for t in tss)


def test_service_resource_roundtrip():
    store.upsert_service_resource("agent-runner", 5_000_000, 6_000_000_000)
    r = store.read_service_resource("agent-runner")
    assert r["mem_bytes"] == 6_000_000_000
    assert r["age_sec"] < 5


def test_agent_counts_shape():
    c = store.agent_counts()
    assert set(c) >= {"live", "by_driver", "orphan", "idle", "errors"}


def test_db_activity_shape():
    a = store.db_activity()
    assert "conns" in a and "slow" in a


def test_chat_backlog_counts_unanswered_user_message():
    uid = f"obs-test-{uuid.uuid4().hex[:8]}"
    old_ts = time.time() - 3600  # 1h ago, well past any reasonable stale_sec
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s, %s, %s, %s)",
            (uid, "m1", old_ts, Jsonb({"role": "user", "text": "hi"})),
        )
    n = store.chat_backlog(stale_sec=60)
    assert n >= 1
