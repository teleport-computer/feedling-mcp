"""model-authored responder（子项目 B 起步，C §7.5 扩展，D1 改用 summary+tail）。

**PR C9b 状态更新**：`respond()` 本身自 Task 7（chat）/ Task 8（wake）起已不再是生产回合
的驱动路径——`worker.py` 的 `process_job`/`_run_wake` 现在统一走 `tool_loop.run_tool_loop`
（provider-native 工具循环，每个模型同一套目录/同一个循环，见 worker.py 模块文档），不再
有"强制 responder round-trip"这一步。`respond()`/`ResponderError`/`_fold_action_results`/
`_action_context_str` 没有被删除——它们仍是几处真实、独立的消费者：
  - `scripts/loadtest/compare_tokens.py`：D4 的 token-per-turn 回滚门，直接驱动 `respond()`
    （以及 `planner.py`/`agent_loop.py`）来测量旧管线的 token 开销基线。
  - `tests/test_v2_no_gateway_dependency.py` / `tests/test_v2_multimodal_e2e.py`：拿 `respond()`
    当一个方便的、真实的 `provider_client` wire-shape 回归探针（网关绕过 / 多模态图片块）。
  - `worker.py` 仍在用 `ResponderError`（chat lane 空终态回复的报错信号，见
    `_on_reply`）和 `_action_context_str`（wake lane 的 screen_watch 静态预取渲染）。
详见 `tests/test_v2_no_dispatch_tiering.py`：它锁定的真正不变量是"worker.py 生产代码
不调用 `respond()`"，而不是"这个模块不存在"。

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

# 单个 action 的 grounding 上限。没有它，一个返回大 blob 的 capability 能把整个
# _ACTION_CONTEXT_CHAR_CAP 吃光，把同回合 fetch 到的记忆卡/感知全挤出 context（BUG-1 的
# 一般形式）。BLOB 键则直接丢——它们对文本 responder 永远没有意义。
_PER_ACTION_CHAR_CAP = 2000
_BLOB_KEYS = frozenset({"image_b64"})


def _strip_blobs(value: Any) -> Any:
    """递归剥掉 `_BLOB_KEYS`。dict/list 之外的值原样返回。"""
    if isinstance(value, dict):
        return {k: _strip_blobs(v) for k, v in value.items() if k not in _BLOB_KEYS}
    if isinstance(value, list):
        return [_strip_blobs(v) for v in value]
    return value


class ResponderError(Exception):
    """无法产出 model-authored 文本（无用户消息 / provider 空回复 / provider 错）。

    **PR C9b**：不再是"responder 专属"的错误类型——它现在是统一工具循环（`tool_loop.
    run_tool_loop`）的空终态回复 / 回合错误信号。`worker.py` 的 `_on_reply`（`process_job`
    chat-lane 分支）在终态文本为空时直接 `raise v2_responder.ResponderError("empty_reply")`，
    从不经过本模块的 `respond()`；`worker._safe_failure_code` 用 `isinstance(exc,
    v2_responder.ResponderError)` 对这两个生产落点（这个类 + `respond()` 内部自己抛的那些）
    做统一分类，落成 `agent_jobs.last_error` 里稳定的 plaintext 错误码。类留在 `responder.py`
    只是历史位置（`respond()` 本身仍在，见模块文档），不代表这个异常只可能从 `respond()` 里冒出来。

    `.kind`（D3 Task 7，类级默认 `""` —— 无需 `getattr` 兜底）：仅 provider 调用失败这一支
    会被填成 `provider_client.classify_provider_error(exc)` 的结果（"transient" /
    "provider_config" / "unknown"），让调用方（`worker._run_wake`）不必重新解析错误消息
    字符串就能判断是否要写支付冷却。empty_reply / no_user_messages 两支保持 `""`（它们
    根本不是 provider 错误，不适用该分类）。"""

    kind: str = ""


def _fold_action_results(action_results: dict[str, Any] | None) -> dict[str, Any]:
    """把 executor 的 action 结果折成 responder 可用的 grounding context。

    形状：`{action_type: [result_dict, ...]}`，每个 result_dict 是 capability 的
    `.to_dict()`（`{"ok", "data", ...}`）。只取 ok=True 且有 data 的那些 —— data 已由
    A 层 capability 做过截断/脱敏，对模型可见是安全的（模型本就能看用户自己的记忆/感知）。
    None/空/畸形输入一律静默忽略，绝不抛错——这是 minimal 路径（无 action）必须继续可用。

    BUG-1 defence-in-depth（parity matrix §E）：blob 键（`_BLOB_KEYS`，如 image_b64）一律
    剥掉；任一 action 序列化后超过 `_PER_ACTION_CHAR_CAP` 就整体替换成一个截断预览，
    这样单个肥 capability 就永远不能把同回合的其它 action 挤出 8000 字符的 context 预算。
    """
    ctx: dict[str, Any] = {}
    if not action_results:
        return ctx
    for action_type, runs in action_results.items():
        if not isinstance(runs, list):
            continue
        payloads = [
            _strip_blobs(r.get("data"))
            for r in runs
            if isinstance(r, dict) and r.get("ok") and r.get("data")
        ]
        if not payloads:
            continue
        folded = payloads if len(payloads) > 1 else payloads[0]
        rendered = json.dumps(folded, ensure_ascii=False)
        if len(rendered) > _PER_ACTION_CHAR_CAP:
            folded = {"_truncated": True, "preview": rendered[:_PER_ACTION_CHAR_CAP]}
        ctx[action_type] = folded
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
    usage_out: dict | None = None,
    system_prompt: str | None = None,
) -> str:
    """出一条 model-authored 回复文本。空回复 / provider 错 → ResponderError（调用方据此
    把 job 标 failed，绝不写占位气泡——no-filler 铁律）。

    D1：`summary`（早前对话摘要字符串，可为空）+ `tail`（双角色逐条消息列表）交给纯
    `context.build_turn_messages` 组装，让模型看到整段对话，而不只是待回复的用户消息。

    `action_results`（§7.5，可选）：executor 产出的 capability 结果，折进 context 让回复
    吃得到 fetch 回来的记忆/感知等 grounding 数据；省略/None 时行为不变。

    `usage_out`（Task 4，可选纯出参）：调用方（worker）传一个空 dict 进来，respond 在
    provider 调用成功后原地写入 `prompt_tokens`/`completion_tokens`（provider 未回
    usage 时两者为 None）。这是保持 responder 纯度的选择——responder 不落库、不知道
    job_id/lane，只把 usage 作为副返回值交给有 job 上下文的 worker 去记
    `jobs_store.record_turn_metric`（二选一：见 D0 rollout 计划 Task 4 note）。
    `-> str` 返回类型和 `text` 本身完全不变，D1 既有调用点/测试不受影响。

    原生 async（hosted-runtime-v2 并发修复）：worker 的回合协程直接 await 本函数，不再
    经 `asyncio.to_thread` 桥线程池——那条桥会把并发悄悄封顶在线程池大小（~32），让
    worker 池自己的并发闸形同虚设。

    `system_prompt`（D3 Task 6，可选）：省略/None 时用聊天默认的 `_SYSTEM_PROMPT`（既有
    调用点不受影响）；wake-lane 调用方（`worker._run_wake`）传入不同的 proactive 框架
    提示，让模型知道这是主动打招呼的时刻而不是在回答用户消息。
    """
    action_context = _action_context_str(action_results)
    messages = context.build_turn_messages(
        system_prompt=system_prompt if system_prompt is not None else _SYSTEM_PROMPT,
        summary=summary,
        tail=tail,
        action_context=action_context,
    )
    # Summary 现在刻意是非特权 user-role 历史数据，不能再用组装后 message role
    # 判断“有没有真实 tail”。直接检查 tail，保留 summary-only 时仍报错的不变量。
    if not any(context._has_payload(m.get("content")) for m in tail):
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
        err = ResponderError(f"provider_call_failed: {type(e).__name__}: {str(e)[:200]}")
        err.kind = provider_client.classify_provider_error(e)
        raise err from e
    if usage_out is not None:
        # 防御性双路抽取：provider 回包形状不一，取不到就是 None，绝不因缺 usage 抛错。
        u = (result or {}).get("usage") or {}
        usage_out["prompt_tokens"] = u.get("prompt_tokens")
        usage_out["completion_tokens"] = u.get("completion_tokens")
    text = str((result or {}).get("reply") or "").strip()
    if not text:
        raise ResponderError("empty_reply")
    return text
