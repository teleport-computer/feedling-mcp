"""V2 记忆抽取（capture / dream）的**纯**核心：BYOK LLM 调用 + 解析 + 卡片→memory action。

依赖方向：只 import provider_client（底层）和 stdlib/typing。**绝不**依赖任何托管运行时、
数据库/持久化层或记忆落库模块 —— prompt 构造、解析函数、信封构造与落库都由调用方（worker）
经参数/注入回调提供，这样本模块可以保持零 I/O、可单测（见源码顶层的依赖方向 grep 测试）。

`cards_to_actions` / `consolidations_to_actions` 是从 `tools/chat_resident_consumer.py`
的 `_capture_actions_from_cards` 移植过来的纯映射逻辑（spec §3.3）。resident 保留它自己的
副本直到退役——刻意的重复，换 kill-resident 之前的零风险。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, NamedTuple

import provider_client

_MAX_TOKENS = 1500
_TEMPERATURE = 0.3
_TIMEOUT_SEC = 90.0

# Dream is maintenance, not a bulk rewrite lane.  These are hard safety
# bounds rather than model instructions: a weak or compromised provider cannot
# bypass them by returning a larger batch.
DREAM_MAX_CONSOLIDATIONS = 5
DREAM_MAX_SUPERSEDED_CARDS = 4
DREAM_OUTPUT_COOLDOWN_SEC = 7 * 24 * 60 * 60
DREAM_MERGE_MIN_TRIGRAM_OVERLAP = 0.30
DREAM_MERGE_MIN_SHARED_TRIGRAMS = 3


def _iso_epoch(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def dream_card_is_eligible(card: dict, *, now_epoch: float) -> bool:
    """Whether an existing card may be consumed by this Dream run.

    Fresh Dream output is deliberately unavailable for seven days.  This
    breaks the nightly output->input feedback loop at both prompt construction
    and deterministic action mapping.  Missing provenance timestamps fail
    closed for Dream-authored cards; ordinary Capture/import cards are not
    delayed.
    """

    if not isinstance(card, dict):
        return False
    source = str(card.get("source") or card.get("capture_mode") or "").strip()
    if source != "memory_dream":
        return True
    created = _iso_epoch(card.get("created_at") or card.get("occurred_at"))
    return created is not None and now_epoch - created >= DREAM_OUTPUT_COOLDOWN_SEC


def _semantic_text(card: dict) -> str:
    values: list[str] = []
    for key in ("title", "summary", "description", "content"):
        value = str(card.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return " ".join(values)


def _normalized_semantic_text(card: dict) -> str:
    return "".join(re.findall(r"[\w]", _semantic_text(card).lower(), flags=re.UNICODE))


def _has_substantive_increment(old_card: dict, result: dict) -> bool:
    """Conservative deterministic fence for one-card Dream rewrites.

    A 1:1 ``thicken``/``supersede`` must contain materially more text *and*
    genuinely new 3-character sequences.  Paraphrases and lossy summaries are
    rejected; multi-card merges use the separate all-targets-present fence.
    """

    old_text = _normalized_semantic_text(old_card)
    new_text = _normalized_semantic_text(result)
    if not old_text or not new_text:
        return False
    min_growth = max(24, math.ceil(len(old_text) * 0.15))
    if len(new_text) < len(old_text) + min_growth:
        return False
    old_grams = {old_text[i : i + 3] for i in range(max(0, len(old_text) - 2))}
    new_grams = {new_text[i : i + 3] for i in range(max(0, len(new_text) - 2))}
    min_novel = max(6, math.ceil(len(old_grams) * 0.10))
    return len(new_grams - old_grams) >= min_novel


def _semantic_trigrams(card: dict) -> set[str]:
    text = _normalized_semantic_text(card)
    return {text[i : i + 3] for i in range(max(0, len(text) - 2))}


def _cards_are_merge_similar(left: dict, right: dict) -> bool:
    """Conservative pairwise duplicate/event similarity for destructive merge.

    The overlap coefficient uses the shorter card as the denominator so an
    expanded duplicate can still match its concise source.  A minimum of three
    shared trigrams prevents a generic short prefix (person/topic name) from
    making two otherwise unrelated cards look similar.
    """

    left_text = _normalized_semantic_text(left)
    right_text = _normalized_semantic_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_grams = _semantic_trigrams(left)
    right_grams = _semantic_trigrams(right)
    denominator = min(len(left_grams), len(right_grams))
    if denominator <= 0:
        return False
    shared = len(left_grams & right_grams)
    return (
        shared >= DREAM_MERGE_MIN_SHARED_TRIGRAMS
        and shared / denominator >= DREAM_MERGE_MIN_TRIGRAM_OVERLAP
    )


def _all_merge_targets_similar(cards: list[dict]) -> bool:
    return all(
        _cards_are_merge_similar(cards[left], cards[right])
        for left in range(len(cards))
        for right in range(left + 1, len(cards))
    )


class ParseRetry(NamedTuple):
    """「解析/语义结果不合格 → 原样打回去重问一次」的注入点。

    判据与纠错文案属于 memory/ 层（依赖方向不允许本模块 import 它），
    所以由 worker 组装三个纯回调传进来：
      should_retry(err)          -> 这个 reason 值不值得重问；
      build_prompt(prompt, err)  -> 第二次的 prompt（含「哪个字段没填」）；
      parse(reply)               -> 第二次的解析（通常放宽成「只丢脏行」）。

    Capture 可选再注入 ``semantic_reasons`` / ``build_semantic_prompt``：
    格式打回和语义打回**共享一次** provider 预算。若格式重问后
    仍少 target_id，直接 fail closed，不再发第三问。
    """

    should_retry: Callable[[str], bool]
    build_prompt: Callable[[str, str], str]
    parse: Callable[[str], tuple]
    semantic_reasons: Callable[[Any], list[str]] | None = None
    build_semantic_prompt: Callable[[str, list[str]], str] | None = None


_PROVIDER_FAILURE_CODES = frozenset(
    {
        "auth_invalid",
        "content_filtered",
        "model_not_found",
        "provider_config",
        "provider_incompatible",
        "quota_insufficient",
        "rate_limited",
        "unknown",
        "upstream_unavailable",
    }
)


def provider_failure_code_from_reason(reason: str) -> str | None:
    """Extract the allowlisted class from an ``extract`` provider reason."""
    prefix = "provider_call_failed:"
    raw = str(reason or "").strip()
    if not raw.startswith(prefix):
        return None
    code = raw[len(prefix) :]
    return code if code in _PROVIDER_FAILURE_CODES else "unknown"


def _provider_failure_code(exc: BaseException) -> str:
    """Return a content-free provider failure class; never inspect messages."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        trace = provider_client.runtime_provider_attempt_trace(exc) or {}
        attempts = trace.get("attempts") if isinstance(trace, dict) else []
        for attempt in reversed(attempts if isinstance(attempts, list) else []):
            candidate = attempt.get("status") if isinstance(attempt, dict) else None
            if isinstance(candidate, int):
                status = candidate
                break
    if status == 402:
        return "quota_insufficient"
    if status in {401, 403}:
        return "auth_invalid"
    if status == 404:
        return "model_not_found"
    if status == 429:
        return "rate_limited"
    if isinstance(status, int) and status >= 500:
        return "upstream_unavailable"

    reliable_class = str(getattr(exc, "feedling_error_class", "") or "")
    if reliable_class == "transient_exhausted":
        return "upstream_unavailable"
    if reliable_class == "provider_config":
        return "provider_config"
    coarse = provider_client.classify_provider_error(exc)
    if coarse == "transient":
        return "upstream_unavailable"
    if coarse == "provider_config":
        return "provider_config"
    return "unknown"


async def extract(
    *,
    provider_config: Any,
    prompt: str,
    parse: Callable[[str], tuple],
    max_tokens: int = _MAX_TOKENS,
    progress_cb: Callable[[str, int], None] | None = None,
    usage_out: Callable[[dict | None], None] | None = None,
    trajectory_out: Callable[[str, dict], Awaitable[None]] | None = None,
    parse_retry: ParseRetry | None = None,
) -> tuple[Any, str | None]:
    """跑一次 BYOK 抽取调用并解析。**永不抛**——失败一律返回 (None, reason)。

    `parse` 是 memory/*_prompt_v1 里的纯解析函数。它的返回是 (value, err) 或
    (value, questions, err)；我们只取首项与末项（末项恒为 err）。

    给了 `parse_retry` 时，内容闸判为「模型把输出示例的占位符抄了回来」的回复会被
    打回、带着「哪个字段没填」重问一次（**最多一次**，第二次的结果直接采信）。
    provider 报错 / 空回复 / JSON 根本没出来不走这条路——重问同一段 prompt 也没用，
    它们各有自己的重试与退避。
    """

    async def _call(attempt_prompt: str) -> tuple[str | None, str | None]:
        """跑一次 provider，返回 (reply, error_reason)。"""
        messages = [{"role": "user", "content": attempt_prompt}]
        if trajectory_out is not None:
            await trajectory_out(
                "provider_request", {"messages": messages, "tools": None}
            )
        try:
            result = await provider_client.reliable_chat_completion_async(
                provider_config,
                messages,
                max_tokens=max_tokens,
                temperature=_TEMPERATURE,
                timeout=_TIMEOUT_SEC,
                progress_cb=progress_cb,
            )
        except Exception as e:  # noqa: BLE001 — 背景 job：归一成 reason，绝不抛
            error_code = _provider_failure_code(e)
            if trajectory_out is not None:
                await trajectory_out(
                    "provider_error",
                    {
                        "error_class": error_code,
                        "provider_attempt_trace": (
                            provider_client.runtime_provider_attempt_trace(e)
                        ),
                    },
                )
            if usage_out is not None:
                usage_out(None)
            return None, f"provider_call_failed:{error_code}"
        if usage_out is not None:
            usage_out(result.get("usage") if isinstance(result, dict) else None)
        if trajectory_out is not None:
            await trajectory_out("provider_response", {"response": result})
        reply = str((result or {}).get("reply") or "").strip()
        if not reply:
            return None, "empty_reply"
        return reply, None

    reply, call_error = await _call(prompt)
    if call_error is not None:
        return None, call_error
    parsed = parse(reply)
    value, err = parsed[0], parsed[-1]
    retried_once = False
    if err:
        if parse_retry is None or not parse_retry.should_retry(str(err)):
            return None, str(err)
        if trajectory_out is not None:
            await trajectory_out("parse_bounced", {"reason": str(err)})
        retry_reply, retry_call_error = await _call(
            parse_retry.build_prompt(prompt, str(err))
        )
        if retry_call_error is not None:
            # 重问这一跳自己挂了：如实报重问的失败原因，别把它伪装成原来的格式问题。
            return None, retry_call_error
        retried = parse_retry.parse(retry_reply)
        value, retry_err = retried[0], retried[-1]
        if retry_err:
            return None, str(retry_err)
        retried_once = True

    semantic_reasons = (
        parse_retry.semantic_reasons(value)
        if parse_retry is not None and parse_retry.semantic_reasons is not None
        else []
    )
    if not semantic_reasons:
        return value, None
    if retried_once or parse_retry is None or parse_retry.build_semantic_prompt is None:
        return None, "semantic_validation_failed_after_retry"

    if trajectory_out is not None:
        await trajectory_out(
            "semantic_bounced", {"reason_count": len(semantic_reasons)}
        )
    retry_reply, retry_call_error = await _call(
        parse_retry.build_semantic_prompt(prompt, semantic_reasons)
    )
    if retry_call_error is not None:
        return None, retry_call_error
    retried = parse_retry.parse(retry_reply)
    retry_value, retry_err = retried[0], retried[-1]
    if retry_err:
        return None, str(retry_err)
    if parse_retry.semantic_reasons(retry_value):
        return None, "semantic_validation_failed_after_retry"
    return retry_value, None


def _inner_from_card(card: dict) -> dict:
    return {
        "summary": str(card.get("summary") or "").strip(),
        "content": str(card.get("content") or "").strip(),
        "bucket": str(card.get("bucket") or "").strip(),
        "threads": list(card.get("threads") or []),
    }


def _memory_envelope_from_card(
    card: dict,
    *,
    occurred_at: str,
    source: str,
    build_envelope: Callable[[dict], dict],
    default_type: str,
) -> dict:
    """Seal one card body and attach the plaintext metadata the real memory
    action validator requires.

    The crypto callback only seals the inner body. ``type``/``occurred_at`` and
    ranking metadata deliberately remain outside that ciphertext so the Garden
    can validate/order cards without decrypting them. The resident runtime's
    ``_capture_build_envelope`` follows the same contract.
    """
    when = str(occurred_at or "").strip()
    if not when:
        raise ValueError("memory_occurred_at_required")
    envelope = dict(build_envelope(_inner_from_card(card)) or {})
    envelope.update(
        {
            "type": str(card.get("type") or default_type).strip().lower()
            or default_type,
            "occurred_at": when,
            "importance": float(card.get("importance") or 0),
            "pulse": float(card.get("pulse") or 0),
            "anchor_memory_ids": [],
            "source": source,
            "last_referenced_at": when,
        }
    )
    return envelope


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
        # Never rewrite an invalid merge/supersede into an add.  Missing-target
        # cards are discarded so valid cards in the same batch can still land.
        if action in {"merge", "supersede"} and not target_id:
            continue
        base = {
            "envelope": _memory_envelope_from_card(
                card,
                occurred_at=occurred_at,
                source="memory_capture",
                build_envelope=build_envelope,
                default_type="event",
            ),
            "reason": reason,
            "capture_mode": capture_mode,
            "source_chat_message_ids": list(source_ids),
        }
        if action == "add":
            actions.append({"type": "memory.add", **base})
            added += 1
            continue
        if action in {"merge", "supersede"} and target_id:
            actions.append(
                {"type": "memory.supersede", "supersedes": target_id, **base}
            )
            superseded += 1
    rejected_without_target = sum(
        1
        for card in cards
        if str(card.get("action") or "").strip().lower() in {"merge", "supersede"}
        and not str(card.get("target_id") or "").strip()
    )
    if cards and not actions and rejected_without_target != len(cards):
        # 模型给了卡但一张都没映射成 action —— 说明它返回了我们不认识的 action 名。
        # 静默写零条会把这件事藏起来，所以硬失败（与 resident 同口径）。
        raise ValueError("capture_no_memory_actions")
    return actions, added, superseded


def cards_to_actions(cards, *, occurred_at, source_ids, build_envelope):
    return _to_actions(
        cards,
        occurred_at=occurred_at,
        source_ids=source_ids,
        build_envelope=build_envelope,
        capture_mode="memory_capture",
        reason="Memory captured from a completed chat window.",
    )


def consolidations_to_actions(
    consolidations,
    *,
    occurred_at,
    source_ids,
    build_envelope,
    existing_cards: list[dict] | None = None,
    now_epoch: float | None = None,
):
    """Map Dream's native ``op/card_ids/result`` shape to multi-card
    ``memory.supersede`` actions.

    Dream never emits Capture's ``action/target_id`` shape. Keeping this mapper
    separate prevents a valid consolidation from degrading to a silent no-op or
    ``capture_no_memory_actions``.
    """
    actions: list[dict] = []
    superseded = 0
    guarded = existing_cards is not None
    by_id = {
        str(card.get("id") or "").strip(): card
        for card in (existing_cards or [])
        if isinstance(card, dict) and str(card.get("id") or "").strip()
    }
    effective_now = (
        float(now_epoch)
        if now_epoch is not None
        else (_iso_epoch(occurred_at) or datetime.now(timezone.utc).timestamp())
    )
    used_ids: set[str] = set()
    policy_rejections = 0
    structurally_valid = 0
    for consolidation in consolidations or []:
        if not isinstance(consolidation, dict):
            continue
        op = str(consolidation.get("op") or "").strip().lower()
        raw_ids = consolidation.get("card_ids")
        card_ids = list(
            dict.fromkeys(
                str(memory_id or "").strip()
                for memory_id in (raw_ids if isinstance(raw_ids, list) else [])
                if str(memory_id or "").strip()
            )
        )
        result = (
            consolidation.get("result")
            if isinstance(consolidation.get("result"), dict)
            else {}
        )
        if op not in {"merge", "thicken", "supersede"} or not card_ids or not result:
            continue
        structurally_valid += 1
        if (
            len(card_ids) > DREAM_MAX_SUPERSEDED_CARDS
            or superseded + len(card_ids) > DREAM_MAX_SUPERSEDED_CARDS
        ):
            policy_rejections += 1
            continue
        if guarded:
            old_cards = [by_id.get(memory_id) for memory_id in card_ids]
            if (
                any(card is None for card in old_cards)
                or any(memory_id in used_ids for memory_id in card_ids)
                or any(
                    not dream_card_is_eligible(card, now_epoch=effective_now)
                    for card in old_cards
                    if card is not None
                )
                or (
                    len(card_ids) == 1
                    and not _has_substantive_increment(old_cards[0], result)
                )
                or (
                    len(card_ids) > 1
                    and not _all_merge_targets_similar(old_cards)
                )
            ):
                policy_rejections += 1
                continue
        card = {"type": "fact", **result}
        actions.append(
            {
                "type": "memory.supersede",
                "supersedes": card_ids,
                "envelope": _memory_envelope_from_card(
                    card,
                    occurred_at=occurred_at,
                    source="memory_dream",
                    build_envelope=build_envelope,
                    default_type="fact",
                ),
                "reason": f"Memory dream {op} consolidation.",
                "capture_mode": "memory_dream",
                "dream_op": op,
                "dream_card_ids": card_ids,
                "source_chat_message_ids": list(source_ids),
            }
        )
        superseded += len(card_ids)
        used_ids.update(card_ids)
        if len(actions) >= DREAM_MAX_CONSOLIDATIONS:
            break
    if consolidations and not actions and not (
        structurally_valid > 0 and policy_rejections == structurally_valid
    ):
        raise ValueError("dream_no_memory_actions")
    return actions, 0, superseded
