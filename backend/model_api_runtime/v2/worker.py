"""V2 worker：Postgres 队列 consumer 的编排（子项目 C 收口 Task 8）。

进程入口在 serve_worker.py；本模块做「一回合一 worker」的编排：claim → coalesce →
planner → executor →（安全点 replan，预算内）→ responder → 落加密回复。turn 执行体走
注入式 TurnDeps（生产实现由 serve_worker 装配、可 import hosted/core；测试注入假实现，
不碰真 enclave/provider/LLM）。

依赖方向（CONTRIBUTING §2）：本模块只 import core.*/model_api_runtime.v2.*/capabilities.*/
provider_client —— 绝不 import `hosted` 或 `agent_runtime.spawners`。official/非官方判定
（是否用户在跑受信任模型）需要 `agent_runtime.spawners._is_official_identity`，那是
hosted-adjacent 的东西；因此它被挪到 TurnDeps.is_official（生产实现在 serve_worker.py
里包一层调用 spawners），worker.py 只拿到已解析好的 bool / 可调用对象。

两套凭证不混（spec §5）：
- provider_config（用户 BYOK）：只喂 planner（official_plan 内部）与 responder 的 LLM
  调用。resolve_provider(user_id) 在本回合只解密一次（single-flight 之外的每 job 一次），
  由 `_slot_loop` 在把 job 交给 `process_job` 之前调用、并把结果原样传入、整个回合复用。
- api_key + runtime_token（enclave-auth）：只喂 executor 的 capability 调用 + 便宜预取
  （memory_index/perception_snapshot）+ `TurnDeps.read_messages`（enclave 内解密取明文）。
  runtime_token 由 `TurnDeps.mint_enclave_token` 铸造——这只是签一个短时效令牌（HMAC，不
  是解密），可以在回合内按需多铸，不违反「resolve_provider 只解密一次」的不变量（那条
  不变量特指 BYOK provider-key 的解密，不是 enclave-auth 令牌的签发次数）。

敏感面分层（spec §5/§9）：executor 产出的 action_results（含解密后的 data）只在内存里
传给 responder；action_digest（非敏感 ok/count 粗计数）才落 runtime_state。status 事件
（processing/reading_*/writing_reply/done）经 status_stream.redact_status 脱敏，detail
只含标签 + 粗计数，绝无原文。

并发：asyncio 事件循环 + asyncio.to_thread 把同步 jobs_store/enclave 调用移出 loop。
provider 调用统一 await async facade；OpenAI-compatible chat 已是原生 async，仍需同步
bridge 的 provider 路径由 serve-worker 把 AnyIO limiter 至少扩到 turn-slot 数，避免默认
线程上限悄悄小于配置的并发。最终目标仍是所有 provider HTTP 原生 async。
ENCLAVE_SEMAPHORE 框住 turn 里所有 enclave-bound 调用（provider-key 解密 + 逐条 chat 解密
+ capability 调用），治 spec R3（enclave 单线程瓶颈，多 worker 齐打会放大 502）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import provider_client
from capabilities import registry as cap_registry
from core import envelope as core_envelope
from core import store as core_store
from core import wake_bus as core_wake_bus
from model_api_runtime.v2 import agent_loop as v2_agent_loop
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import compaction as v2_compaction
from model_api_runtime.v2 import context
from model_api_runtime.v2 import executor as v2_executor
from model_api_runtime.v2 import extraction as v2_extraction
from model_api_runtime.v2 import invalidation as v2_inval
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import planner as v2_planner
from model_api_runtime.v2 import responder as v2_responder
from model_api_runtime.v2 import status_stream
# 纯 prompt/parse 模块（无 I/O、不碰 DB/enclave）——依赖方向允许 worker 直接 import
# （extraction.py 同样只 import 这两个 + provider_client）。
from memory.capture_prompt_v1 import build_capture_prompt, parse_capture_cards
from memory.dream_prompt_v1 import build_dream_prompt, parse_dream_consolidations

log = logging.getLogger("feedling.runtime_v2.worker")


def _positive_int_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value

# —— 三个有界闸 ——（spec §6）
# 每进程并发 job 数（= 并发回合数）。线上多进程 × CVM 共抢同一张 agent_jobs → 线性扩容。
MAX_WORKERS = _positive_int_env("FEEDLING_V2_MAX_WORKERS", "4")
# 单 job 内 executor 并行读上限。
MAX_READ_ACTION_PARALLELISM = _positive_int_env("FEEDLING_V2_MAX_READ_PARALLELISM", "4")
# 跨所有 job 共享的 enclave 并发闸（provider-key 解密 + chat 解密 + capability 调用都过它）。治 R3。
ENCLAVE_CONCURRENCY = _positive_int_env("FEEDLING_V2_ENCLAVE_CONCURRENCY", "2")
ENCLAVE_SEMAPHORE = asyncio.Semaphore(ENCLAVE_CONCURRENCY)


def _reserved_lane_slots(max_workers: int, reserved: int | None = None) -> list:
    """Per-slot lane allowlist（D3 Task 5：把 Task 2 的 claim lane-reservation 接进池编排）。

    前 `reserved` 个 slot 只允许抢 {"chat","manual_wake"}（一次 heartbeat/capture 唤醒风暴
    绝不会饿死聊天回复）；其余 slot 不设限（None＝任意 lane，含 heartbeat/capture/
    maintenance）。reserved 未显式传时默认 max(1, max_workers // 2)，但始终至少保留
    一个 unrestricted slot；单-worker 部署因此不能做 lane reservation。否则 scheduled/
    maintenance/capture 等非 chat job 会在一个健康进程里永久 pending。reserved 会被夹到
    [0, max_workers - 1] 区间内，防御越界配置。"""
    n = max(1, int(max_workers))
    r = reserved if reserved is not None else max(1, n // 2)
    r = max(0, min(r, n - 1))
    return [{"chat", "manual_wake"} if i < r else None for i in range(n)]

# D1（full-conversation context）：responder 现在吃 summary+tail 而不是"仅未回复的 user
# 消息"。tail 超过 _TAIL_BUDGET 条（双角色计数）时，chat turn 顺手（best-effort，不阻塞
# 回复）入队一个 maintenance lane 的 compaction job，把最旧的一批折进摘要，只留
# _TAIL_KEEP 条最近消息逐字保留。_TAIL_HARD_CAP 是 chat turn 读 tail 时的硬上限（喂
# responder 用，不是压缩用——压缩自己读到 watermark 之后的全部，见 `_run_compaction`）。
_TAIL_BUDGET = int(os.environ.get("FEEDLING_V2_TAIL_BUDGET_MSGS", "20"))
_TAIL_KEEP = int(os.environ.get("FEEDLING_V2_TAIL_KEEP_MSGS", "10"))
_TAIL_HARD_CAP = int(os.environ.get("FEEDLING_V2_TAIL_HARD_CAP", "60"))
_COMPACTION_BATCH = int(os.environ.get("FEEDLING_V2_COMPACTION_BATCH_MSGS", "200"))

# 每回合最多注入最近 N 张图。enclave 单线程（每张图一次往返），且无 prompt caching ——
# tail 里的图片每个回合都要重发，token 成本随图片数线性上升。
_TAIL_IMAGE_LIMIT = int(os.environ.get("FEEDLING_V2_TAIL_IMAGE_LIMIT", "2"))
# 单张图 b64 上限；超限跳过注入、退化成文本标记（不引入图像缩放依赖）。
_IMAGE_MAX_B64_CHARS = int(os.environ.get("FEEDLING_V2_IMAGE_MAX_B64_CHARS", "2000000"))

# 工具循环轮数上限（spec §6）。撞上限 → 停止取工具，用手上的结果强制收口回复。
_LOOP_MAX_ROUNDS = int(os.environ.get("FEEDLING_V2_LOOP_MAX_ROUNDS", "3"))
# 一个回合内**跨两层循环**（外层消息驱动 replan × 内层模型驱动 tool loop）的 LLM 调用硬闸。
# 上界是 replan_budget(2) × _LOOP_MAX_ROUNDS(3) + 1(responder) = 7 —— 故意让 6 咬住它。
# 两层语义不同（外层"用户又说话了"、内层"我还想再查"）不能合并，但必须共用一个预算，
# 否则一个话痨用户 + 一个爱查东西的 planner 能把用户的 BYOK key 烧穿。
# 恒留 1 个名额给 responder：no-filler 铁律要求 chat lane 一定产出 model-authored 文本。
_TURN_MAX_LLM_CALLS = int(os.environ.get("FEEDLING_V2_TURN_MAX_LLM_CALLS", "6"))

# D3 Task 6 (proactive/wake lanes): the scheduler (Task 4/9) enqueues jobs in
# these three lanes when it decides the companion should reach out without the
# user having spoken first. "capture" is intentionally NOT in this set — it's
# a different capability shape (memory extraction, not a model-authored reply)
# and is scoped to a follow-up task; a capture-lane job falling through to the
# default chat path below would be wrong (no coalesced pending messages ->
# it would just complete as a no-op), so it's left alone here rather than
# silently mishandled by this task's scope.
_WAKE_LANES = frozenset({"heartbeat", "scheduled", "manual_wake", "screen_watch"})
# 记忆抽取 lane（capture=一窗对话→记忆卡，dream=现有卡片→合并）。同形：
# build prompt → BYOK 抽取 → parse → memory actions。永不写气泡、永不弹 error chip。
_EXTRACTION_LANES = frozenset({"capture", "dream"})
_WAKE_SYSTEM_PROMPT = (
    "You are the user's companion. This is a PROACTIVE moment — the user has not "
    "just spoken. Look at the conversation so far. If there is something genuine, "
    "specific, and worth saying right now — a follow-up, a thought, a check-in — say "
    "it naturally in your own voice. If there is nothing worth saying, reply with an "
    "empty message; staying silent is correct and expected."
)
_WAKE_NUDGE = "(A quiet moment has passed. Reach out only if something is genuinely worth saying right now.)"
# screen_watch lane (Task 3): a wake grounded on recent shared-screen frames rather
# than a perception snapshot. Its own system prompt sits beside _WAKE_SYSTEM_PROMPT;
# _run_wake selects it only for lane=="screen_watch". Silence is still the correct
# answer most ticks (inherits the "weak wake sleeps" empty_reply path).
_SCREEN_WATCH_SYSTEM_PROMPT = (
    "You are the user's personal companion, quietly watching the screen they are sharing. "
    "Recent frames (with captions) are provided as grounding context. "
    "Speak ONLY if you have something genuinely useful or warm to say about what changed on "
    "screen right now. If nothing is worth saying, reply with an empty message — silence is "
    "the correct answer most of the time. Never narrate that you are watching or that you "
    "looked at frames."
)
# D3 Task 7 (BYOK payment cooldown): a "provider_config" wake failure (402 out-of-credits,
# 401/403 bad key) means the user's BYOK key is dead/broke — retrying it every heartbeat
# interval is a retry storm against a key that cannot succeed until the user fixes it
# (mirrors the original resident runtime's 600s payment cooldown). We write
# `payment_cooldown_until` on the wake schedule; `jobs_store.due_heartbeat_users` already
# excludes cooled-down users (Task 1), so no further wakes fire until it lapses.
_WAKE_COOLDOWN_SEC = float(os.environ.get("FEEDLING_V2_WAKE_COOLDOWN_SEC", "600"))


class LostJobLease(RuntimeError):
    """The reaper or another lifecycle transition fenced this worker out."""


class RuntimeModeChanged(RuntimeError):
    """The user rolled back while this V2 job was queued or running."""


def _safe_failure_code(scope: str, exc: BaseException) -> str:
    """Stable plaintext error code that never embeds exception messages."""
    if isinstance(exc, v2_responder.ResponderError):
        raw = str(exc)
        if raw in {"empty_reply", "no_user_messages"}:
            kind = raw
        elif exc.kind in {"transient", "provider_config", "unknown"}:
            kind = f"provider_{exc.kind}"
        else:
            kind = "responder_error"
    else:
        kind = type(exc).__name__.lower() or "error"
    return f"{scope}:{kind}"[:120]


@dataclass
class TurnDeps:
    """turn 执行体的注入式依赖（生产实现见 serve_worker.build_production_deps）。

    respond/append_reply 不在这里：那两步现在直接调用同层的 v2.responder（本就 hosted-
    free，无需经 DI 间接一层）和本模块的 `_write_encrypted_reply`（只 import core.envelope/
    core.store，同样 hosted-free）。留在 TurnDeps 里的四样都是必须跨依赖方向注入的东西：
    enclave-bound 的解密/判定、需要 hosted.config_store 才能做的 BYOK 解析，或者需要
    hosted.config_store 才能落的终态失败标记（Task 3）。
    """
    read_messages: Callable[[str], list[dict]]           # user_id -> [{"id","ts","role","content"}]（enclave 解密明文）
    resolve_provider: Callable[[str], tuple[Any, dict]]   # user_id -> (ProviderConfig|None, meta)：BYOK，回合内只调一次
    is_official: Callable[[Any], bool]                    # provider_config -> bool；生产实现包 agent_runtime.spawners（hosted-adjacent），worker.py 自身不 import 它
    mint_enclave_token: Callable[[str], str]              # user_id -> 短时效 runtime_token（HMAC 签发，非解密，可按需多铸）
    # (user_id, message) -> None：终态失败时的第二个可见性出口（Task 3）——agent_jobs.last_error
    # 对 iOS 不可见，这个回调让 serve_worker 把同一条 message 也 patch 进
    # hosted.config_store 的 last_runtime_error（iOS 错误 chip 读的字段，见
    # hosted/setup_core.py:265）。默认 None：worker.py 自身不 import hosted，测试/其他调用方
    # 不必提供；生产装配见 serve_worker.build_production_deps。
    record_terminal_error: Callable[[str, str], None] | None = None
    # Production cursor-aware reader. Keeping this optional preserves the many
    # pure tests that inject the older one-argument reader.
    read_messages_since: Callable[[str, float], list[dict]] | None = None
    runtime_mode_enabled: Callable[[str], bool] | None = None
    # (job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms) -> None
    # （kwargs-only，见 jobs_store.record_turn_metric）：D4 load-testing 消费的每回合
    # provider token usage + 延迟。只在 responder.respond 成功返回后（chat lane）由
    # worker 调用——responder 自己不落库/不知道 job_id/lane（见 responder.respond 的
    # usage_out 出参文档）。默认 None：worker.py 自身不 import jobs_store 的落库细节
    # 之外的东西，测试/其他调用方不必提供；生产装配见 serve_worker.build_production_deps。
    record_turn_metric: Callable[..., None] | None = None
    # (user_id, after_ts, limit) -> [{"id","ts","role","content"}]：最近窗口，BOTH
    # roles，ts>after_ts，enclave 解密明文（D1：让 turn 能看见真实对话上下文，不再局限于
    # "上次回复之后的 user 消息"那一批）。默认 None：worker.py 自身不 import hosted，
    # 测试/其他调用方不必提供；生产装配见 serve_worker.build_production_deps。
    read_tail: Callable[[str, float, int], list[dict]] | None = None
    read_compaction_tail: Callable[[str, float, int], list[dict]] | None = None
    # user_id -> (summary_plaintext, watermark_ts, version)：读取该用户当前会话摘要（enclave
    # 解密明文）；从未压缩过时 ("", 0.0, 0)（D1：turn 看 摘要+尾巴 而不是全量重放）。默认
    # None：worker.py 自身不 import hosted，测试/其他调用方不必提供；生产装配见
    # serve_worker.build_production_deps。
    read_summary: Callable[[str], tuple[str, float, int]] | None = None
    # (user_id, summary, watermark_ts, expected_version) -> True if CAS landed：本地加密
    # （core_envelope，非 enclave 往返）+ CAS 写回 v2_conversation_summary（Task 2 storage）。
    # expected_version 不匹配（别的回合已推进过摘要）时返回 False，调用方按丢弃本次压缩处理，
    # 不重试、不报错——下一回合会用新版本重新压缩。默认 None：同上。
    write_summary: Callable[[str, str, float, int], bool] | None = None
    # (user_id, message_ids) -> {message_id: {"image_mime": str, "image_b64": str}}：只对
    # 指定的图片消息做 enclave 解密。**不能**并进 read_tail —— compaction 用 limit=10_000 调
    # read_tail，b64 会进摘要器 prompt，且该用户历史上每张图都会被解密一次。默认 None：
    # worker.py 自身不 import hosted/capabilities 的装配细节；生产装配见 serve_worker。
    read_images: Callable[[str, list[str]], dict[str, dict]] | None = None
    # —— capture/dream 记忆抽取 lane 的三个注入回调（Task 3）——
    # 全部默认 None：worker.py 不 import `hosted`/`memory_core`/`core.envelope`-for-memory
    # （否则违反 CONTRIBUTING §2 的依赖方向），所以记忆上下文读取、信封加密、落库都作为
    # 可调用对象由 serve_worker.build_production_deps 注入；测试注入假实现直接跑。
    # user_id -> {"ai_name","user_name","buckets","threads","identity","cards"}（均为字符串，
    # 任意一项可为 ""）：capture/dream prompt 需要的记忆上下文（enclave 解密明文）。取数失败
    # → 降级为空上下文，不失败 job（spec §3.5）。
    read_memory_context: Callable[[str], dict] | None = None
    # (user_id, actions) -> result dict：把抽取产出的 memory.add/memory.supersede action 落库
    # （走既有 /v1/memory/actions 同路径的服务端实现）。None 时跳过持久化、仍干净 mark_completed
    # （让 handler 无 DB/enclave 也可单测）。
    apply_memory_actions: Callable[[str, list[dict]], dict] | None = None
    # (user_id, inner) -> envelope：把一张卡的明文草稿封成客户端加密信封（E2E）。传给
    # extraction.cards_to_actions/consolidations_to_actions 的 build_envelope。None 时同上跳过持久化。
    build_memory_envelope: Callable[[str, dict], dict] | None = None
    # user_id -> {"applied": int, "discarded": int}（Task 6 / spec A6）：run the
    # generation-fenced effect-outbox applier (`effect_outbox.apply_pending_effects`)
    # with this turn's real dispatch sinks at end-of-turn. worker.py itself never
    # imports `model_api_runtime.v2.effect_outbox`'s dispatch-side wiring (the 7
    # sinks live in serve_worker.py, the assembly tier, since several of them
    # touch hosted-adjacent writers) — it only calls this injected callable.
    # None (the default for every pre-existing test/caller that doesn't wire it)
    # skips the step entirely: no effects have been enqueued into the outbox by
    # any producer yet (that lands in PR C), so calling it today is a no-op read
    # of an empty pending set; the field exists so the call site is already wired
    # ahead of the producer landing.
    apply_pending_effects: Callable[[str], dict] | None = None


async def _cap_data(store, action_type, *, api_key, runtime_token, params=None, enclave_sem=None) -> dict:
    """便宜预取一个 capability 的 data（无 LLM，用 enclave-auth 凭证）。失败退化为 {}——
    planner 有 index 更好、没有也能规划（rule_plan/official_plan 都容忍空 memory_index）。

    enclave-bound（spec §11 R3）：capability 调用可能触达 enclave（如 perception_snapshot
    的解密读），跟 executor._run_one 的 capability 调用一样必须过 enclave_sem，否则多 worker
    并发预取会绕开闸门直接打单线程 enclave。enclave_sem 为 None（部分单测直调）时不设闸——
    与 executor._run_one/process_job 对 enclave_sem 的处理口径一致。"""
    async def _call():
        return await asyncio.to_thread(
            cap_registry.run_capability,
            action_type, store, api_key=api_key, runtime_token=runtime_token, params=params or {})

    try:
        if enclave_sem is not None:
            async with enclave_sem:
                result = await _call()
        else:
            result = await _call()
        data = result.to_dict()
        return data.get("data") or {} if data.get("ok") else {}
    except Exception:  # noqa: BLE001 — 预取失败不该拖垮整个回合
        return {}


async def _coalesce_inputs(
    deps: TurnDeps, user_id: str, since_ts: float, *, enclave_sem=None
) -> tuple[list[dict], float]:
    """经注入的 `TurnDeps.read_messages` 取 enclave 内解密的**明文**近期消息，再按 §7.1 合并。

    `read_messages` 逐条在 enclave 内解密（worker 不 shell out、不碰 K_user）——enclave-bound
    （spec §11 R3），过 enclave_sem 才能跟 provider-key 解密/executor capability 调用共享同一
    闸门，否则 N 个并发 worker 会绕开闸门直接打单线程 enclave。enclave_sem 为 None（部分单测
    直调）时不设闸。返回 (coalesced, cursor)。cursor==0.0 表示本次没有新消息被折入（调用方
    不应据此回退 last_replied_ts——见 process_job 里的单调前进处理）。"""
    async def _read():
        if deps.read_messages_since is not None:
            return await asyncio.to_thread(deps.read_messages_since, user_id, since_ts)
        return await asyncio.to_thread(deps.read_messages, user_id)

    if enclave_sem is not None:
        async with enclave_sem:
            messages = await _read()
    else:
        messages = await _read()
    return v2_coalesce.coalesce_pending(messages, since_ts=since_ts)


def _write_encrypted_reply(store, text: str) -> dict | None:
    """把 model-authored 回复封 shared 信封落**加密** chat_messages，并唤醒本地 chat waiter。

    照既有 model_api 线的写法：服务器只持有密文（E2E）。信封构建失败（如用户从未
    onboard 过加密身份）返回 None——调用方视为「无法投递」，不当作 no-filler 违规
    （已经拿到了 model-authored 文本，只是没法安全落库；上层记 last_error 更诚实）。"""
    env, err = core_envelope._build_shared_envelope_for_store(store, text.encode("utf-8"))
    if env is None:
        return None
    # Strict persistence is required for a terminal V2 reply.  The legacy
    # append API swallows DB failures after mutating its in-process cache, which
    # could otherwise let this job complete with no durable reply.
    row = store.append_chat("openclaw", "model_api", env, strict=True)
    store.notify_chat_waiters()
    return row


def _emit_status(user_id, job_id, kind: str) -> None:
    """落一条顶层阶段性 status 事件（processing/writing_reply/done），复用
    status_stream.redact_status 拿到统一的标签——不在本模块里重复维护中文文案。"""
    ev = status_stream.redact_status(kind)
    jobs_store.append_status_event(user_id, ev["kind"], job_id=job_id, label=ev["label"], detail=ev["detail"])


def _surface_terminal_error(deps: TurnDeps, user_id: str, job_id, message: str) -> None:
    """终态失败的第二个可见性出口（Task 3）：mark_failed 只写 agent_jobs.last_error，
    对 iOS 不可见。这里补两件事——(1) 一条 "error"-kind status 事件（iOS 的 status
    poll 表面）；(2) 若装配了 deps.record_terminal_error，再调用它，让 serve_worker
    把同一条 message 也 patch 进 hosted.config_store 的 last_runtime_error（iOS 错误
    chip 真正读的字段）。两步各自单独 try/except：可见性本身出故障绝不能掩盖/顶替
    原始失败，也绝不能让这条失败-surfacing 路径反过来把 turn 的失败处理循环打崩。"""
    try:
        _emit_status(user_id, job_id, "error")
    except Exception as e:  # noqa: BLE001 — 可见性故障绝不能掩盖原始失败或拖垮循环
        log.warning("[v2.worker] job %s failed to emit error status: %s", job_id, e)
    if deps.record_terminal_error is not None:
        try:
            deps.record_terminal_error(user_id, message)
        except Exception as e:  # noqa: BLE001 — 同上
            log.warning("[v2.worker] job %s record_terminal_error callback failed: %s", job_id, e)


async def _run_compaction(
    job_id, user_id: str, deps: TurnDeps, provider_config: Any,
    enclave_sem: "asyncio.Semaphore", claimed_by: str | None = None,
) -> str:
    """maintenance-lane 压缩：把超预算 tail 的最旧一批折进加密 summary（append-and-merge，
    CAS 原子写，见 `model_api_runtime.v2.compaction.compact` + `jobs_store.upsert_summary_row_cas`）。
    用户 BYOK key（provider_config 已由 `_run_turn` 单次解密并传入，压缩本身不再多解密一次）。

    自成一体、自己的 try/except：这是后台维护 job，绝不写聊天气泡、失败绝不给用户弹
    error chip（不调 `_surface_terminal_error`，不落 "error"-kind status 事件）——只是
    静默 `mark_failed`，跟 chat turn 的用户可见失败路径彻底分开。
    """
    try:
        async with enclave_sem:
            summary, watermark, version = await asyncio.to_thread(deps.read_summary, user_id)
            reader = deps.read_compaction_tail or deps.read_tail
            tail = await asyncio.to_thread(
                reader, user_id, watermark, _COMPACTION_BATCH + _TAIL_KEEP)
        if len(tail) <= _TAIL_KEEP:
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            return "completed"
        old = tail[: min(_COMPACTION_BATCH, len(tail) - _TAIL_KEEP)]
        new_watermark = old[-1]["ts"]
        new_summary = await v2_compaction.compact(
            provider_config=provider_config, current_summary=summary, old_messages=old,
            llm=provider_client.reliable_chat_completion_async)
        if new_summary.strip() == summary.strip():  # 空/no-op 折叠 → 不推进 watermark/version
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            return "completed"
        if claimed_by and not await asyncio.to_thread(
            jobs_store.renew_job_lease, job_id, claimed_by,
            ttl_sec=jobs_store.RUNNING_TTL_SEC,
        ):
            raise LostJobLease("compaction lease lost before summary write")
        if deps.runtime_mode_enabled is not None and not await asyncio.to_thread(
            deps.runtime_mode_enabled, user_id
        ):
            raise RuntimeModeChanged("user rolled back before summary write")
        # write_summary 是本地加密（core_envelope，非 enclave 往返）+ CAS 写库，不占用
        # 稀缺的 enclave_sem——只有解密才走 enclave HTTP（见 _read_summary/_read_tail）。
        ok = await asyncio.to_thread(deps.write_summary, user_id, new_summary, new_watermark, version)
        if ok:
            completed = await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            if completed and len(tail) >= _COMPACTION_BATCH + _TAIL_KEEP:
                await asyncio.to_thread(
                    jobs_store.enqueue_job, user_id, "maintenance", reason="compaction_catchup")
                await asyncio.to_thread(core_wake_bus.notify, "v2_jobs", user_id)
            return "completed"
        # CAS 没落地：别的写手已经推进过版本，本次压缩视为丢弃，不重试、不报错。
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, "summary_cas_lost", claimed_by=claimed_by)
        return "failed"
    except Exception as e:  # noqa: BLE001 — 后台 job：静默 mark_failed，绝不弹用户可见 error/写气泡
        code = _safe_failure_code("compaction_failed", e)
        log.warning("[v2.worker] compaction job %s failed code=%s", job_id, code)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, code,
            claimed_by=claimed_by)
        return "failed"


async def _run_wake(
    job_id, user_id: str, lane: str, deps: TurnDeps, provider_config: Any,
    enclave_sem: "asyncio.Semaphore", claimed_by: str,
) -> str:
    """wake-lane（heartbeat/scheduled/manual_wake）turn：让伴侣主动开口，而不是回答用户
    刚发的消息（用户根本没发消息——这就是唤醒的定义）。同 `_run_compaction` 一样自成
    一体、自己的 try/except：这是后台/主动发起的 job，provider 解析失败或任何未预期异常
    都静默 `mark_failed`，绝不 `_surface_terminal_error`、绝不写占位气泡。

    跟 `_run_compaction` 的关键区别：压缩从不产出用户可见内容，wake 恰恰相反——目的就是
    让模型主动写一条聊天气泡。区分两种"没写成"：
    - "weak wake sleeps"（弱唤醒睡回去）：`v2_responder.respond` 抛
      `ResponderError("empty_reply")`（模型选择保持沉默）或
      `ResponderError("no_user_messages")`（tail+nudge 退化到没有非 system 轮次，理论上
      不会发生——nudge 本身就是一条 user 消息——但同样按"无话可说"处理，保险）。这两种
      都不是失败：`mark_completed`，零气泡，不弹 error。
    - 真 provider 错误（其他任何 `ResponderError`，如 402/enclave 瞬时故障）：真失败，
      `mark_failed`，同样静默（不弹用户可见 error chip——背景 job，同 maintenance 的
      隔离口径）。

    prompt 组装：读 summary+tail（同 chat 路径的 D1 读法），追加一条固定的
    `_WAKE_NUDGE`（as a user-role turn，让 `context.build_turn_messages`/
    `responder.respond` 的"至少一条非 system 消息"不变量恒真，即使 tail 本身是空的）。
    `system_prompt=_WAKE_SYSTEM_PROMPT` 覆盖聊天默认提示，明确告诉模型这是主动时刻。
    """
    try:
        store = core_store.get_store(user_id)

        async def _fence_wake_effect(effect: str) -> None:
            if not await asyncio.to_thread(
                jobs_store.renew_job_lease,
                job_id,
                claimed_by,
                ttl_sec=jobs_store.RUNNING_TTL_SEC,
            ):
                raise LostJobLease(f"wake lease lost before {effect}")
            if deps.runtime_mode_enabled is not None and not await asyncio.to_thread(
                deps.runtime_mode_enabled, user_id
            ):
                raise RuntimeModeChanged(f"user rolled back before {effect}")

        async with enclave_sem:
            if deps.read_summary is not None:
                summary, watermark, _ver = await asyncio.to_thread(deps.read_summary, user_id)
            else:
                summary, watermark, _ver = ("", 0.0, 0)
            if deps.read_tail is not None:
                tail = await asyncio.to_thread(deps.read_tail, user_id, watermark, _TAIL_BUDGET)
            else:
                tail = []
            tail = await asyncio.to_thread(
                _inject_tail_images, tail, user_id=user_id, read_images=deps.read_images)
        wake_tail = list(tail) + [{"role": "user", "content": _WAKE_NUDGE}]
        # screen_watch lane grounds on recent shared-screen frames (Task 3). Fetch
        # ONLY screen_recent — NOT perception_snapshot: the resident explicitly sets
        # perception_digest=None for screen-watch jobs (chat_resident_consumer.py:6611).
        #
        # This _cap_data call sits DELIBERATELY OUTSIDE the `async with enclave_sem`
        # block above: `_cap_data` acquires enclave_sem ITSELF (see its body), and
        # asyncio.Semaphore is NOT reentrant. Nesting it inside another
        # `async with enclave_sem` deadlocks whenever the semaphore value is 1
        # (FEEDLING_V2_ENCLAVE_CONCURRENCY defaults to 2, so a naive test would pass
        # while production wedges wherever the value is 1). The gate still bounds the
        # call — _cap_data holds the semaphore for its own turn.
        screen_results = None
        if lane == "screen_watch":
            token = deps.mint_enclave_token(user_id)
            data = await _cap_data(
                store, "screen_recent", api_key=None, runtime_token=token,
                enclave_sem=enclave_sem)
            # _fold_action_results caps each action at _PER_ACTION_CHAR_CAP=2000 chars
            # (the multimodal round's anti-poisoning cap); captions fit.
            screen_results = {"screen_recent": [{"ok": True, "data": data}]}
        try:
            reply = await v2_responder.respond(
                provider_config=provider_config, summary=summary, tail=wake_tail,
                action_results=screen_results,
                system_prompt=(_SCREEN_WATCH_SYSTEM_PROMPT if lane == "screen_watch"
                               else _WAKE_SYSTEM_PROMPT))
        except v2_responder.ResponderError as e:
            if "empty_reply" in str(e) or "no_user_messages" in str(e):
                # Weak wake sleeps: the model (or a degenerate prompt) chose silence —
                # this is a SUCCESSFUL wake, not a failure. No bubble, no error surface.
                await asyncio.to_thread(
                    jobs_store.mark_completed, job_id, claimed_by=claimed_by)
                return "completed"
            if e.kind == "provider_config":
                # Dead/broke BYOK key (402 out-of-credits, 401/403 bad key) — back off
                # BEFORE the silent mark_failed below, so the scheduler stops hammering
                # this key every heartbeat interval (Task 1's due_heartbeat_users query
                # already excludes users still in cooldown).
                await _fence_wake_effect("payment cooldown")
                await asyncio.to_thread(
                    jobs_store.upsert_wake_schedule, user_id,
                    payment_cooldown_until=time.time() + _WAKE_COOLDOWN_SEC)
            raise
        await _fence_wake_effect("wake reply")
        row = await asyncio.to_thread(_write_encrypted_reply, store, reply)
        if row is None:
            raise RuntimeError("reply_encryption_failed")
        await asyncio.to_thread(
            jobs_store.mark_completed, job_id, claimed_by=claimed_by)
        return "completed"
    except Exception as e:  # noqa: BLE001 — wake job: silent mark_failed, never surface/bubble
        code = _safe_failure_code("wake_failed", e)
        log.warning("[v2.worker] wake job %s lane=%s failed code=%s", job_id, lane, code)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, code, claimed_by=claimed_by)
        return "failed"


async def _run_extraction(job_id, user_id: str, lane: str, deps: TurnDeps,
                          provider_config: Any, enclave_sem: "asyncio.Semaphore",
                          claimed_by: str | None = None) -> str:
    """capture / dream：后台记忆抽取。自成一体的 try/except —— 绝不落进 process_job 那个
    chat-turn 的 except（那条会 emit 用户可见的 error status + record_terminal_error）。

    空结果（0 张卡 / 0 条合并）是**成功**：mark_completed，不写任何东西。与 wake lane 的
    「弱唤醒睡回去」同口径 —— 模型选择什么都不做，不是失败。
    """
    try:
        ctx = {}
        # 两次读都是 enclave-bound（read_memory_context 内部 buckets/threads/index 各走一次
        # post_enclave 往返；read_tail 逐条解密），所以**必须同在 enclave_sem 闸内**——enclave
        # 是单线程瓶颈，正是整个子项目要保护的东西（spec §4）。
        async with enclave_sem:
            if deps.read_memory_context is not None:
                try:
                    ctx = await asyncio.to_thread(deps.read_memory_context, user_id) or {}
                except Exception as e:  # noqa: BLE001 — 上下文取数失败 → 降级，不失败（spec §3.5）
                    log.warning("[v2.worker] memory context unavailable for %s: %s", user_id, e)
            tail = await asyncio.to_thread(deps.read_tail, user_id, 0.0, _TAIL_HARD_CAP) \
                if deps.read_tail is not None else []
        window = "\n".join(
            f"- {m.get('role')}: {context.text_of(m.get('content'))}" for m in tail).strip()
        source_ids = [str(m.get("id")) for m in tail if m.get("id")]

        if lane == "capture":
            prompt = build_capture_prompt(
                ai_name=ctx.get("ai_name", ""), user_name=ctx.get("user_name", ""),
                buckets=ctx.get("buckets", ""), threads=ctx.get("threads", ""),
                identity=ctx.get("identity", ""), window=window)
            parse, to_actions = parse_capture_cards, v2_extraction.cards_to_actions
        else:
            prompt = build_dream_prompt(
                ai_name=ctx.get("ai_name", ""), user_name=ctx.get("user_name", ""),
                cards=ctx.get("cards", ""), recent_conversations=window)
            # parse_dream_consolidations 返回 (consolidations, questions, err)。
            # questions 属于「主动提问」= wake 语义，本轮明确丢弃（spec §5.3）。
            parse, to_actions = parse_dream_consolidations, v2_extraction.consolidations_to_actions

        items, reason = await v2_extraction.extract(
            provider_config=provider_config, prompt=prompt, parse=parse)
        if reason:
            raise RuntimeError(reason)
        if not items:
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            return "completed"

        if deps.build_memory_envelope is None or deps.apply_memory_actions is None:
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            return "completed"

        actions, _added, _superseded = to_actions(
            items, occurred_at="", source_ids=source_ids,
            build_envelope=lambda inner: deps.build_memory_envelope(user_id, inner))
        if claimed_by and not await asyncio.to_thread(
            jobs_store.renew_job_lease, job_id, claimed_by,
            ttl_sec=jobs_store.RUNNING_TTL_SEC,
        ):
            raise LostJobLease("extraction lease lost before memory write")
        if deps.runtime_mode_enabled is not None and not await asyncio.to_thread(
            deps.runtime_mode_enabled, user_id
        ):
            raise RuntimeModeChanged("user rolled back before memory write")
        await asyncio.to_thread(deps.apply_memory_actions, user_id, actions)
        await asyncio.to_thread(
            jobs_store.mark_completed, job_id, claimed_by=claimed_by)
        return "completed"
    except Exception as e:  # noqa: BLE001 — 背景 job：静默 mark_failed，绝不 surface/写气泡
        code = _safe_failure_code("extraction_failed", e)
        log.warning("[v2.worker] extraction job %s lane=%s failed code=%s", job_id, lane, code)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, code,
            claimed_by=claimed_by)
        return "failed"


def _inject_tail_images(tail: list[dict], *, user_id: str, read_images) -> list[dict]:
    """把 tail 里最近 `_TAIL_IMAGE_LIMIT` 个图片行的 content 换成 OpenAI 风格 content block
    列表（caption 文本块在前、图片块在后）。返回**新列表**，绝不原地改输入行——compaction
    共用 read_tail 产出的那些 dict。

    任何失败（无 reader / 解密抛错 / 超尺寸 / 缺字段）都静默降级成原来的文本行：用户拿到
    一条看不见图的回复，好过拿到 error chip（no-filler 铁律）。
    """
    if read_images is None:
        return tail
    targets = [r for r in tail if r.get("has_image") and r.get("id")]
    if not targets:
        return tail
    wanted = [str(r["id"]) for r in targets[-_TAIL_IMAGE_LIMIT:]]
    try:
        fetched = read_images(user_id, wanted) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("[v2.worker] read_images failed for %s: %s", user_id, e)
        return tail

    out: list[dict] = []
    for row in tail:
        got = fetched.get(str(row.get("id"))) if row.get("has_image") else None
        b64 = str((got or {}).get("image_b64") or "")
        if not b64 or len(b64) > _IMAGE_MAX_B64_CHARS:
            if got and b64:
                log.warning("[v2.worker] image too large, sending text only (msg=%s, %d chars)",
                            row.get("id"), len(b64))
            out.append(row)
            continue
        mime = str(got.get("image_mime") or "image/jpeg")
        blocks: list[dict] = []
        caption = context.text_of(row.get("content"))
        # `[image]` 是我们自己塞的占位符，不是用户写的字——别当成用户的话发给模型。
        if caption and caption != "[image]":
            blocks.append({"type": "text", "text": caption})
        blocks.append({"type": "image_url",
                       "image_url": {"url": f"data:{mime};base64,{b64}"}})
        out.append({**row, "content": blocks})
    return out


async def process_job(
    job: dict,
    deps: TurnDeps,
    *,
    provider_config: Any,
    is_official: bool,
    api_key: str | None,
    runtime_token: str,
    enclave_sem: "asyncio.Semaphore" = None,
    read_parallelism: int = None,
    replan_budget: int = v2_inval.DEFAULT_REPLAN_BUDGET,
) -> str:
    """一回合：coalesce → planner → executor → (安全点 replan，预算内) → responder（spec §7/§8）。

    provider_config/is_official 由调用方（`_run_turn`）在回合开始前解析好、原样传入并复用
    全程——本函数内绝不再调 `deps.resolve_provider`（single-decrypt-per-turn 不变量）。
    api_key/runtime_token 是 enclave-auth 的两套凭证，只喂 capability 侧（预取 + executor +
    replan 时的 read_messages），从不流向 planner/responder 的 LLM 调用。

    返回终态字符串（"completed"/"failed"），任一步失败 → mark_failed（绝不写占位气泡）。
    """
    if enclave_sem is None:
        enclave_sem = ENCLAVE_SEMAPHORE
    if read_parallelism is None:
        read_parallelism = MAX_READ_ACTION_PARALLELISM

    job_id = job["id"]
    user_id = str(job["user_id"])
    lane = job.get("lane") or "chat"
    claimed_by = str(job.get("claimed_by") or "")
    observed_generation = int(job.get("input_generation") or 0)

    try:
        if not claimed_by or not await asyncio.to_thread(
            jobs_store.mark_running, job_id, claimed_by=claimed_by
        ):
            raise LostJobLease("job ownership lost before start")

        async def _renew_lease() -> None:
            if not await asyncio.to_thread(
                jobs_store.renew_job_lease,
                job_id,
                claimed_by,
                ttl_sec=jobs_store.RUNNING_TTL_SEC,
            ):
                raise LostJobLease("job lease expired or ownership changed")

        async def _ensure_runtime_mode() -> None:
            if deps.runtime_mode_enabled is None or await asyncio.to_thread(
                deps.runtime_mode_enabled, user_id
            ):
                return
            await asyncio.to_thread(
                jobs_store.mark_failed,
                job_id,
                "runtime_mode_changed",
                claimed_by=claimed_by,
            )
            raise RuntimeModeChanged("user is no longer assigned to V2")

        await _ensure_runtime_mode()

        if lane == "maintenance":
            # 自成一体的压缩路径：自己的 try/except（见 `_run_compaction`），绝不落到本
            # 函数下面那个 chat-turn 的 `except`——那个分支会 emit 用户可见的 error status
            # + record_terminal_error（iOS 错误 chip），压缩失败是后台维护事，不该弹给用户。
            return await _run_compaction(
                job_id, user_id, deps, provider_config, enclave_sem, claimed_by)
        if lane in _WAKE_LANES:
            # Self-contained wake path (D3 Task 6): proactive turn, not a reply to a
            # just-sent user message. Own try/except inside `_run_wake` — never falls
            # into the chat-turn `except` below (that branch emits a user-visible
            # error status + record_terminal_error, which wake failures must not do).
            return await _run_wake(
                job_id, user_id, lane, deps, provider_config, enclave_sem, claimed_by)
        if lane in _EXTRACTION_LANES:
            # 自成一体的记忆抽取路径（capture/dream，Task 3）：build prompt → BYOK 抽取 →
            # parse → memory actions。同 _run_compaction/_run_wake 一样有自己的 try/except，
            # 绝不落进下面 chat-turn 的 except（那条会 emit 用户可见 error status +
            # record_terminal_error）——后台 job 永不写气泡、永不弹 error chip。
            return await _run_extraction(
                job_id, user_id, lane, deps, provider_config, enclave_sem, claimed_by)
        if lane != "chat":
            # 真·未注册 lane 的兜底：maintenance/wake（heartbeat/scheduled/manual_wake）/
            # capture/dream 都已在上面各自的 handler 里分派完；能落到这里的只剩既不是 chat、
            # 又没有对应 handler 的 lane（配置错误 / 未来新增但未接线的 lane）。若放它掉进下面
            # 的 chat 回合，planner 一旦要求回复就会写出用户可见的聊天气泡、失败还弹 error chip。
            #
            # 显式失败，静默（背景 job 的既有口径：不写气泡、不 _surface_terminal_error）——
            # 落到这里就是「明确失败」而不是「偷偷写气泡」。
            log.warning("[v2.worker] job %s has unhandled lane=%s", job_id, lane)
            await asyncio.to_thread(
                jobs_store.mark_failed, job_id, f"unhandled_lane:{lane}",
                claimed_by=claimed_by)
            return "failed"
        store = core_store.get_store(user_id)
        runtime_state = await asyncio.to_thread(jobs_store.get_runtime_state, user_id)
        await asyncio.to_thread(_emit_status, user_id, job_id, "processing")

        since = float(runtime_state.get("last_replied_ts") or 0)
        generation = await asyncio.to_thread(
            jobs_store.get_input_generation, job_id, claimed_by=claimed_by)
        if generation is None:
            raise LostJobLease("job ownership lost before input read")
        observed_generation = generation
        coalesced, cursor = await _coalesce_inputs(deps, user_id, since, enclave_sem=enclave_sem)
        if not coalesced and lane == "chat":
            # 无未回复消息（已被别的回合吃掉，或是竞态下的重复 claim）——干净收尾，不落 filler。
            completed, successor_id = await asyncio.to_thread(
                jobs_store.finish_chat_job,
                job_id,
                claimed_by=claimed_by,
                observed_generation=observed_generation,
            )
            if not completed:
                raise LostJobLease("job ownership lost during empty finalization")
            if successor_id is not None:
                await asyncio.to_thread(core_wake_bus.notify, "v2_jobs", user_id)
            await asyncio.to_thread(_emit_status, user_id, job_id, "done")
            await asyncio.to_thread(core_wake_bus.notify, "chat", user_id)
            return "completed"

        replan_count = 0
        # 跨两层循环共享的 LLM 调用计数（见 _TURN_MAX_LLM_CALLS）。外层 replan 不重置它。
        llm_calls = 0
        loop_res = v2_agent_loop.LoopResult({}, {}, None, 0, v2_agent_loop.NO_ACTIONS)

        while True:
            # 便宜预取（无 LLM，enclave-auth 凭证）：memory index + 感知摘要；确定性 digest（无 LLM，§7.2）。
            memory_index = await _cap_data(
                store, "memory_index", api_key=api_key, runtime_token=runtime_token,
                enclave_sem=enclave_sem)
            perception_summary = await _cap_data(
                store, "perception_snapshot", api_key=api_key, runtime_token=runtime_token,
                enclave_sem=enclave_sem)
            digest = {"messages": [{"content": m["content"][:400]} for m in coalesced[-6:]]}

            async def _decide(round_idx: int, prior: dict) -> v2_agent_loop.Decision:
                """json_planner 后端（spec §5 默认）：跑用户 BYOK 的结构化 JSON planner。

                预算耗尽 → 立刻收手（wants_reply=True），把最后一个名额留给 responder。
                `final_text` 恒为 None —— 散文由 responder 写（spec §4）。
                """
                nonlocal llm_calls
                await _renew_lease()
                if llm_calls >= _TURN_MAX_LLM_CALLS - 1:
                    return v2_agent_loop.Decision(actions=[], wants_reply=True)
                llm_calls += 1
                steps = await v2_planner.plan(
                    store,
                    provider_config=provider_config, is_official=is_official,
                    coalesced_messages=coalesced, digest=digest, memory_index=memory_index,
                    perception_summary=perception_summary, runtime_state=runtime_state,
                    lane=lane, reason=str(job.get("reason") or ""),
                    prior_action_results=prior or None)
                return v2_agent_loop.Decision(
                    actions=[s for s in steps if s["type"] != "final_response"],
                    wants_reply=any(s["type"] == "final_response" for s in steps))

            async def _run_tools(actions: list[dict]) -> dict:
                """Persist trajectory shape, fence ownership, then drain tools."""
                # A planner call happens between the preceding renewal and this
                # side-effect boundary. Fence again before any capability executes,
                # especially the serialized write actions.
                await _ensure_runtime_mode()
                await _renew_lease()
                action_ids = await asyncio.to_thread(
                    jobs_store.add_actions, job_id, user_id,
                    [{"type": s["type"], "payload": s["payload"]} for s in actions])
                for s, aid in zip(actions, action_ids):
                    s["_action_id"] = aid

                async def _before_write() -> None:
                    await _ensure_runtime_mode()
                    await _renew_lease()

                return await v2_executor.execute_plan(
                    store, job_id, api_key=api_key, runtime_token=runtime_token,
                    plan=actions, read_parallelism=read_parallelism,
                    enclave_sem=enclave_sem, before_write=_before_write)

            loop_res = await v2_agent_loop.run_turn(
                decide=_decide, run_tools=_run_tools, max_rounds=_LOOP_MAX_ROUNDS)

            # 安全点（before_final_response）：跨进程/跨 worker 写入的新消息只活在 DB 里，
            # 本进程内存态的 store.chat_messages 未必看得到——先 reload 再判定，避免漏判。
            # evaluate 只看 role/ts（密文行本身不含明文，无需解密即可判定「有没有新用户消息」）。
            refresh_chat = getattr(store, "reload_chat_strict", None)
            if not callable(refresh_chat):
                refresh_chat = store.reload
            await asyncio.to_thread(refresh_chat)
            decision = v2_inval.evaluate(
                store.chat_messages, safe_point="before_final_response",
                coalesced_cursor_ts=cursor, replan_count=replan_count, replan_budget=replan_budget)
            if decision == v2_inval.REPLAN and llm_calls < _TURN_MAX_LLM_CALLS - 1:
                await asyncio.to_thread(v2_inval.invalidate, job_id, replan_job_id=job_id)
                replan_count += 1
                generation = await asyncio.to_thread(
                    jobs_store.get_input_generation, job_id, claimed_by=claimed_by)
                if generation is None:
                    raise LostJobLease("job ownership lost before replan read")
                observed_generation = generation
                coalesced, cursor = await _coalesce_inputs(deps, user_id, since, enclave_sem=enclave_sem)
                continue
            break

        action_state = {"action_results": loop_res.action_results,
                        "action_digest": loop_res.action_digest}
        # BUG-4（矩阵 §E）：chat lane **恒**回复。「planner 没要 final_response」在单轮形状下
        # 被 worker 误读成「这回合不用回复」，可信模型漏写时用户消息被静默吞掉、零气泡。
        # 循环下同一个信号的含义是「想再查一轮」；轮数/预算用尽就用手上的结果强制收口。
        # 这不是占位气泡——responder 仍然产出真正的 model-authored 文本（no-filler 不变量）。
        wants_reply = lane == "chat" or loop_res.stop_reason == v2_agent_loop.WANTS_REPLY
        if wants_reply:
            await _ensure_runtime_mode()
            await _renew_lease()
            await asyncio.to_thread(_emit_status, user_id, job_id, "writing_reply")
            # D1：responder 现在吃 summary（早前对话摘要）+ tail（双角色逐字近期窗口），
            # 不再是"仅合并的未回复 user 消息"。deps.read_summary/read_tail 为 None 时
            # （既有单测不装配这两样、只打桩 v2_responder.respond）退化成空摘要+空 tail——
            # 不影响那些测试的断言（它们看的是 respond 被打桩后的返回值/调用与否）。
            if deps.read_summary is not None and deps.read_tail is not None:
                async with enclave_sem:
                    summary, watermark, _ver = await asyncio.to_thread(deps.read_summary, user_id)
                    tail = await asyncio.to_thread(deps.read_tail, user_id, watermark, _TAIL_HARD_CAP)
                    tail = await asyncio.to_thread(
                        _inject_tail_images, tail, user_id=user_id, read_images=deps.read_images)
            else:
                summary, tail = "", []
            # responder 走 BYOK provider_config；ResponderError（空回复/provider 错）交给下面
            # 统一的 except 兜底 mark_failed——no-filler 铁律：绝不写占位气泡。v2_responder.respond
            # 现在原生 async，直接 await，不再经 to_thread 桥线程池（同上，治并发天花板）。
            # Task 4：usage_out 是纯出参（responder 保持 hosted-free/无 job 上下文），成功
            # 返回后由 worker（有 job_id/user_id/lane）记 v2_turn_metrics，喂 D4 load-test。
            if loop_res.final_text:
                # 原生 tool-calling 后端在收手时自带回复（spec §3.1）。默认的 json_planner
                # 后端恒为 None，所以今天这条分支不会走到——留着，是为了别在接原生后端时
                # 白丢一次已经生成好的文本、再花一次 token 让 responder 重写。
                reply = loop_res.final_text
            else:
                _usage: dict = {}
                _t0 = time.monotonic()
                reply = await v2_responder.respond(
                    provider_config=provider_config, summary=summary, tail=tail,
                    action_results=action_state["action_results"], usage_out=_usage)
                if deps.record_turn_metric is not None:
                    try:
                        await asyncio.to_thread(
                            deps.record_turn_metric,
                            job_id=job_id, user_id=user_id, lane=lane,
                            prompt_tokens=_usage.get("prompt_tokens"),
                            completion_tokens=_usage.get("completion_tokens"),
                            latency_ms=int((time.monotonic() - _t0) * 1000),
                        )
                    except Exception as e:  # noqa: BLE001 — 记指标失败绝不能拖垮已经产出的回复
                        log.warning("[v2.worker] record_turn_metric failed job=%s: %s", job_id, e)
            await _ensure_runtime_mode()
            await _renew_lease()
            reply_row = await asyncio.to_thread(_write_encrypted_reply, store, reply)
            if reply_row is None:
                raise RuntimeError("reply_encryption_failed")
            # 超预算 → best-effort 入队一个 maintenance lane 的压缩 job（不阻塞、不拖垮
            # 本回合——enqueue_job 本身命中 single-flight 会 coalesce，失败只记日志）。
            if tail and context.needs_compaction(tail, budget=_TAIL_BUDGET):
                try:
                    await asyncio.to_thread(jobs_store.enqueue_job, user_id, "maintenance", reason="compaction")
                except Exception as e:  # noqa: BLE001 — 压缩入队失败绝不能拖垮已经写成的这条回复
                    log.warning("[v2.worker] enqueue compaction failed for %s: %s", user_id, e)

        # last_replied_ts 单调前进：cursor==0.0（本轮没折入新消息，如纯 sleep 的 heartbeat）
        # 时绝不回退已有游标——否则下一回合会把早就答过的旧消息重新折入。
        new_last_replied = cursor if cursor > since else since
        await asyncio.to_thread(
            jobs_store.upsert_runtime_state, user_id,
            {"last_replied_ts": new_last_replied, "action_digest": action_state["action_digest"]})
        await _ensure_runtime_mode()
        completed, successor_id = await asyncio.to_thread(
            jobs_store.finish_chat_job,
            job_id,
            claimed_by=claimed_by,
            observed_generation=observed_generation,
        )
        if not completed:
            raise LostJobLease("job ownership lost during finalization")
        if deps.record_terminal_error is not None:
            try:
                await asyncio.to_thread(deps.record_terminal_error, user_id, "")
            except Exception as e:  # noqa: BLE001 — reply/job are already durable
                log.warning("[v2.worker] clear terminal error failed job=%s: %s", job_id, e)
        await asyncio.to_thread(_emit_status, user_id, job_id, "done")
        # 跨进程唤醒 web 层 parked 的 chat long-poll（worker 与 web 是不同进程/CVM，origin 不同）。
        await asyncio.to_thread(core_wake_bus.notify, "chat", user_id)
        if successor_id is not None:
            await asyncio.to_thread(core_wake_bus.notify, "v2_jobs", user_id)
        # End-of-turn effect-outbox drain (Task 6 / spec A6): apply any pending
        # generation-fenced effects for this user with the real dispatch sinks.
        # Best-effort — the turn's own reply/runtime-state/job transition above
        # are already durable by this point, so a failure here must not turn a
        # completed turn into a failed one.
        if deps.apply_pending_effects is not None:
            try:
                await asyncio.to_thread(deps.apply_pending_effects, user_id)
            except Exception as e:  # noqa: BLE001 — see comment above
                log.warning("[v2.worker] apply_pending_effects failed user=%s: %s", user_id, e)
        return "completed"
    except LostJobLease as e:
        # The winning lifecycle transition (normally the reaper) owns terminal
        # visibility. A stale worker must not overwrite it or emit a duplicate error.
        log.warning("[v2.worker] job %s fenced out: %s", job_id, e)
        return "failed"
    except RuntimeModeChanged as e:
        log.info("[v2.worker] job %s stopped at rollout fence: %s", job_id, e)
        return "failed"
    except Exception as e:  # noqa: BLE001 — 任何失败落 last_error，绝不写占位气泡
        message = _safe_failure_code("turn_failed", e)
        log.warning("[v2.worker] job %s failed code=%s", job_id, message)
        owned = await asyncio.to_thread(
            jobs_store.mark_failed, job_id, message, claimed_by=claimed_by)
        if owned:
            await asyncio.to_thread(_surface_terminal_error, deps, user_id, job_id, message)
        return "failed"


async def _run_turn(job: dict, deps: TurnDeps) -> str:
    """把一个已 claim 的 job 交给 `process_job` 之前，先做一次性的 enclave-bound 解析：
    单次解密 BYOK provider key（single-decrypt-per-turn）+ 判定 is_official + 铸一个
    enclave-auth runtime_token。resolve_provider 失败（未配置/解密失败等）直接 mark_failed，
    不进入全流程（没有可用 provider，planner/responder 都跑不了）。"""
    job_id = job["id"]
    user_id = str(job["user_id"])
    claimed_by = str(job.get("claimed_by") or "")
    lane = str(job.get("lane") or "chat")
    try:
        async with ENCLAVE_SEMAPHORE:
            provider_config, meta = await asyncio.to_thread(deps.resolve_provider, user_id)
        if provider_config is None:
            err = "provider_unavailable"
            owned = await asyncio.to_thread(
                jobs_store.mark_failed, job_id, err, claimed_by=claimed_by)
            if owned and lane == "chat":
                await asyncio.to_thread(_surface_terminal_error, deps, user_id, job_id, err)
            return "failed"
        is_official = deps.is_official(provider_config)
        runtime_token = await asyncio.to_thread(deps.mint_enclave_token, user_id)
        return await process_job(
            job, deps,
            provider_config=provider_config, is_official=is_official,
            api_key=None, runtime_token=runtime_token,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — claimed work must always terminalize visibly
        message = _safe_failure_code("turn_setup_failed", e)
        log.warning("[v2.worker] job %s outer turn failure code=%s", job_id, message)
        owned = await asyncio.to_thread(
            jobs_store.mark_failed, job_id, message, claimed_by=claimed_by)
        if owned and lane == "chat":
            await asyncio.to_thread(_surface_terminal_error, deps, user_id, job_id, message)
        return "failed"


# —— "v2_jobs" 即时唤醒（FIX 3）——
# chat_send_core.model_api_chat_send_core 入队新 job 后调
# core_wake_bus.notify("v2_jobs", user_id)（跨进程 Postgres NOTIFY——web 层跟本 worker
# 通常是不同进程/CVM）。serve_worker.wire_assembly 把 "v2_jobs" channel 接到
# `on_v2_job_notify`；但 wake_bus 的监听线程回调发生在一个普通 OS 线程上，不是本进程
# event loop 的线程，不能直接 `event.set()`（asyncio 原语不是线程安全的）——必须经
# `loop.call_soon_threadsafe` 桥回 event loop。context（loop/event）由
# serve_worker._serve 在 event loop 起来之后才设置（wire_assembly 本身在 `asyncio.run`
# 之前跑，那时还没有 running loop）；未设置时（例如单测直接调 run_worker_loop，不经
# serve_worker 装配）on_v2_job_notify 静默 no-op，_slot_loop 照旧退化为纯 poll——
# 不影响正确性，只影响拿到新 job 的延迟上限（至多 poll_interval）。
_wake_loop: "asyncio.AbstractEventLoop | None" = None
_wake_event: "asyncio.Event | None" = None


def set_job_wake_context(loop: "asyncio.AbstractEventLoop", event: "asyncio.Event") -> None:
    """由 serve_worker._serve（event loop 已起）调用一次，把本进程的 loop/event 绑定给
    `on_v2_job_notify` 使用。"""
    global _wake_loop, _wake_event
    _wake_loop = loop
    _wake_event = event


def on_v2_job_notify(user_id: str) -> None:
    """core_wake_bus 的 "v2_jobs" channel handler（由 serve_worker.wire_assembly 注册）。
    在 wake-bus 监听线程上被调用——只能用 call_soon_threadsafe 桥到 event loop 线程去
    set()，直接调用 event.set() 会跨线程碰 asyncio 内部状态，不安全。"""
    loop, event = _wake_loop, _wake_event
    if loop is None or event is None:
        return  # 装配未接线（单测）或启动竞态窗口——退化为纯 poll，非错误
    loop.call_soon_threadsafe(event.set)


async def _wait_for_job_or_stop(
    stop_event: asyncio.Event, wake_event: "asyncio.Event | None", poll_interval: float
) -> None:
    """抢不到活时的等待：stop_event 置位立刻醒（drain）；有 wake_event 时命中即时唤醒
    立刻醒（不必等满 poll_interval）；否则最多等 poll_interval（原 poll-only 行为，
    wake_event=None 时的向后兼容路径——未经 serve_worker 装配的调用方/既有测试不受影响）。"""
    if wake_event is None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass
        return
    stop_task = asyncio.ensure_future(stop_event.wait())
    wake_task = asyncio.ensure_future(wake_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {stop_task, wake_task}, timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (stop_task, wake_task):
            if not t.done():
                t.cancel()
    if wake_task in done:
        wake_event.clear()  # 消费掉这次唤醒；下个 slot 的 wait() 若已在 done 里不受影响


async def _slot_loop(
    worker_id: str, *, poll_interval: float, stop_event: asyncio.Event, deps: TurnDeps,
    wake_event: "asyncio.Event | None" = None, lanes: "set | None" = None,
) -> None:
    """一个 job-slot：抢一个 job 就跑一回合，抢不到就等待（poll_interval 兜底，
    wake_event 命中时立刻醒——见 `_wait_for_job_or_stop`）。stop_event 置位后不再抢新活，
    跑完手上的即退出（优雅 drain）。

    lanes（可选）：转给 `jobs_store.claim_next_job` 的 lane 白名单（Task 2）。None＝不限制
    （行为与改动前完全一致）；非 None 时这个 slot 只抢白名单里的 lane——`run_worker_loop`
    用它给部分 slot 划专用车道（见 `_reserved_lane_slots`）。

    per-iteration 的抢活 + 跑回合整段包 try/except：单个 slot 上的瞬时故障（例如 claim/
    mark_running 撞到一次性 DB 错误）绝不允许冒出这个协程、拖垮 run_worker_loop 里其他
    仍然健康的 slot——记日志后 continue，下一轮再抢。"""
    while not stop_event.is_set():
        job = None
        try:
            job = await asyncio.to_thread(jobs_store.claim_next_job, worker_id, lanes=lanes)
            if job is None:
                await _wait_for_job_or_stop(stop_event, wake_event, poll_interval)
                continue
            await _run_turn(job, deps)
        except Exception as e:  # noqa: BLE001 — 单 slot 故障绝不冒出去拖垮其他 slot
            log.warning("[v2.worker] slot %s iteration failed: %s", worker_id, e)
            if job is not None:
                try:
                    user_id = str(job.get("user_id") or "")
                    message = f"slot_failure:{type(e).__name__.lower()}"
                    owned = await asyncio.to_thread(
                        jobs_store.mark_failed,
                        job["id"],
                        message,
                        claimed_by=str(job.get("claimed_by") or worker_id),
                    )
                    if owned and str(job.get("lane") or "chat") == "chat":
                        await asyncio.to_thread(
                            _surface_terminal_error, deps, user_id, job["id"], message)
                except Exception as recovery_error:  # noqa: BLE001
                    # Recovery is best-effort: the independent lease reaper is
                    # the final owner of terminalization.  A second DB/error-
                    # surface failure must never kill this slot and leave the
                    # process advertising capacity it no longer has.
                    log.error(
                        "[v2.worker] slot %s recovery failed code=%s",
                        worker_id,
                        _safe_failure_code("slot_recovery_failed", recovery_error),
                    )
            await _wait_for_job_or_stop(stop_event, wake_event, poll_interval)
            continue


async def run_worker_loop(
    worker_id: str, *, max_workers: int, poll_interval: float, stop_event: asyncio.Event, deps: TurnDeps,
    wake_event: "asyncio.Event | None" = None,
) -> None:
    """起 max_workers 个 job-slot 协程共抢同一张 agent_jobs（SKIP LOCKED 无争用）。
    stop_event 置位 → 所有 slot 跑完手上 job 后退出（SIGTERM 优雅 drain 的落点）。
    wake_event（可选）由 serve_worker._serve 传入，桥 "v2_jobs" 即时唤醒（FIX 3）——
    未传（None）时所有 slot 退化为纯 poll，向后兼容既有调用方/测试。

    lane 预留（D3 Task 5）：前几个 slot 只抢 {"chat","manual_wake"}（见 `_reserved_lane_slots`），
    保证 scheduler（Task 4）产出的 heartbeat 唤醒风暴抢不走全部 slot、饿死聊天回复，
    同时始终留一个 unrestricted slot，避免后台 lane 永久 pending。
    `FEEDLING_V2_CHAT_RESERVED_SLOTS` 显式设置时覆盖默认的 max(1, max_workers // 2)；
    留空/未设置时用默认值。

    `_slot_loop` catches recoverable per-job failures.  Any exception that still
    escapes is therefore a broken slot invariant: cancel the siblings and let
    the process supervisor restart the worker instead of silently running with
    fewer (possibly zero) slots while its heartbeat advertises full capacity."""
    _reserved_env = os.environ.get("FEEDLING_V2_CHAT_RESERVED_SLOTS", "").strip()
    reserved = int(_reserved_env) if _reserved_env else None
    assignments = _reserved_lane_slots(max_workers, reserved)
    slots = [
        asyncio.create_task(
            _slot_loop(f"{worker_id}#{i}", poll_interval=poll_interval, stop_event=stop_event,
                       deps=deps, wake_event=wake_event, lanes=assignments[i])
        )
        for i in range(len(assignments))
    ]
    try:
        await asyncio.gather(*slots)
    except BaseException:
        stop_event.set()
        for slot in slots:
            if not slot.done():
                slot.cancel()
        await asyncio.gather(*slots, return_exceptions=True)
        raise
