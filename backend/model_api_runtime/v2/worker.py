"""V2 Postgres-queue turn orchestration.

The process entrypoint is ``serve_worker.py``. Each claimed job enters the
same provider-native ``tool_loop.run_tool_loop`` with the same tool catalog;
there is no provider-identity classifier or alternate rule path. Chat turns
must end in an encrypted model-authored reply, while proactive wake turns may
legitimately remain silent. ``TurnDeps`` keeps hosted/enclave assembly above
this module so the worker never imports the hosted layer.

两套凭证不混（spec §5）：
- provider_config（用户 BYOK）：只喂统一工具循环里的 provider 调用（`tool_loop.run_tool_loop`）。
  resolve_provider(user_id) 在本回合只解密一次（single-flight 之外的每 job 一次），
  由 `_slot_loop` 在把 job 交给 `process_job` 之前调用、并把结果原样传入、整个回合复用。
- api_key + runtime_token（enclave-auth）：只喂 executor 的 capability 调用 + 便宜预取
  （memory_index/perception_snapshot）+ `TurnDeps.read_messages`（enclave 内解密取明文）。
  runtime_token 由 `TurnDeps.mint_enclave_token` 铸造——这只是签一个短时效令牌（HMAC，不
  是解密），可以在回合内按需多铸，不违反「resolve_provider 只解密一次」的不变量（那条
  不变量特指 BYOK provider-key 的解密，不是 enclave-auth 令牌的签发次数）。

敏感面分层（spec §5/§9）：capability 结果只在当前 native tool transcript
里存活；非敏感 action_digest（ok/count 粗计数）才落 runtime_state。status
事件（processing/writing_reply/done/error）经 status_stream.redact_status 脱敏。

并发：asyncio 事件循环 + asyncio.to_thread 把同步 jobs_store/enclave 调用移出 loop。
四种 provider wire 全部 await 原生 async HTTP transport；provider 并发不再受默认线程池
大小限制。
ENCLAVE_SEMAPHORE 框住 turn 里所有 enclave-bound 调用（provider-key 解密 + 逐条 chat 解密
+ capability 调用），治 spec R3（enclave 单线程瓶颈，多 worker 齐打会放大 502）。
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import itertools
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import db
import provider_client
from provider_types import ToolExchange
from capabilities import registry as cap_registry
from core import envelope as core_envelope
from core import store as core_store
from core import wake_bus as core_wake_bus
from model_api_runtime.v2 import coalesce as v2_coalesce
from model_api_runtime.v2 import compaction as v2_compaction
from model_api_runtime.v2 import context
from model_api_runtime.v2 import cursor as v2_cursor
from model_api_runtime.v2 import effect_outbox as v2_effect_outbox
from model_api_runtime.v2 import effect_id as v2_effect_id
from model_api_runtime.v2 import executor as v2_executor
from model_api_runtime.v2 import extraction as v2_extraction
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import kill_switch
from model_api_runtime.v2 import status_stream
from model_api_runtime.v2 import tool_loop as v2_tool_loop
# 纯 prompt/parse 模块（无 I/O、不碰 DB/enclave）——依赖方向允许 worker 直接 import
# （extraction.py 同样只 import 这两个 + provider_client）。
from memory.capture_prompt_v1 import build_capture_prompt, parse_capture_cards
from memory.dream_prompt_v1 import build_dream_prompt, parse_dream_consolidations

log = logging.getLogger("feedling.runtime_v2.worker")

# `_slot_loop` installs one callback in this task-local context for the duration
# of an active turn.  Deep helpers can report real progress without threading a
# telemetry argument through every public/tested function signature.  Context
# variables are copied per asyncio Task, so concurrent slots cannot refresh one
# another's watchdog clocks.
_TURN_PROGRESS_CB: contextvars.ContextVar[Callable[[str], None] | None] = (
    contextvars.ContextVar("v2_turn_progress_cb", default=None)
)


def _report_turn_progress(stage: str) -> None:
    """Refresh this slot's stall clock at a completed/starting work boundary.

    The callback is process-local telemetry only and must never affect turn
    correctness.  `_slot_loop`/the pipe callback already defend independently;
    this final guard keeps a broken observer from failing a user turn.
    """
    callback = _TURN_PROGRESS_CB.get()
    if callback is None:
        return
    try:
        callback(str(stage))
    except Exception as exc:  # noqa: BLE001 — watchdog telemetry is best-effort
        log.debug("[v2.worker] turn progress callback failed stage=%s: %s", stage, exc)


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

# D1（full-conversation context）：turn 使用 summary+tail，而不是"仅未回复的 user
# 消息"。tail 超过 _TAIL_BUDGET 条（双角色计数）时，chat turn 顺手（best-effort，不阻塞
# 回复）入队一个 maintenance lane 的 compaction job，把最旧的一批折进摘要，只留
# _TAIL_KEEP 条最近消息逐字保留。_TAIL_HARD_CAP 是 chat turn 读 tail 时的硬上限（喂
# 当前 turn，不是压缩用——压缩自己读到 watermark 之后的全部，见 `_run_compaction`）。
_TAIL_BUDGET = int(os.environ.get("FEEDLING_V2_TAIL_BUDGET_MSGS", "20"))
_TAIL_KEEP = int(os.environ.get("FEEDLING_V2_TAIL_KEEP_MSGS", "10"))
_TAIL_HARD_CAP = int(os.environ.get("FEEDLING_V2_TAIL_HARD_CAP", "60"))
_COMPACTION_BATCH = _positive_int_env("FEEDLING_V2_COMPACTION_BATCH_MSGS", "200")
# A message-count cap alone is not a prompt-size bound: 200 maximum-size chat
# rows can still produce a multi-megabyte compaction request.  Both inline
# catch-up and the maintenance lane therefore take the oldest prefix that fits
# this rendered-character budget.  A single row larger than the budget fails
# loudly instead of advancing the watermark past content the model never saw.
_COMPACTION_BATCH_CHARS = _positive_int_env(
    "FEEDLING_V2_COMPACTION_BATCH_CHARS", "120000")
# Catch-up may legitimately need many successful batches (for example after a
# long worker outage), but it must not monopolise a turn forever.  This is an
# overall wall-clock bound; the per-provider timeout remains independently
# enforced by compaction.compact/provider_client.
_PROMPT_CATCHUP_DEADLINE_SEC = float(
    os.environ.get("FEEDLING_V2_PROMPT_CATCHUP_DEADLINE_SEC", "600"))
if not math.isfinite(_PROMPT_CATCHUP_DEADLINE_SEC) or _PROMPT_CATCHUP_DEADLINE_SEC <= 0:
    raise RuntimeError("FEEDLING_V2_PROMPT_CATCHUP_DEADLINE_SEC must be positive")

# 每回合最多注入最近 N 张图。enclave 单线程（每张图一次往返），且无 prompt caching ——
# tail 里的图片每个回合都要重发，token 成本随图片数线性上升。
_TAIL_IMAGE_LIMIT = int(os.environ.get("FEEDLING_V2_TAIL_IMAGE_LIMIT", "2"))
# 单张图 b64 上限；超限跳过注入、退化成文本标记（不引入图像缩放依赖）。
_IMAGE_MAX_B64_CHARS = int(os.environ.get("FEEDLING_V2_IMAGE_MAX_B64_CHARS", "2000000"))

# 单个 native tool loop 的 provider 调用硬闸。最后一次调用会禁用
# tools 来强制收口，使模型无法用无限工具链烧穿用户的 BYOK key。
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


class TurnError(RuntimeError):
    """A turn cannot safely produce or cover its required final reply."""


def _safe_failure_code(scope: str, exc: BaseException) -> str:
    """Stable plaintext error code that never embeds exception messages."""
    if isinstance(exc, TurnError):
        raw = str(exc)
        if raw in {"empty_reply", "no_user_messages"}:
            kind = raw
        else:
            # Keep the established persisted code stable across the internal
            # responder-module removal; dashboards and clients may group by it.
            kind = "responder_error"
    else:
        kind = type(exc).__name__.lower() or "error"
    return f"{scope}:{kind}"[:120]


@dataclass
class TurnDeps:
    """turn 执行体的注入式依赖（生产实现见 serve_worker.build_production_deps）。

    Hosted configuration, encrypted-message reads, and side-effect sinks are
    injected here. Model calls and encrypted reply construction stay in this
    hosted-free module.
    """
    read_messages: Callable[[str], list[dict]]           # user_id -> [{"id","ts","role","content"}]（enclave 解密明文）
    resolve_provider: Callable[[str], tuple[Any, dict]]   # user_id -> (ProviderConfig|None, meta)：BYOK，回合内只调一次
    mint_enclave_token: Callable[[str], str]              # user_id -> 短时效 runtime_token（HMAC 签发，非解密，可按需多铸）
    # Compatibility/test seam for dependency-isolated callers. Production
    # leaves this None: jobs_store's terminal outbox now updates the captured
    # active route and acknowledges delivery in one route-version-fenced DB
    # transaction, avoiding callback crash windows and stale-route replay.
    record_terminal_error: Callable[[str, str], None] | None = None
    # Production cursor-aware reader. Keeping this optional preserves the many
    # pure tests that inject the older one-argument reader.
    read_messages_since: Callable[[str, float], list[dict]] | None = None
    # Seq-native production boundaries.  These are distinct from the legacy ts
    # callbacks above so a positional float/int mix-up cannot silently drop a
    # same-timestamp message during rollout.
    read_messages_after_seq: Callable[[str, int], list[dict]] | None = None
    runtime_mode_enabled: Callable[[str], bool] | None = None
    # (job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms) -> None
    # （kwargs-only，见 jobs_store.record_turn_metric）：**已废弃/不再被 process_job 调用**
    # ——spec B5 把逐次 per-call INSERT 换成了一个 per-job 的 `TurnMetrics` 累加器，终态时
    # 直接调 `jobs_store.record_whole_turn_metric`（worker.py 内部完成，不再需要经这个
    # 注入回调）。字段留着只是不破坏既有装配/测试（`serve_worker.build_production_deps`
    # 仍会注入 `jobs_store.record_turn_metric`，但 worker.py 不再读它）；新代码不要依赖
    # 这个回调被调用。
    record_turn_metric: Callable[..., None] | None = None
    # (user_id, after_ts, limit) -> [{"id","ts","role","content"}]：最近窗口，BOTH
    # roles，ts>after_ts，enclave 解密明文（D1：让 turn 能看见真实对话上下文，不再局限于
    # "上次回复之后的 user 消息"那一批）。默认 None：worker.py 自身不 import hosted，
    # 测试/其他调用方不必提供；生产装配见 serve_worker.build_production_deps。
    read_tail: Callable[[str, float, int], list[dict]] | None = None
    read_compaction_tail: Callable[[str, float, int], list[dict]] | None = None
    read_tail_after_seq: Callable[..., list[dict]] | None = None
    read_compaction_tail_after_seq: Callable[..., list[dict]] | None = None
    # user_id -> (summary_plaintext, watermark_ts, version)：读取该用户当前会话摘要（enclave
    # 解密明文）；从未压缩过时 ("", 0.0, 0)（D1：turn 看 摘要+尾巴 而不是全量重放）。默认
    # None：worker.py 自身不 import hosted，测试/其他调用方不必提供；生产装配见
    # serve_worker.build_production_deps。
    read_summary: Callable[[str], tuple[str, float, int]] | None = None
    # user_id -> (summary, watermark_ts, version, watermark_seq)
    read_summary_with_seq: Callable[[str], tuple[str, float, int, int]] | None = None
    # (user_id, summary, watermark_ts, expected_version[, watermark_seq]) -> True if CAS
    # landed：本地加密（core_envelope，非 enclave 往返）+ CAS 写回 v2_conversation_summary
    # （Task 2 storage）。expected_version 不匹配（别的回合已推进过摘要）时返回 False，
    # 调用方按丢弃本次压缩处理，不重试、不报错——下一回合会用新版本重新压缩。默认 None：同上。
    # watermark_seq（D5/Task 9）是可选的第 5 个位置参数——_run_compaction 只有在能拿到折叠
    # 批次最后一行的精确 seq 时才会传它（生产路径总是能拿到；某些窄签名的测试 fake 只接 4
    # 个参数，_run_compaction 会退化成旧的 4 参调用，两边都不破）。
    write_summary: Callable[..., bool] | None = None
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


@dataclass
class TurnMetrics:
    """Per-job whole-turn metric accumulator (Hosted Runtime V2 PR B / spec B5).

    Exactly one instance is created per job at turn start (in `_run_turn`, the
    earliest point `job_id`/`user_id`/`lane` are known — before that, chat-turn
    setup failures like an unresolvable BYOK provider have nowhere else to
    attribute a metric row to) and threaded down through `process_job` and
    each self-contained lane handler (`_run_compaction`/`_run_wake`/
    `_run_extraction`). `flush()` upserts ONE `v2_turn_metrics` row per job_id
    (idempotent replace, never append — see `jobs_store.record_whole_turn_metric`),
    called at exactly the terminal points spec'd by B5: EVERY lane's success
    return AND every `mark_failed` call site — chat, `maintenance` (compaction),
    wake (`heartbeat`/`scheduled`/`manual_wake`/`screen_watch`) and extraction
    (`capture`/`dream`) alike. The background lanes are not metric-free: each
    makes at least one real BYOK provider call per job (`_run_wake` and chat's
    `process_job` both drive `tool_loop.run_tool_loop`, which calls `add_call`
    itself via its `add_usage` callback for every provider round; `_run_extraction`
    → `v2_extraction.extract`, `_run_compaction` → `v2_compaction.compact`, the
    last two only when the lane actually reaches its compaction/extraction call —
    e.g. a compaction job whose tail is already under budget skips the call and
    legitimately flushes `model_calls=0`), so a success there carries real
    token/usage data worth a row — this is precisely the lane that burns
    idle-user BYOK tokens on heartbeat/scheduled wakes, so it is the metric
    consumers most need. `tool_loop.run_tool_loop` surfaces usage via its
    `add_usage` callback (chat and wake alike, since PR C9b — see `process_job`/
    `_run_wake`), so both lanes record real prompt/completion/cache tokens per
    provider round. `v2_extraction.extract` and `v2_compaction.compact` expose
    the same usage callback, closing the former background-lane accounting gap.
    Missing provider telemetry remains NULL rather than being invented as zero;
    explicit coverage counters make partial telemetry visible.

    `add_call` accumulates usage from a REAL provider call —
    `tool_loop.run_tool_loop` (chat and wake both use the same provider-native
    rounds), `v2_extraction.extract`, and `v2_compaction.compact`. `retries` stays 0
    today: `reliable_chat_completion*` retries internally but
    doesn't yet surface a retry count to its callers (out of scope for this
    task) — the column/field exist so a future provider-level retry count can
    be wired in without another migration.
    """
    job_id: Any
    user_id: str
    lane: str
    model_calls: int = 0
    retries: int = 0
    provider: str | None = None
    model: str | None = None
    cache_route_fingerprint: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_miss_tokens: int | None = None
    usage_reported_calls: int = 0
    cache_reported_calls: int = 0
    _flushed: bool = False
    _started: float = 0.0

    def __post_init__(self) -> None:
        self._started = time.monotonic()

    @staticmethod
    def _sum_optional(current: int | None, value: Any) -> int | None:
        if value is None:
            return current
        try:
            parsed = max(0, int(value))
        except (TypeError, ValueError):
            return current
        return (current or 0) + parsed

    def bind_provider(self, provider_config: Any) -> None:
        """Record non-secret provider/model/cache-route identity after BYOK resolution."""
        provider = str(getattr(provider_config, "provider", "") or "").strip()
        model = str(getattr(provider_config, "model", "") or "").strip()
        route = str(
            getattr(provider_config, "prompt_cache_route_fingerprint", "") or ""
        ).strip()
        self.provider = provider or None
        self.model = model or None
        self.cache_route_fingerprint = route or None

    def add_call(self, usage: dict | None, *, retried: bool = False) -> None:
        self.model_calls += 1
        if retried:
            self.retries += 1
        if not isinstance(usage, dict):
            return
        usage_fields = (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens",
        )
        if any(usage.get(field) is not None for field in usage_fields):
            self.usage_reported_calls += 1
        cache_fields = (
            "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens",
        )
        if any(usage.get(field) is not None for field in cache_fields):
            self.cache_reported_calls += 1
        self.prompt_tokens = self._sum_optional(
            self.prompt_tokens, usage.get("prompt_tokens"))
        self.completion_tokens = self._sum_optional(
            self.completion_tokens, usage.get("completion_tokens"))
        self.cache_read_tokens = self._sum_optional(
            self.cache_read_tokens, usage.get("cache_read_tokens"))
        self.cache_write_tokens = self._sum_optional(
            self.cache_write_tokens, usage.get("cache_write_tokens"))
        self.cache_miss_tokens = self._sum_optional(
            self.cache_miss_tokens, usage.get("cache_miss_tokens"))

    def flush(self, *, failed: bool, status: str) -> None:
        """Idempotent per-job upsert; guarded so this SAME accumulator instance
        never double-writes even if a lane handler somehow reaches two terminal
        points (harmless either way — the DB side is an idempotent replace on
        job_id — but this avoids a redundant round-trip)."""
        if self._flushed:
            return
        self._flushed = True
        latency_ms = int((time.monotonic() - self._started) * 1000)
        jobs_store.record_whole_turn_metric(
            self.job_id, self.user_id, self.lane,
            prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cache_miss_tokens=self.cache_miss_tokens,
            usage_reported_calls=self.usage_reported_calls,
            cache_reported_calls=self.cache_reported_calls,
            provider=self.provider, model=self.model,
            cache_route_fingerprint=self.cache_route_fingerprint,
            latency_ms=latency_ms, model_calls=self.model_calls, retries=self.retries,
            failed=failed, status=status)


async def _cap_data(store, action_type, *, api_key, runtime_token, params=None, enclave_sem=None) -> dict:
    """便宜预取一个 capability 的 data（无 LLM，用 enclave-auth 凭证）。失败退化为 {}——
    调用方（如 `_run_wake` 的 screen_watch screen_recent 预取）容忍空结果，不是必须成功
    的前提，模型自己会看到空 grounding context 并据此决定要不要再发 tool_call 补查。

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


def _max_seq(rows: list[dict], *, default: int) -> int:
    """Highest ``seq`` among ``rows`` (coalesced messages carry it — see
    ``coalesce.coalesce_pending``), or ``default`` when none carries one (pure
    ts-only test inputs). Used to advance the DURABLE reply cursor to the max
    answered seq; never regresses below ``default`` (the turn's start cursor)."""
    seqs = [int(r["seq"]) for r in rows if r.get("seq") is not None]
    return max(seqs) if seqs else default


async def _coalesce_inputs(
    deps: TurnDeps, user_id: str, since_seq: int, *, enclave_sem=None
) -> tuple[list[dict], int, float]:
    """Read/decrypt and coalesce inputs behind the durable seq boundary.

    Production uses ``read_messages_after_seq`` and validates every returned
    row's stable identity.  ``read_messages_since`` is the rollout-compatible
    seq-aware seam used by existing tests/callers; a one-argument reader is the
    final compatibility fallback and may return rows without seq.  All reader
    calls are enclave-bound and share the turn's semaphore.

    Returns ``(coalesced, max_seq, max_ts)``.  The seq is the only correctness
    cursor; max_ts is retained solely for rollback telemetry/compatibility.
    """
    strict_seq_reader = deps.read_messages_after_seq
    reader = strict_seq_reader or deps.read_messages_since

    async def _read():
        if reader is not None:
            return await asyncio.to_thread(reader, user_id, int(since_seq))
        return await asyncio.to_thread(deps.read_messages, user_id)

    if enclave_sem is not None:
        async with enclave_sem:
            messages = await _read()
    else:
        messages = await _read()
    user_rows = [
        row for row in messages
        if str(row.get("role") or "") in {"user", "human"}
    ]
    complete_seq_input = bool(user_rows) and all(
        row.get("seq") is not None for row in user_rows)
    if strict_seq_reader is not None or complete_seq_input:
        coalesced, seq_cursor = v2_coalesce.coalesce_pending(
            messages, since_seq=int(since_seq))
        ts_cursor = max(
            (float(row.get("ts") or 0.0) for row in coalesced),
            default=0.0,
        )
        return coalesced, int(seq_cursor), ts_cursor

    # Narrow compatibility path for old one-argument fakes that do not carry
    # a DB identity.  It cannot advance the durable seq cursor.
    coalesced, ts_cursor = v2_coalesce.coalesce_pending(messages, since_ts=-1.0)
    return coalesced, _max_seq(coalesced, default=since_seq), float(ts_cursor)


def _make_fold_new_messages(
    user_id: str, deps: TurnDeps, cursor_box: dict, enclave_sem: "asyncio.Semaphore | None" = ENCLAVE_SEMAPHORE
) -> Callable[[], Awaitable[list[dict]]]:
    """Build the per-round message-fold closure `tool_loop.run_tool_loop` calls before every
    provider round after the first (spec C7 / C6 wiring; Global Constraints "per-round fold,
    no debounce/restart").

    MUST reuse the SAME enclave-decrypt path the turn's own coalesce step uses
    (`_coalesce_inputs`'s `_read()` inner: `deps.read_messages_since` when wired, else
    `deps.read_messages`) — NOT `db.chat_messages_after_seq`. Production `chat_messages`
    rows for real users are E2E envelopes (`body_ct`/`K_enclave`); the raw DB layer never
    sees plaintext, only the enclave-bound reader does (see `serve_worker._read_messages`,
    which decrypts each row via `/v1/envelope/decrypt` before returning it). Reading the raw
    DB doc here would feed CIPHERTEXT straight into the model prompt for every real user —
    this closure exists specifically to avoid that, so it must call the injected reader, the
    same as the turn's initial coalesce.

    ASYNC + enclave_sem-gated (BUG-2 fix): the reader is a synchronous, enclave-bound HTTP
    decrypt call — exactly like `_coalesce_inputs`'s own read. `_coalesce_inputs` already
    wraps that identical call in `async with enclave_sem` + `asyncio.to_thread` so it neither
    blocks the event loop thread nor bypasses the shared enclave concurrency gate (spec §11
    R3). This closure must do the same: it is called once per round by
    `tool_loop.run_tool_loop`, which `await`s it, so calling the sync reader directly here
    (no thread offload, no semaphore) would block the loop thread for the enclave round-trip
    AND let concurrent workers hit the single-threaded enclave ungated. `enclave_sem` defaults
    to the module-level `ENCLAVE_SEMAPHORE` (the same shared gate `process_job` uses); tests
    that want to exercise the closure with no gating pass `enclave_sem=None` (mirrors
    `_coalesce_inputs`/`_cap_data`'s own `enclave_sem is None` no-gate tolerance).

    `cursor_box` is a mutable `{"seq": int, "ts": float}` dict the caller owns and shares with
    this closure by reference — `run_tool_loop` holds no cursor state itself, it only calls
    the closure — so repeated `fold_new_messages()` calls across rounds advance the SAME
    **seq** cursor in place. Callers seed it with the max seq the turn's own initial coalesce
    already answered (see `_coalesce_inputs`), so the first fold call only sees messages with
    `seq >` that — the injected seq reader (`serve_worker._read_messages(user_id, after_seq)`)
    does the `seq > cursor` filter at the DB layer.

    Merge/filter goes through `coalesce.coalesce_pending(rows, since_seq=...)` on the strict
    production path (the reader also bounds rows by seq), leaving only the user-role filter,
    id-dedupe and empty-content drop. The cursor advances to `max(cursor_box["seq"], max seq
    folded)` after every call and never regresses (seq is a monotonic identity column, so a
    later message always has a strictly greater seq — unlike wall-clock ts, where a same-ts
    tie or a late-arriving earlier-ts message could strand a message below the boundary
    forever; that ts fragility is exactly the D5 no-reply bug this seq wiring fixes).
    """

    async def fold_new_messages() -> list[dict]:
        seq_native = "seq" in cursor_box
        if seq_native:
            seq_reader = deps.read_messages_after_seq or deps.read_messages_since
            if seq_reader is not None:
                reader = seq_reader
                args = (user_id, int(cursor_box["seq"]))
            else:
                # Compatibility for old one-argument injected readers.  Real
                # production turns always take the strict branch above.
                reader = deps.read_messages
                args = (user_id,)
        else:
            reader = deps.read_messages_since if deps.read_messages_since is not None else deps.read_messages
            args = (user_id, cursor_box["ts"]) if deps.read_messages_since is not None else (user_id,)

        async def _read():
            return await asyncio.to_thread(reader, *args)

        if enclave_sem is not None:
            async with enclave_sem:
                rows = await _read()
        else:
            rows = await _read()
        if seq_native:
            user_rows = [
                row for row in rows
                if str(row.get("role") or "") in {"user", "human"}
            ]
            has_seq = bool(user_rows) and all(
                row.get("seq") is not None
                for row in user_rows
            )
            if deps.read_messages_after_seq is not None or has_seq:
                coalesced, cursor = v2_coalesce.coalesce_pending(
                    rows, since_seq=int(cursor_box["seq"]))
                if cursor:
                    cursor_box["seq"] = max(cursor_box["seq"], int(cursor))
                ts_cursor = max(
                    (float(row.get("ts") or 0.0) for row in coalesced),
                    default=0.0,
                )
            else:
                # Old one-argument fakes may not expose DB seq. Keep their
                # timestamp fold behavior without weakening strict production.
                coalesced, ts_cursor = v2_coalesce.coalesce_pending(
                    rows, since_ts=float(cursor_box.get("ts", -1.0)))
            if ts_cursor > cursor_box.get("ts", 0.0):
                cursor_box["ts"] = ts_cursor
        else:
            coalesced, cursor = v2_coalesce.coalesce_pending(
                rows, since_ts=cursor_box["ts"])
            if cursor:
                cursor_box["ts"] = max(cursor_box["ts"], float(cursor))
        return coalesced

    return fold_new_messages


def _make_build_messages_fn(
    *, system_prompt: str, summary: str, tail: list[dict], extra_context: str = ""
) -> Callable[[list], list]:
    """Build the fixed base prompt plus the loop's chronological native transcript.

    `system_prompt`/`summary`/`tail` are the turn's base context — captured once at loop
    entry (D1: the caller already resolved these once via `deps.read_summary`/`deps.read_tail`
    before starting the loop).  The transcript then contains, in arrival order:

    * newly folded user messages; and
    * ``ToolExchange`` objects holding the exact provider-native assistant tool-call
      payload followed by call-id-matched results.

    Tool observations must not be flattened into a system prompt.  Doing that drops
    tool-call ids, loses Gemini thought signatures, and makes the second request an
    invalid native tool conversation on every supported wire.

    `extra_context` (optional, spec C8) is STATIC grounding resolved once before the
    loop starts (e.g. the wake lane's `screen_recent` prefetch for the `screen_watch` lane,
    rendered via `context.action_context_str`). It remains in the base system context;
    dynamic tool results remain native exchanges.
    """

    base_messages = context.build_turn_messages(
        system_prompt=system_prompt,
        summary=summary,
        tail=tail,
        action_context=extra_context,
    )

    def build_messages(transcript: list) -> list:
        rendered: list = []
        for item in transcript:
            if isinstance(item, ToolExchange):
                rendered.append(item)
                continue
            content = item.get("content")
            if context._has_payload(content):
                rendered.append({"role": "user", "content": content})
        return list(base_messages) + rendered

    return build_messages


def _memory_tool_actions(raw_actions) -> list[dict]:
    """Translate the model's PLAINTEXT memory_write actions (tool_schema:
    ``{op, summary, content, bucket, target_id}``) into the server-side
    memory-action shape ``memory.actions._execute_memory_action`` accepts —
    ``{"type": "memory.add"|"memory.supersede"|"memory.delete", "memory":
    {summary, content, bucket, threads}, ...}`` — WITHOUT an ``envelope`` so the
    plaintext write path (`memory.actions._memory_add_action`) builds the E2E
    envelope server-side (the model can never build one). Mirrors the inner shape
    ``extraction._inner_from_card`` uses. Before this, the raw model actions went
    straight to ``memory_core.actions`` and were rejected with title_required/400,
    so every memory_write tool turn died with turn_failed:runtimeerror."""
    out: list[dict] = []
    for a in raw_actions or []:
        if not isinstance(a, dict):
            continue
        op = str(a.get("op") or a.get("action") or a.get("type") or "add").strip().lower()
        op = op.replace("memory.", "")
        nested = a.get("memory") if isinstance(a.get("memory"), dict) else {}
        summary = str(
            a.get("summary") or a.get("title")
            or nested.get("summary") or nested.get("title") or ""
        ).strip()
        content = str(
            a.get("content") or a.get("description") or a.get("text")
            or nested.get("content") or nested.get("description")
            or nested.get("text") or summary
        ).strip()
        if not summary:
            summary = content[:80]
        target = str(
            a.get("target_id") or a.get("id") or a.get("supersedes")
            or a.get("memory_id") or ""
        ).strip()
        if op in ("delete", "remove") and target:
            out.append({"type": "memory.delete", "memory_id": target})
            continue
        inner = {
            "summary": summary,
            "content": content or summary,
            "bucket": str(a.get("bucket") or nested.get("bucket") or "").strip(),
            "threads": (
                list(a.get("threads") or [])
                if isinstance(a.get("threads"), list)
                else list(nested.get("threads") or [])
                if isinstance(nested.get("threads"), list)
                else []
            ),
        }
        base = {"reason": "Written by the agent via the memory_write tool.",
                "capture_mode": "agent_tool"}
        if op in ("update", "supersede", "merge", "patch") and target:
            out.append({"type": "memory.supersede", "supersedes": target, "memory": inner, **base})
        else:
            out.append({"type": "memory.add", "memory": inner, **base})
    return out


def _write_tool_effect_payload(tc) -> tuple[str, dict]:
    """Map a WRITE_ACTIONS tool_call to its PR A `(effect_type, payload)` (spec C6). ONE
    definition shared by every lane's `enqueue_write_effect` closure (`process_job`'s chat
    branch — Task 7 — and `_run_wake` — Task 8) so the write-tool -> effect_type mapping never
    drifts between lanes. `cap_registry.WRITE_ACTIONS` is the closed set
    `executor.dispatch_tool_calls` ever routes here — a new write capability shipping without a
    mapping below must fail loudly, not silently drop the write."""
    if tc.name == "memory_write":
        return "memory", {"actions": _memory_tool_actions(tc.args.get("actions"))}
    if tc.name == "identity_patch":
        # Preserve every advertised schema form.  Collapsing to only ``patch``
        # silently discarded top-level self_introduction/signature calls.
        return "identity", dict(tc.args)
    if tc.name in ("schedule_wake", "cancel_wake"):
        # Keep the trusted operation authoritative even if a future dispatcher
        # accidentally weakens top-level unknown-field rejection.
        return "schedule", {**tc.args, "op": tc.name}
    raise ValueError(f"no effect mapping for write tool {tc.name!r}")


# Version the durable type as well as the payload wrapper.  A pre-encryption
# worker overlapping a deploy does not recognize these names, so it leaves the
# row pending instead of interpreting the ciphertext wrapper as an empty legacy
# schedule/memory payload and marking the write applied.
ENCRYPTED_TOOL_EFFECT_TYPES = {
    "memory": "memory_encrypted_v1",
    "identity": "identity_encrypted_v1",
    "schedule": "schedule_encrypted_v1",
}


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


def _build_encrypted_reply_effect_payload(
    store,
    text: str,
    *,
    effect_id: str,
    reply_through_seq: int | None = None,
) -> dict:
    """Encrypt reply content before it enters the durable outbox.

    The envelope item id is a stable 16-byte hex digest of the deterministic
    effect id.  Retries therefore target the same chat row, while the outbox
    stores ciphertext only.  A final chat reply additionally carries the exact
    consumed input seq for the sink's atomic reply+cursor transaction.
    """
    item_id = hashlib.sha256(effect_id.encode("utf-8")).hexdigest()[:32]
    envelope, error = core_envelope._build_shared_envelope_for_store(
        store, str(text).encode("utf-8"), item_id=item_id)
    if envelope is None:
        raise RuntimeError(error or "reply envelope build failed")
    payload: dict = {"envelope": envelope}
    if reply_through_seq is not None:
        seq = int(reply_through_seq)
        if seq < 0:
            raise ValueError("reply_through_seq must be >= 0")
        payload["reply_through_seq"] = seq
    return payload


def _tool_effect_item_id(effect_id: str) -> str:
    """Return the row-bound envelope id for a deterministic tool effect."""
    return hashlib.sha256(
        f"v2-tool-effect:{effect_id}".encode("utf-8")
    ).hexdigest()[:32]


def _build_encrypted_tool_effect_payload(
    store,
    payload: dict,
    *,
    effect_id: str,
) -> dict:
    """Encrypt a model-authored write payload before durable outbox storage.

    Memory text, identity patches, and scheduled-wake reasons are conversation
    content.  Persisting those arguments as plain JSON in ``v2_effect_outbox``
    would bypass the same at-rest boundary enforced for chat and summaries.
    Canonical JSON plus a deterministic, domain-separated envelope id keeps a
    retry of the same deterministic effect byte-for-byte addressable without
    exposing the payload to Postgres.
    """
    if not isinstance(payload, dict):
        raise TypeError("tool effect payload must be a dict")
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    item_id = _tool_effect_item_id(effect_id)
    envelope, error = core_envelope._build_shared_envelope_for_store(
        store,
        plaintext,
        item_id=item_id,
    )
    if envelope is None:
        raise RuntimeError(error or "tool effect envelope build failed")
    return {"effect_envelope": envelope}


def _write_encrypted_reply_effect(
    store,
    envelope: dict,
    *,
    reply_through_seq: int = 0,
) -> dict:
    """Naturally-idempotent sink for encrypted V2 reply effects."""
    row = store.append_chat(
        "openclaw",
        "model_api",
        envelope,
        strict=True,
        reply_through_seq=int(reply_through_seq),
    )
    store.notify_chat_waiters()
    return row


def _emit_status(user_id, job_id, kind: str) -> None:
    """落一条顶层阶段性 status 事件（processing/writing_reply/done），复用
    status_stream.redact_status 拿到统一的标签——不在本模块里重复维护中文文案。"""
    ev = status_stream.redact_status(kind)
    jobs_store.append_status_event(user_id, ev["kind"], job_id=job_id, label=ev["label"], detail=ev["detail"])


def _surface_terminal_error(deps: TurnDeps, user_id: str, job_id, message: str) -> None:
    """Promptly reconcile the durable terminal-failure visibility marker.

    ``jobs_store.mark_failed`` and the timeout reaper transactionally enqueue
    the marker with the terminal job transition, so this call is only the
    low-latency attempt—not the correctness boundary.  The independent reaper
    retries both sinks after a crash or transient failure.  ``ensure`` also
    covers post-completion effect-delivery uncertainty, whose job correctly
    remains completed but still needs the same user-visible error surfaces.
    """
    try:
        jobs_store.ensure_terminal_failure_outbox(job_id, user_id, message)
        jobs_store.reconcile_terminal_failure_outbox(
            record_terminal_error=deps.record_terminal_error,
            job_id=job_id,
        )
    except Exception as e:  # noqa: BLE001 — durable marker remains retryable
        log.warning(
            "[v2.worker] job %s terminal visibility attempt failed code=%s",
            job_id,
            type(e).__name__.lower(),
        )


def _compaction_message_chars(message: dict) -> int:
    """Rendered size of one row in ``compaction._render_old_messages``.

    Keep this deliberately in lock-step with that renderer (``role: content``
    plus one line separator).  It is a conservative one-character overcount
    for the final row, which is preferable to letting a batch exceed its
    configured request budget.
    """
    return len(str(message.get("role") or "")) + 2 + len(
        str(message.get("content") or "")) + 1


def _bounded_compaction_prefix(
    messages: list[dict],
    *,
    max_messages: int | None = None,
    max_chars: int | None = None,
) -> list[dict]:
    """Return the oldest contiguous prefix within both compaction budgets.

    Watermarks may only advance over a contiguous oldest-first prefix.  Never
    skip an oversized first row to fit later rows: doing so would make the
    summary's seq coverage dishonest.  Instead fail loudly and leave the
    watermark untouched for diagnosis/retry.
    """
    max_messages = _COMPACTION_BATCH if max_messages is None else int(max_messages)
    max_chars = _COMPACTION_BATCH_CHARS if max_chars is None else int(max_chars)
    out: list[dict] = []
    used = 0
    for message in messages[:max_messages]:
        size = _compaction_message_chars(message)
        if size > max_chars and not out:
            raise ValueError("compaction_message_exceeds_char_budget")
        if used + size > max_chars:
            break
        out.append(message)
        used += size
    return out


async def _compaction_llm_with_progress(*args: Any, **kwargs: Any) -> Any:
    """Reliable compaction provider call with per-attempt stall heartbeats."""
    def _attempt_progress(stage: str, attempt: int) -> None:
        _report_turn_progress(f"compaction_provider_{stage}_{attempt}")

    return await provider_client.reliable_chat_completion_async(
        *args, progress_cb=_attempt_progress, **kwargs)


async def _run_compaction(
    job_id, user_id: str, deps: TurnDeps, provider_config: Any,
    enclave_sem: "asyncio.Semaphore", claimed_by: str | None = None,
    tm: "TurnMetrics | None" = None,
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
            if (
                deps.read_summary_with_seq is not None
                and (deps.read_compaction_tail_after_seq is not None
                     or deps.read_tail_after_seq is not None)
            ):
                summary, _watermark_ts, version, watermark_seq = await asyncio.to_thread(
                    deps.read_summary_with_seq, user_id)
                reader = deps.read_compaction_tail_after_seq or deps.read_tail_after_seq
                tail = await asyncio.to_thread(
                    reader, user_id, watermark_seq, _COMPACTION_BATCH + _TAIL_KEEP)
            else:
                summary, watermark, version = await asyncio.to_thread(deps.read_summary, user_id)
                reader = deps.read_compaction_tail or deps.read_tail
                tail = await asyncio.to_thread(
                    reader, user_id, watermark, _COMPACTION_BATCH + _TAIL_KEEP)
        if len(tail) <= _TAIL_KEEP:
            # No compaction call made at all (tail already under budget) — a
            # legitimate model_calls=0 success.
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            if tm is not None:
                tm.flush(failed=False, status="ok")
            return "completed"
        old = _bounded_compaction_prefix(
            tail[: max(0, len(tail) - _TAIL_KEEP)])
        if not old:
            raise RuntimeError("compaction_batch_empty")
        new_watermark = old[-1]["ts"]
        # D5/Task 9: also advance the seq watermark, atomically, in the same CAS
        # write as new_watermark below. The tail row dict itself may already carry
        # "seq" (a seq-aware reader); if not (e.g. today's ts-windowed
        # _read_compaction_tail, or a narrow test double), fall back to an exact
        # by-id lookup — never a ts-range estimate, which would be ambiguous
        # under same-ts ties (see db.chat_seq_for_msg_id's docstring). A tail row
        # with no "id" at all (only synthetic test doubles) leaves the seq
        # watermark unadvanced this round rather than guessing.
        new_watermark_seq = old[-1].get("seq")
        if new_watermark_seq is None:
            last_id = old[-1].get("id")
            if last_id is not None:
                new_watermark_seq = await asyncio.to_thread(
                    db.chat_seq_for_msg_id, user_id, last_id)
        _report_turn_progress("compaction_batch_start")
        new_summary = await v2_compaction.compact(
            provider_config=provider_config, current_summary=summary, old_messages=old,
            llm=_compaction_llm_with_progress,
            usage_out=tm.add_call if tm is not None else None)
        _report_turn_progress("compaction_batch_complete")
        if new_summary.strip() == summary.strip():  # 空/no-op 折叠 → 不推进 watermark/version
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            if tm is not None:
                tm.flush(failed=False, status="ok")
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
        # watermark_seq 只在算出来时才多传一个位置参数（见上）——narrow-signature 的旧
        # fake（不接这个参数）走的正是这条 4 参分支，不受影响。
        if new_watermark_seq is not None:
            ok = await asyncio.to_thread(
                deps.write_summary, user_id, new_summary, new_watermark, version, new_watermark_seq)
        else:
            ok = await asyncio.to_thread(
                deps.write_summary, user_id, new_summary, new_watermark, version)
        if ok:
            completed = await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            # A char-limited batch can be smaller than the message-count cap.
            # Requeue whenever this snapshot still has more than the verbatim
            # keep-tail after the rows we just folded, not only when the reader
            # happened to hit its count limit.
            if completed and (
                len(tail) >= _COMPACTION_BATCH + _TAIL_KEEP
                or len(tail) - len(old) > _TAIL_KEEP
            ):
                await asyncio.to_thread(
                    jobs_store.enqueue_job, user_id, "maintenance", reason="compaction_catchup")
                await asyncio.to_thread(core_wake_bus.notify, "v2_jobs", user_id)
            if tm is not None:
                tm.flush(failed=False, status="ok")
            return "completed"
        # CAS 没落地：别的写手已经推进过版本，本次压缩的 batch 作废——但 tail 是否
        # 仍然超预算跟这次 CAS 输赢无关，绝不能静默放弃，否则超预算 tail 永久堆积、
        # 加密开销/上下文预算一路涨上去没人再折。跟成功路径 (:674-677) 同一个
        # "maintenance" lane + enqueue_job 的 per-user 单飞 coalesce（本来就防
        # 重试风暴），reason 换成 cas_lost_retry 便于跟正常的 catch-up 区分。下一次
        # 尝试会重新从（未被本次推进的）watermark 读 summary/tail，不复用这次算出
        # 的、已经作废的 batch。
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, "summary_cas_lost", claimed_by=claimed_by)
        await asyncio.to_thread(
            jobs_store.enqueue_job, user_id, "maintenance", reason="cas_lost_retry")
        await asyncio.to_thread(core_wake_bus.notify, "v2_jobs", user_id)
        if tm is not None:
            tm.flush(failed=True, status="summary_cas_lost")
        return "failed"
    except Exception as e:  # noqa: BLE001 — 后台 job：静默 mark_failed，绝不弹用户可见 error/写气泡
        code = _safe_failure_code("compaction_failed", e)
        log.warning("[v2.worker] compaction job %s failed code=%s", job_id, code)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, code,
            claimed_by=claimed_by)
        if tm is not None:
            tm.flush(failed=True, status=code)
        return "failed"


def _gap_from_count(unsummarized_count: int, tail_limit: int) -> bool:
    """Pure/no-I/O core of D6 gap detection: True when THIS USER has more
    unsummarized messages (``seq > watermark_seq``) than the bounded tail
    window can hold. The tail keeps only the newest ``tail_limit`` of them
    (``serve_worker._read_tail_window``'s ``candidates[-limit:]`` slice) —
    anything beyond that count is neither folded into the summary nor inside
    the tail: D6's silent-drop hole.

    COUNT-based, NOT seq-arithmetic. ``chat_messages.seq`` is a TABLE-WIDE
    ``BIGINT GENERATED ALWAYS AS IDENTITY`` counter shared by every user (see
    migration 0001_baseline.py) — in production, other users' concurrent
    inserts interleave with this user's, so the raw seq SPAN since the
    watermark (``max_seq - watermark_seq``, the OLD buggy test — ``max_seq``
    being the GLOBAL max across all users) has no fixed relationship to how
    many of THIS user's own messages actually sit in that span: it can be
    huge (mostly other users' rows) even when this user has only a handful
    unsummarized. That produced a FALSE gap on ~every multi-user turn — every
    turn ran a needless synchronous BYOK catch-up compaction, collapsing the
    verbatim tail to empty and occasionally failing the turn outright
    (``prompt_coverage_incomplete`` after 3 no-op-fold retries). The fix:
    compare THIS USER's own row count (``db.count_messages_after_seq``,
    scoped by ``user_id``) against ``tail_limit`` directly — ``max_seq <= 0``
    no longer needs a special case, since a user with zero rows past the
    watermark has ``unsummarized_count == 0 <= tail_limit`` naturally."""
    return unsummarized_count > tail_limit


async def _unsummarized_count(user_id: str, watermark_seq: int) -> int:
    """THIS USER's own ``chat_messages`` row count with ``seq > watermark_seq``
    — one cheap indexed ``COUNT(*)`` (see ``db.count_messages_after_seq``),
    scoped by ``user_id``. Replaces the old ``max_seq - watermark_seq``
    global-seq-span estimate — see ``_gap_from_count``'s docstring for why
    that was wrong."""
    return await asyncio.to_thread(db.count_messages_after_seq, user_id, watermark_seq)


async def _prompt_coverage_gap(user_id: str, *, watermark_seq: int, tail_limit: int) -> bool:
    """D6 gap check: fetch THIS USER's own unsummarized row count and run it
    through the pure ``_gap_from_count`` core. One indexed COUNT query on the
    fast (overwhelmingly common) no-gap path — no enclave/decrypt work, no
    LLM call, no compaction."""
    count = await _unsummarized_count(user_id, watermark_seq)
    return _gap_from_count(count, tail_limit)


async def _assert_prompt_covers_seq(user_id: str, *, watermark_seq: int, tail_limit: int) -> None:
    """D6 hard invariant, count-based (see ``_prompt_coverage_gap``): raises
    ``TurnError`` if a coverage hole would remain — see
    ``_assert_prompt_covers`` (the wrapper actually wired into the two call
    sites, which additionally re-derives ``watermark_seq`` fresh) for why
    raising, not truncating/ignoring, is the deliberate choice here."""
    if await _prompt_coverage_gap(user_id, watermark_seq=watermark_seq, tail_limit=tail_limit):
        raise TurnError("prompt_coverage_incomplete")


async def _assert_prompt_covers(user_id: str, tail_limit: int) -> None:
    """Post-assembly hard assertion (D6/Task 10): every message with
    ``seq > watermark_seq`` must be inside the tail window this turn is about
    to hand the model. Re-derives ``watermark_seq`` fresh from the DB
    (``jobs_store.get_summary_row``) rather than reusing
    ``_ensure_prompt_coverage``'s return value — this is deliberately an
    INDEPENDENT re-check, not a cache read, so it still catches a future
    wiring bug (e.g. the tail read moved ahead of the coverage call, or the
    coverage call got dropped from a call site entirely) instead of silently
    trusting whatever the earlier call computed. One extra cheap indexed
    COUNT read per turn (see ``_prompt_coverage_gap``) — negligible next to
    the LLM/enclave work already in the same turn.

    Raising here (rather than truncating the tail or proceeding) is
    deliberate: reaching a real gap at THIS point means
    ``_ensure_prompt_coverage`` either wasn't called before this, or its
    catch-up didn't actually land — a bug, not a recoverable runtime
    condition — so the turn must fail loudly (``mark_failed``, requeue-able)
    instead of silently shipping a prompt with a known coverage hole, which
    is exactly the silent-drop bug this task exists to close."""
    summary_row = await asyncio.to_thread(jobs_store.get_summary_row, user_id)
    watermark_seq = int(summary_row["watermark_seq"]) if summary_row else 0
    await _assert_prompt_covers_seq(user_id, watermark_seq=watermark_seq, tail_limit=tail_limit)


async def _assert_prompt_tail_exact(
    user_id: str,
    *,
    watermark_seq: int,
    through_seq: int,
    tail: list[dict],
) -> None:
    """Assert exact prompt membership against one race-bounded DB snapshot.

    Count-only checks can pass with the wrong rows.  This compares the ordered
    seq identities the prompt actually contains with every stored row in
    ``watermark_seq < seq <= through_seq``.  Messages committed after the
    snapshot belong to the next round-boundary fold and cannot displace an older
    row or create a false failure.
    """
    try:
        actual = [int(row["seq"]) for row in tail]
    except (KeyError, TypeError, ValueError) as exc:
        raise TurnError("prompt_coverage_incomplete") from exc
    expected = await asyncio.to_thread(
        db.chat_seqs_after_seq,
        user_id,
        int(watermark_seq),
        through_seq=int(through_seq),
    )
    if actual != expected:
        raise TurnError("prompt_coverage_incomplete")


async def _ensure_prompt_coverage(
    user_id: str,
    deps: TurnDeps,
    *,
    provider_config: Any,
    enclave_sem: "asyncio.Semaphore",
    tail_limit: int,
    max_retries: int = 3,
    job_id: Any | None = None,
    claimed_by: str | None = None,
    catchup_deadline_sec: float | None = None,
    add_usage: Callable[[dict | None], None] | None = None,
) -> tuple[int, int]:
    """D6 (Task 10): close a compaction backlog gap BEFORE assembling a turn's
    prompt. Today's bug: ``_read_tail`` only ever returns the newest
    ``tail_limit`` messages after the summary watermark (``result[-limit:]``,
    see ``serve_worker._read_tail_window``) — if compaction has fallen more
    than ``tail_limit`` messages behind, the messages strictly between the
    watermark and the tail's start seq are SILENTLY DROPPED: not summarized
    (compaction hasn't reached them yet) and not in the tail (the
    newest-``tail_limit`` slice excludes them). This closes that hole with a
    SYNCHRONOUS catch-up compaction run inline, before the caller reads the
    actual prompt content — unlike the existing best-effort background
    maintenance enqueue (``process_job``'s ``needs_compaction`` check), which
    only fires AFTER a reply has already gone out on a tail that had the hole.

    Gap math (COUNT-based on THIS USER's own rows — see ``_gap_from_count``/
    ``_prompt_coverage_gap``, no enclave/decrypt work on the fast path):
    ``watermark_seq`` comes from ``jobs_store.get_summary_row`` (already
    performs the D5/Task 9 ts->seq back-compat translation for pre-migration
    rows — that function's own docstring names this exact call site).
    ``unsummarized_count`` is ``db.count_messages_after_seq(user_id,
    watermark_seq)`` — deliberately NOT ``db.chat_max_seq`` seq-arithmetic
    (``chat_messages.seq`` is a table-wide identity counter shared by every
    user; other users' interleaved inserts make a global-seq span meaningless
    as a per-user count — see ``_gap_from_count``'s docstring for the full
    writeup of the bug this replaced). No gap is the overwhelmingly common
    case and costs exactly two cheap indexed reads (summary row + COUNT) — no
    tail decrypt, no LLM call, no compaction.

    Gap found: run as many bounded inline catch-up batches as are needed to
    cover ONLY the pre-tail gap. Each batch is the oldest contiguous prefix,
    capped by both ``_COMPACTION_BATCH`` messages and
    ``_COMPACTION_BATCH_CHARS`` rendered characters — reuses
    ``deps.read_compaction_tail``/``_read_compaction_tail``'s
    oldest-first-from-watermark contract, exactly like ``_run_compaction``'s
    periodic fold. Each batch goes through ``v2_compaction.compact``, then
    CAS-writes the advanced summary via ``deps.write_summary`` (watermark_ts
    AND watermark_seq advance atomically in the same CAS row, per Task 9),
    then loops to re-check from a fresh read. Looping (rather than assuming the
    CAS landed) handles two real races: (a) CAS loss — a concurrently running
    periodic maintenance job or another turn for this user advanced the
    summary first; re-reading at the top of the next iteration picks up
    whatever version won, which may already close the gap with zero extra
    compaction work; (b) a genuine no-op fold (``compact`` returns unchanged
    text, e.g. an empty/failed LLM reply) never advances the watermark and
    must not spin forever. ``max_retries`` therefore bounds CONSECUTIVE
    no-progress/CAS-loss attempts, and resets whenever a fresh read observes
    the watermark advance; it does not cap successful batches. The whole
    catch-up is additionally bounded by ``catchup_deadline_sec`` (default
    ``_PROMPT_CATCHUP_DEADLINE_SEC``). Production callers pass ``job_id`` and
    ``claimed_by`` so the active lease is renewed between batches.

    Bounded-retry exhaustion is NOT a silent pass-through: raises
    ``TurnError("prompt_coverage_incomplete")`` so the turn
    fails visibly (``mark_failed``, requeue-able) rather than shipping a
    prompt with a known coverage hole. ``deps.read_summary``/
    ``deps.read_compaction_tail``-or-``deps.read_tail``/``deps.write_summary``
    being unwired (``None`` — older/minimal test deps) is likewise NOT
    treated as "can't happen here": it raises the same way on the first gap
    found, since without those readers this function has no way to close it.

    Returns ``(watermark_seq, max_seq)`` as of the last (successful, no-gap)
    check purely for callers that want it cheaply (``max_seq`` is
    ``db.chat_max_seq`` — a GLOBAL anchor, informational only, no longer part
    of the gap math itself); the actual hard invariant enforcement after the
    real tail read is ``_assert_prompt_covers``, which re-derives its own
    fresh state rather than trusting this return value.
    """
    deadline_sec = (
        _PROMPT_CATCHUP_DEADLINE_SEC
        if catchup_deadline_sec is None
        else float(catchup_deadline_sec)
    )
    if not math.isfinite(deadline_sec) or deadline_sec <= 0:
        raise TurnError("prompt_coverage_incomplete")
    deadline_at = time.monotonic() + deadline_sec

    def _remaining() -> float:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TurnError("prompt_coverage_incomplete")
        return remaining

    async def _within_deadline(start: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await asyncio.wait_for(start(), timeout=_remaining())
        except asyncio.TimeoutError as exc:
            raise TurnError(
                "prompt_coverage_incomplete") from exc

    async def _renew_catchup_lease() -> None:
        if job_id is None or not claimed_by:
            return
        if not await _within_deadline(
            lambda: asyncio.to_thread(
                jobs_store.renew_job_lease,
                job_id,
                claimed_by,
                ttl_sec=jobs_store.RUNNING_TTL_SEC,
            )
        ):
            raise LostJobLease("job lease lost during prompt catch-up")

    # Successful batches may be arbitrarily numerous.  Only attempts after
    # which a fresh DB read observes no watermark movement consume this
    # budget.  This distinguishes a healthy large backlog from a stuck/no-op
    # compactor or repeated CAS loss.
    no_progress_attempts = 0
    last_observed_watermark: int | None = None
    attempted_since_observation = False
    while True:
        _remaining()
        summary_row = await _within_deadline(
            lambda: asyncio.to_thread(jobs_store.get_summary_row, user_id))
        watermark_seq = int(summary_row["watermark_seq"]) if summary_row else 0
        if last_observed_watermark is not None:
            if watermark_seq > last_observed_watermark:
                no_progress_attempts = 0
            elif attempted_since_observation:
                no_progress_attempts += 1
        last_observed_watermark = watermark_seq
        attempted_since_observation = False

        unsummarized_count = await _within_deadline(
            lambda: _unsummarized_count(user_id, watermark_seq))
        if not _gap_from_count(unsummarized_count, tail_limit):
            max_seq = await _within_deadline(
                lambda: asyncio.to_thread(db.chat_max_seq, user_id))
            return watermark_seq, max_seq  # no gap — the common fast path
        seq_callbacks = (
            deps.read_summary_with_seq is not None
            and (deps.read_compaction_tail_after_seq is not None
                 or deps.read_tail_after_seq is not None)
        )
        legacy_callbacks = (
            deps.read_summary is not None
            and (deps.read_compaction_tail is not None or deps.read_tail is not None)
        )
        if no_progress_attempts >= max_retries or deps.write_summary is None or not (
            seq_callbacks or legacy_callbacks
        ):
            raise TurnError("prompt_coverage_incomplete")
        fold_count = min(unsummarized_count - tail_limit, _COMPACTION_BATCH)
        if fold_count <= 0:
            continue

        async def _read_gap():
            if seq_callbacks:
                summary_ = await asyncio.to_thread(deps.read_summary_with_seq, user_id)
                # A concurrent compactor advanced between the cheap metadata read
                # and summary decrypt. Re-loop from its winning watermark.
                if int(summary_[3]) != watermark_seq:
                    return summary_, [], True
                reader_ = deps.read_compaction_tail_after_seq or deps.read_tail_after_seq
                old_ = await asyncio.to_thread(
                    reader_, user_id, watermark_seq, fold_count)
                return summary_, old_, False

            summary_ = await asyncio.to_thread(deps.read_summary, user_id)
            reader_ = deps.read_compaction_tail or deps.read_tail
            # Cover only the rows that would otherwise fall before the verbatim
            # tail — not the tail itself. ``fold_count`` is the smaller of the
            # actual per-user gap and the message-count batch cap; the char cap
            # is applied after decrypting this already-bounded oldest prefix.
            old_ = await asyncio.to_thread(reader_, user_id, summary_[1], fold_count)
            return summary_, old_, False

        # enclave_sem may be None in tests that call this helper directly
        # (mirrors `_coalesce_inputs`/`_cap_data`'s own `enclave_sem is None`
        # no-gate tolerance) — production callers always pass the shared gate.
        async def _read_gap_gated():
            if enclave_sem is not None:
                async with enclave_sem:
                    return await _read_gap()
            return await _read_gap()

        summary_fields, old, raced = await _within_deadline(_read_gap_gated)
        if raced:
            continue
        summary, watermark_ts, version = summary_fields[:3]
        try:
            old = _bounded_compaction_prefix(old)
        except ValueError as exc:
            raise TurnError(
                "prompt_coverage_incomplete") from exc
        if not old:
            # The count says a gap exists but the reader returned nothing
            # (e.g. a ts-windowed fake/reader whose window doesn't line up
            # with the real seq boundary) — looping again would just burn
            # retry budget for no benefit. Fail now rather than spin.
            raise TurnError("prompt_coverage_incomplete")
        attempted_since_observation = True
        _report_turn_progress("prompt_catchup_batch_start")
        new_summary = await _within_deadline(
            lambda: v2_compaction.compact(
                provider_config=provider_config,
                current_summary=summary,
                old_messages=old,
                llm=_compaction_llm_with_progress,
                usage_out=add_usage,
            )
        )
        _report_turn_progress("prompt_catchup_batch_complete")
        if new_summary.strip() == summary.strip():
            # Genuine no-op fold (empty/failed LLM reply — mirrors
            # `_run_compaction`'s identical guard). Do NOT advance the
            # watermark: these messages were NOT actually folded into the
            # summary text, so pretending they were covered would satisfy the
            # seq arithmetic while silently losing their content — exactly
            # the bug this task exists to close, just moved one layer down.
            # Leave the gap in place; the `while` loop's fresh watermark read
            # consumes one no-progress attempt and eventually raises.
            await _renew_catchup_lease()
            continue
        # Fence the summary CAS with current job ownership and refresh enough
        # lease for the following batch.  Renew BEFORE the write so a batch
        # whose lease expired during provider work cannot mutate coverage.
        await _renew_catchup_lease()
        new_watermark_ts = old[-1]["ts"]
        new_watermark_seq = old[-1].get("seq")
        if new_watermark_seq is None:
            last_id = old[-1].get("id")
            if last_id is not None:
                new_watermark_seq = await _within_deadline(
                    lambda: asyncio.to_thread(
                        db.chat_seq_for_msg_id, user_id, last_id))
        if seq_callbacks and new_watermark_seq is None:
            raise TurnError("prompt_coverage_incomplete")
        if new_watermark_seq is not None:
            await _within_deadline(
                lambda: asyncio.to_thread(
                    deps.write_summary, user_id, new_summary, new_watermark_ts,
                    version, new_watermark_seq))
        else:
            await _within_deadline(
                lambda: asyncio.to_thread(
                    deps.write_summary, user_id, new_summary, new_watermark_ts,
                    version))
        _report_turn_progress("prompt_catchup_watermark_write")
        # Loop: the top of `while` re-reads and re-checks against whatever
        # watermark actually landed (this write's, a concurrent writer's, or
        # unchanged on CAS loss/no-op fold).


async def _run_wake(
    job_id, user_id: str, lane: str, deps: TurnDeps, provider_config: Any,
    enclave_sem: "asyncio.Semaphore", claimed_by: str,
    tm: "TurnMetrics | None" = None,
) -> str:
    """wake-lane（heartbeat/scheduled/manual_wake/screen_watch）turn：让伴侣主动开口，而不是
    回答用户刚发的消息（用户根本没发消息——这就是唤醒的定义）。同 `_run_compaction` 一样自成
    一体、自己的 try/except：这是后台/主动发起的 job，provider 解析失败或任何未预期异常
    都静默 `mark_failed`，绝不 `_surface_terminal_error`、绝不写占位气泡。

    D3 Task 8 (PR C spec C8)：跟 chat 分支（`process_job`，Task 7）一样跑同一个
    `tool_loop.run_tool_loop`。`turn_authorization=True` 传给 `dispatch_tool_calls`（跟 chat
    传的值一样，语义是 wake_trigger 而不是 user——两者都在 `provenance.turn_has_write_
    authorization` 意义下"有资格授权写"）。跟 chat 分支的两点关键差异：
    - 不要求非空用户消息：`wake_tail` 恒含一条固定的 `_WAKE_NUDGE`（user-role），
      `build_messages` 因此永远至少有一条非 system 轮次。
    - 空回复合法："weak wake sleeps"（弱唤醒睡回去）：`_on_reply` 对空文本（无论
      intermediate 还是 terminal）直接 no-op，不入队 reply effect、不报错——跟 chat 分支
      "终态空文本 = no-filler 失败"的语义**相反**。循环正常跑完（`run_tool_loop` 不抛异常）
      即视为成功，`mark_completed`，只是没写出气泡。

    真 provider 错误（`chat_completion_async` 抛出的任何异常）：静默 `mark_failed`，同样
    不弹用户可见 error chip——背景 job，同 maintenance 的隔离口径。402/401/403 一类
    "provider_config"错误（死/欠费 BYOK key）额外写一条 payment_cooldown（D3 Task 7），
    让 scheduler 的 `due_heartbeat_users` 停止对一把修不好的钥匙反复重试。

    prompt 组装：读 summary+tail（同 chat 路径的 D1 读法）+ 固定的 `_WAKE_NUDGE`。
    `system_prompt`：`_SCREEN_WATCH_SYSTEM_PROMPT`（screen_watch lane）或
    `_WAKE_SYSTEM_PROMPT`（其余三条 wake lane）。screen_watch 的 `screen_recent` 预取
    结果通过 `_make_build_messages_fn` 的 `extra_context` 参数（复用
    `context.action_context_str` 的渲染）注入——它是回合开始时取一次的静态
    grounding，不随 tool-loop 轮次增长，跟 `prior_tool_results`（每轮动态积累的工具
    观测）是两回事。
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
            # D4 live kill switch: same RuntimeModeChanged fence as the runtime-mode
            # check below — _run_wake's outer `except Exception` mark_faileds/flushes
            # it silently (no user-visible error chip for a background wake job),
            # exactly like every other raise in this closure.
            if await asyncio.to_thread(kill_switch.turns_halted):
                raise RuntimeModeChanged(f"v2 turns halted before {effect}")
            if deps.runtime_mode_enabled is not None and not await asyncio.to_thread(
                deps.runtime_mode_enabled, user_id
            ):
                raise RuntimeModeChanged(f"user rolled back before {effect}")

        # D6/Task 10: close a compaction backlog gap BEFORE reading the actual
        # prompt content — see `_ensure_prompt_coverage`'s docstring. Only run
        # when BOTH readers are wired (mirrors the joint `read_summary is not
        # None and read_tail is not None` gate `process_job` uses below); a
        # coverage check is meaningless without a real tail reader to bound.
        seq_context = (
            deps.read_summary_with_seq is not None
            and deps.read_tail_after_seq is not None
        )
        legacy_context = deps.read_summary is not None and deps.read_tail is not None
        if seq_context or legacy_context:
            await _ensure_prompt_coverage(
                user_id, deps, provider_config=provider_config,
                enclave_sem=enclave_sem, tail_limit=_TAIL_BUDGET,
                job_id=job_id, claimed_by=claimed_by,
                add_usage=tm.add_call if tm is not None else None)
        async with enclave_sem:
            if seq_context:
                summary, _watermark_ts, _ver, watermark_seq = await asyncio.to_thread(
                    deps.read_summary_with_seq, user_id)
                wake_snapshot_seq = await asyncio.to_thread(db.chat_max_seq, user_id)
                tail = await asyncio.to_thread(
                    deps.read_tail_after_seq,
                    user_id,
                    watermark_seq,
                    _TAIL_BUDGET,
                    through_seq=wake_snapshot_seq,
                )
            elif legacy_context:
                summary, watermark, _ver = await asyncio.to_thread(deps.read_summary, user_id)
                tail = await asyncio.to_thread(deps.read_tail, user_id, watermark, _TAIL_BUDGET)
            else:
                summary, tail = "", []
            tail = await asyncio.to_thread(
                _inject_tail_images, tail, user_id=user_id, read_images=deps.read_images)
        if seq_context:
            await _assert_prompt_tail_exact(
                user_id,
                watermark_seq=watermark_seq,
                through_seq=wake_snapshot_seq,
                tail=tail,
            )
        elif legacy_context:
            # Post-assembly hard assertion (D6): independent re-derivation,
            # not a reuse of `_ensure_prompt_coverage`'s return — see
            # `_assert_prompt_covers`'s docstring for why.
            await _assert_prompt_covers(user_id, _TAIL_BUDGET)
        wake_tail = list(tail) + [{"role": "user", "content": _WAKE_NUDGE}]

        # Minted ONCE, unconditionally: cheap HMAC signing (not a decrypt — see
        # TurnDeps.mint_enclave_token's docstring), used both by the screen_watch
        # prefetch below and by every capability the tool loop's dispatcher may
        # invoke this turn (any wake lane can now read/write through the same
        # catalog chat uses, not just screen_watch's screen_recent prefetch).
        token = deps.mint_enclave_token(user_id)

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
            data = await _cap_data(
                store, "screen_recent", api_key=None, runtime_token=token,
                enclave_sem=enclave_sem)
            # _fold_action_results caps each action at _PER_ACTION_CHAR_CAP=2000 chars
            # (the multimodal round's anti-poisoning cap); captions fit.
            screen_results = {"screen_recent": [{"ok": True, "data": data}]}

        # Pin effects to the generation admitted/claimed for this job, never a
        # fresh read. A resident->v2 ABA during a long provider call can leave
        # state==v2 again at a boundary; reading "current" here would let the
        # old turn mint effects authorized by the new runtime generation.
        pinned_generation = await asyncio.to_thread(
            jobs_store.get_expected_runtime_generation,
            job_id,
            claimed_by=claimed_by,
        )
        gen = int(
            pinned_generation
            if pinned_generation is not None
            else await asyncio.to_thread(db.get_runtime_generation, user_id)
        )
        ordinal = itertools.count()

        async def _enqueue_write_effect(tc) -> None:
            logical_effect_type, payload = _write_tool_effect_payload(tc)
            effect_type = ENCRYPTED_TOOL_EFFECT_TYPES[logical_effect_type]
            ordinal_value = next(ordinal)
            effect_id = v2_effect_id.derive(
                job_id=job_id,
                effect_type=effect_type,
                ordinal=ordinal_value,
            )
            encrypted_payload = await asyncio.to_thread(
                _build_encrypted_tool_effect_payload,
                store,
                payload,
                effect_id=effect_id,
            )
            enqueued_id = await asyncio.to_thread(
                v2_effect_outbox.enqueue_effect,
                job_id=job_id,
                user_id=user_id,
                effect_type=effect_type,
                ordinal=ordinal_value,
                expected_generation=gen,
                payload=encrypted_payload,
            )
            if enqueued_id != effect_id:
                raise RuntimeError("tool effect id derivation mismatch")

        async def _before_write() -> None:
            await _fence_wake_effect("memory/identity/schedule write")

        async def _dispatch_tools(tool_calls):
            await _fence_wake_effect("tool dispatch")
            return await v2_executor.dispatch_tool_calls(
                tool_calls, store=store, api_key=None, runtime_token=token,
                enclave_sem=enclave_sem, turn_authorization=True,  # wake_trigger authorizes writes
                enqueue_write_effect=_enqueue_write_effect, before_write=_before_write,
                read_parallelism=MAX_READ_ACTION_PARALLELISM)

        async def _on_reply(text: str, *, final: bool) -> None:
            text = str(text or "").strip()
            if not text:
                # Silence is a legitimate wake outcome — both mid-loop (an empty
                # `reply{}` call) and terminal ("weak wake sleeps"): unlike the chat
                # lane, an empty terminal text is NOT a failure here, so this is a
                # plain no-op, never a raise.
                return
            # The provider call may have taken minutes. Re-check ownership at
            # the actual effect boundary rather than relying on the fence from
            # before the round began.
            await _fence_wake_effect("reply")
            ordinal_value = next(ordinal)
            payload = {"text": text}
            if deps.read_messages_after_seq is not None:
                eid = v2_effect_id.derive(
                    job_id=job_id, effect_type="reply", ordinal=ordinal_value)
                consumed_seq = None
                if final and cursor_box["seq"] > wake_start_seq:
                    consumed_seq = cursor_box["seq"]
                payload = await asyncio.to_thread(
                    _build_encrypted_reply_effect_payload,
                    store,
                    text,
                    effect_id=eid,
                    reply_through_seq=consumed_seq,
                )
            await asyncio.to_thread(
                v2_effect_outbox.enqueue_effect,
                job_id=job_id,
                user_id=user_id,
                effect_type="reply",
                ordinal=ordinal_value,
                expected_generation=gen,
                payload=payload,
            )
            # C6: drain immediately so an intermediate bubble is visible mid-loop.
            # Offloaded — the reply sink's enclave envelope round-trip must not
            # block the event loop thread (same reasoning as the chat `_on_reply`
            # below and the already-fixed per-round fold offload).
            if deps.apply_pending_effects is not None:
                await asyncio.to_thread(deps.apply_pending_effects, user_id)

        def _add_usage(usage) -> None:
            if tm is not None:
                tm.add_call(usage)

        # Snapshot the boundary at wake start. Production uses the same total-order
        # seq reader as chat; timestamp fallback remains only for narrow tests.
        if deps.read_messages_after_seq is not None:
            wake_start_seq = (
                wake_snapshot_seq if seq_context
                else await asyncio.to_thread(db.chat_max_seq, user_id)
            )
            cursor_box = {"seq": wake_start_seq, "ts": time.time()}
        else:
            wake_start_seq = 0
            cursor_box = {"ts": time.time()}
        fold_new_messages = _make_fold_new_messages(user_id, deps, cursor_box, enclave_sem=enclave_sem)
        build_messages = _make_build_messages_fn(
            system_prompt=(_SCREEN_WATCH_SYSTEM_PROMPT if lane == "screen_watch"
                           else _WAKE_SYSTEM_PROMPT),
            summary=summary, tail=wake_tail,
            extra_context=context.action_context_str(screen_results) if screen_results else "")

        await _fence_wake_effect("wake turn")
        try:
            await v2_tool_loop.run_tool_loop(
                provider_config=provider_config, build_messages=build_messages,
                dispatch_tools=_dispatch_tools, on_reply=_on_reply,
                fold_new_messages=fold_new_messages, add_usage=_add_usage,
                max_calls=_TURN_MAX_LLM_CALLS,
                fold_before_first=deps.read_messages_after_seq is not None,
                on_progress=_report_turn_progress)
        except Exception as e:  # noqa: BLE001 — classify below, then let it fall to the outer silent mark_failed
            if provider_client.classify_provider_error(e) == "provider_config":
                # Dead/broke BYOK key (402 out-of-credits, 401/403 bad key) — back off
                # BEFORE the silent mark_failed below, so the scheduler stops hammering
                # this key every heartbeat interval (Task 1's due_heartbeat_users query
                # already excludes users still in cooldown).
                await _fence_wake_effect("payment cooldown")
                await asyncio.to_thread(
                    jobs_store.upsert_wake_schedule, user_id,
                    payment_cooldown_until=time.time() + _WAKE_COOLDOWN_SEC)
            raise

        await asyncio.to_thread(
            jobs_store.mark_completed, job_id, claimed_by=claimed_by)
        # End-of-turn drain (mirrors process_job's chat-branch finalize): a write
        # tool_call in the LAST round has no subsequent on_reply to trigger a drain,
        # so flush whatever's still pending. Best-effort — the job is already
        # durably completed by this point, so a drain failure must not flip it to
        # failed (same reasoning as the chat-lane end-of-turn drain).
        if deps.apply_pending_effects is not None:
            try:
                await asyncio.to_thread(deps.apply_pending_effects, user_id)
            except Exception as e:  # noqa: BLE001 — see comment above
                log.warning("[v2.worker] wake apply_pending_effects failed user=%s: %s", user_id, e)
        if tm is not None:
            tm.flush(failed=False, status="ok")
        return "completed"
    except Exception as e:  # noqa: BLE001 — wake job: silent mark_failed, never surface/bubble
        code = _safe_failure_code("wake_failed", e)
        log.warning("[v2.worker] wake job %s lane=%s failed code=%s", job_id, lane, code)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, code, claimed_by=claimed_by)
        if tm is not None:
            tm.flush(failed=True, status=code)
        return "failed"


async def _run_extraction(job_id, user_id: str, lane: str, deps: TurnDeps,
                          provider_config: Any, enclave_sem: "asyncio.Semaphore",
                          claimed_by: str | None = None,
                          tm: "TurnMetrics | None" = None) -> str:
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
            if deps.read_tail_after_seq is not None:
                through_seq = await asyncio.to_thread(db.chat_max_seq, user_id)
                tail = await asyncio.to_thread(
                    deps.read_tail_after_seq,
                    user_id,
                    0,
                    _TAIL_HARD_CAP,
                    through_seq=through_seq,
                )
            elif deps.read_tail is not None:
                tail = await asyncio.to_thread(
                    deps.read_tail, user_id, 0.0, _TAIL_HARD_CAP)
            else:
                tail = []
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

        _report_turn_progress("extraction_provider_start")
        items, reason = await v2_extraction.extract(
            provider_config=provider_config, prompt=prompt, parse=parse,
            progress_cb=lambda stage, attempt: _report_turn_progress(
                f"extraction_provider_{stage}_{attempt}"),
            usage_out=tm.add_call if tm is not None else None,
        )
        _report_turn_progress("extraction_provider_complete")
        if reason:
            raise RuntimeError(reason)
        if not items:
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            if tm is not None:
                tm.flush(failed=False, status="ok")
            return "completed"

        if deps.build_memory_envelope is None or deps.apply_memory_actions is None:
            await asyncio.to_thread(
                jobs_store.mark_completed, job_id, claimed_by=claimed_by)
            if tm is not None:
                tm.flush(failed=False, status="ok")
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
        if tm is not None:
            tm.flush(failed=False, status="ok")
        return "completed"
    except Exception as e:  # noqa: BLE001 — 背景 job：静默 mark_failed，绝不 surface/写气泡
        code = _safe_failure_code("extraction_failed", e)
        log.warning("[v2.worker] extraction job %s lane=%s failed code=%s", job_id, lane, code)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, code,
            claimed_by=claimed_by)
        if tm is not None:
            tm.flush(failed=True, status=code)
        return "failed"


_LEASE_KEEPALIVE_INTERVAL_SEC = max(
    1.0, min(60.0, float(jobs_store.RUNNING_TTL_SEC) / 3.0))


async def _keep_active_job_lease(
    job_id: Any,
    claimed_by: str,
    stop_event: asyncio.Event,
    *,
    interval: float = _LEASE_KEEPALIVE_INTERVAL_SEC,
) -> None:
    """Renew ownership while a healthy long turn crosses provider boundaries.

    The process watchdog remains the physical liveness authority: a stalled
    async turn is SIGKILLed after the per-turn stall timeout, and a synchronous
    event-loop wedge cannot schedule this coroutine at all.  Thus this keeper
    prevents the 300s DB reaper from racing legitimate multi-attempt work
    without recreating an immortal blind heartbeat.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            owned = await asyncio.to_thread(
                jobs_store.renew_job_lease,
                job_id,
                claimed_by,
                ttl_sec=jobs_store.RUNNING_TTL_SEC,
            )
        except Exception as exc:  # noqa: BLE001 — explicit write fences still decide ownership
            log.warning("[v2.worker] lease keepalive failed job=%s: %s", job_id, exc)
            continue
        if not owned:
            return


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
    api_key: str | None,
    runtime_token: str,
    enclave_sem: "asyncio.Semaphore" = None,
    read_parallelism: int = None,
    tm: "TurnMetrics | None" = None,
) -> str:
    """一回合：coalesce → `tool_loop.run_tool_loop`（provider-native 统一工具循环）→ 落
    加密回复（chat）/沉默是合法结果（wake，见 `_run_wake`）。

    provider_config 由调用方（`_run_turn`）在回合开始前解析好、原样传入并复用全程——
    本函数内绝不再调 `deps.resolve_provider`（single-decrypt-per-turn 不变量）。
    api_key/runtime_token 是 enclave-auth 的两套凭证，只喂 capability 侧（预取 + executor
    的 tool_call 派发），从不流向 provider 的 LLM 调用。

    返回终态字符串（"completed"/"failed"），任一步失败 → mark_failed（绝不写占位气泡）。

    `tm`（spec B5，可选）：`_run_turn` 创建的这个 job 的 `TurnMetrics` whole-turn 累加器；
    None 时（既有直调 `process_job` 的单测、未经 `_run_turn`）就地新建一个，本函数自己
    的终态 flush 点仍然生效。
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
    if tm is None:
        tm = TurnMetrics(job_id=job_id, user_id=user_id, lane=lane)
    tm.bind_provider(provider_config)
    lease_keepalive_stop = asyncio.Event()
    lease_keepalive_task: asyncio.Task | None = None

    try:
        if not claimed_by or not await asyncio.to_thread(
            jobs_store.mark_running, job_id, claimed_by=claimed_by
        ):
            raise LostJobLease("job ownership lost before start")
        lease_keepalive_task = asyncio.create_task(
            _keep_active_job_lease(job_id, claimed_by, lease_keepalive_stop))

        async def _renew_lease() -> None:
            if not await asyncio.to_thread(
                jobs_store.renew_job_lease,
                job_id,
                claimed_by,
                ttl_sec=jobs_store.RUNNING_TTL_SEC,
            ):
                raise LostJobLease("job lease expired or ownership changed")

        async def _ensure_runtime_mode() -> None:
            # D4 live kill switch: checked first, same fence as runtime-mode-disabled
            # below (mark_failed + tm.flush + raise RuntimeModeChanged) — this closure
            # is what _before_write/_dispatch_tools/the turn-start check all call, so
            # gating here fences every active write and tool-dispatch round while
            # halted without a second bare-raise path that would skip the bookkeeping.
            if await asyncio.to_thread(kill_switch.turns_halted):
                owned = await asyncio.to_thread(
                    jobs_store.mark_failed,
                    job_id,
                    "turns_halted",
                    claimed_by=claimed_by,
                )
                if owned and lane == "chat":
                    await asyncio.to_thread(
                        _surface_terminal_error,
                        deps,
                        user_id,
                        job_id,
                        "turns_halted",
                    )
                tm.flush(failed=True, status="turns_halted")
                raise RuntimeModeChanged("v2 turns halted")
            if deps.runtime_mode_enabled is None or await asyncio.to_thread(
                deps.runtime_mode_enabled, user_id
            ):
                return
            owned = await asyncio.to_thread(
                jobs_store.mark_failed,
                job_id,
                "runtime_mode_changed",
                claimed_by=claimed_by,
            )
            if owned and lane == "chat":
                await asyncio.to_thread(
                    _surface_terminal_error,
                    deps,
                    user_id,
                    job_id,
                    "runtime_mode_changed",
                )
            tm.flush(failed=True, status="runtime_mode_changed")
            raise RuntimeModeChanged("user is no longer assigned to V2")

        await _ensure_runtime_mode()

        if lane == "maintenance":
            # 自成一体的压缩路径：自己的 try/except（见 `_run_compaction`），绝不落到本
            # 函数下面那个 chat-turn 的 `except`——那个分支会 emit 用户可见的 error status
            # + record_terminal_error（iOS 错误 chip），压缩失败是后台维护事，不该弹给用户。
            return await _run_compaction(
                job_id, user_id, deps, provider_config, enclave_sem, claimed_by, tm)
        if lane in _WAKE_LANES:
            # Self-contained wake path (D3 Task 6): proactive turn, not a reply to a
            # just-sent user message. Own try/except inside `_run_wake` — never falls
            # into the chat-turn `except` below (that branch emits a user-visible
            # error status + record_terminal_error, which wake failures must not do).
            return await _run_wake(
                job_id, user_id, lane, deps, provider_config, enclave_sem, claimed_by, tm)
        if lane in _EXTRACTION_LANES:
            # 自成一体的记忆抽取路径（capture/dream，Task 3）：build prompt → BYOK 抽取 →
            # parse → memory actions。同 _run_compaction/_run_wake 一样有自己的 try/except，
            # 绝不落进下面 chat-turn 的 except（那条会 emit 用户可见 error status +
            # record_terminal_error）——后台 job 永不写气泡、永不弹 error chip。
            return await _run_extraction(
                job_id, user_id, lane, deps, provider_config, enclave_sem, claimed_by, tm)
        if lane != "chat":
            # 真·未注册 lane 的兜底：maintenance/wake（heartbeat/scheduled/manual_wake）/
            # capture/dream 都已在上面各自的 handler 里分派完；能落到这里的只剩既不是 chat、
            # 又没有对应 handler 的 lane（配置错误 / 未来新增但未接线的 lane）。若放它掉进下面
            # 的 chat 回合，模型一旦回复就会写出用户可见的聊天气泡、失败还弹 error chip。
            #
            # 显式失败，静默（背景 job 的既有口径：不写气泡、不 _surface_terminal_error）——
            # 落到这里就是「明确失败」而不是「偷偷写气泡」。
            log.warning("[v2.worker] job %s has unhandled lane=%s", job_id, lane)
            await asyncio.to_thread(
                jobs_store.mark_failed, job_id, f"unhandled_lane:{lane}",
                claimed_by=claimed_by)
            tm.flush(failed=True, status=f"unhandled_lane:{lane}")
            return "failed"
        store = core_store.get_store(user_id)
        runtime_state = await asyncio.to_thread(jobs_store.get_runtime_state, user_id)
        await asyncio.to_thread(_emit_status, user_id, job_id, "processing")

        seq_native = deps.read_messages_after_seq is not None
        if seq_native:
            # Recovery comes before cursor load/provider work. A previous process
            # may have durably enqueued its final compound reply but crashed before
            # draining it; generating another answer first would duplicate the turn.
            if deps.apply_pending_effects is not None:
                await asyncio.to_thread(deps.apply_pending_effects, user_id)
        # The migrated durable cursor is authoritative.  In particular, do not
        # reinterpret a legitimate zero via last_replied_ts here: replaying a
        # conservative boundary is safe, while a <= timestamp bootstrap can
        # permanently classify the exact same-ts stranded message as answered.
        since_seq = await asyncio.to_thread(v2_cursor.load_seq, store)
        generation = await asyncio.to_thread(
            jobs_store.get_input_generation, job_id, claimed_by=claimed_by)
        if generation is None:
            raise LostJobLease("job ownership lost before input read")
        observed_generation = generation
        coalesced, cursor_seq, cursor_ts = await _coalesce_inputs(
            deps, user_id, since_seq, enclave_sem=enclave_sem)
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
            tm.flush(failed=False, status="ok")
            return "completed"

        # —— Unified provider-native tool loop (spec C6 + C9a) ——
        # Every model drives the same catalog through the same loop. Writes
        # never run inline; they become
        # PR A generation-fenced effects (enqueued here, drained by
        # `deps.apply_pending_effects`). `reply` (intermediate) and terminal
        # plain text both flow through `on_reply` -> a "reply" effect, drained
        # immediately for mid-loop visibility (C6).
        # Use claim-time ownership generation (ABA-safe); see wake path above.
        gen = int(
            job.get("expected_runtime_generation")
            if job.get("expected_runtime_generation") is not None
            else await asyncio.to_thread(db.get_runtime_generation, user_id)
        )
        ordinal = itertools.count()
        action_digest: dict[str, dict] = {}

        # D1 base context (summary + tail), read once at loop entry. deps.read_summary/
        # read_tail being None (older/minimal test deps) degrades to empty
        # summary/tail, exactly as before.
        seq_context = (
            deps.read_summary_with_seq is not None
            and deps.read_tail_after_seq is not None
        )
        covered_seq = int(cursor_seq or 0) if seq_native else 0
        legacy_context = deps.read_summary is not None and deps.read_tail is not None
        if seq_context or legacy_context:
            # D6/Task 10: close a compaction backlog gap BEFORE reading the
            # actual prompt content — see `_ensure_prompt_coverage`'s
            # docstring. Common case (no gap) costs two cheap indexed reads
            # and returns immediately without touching the enclave/LLM.
            await _ensure_prompt_coverage(
                user_id, deps, provider_config=provider_config,
                enclave_sem=enclave_sem, tail_limit=_TAIL_HARD_CAP,
                job_id=job_id, claimed_by=claimed_by,
                add_usage=tm.add_call)
            async with enclave_sem:
                if seq_context:
                    summary, _watermark_ts, _ver, watermark_seq = await asyncio.to_thread(
                        deps.read_summary_with_seq, user_id)
                    through_seq = await asyncio.to_thread(db.chat_max_seq, user_id)
                    # The base tail already contains every row through this
                    # snapshot. Seed the first round-boundary fold after it;
                    # otherwise a user message arriving after coalesce but before
                    # the tail snapshot appears once in the base tail and once
                    # again as a folded input.
                    covered_seq = max(covered_seq, int(through_seq))
                    tail = await asyncio.to_thread(
                        deps.read_tail_after_seq,
                        user_id,
                        watermark_seq,
                        _TAIL_HARD_CAP,
                        through_seq=through_seq,
                    )
                else:
                    summary, watermark, _ver = await asyncio.to_thread(
                        deps.read_summary, user_id)
                    tail = await asyncio.to_thread(
                        deps.read_tail, user_id, watermark, _TAIL_HARD_CAP)
                tail = await asyncio.to_thread(
                    _inject_tail_images, tail, user_id=user_id, read_images=deps.read_images)
            # Post-assembly hard assertion (D6): independent re-derivation,
            # not a reuse of `_ensure_prompt_coverage`'s return — see
            # `_assert_prompt_covers`'s docstring for why.
            if seq_context:
                await _assert_prompt_tail_exact(
                    user_id,
                    watermark_seq=watermark_seq,
                    through_seq=through_seq,
                    tail=tail,
                )
            else:
                await _assert_prompt_covers(user_id, _TAIL_HARD_CAP)
        else:
            summary, tail = "", []

        async def _enqueue_write_effect(tc) -> None:
            """WRITE tool_call -> PR A effect (spec C6). Mapping lives in the shared
            `_write_tool_effect_payload` (also used by `_run_wake` — Task 8)."""
            logical_effect_type, payload = _write_tool_effect_payload(tc)
            effect_type = ENCRYPTED_TOOL_EFFECT_TYPES[logical_effect_type]
            ordinal_value = next(ordinal)
            effect_id = v2_effect_id.derive(
                job_id=job_id,
                effect_type=effect_type,
                ordinal=ordinal_value,
            )
            encrypted_payload = await asyncio.to_thread(
                _build_encrypted_tool_effect_payload,
                store,
                payload,
                effect_id=effect_id,
            )
            enqueued_id = await asyncio.to_thread(
                v2_effect_outbox.enqueue_effect,
                job_id=job_id,
                user_id=user_id,
                effect_type=effect_type,
                ordinal=ordinal_value,
                expected_generation=gen,
                payload=encrypted_payload,
            )
            if enqueued_id != effect_id:
                raise RuntimeError("tool effect id derivation mismatch")

        async def _before_write() -> None:
            # Recheck runtime mode and lease before each serialized write in
            # the native tool dispatcher.
            # D4 live kill switch check lives in _ensure_runtime_mode itself (see
            # below) so it gets the same mark_failed+flush bookkeeping as the
            # runtime-mode-disabled case, not a bare raise that would skip it.
            await _ensure_runtime_mode()
            await _renew_lease()

        async def _dispatch_tools(tool_calls):
            # Fence once per round before any capability executes (mirrors the
            # old _run_tools's renewal ahead of the read burst); writes get a
            # second, per-write fence via before_write above.
            await _ensure_runtime_mode()
            await _renew_lease()
            for tc in tool_calls:
                action_digest.setdefault(tc.name, {"ok": 0, "count": 0})["count"] += 1
            results = await v2_executor.dispatch_tool_calls(
                tool_calls, store=store, api_key=api_key, runtime_token=runtime_token,
                enclave_sem=enclave_sem, turn_authorization=True,
                enqueue_write_effect=_enqueue_write_effect, before_write=_before_write,
                read_parallelism=read_parallelism)
            for tc, r in zip(tool_calls, results):
                if not r.content.startswith("error"):
                    action_digest[tc.name]["ok"] += 1
            return results

        async def _on_reply(text: str, *, final: bool) -> None:
            text = str(text or "").strip()
            if final and not text:
                # BUG-4 no-filler: chat lane always replies — an empty terminal
                # text is a model/provider failure here (unlike wake, where
                # silence is legitimate), so this becomes a mark_failed via the
                # outer except below, never a placeholder bubble.
                raise TurnError("empty_reply")
            if not text:
                return  # empty intermediate reply{} call: no bubble, not an error
            # A cutover/ABA can happen while awaiting the provider. Fence at
            # the reply effect itself; the pre-round check is not sufficient.
            await _ensure_runtime_mode()
            await _renew_lease()
            ordinal_value = next(ordinal)
            payload = {"text": text}
            if seq_native:
                eid = v2_effect_id.derive(
                    job_id=job_id, effect_type="reply", ordinal=ordinal_value)
                payload = await asyncio.to_thread(
                    _build_encrypted_reply_effect_payload,
                    store,
                    text,
                    effect_id=eid,
                    reply_through_seq=(cursor_box["seq"] if final else None),
                )
            await asyncio.to_thread(
                v2_effect_outbox.enqueue_effect,
                job_id=job_id,
                user_id=user_id,
                effect_type="reply",
                ordinal=ordinal_value,
                expected_generation=gen,
                payload=payload,
            )
            # C6: drain immediately so an intermediate bubble is visible mid-loop,
            # not only at end-of-turn. Offloaded — the reply sink's
            # `_write_encrypted_reply` does an enclave envelope round-trip and must
            # not run synchronously on the event-loop thread (the old chat path
            # offloaded this same write via `asyncio.to_thread`; mid-loop drain
            # dropped that until now — same failure class as the per-round fold
            # offload above).
            if deps.apply_pending_effects is not None:
                await asyncio.to_thread(deps.apply_pending_effects, user_id)

        # The strict production path seeds after the exact prompt snapshot so a
        # user row already present in the verbatim tail is not folded twice.
        # ``ts`` rides along only for rollback telemetry; seq remains the sole
        # correctness boundary and is committed with the final reply.
        cursor_box = {
            "seq": max(int(since_seq), int(cursor_seq), int(covered_seq)),
            "ts": float(cursor_ts),
        }
        # Pass THIS turn's enclave_sem through explicitly (not the closure's module-level
        # default) — process_job may have been called with an injected/test semaphore
        # (e.g. tests/test_v2_worker.py's _CountingSemaphore), and the per-round fold must
        # share the exact same gate the rest of this turn's enclave-bound calls use.
        fold_new_messages = _make_fold_new_messages(user_id, deps, cursor_box, enclave_sem=enclave_sem)
        build_messages = _make_build_messages_fn(
            system_prompt=context.CHAT_SYSTEM_PROMPT, summary=summary, tail=tail)

        await _ensure_runtime_mode()
        await _renew_lease()
        await asyncio.to_thread(_emit_status, user_id, job_id, "writing_reply")
        outcome = await v2_tool_loop.run_tool_loop(
            provider_config=provider_config, build_messages=build_messages,
            dispatch_tools=_dispatch_tools, on_reply=_on_reply,
            fold_new_messages=fold_new_messages, add_usage=tm.add_call,
            max_calls=_TURN_MAX_LLM_CALLS, fold_before_first=seq_native,
            on_progress=_report_turn_progress)
        if not outcome.replied_intermediate and not (outcome.final_text or "").strip():
            # BUG-4 no-filler class: the loop returned WITHOUT ever producing a
            # bubble — budget_exhausted (max_calls reached with no terminal
            # final_text call), or a misbehaving last round that returns
            # tool_calls despite tools=None (so `_on_reply`'s own "final and not
            # text" guard above never fires because pr.tool_calls was truthy and
            # on_reply was never called with final=True at all). Chat always
            # replies — silently completing here would drop the user's message
            # exactly like the already-fixed BUG-4. Raise the same signal
            # `_on_reply` uses so it falls into the outer except below:
            # mark_failed + terminal error status + tm.flush(failed=True).
            raise TurnError("empty_reply")
        if seq_native:
            durable_seq = await asyncio.to_thread(v2_cursor.load_seq, store)
            if durable_seq < cursor_box["seq"]:
                raise RuntimeError("final reply cursor was not durably committed")

        # 超预算 → best-effort 入队一个 maintenance lane 的压缩 job（不阻塞、不拖垮
        # 本回合——enqueue_job 本身命中 single-flight 会 coalesce，失败只记日志）。
        if tail and context.needs_compaction(tail, budget=_TAIL_BUDGET):
            try:
                await asyncio.to_thread(jobs_store.enqueue_job, user_id, "maintenance", reason="compaction")
            except Exception as e:  # noqa: BLE001 — 压缩入队失败绝不能拖垮已经写成的这条回复
                log.warning("[v2.worker] enqueue compaction failed for %s: %s", user_id, e)

        # Strict production replies commit the seq cursor atomically inside the
        # final compound reply effect.  Keep the old standalone cursor effect
        # only for compatibility callers that do not provide the production
        # read_messages_after_seq seam (and therefore cannot use the compound
        # encrypted reply sink); it is never selected by build_production_deps.
        new_seq = max(int(since_seq), int(cursor_box["seq"]))
        if not seq_native and new_seq > since_seq:
            await asyncio.to_thread(
                v2_effect_outbox.enqueue_effect,
                job_id=job_id,
                user_id=user_id,
                effect_type="cursor",
                ordinal=next(ordinal),
                expected_generation=gen,
                payload={"new_seq": new_seq},
            )

        # Keep last_replied_ts freshly dual-written: nothing READS it in the seq
        # world, but a code rollback to the ts boundary must resume from a fresh
        # cursor rather than re-reading history. cursor_box["ts"] tracks the max
        # ts answered across the initial coalesce AND every mid-turn fold, so the
        # rollback cursor stays faithful. action_digest rides this same upsert.
        _prev_ts = 0.0
        try:
            _prev_ts = float(runtime_state.get("last_replied_ts") or 0)
        except (TypeError, ValueError):
            _prev_ts = 0.0
        new_last_replied = cursor_box["ts"] if cursor_box["ts"] > _prev_ts else _prev_ts
        await asyncio.to_thread(
            jobs_store.upsert_runtime_state, user_id,
            {"last_replied_ts": new_last_replied, "action_digest": action_digest})
        await _ensure_runtime_mode()
        completed, successor_id = await asyncio.to_thread(
            jobs_store.finish_chat_job,
            job_id,
            claimed_by=claimed_by,
            observed_generation=observed_generation,
        )
        if not completed:
            raise LostJobLease("job ownership lost during finalization")
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
            except db.EffectDeliveryUncertainError:
                # The chat/job transition is already durable, so do not try to
                # rewrite it to failed.  This is nevertheless a user-visible
                # terminal delivery problem: surface a stable code through the
                # status stream and hosted last_runtime_error instead of leaving
                # an unresolved generic effect as a log-only failure.
                message = "effect_delivery_uncertain"
                log.warning(
                    "[v2.worker] unresolved effect delivery user=%s job=%s",
                    user_id, job_id,
                )
                await asyncio.to_thread(
                    _surface_terminal_error, deps, user_id, job_id, message)
            except Exception as e:  # noqa: BLE001 — see comment above
                log.warning("[v2.worker] apply_pending_effects failed user=%s: %s", user_id, e)
        tm.flush(failed=False, status="ok")
        return "completed"
    except LostJobLease as e:
        # The winning lifecycle transition (normally the reaper) owns terminal
        # visibility. A stale worker must not overwrite it or emit a duplicate error.
        # No tm.flush here on purpose: this worker doesn't own the terminal state,
        # so it must not also claim ownership of (or overwrite) the metric row.
        log.warning("[v2.worker] job %s fenced out: %s", job_id, e)
        return "failed"
    except RuntimeModeChanged as e:
        # _ensure_runtime_mode owns all bookkeeping before raising: when its
        # mark_failed transition wins, accepted chat jobs are surfaced through
        # status + last_runtime_error; every lane's metric is flushed there too.
        # Repeating either action here would duplicate user-visible errors/metrics.
        log.info("[v2.worker] job %s stopped at rollout fence: %s", job_id, e)
        return "failed"
    except Exception as e:  # noqa: BLE001 — 任何失败落 last_error，绝不写占位气泡
        message = _safe_failure_code("turn_failed", e)
        log.warning("[v2.worker] job %s failed code=%s", job_id, message)
        owned = await asyncio.to_thread(
            jobs_store.mark_failed, job_id, message, claimed_by=claimed_by)
        if owned:
            await asyncio.to_thread(_surface_terminal_error, deps, user_id, job_id, message)
        tm.flush(failed=True, status=message)
        return "failed"
    finally:
        lease_keepalive_stop.set()
        if lease_keepalive_task is not None:
            lease_keepalive_task.cancel()
            await asyncio.gather(lease_keepalive_task, return_exceptions=True)


async def _run_turn(job: dict, deps: TurnDeps) -> str:
    """把一个已 claim 的 job 交给 `process_job` 之前，先做一次性的 enclave-bound 解析：
    单次解密 BYOK provider key（single-decrypt-per-turn）+ 铸一个
    enclave-auth runtime_token。resolve_provider 失败（未配置/解密失败等）直接 mark_failed，
    不进入全流程（没有可用 provider，tool loop 跑不了）。

    spec B5：这是 job_id/user_id/lane 最早齐全的地方，所以本 job 的 `TurnMetrics`
    whole-turn 累加器在这里创建（早于 `process_job`——provider 解析失败时根本不会进
    `process_job`，那次失败仍需要一行 v2_turn_metrics），再原样传给 `process_job`
    复用（同一个 job 只有一行，不会因为累加器实例不同而分裂成两次 upsert）。"""
    job_id = job["id"]
    user_id = str(job["user_id"])
    claimed_by = str(job.get("claimed_by") or "")
    lane = str(job.get("lane") or "chat")
    tm = TurnMetrics(job_id=job_id, user_id=user_id, lane=lane)
    try:
        async with ENCLAVE_SEMAPHORE:
            provider_config, meta = await asyncio.to_thread(deps.resolve_provider, user_id)
        _report_turn_progress("provider_config_resolved")
        if provider_config is None:
            err = "provider_unavailable"
            owned = await asyncio.to_thread(
                jobs_store.mark_failed, job_id, err, claimed_by=claimed_by)
            if owned and lane == "chat":
                await asyncio.to_thread(_surface_terminal_error, deps, user_id, job_id, err)
            tm.flush(failed=True, status=err)
            return "failed"
        tm.bind_provider(provider_config)
        runtime_token = await asyncio.to_thread(deps.mint_enclave_token, user_id)
        _report_turn_progress("runtime_token_minted")
        return await process_job(
            job, deps,
            provider_config=provider_config,
            api_key=None, runtime_token=runtime_token,
            tm=tm,
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
        tm.flush(failed=True, status=message)
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
    slot_id: int = 0, progress_cb: "Callable[[int, float | None], None] | None" = None,
) -> None:
    """一个 job-slot：抢一个 job 就跑一回合，抢不到就等待（poll_interval 兜底，
    wake_event 命中时立刻醒——见 `_wait_for_job_or_stop`）。stop_event 置位后不再抢新活，
    跑完手上的即退出（优雅 drain）。

    lanes（可选）：转给 `jobs_store.claim_next_job` 的 lane 白名单（Task 2）。None＝不限制
    （行为与改动前完全一致）；非 None 时这个 slot 只抢白名单里的 lane——`run_worker_loop`
    用它给部分 slot 划专用车道（见 `_reserved_lane_slots`）。

    progress_cb（可选，PR D Task 2 + hard-timeout fix）：`progress_cb(slot_id, turn_start)`
    在真实 slot 活动的天然边界调用——claim 到一个 job 之后（即将进入 `_run_turn`）、
    `_run_turn` 跑完、每次空转 poll 醒来，以及 task-local `_report_turn_progress` 转发的
    provider round / reliable retry / tool batch / compaction batch 边界。这样一个合法长回合会
    刷新 stall clock，但所有消息始终携带同一个 `turn_start`，绝不重置 absolute clock。
    `slot_id` 是 `run_worker_loop` 里的下标（不是 `"{worker_id}#{i}"` 复合标签）。

    `turn_start`（hard-timeout fix）：这个 slot 当前正在跑的 turn 的开始时刻
    （`time.monotonic()`），空转/回合已完成时为 `None`。这是让 `ChildSupervisor.
    poll_liveness()` 能报告"最老一个仍在跑的 turn 已经跑了多久"（`current_turn_age_sec`，
    见 `child_supervisor.py`）的唯一信号来源——一个卡在单个 turn 里永不返回的 slot 会
    STOP SENDING in-turn boundaries；父进程保留固定 start 与最后 progress receipt，分别让
    absolute age 和 stall age 随挂钟增长。其它 slot 或 event-loop heartbeat 不能替它刷新。

    `progress_cb` 必须便宜、且绝不能把异常炸进这个循环——调用点自己包 try/except，记
    日志后吞掉，不影响抢活/跑回合的主路径。默认 None：向后兼容既有调用方/测试，不传就是
    纯 no-op。

    per-iteration 的抢活 + 跑回合整段包 try/except：单个 slot 上的瞬时故障（例如 claim/
    mark_running 撞到一次性 DB 错误）绝不允许冒出这个协程、拖垮 run_worker_loop 里其他
    仍然健康的 slot——记日志后 continue，下一轮再抢。"""

    def _signal_progress(turn_start: float | None = None) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(slot_id, turn_start)
        except Exception as e:  # noqa: BLE001 — 心跳信号故障绝不能拖垮 slot 主循环
            log.debug("[v2.worker] slot %s progress_cb failed: %s", worker_id, e)

    while not stop_event.is_set():
        job = None
        try:
            # D4 live kill switch: while halted, claim nothing this iteration — idle
            # and re-check on the next poll/wake (fail-open here on a control-plane
            # read error: admission already fail-closes new work at the front door;
            # a slot that's already running must not treat a transient DB blip on
            # THIS read as a reason to stop draining jobs it may already own).
            if await asyncio.to_thread(kill_switch.turns_halted):
                await _wait_for_job_or_stop(stop_event, wake_event, poll_interval)
                _signal_progress()
                continue
            job = await asyncio.to_thread(jobs_store.claim_next_job, worker_id, lanes=lanes)
            if job is None:
                await _wait_for_job_or_stop(stop_event, wake_event, poll_interval)
                _signal_progress()
                continue
            # (a) claimed a job, about to enter _run_turn — record the turn's start
            # time and report it so a wedge INSIDE _run_turn (the slot never reaches
            # (b) below) still shows up as a climbing current_turn_age_sec even
            # though this slot never sends another message.
            turn_start = time.monotonic()
            _signal_progress(turn_start)
            # Keep the public `_run_turn(job, deps)` seam unchanged (many tests
            # and assembly callers replace it directly).  A task-local callback
            # lets provider/tool/compaction helpers refresh this exact slot's
            # stall clock without leaking progress across concurrent slots.
            def _active_turn_progress(_stage: str) -> None:
                _signal_progress(turn_start)

            progress_token = _TURN_PROGRESS_CB.set(_active_turn_progress)
            try:
                await _run_turn(job, deps)
            finally:
                _TURN_PROGRESS_CB.reset(progress_token)
            _signal_progress(None)  # (b) turn completed — back to idle
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
                    # spec B5: `_run_turn` already wraps its whole body in its own
                    # `except Exception` that flushes `tm` before returning "failed" (and
                    # its `except asyncio.CancelledError: raise` doesn't land here either —
                    # CancelledError is BaseException, not Exception, so this outer
                    # `except Exception` wouldn't catch it even if it did propagate this
                    # far). What CAN still reach here despite that: a secondary exception
                    # raised from WITHIN `_run_turn`'s own except-block bookkeeping itself
                    # (e.g. `jobs_store.mark_failed`/`tm.flush` throwing before the flush
                    # completes) — a narrow escape where `_run_turn`'s own `tm` never got
                    # a chance to flush, so this whole-turn-metric gap still needs its own
                    # row (model_calls=0 — no visibility into how far the turn got before
                    # it escaped).
                    await asyncio.to_thread(
                        jobs_store.record_whole_turn_metric,
                        job["id"], user_id, str(job.get("lane") or "chat"),
                        prompt_tokens=None, completion_tokens=None, latency_ms=0,
                        model_calls=0, retries=0, failed=True, status=message)
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
            _signal_progress()  # (c) idle/error poll wake — slot is alive, just cycling
            continue


async def run_worker_loop(
    worker_id: str, *, max_workers: int, poll_interval: float, stop_event: asyncio.Event, deps: TurnDeps,
    wake_event: "asyncio.Event | None" = None,
    progress_cb: "Callable[[int, float | None], None] | None" = None,
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

    progress_cb（可选，PR D Task 2）：原样转给每个 `_slot_loop`，附带该 slot 在
    `assignments` 里的下标作为 `slot_id`——见 `_slot_loop` 自己的 docstring。默认 None：
    向后兼容既有调用方/测试。

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
                       deps=deps, wake_event=wake_event, lanes=assignments[i],
                       slot_id=i, progress_cb=progress_cb)
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
