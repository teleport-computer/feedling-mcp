"""V2 记忆抽取（capture / dream）的**纯**核心：BYOK LLM 调用 + 解析 + 卡片→memory action。

依赖方向：只 import provider_client（底层）和 stdlib/typing。**绝不**依赖任何托管运行时、
数据库/持久化层或记忆落库模块 —— prompt 构造、解析函数、信封构造与落库都由调用方（worker）
经参数/注入回调提供，这样本模块可以保持零 I/O、可单测（见源码顶层的依赖方向 grep 测试）。

`cards_to_actions` / `consolidations_to_actions` 是从 `tools/chat_resident_consumer.py`
的 `_capture_actions_from_cards` 移植过来的纯映射逻辑（spec §3.3）。resident 保留它自己的
副本直到退役——刻意的重复，换 kill-resident 之前的零风险。
"""
from __future__ import annotations

from typing import Any, Callable

import provider_client

_MAX_TOKENS = 1500
_TEMPERATURE = 0.3
_TIMEOUT_SEC = 90.0


async def extract(
    *,
    provider_config: Any,
    prompt: str,
    parse: Callable[[str], tuple],
    max_tokens: int = _MAX_TOKENS,
    progress_cb: Callable[[str, int], None] | None = None,
) -> tuple[Any, str | None]:
    """跑一次 BYOK 抽取调用并解析。**永不抛**——失败一律返回 (None, reason)。

    `parse` 是 memory/*_prompt_v1 里的纯解析函数。它的返回是 (value, err) 或
    (value, questions, err)；我们只取首项与末项（末项恒为 err）。
    """
    messages = [{"role": "user", "content": prompt}]
    try:
        result = await provider_client.reliable_chat_completion_async(
            provider_config, messages,
            max_tokens=max_tokens, temperature=_TEMPERATURE, timeout=_TIMEOUT_SEC,
            progress_cb=progress_cb,
        )
    except Exception as e:  # noqa: BLE001 — 背景 job：归一成 reason，绝不抛
        return None, f"provider_call_failed:{type(e).__name__}"
    reply = str((result or {}).get("reply") or "").strip()
    if not reply:
        return None, "empty_reply"
    parsed = parse(reply)
    value, err = parsed[0], parsed[-1]
    if err:
        return None, str(err)
    return value, None


def _inner_from_card(card: dict) -> dict:
    return {
        "summary": str(card.get("summary") or "").strip(),
        "content": str(card.get("content") or "").strip(),
        "bucket": str(card.get("bucket") or "").strip(),
        "threads": list(card.get("threads") or []),
    }


def _to_actions(
    cards: list[dict],
    *,
    occurred_at: str,
    source_ids: list[str],
    build_envelope: Callable[[dict], dict],
    capture_mode: str,
    reason: str,
) -> tuple[list[dict], int, int]:
    actions: list[dict] = []
    added = 0
    superseded = 0
    for card in cards or []:
        action = str(card.get("action") or "").strip().lower()
        target_id = str(card.get("target_id") or "").strip()
        base = {
            "envelope": build_envelope(_inner_from_card(card)),
            "reason": reason,
            "capture_mode": capture_mode,
            "source_chat_message_ids": list(source_ids),
        }
        if action == "add" or (action in {"merge", "supersede"} and not target_id):
            actions.append({"type": "memory.add", **base})
            added += 1
            continue
        if action in {"merge", "supersede"} and target_id:
            actions.append({"type": "memory.supersede", "supersedes": target_id, **base})
            superseded += 1
    if cards and not actions:
        # 模型给了卡但一张都没映射成 action —— 说明它返回了我们不认识的 action 名。
        # 静默写零条会把这件事藏起来，所以硬失败（与 resident 同口径）。
        raise ValueError("capture_no_memory_actions")
    return actions, added, superseded


def cards_to_actions(cards, *, occurred_at, source_ids, build_envelope):
    return _to_actions(cards, occurred_at=occurred_at, source_ids=source_ids,
                       build_envelope=build_envelope, capture_mode="memory_capture",
                       reason="Memory captured from a completed chat window.")


def consolidations_to_actions(consolidations, *, occurred_at, source_ids, build_envelope):
    return _to_actions(consolidations, occurred_at=occurred_at, source_ids=source_ids,
                       build_envelope=build_envelope, capture_mode="memory_dream",
                       reason="Memory consolidated during a dream pass.")
