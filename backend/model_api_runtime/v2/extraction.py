"""V2 记忆抽取（capture / dream）的**纯**核心：BYOK LLM 调用 + 解析 + 卡片→memory action。

依赖方向：只 import provider_client（底层）和 stdlib/typing。**绝不**依赖任何托管运行时、
数据库/持久化层或记忆落库模块 —— prompt 构造、解析函数、信封构造与落库都由调用方（worker）
经参数/注入回调提供，这样本模块可以保持零 I/O、可单测（见源码顶层的依赖方向 grep 测试）。

`cards_to_actions` / `consolidations_to_actions` 是从 `tools/chat_resident_consumer.py`
的 `_capture_actions_from_cards` 移植过来的纯映射逻辑（spec §3.3）。resident 保留它自己的
副本直到退役——刻意的重复，换 kill-resident 之前的零风险。
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Awaitable, Callable, NamedTuple

import provider_client

_MAX_OUTPUT_TOKEN_SETTINGS = {
    "capture": ("FEEDLING_V2_CAPTURE_MAX_OUTPUT_TOKENS", 1500),
    "dream": ("FEEDLING_V2_DREAM_MAX_OUTPUT_TOKENS", 4000),
}


def _max_output_tokens_from_env(lane: str) -> int:
    """Resolve one extraction lane's positive output budget."""
    try:
        env_name, default = _MAX_OUTPUT_TOKEN_SETTINGS[lane]
    except KeyError as exc:
        raise ValueError(f"unsupported extraction lane: {lane}") from exc
    raw = os.environ.get(env_name, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{env_name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{env_name} must be a positive integer")
    return value


CAPTURE_MAX_OUTPUT_TOKENS = _max_output_tokens_from_env("capture")
DREAM_MAX_OUTPUT_TOKENS = _max_output_tokens_from_env("dream")


def max_output_tokens_for_lane(lane: str) -> int:
    """Return the startup-resolved budget without coupling capture to Dream."""
    if lane == "capture":
        return CAPTURE_MAX_OUTPUT_TOKENS
    if lane == "dream":
        return DREAM_MAX_OUTPUT_TOKENS
    raise ValueError(f"unsupported extraction lane: {lane}")


_TEMPERATURE = 0.3
_TIMEOUT_SEC = 90.0

class ParseRetry(NamedTuple):
    """「截断/解析/语义结果不合格 → 原样打回去重问一次」的注入点。

    判据与纠错文案属于 memory/ 层（依赖方向不允许本模块 import 它），
    所以由 worker 组装三个纯回调传进来：
      should_retry(err)          -> 这个 reason 值不值得重问；
      build_prompt(prompt, err)  -> 第二次的 prompt（含「哪个字段没填」）；
      parse(reply)               -> 第二次的解析（通常放宽成「只丢脏行」）。

    Capture 可选再注入 ``semantic_reasons`` / ``build_semantic_prompt``；
    capture / Dream 都可注入 ``build_truncation_prompt``。截断、格式打回和
    语义打回**共享一次** provider 预算，任何第二问失败都直接 fail closed，
    不再发第三问。
    """

    should_retry: Callable[[str], bool]
    build_prompt: Callable[[str, str], str]
    parse: Callable[[str], tuple]
    semantic_reasons: Callable[[Any], list[str]] | None = None
    build_semantic_prompt: Callable[[str, list[str]], str] | None = None
    build_truncation_prompt: Callable[[str], str] | None = None


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
    max_tokens: int = CAPTURE_MAX_OUTPUT_TOKENS,
    progress_cb: Callable[[str, int], None] | None = None,
    usage_out: Callable[[dict | None], None] | None = None,
    trajectory_out: Callable[[str, dict], Awaitable[None]] | None = None,
    failure_detail_out: Callable[[dict], None] | None = None,
    parse_retry: ParseRetry | None = None,
) -> tuple[Any, str | None]:
    """跑一次 BYOK 抽取调用并解析。**永不抛**——失败一律返回 (None, reason)。

    `parse` 是 memory/*_prompt_v1 里的纯解析函数。它的返回是 (value, err) 或
    (value, questions, err)；我们只取首项与末项（末项恒为 err）。

    给了 `parse_retry` 时，截断、内容闸或语义闸可带原因重问一次；三条路径共享
    **最多一次**的 provider 预算。provider 报错 / 空回复不走这条路，它们各有
    自己的重试与退避。
    """

    async def _call(
        attempt_prompt: str,
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """跑一次 provider，返回 reply、error 与 content-free 响应形状。"""
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
            return None, f"provider_call_failed:{error_code}", {}
        if usage_out is not None:
            usage_out(result.get("usage") if isinstance(result, dict) else None)
        if trajectory_out is not None:
            await trajectory_out("provider_response", {"response": result})
        raw_stop_reason = str((result or {}).get("stop_reason") or "").strip().lower()
        usage = (result or {}).get("usage")
        raw_completion_tokens = (
            usage.get("completion_tokens") if isinstance(usage, dict) else None
        )
        response_shape = {
            "stop_reason": (
                "length"
                if raw_stop_reason == "length"
                else ("other" if raw_stop_reason else "")
            ),
            "completion_tokens": (
                max(0, int(raw_completion_tokens))
                if isinstance(raw_completion_tokens, (int, float))
                and not isinstance(raw_completion_tokens, bool)
                else None
            ),
            "max_tokens": max_tokens,
        }
        reply = str((result or {}).get("reply") or "").strip()
        if not reply:
            return None, "empty_reply", response_shape
        return reply, None, response_shape

    async def _report_truncated(
        response_shape: dict[str, Any], *, attempt: int
    ) -> bool:
        if response_shape.get("stop_reason") != "length":
            return False
        if trajectory_out is not None:
            await trajectory_out(
                "extraction_output_truncated",
                {**response_shape, "attempt": attempt},
            )
        return True

    def _record_truncation_failure(response_shape: dict[str, Any]) -> None:
        if failure_detail_out is not None:
            failure_detail_out(dict(response_shape))

    retried_once = False
    reply, call_error, response_shape = await _call(prompt)
    if await _report_truncated(response_shape, attempt=1):
        if parse_retry is None or parse_retry.build_truncation_prompt is None:
            _record_truncation_failure(response_shape)
            return None, "output_truncated"
        if trajectory_out is not None:
            await trajectory_out(
                "extraction_output_truncation_retry",
                {
                    "attempt": 2,
                    "strategy": "concise_prompt",
                    "max_tokens": max_tokens,
                },
            )
        reply, call_error, response_shape = await _call(
            parse_retry.build_truncation_prompt(prompt)
        )
        retried_once = True
        if await _report_truncated(response_shape, attempt=2):
            _record_truncation_failure(response_shape)
            return None, "output_truncated"
    if call_error is not None:
        return None, call_error
    parsed = parse(reply)
    value, err = parsed[0], parsed[-1]
    if err:
        if (
            retried_once
            or parse_retry is None
            or not parse_retry.should_retry(str(err))
        ):
            return None, str(err)
        if trajectory_out is not None:
            await trajectory_out("parse_bounced", {"reason": str(err)})
        retry_reply, retry_call_error, _retry_response_shape = await _call(
            parse_retry.build_prompt(prompt, str(err))
        )
        if await _report_truncated(_retry_response_shape, attempt=2):
            _record_truncation_failure(_retry_response_shape)
            return None, "output_truncated"
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
    retry_reply, retry_call_error, _retry_response_shape = await _call(
        parse_retry.build_semantic_prompt(prompt, semantic_reasons)
    )
    if await _report_truncated(_retry_response_shape, attempt=2):
        _record_truncation_failure(_retry_response_shape)
        return None, "output_truncated"
    if retry_call_error is not None:
        return None, retry_call_error
    retried = parse_retry.parse(retry_reply)
    retry_value, retry_err = retried[0], retried[-1]
    if retry_err:
        return None, str(retry_err)
    if parse_retry.semantic_reasons(retry_value):
        return None, "semantic_validation_failed_after_retry"
    return retry_value, None


def _inner_from_card(card: dict, *, voice_call_id: str = "") -> dict:
    inner = {
        "summary": str(card.get("summary") or "").strip(),
        "content": str(card.get("content") or "").strip(),
        "bucket": str(card.get("bucket") or "").strip(),
        "threads": list(card.get("threads") or []),
    }
    # 溯源提示:这张卡来自一个含该通电话的 capture 窗口,agent 可据此调
    # voice_transcript_read 回看原文。只在窗口"恰好含一通电话"时打——多通电话
    # 的窗口无法判断某张卡属于哪一通,给了就是假精度。放在加密正文里(不放
    # envelope 明文):服务端因此看不见"哪张卡来自哪通电话"。
    if voice_call_id:
        inner["voice_call_id"] = str(voice_call_id)[:96]
    return inner


def _memory_envelope_from_card(
    card: dict,
    *,
    occurred_at: str,
    source: str,
    build_envelope: Callable[[dict], dict],
    default_type: str,
    voice_call_id: str = "",
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
    envelope = dict(build_envelope(_inner_from_card(card, voice_call_id=voice_call_id)) or {})
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
    voice_call_id: str = "",
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
                voice_call_id=voice_call_id,
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


def cards_to_actions(cards, *, occurred_at, source_ids, build_envelope,
                     voice_call_id: str = ""):
    return _to_actions(
        cards,
        occurred_at=occurred_at,
        source_ids=source_ids,
        build_envelope=build_envelope,
        voice_call_id=voice_call_id,
        capture_mode="memory_capture",
        reason="Memory captured from a completed chat window.",
    )


def _latest_source_occurred_at(
    cards: list[dict],
    *,
    on_degraded=None,
) -> str:
    """源卡里**已知的最晚**事件时间;全都没有时退到 created_at 的日期。

    Dream 重写旧记忆,所以 job 时间和最新聊天时间都不能代表事件何时发生 ——
    这一点没变,`consolidations_to_actions` 里「拿不到源卡」那条仍然 fail-closed。

    ⚠️ 2026-08-17 由 fail-closed 改为降级(Seven 定)。原来只要**一张**源卡缺
    `occurred_at` 就整轮抛错,而实测四个受影响用户的花园里缺失率 12%~64% ——
    dream 每次挑几张源卡,碰上一张就废,等于这些用户的 dream **永久阻塞且无感**。
    代价对比:
      · 降级 → 合并卡的时间可能偏 → 花园里排序位置偏(显示问题)
      · 拦死 → 那个用户的 dream 整个不跑(功能没了)
    排序偏远轻于功能没了。

    取**已知里最晚的**而不是随便一张:这个函数的语义就是「最晚」,
    随手取一张可能取到旧的,而明明有更新的已知值。

    `on_degraded(known, missing, fallback_used)` 用于留痕。**必须留痕**:
    那批缺 occurred_at 的卡 created_at 集中在 08-10~08-14,**是最近两周写的**,
    说明产生脏数据的写入路径**可能还开着**。降级如果不留痕,就把源头永久盖住了。
    """
    ranked: list[tuple[datetime, str]] = []
    missing = 0
    for card in cards:
        raw = str(card.get("occurred_at") or "").strip()
        if not raw:
            missing += 1
            continue
        candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            missing += 1
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        ranked.append((parsed.astimezone(timezone.utc), raw))
    if ranked:
        if missing and on_degraded is not None:
            on_degraded(len(ranked), missing, False)
        return max(ranked, key=lambda item: item[0])[1]

    # 一张可用的都没有 → 退到 created_at 的**日期**(不是完整时刻:
    # created_at 是写入时间,只有日期这一档是对事件时间的诚实近似)。
    # ⚠️ 这里同样取**最晚**,不是第一张 —— 我第一版写成了「遍历到第一张能解析的
    # 就返回」,结果输入顺序一变结果就变(codex2 实测正序 08-10 / 反序 08-11)。
    # 我自己的 docstring 写着「取已知里最晚的」,实现却没照做。
    created_ranked: list[datetime] = []
    for card in cards:
        created = str(card.get("created_at") or "").strip()
        if not created:
            continue
        candidate = created[:-1] + "+00:00" if created.endswith("Z") else created
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        created_ranked.append(parsed.astimezone(timezone.utc))
    if created_ranked:
        if on_degraded is not None:
            on_degraded(0, missing, True)
        return max(created_ranked).date().isoformat()

    # created_at 也全都没有/解析不了 —— 实测 260 张卡里 0 例,但仍然 fail-closed:
    # 到这一步已经没有任何可信的时间来源,编一个不如失败。
    raise ValueError("dream_source_occurred_at_unavailable")


def consolidations_to_actions(
    consolidations,
    *,
    occurred_at,
    source_ids,
    build_envelope,
    existing_cards: list[dict] | None = None,
    on_source_time_degraded=None,
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
        rationale = str(consolidation.get("rationale") or "").strip()
        if not rationale:
            policy_rejections += 1
            continue
        if guarded:
            # 2026-08-05 复盘只保留结构性判据:目标卡必须真实存在、不能被两条
            # 提案重复退休。语义审查员与 15% 增量栅栏(内容质量判断)已拆除 ——
            # 出口硬闸移到 parse 层(卡id泄漏/墓碑短语)与 worker 层(爆炸半径保险丝)。
            old_cards = [by_id.get(memory_id) for memory_id in card_ids]
            if any(card is None for card in old_cards) or any(
                memory_id in used_ids for memory_id in card_ids
            ):
                policy_rejections += 1
                continue
        else:
            # Without the exact disclosed source cards there is no honest
            # event time to carry forward.  The worker's shared ``occurred_at``
            # is capture/chat time and must never become Dream's silent fallback.
            raise ValueError("dream_source_occurred_at_unavailable")
        source_occurred_at = _latest_source_occurred_at(
            old_cards, on_degraded=on_source_time_degraded
        )
        card = {"type": "fact", **result}
        actions.append(
            {
                "type": "memory.supersede",
                "supersedes": card_ids,
                "envelope": _memory_envelope_from_card(
                    card,
                    occurred_at=source_occurred_at,
                    source="memory_dream",
                    build_envelope=build_envelope,
                    default_type="fact",
                ),
                "reason": f"Memory dream {op} consolidation.",
                "capture_mode": "memory_dream",
                "dream_op": op,
                "dream_card_ids": card_ids,
                "dream_rationale": rationale,
                "source_chat_message_ids": list(source_ids),
            }
        )
        superseded += len(card_ids)
        used_ids.update(card_ids)
    if consolidations and not actions and not (
        structurally_valid > 0 and policy_rejections == structurally_valid
    ):
        raise ValueError("dream_no_memory_actions")
    return actions, 0, superseded
