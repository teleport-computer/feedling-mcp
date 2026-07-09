"""long-poll 返回体带 agent_status_events 游标（spec §9）。DB-backed（真表 + 真路由）。

覆盖：(1) poll_core.build_response 带 status 字段；(2) db 原语按 after_id 增量取；
(3) chat_poll 在只有 status 更新（无新聊天消息）时也能返回 status 事件；
(4) 零回归——不带 since_status_id 的旧式 poll 行为不变（仍无游标依赖，字段只是新增）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import conftest  # noqa: E402
import db  # noqa: E402
from chat import poll_core as chat_poll_core  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402


def test_build_response_carries_status_cursor():
    resp = chat_poll_core.build_response(
        messages=[], context={"runtime_v2": {}, "client_release": {}},
        consumer_id="c1", claim=True, timed_out=True,
        status_events=[{"id": 5, "kind": "reading_memory", "label": "读取上下文", "detail": {}}],
        status_cursor=5)
    assert resp["agent_status_events"][0]["kind"] == "reading_memory"
    assert resp["status_cursor"] == 5


def test_build_response_defaults_are_empty_and_backward_compatible():
    """Zero-regression: an old-style call site with no status kwargs still works
    and gets an empty status list / zero cursor — the additive fields never
    surprise a caller that doesn't know about them."""
    resp = chat_poll_core.build_response(
        messages=[{"id": 1}], context={"runtime_v2": {}, "client_release": {}},
        consumer_id="c1", claim=True, timed_out=False,
    )
    assert resp["messages"] == [{"id": 1}]
    assert resp["agent_status_events"] == []
    assert resp["status_cursor"] == 0
    # exact legacy field set + the two additive fields, nothing else snuck in.
    assert set(resp.keys()) == {
        "messages", "runtime_v2", "client_release", "timed_out",
        "consumer_id", "claimed", "agent_status_events", "status_cursor",
    }


def test_db_list_agent_status_events_increments_by_after_id(client, backend_env):
    conftest.seed_user("usr_status1")
    a = jobs_store.append_status_event("usr_status1", "processing", label="处理中", detail={})
    b = jobs_store.append_status_event("usr_status1", "reading_memory", label="读取上下文", detail={"count": 3})
    all_ev = db.list_agent_status_events("usr_status1", after_id=0, limit=50)
    assert [e["kind"] for e in all_ev] == ["processing", "reading_memory"]
    after_a = db.list_agent_status_events("usr_status1", after_id=a, limit=50)
    assert [e["id"] for e in after_a] == [b]


def test_chat_poll_returns_status_only_update(client, backend_env):
    conftest.seed_user("usr_status2")
    key = conftest.seed_api_key("usr_status2")
    jobs_store.append_status_event("usr_status2", "reading_perception", label="读取感知", detail={})
    r = client.get("/v1/chat/poll?since=0&timeout=0&since_status_id=0",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    body = r.json
    assert any(e["kind"] == "reading_perception" for e in body["agent_status_events"])
    assert body["status_cursor"] >= 1


def test_chat_poll_old_style_call_is_unaffected(client, backend_env):
    """Zero-regression: a client that never sends since_status_id (and never
    reads the new fields) gets the exact same messages/timing as before —
    status events for the SAME user must not change whether/when the call
    returns when no since_status_id is negotiated (defaults to 0, so any
    existing status event would show up, but no messages/timeout behavior is
    altered by their mere existence)."""
    conftest.seed_user("usr_status3")
    key = conftest.seed_api_key("usr_status3")
    r = client.get("/v1/chat/poll?since=0&timeout=0",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    body = r.json
    assert body["messages"] == []
    assert body["timed_out"] is True
    # additive fields present with safe defaults when there is nothing pending.
    assert body["agent_status_events"] == []
    assert body["status_cursor"] == 0
