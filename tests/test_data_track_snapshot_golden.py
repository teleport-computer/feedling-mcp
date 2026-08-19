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
#
# ⚠️ 必须是**过去**的时刻。第一版用了 1_800_000_000(2027-01),比真实 now 还晚,
# 于是每一行都落进「今天」实时窗、冻结那条路一次都没被走到 —— 12 条测试全绿而
# 新代码零覆盖。同一个疏忽还盖住了一个真 bug:格子求和当时没有 day 上界,与实时
# 窗重叠会双计。**种子时刻必须让被测的分支真的被走到**,这和「样本取值不许让不同
# 分支产出相同结果」是同一条。
_T0 = 1_780_272_000.0  # 2026-06-01 08:00 UTC —— 稳定在过去,足以让日子早已关闭


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
def clean_chat_rollup():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_daily_rollup")
        conn.execute("DELETE FROM chat_rollup_watermark")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_daily_rollup")
        conn.execute("DELETE FROM chat_rollup_watermark")


@pytest.fixture()
def seeded_user(clean_chat_rollup) -> str:
    uid = "usr_dt_golden_chat"
    seed_user(uid)
    _seed_chat(uid)
    # 冻结一次 —— 重写后的端点从格子读历史,不冻等于历史不可见。真实环境里
    # 调度器每天跑,所以「已冻结」才是常态;「没冻结」是缺口态,单独一条测
    # (test_unfrozen_history_is_declared_not_silently_zero)。
    db.freeze_completed_chat_days()
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


# --- 期3b:冻结格子必须与现算口径逐字一致 ----------------------------------- #
#
# 格子是给端点提速用的,提速不许改数。所以承重判据不是"格子里有数",而是
# **格子求和 == 现算结果**。任一字段的谓词抄漏一个条件,这里就会红。


def _freeze_and_sum(uid: str) -> dict:
    """把该用户所有日格子求和,还原成 snapshot 的 chat 形状。"""
    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT total, user_messages, agent_messages, image_messages,"
            " proactive_messages, model_api_user_messages,"
            " model_api_agent_messages, model_api_greetings,"
            " first_ts, last_ts, proactive_last_ts, last_user_ts, last_agent_ts,"
            " by_role, by_source, by_content_type"
            " FROM chat_daily_rollup WHERE user_id = %s ORDER BY day",
            (uid,),
        ).fetchall()
    assert rows, "没有冻出任何格子 —— 下面的比对会变成空对空"
    out = {key: 0 for key in _EXPECTED_CHAT_COUNTS}
    stamps: dict[str, list] = {k: [] for k in
                               ("first_ts", "last_ts", "proactive_last_ts",
                                "last_user_ts", "last_agent_ts")}
    dists: dict[str, dict] = {"by_role": {}, "by_source": {}, "by_content_type": {}}
    for row in rows:
        for index, key in enumerate(_EXPECTED_CHAT_COUNTS):
            out[key] += int(row[index])
        for offset, key in enumerate(stamps):
            value = row[8 + offset]
            if value is not None:
                stamps[key].append(float(value))
        for offset, key in enumerate(dists):
            for bucket, n in (row[13 + offset] or {}).items():
                dists[key][bucket] = dists[key].get(bucket, 0) + int(n)
    out["first_ts"] = min(stamps["first_ts"]) if stamps["first_ts"] else None
    for key in ("last_ts", "proactive_last_ts", "last_user_ts", "last_agent_ts"):
        out[key] = max(stamps[key]) if stamps[key] else None
    out.update(dists)
    return out


def test_frozen_cells_reproduce_the_live_computation(seeded_user):
    """格子求和 == 现算。这是期3b 的承重断言:提速不许改数。"""
    live = _chat(seeded_user)
    frozen = _freeze_and_sum(seeded_user)
    for key in _EXPECTED_CHAT_COUNTS:
        assert frozen[key] == live[key], f"{key}: 冻结 {frozen[key]} != 现算 {live[key]}"
    for key in ("first_ts", "last_ts", "proactive_last_ts",
                "last_user_ts", "last_agent_ts"):
        assert frozen[key] == live[key], f"{key} 归并错"
    for key in ("by_role", "by_source", "by_content_type"):
        assert frozen[key] == live[key], f"{key} 分布不一致"


def test_frozen_cells_cover_the_two_proactive_distributions(clean_chat_rollup):
    """codex2 抓出的两条漏扫:live_activity_status / alert_status 也必须入格子,
    否则它们仍是无窗全史 GROUP BY,端点照样随历史变慢。"""
    uid = "usr_dt_golden_proactive"
    seed_user(uid)
    rows = [
        {"role": "openclaw", "source": "agent_initiated_proactive",
         "content_type": "text", "live_activity_status": "shown",
         "alert_status": "delivered"},
        {"role": "openclaw", "source": "agent_initiated_proactive",
         "content_type": "text", "live_activity_status": "shown"},
        # 非主动消息:这两个维度只统计主动消息,它不该进去
        {"role": "user", "source": "ios", "content_type": "text",
         "live_activity_status": "shown", "alert_status": "delivered"},
    ]
    for index, doc in enumerate(rows):
        mid = f"p{index}"
        db.chat_append(uid, mid, _T0 + index, {"id": mid, **doc}, 1000)
    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        live_activity, alert = conn.execute(
            "SELECT live_activity_status, alert_status FROM chat_daily_rollup"
            " WHERE user_id = %s", (uid,),
        ).fetchone()
    assert live_activity == {"shown": 2}
    assert alert == {"delivered": 1, "unknown": 1}


def test_a_closed_day_is_never_rewritten(seeded_user):
    """冻结即定型:再冻一次不改数(ON CONFLICT DO NOTHING)。

    这条同时是「历史事实语义」的执行证据 —— 冻完之后源里发生什么都不回头改。"""
    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        before = conn.execute(
            "SELECT day, total, frozen_at FROM chat_daily_rollup"
            " WHERE user_id = %s ORDER BY day", (seeded_user,),
        ).fetchall()
    # 再来一条消息落在**同一天**
    db.chat_append(seeded_user, "late", _T0 + 5,
                   {"id": "late", "role": "user", "source": "ios",
                    "content_type": "text"}, 1000)
    # ⚠️ 必须清掉水位,强迫冻结器**重新访问**那一天。
    # 这条断言我改对了两次才真正承重,过程值得写下来:
    #   v1 直接重跑冻结 —— 水位让游标从 through_day+1 起步,根本不回访旧日;
    #   v2 把 through_day 拨回 backfill_from —— 游标仍从**它的次日**起步,
    #      而种子全在那一天,照样不回访。
    # 两版拿 DO NOTHING→DO UPDATE 做突变都是绿的,即断言恒真、测的是水位不是
    # ON CONFLICT。清空水位才让冻结器真的重跑那一天。
    # (同族假绿今晚第二次:上一次是期3a 恒等式用 1:1 样本、两条路径数字巧合相等。)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM chat_rollup_watermark")
    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        after = conn.execute(
            "SELECT day, total, frozen_at FROM chat_daily_rollup"
            " WHERE user_id = %s ORDER BY day", (seeded_user,),
        ).fetchall()
    assert before == after, "已冻结的日子被改写了 —— ON CONFLICT 应该是 DO NOTHING"


def test_cleared_history_still_counts_as_having_happened(clean_chat_rollup):
    """Seven 的口径:清空历史不该让「累计发生」掉下去。

    codex2 指出的数据源后果:chat_clear 把行搬进 chat_message_archive 再删 live,
    只扫 live 的话,首次回填会永久漏掉上线前清过的历史、当天窗口也会在 clear
    瞬间骤降 —— 正是 Seven 要避免的那件事,只是范围更窄。"""
    uid = "usr_dt_golden_cleared"
    seed_user(uid)
    for index in range(4):
        mid = f"c{index}"
        db.chat_append(uid, mid, _T0 + index,
                       {"id": mid, "role": "user", "source": "ios",
                        "content_type": "text"}, 1000)
    # 模拟 clear 的权威事务:先搬进归档,再删 live
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_message_archive"
            " (user_id, source_seq, msg_id, ts, doc, clear_generation)"
            " SELECT user_id, seq, msg_id, ts, doc, 1 FROM chat_messages"
            " WHERE user_id = %s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id = %s", (uid,))

    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT total, user_messages FROM chat_daily_rollup"
            " WHERE user_id = %s", (uid,)).fetchall()
    assert rows, "clear 之后一个格子都没冻出来 —— 归档源没被扫到"
    assert sum(r[0] for r in rows) == 4
    assert sum(r[1] for r in rows) == 4


def test_a_row_present_in_both_tables_is_not_double_counted(clean_chat_rollup):
    """搬迁那一瞬间同一条会同时出现在 live 与 archive。UNION 按
    (user_id, source_seq) 去重;写成 UNION ALL 就会双计。"""
    uid = "usr_dt_golden_dual"
    seed_user(uid)
    db.chat_append(uid, "d0", _T0,
                   {"id": "d0", "role": "user", "source": "ios",
                    "content_type": "text"}, 1000)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_message_archive"
            " (user_id, source_seq, msg_id, ts, doc, clear_generation)"
            " SELECT user_id, seq, msg_id, ts, doc, 1 FROM chat_messages"
            " WHERE user_id = %s", (uid,))
    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        total = conn.execute(
            "SELECT SUM(total) FROM chat_daily_rollup WHERE user_id = %s",
            (uid,)).fetchone()[0]
    assert total == 1, "live+archive 同时存在时被双计了"


def test_transient_rows_are_counted_exactly_like_the_live_view(clean_chat_rollup):
    """verify_ping 与语音逐轮行是硬删不归档的两族。

    这里钉的是**一个被写下来的选择**:不给它们加过滤。加了会改动 total 与三个
    分布的数字(即改前端显示),而口径是「那天发生过多少」——冻结时还在就算,
    且与当时的现算逐字一致。决定性靠生命周期(当日内清掉、D+1 才冻),不靠过滤。

    ⚠️ 顺带钉死一个我犯过的错误推断:verify_ping 不进 user_messages,
    **不代表**删它不影响格子 —— 它照样进 total 和 by_source。"""
    uid = "usr_dt_golden_transient"
    seed_user(uid)
    db.chat_append(uid, "t0", _T0,
                   {"id": "t0", "role": "user", "source": "ios",
                    "content_type": "text"}, 1000)
    db.chat_append(uid, "t1", _T0 + 1,
                   {"id": "t1", "role": "user", "source": "verify_ping",
                    "content_type": "text"}, 1000)
    db.chat_append(uid, "t2", _T0 + 2,
                   {"id": "t2", "role": "user", "source": "ios",
                    "content_type": "text", "voice_call_id": "call-1"}, 1000)

    db.freeze_completed_chat_days()
    live = _chat(uid)
    frozen = _freeze_and_sum(uid)
    assert frozen["total"] == live["total"] == 3
    # verify_ping 进 total 与 by_source,但不进 user_messages —— 三者必须同时成立
    assert frozen["by_source"]["verify_ping"] == 1
    assert frozen["user_messages"] == live["user_messages"] == 2


def test_unfrozen_history_is_declared_not_silently_zero(clean_chat_rollup):
    """缺口态必须**自报**,不能长得和「真的没有」一样(Seven 定 A)。

    重写后历史只从格子读,所以冻结器没跑过的那段历史在端点上是看不见的。
    这本身不可避免(唯一的替代是回到全史现算,即被 gunicorn 打死的那条路),
    但它绝不能静默:一个 0 旁边没有覆盖范围,就会被当成「这人没用过」——
    正是 Seven 拍这条口径时要避免的那件事。
    """
    uid = "usr_dt_golden_gap"
    seed_user(uid)
    _seed_chat(uid)
    # 故意不冻
    coverage = db.chat_rollup_coverage()
    assert coverage["through_day"] is None, "还没冻却报了覆盖范围"
    assert coverage["complete"] is False

    # 一次 tick 最多推进 45 天(_CHAT_ROLLUP_MAX_DAYS_PER_TICK),积压久了要多跑
    # 几轮才追平 —— 这正是「缺口」的常见来源之一,也是 coverage 必须自报的理由。
    for _ in range(10):
        db.freeze_completed_chat_days()
        after = db.chat_rollup_coverage()
        if after["complete"]:
            break
    assert after["through_day"] is not None
    assert after["backfill_from"] is not None
    assert after["complete"] is True, f"追赶没收敛: {after}"


def test_catch_up_is_sliced_so_one_tick_cannot_wedge(clean_chat_rollup):
    """单次 tick 有上限,积压不会让一次调用变成无界回填。

    这条同时解释了上一条为什么要循环:冻结器是**分片追赶**的,首跑遇到长历史
    时 coverage 会先报 complete=False,几轮之后才追平。把它写成断言,免得后人
    看到 complete=False 就以为冻结器坏了。"""
    uid = "usr_dt_golden_backlog"
    seed_user(uid)
    _seed_chat(uid)
    first = db.freeze_completed_chat_days()
    assert 0 < len(first) <= db._CHAT_ROLLUP_MAX_DAYS_PER_TICK
    second = db.freeze_completed_chat_days()
    assert second, "第二轮没有继续推进"
    assert second[0] > first[-1], "第二轮应从上次水位之后接着冻"


def test_deleting_an_account_removes_its_chat_cells(clean_chat_rollup):
    """销号必须带走格子。

    lane 格子是匿名归并(纯结果计数,加总仍可读);chat 格子是**丢弃** —— 它带
    着这个人的活动形状(首末时刻、来源构成),把这些跨账号加总只会得到一行没人
    能解释的数。留着更糟:一个已注销的人会继续出现在活跃统计里。
    """
    uid = "usr_dt_golden_delete"
    seed_user(uid)
    _seed_chat(uid)
    db.freeze_completed_chat_days()
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_daily_rollup WHERE user_id=%s", (uid,),
        ).fetchone()[0] > 0
    db.delete_user(uid)
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM chat_daily_rollup WHERE user_id=%s", (uid,),
        ).fetchone()[0] == 0


def test_the_scheduler_actually_calls_the_chat_freeze():
    """冻结器必须被真的接到调度器上。

    这条看着像凑数,其实是本期最容易静默失败的一处:重写后端点**只**从格子读
    历史,冻结是填充它的唯一来源。调度器没接线的话,页面显示的就是「这些用户
    从没说过话」—— 一个没有任何报错、看起来完全正常的空面板。
    (同族教训:T130 的 wake 道 trace_id 全空,机制建好了但没接到这条道上。)
    """
    from admin import lane_rollup_scheduler as sched

    called: list[str] = []
    real = db.freeze_completed_chat_days
    try:
        db.freeze_completed_chat_days = (  # type: ignore[assignment]
            lambda **kw: called.append("chat") or [])
        sched._tick()
    finally:
        db.freeze_completed_chat_days = real  # type: ignore[assignment]
    assert called == ["chat"], "调度器 tick 没有调用 chat 冻结"
