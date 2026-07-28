"""被 wedge-skip 跳过的消息必须还能被重投递 —— 「有时候不回消息」曾经是真丢了。

⚠️ 本文件从「复现缺陷」转成了「守护修复」：下面描述的是修复前的行为，
断言现在验证的是修复后的行为（豁免 supersede、保留年龄下限、恢复留痕）。


2026-07-22 prod：runner 重启后的 ~2.5h 内，consumer 日志里
``poll returned claimed messages but decrypt history did not include those ids``
出现 344 次，其中 39 次累计到 ``CHAT_POLL_WEDGE_SKIP_AFTER`` 后打出
``absent from decrypt history ... advancing cursor`` —— 即
``_advance_past_unfetchable`` 把游标推过了取不到明文的那条消息。
库侧对账：**10 条用户消息的 reply_message_id 永远为 null、涉及 7 个用户**，
且全部 ``reply_claimed_by`` 非空（被认领过后丢弃）。usr_798f55239b84 一人丢 3 条。

单看 consumer 侧，跳过本身还算「可恢复」：游标虽然过了，服务端还有
redelivery backstop（``CHAT_REDELIVERY_WINDOW_SEC``，默认 1h）兜底。
真正让它变成**永久**丢失的是与 ``_redelivery_floor`` 的交互：

    floor = max(now - REDELIVERY_WINDOW, 最新一条【已回复】的会话消息 ts)
    ts <= floor 的消息永不重投

该下限的设计意图是合理的 —— 注释写着「用户重新问了并得到回答，旧那条是被
*取代* 而非丢失，迟到的回复会乱序」。但这个假设在 wedge-skip 场景下不成立：
消息 A 不是被用户重问取代的，是被系统跳过的，A 和 B 的内容可能毫不相干。
于是只要用户在 A 之后又说了一句并得到回复，A 就被永久判定为「已被取代」，
静默消失，且没有任何错误上报。

本测试复现这条链：A 被跳过 → B 正常回复 → A 再也不会回来。

Run:  python -m pytest tests/test_chat_skipped_message_never_redelivered.py -q
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
from chat import service as chat_service  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402

CONSUMER = "agent-runner:test"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _add_user_msg(store, msg_id: str, ts: float, uid: str, **extra) -> dict:
    """写入一条用户消息 —— DB 和本 worker 的缓存都要有。

    认领走的是 ``db.chat_try_claim_reply`` 的条件 UPDATE，只存在于内存缓存里
    的行会 CAS 落空、被当成「输给了别的 worker」而静默跳过，那样测出来的
    「没被重投」是假阳性。所以必须真正落库。
    """
    doc = {
        "id": msg_id, "role": "user", "source": "chat", "ts": ts, "v": 1,
        "content_type": "text", "visibility": "shared", "owner_user_id": uid,
        "body_ct": "x", "nonce": "x", "K_user": "x",
    }
    doc.update(extra)
    db.chat_append(uid, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)
    with store.chat_lock:
        store.chat_messages.append(doc)
    return doc


def _add_agent_msg(store, msg_id: str, ts: float, uid: str, **extra) -> dict:
    doc = {
        "id": msg_id, "role": "openclaw", "source": "chat", "ts": ts, "v": 1,
        "content_type": "text", "visibility": "shared", "owner_user_id": uid,
        "body_ct": "x", "nonce": "x", "K_user": "x",
    }
    doc.update(extra)
    db.chat_append(uid, msg_id, ts, doc, core_store.MAX_CHAT_MESSAGES)
    with store.chat_lock:
        store.chat_messages.append(doc)
    return doc


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x77" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def test_skipped_message_is_permanently_lost_once_a_later_turn_is_answered(user):
    uid = user
    store = core_store.get_store(uid)
    now = time.time()

    a_ts, b_ts = now - 300, now - 240
    # A：consumer 取不到明文，被 _advance_past_unfetchable 跳过 —— 认领过、
    # 从未回复。这正是 prod 那 10 条的库内形态。
    _add_user_msg(
        store, "m_skipped", a_ts, uid,
        reply_claimed_by=CONSUMER,
        reply_claimed_at=f"{a_ts:.3f}",
        reply_claim_expires_at=f"{a_ts:.3f}",   # 租约早已过期
    )
    # B：用户随后又说了一句，这次正常回复了。
    _add_user_msg(
        store, "m_answered", b_ts, uid,
        reply_status="replied", reply_message_id="r_b",
    )

    # 前置条件：A 确实处在「该被 backstop 救回」的状态 —— 未回复、租约已过期、
    # 且仍在 1h 重投窗口内。若这条断言失败，本测试就不再复现 prod 状态了。
    assert chat_service.CHAT_REDELIVERY_WINDOW_SEC >= 600
    assert now - a_ts < chat_service.CHAT_REDELIVERY_WINDOW_SEC

    # consumer 的游标已被 wedge-skip 推到 A 之后。
    pending = chat_service._pending_chat_messages_for_poll(
        store, since=b_ts, consumer_id=CONSUMER, claim=True,
    )
    ids = [m.get("id") for m in pending]

    # 修复后：A 被【认领后丢弃】(claim 过期、无回复)，不是被用户重问取代的，
    # 所以 supersede 下限对它不适用 —— 仍在 1h 重投窗口内就必须还能投递。
    assert "m_skipped" in ids, (
        "被认领后丢弃的消息仍被 _redelivery_floor 判成【已被取代】而永久消失"
    )

    # supersede 下限本身语义不变：B 的回复仍然把它顶到 A 之上。
    # 变的只是「谁豁免于它」。
    floor = chat_service._redelivery_floor(store, time.time())
    assert floor >= b_ts > a_ts, (
        f"floor={floor} 应被【已回复】的 B(ts={b_ts}) 顶到 A(ts={a_ts}) 之上"
    )


def test_backstop_does_redeliver_when_no_later_turn_was_answered(user):
    """对照组：没有更新的已回复消息时，同一条 A 会被 backstop 正常救回。

    这条确认上面的永久丢失确实由 _redelivery_floor 造成，而不是
    「未回复消息本来就不会重投」。
    """
    uid = user
    store = core_store.get_store(uid)
    now = time.time()
    a_ts = now - 300

    _add_user_msg(
        store, "m_skipped_only", a_ts, uid,
        reply_claimed_by=CONSUMER,
        reply_claimed_at=f"{a_ts:.3f}",
        reply_claim_expires_at=f"{a_ts:.3f}",
    )

    pending = chat_service._pending_chat_messages_for_poll(
        store, since=a_ts + 1, consumer_id=CONSUMER, claim=True,
    )
    assert "m_skipped_only" in [m.get("id") for m in pending], (
        "没有更新的已回复消息时，backstop 应当把 A 重新投递回来"
    )


def test_legacy_adjacent_reply_settles_abandoned_parent_without_rerunning(user):
    uid = user
    store = core_store.get_store(uid)
    now = time.time()
    parent_ts = now - 300
    _add_user_msg(
        store, "m_legacy_parent", parent_ts, uid,
        reply_claimed_by=CONSUMER,
        reply_claimed_at=f"{parent_ts:.3f}",
        reply_claim_expires_at=f"{parent_ts:.3f}",
    )
    _add_agent_msg(store, "m_legacy_reply", parent_ts + 30, uid)
    _add_user_msg(
        store, "m_newer_done", parent_ts + 60, uid,
        reply_status="replied", reply_message_id="r_newer",
    )

    pending = chat_service._pending_chat_messages_for_poll(
        store, since=now, consumer_id=CONSUMER, claim=True,
    )

    assert "m_legacy_parent" not in [m.get("id") for m in pending]
    parent = db.chat_get_strict(uid, "m_legacy_parent")
    reply = db.chat_get_strict(uid, "m_legacy_reply")
    assert parent["reply_status"] == "replied"
    assert parent["reply_message_id"] == "m_legacy_reply"
    assert reply["reply_to_message_id"] == "m_legacy_parent"


def test_legacy_reply_is_not_guessed_across_consecutive_user_messages(user):
    uid = user
    store = core_store.get_store(uid)
    now = time.time()
    parent_ts = now - 300
    _add_user_msg(
        store, "m_first_user", parent_ts, uid,
        reply_claimed_by=CONSUMER,
        reply_claimed_at=f"{parent_ts:.3f}",
        reply_claim_expires_at=f"{parent_ts:.3f}",
    )
    _add_user_msg(store, "m_second_user", parent_ts + 20, uid)
    _add_agent_msg(store, "m_reply_for_second", parent_ts + 40, uid)

    pending = chat_service._pending_chat_messages_for_poll(
        store, since=now, consumer_id=CONSUMER, claim=True,
    )

    assert "m_first_user" in [m.get("id") for m in pending]
    assert db.chat_get_strict(uid, "m_first_user").get("reply_message_id") in (None, "")


def test_abandoned_message_older_than_the_window_is_not_retried_forever(user):
    """豁免的是「被取代」，不是「太老」—— 否则永久解不开的那条会无限重投。

    wedge-skip 的根因（enclave 取不到明文）可能是永久性的：那条消息每次重投都会
    再走一遍 claim → 取明文失败 → 跳过。年龄下限是这个循环的唯一刹车，必须对
    「被认领后丢弃」的消息同样生效。
    """
    uid = user
    store = core_store.get_store(uid)
    now = time.time()
    old_ts = now - chat_service.CHAT_REDELIVERY_WINDOW_SEC - 60  # 窗口外

    _add_user_msg(
        store, "m_too_old", old_ts, uid,
        reply_claimed_by=CONSUMER,
        reply_claimed_at=f"{old_ts:.3f}",
        reply_claim_expires_at=f"{old_ts:.3f}",
    )
    assert chat_service._claim_abandoned(
        {"reply_claimed_by": CONSUMER, "reply_claim_expires_at": f"{old_ts:.3f}"}, now
    ), "前置条件：这条确实是「被认领后丢弃」的形态"

    pending = chat_service._pending_chat_messages_for_poll(
        store, since=now, consumer_id=CONSUMER, claim=True,
    )
    assert "m_too_old" not in [m.get("id") for m in pending], (
        "窗口外的消息仍被重投 —— 永久不可读的消息会无限重试"
    )


def test_supersede_rule_still_blocks_a_message_that_was_never_claimed(user):
    """反向守卫：原本的「被取代」保护没有被削弱。

    从未被认领过的旧消息 + 之后有已回复的新消息 = 会话真的往前走了，
    迟到的回复会乱序。这条必须仍然被挡住。
    """
    uid = user
    store = core_store.get_store(uid)
    now = time.time()
    a_ts, b_ts = now - 300, now - 240

    _add_user_msg(store, "m_never_claimed", a_ts, uid)  # 没有任何 claim 字段
    _add_user_msg(
        store, "m_answered_later", b_ts, uid,
        reply_status="replied", reply_message_id="r_b",
    )

    pending = chat_service._pending_chat_messages_for_poll(
        store, since=b_ts, consumer_id=CONSUMER, claim=True,
    )
    assert "m_never_claimed" not in [m.get("id") for m in pending], (
        "supersede 保护被削弱了 —— 会话已往前走的旧消息不该再重投"
    )


def test_recovering_an_abandoned_message_is_observable(user, capsys):
    """救回一条被丢弃的消息必须留下痕迹。

    这条链之前唯一的痕迹在 consumer 侧一行 log.error，服务端零上报 —— prod 那
    40 条是靠 `reply_message_id is null` 对账翻出来的，不是被告警发现的。
    重投「被认领后丢弃」的消息是一次系统自纠，必须可 grep、可计数。
    """
    uid = user
    store = core_store.get_store(uid)
    now = time.time()
    a_ts = now - 300

    _add_user_msg(
        store, "m_recovered", a_ts, uid,
        reply_claimed_by=CONSUMER,
        reply_claimed_at=f"{a_ts:.3f}",
        reply_claim_expires_at=f"{a_ts:.3f}",
    )
    capsys.readouterr()  # 丢掉写入阶段的输出

    pending = chat_service._pending_chat_messages_for_poll(
        store, since=now, consumer_id=CONSUMER, claim=True,
    )
    assert "m_recovered" in [m.get("id") for m in pending]

    out = capsys.readouterr().out
    assert "abandoned_claim_recovered" in out, (
        "重投被丢弃的消息没有任何服务端痕迹 —— 静默丢失变成了静默恢复"
    )
    assert "m_recovered" in out and uid in out, (
        "痕迹里没有 user_id / message id，对账时定位不到是谁的哪一条"
    )
