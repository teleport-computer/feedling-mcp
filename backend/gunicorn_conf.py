"""gunicorn server config — 生产启动钩子（ASGI FastAPI，``asgi_app:app``）。

``on_starting`` 在 gunicorn master 进程启动时执行一次（worker fork 之前）。两件事：

1. **fail-fast 托管校验**：gateway-only codex 用户依赖 in-CVM LiteLLM gateway，未开则
   拒绝启动而非让请求在运行期 hang（dev 入口 ``serve_dev.py`` 跑同一校验）。
2. **DB migration 单点**（ASGI 迁移计划 §5.2/§8.1/§19.6）：``db.init_schema()``
   （alembic ``upgrade head``）在 master、fork 之前跑一次。这是 schema 升级的唯一
   位置——漏掉 = 新镜像服务旧 schema；放 master 也消除"每 worker 各跑一次 alembic"
   的竞态。

``on_starting`` 是 master 钩子，与 worker class 无关。enclave_app 不路由 chat send、
不加载本 config。"""

import os


def _worker_count() -> int:
    """gunicorn worker (process) count, env-driven (``FEEDLING_BACKEND_WORKERS``,
    default 1, clamped ≥1). The backend already supports ``-w N`` — a cross-worker
    LISTEN/NOTIFY wake bus (``core.wake_bus``) so a write in one worker wakes
    long-poll waiters in the others, and advisory-lock leader election
    (``core.leader.run_singleton``) so only ONE worker binds the :9998 WS server.
    So raising this is a pure config change. Sizing is bounded by Postgres
    max_connections: each worker holds ~16 pool + 1 LISTEN + 1 election ≈ 18
    connections, so -w2 ≈ 36, -w3 ≈ 54 — check the RDS instance's max_connections
    before raising (test t4g-micro = 79 → -w2 safe / -w3 edge)."""
    # `or "1"` guards the empty string: CI passes `-e FEEDLING_BACKEND_WORKERS=$VAR`
    # and an unset GitHub var expands to "", so the key is SET-but-empty — int("")
    # would crash gunicorn config load and the backend would fail to boot.
    return max(1, int((os.environ.get("FEEDLING_BACKEND_WORKERS") or "").strip() or "1"))


# gunicorn reads this module-level name for the worker count (no `-w` on the CLI,
# so the config file wins). Default 1 = unchanged behavior until the env is set.
workers = _worker_count()


# Worker 定期回收：结构性内存上限。2026-07-14 prod 实测：请求处理的分配 churn 散布在
# anyio 线程池 40 线程的 glibc per-thread arena 里（每 worker ~60 个 64MiB arena、
# 近乎全驻留、占 RSS 80%+），12h 涨到 2-3GB/worker 且不自趋稳；CVM 无 swap，
# available < 1000M 就进内核 OOM killer 红线（可能误杀 enclave 容器）。arena 高水位
# 只涨不还，进程回收是唯一可靠的归零手段。
#
# 尺寸是双边约束（2026-07-14 test 实测踩过下限）：
#   - 上限：回收周期须远早于内存红线。prod 每 worker ~15.5 req/s、涨 ~0.2GB/h →
#     50k 请求 ≈ ~54-65min 寿命、峰值 ~0.2-0.3GB/worker，远离红线。
#   - 下限：寿命必须盖过 leader 单例的后台长任务。2000 请求在 prod = **~2 分钟**
#     一次回收——长轮询高频被排空、tee-sync/:9998 WS leader 反复换手，in-process
#     reconcile（数十分钟）永远跑不完（test 实测：部署后 2h 零 tee-sync tick）。
# jitter 必须够大，不只是非零：同批启动的 worker 几乎同速消化请求，jitter 太小
# （10k/50k=20%）时回收簇跨代保持松散同步——2026-07-15 prod 实测 01:14-01:30 的
# 16 分钟里 3/4 worker 相继回收（各自寿命 59-76min 正常，但到期时刻聚成一簇，
# 偶发两个窗口重叠就是瞬时容量减半）。取 max_requests 的一半，相位一两代内就
# 完全去相关。
max_requests = 50000
max_requests_jitter = 25000

# 回收/重启时排空在途请求的窗口。gunicorn 默认 30s，而 /v1/chat/poll 长轮询本身
# 最长 30s——贴着悬崖，回收瞬间正好 park 住的 consumer 会被掐断（它靠重试恢复，
# 但每次回收都制造一批假错误）。120s 让最长的长轮询 + 收尾写从容排空。
graceful_timeout = 120


# Idle timeout for keep-alive connections. gunicorn's default is 2s and
# uvicorn_worker maps this straight onto uvicorn's ``timeout_keep_alive``.
# At 2s the server closes an idle connection while still omitting
# ``Connection: close``, so a pooling client (iOS URLSession) reuses a socket
# the server has already FIN'd — the request dies in transit as
# NSURLErrorNetworkConnectionLost, which the app renders as "网络连接失败".
# That is why the first tap after idling in a form (e.g. the model-API key
# sheet, where picking a provider + model + pasting the key takes far longer
# than 2s) failed and the second one worked. The server's idle timeout must
# outlive the client's pool-reuse window, so use the nginx-style 75s.
# Sizing note: idle connections count toward uvicorn's ``limit_concurrency``
# (2048, see asgi/worker.py) and haproxy's ``maxconn`` (4096) — at current
# user counts that is not close.
keepalive = 75


def on_starting(server):
    # on_starting 跑在 gunicorn master 进程、worker fork 之前。--chdir backend 或
    # WorkingDirectory=backend 的 path 注入时序不保证在此时完成，故自插 backend 目录
    # 到 sys.path，使 hosted 包在任何启动方式（容器 WORKDIR /app + --chdir backend、
    # systemd WorkingDirectory=backend）下均可解析。
    import os
    import sys
    here = os.path.dirname(os.path.abspath(__file__))  # .../backend
    if here not in sys.path:
        sys.path.insert(0, here)
    from hosted import agent_runtime_cutover
    agent_runtime_cutover.assert_hosting_ready()
    # DB migration single-point (master, once, before fork). See module docstring:
    # this is the only place the schema is upgraded — workers never run alembic.
    import db
    db.init_schema()
    from hosted import config_store
    try:
        policy_result = config_store.reconcile_hosted_runtime_policy()
    finally:
        # Policy reconciliation opens db.py's process-local psycopg pool in the
        # Gunicorn master. Stop its threads/connections before workers fork;
        # every child will lazily create a clean pool on first use.
        db.close_pool()
    print(
        "[gunicorn] on_starting: hosting ready + schema init + runtime policy "
        f"done ({policy_result})",
        flush=True,
    )
