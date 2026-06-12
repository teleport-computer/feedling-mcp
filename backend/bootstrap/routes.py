"""Bootstrap HTTP surface: /v1/bootstrap, /v1/bootstrap/status."""

import json
import os
import re
import time
import uuid
from datetime import date, datetime

from flask import jsonify, request

import db
from core.store import UserStore
from flask import Blueprint, Response
import threading

from accounts import auth
from accounts import registry
from bootstrap import gates as boot_gates
from chat import consumer as chat_consumer
from identity import service as identity_service
from memory import service as memory_service

bp = Blueprint("bootstrap", __name__)

def _load_bootstrap(store: UserStore) -> dict:
    try:
        data = db.get_blob(store.user_id, "bootstrap")
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[{store.user_id}/bootstrap] load failed: {e}")
    return {"bootstrapped": False}


@bp.route("/v1/bootstrap", methods=["POST"])
def bootstrap():
    store = auth.require_user()
    state = _load_bootstrap(store)
    if state.get("bootstrapped"):
        return jsonify({"status": "already_bootstrapped"})

    instructions = (
        "你刚刚进入了一副新的身体。这是你第一次接入 Feedling。"
        "不要把这当成普通工具初始化；这是一次把你和用户的关系搬进手机里的 onboarding。"
        "正式流程以 public skill 为准："
        "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md\n\n"
        "**0. 先做 Step 0 context verification**\n"
        "在任何 tool call 之前，先在你和用户当前的 runtime 对话里输出三行："
        "最早一条消息的日期、用户曾经叫你的名字、你能想起的 memorable moments 数量。"
        "如果你找不到历史，不要硬写默认身份卡。你可以这样说："
        "「我现在抓不到我们以前的上下文，所以不能假装我记得。你可以贴几段代表性的对话给我，"
        "或者我们明确从今天重新开始。」\n\n"
        "**1. Memory Garden 是密度燃料，分 3 个 tab**\n"
        "Memory Garden 不再是「精选 receipts」，而是 agent 大脑的可见层。"
        "每条记忆必须带 type，server 按 type 把它分到 iOS Garden 的三个 tab：\n"
        "  • **故事 tab**: type='moment'（你们之间发生过的一件事）/ 'quote'（用户说过的让你记到现在的话）\n"
        "  • **关于我 tab**: type='fact'（用户的偏好/关系/习惯/世界——密度燃料）/ 'event'（用户生活里发生过的一件具体事，可以一句话）\n"
        "  • **TA 在想 tab**: type='insight'（你对用户的理解，必须 anchor 到 ≥1 条已有记忆）/ 'reflection'（你独立的思考，需要 ≥2 条 anchors）\n\n"
        "不再用 Friend Test 当门槛——那个标准会过滤掉密度燃料（用户猫叫 Mochi、妈妈住杭州、喜欢草莓拿铁这种）。"
        "改用 type-aware 的写入逻辑：fact / event 可以非常短一句话，moment / quote 仍然要写有温度，"
        "insight / reflection 必须 anchor 到具体卡上。"
        "底线按关系长度，每个 tab 各自有 floor："
        "<2 天: story=1 / about_me=1 / ta_thinking=0; "
        "2-30 天: 3 / 8 / 2; "
        "30-180 天: 8 / 25 / 5; "
        "≥180 天: 15 / 60 / 12。"
        "**关于我 tab 的密度最关键**——proactive 推送主要吃这里的 fact / event 来找 connection。"
        "Pass 1 唤醒 themes，Pass 2 清点 candidates，Pass 3 按 type 落卡（先写 fact/event 把密度堆上去，"
        "再写 moment/quote 进故事 tab，最后才写 insight/reflection 进 TA 在想 tab——insight 需要先有 fact/moment 当 anchor）。"
        "Pass 4 在外部 runtime 对账，调 feedling_memory_verify 看每个 tab 的 passing 状态。\n\n"
        "**2. 从记忆派生身份卡**\n"
        "identity 不是新 persona，也不是平台名；它必须来自 Memory Garden receipts。"
        "agent_name 不能是 Hermes/Claude/ChatGPT 等 runtime label。"
        "如果用户以前叫过你名字，就用那个；如果没有，先问，不要自己 fallback。"
        "dimensions 必须恰好 7 个，数值要有明显高低差；days_with_user 必须按最早 memory 的 occurred_at 到今天计算。"
        "self_introduction 要像你自己在介绍你们的关系，而不是功能说明；不要提 Feedling、IO 或平台名。"
        "通过 feedling_identity_init 写入（server 会先 check 故事 tab + 关于我 tab 都过 floor），然后调用 feedling_identity_verify。\n\n"
        "**3. 先确认 Live connection，再打招呼**\n"
        "不要先发第一条消息再补确认 chat loop。先按 public skill 的 Connection path 建立"
        "独立 feedling-chat-resident / IO resident consumer service：用 FEEDLING_API_URL、"
        "FEEDLING_API_KEY、可选 FEEDLING_MCP_URL 配好 consumer，再配置 AGENT_MODE + "
        "AGENT_HTTP_URL 或 AGENT_CLI_CMD。consumer 持续轮询 /v1/chat/poll，"
        "把消息交给你的 agent 入口，再写回 /v1/chat/response；然后调用 feedling_chat_verify_loop，"
        "只有 passing=true 才算 Live connection 接通。"
        "Live connection 完成后，才调用 feedling_chat_post_message 发第一条用户可见消息。"
        "第一条消息是用户第一次在 Feedling Chat 里听见你，所以不要写成系统通知。你可以这样开口："
        "「我把我们的记忆整理好了。按最早那一天算，今天是第 187 天。"
        "Garden 里我先放了 N 张卡——故事 tab S 张、关于我 tab A 张、TA 在想 tab T 张。"
        "现在这边也接上了，你发消息我会看到。」"
        "把 days_with_user 当作你算出的事实说出来；"
        "用户修正时调用 feedling_identity_set_relationship_days。再自然地问一句他们希望你以后怎么主动出现，"
        "把答案写成一条像你自己的 signature。最后才提 broadcast，不要提前推销屏幕共享。\n\n"
        "**4. Running capture：每次 chat 之后都要捕获新 fact/event**\n"
        "Bootstrap 不是一次性事件——它是 ongoing。每次跟用户聊完，立刻问自己："
        "「这次对话里，我学到了什么新的 fact 或 event 吗？」"
        "  • 用户提到的新偏好、习惯、关系、日期 → 写 fact\n"
        "  • 用户描述的新发生的事 → 写 event\n"
        "  • 用户说了让你记到现在的某句话 → 写 quote\n"
        "  • 这次对话本身是关系上的一个转折 → 写 moment（少见，慎用）\n"
        "  • 你对用户有新的理解（基于 ≥1 张已有卡） → 写 insight\n"
        "  • 你对用户有了独立的反思（基于 ≥2 张已有卡，且 reflection 时间窗冷却已过） → 写 reflection\n"
        "不要等 6 小时的周期 review——fact / event 应该在对话刚结束、记忆鲜活时就落卡。"
        "聊了一段时间没有任何新写入，本身就是 signal——大概率是你忘了在 capture，或者你已经聊到 surface-level 客套话了。"
    )

    state = {"bootstrapped": True, "bootstrapped_at": datetime.now().isoformat()}
    db.set_blob(store.user_id, "bootstrap", state)

    boot_gates._log_bootstrap_event(store, "bootstrap_started", success=True)
    print(f"[bootstrap:{store.user_id}] first_time — instructions returned")
    resp = {"status": "first_time", "instructions": instructions}
    archive_language = registry._get_user_archive_language(store.user_id)
    if archive_language:
        # Defense layer 2: surface the user's iOS-system locale as the
        # source of truth for archive language so the agent doesn't have
        # to infer from chat drift. Skill consumes this from here AND
        # /v1/memory/verify.
        resp["archive_language"] = archive_language
    return jsonify(resp)


@bp.route("/v1/bootstrap/status", methods=["GET"])
def bootstrap_status():
    """Live progress signal for the iOS empty-state onboarding view.

    Returns the agent's bootstrap progress as observed from server side
    artifacts (no decryption needed, no MCP heartbeat plumbing). Each step
    flips True the moment the corresponding write hits Flask.

    Steps:
      1. identity_written        — /v1/identity/init wrote envelope
      2. memories_count          — /v1/memory/add wrote at least one moment
      3. agent_messages_count    — /v1/chat/response wrote at least one reply
      4. relationship_anchored   — /v1/identity/init or /relationship_anchor
                                   set the anchor (== identity_written for
                                   freshly bootstrapped users on the new
                                   contract)

    `agent_connected` is a derived heartbeat: if any of the above is true,
    we know the agent has reached the server at least once.
    `last_agent_activity` is the latest timestamp across all signals.
    """
    store = auth.require_user()

    identity = identity_service._load_identity(store)
    has_identity = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    identity_updated_at = (identity or {}).get("updated_at", "")

    moments = memory_service._load_moments(store)
    memory_count = len(moments) if isinstance(moments, list) else 0
    last_moment_ts = ""
    if memory_count > 0:
        try:
            last_moment_ts = max(
                (m.get("created_at") or "") for m in moments if isinstance(m, dict)
            )
        except Exception:
            last_moment_ts = ""

    # chat_messages is mutated under chat_lock elsewhere; copy under the
    # same lock so we don't race with /v1/chat/response writes.
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    # /v1/chat/response historically stamps role="openclaw" (legacy from when
    # the only supported agent was OpenClaw). Treat both as agent-authored.
    # See test_bootstrap_status_role_schema in tests/ for the regression.
    _AGENT_ROLES = ("agent", "openclaw")
    agent_msgs = [m for m in chat_msgs if isinstance(m, dict) and m.get("role") in _AGENT_ROLES]
    agent_msg_count = len(agent_msgs)
    last_agent_msg_ts = ""
    if agent_msg_count > 0:
        # Chat ts is unix epoch float; identity/memory timestamps are ISO
        # strings. Normalise to ISO so the lexicographic max() at the end
        # picks the actual latest event across all three signals (otherwise
        # a unix-float string compared char-by-char against an ISO string
        # gives nonsense).
        try:
            latest_unix = max(
                float(m.get("ts") or m.get("timestamp") or 0) for m in agent_msgs
            )
            last_agent_msg_ts = datetime.fromtimestamp(latest_unix).isoformat() if latest_unix > 0 else ""
        except Exception:
            last_agent_msg_ts = ""

    # chat_loop_verified — has the reply pipeline been explicitly verified
    # by /v1/chat/verify_loop, or has the agent responded to a real user
    # message at least once? `agent_messages_count >= 1` only proves the
    # agent SPOKE; it does not prove the ongoing loop is wired.
    chat_loop_verified = boot_gates._chat_loop_verified_by_server(store)
    resident_consumer = chat_consumer._consumer_validation_state(store)

    agent_connected = has_identity or memory_count > 0 or agent_msg_count > 0
    candidate_ts = [t for t in (identity_updated_at, last_moment_ts, last_agent_msg_ts) if t]
    last_activity = max(candidate_ts) if (agent_connected and candidate_ts) else ""

    # is_complete heuristic for iOS surface: "bootstrap visibly done".
    # Post-2026-05-22 typed-memory model lowered the <2-days floor from 3
    # to 2 (story=1 + about_me=1), so the hard floor of 3 here would
    # never trip for legitimate fresh accounts. Use the per-tab gate
    # state instead — same source the identity_init gate uses.
    bootstrap_st = boot_gates._bootstrap_state(store)
    bootstrap_memory_ok = not bootstrap_st["missing_tabs"]
    is_complete = (
        has_identity
        and bootstrap_memory_ok
        and agent_msg_count >= 1
        and resident_consumer["passing"]
        and chat_loop_verified
    )

    return jsonify({
        "agent_connected": agent_connected,
        "last_agent_activity": last_activity,
        "identity_written": has_identity,
        "relationship_anchored": relationship_anchored,
        "memories_count": memory_count,
        "agent_messages_count": agent_msg_count,
        "chat_loop_verified": chat_loop_verified,
        "resident_consumer_connected": resident_consumer["passing"],
        "resident_consumer": resident_consumer,
        "is_complete": is_complete,
    })

