def test_keepalive_outlives_client_connection_pools():
    """gunicorn's ``keepalive`` default is 2s, and uvicorn_worker maps it straight
    onto ``timeout_keep_alive``. At 2s the server closes an idle connection while
    still omitting ``Connection: close`` — so a pooling client (iOS URLSession)
    reuses a socket the server has already FIN'd and the request dies in transit
    (NSURLErrorNetworkConnectionLost, surfaced as "网络连接失败"). Symptom: the
    first tap on any POST after the user idles in a form fails, the second works.

    The invariant: the server's idle timeout must comfortably outlive a client's
    connection-pool reuse window, not undercut it.
    """
    import importlib

    gconf = importlib.import_module("gunicorn_conf")
    assert getattr(gconf, "keepalive", 2) >= 60


def test_on_starting_calls_assert_hosting_ready(monkeypatch):
    import sys, os, importlib

    # Import gunicorn_conf (backend is on sys.path via PYTHONPATH=. when pytest
    # runs from the backend dir — no manual pre-injection here).
    gconf = importlib.import_module("gunicorn_conf")
    here = os.path.dirname(os.path.abspath(gconf.__file__))

    # Simulate the condition on_starting must handle: backend not yet in sys.path
    # (gunicorn master starts before --chdir has a chance to inject the path).
    saved = sys.path[:]
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != here]
    try:
        called = {}
        monkeypatch.setattr("hosted.agent_runtime_cutover.assert_hosting_ready",
                            lambda: called.setdefault("x", True))
        gconf.on_starting(None)
        assert called.get("x") is True
        # Hardening check: on_starting must have re-inserted backend into sys.path.
        assert here in [os.path.abspath(p) for p in sys.path]
    finally:
        sys.path[:] = saved


def test_worker_recycling_bounds_arena_growth():
    """backend worker 的 glibc arena 会随请求 churn 无界膨胀（2026-07-14 prod
    实测：每 worker ~60 个 64MiB arena 占 RSS 80%+，12h 涨到 2-3GB/worker，CVM
    无 swap、available<1000M 即 OOM killer 红线）。worker 定期回收是结构性上限：
    max_requests 到点回收 + jitter 防四个 worker 同时回收。"""
    import importlib

    gconf = importlib.import_module("gunicorn_conf")
    assert getattr(gconf, "max_requests", 0) >= 1000
    # 无 jitter = 同批启动的 worker 几乎同时到阈值同时回收 → 服务闪断。而且
    # jitter 太小（如 10k/50k=20%）会让同批 worker 的回收簇跨代保持松散同步
    # （2026-07-15 prod 实测：01:14-01:30 十六分钟内 3/4 worker 相继回收）。
    # ≥ max_requests 的一半才能让相位在一两代内充分去相关。
    assert getattr(gconf, "max_requests_jitter", 0) >= 20000
    # 下限同样重要：prod 每 worker ~15.5 req/s，max_requests=2000 意味着 ~2 分钟
    # 就回收一次——长轮询高频被排空、leader 单例(tee-sync/:9998 WS)反复换手，
    # 后台 reconcile 永远跑不完(2026-07-14 test 实测:部署后 2h 零 tee-sync tick)。
    # 目标 worker 寿命 ≥ ~1h → ≥ 50k 请求。
    assert getattr(gconf, "max_requests", 0) >= 50000


def test_graceful_timeout_outlives_long_poll():
    """回收 worker 时要排空在途请求；/v1/chat/poll 长轮询最长 30s，graceful_timeout
    低于它就会掐断等待中的 consumer（默认 30s 是贴着悬崖）。"""
    import importlib

    gconf = importlib.import_module("gunicorn_conf")
    assert getattr(gconf, "graceful_timeout", 30) >= 60


def test_backend_compose_caps_malloc_arenas():
    """MALLOC_ARENA_MAX 把 glibc per-thread arena 数量封顶（不设时 64 位默认
    8×核数=64 个，正是 prod 实测膨胀形态）。prod 与 test 的 backend 服务都必须带。"""
    import pathlib

    import yaml

    for name in ("docker-compose.phala.yaml", "docker-compose.phala.test.yaml"):
        compose = yaml.safe_load(
            (pathlib.Path(__file__).parent.parent / "deploy" / name).read_text())
        env = compose["services"]["backend"]["environment"]
        assert "MALLOC_ARENA_MAX" in env, f"{name} backend 缺 MALLOC_ARENA_MAX"
        assert str(env["MALLOC_ARENA_MAX"]).strip('"') in {"2", "4"}
