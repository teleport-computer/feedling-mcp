"""V2 worker 进程入口 + 生产依赖装配（子项目 B，Task 8 扩到全流程）。

部署目标（已钉死，见 spec §2.1 与 merge-conditions-backlog 条件 5）：这是**同一 backend/
镜像的兄弟入口**，运行在 **runner CVM（agent-runner supervisor）** 内，与常驻 consumer、
genesis import worker 并肩——**不是**独立 HTTP 服务、**不是**独立 repo、**不**贴着主 app
CVM 的 FastAPI backend 跑。HTTP 化会把 backend→enclave→backend 的 reentrant 502 根因请回来；
贴主 app 跑则与 backend 争 CPU/内存。实际进程启动（manifest/compose）属子项目 D 的 rollout，
本文件只钉死「跑在哪」这一决定。

装配层：这里（且只有这里）可同时 import hosted/agent_runtime/core/model_api_runtime，把
需要上层的实现注入进 worker.TurnDeps，令 worker.py 保持不逆依赖（CONTRIBUTING §2）。
worker.py 明确不 import `hosted` 或 `agent_runtime.spawners`——official/非官方判定（用户是
否在跑受信任模型）需要 `agent_runtime.spawners._is_official_identity`，那是 hosted-adjacent
的东西，所以只在这里包一层，经 `TurnDeps.is_official` 注入进去。

生产 turn 依赖：
- resolve_provider：mint 一个 user-scoped runtime token → hosted.config_store 用它 JIT
  解密 provider key（单次；只留内存，不落库）。enclave-bound（受 worker.ENCLAVE_SEMAPHORE 框住）。
  BYOK-only：`_load_runtime_provider_config` 只从该用户自己的 `model_api_config` 信封解出
  provider key，从不读取/回退任何平台系统 key。
- is_official：包一层 `agent_runtime.spawners._is_official_identity`（driver/endpoint 是否
  官方原生）——这是 worker.py 不能自己算的唯一原因，就是这个函数活在 hosted-adjacent 的
  agent_runtime 包里。
- mint_enclave_token：签发 enclave-auth runtime_token（scope=envelope_decrypt）。只是 HMAC
  签名，不是解密，回合内可按需多签（executor 的 capability 调用 + read_messages 的逐条
  chat 解密都要用它）。
- read_messages：读该用户 chat_messages 中自上一条 assistant 之后的 user 行，逐条经 enclave
  解密取明文（服务器永不本地解密）；带上 id/ts 供 v2.coalesce 按 since_ts 过滤/去重。enclave-bound。

respond/append_reply 不再是 TurnDeps 的字段：worker.process_job 现在直接调用同层的
`model_api_runtime.v2.responder.respond`（本就 hosted-free）和自己的
`_write_encrypted_reply`（只 import core.envelope/core.store，同样 hosted-free），不需要
再经这层装配转发一次。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import types

from accounts import registry as accounts_registry
from agent_runtime import spawners as agent_spawners
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import runtime_token
from core import store as core_store
from core import wake_bus as core_wake_bus
from hosted import config_store as hosted_config_store
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import scheduler
from model_api_runtime.v2 import worker as v2_worker
from proactive import gate as proactive_gate
import db

log = logging.getLogger("feedling.runtime_v2.serve_worker")

_ASSISTANT_ROLES = ("openclaw", "assistant", "agent")

# Scope name matches the existing host-all/genesis-worker convention
# (agent_runtime/supervisor.py mints "envelope_decrypt", not a colon-form) so a
# future scope-enforcement change (currently the enclave's local HMAC check
# only verifies signature+expiry+user_id, not scope — see enclave/auth.py
# local_user_id_from_token) doesn't silently start rejecting this worker.
_RUNTIME_TOKEN_SCOPE = ["envelope_decrypt"]


def _mint_runtime_token(user_id: str) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        secret,
        user_id=user_id,
        runtime_instance_id="v2-worker",
        scope=_RUNTIME_TOKEN_SCOPE,
        ttl=900.0,
    )


def _resolve_provider(user_id: str):
    """单次解密该用户 provider key（enclave-bound，BYOK-only）。返回 (ProviderConfig|None, meta)。

    ``hosted_config_store._load_runtime_provider_config`` reads only this user's
    own ``model_api_config`` envelope (``api_key_envelope``) — there is no
    platform/system LLM key anywhere on this path. On any failure (unconfigured,
    untested, envelope missing, decrypt failure, invalid config) it returns a
    ``(None, {"error": ...})`` tuple; this function forwards that shape as-is so
    ``worker._run_turn`` can mark the job failed without a placeholder reply.
    """
    store = core_store.get_store(user_id)
    try:
        token = _mint_runtime_token(user_id)
    except Exception as e:  # noqa: BLE001
        return None, {"error": "runtime_token_mint_failed", "detail": str(e)[:160]}
    # api_key=None: hosted/host-all turns never hold the user's long-term
    # Feedling API key — only the runtime token authenticates to the enclave.
    runtime = hosted_config_store._load_runtime_provider_config(store, None, runtime_token=token)
    if isinstance(runtime, tuple):
        return None, runtime[1]
    return runtime, {}


def _read_messages(user_id: str) -> list[dict]:
    """读该用户自上一条 assistant 之后的 user 消息，逐条经 enclave 解密成明文文本。
    服务器永不本地解密——每条信封走 enclave /v1/envelope/decrypt。

    The full stored message dict is forwarded to ``_decrypt_envelope_via_enclave``
    (not a hand-picked subset) — mirrors ``content.content_core._build_rewrapped_envelope``,
    which passes the whole ``record``. This matters: the enclave's AEAD
    additional-data is ``owner_user_id||v||id`` (see ``enclave/envelope.py
    decrypt_envelope`` / ``build_aead_aad``), so dropping the message's ``id``
    would make every real (non-synthetic) chat envelope fail AEAD verification.

    Each returned dict carries ``id``/``ts`` alongside ``role``/``content`` (Task 8):
    ``model_api_runtime.v2.coalesce.coalesce_pending`` filters by ``ts > since_ts`` and
    dedupes by ``id`` — Plan B's shape (``role``/``content`` only) predates the v2
    coalesce/planner/executor pipeline and didn't need either field.
    """
    store = core_store.get_store(user_id)
    rows = list(getattr(store, "chat_messages", []) or [])
    # 找到最后一条 assistant 回复的下标；只回放其后的 user 消息（未答的那批）。
    last_assistant = -1
    for idx, m in enumerate(rows):
        if str(m.get("role") or "") in _ASSISTANT_ROLES:
            last_assistant = idx
    pending = rows[last_assistant + 1:]
    token = _mint_runtime_token(user_id)
    out: list[dict] = []
    for m in pending:
        if str(m.get("role") or "") != "user":
            continue
        mid, ts = m.get("id"), m.get("ts")
        if m.get("content_type") == "image":
            out.append({"id": mid, "ts": ts, "role": "user", "content": "[image]"})
            continue
        if not m.get("body_ct") or m.get("K_enclave") is None:
            continue  # 无 enclave 钥的合成/本地-only 消息跳过
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            m, None, purpose="v2_chat_read", runtime_token=token
        ).decode("utf-8")
        if plaintext.strip():
            out.append({"id": mid, "ts": ts, "role": "user", "content": plaintext})
    return out


def _read_tail(user_id: str, after_ts: float, limit: int) -> list[dict]:
    """读该用户最近一个窗口内的消息（BOTH roles），逐条经 enclave 解密取明文（D1）。

    镜像 `_read_messages` 的解密/过滤规则，但服务于不同目的：`_read_messages` 只回放
    "自上一条 assistant 回复之后"的未答 user 消息（喂 coalesce/planner 判断本回合要不要
    起新 job）；`_read_tail` 给 turn 看一段**真实的、双角色的**近期对话尾巴（喂 responder
    的上下文窗口），所以：
    - 不按 last_assistant 下标切片、不跳过非 user 行；
    - 只保留 ts > after_ts 的行；
    - assistant 角色（`_ASSISTANT_ROLES`：openclaw/assistant/agent）规整为 "assistant"，
      其余规整为 "user"；
    - 过滤后只留最近 limit 条（`result[-limit:]`），保持时间序。

    Skip 规则与 `_read_messages` 一致：无 `body_ct` 或 `K_enclave is None` 的合成/
    本地-only 行跳过；`content_type == "image"` 走 "[image]" 简写，不经 enclave。"""
    store = core_store.get_store(user_id)
    rows = list(getattr(store, "chat_messages", []) or [])
    rows = sorted(rows, key=lambda m: m.get("ts") or 0.0)
    token = _mint_runtime_token(user_id)
    out: list[dict] = []
    for m in rows:
        ts = m.get("ts")
        if ts is None or not (ts > after_ts):
            continue
        mid = m.get("id")
        role = "assistant" if str(m.get("role") or "") in _ASSISTANT_ROLES else "user"
        if m.get("content_type") == "image":
            out.append({"id": mid, "ts": ts, "role": role, "content": "[image]"})
            continue
        if not m.get("body_ct") or m.get("K_enclave") is None:
            continue  # 无 enclave 钥的合成/本地-only 消息跳过
        plaintext = core_enclave._decrypt_envelope_via_enclave(
            m, None, purpose="v2_chat_read", runtime_token=token
        ).decode("utf-8")
        if plaintext.strip():
            out.append({"id": mid, "ts": ts, "role": role, "content": plaintext})
    if limit <= 0:
        return []
    return out[-limit:]


def _read_summary(user_id: str) -> tuple[str, float, int]:
    """读取该用户当前的会话摘要（Task 2 storage：v2_conversation_summary），逐条走 enclave
    解密取明文（服务器永不本地解密）。从未压缩过（无行）时返回 ("", 0.0, 0)；行存在但
    summary_envelope 为空（Task 2 首建行、尚未真正压缩过一次）时返回 ("", watermark_ts,
    version)，不触发 enclave 往返——没有密文可解。"""
    row = jobs_store.get_summary_row(user_id)
    if row is None:
        return "", 0.0, 0
    env = row["summary_envelope"]
    if not env:
        return "", row["watermark_ts"], row["version"]
    token = _mint_runtime_token(user_id)
    plaintext = core_enclave._decrypt_envelope_via_enclave(
        env, None, purpose="v2_summary_read", runtime_token=token
    ).decode("utf-8")
    return plaintext, row["watermark_ts"], row["version"]


def _write_summary(user_id: str, summary: str, watermark_ts: float, expected_version: int) -> bool:
    """把新压缩出的摘要**本地**加密（core_envelope，非 enclave 往返——跟 worker._write_encrypted_reply
    同一套写法）后 CAS 写回 v2_conversation_summary。信封构建失败（用户从未 onboard 过加密
    身份）时直接返回 False、不调用 CAS——调用方应当把本次压缩当作丢弃处理，不重试。"""
    store = core_store.get_store(user_id)
    env, err = core_envelope._build_shared_envelope_for_store(store, summary.encode("utf-8"))
    if env is None:
        log.warning("[v2.serve_worker] _write_summary build envelope failed for %s: %s", user_id, err)
        return False
    return jobs_store.upsert_summary_row_cas(
        user_id, summary_envelope=env, watermark_ts=watermark_ts, expected_version=expected_version)


def _wake_decision_for_user(user_id: str) -> dict:
    """Read-only heartbeat wake decision via the real proactive gate (assembly
    layer — reuses gate._build_proactive_v2_wake_decision so activation gate /
    broadcast suppression / all landmines hold with zero drift). No enqueue here;
    the scheduler decides what to do with should_wake."""
    store = core_store.get_store(user_id)
    payload = {"trigger": "heartbeat"}
    d = proactive_gate._build_proactive_v2_wake_decision(store, payload)
    return {
        "should_wake": bool(d.get("should_wake_agent")),
        "wake_interval_sec": int(d.get("wake_interval_sec") or 7200),
        "block_reason": str(d.get("reason") or ""),
    }


def _is_official(provider_config) -> bool:
    """包一层 `agent_runtime.spawners._is_official_identity`——worker.py 不能自己 import
    agent_runtime（hosted-adjacent），所以这个判定只能在装配层做好、经 TurnDeps 注入。
    provider_config 为 None 时按官方处理（跟 `_is_official_identity` 对空 provider 的
    缺省一致；实际上 resolve_provider 失败时回合根本不会走到 is_official 这一步）。"""
    if provider_config is None:
        return True
    return agent_spawners._is_official_identity(
        str(getattr(provider_config, "provider", "") or ""),
        str(getattr(provider_config, "base_url", "") or ""))


def _record_terminal_error(user_id: str, message: str) -> None:
    """Task 3: patch hosted.config_store's `last_runtime_error` (the field
    `hosted/setup_core.py:265` reads for iOS's error chip) when a v2 turn fails
    terminally. worker.py only has `user_id` at its early-failure call site (no
    `store` binding there), so this re-fetches the store itself."""
    hosted_config_store.set_last_runtime_error(core_store.get_store(user_id), message)


def build_production_deps() -> v2_worker.TurnDeps:
    return v2_worker.TurnDeps(
        read_messages=_read_messages,
        resolve_provider=_resolve_provider,
        is_official=_is_official,
        mint_enclave_token=_mint_runtime_token,
        record_terminal_error=_record_terminal_error,
        record_turn_metric=jobs_store.record_turn_metric,
        read_tail=_read_tail,
        read_summary=_read_summary,
        write_summary=_write_summary,
    )


def _build_scheduler_deps():
    """装配 `model_api_runtime.v2.scheduler.run_scheduler_tick` 要的 deps（D3 Task 5）。
    scheduler.py 是纯模块（不 import hosted/agent_runtime/proactive）——这里把它接到真实
    实现：due_heartbeat_users（Task 2 落的 v2_wake_schedule 表）、_wake_decision_for_user
    （Task 3 适配器，包一层 proactive_gate，读专用，本身不 enqueue）、enqueue_job("heartbeat")
    /upsert_wake_schedule(next_heartbeat_at=...)。

    leader-election 有意跳过（见 D3 plan Task 5 说明）：`enqueue_job` 走
    ux_agent_jobs_singleflight 分区唯一索引，多个 serve_worker 进程的 scheduler tick
    并发对同一用户各自判定 should_wake 也只会各自 INSERT 一次、第二个撞唯一索引 coalesce
    成同一行——重复调度天然无害。prod 只跑一个 serve-worker 容器，这条不变量目前甚至用
    不上，但即使将来横向扩容也不需要另起一套选主。"""
    return types.SimpleNamespace(
        due_users=lambda: jobs_store.due_heartbeat_users(),
        wake_decision=_wake_decision_for_user,
        enqueue_heartbeat=lambda uid: jobs_store.enqueue_job(uid, "heartbeat"),
        advance_heartbeat=lambda uid, next_at: jobs_store.upsert_wake_schedule(
            uid, next_heartbeat_at=next_at),
    )


def wire_assembly() -> None:
    """复刻 asgi/lifespan.py 的关键接线（本进程无 lifespan）：注入 envelope pubkey getter、
    载入内存 registry、起 wake-bus listener、接上 "v2_jobs" 即时唤醒（FIX 3）。幂等
    （load_users/register_handler/start_listener 均自身幂等；重复调用安全）。

    "v2_jobs"：chat_send_core 入队新 job 后 NOTIFY 这个 channel；`v2_worker.on_v2_job_notify`
    只是把它桥到本进程 event loop 的一个 asyncio.Event（context 由 `_serve` 在 loop 起来后
    经 `v2_worker.set_job_wake_context` 设置——这里只负责注册 handler，不依赖 running loop）。"""
    core_envelope.get_user_public_key = accounts_registry._get_user_public_key
    accounts_registry.load_users()
    core_wake_bus.register_handler("users", lambda _uid: accounts_registry.load_users())
    core_wake_bus.register_handler("v2_jobs", v2_worker.on_v2_job_notify)
    core_wake_bus.start_listener()


def build_health_app():
    """极薄 FastAPI，仅暴露 /healthz 供部署平台存活探针（spec §2.1）。"""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "db": db.healthcheck()}

    return app


_REAP_INTERVAL_SEC = float(os.environ.get("FEEDLING_V2_REAP_INTERVAL_SEC", "30"))


async def _reaper_loop(stop_event: asyncio.Event, *, interval: float = _REAP_INTERVAL_SEC) -> None:
    """周期性回收卡死的 claimed/running job（FIX 1）：若某个 worker 在 claim_next_job 和
    终态 mark_* 之间死掉（进程崩溃/被杀），该 job 会永远卡在 claimed/running——single-flight
    的 partial unique index 会让这个用户之后所有 chat/send 都合并进这个死 job，用户从此
    再也收不到回复、也没有自愈路径。`jobs_store.reap_stuck_jobs()` 早就实现好且有测试
    覆盖，只是没人调用它——这里把它接成一个跟 run_worker_loop 并发跑的周期任务。

    每 ~interval 秒跑一次，interruptible（stop_event 置位时不必等满这个周期就退出，
    drain 更快）；单次 DB 错误只记日志、不杀进程——reaper 本身故障绝不能拖垮整个 worker
    进程（那样反而制造更多卡死 job）。"""
    while not stop_event.is_set():
        try:
            reaped = await asyncio.to_thread(jobs_store.reap_stuck_jobs)
            if reaped:
                log.info("[v2.serve_worker] reaper expired %d stuck job(s)", reaped)
        except Exception as e:  # noqa: BLE001 — 瞬时 DB 错误绝不能杀掉 reaper/worker 进程
            log.warning("[v2.serve_worker] reap_stuck_jobs failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_HEARTBEAT_INTERVAL_SEC = float(os.environ.get("FEEDLING_V2_HEARTBEAT_INTERVAL_SEC", "10"))


async def _heartbeat_loop(
    worker_id: str, stop_event: asyncio.Event, *, interval: float = _HEARTBEAT_INTERVAL_SEC
) -> None:
    """UPSERT this process's liveness row every ~interval seconds (Task 2: the
    db_action_v2 chat/send guard needs something to check — without this, a
    pool where every serve_worker process has died would queue jobs forever
    with no error). Reuses the same ``worker_id`` this process passes to
    ``claim_next_job``/``run_worker_loop`` — one row per live process.

    Emits one heartbeat immediately on startup (before the first sleep) so a
    just-started pool is visible right away rather than only after the first
    full interval. Sleeps in a single ``wait_for(stop_event.wait(), timeout=...)``
    (mirrors ``_reaper_loop``) so ``stop_event.set()`` wakes it immediately
    instead of waiting out the rest of the interval — shutdown must not be
    delayed by a stale/soon-to-expire heartbeat row.
    """
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(jobs_store.record_worker_heartbeat, worker_id)
        except Exception as e:  # noqa: BLE001 — a heartbeat write failure must not kill the worker
            log.warning("[v2.serve_worker] record_worker_heartbeat failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


_SCHEDULER_INTERVAL_SEC = float(os.environ.get("FEEDLING_V2_SCHEDULER_INTERVAL_SEC", "30"))


async def _scheduler_loop(stop_event: asyncio.Event, *, interval: float = _SCHEDULER_INTERVAL_SEC) -> None:
    """周期性跑一遍纯调度器（D3 Task 4 `scheduler.run_scheduler_tick`，Task 5 接线）：
    对每个到期用户判定是否唤醒 heartbeat（经 `_wake_decision_for_user` 复用真实
    proactive gate），should_wake 就 enqueue_job("heartbeat")（single-flight 去重、
    走 Task 2 的 lane 优先级），无论如何都 advance_heartbeat 推进下次到期时间——
    不会同一批用户每个 tick 都重新判一遍。

    镜像 `_reaper_loop`/`_heartbeat_loop` 的结构：interruptible 的
    `wait_for(stop_event.wait(), timeout=interval)`（stop_event 置位立刻醒，drain 不被
    卡满一个周期）；单次异常只记日志，绝不允许冒出去杀掉这个循环或拖垮跟它并发跑的
    run_worker_loop/reaper/heartbeat（scheduler.run_scheduler_tick 本身逐用户已经吞异常，
    这里再兜一层，防的是 deps 装配/DB 连接层面的意外）。

    leader-election 有意跳过——见 `_build_scheduler_deps` 的说明：single-flight 唯一索引
    让重复调度天然无害，prod 只跑一个 serve-worker 容器，不需要另起选主。

    `run_scheduler_tick` 是同步函数（内部只做 dict/list 操作 + 通过 deps 调用同步 DB
    函数），过 `asyncio.to_thread` 挪出 event loop，跟其它 loop 里所有同步 jobs_store
    调用的桥法一致。`now=time.time()`：装配层读墙钟，scheduler.py 自己不摸时钟（保持
    纯/可测）。"""
    while not stop_event.is_set():
        try:
            deps = _build_scheduler_deps()
            result = await asyncio.to_thread(scheduler.run_scheduler_tick, deps, now=time.time())
            if result.get("considered"):
                log.info(
                    "[v2.serve_worker] scheduler tick considered=%s enqueued=%s skipped=%s",
                    result.get("considered"), result.get("enqueued"), result.get("skipped"))
        except Exception as e:  # noqa: BLE001 — 瞬时故障绝不能杀掉 scheduler/worker 进程
            log.warning("[v2.serve_worker] scheduler tick failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _serve(worker_id: str, *, poll_interval: float) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    deps = build_production_deps()
    # "v2_jobs" 即时唤醒（FIX 3）：event 必须在 running loop 里创建/绑定——wire_assembly
    # 本身在 asyncio.run 之前跑，那时还没有 loop 可绑。
    wake_event = asyncio.Event()
    v2_worker.set_job_wake_context(loop, wake_event)
    log.info("[v2.serve_worker] starting worker=%s max_workers=%s", worker_id, v2_worker.MAX_WORKERS)
    await asyncio.gather(
        v2_worker.run_worker_loop(
            worker_id,
            max_workers=v2_worker.MAX_WORKERS,
            poll_interval=poll_interval,
            stop_event=stop_event,
            deps=deps,
            wake_event=wake_event,
        ),
        _reaper_loop(stop_event),
        _heartbeat_loop(worker_id, stop_event),
        _scheduler_loop(stop_event),
        # 防御性纵深（对齐 run_worker_loop 内层 gather）：四个协程内部都已吞尽异常，
        # 但万一某个漏网抛出，return_exceptions 保证不牵连拖垮另一个 loop。
        return_exceptions=True,
    )
    log.info("[v2.serve_worker] drained; exiting worker=%s", worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Schema single-point for this standalone process (idempotent — see
    # db.init_schema docstring). The ASGI backend's gunicorn on_starting also
    # runs this once per deploy; this worker is its own entrypoint in the runner
    # CVM (see module docstring) with no shared master migration hook, so it must
    # not assume the schema is already at head.
    db.init_schema()
    wire_assembly()
    worker_id = os.environ.get("FEEDLING_V2_WORKER_ID", f"v2-worker-{os.getpid()}")
    poll_interval = float(os.environ.get("FEEDLING_V2_POLL_INTERVAL_SEC", "1.0"))
    asyncio.run(_serve(worker_id, poll_interval=poll_interval))


if __name__ == "__main__":
    main()
