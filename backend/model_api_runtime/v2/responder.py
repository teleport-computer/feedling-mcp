"""model-authored responder（子项目 B 起步，C §7.5 扩展，D1 改用 summary+tail）。

用**用户自己的 BYOK** ProviderConfig 出最终回复（spec §7.3 不变量：无平台 key 兜底）。
D1：不再只喂"合并的未回复用户消息"——改为消费一个 `summary`（早前对话摘要字符串）+
`tail`（双角色逐条消息列表），组装委托给纯函数 `context.build_turn_messages`，让模型
看到整段对话（摘要 + 双角色逐字尾巴），而不只是待回复的用户轮次。
C 阶段扩展：executor 产出的 `action_results`（capability 输出，如记忆卡片/感知摘要）折进
context，让回复吃得到 fetch 回来的东西——`action_results=None` 时行为与不带该 kwarg 完全
一致，no-filler、BYOK-only、空回复报错三条不变量全部照旧。

依赖方向：本模块只 import provider_client（底层）+ 同层的 v2.context（纯组装，无 I/O），
不 import hosted.*（在其上层）。provider-key 的单次解密由 worker 的注入式 resolve_provider
完成（见 worker.py / serve_worker.py）。
"""
from __future__ import annotations

import json
from typing import Any

import provider_client

from model_api_runtime.v2 import context

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


def _action_context_str(action_results: dict[str, Any] | None) -> str:
    """把 `_fold_action_results` 折出的 grounding dict 渲染成 context.build_turn_messages
    要的纯字符串（截断保护，见 `_ACTION_CONTEXT_CHAR_CAP`）；无可折内容时返回空串。"""
    folded = _fold_action_results(action_results)
    if not folded:
        return ""
    return (
        "Grounding context fetched for this turn (memory cards, perception, "
        "etc.) — use it if relevant, do not narrate that it was fetched:\n"
        + json.dumps(folded, ensure_ascii=False)[:_ACTION_CONTEXT_CHAR_CAP]
    )


async def respond(
    *,
    provider_config: Any,
    summary: str,
    tail: list[dict],
    action_results: dict[str, Any] | None = None,
) -> str:
    """出一条 model-authored 回复文本。空回复 / provider 错 → ResponderError（调用方据此
    把 job 标 failed，绝不写占位气泡——no-filler 铁律）。

    D1：`summary`（早前对话摘要字符串，可为空）+ `tail`（双角色逐条消息列表）交给纯
    `context.build_turn_messages` 组装，让模型看到整段对话，而不只是待回复的用户消息。

    `action_results`（§7.5，可选）：executor 产出的 capability 结果，折进 context 让回复
    吃得到 fetch 回来的记忆/感知等 grounding 数据；省略/None 时行为不变。

    原生 async（hosted-runtime-v2 并发修复）：worker 的回合协程直接 await 本函数，不再
    经 `asyncio.to_thread` 桥线程池——那条桥会把并发悄悄封顶在线程池大小（~32），让
    worker 池自己的并发闸形同虚设。
    """
    action_context = _action_context_str(action_results)
    messages = context.build_turn_messages(
        system_prompt=_SYSTEM_PROMPT,
        summary=summary,
        tail=tail,
        action_context=action_context,
    )
    # 除了 system 块（system prompt / summary / action context）没有任何 tail 轮次
    # 折进来——保留原有"无用户消息报错"不变量，现在是对 tail 判的。
    if not any(m["role"] != "system" for m in messages):
        raise ResponderError("no_user_messages")
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
