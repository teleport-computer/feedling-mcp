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

from accounts import registry as accounts_registry
from agent_runtime import spawners as agent_spawners
from core import enclave as core_enclave
from core import envelope as core_envelope
from core import runtime_token
from core import store as core_store
from core import wake_bus as core_wake_bus
from hosted import config_store as hosted_config_store
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker as v2_worker
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
        # 防御性纵深（对齐 run_worker_loop 内层 gather）：三个协程内部都已吞尽异常，
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
