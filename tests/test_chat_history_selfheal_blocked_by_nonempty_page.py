"""陈旧自愈被「非空结果」挡住 —— consumer 取明文缺 id 的真正来源。

一直以来把 consumer 那句
``poll returned claimed messages but decrypt history did not include those ids``
当成「enclave 取明文失败」（超时 / 解密错误 / 负载）。**不是**。
enclave 的 ``/v1/chat/history`` 只是把请求转发给 backend 的同名接口
（``backend/enclave/routes/chat.py:276``），而 backend 那边返回了 **200 + 一个
缺了那条消息的列表**。没有超时，没有解密报错 —— 少的那条根本没被读出来。

为什么没被读出来：backend 的 history 服务于每个 worker 各自的
``store.chat_messages`` 内存缓存，跨 worker 靠 LISTEN/NOTIFY 同步。2026-07-15
为此加过读时自愈 ``_self_heal_if_stale``（"push arrives, chat page doesn't"），
但它的触发条件是（``backend/chat/chat_core.py:374``）：

    filtered = [m for m in all_msgs if m.ts > since]
    if not filtered and _self_heal_if_stale(...):   # ← 只在【完全空】时才探 DB

于是只要缓存里还有**任何一条**比 ``since`` 新的消息——哪怕只是 agent 自己刚
写下的那条回复——``filtered`` 就非空，陈旧检查被整个跳过，真正缺失的那条用户
消息永远补不回来。

prod 现场（usr_90184ac4，22:15:26–22:15:46 连打 5 次全部落空）正是这个形状：
consumer 带 ``since=22:13:24`` 去取，而该 worker 缓存里有 22:13:40 的 agent
回复（ts > since ⇒ filtered 非空 ⇒ 不自愈），缺的是 22:15:25 那条用户消息。
5 次重试全部命中同一条陈旧路径，然后 ``_advance_past_unfetchable`` 把它跳过。

⇒ 根因是**缓存一致性**，不是加解密、也不是超时。负载（主动消息 +218%）是
加剧因素——写多了 NOTIFY 更容易丢——但不是直接原因。

Run:  python -m pytest tests/test_chat_history_selfheal_blocked_by_nonempty_page.py -q
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _doc(msg_id: str, ts: float, uid: str, role: str) -> dict:
    return {
        "id": msg_id, "role": role, "source": "chat", "ts": ts, "v": 1,
        "content_type": "text", "visibility": "shared", "owner_user_id": uid,
        "body_ct": "x", "nonce": "x", "K_user": "x",
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
        json={"public_key": _b64(b"\x64" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"], res.get_json()["api_key"]


def test_history_misses_row_when_page_is_nonempty(user):
    """缓存里有一条更新的 agent 回复 ⇒ 自愈不触发 ⇒ 另一 worker 写的用户消息缺失。"""
    uid, api_key = user
    client = make_client()
    store = core_store.get_store(uid)
    now = time.time()
    since = now - 300
    reply_ts, missing_ts = now - 280, now - 100

    # 本 worker 自己写下的 agent 回复：DB + 缓存都有，且 ts > since。
    with store.chat_lock:
        store.chat_messages.append(_doc("m_reply", reply_ts, uid, "agent"))
    db.chat_append(uid, "m_reply", reply_ts, _doc("m_reply", reply_ts, uid, "agent"),
                   core_store.MAX_CHAT_MESSAGES)

    # 另一个 worker 写入的用户消息：只进 DB，本 worker 缓存没收到 NOTIFY。
    db.chat_append(uid, "m_from_other_worker", missing_ts,
                   _doc("m_from_other_worker", missing_ts, uid, "user"),
                   core_store.MAX_CHAT_MESSAGES)

    # 前置条件：这就是 prod 的陈旧状态。
    assert all(m.get("id") != "m_from_other_worker" for m in store.chat_messages)
    assert any(m.get("id") == "m_from_other_worker" for m in db.chat_load(uid))

    res = client.get(
        f"/v1/chat/history?since={since}&limit=50&include_image_body=false",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    ids = [m.get("id") for m in res.get_json().get("messages", [])]

    # 修复前：回复在（所以这一页非空）⇒ 自愈被 `not filtered` 挡住 ⇒ 缺的恰恰
    # 是那条用户消息，正是 consumer 看到的「claimed id 不在 decrypt history 里」。
    # 修复后（chat_core 去掉 `not filtered` 前置条件）：非空页也探测，缺行补回。
    assert "m_reply" in ids
    assert "m_from_other_worker" in ids, (
        "非空页仍未触发自愈 —— 缺陷回归了（原复现见本文件 docstring）"
    )


def test_history_does_self_heal_when_page_would_be_empty(user):
    """对照组：同样的陈旧缓存，只要这一页会是空的，自愈就正常补回来。

    证明缺陷出在 `if not filtered` 这个门槛上，而不是「自愈根本不工作」。
    """
    uid, api_key = user
    client = make_client()
    store = core_store.get_store(uid)
    now = time.time()
    since = now - 300
    missing_ts = now - 100

    # 这次缓存里没有任何比 since 新的消息 —— 唯一的行只在 DB 里。
    db.chat_append(uid, "m_only_in_db", missing_ts,
                   _doc("m_only_in_db", missing_ts, uid, "user"),
                   core_store.MAX_CHAT_MESSAGES)
    assert all(m.get("id") != "m_only_in_db" for m in store.chat_messages)

    res = client.get(
        f"/v1/chat/history?since={since}&limit=50&include_image_body=false",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    ids = [m.get("id") for m in res.get_json().get("messages", [])]
    assert "m_only_in_db" in ids, (
        "空页时自愈应当探 DB 并补回该行（2026-07-15 的既有修复）"
    )


def test_history_heals_a_missing_MIDDLE_row(user):
    """自愈判据的盲区：缺失行不是最新的一条时，`newest_db_ts > raw_max_ts`
    检测不到。

    去掉 `not filtered` 之后，探测仍以「DB 最新 ts vs 内存最大 ts」为判据。
    可缺失的行未必是最新的：用户连发两条，第一条 W 的跨 worker NOTIFY 丢了、
    第二条 X 没丢，本 worker 看到 X 并写下回复 R。此时
    内存最大 ts == DB 最新 ts（都是 R），判据不成立 ⇒ W 永远补不回来。
    只比「最大 ts」永远盖不住中间缺行；判据必须比「since 之后的行数」。
    """
    uid, api_key = user
    client = make_client()
    store = core_store.get_store(uid)
    now = time.time()
    since = now - 400
    w_ts, x_ts, r_ts = now - 300, now - 200, now - 190

    # W：用户第一条，只进 DB（另一 worker 写入，本 worker 的 NOTIFY 丢失）。
    db.chat_append(uid, "m_W_lost", w_ts, _doc("m_W_lost", w_ts, uid, "user"),
                   core_store.MAX_CHAT_MESSAGES)
    # X：用户第二条 + R：本 worker 对 X 的回复 —— 都进 DB 和缓存，把两侧
    # 「最大 ts」顶到齐平，正好让旧判据失明。
    x = _doc("m_X_seen", x_ts, uid, "user")
    r = _doc("m_R_reply", r_ts, uid, "agent")
    for mid, ts, d in (("m_X_seen", x_ts, x), ("m_R_reply", r_ts, r)):
        db.chat_append(uid, mid, ts, d, core_store.MAX_CHAT_MESSAGES)
    with store.chat_lock:
        store.chat_messages.extend([x, r])

    # 前置条件：两侧最大 ts 齐平（旧判据必然失明），且 W 只在 DB。
    raw_max_mem = max(float(m["ts"]) for m in store.chat_messages)
    assert abs(raw_max_mem - db.chat_newest_ts(uid)) < 1e-6
    assert all(m.get("id") != "m_W_lost" for m in store.chat_messages)

    res = client.get(
        f"/v1/chat/history?since={since}&limit=50&include_image_body=false",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    ids = [m.get("id") for m in res.get_json().get("messages", [])]
    assert "m_W_lost" in ids, (
        f"缺失的中间行未被补回（自愈只比最大 ts 的盲区）：{ids}"
    )
