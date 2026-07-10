"""V2 short planner（spec §7.2/7.3）。

official/可信模型 → 用**用户自己的 BYOK key** 的结构化 JSON planner（Task 4）；
弱/杂牌模型 → **确定性、零 LLM** 的规则 planner。**不存在平台级 LLM key 兜底**（§7.3 硬不变量）。
planner 只出 [{type,payload}]（≤5），非响应 action 不产生可见文本，需回复则含末位 final_response。
"""
from __future__ import annotations

import json
from typing import Any

import provider_client

# 封闭动作词表（§4.3，NO recent_chat_digest——它不是 capability，digest 在 worker 确定性构建）。
# 词表外一律丢弃。final_response 是唯一可见/作者 action。
#
# chat_image_read 被**故意移出**词表（BUG-1，见 docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md §E）：
# 它返回原始 image_b64，会经 responder 的文本 grounding context 挤掉记忆卡/感知。多模态那一轮
# 会给 provider_client 真正的 image content block，届时把它加回来。
_READ_ACTIONS = frozenset({
    "identity_get", "memory_index", "memory_fetch", "memory_search",
    "perception_snapshot", "perception_trend", "perception_history",
    "screen_recent", "screen_read", "photo_recent", "photo_read",
    "web_search", "web_fetch",
})
_WRITE_ACTIONS = frozenset({
    "memory_write", "identity_patch", "schedule_wake", "cancel_wake",
})

MAX_PLAN_ACTIONS = 5

# 喂回 planner 的上轮结果预览上限。别喂原始 data——那会把 planner 的 prompt 撑成
# responder 的 grounding context，两轮就爆。planner 只需要知道「查到了什么量级的东西」
# 来决定还要不要再查。
_PRIOR_PREVIEW_CHARS = 600


def _compact_prior(prior_action_results: dict[str, Any] | None) -> dict[str, Any]:
    """把上一轮的 action 结果压成 planner 可读的极简摘要：成败计数 + 截断预览。"""
    out: dict[str, Any] = {}
    for action_type, runs in (prior_action_results or {}).items():
        if not isinstance(runs, list):
            continue
        ok = [r for r in runs if isinstance(r, dict) and r.get("ok")]
        fail = [r for r in runs if isinstance(r, dict) and not r.get("ok")]
        first_data = ok[0].get("data") if ok else None
        preview = json.dumps(first_data, ensure_ascii=False)[:_PRIOR_PREVIEW_CHARS] if first_data else ""
        out[action_type] = {"ok_count": len(ok), "fail_count": len(fail), "preview": preview}
    return out


def validate_plan(raw: Any) -> list[dict[str, Any]]:
    """把模型/规则输出收敛成安全、有序、≤5 的 action 列表。

    丢未知类型；至多一个 final_response 且置于末位；给 final_response 留位后截断。
    永不抛异常——垃圾 plan 退化为 []。
    """
    plan_in = raw.get("plan") if isinstance(raw, dict) else raw
    if not isinstance(plan_in, list):
        return []
    steps: list[dict[str, Any]] = []
    wants_reply = False
    for item in plan_in:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip()
        if t == "final_response":
            wants_reply = True
            continue
        if t not in _READ_ACTIONS and t not in _WRITE_ACTIONS:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            # 容忍扁平形状 {"type":"memory_fetch","ids":[...]}
            payload = {k: v for k, v in item.items() if k not in ("type", "payload")}
        steps.append({"type": t, "payload": payload})
    if wants_reply:
        steps = steps[: MAX_PLAN_ACTIONS - 1]
        steps.append({"type": "final_response", "payload": {}})
    else:
        steps = steps[:MAX_PLAN_ACTIONS]
    return steps


def rule_plan(*, coalesced_messages: list[dict], memory_index: dict, lane: str) -> list[dict[str, Any]]:
    """弱/杂牌模型的**确定性、零 LLM** planner（§7.3）。绝不调用任何 provider。

    chat lane 或有用户文本 → 便宜读近期记忆卡（若 index 里有）再 final_response；
    wake 且无可见输入、不值得回复 → sleep。parity 压在 responder（用户 key 出最终回复）。
    """
    has_user_text = any(str(m.get("content") or "").strip() for m in coalesced_messages)
    if lane == "chat" or has_user_text:
        steps: list[dict[str, Any]] = []
        items = (memory_index or {}).get("items") or []
        ids = [str(it.get("id")) for it in items[:3] if it.get("id")]
        if ids:
            steps.append({"type": "memory_fetch", "payload": {"ids": ids}})
        steps.append({"type": "final_response", "payload": {}})
        return steps
    # `sleep` is a deterministic wake-lane CONTROL signal, not part of the LLM-facing
    # _WRITE_ACTIONS vocabulary (removed there in Task 4) — the executor gracefully
    # skips it, and wake-lane semantics (what "sleeping" means, retries, etc.) belong
    # to subproject D (proactive), out of scope for the foreground chat path here.
    return [{"type": "sleep", "payload": {"reason": "no_visible_input"}}]


async def plan(
    store,
    *,
    provider_config: "provider_client.ProviderConfig",
    is_official: bool,
    coalesced_messages: list[dict],
    digest: dict,
    memory_index: dict,
    perception_summary: dict,
    runtime_state: dict,
    lane: str,
    reason: str,
    prior_action_results: dict | None = None,
) -> list[dict[str, Any]]:
    """回合 planner。is_official=False → 确定性规则（零 LLM）；True → 用户 BYOK provider_config 结构化 JSON planner。

    provider_config 是 worker JIT 解密的用户自己的 BYOK 凭证（含 provider/model/base_url/api_key）；driver
    内含其中，is_official 由 worker 用 _is_official_identity 派生后传入。

    原生 async（hosted-runtime-v2 并发修复）：official_plan 内部 await 用户 BYOK 的
    provider 调用，不再经 `asyncio.to_thread` 桥线程池；rule_plan 零 LLM，本就是纯函数，
    直接同步跑完即可（不必也不应该为它单开一个协程调度点）。

    prior_action_results（agent_loop 累积的本回合已完成 action 结果，形如
    {action_type: [{"ok": bool, "data": ...}, ...]}）只喂给 official_plan——rule_plan
    是确定性规则，不从工具结果里学习，忽略此参数（弱/杂牌模型用户实质上仍是单轮）。
    """
    if not is_official:
        return rule_plan(coalesced_messages=coalesced_messages, memory_index=memory_index, lane=lane)
    return await official_plan(
        provider_config=provider_config,
        coalesced_messages=coalesced_messages, digest=digest, memory_index=memory_index,
        perception_summary=perception_summary, runtime_state=runtime_state,
        lane=lane, reason=reason, prior_action_results=prior_action_results,
    )


_PLANNER_SYSTEM = (
    "You are Feedling's turn planner for the foreground chat path. Output ONLY a JSON "
    'object {"plan":[{"type":"...","payload":{...}}],"reason":"..."}. '
    "Choose 1-5 short actions from this EXACT vocabulary: "
    "identity_get, memory_index, memory_fetch, memory_search, perception_snapshot, "
    "perception_trend, perception_history, screen_recent, screen_read, photo_recent, "
    "photo_read, web_search, web_fetch, memory_write, identity_patch, "
    "schedule_wake, cancel_wake, "
    "final_response. memory_search is keyword/grep search over memory cards (needs a "
    "payload.query string) — prefer it over memory_index when the user asks to find/recall "
    "something specific. "
    "Rules: prefer the SHORTEST plan; non-response actions must not produce visible text; "
    "do not mutate state without a strong reason; a reply is always warranted here, so "
    "include final_response LAST. "
    "If `prior_action_results` is present, it holds what THIS turn's earlier tool rounds "
    "already returned: request more actions only if they are still missing something, "
    "otherwise include final_response now. You get at most 3 rounds. "
    "schedule_wake takes payload.at (ISO time or a relative spec like '2h') and optional "
    "tz/reason; cancel_wake takes payload.wake_id. Use them only when the user actually "
    "asks to be reminded or checked on later. "
    "Never wrap the JSON in Markdown."
)


def _planner_user_payload(
    *, coalesced_messages, digest, memory_index, perception_summary, runtime_state, lane, reason,
    prior_action_results=None,
) -> dict:
    payload = {
        "lane": lane,
        "reason": reason,
        "messages": [{"content": str(m.get("content") or "")[:2000]} for m in coalesced_messages[-8:]],
        "recent_chat_digest": digest,
        "memory_index": memory_index,
        "perception_summary": perception_summary,
        "runtime_state": runtime_state or {},   # 只含非敏感 digest（无 provider 三元组、无 key）
    }
    compact = _compact_prior(prior_action_results)
    if compact:
        payload["prior_action_results"] = compact
    return payload


def _parse_plan_json(reply: str) -> Any:
    """从模型回复里抠出 JSON（剥 markdown fence + 取首尾花括号）。永不抛，失败返回 {}。

    provider_client 只对 openai-wire 原生透传 response_format；anthropic/gemini 仅注入软指令，
    故必须防御式解析。"""
    text = str(reply or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1 and text[:nl].strip().isalpha():  # 丢掉 ```json 这类语言标记行
            text = text[nl + 1:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start: end + 1]
    try:
        return json.loads(text)
    except Exception:
        return {}


async def official_plan(
    *,
    provider_config: "provider_client.ProviderConfig",
    coalesced_messages: list[dict],
    digest: dict,
    memory_index: dict,
    perception_summary: dict,
    runtime_state: dict,
    lane: str,
    reason: str,
    prior_action_results: dict | None = None,
) -> list[dict[str, Any]]:
    """轻量结构化 JSON planner，跑用户自己的 **BYOK provider_config**（§7.2/7.3）。

    解析失败 / 空 plan / provider 错误 → 回退确定性规则（不换平台 key，parity 压 responder）。

    原生 async（hosted-runtime-v2 并发修复）：worker 直接 await 本函数，不再经
    `asyncio.to_thread` 桥线程池——那条桥会把并发悄悄封顶在线程池大小（~32）。
    """
    payload = _planner_user_payload(
        coalesced_messages=coalesced_messages, digest=digest,
        memory_index=memory_index, perception_summary=perception_summary,
        runtime_state=runtime_state, lane=lane, reason=reason,
        prior_action_results=prior_action_results,
    )
    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:12000]},
    ]
    try:
        result = await provider_client.reliable_chat_completion_async(
            provider_config, messages,
            max_tokens=400, temperature=0.0, timeout=30.0,
            response_format={"type": "json_object"},
            require_reply=True, max_attempts=2,
        )
    except Exception:
        return rule_plan(coalesced_messages=coalesced_messages, memory_index=memory_index, lane=lane)
    steps = validate_plan(_parse_plan_json(result.get("reply") or ""))
    if not steps:
        return rule_plan(coalesced_messages=coalesced_messages, memory_index=memory_index, lane=lane)
    return steps
