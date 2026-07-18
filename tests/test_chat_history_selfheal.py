"""Durable chat-history reads and their hot-cache fallback.

The primary history path pages PostgreSQL directly, so missed cross-worker
LISTEN/NOTIFY broadcasts and the bounded per-process hot window cannot hide a
durable row. If that bounded read fails, the legacy in-memory path remains a
fail-open fallback and may use the stale-store probe/reload behavior exercised
below.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
import db  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _hk(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _env(user_id: str, marker: str) -> dict:
    return {
        "v": 1,
        "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared",
        "owner_user_id": user_id,
    }


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    yield body["user_id"], body["api_key"]


def _send(client, user_id, api_key, marker):
    res = client.post(
        "/v1/chat/message",
        headers=_hk(api_key),
        json={"envelope": _env(user_id, marker)},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def _history_since(client, api_key, since):
    res = client.get(
        f"/v1/chat/history?since={since}&limit=40", headers=_hk(api_key)
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def _append_db_only(user_id: str, marker: str, ts: float) -> None:
    """模拟「另一个 worker 写入」：直接落 DB，绕开本进程内存 store——本 worker
    仿佛漏掉了那条 NOTIFY。doc 直接克隆该用户 DB 里最近一行、换 id/ts，保证行形
    状与真实写路径一字不差。"""
    rows = db.chat_load(user_id)
    assert rows, "seed a message via the API first"
    doc = dict(rows[-1])
    doc["id"] = marker
    doc["ts"] = ts
    db.chat_append(user_id, marker, ts, doc, 500)


def test_history_selfheals_when_db_has_newer_row(user):
    """漏广播场景：DB 有更新的行而内存没有 → 带 since 的空结果必须触发就地重载
    并把该行带回来（而不是让用户等 15 分钟 TTL）。"""
    user_id, api_key = user
    client = make_client()
    first = _send(client, user_id, api_key, "m1")
    ts1 = float(first["ts"])

    _append_db_only(user_id, "m2-cross-worker", ts1 + 5.0)

    body = _history_since(client, api_key, ts1)
    ids = [m.get("id") for m in body.get("messages") or []]
    assert "m2-cross-worker" in ids, f"stale store not healed: {body}"


def test_history_no_reload_when_memory_fresh(user, monkeypatch):
    """内存与 DB 一致时，空结果绝不触发重载（探针高频路径的成本护栏）。"""
    user_id, api_key = user
    client = make_client()
    first = _send(client, user_id, api_key, "m1")
    ts1 = float(first["ts"])

    evicted = []
    monkeypatch.setattr(core_store, "_evict_store",
                        lambda uid: evicted.append(uid))
    body = _history_since(client, api_key, ts1 + 100.0)
    assert (body.get("messages") or []) == []
    assert evicted == []


def test_history_probe_compares_raw_not_visible(user, monkeypatch):
    """最新一条是被隐藏的 verify 行（内存与 DB 一致）时不得重载——探针必须比对
    原始 store.chat_messages 的最大 ts，而不是可见过滤后的列表。"""
    user_id, api_key = user
    client = make_client()
    first = _send(client, user_id, api_key, "m1")
    ts1 = float(first["ts"])

    # verify 回执行：agent 角色 + source=verify_ping → 永远隐藏于可见 feed，
    # 但存在于内存 raw 列表和 DB（经真实 append 路径写入，保持两侧一致）。
    store = core_store.get_store(user_id)
    store.append_chat("agent", "verify_ping", _env(user_id, "verify-ack-1"))

    evicted = []
    monkeypatch.setattr(core_store, "_evict_store",
                        lambda uid: evicted.append(uid))
    body = _history_since(client, api_key, ts1 + 3.0)
    assert (body.get("messages") or []) == []
    assert evicted == [], "raw-consistent store must not reload on hidden rows"


def test_history_probe_fails_open(user, monkeypatch):
    """探针 DB 故障时 fail-open：照常返回（可能 stale 的）空结果，绝不 500。"""
    user_id, api_key = user
    client = make_client()
    first = _send(client, user_id, api_key, "m1")
    ts1 = float(first["ts"])

    def boom(uid):
        raise RuntimeError("db probe down")
    monkeypatch.setattr(db, "chat_newest_ts", boom)
    body = _history_since(client, api_key, ts1)
    assert (body.get("messages") or []) == []


def test_history_and_single_body_read_past_hot_window(user, monkeypatch):
    """The worker cache is only a hot window, never the history boundary."""
    user_id, api_key = user
    client = make_client()
    first = _send(client, user_id, api_key, "oldest")
    oldest_ts = float(first["ts"])

    for index in range(1, 6):
        _append_db_only(user_id, f"new-{index}", oldest_ts + index)

    monkeypatch.setattr(core_store, "MAX_CHAT_MESSAGES", 3)
    core_store._evict_store(user_id)
    hot_store = core_store.get_store(user_id)
    assert [row["id"] for row in hot_store.chat_messages] == [
        "new-3", "new-4", "new-5",
    ]

    history = client.get("/v1/chat/history?limit=40", headers=_hk(api_key))
    assert history.status_code == 200
    payload = history.get_json()
    assert payload["total"] == 6
    assert [row["id"] for row in payload["messages"]] == [
        "oldest", "new-1", "new-2", "new-3", "new-4", "new-5",
    ]

    body = client.get(
        "/v1/chat/messages/oldest/body", headers=_hk(api_key))
    assert body.status_code == 200
    assert body.get_json()["message"]["body_ct"] == _env(user_id, "oldest")["body_ct"]
