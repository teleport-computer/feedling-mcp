"""model-authored responder（子项目 B 起步，C §7.5 扩展）。

用**用户自己的 BYOK** ProviderConfig 出最终回复（spec §7.3 不变量：无平台 key 兜底）。
B 阶段无 planner——直接把合并的用户消息交给 provider，取 model-authored 文本。
C 阶段扩展：executor 产出的 `action_results`（capability 输出，如记忆卡片/感知摘要）折进
context，让回复吃得到 fetch 回来的东西；对外签名新增一个可选 kwarg：
`respond(*, provider_config, coalesced_messages, runtime_state, action_results=None) -> str`。
默认 `None` 保持 B 阶段调用方 / 既有测试行为不变——no-filler、BYOK-only、空回复报错三条
不变量全部照旧，`action_results` 只是额外折进 prompt 的 grounding context。

依赖方向：本模块只 import provider_client（底层），不 import hosted.*（在其上层）。
provider-key 的单次解密由 worker 的注入式 resolve_provider 完成（见 worker.py / serve_worker.py）。
"""
from __future__ import annotations

import json
from typing import Any

import provider_client

# 最小系统提示：no-filler 铁律——回复即 model-authored 聊天气泡内容，无占位、无“正在处理”。
_SYSTEM_PROMPT = (
    "You are the user's personal companion. Reply directly and concisely to the "
    "user's latest messages. Do not narrate tool use or system status."
)

_MAX_TOKENS = 700
_TEMPERATURE = 0.7
_TIMEOUT_SEC = 60.0
_ACTION_CONTEXT_CHAR_CAP = 8000


class ResponderError(Exception):
    """responder 无法产出 model-authored 文本（无用户消息 / provider 空回复 / provider 错）。"""


def _fold_action_results(action_results: dict[str, Any] | None) -> dict[str, Any]:
    """把 executor 的 action 结果折成 responder 可用的 grounding context。

    形状：`{action_type: [result_dict, ...]}`，每个 result_dict 是 capability 的
    `.to_dict()`（`{"ok", "data", ...}`）。只取 ok=True 且有 data 的那些 —— data 已由
    A 层 capability 做过截断/脱敏，对模型可见是安全的（模型本就能看用户自己的记忆/感知）。
    None/空/畸形输入一律静默忽略，绝不抛错——这是 minimal 路径（无 action）必须继续可用。
    """
    ctx: dict[str, Any] = {}
    if not action_results:
        return ctx
    for action_type, runs in action_results.items():
        if not isinstance(runs, list):
            continue
        payloads = [
            r.get("data")
            for r in runs
            if isinstance(r, dict) and r.get("ok") and r.get("data")
        ]
        if payloads:
            ctx[action_type] = payloads if len(payloads) > 1 else payloads[0]
    return ctx


def _build_messages(
    coalesced_messages: list[dict],
    runtime_state: dict,
    action_results: dict[str, Any] | None = None,
) -> list[dict]:
    user_turns = [
        {"role": "user", "content": str(m.get("content") or "")}
        for m in coalesced_messages
        if str(m.get("role") or "") == "user" and str(m.get("content") or "").strip()
    ]
    if not user_turns:
        raise ResponderError("no_user_messages")
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    action_context = _fold_action_results(action_results)
    if action_context:
        messages.append({
            "role": "system",
            "content": (
                "Grounding context fetched for this turn (memory cards, perception, "
                "etc.) — use it if relevant, do not narrate that it was fetched:\n"
                + json.dumps(action_context, ensure_ascii=False)[:_ACTION_CONTEXT_CHAR_CAP]
            ),
        })
    messages.extend(user_turns)
    return messages


async def respond(
    *,
    provider_config: Any,
    coalesced_messages: list[dict],
    runtime_state: dict,
    action_results: dict[str, Any] | None = None,
) -> str:
    """出一条 model-authored 回复文本。空回复 / provider 错 → ResponderError（调用方据此
    把 job 标 failed，绝不写占位气泡——no-filler 铁律）。

    `action_results`（§7.5，可选）：executor 产出的 capability 结果，折进 context 让回复
    吃得到 fetch 回来的记忆/感知等 grounding 数据；省略/None 时行为与 B 阶段完全一致。

    原生 async（hosted-runtime-v2 并发修复）：worker 的回合协程直接 await 本函数，不再
    经 `asyncio.to_thread` 桥线程池——那条桥会把并发悄悄封顶在线程池大小（~32），让
    worker 池自己的并发闸形同虚设。
    """
    messages = _build_messages(coalesced_messages, runtime_state, action_results)
    try:
        result = await provider_client.reliable_chat_completion_async(
            provider_config,
            messages,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            timeout=_TIMEOUT_SEC,
        )
    except Exception as e:  # noqa: BLE001 — 归一成 ResponderError 交给 worker 落 last_error
        raise ResponderError(f"provider_call_failed: {type(e).__name__}: {str(e)[:200]}") from e
    text = str((result or {}).get("reply") or "").strip()
    if not text:
        raise ResponderError("empty_reply")
    return text
