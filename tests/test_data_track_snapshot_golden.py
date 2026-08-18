"""admin_data_track_snapshot 的输出形状 golden —— 期3b 重写的安全网。

这个端点在 prod 上对全部用户、全部历史现算(无时间窗),实测 JSON 端点
99-115s、被 gunicorn 的 120s 超时打死,等于已经死了。期3b 要把 chat 那块
换成「历史读冻结格子 + 当天读实时窗 + 存量点查」的拼装。

**行 schema 必须零变化**:data-track 前端是纯服务端渲染、零 JS fetch,
view=users 与 JSON summary/users 直接吃这个 dict 的形状。所以重写之前先把
现有行为逐字段钉住,重写之后拿同一份种子数据对。

⚠️ 这个文件的价值**完全取决于它真的会红**。Supervisor 的硬化要求①:
golden 必须先被证明会红(故意让实现少算一个字段、看它变红),再声明它承重
——否则就是又一个"测了机制没测接线"的假守卫。tests 里有一条
``test_the_golden_would_notice_a_missing_field`` 把这件事做成常驻断言,
不依赖谁记得手工验一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from conftest import seed_user  # noqa: E402


# 固定时刻,不用 now():golden 里出现浮动值就只能断言"存在",等于没钉住。
_T0 = 1_800_000_000.0


def _seed_chat(uid: str) -> None:
    """一份刻意"品种齐全"的聊天历史。

    每个计数字段都至少有一条**只有它**会数到的消息,否则某个字段算错时其他
    字段的数字会替它把 golden 撑住(期3a 的 1:1 假绿就是这么来的)。
    """
    rows = [
        # role=user,普通来源 —— 进 total/user_messages
        {"role": "user", "source": "ios", "content_type": "text"},
        # role=user,verify_ping —— 进 total 但**不**进 user_messages
        {"role": "user", "source": "verify_ping", "content_type": "text"},
        # role=user,resident_maintenance —— 同上,另一个排除来源
        {"role": "user", "source": "resident_maintenance", "content_type": "text"},
        # role=agent —— 进 agent_messages
        {"role": "agent", "source": "resident", "content_type": "text"},
        # role=openclaw —— 也算 agent_messages(两个角色名同族)
        {"role": "openclaw", "source": "resident", "content_type": "text"},
        # 图片 —— 只有 image_messages 会数
        {"role": "agent", "source": "resident", "content_type": "image"},
        # 主动消息 —— proactive_messages + proactive_last_ts
        {"role": "openclaw", "source": "agent_initiated_proactive",
         "content_type": "text"},
        # model_api 用户侧
        {"role": "user", "source": "model_api", "content_type": "text"},
        # model_api agent 侧
        {"role": "openclaw", "source": "model_api", "content_type": "text"},
        # model_api 开场白 —— 只有 model_api_greetings 会数
        {"role": "openclaw", "source": "model_api", "content_type": "text",
         "model_api_kind": "onboarding_greeting"},
    ]
    for index, doc in enumerate(rows):
        msg_id = f"m{index}"
        db.chat_append(uid, msg_id, _T0 + index, {"id": msg_id, **doc}, 1000)


def _chat(uid: str) -> dict:
    return db.admin_data_track_snapshot([uid])[uid]["chat"]


@pytest.fixture()
def seeded_user() -> str:
    uid = "usr_dt_golden_chat"
    seed_user(uid)
    _seed_chat(uid)
    return uid


# 逐字段钉死。数值是从上面那份种子推出来的,不是从实现里抄的——抄实现的
# golden 只能证明"实现等于它自己"。
#
# 佐证:第一版手推时 agent_messages 和 model_api_agent_messages 两个我算错了
# (漏数一条 openclaw、把一条 user 记成 agent),是这份 golden 先把**我**抓了
# 出来。如果当初是从实现里抄的,它会一次就绿,也就什么都证明不了。
_EXPECTED_CHAT_COUNTS = {
    "total": 10,
    "user_messages": 2,        # ios + model_api;verify_ping/resident_maintenance 排除
    "agent_messages": 6,       # agent×2(idx 3,5) + openclaw×4(idx 4,6,8,9)
    "image_messages": 1,
    "proactive_messages": 1,
    "model_api_user_messages": 1,
    "model_api_agent_messages": 2,   # idx 8,9(9 就是开场白那条)
    "model_api_greetings": 1,
}


def test_chat_counts_golden(seeded_user):
    chat = _chat(seeded_user)
    actual = {key: chat[key] for key in _EXPECTED_CHAT_COUNTS}
    assert actual == _EXPECTED_CHAT_COUNTS


def test_chat_timestamps_golden(seeded_user):
    chat = _chat(seeded_user)
    assert chat["first_ts"] == _T0
    assert chat["last_ts"] == _T0 + 9
    assert chat["proactive_last_ts"] == _T0 + 6
    # 最后一条**真实**用户消息是 model_api 那条(index 7),不是被排除的
    # verify_ping/resident_maintenance。这个字段被 chat.last_user_at 当作
    # 判活依据用过,错一位就会把废号显示成活的。
    assert chat["last_user_ts"] == _T0 + 7
    assert chat["last_agent_ts"] == _T0 + 9


def test_chat_distributions_golden(seeded_user):
    chat = _chat(seeded_user)
    assert chat["by_role"] == {"user": 4, "agent": 2, "openclaw": 4}
    assert chat["by_source"] == {
        "ios": 1,
        "verify_ping": 1,
        "resident_maintenance": 1,
        "resident": 3,
        "agent_initiated_proactive": 1,
        "model_api": 3,
    }
    assert chat["by_content_type"] == {"text": 9, "image": 1}


def test_snapshot_family_set_is_frozen(seeded_user):
    """重写只许动 chat 的**内部**,不许增减顶层族 —— 前端按族取值。"""
    payload = db.admin_data_track_snapshot([seeded_user])[seeded_user]
    assert set(payload) >= {"chat", "app_usage"}
    # chat 的键集合本身也是契约
    assert set(payload["chat"]) == set(_EXPECTED_CHAT_COUNTS) | {
        "first_ts", "last_ts", "proactive_last_ts",
        "last_user_ts", "last_agent_ts",
        "by_role", "by_source", "by_content_type",
    }


def test_a_user_with_no_chat_still_gets_a_well_formed_row():
    """空用户不能塌成 KeyError —— 前端对每个用户都取 chat.*。"""
    uid = "usr_dt_golden_empty"
    seed_user(uid)
    payload = db.admin_data_track_snapshot([uid])[uid]
    # 当前实现对无聊天用户不建 chat 键;这是既有契约的一部分,先钉住,
    # 重写后若要改成给空 dict,必须是**有意**的决定并同步改前端。
    assert "chat" not in payload
    assert payload["app_usage"] == {
        "foreground_sec": 0, "sessions": 0, "last_at": None,
    }


def test_the_golden_would_notice_a_missing_field(seeded_user, monkeypatch):
    """证明上面那些 golden **真的会红**,而不是碰巧全绿。

    Supervisor 硬化要求①:先证明会红,再声明它承重。做成常驻断言而不是
    "我手工验过一次" —— 手工验过的东西没人能复查,而这条每次 CI 都在验。

    做法:包一层,把 chat 里的一个字段打回 0,然后要求 golden 断言抛
    AssertionError。如果 golden 是空的/写松了,这里就不会抛,测试变红。
    """
    real = db.admin_data_track_snapshot

    def crippled(user_ids):
        payload = real(user_ids)
        for row in payload.values():
            if "chat" in row:
                row["chat"] = {**row["chat"], "user_messages": 0}
        return payload

    monkeypatch.setattr(db, "admin_data_track_snapshot", crippled)
    with pytest.raises(AssertionError):
        test_chat_counts_golden(seeded_user)

    # 分布字段同理,单独证一次 —— 计数红不代表分布也被钉住了。
    def crippled_distribution(user_ids):
        payload = real(user_ids)
        for row in payload.values():
            if "chat" in row:
                row["chat"] = {**row["chat"], "by_source": {}}
        return payload

    monkeypatch.setattr(db, "admin_data_track_snapshot", crippled_distribution)
    with pytest.raises(AssertionError):
        test_chat_distributions_golden(seeded_user)
