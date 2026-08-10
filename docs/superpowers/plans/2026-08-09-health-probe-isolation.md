# 健康探针隔离实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/healthz` 和 `/healthz/runner` 使用独立双线程执行池，并通过数据库连接、SQL 和路由三级硬超时，确保业务 ASGI 阻塞线程池饱和时仍在三秒内返回结构化响应。

**Architecture:** 新建进程内 `asgi.health_executor`，只承载公开健康检查的同步工作；普通业务继续使用现有 AnyIO 线程池。`db.py` 为健康查询提供可选的一秒连接获取超时和事务内一秒 `statement_timeout`，两个路由再以三秒外层截止时间包住完整调用，并把截止时间映射为不泄密的 503。

**Tech Stack:** Python 3.12、FastAPI/Starlette、Gunicorn、`concurrent.futures.ThreadPoolExecutor`、AnyIO、Psycopg 3、pytest、httpx ASGITransport、MkDocs/公开 OpenAPI 生成工具。

## Global Constraints

- 每个 Gunicorn worker 的专用健康执行池固定为两个线程；不得把普通业务任务提交到该执行池。
- 数据库连接获取超时固定为 `1.0` 秒，事务内 PostgreSQL `statement_timeout` 固定为 `1000` 毫秒，路由总截止时间固定为 `3.0` 秒。
- `/healthz` 成功、降级和现有 DB 故障响应契约保持不变；新截止时间故障使用 `health_check_timeout`。
- `/healthz/runner` 的 runner 数量判定和聚合隐私契约保持不变；新截止时间故障使用 `runner_health_check_timeout`。
- 外部 health-server 的 15 秒请求超时、60 秒间隔和连续三次失败阈值保持不变。
- 不调整普通 ASGI 线程池、Gunicorn worker 数量、Psycopg pool 大小、部署端口或容器拓扑。
- 不增加数据库迁移、持久状态、密钥或运行时依赖。
- 所有新测试位于 `tests/`；纯单测文件必须加入 `tests/conftest.py::_PURE_UNIT`，避免无 PostgreSQL 环境静默漏跑。
- 模块调用遵守仓库规则：使用 `from asgi import health_executor` 和 `health_executor.run(...)`，不绑定裸函数。
- 实施默认面向 `test` 分支；不得绕过 test 环境直接向 `main` 推送。

---

## 文件结构

- 新建 `backend/asgi/health_executor.py`：进程内双线程执行池、三秒默认截止时间和稳定超时异常；不包含路由或数据库逻辑。
- 修改 `backend/db.py`：健康查询常量、事务内 statement timeout 上下文，以及两个现有查询的可选健康专用超时参数。
- 修改 `backend/asgi/health.py`：通过专用执行池收集检查，并构造 `/healthz` 截止时间 503。
- 修改 `backend/asgi/runner_health.py`：通过专用执行池查询心跳，并构造 runner 截止时间 503。
- 新建 `tests/test_health_executor.py`：执行池双槽并发和外层截止时间纯单测。
- 新建 `tests/test_db_health_timeouts.py`：连接参数、事务内 SQL 超时和默认兼容行为纯单测。
- 新建 `tests/test_health_route_isolation.py`：占满普通 AnyIO 线程池后的双路由回归，以及两个公开超时响应契约。
- 修改 `tests/test_asgi_runner_health.py`：现有 DB fake 接受新增健康专用关键字参数。
- 修改 `tests/conftest.py`：登记三个新纯单测文件。
- 修改 `tools/public_openapi_contracts.py`：公开 503 描述加入健康检查截止时间语义。
- 修改 `tests/openapi/test_public_openapi.py`：锁定新增的公开截止时间描述。
- 生成 `docs-site/openapi/public.json`：同步公开 OpenAPI 静态产物。
- 修改 `docs-site/content/docs/architecture.mdx`：记录健康检查容量隔离和三级超时。
- 修改 `docs-site/content/docs/changelog.mdx`：在 `Unreleased` 记录行为变化。

---

### Task 1: 建立专用健康检查执行器

**Files:**
- Create: `backend/asgi/health_executor.py`
- Create: `tests/test_health_executor.py`
- Modify: `tests/conftest.py:107-215`

**Interfaces:**
- Consumes: Python 标准库 `asyncio`、`concurrent.futures.ThreadPoolExecutor`、`functools.partial`。
- Produces: `HEALTH_CHECK_DEADLINE_SECONDS: float = 3.0`、`HealthCheckTimeout`、`async run(fn, /, *args, deadline_seconds=3.0, **kwargs) -> T`。

- [ ] **Step 1: 先写执行器失败测试**

新建 `tests/test_health_executor.py`：

```python
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi import health_executor


def test_health_executor_runs_two_checks_concurrently():
    barrier = threading.Barrier(2)

    def check(value: str) -> str:
        barrier.wait(timeout=1.0)
        return value

    async def go():
        return await asyncio.gather(
            health_executor.run(check, "api"),
            health_executor.run(check, "runner"),
        )

    assert asyncio.run(go()) == ["api", "runner"]


def test_health_executor_maps_outer_deadline_to_stable_exception():
    started = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=1.0)

    async def go():
        task = asyncio.create_task(
            health_executor.run(blocked, deadline_seconds=0.01)
        )
        while not started.is_set():
            await asyncio.sleep(0)
        with pytest.raises(health_executor.HealthCheckTimeout):
            await task

    try:
        asyncio.run(go())
    finally:
        release.set()
```

同时把 `"test_health_executor.py"` 加进 `tests/conftest.py` 的 `_PURE_UNIT` 集合。

- [ ] **Step 2: 运行测试，确认红灯原因正确**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_health_executor.py -q
```

Expected: FAIL during collection with `ImportError` because `asgi.health_executor` does not exist.

- [ ] **Step 3: 写最小执行器实现**

新建 `backend/asgi/health_executor.py`：

```python
"""Health-only blocking executor, isolated from ordinary ASGI work."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

HEALTH_CHECK_DEADLINE_SECONDS = 3.0
_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="feedling-health",
)


class HealthCheckTimeout(RuntimeError):
    """The health callable did not finish before its route deadline."""


async def run(
    fn: Callable[P, T],
    /,
    *args: P.args,
    deadline_seconds: float = HEALTH_CHECK_DEADLINE_SECONDS,
    **kwargs: P.kwargs,
) -> T:
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, partial(fn, *args, **kwargs))
    try:
        return await asyncio.wait_for(future, timeout=deadline_seconds)
    except TimeoutError as exc:
        raise HealthCheckTimeout("health check deadline exceeded") from exc
```

不要在这里 import `db`、FastAPI 或任何业务包。

- [ ] **Step 4: 运行执行器测试，确认绿灯**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_health_executor.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: 运行静态检查**

Run:

```bash
.venv-test/bin/python -m pyflakes backend/asgi/health_executor.py tests/test_health_executor.py
git diff --check
```

Expected: both commands exit 0 with no output.

- [ ] **Step 6: 提交独立执行器**

```bash
git add backend/asgi/health_executor.py tests/test_health_executor.py tests/conftest.py
git commit -m "feat: add dedicated health executor"
```

---

### Task 2: 为健康数据库查询增加连接与语句硬超时

**Files:**
- Modify: `backend/db.py:53-105,162-181,365-394`
- Create: `tests/test_db_health_timeouts.py`
- Modify: `tests/conftest.py:107-216`

**Interfaces:**
- Consumes: Task 1 不提供运行时依赖；本任务只扩展现有 `db.py` 接口。
- Produces: `HEALTH_DB_ACQUIRE_TIMEOUT_SECONDS = 1.0`、`HEALTH_DB_STATEMENT_TIMEOUT_MS = 1000`、`health_probe(timeout=2.0, *, statement_timeout_ms=None) -> dict`、`list_supervisor_instance_heartbeats(*, timeout=None, statement_timeout_ms=None) -> list[dict]`。

- [ ] **Step 1: 写数据库超时失败测试**

新建 `tests/test_db_health_timeouts.py`，使用 fake pool，不访问真实数据库：

```python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return _Context(self)

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return _Rows(self.rows)


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.connection_calls = []

    def connection(self, **kwargs):
        self.connection_calls.append(kwargs)
        return _Context(self.conn)


def test_health_probe_bounds_acquire_and_statement_timeout(monkeypatch):
    conn = _Connection()
    pool = _Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    result = db.health_probe(timeout=1.0, statement_timeout_ms=1000)

    assert result["ok"] is True
    assert pool.connection_calls == [{"timeout": 1.0}]
    assert conn.transactions == 1
    assert conn.calls[0] == (
        "SELECT set_config('statement_timeout', %s, true)",
        ("1000ms",),
    )
    assert conn.calls[1][0] == "SELECT 1"


def test_runner_heartbeat_health_path_uses_same_bounds(monkeypatch):
    row = ("runner-a", "host", 0, 1, 0, 0, True, False, "v", 995.0, {})
    conn = _Connection([row])
    pool = _Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    rows = db.list_supervisor_instance_heartbeats(
        timeout=1.0,
        statement_timeout_ms=1000,
    )

    assert rows[0]["owner"] == "runner-a"
    assert pool.connection_calls == [{"timeout": 1.0}]
    assert conn.transactions == 1
    assert conn.calls[0][1] == ("1000ms",)


def test_runner_heartbeat_default_path_preserves_pool_defaults(monkeypatch):
    conn = _Connection([])
    pool = _Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    assert db.list_supervisor_instance_heartbeats() == []

    assert pool.connection_calls == [{}]
    assert conn.transactions == 0
    assert all("set_config" not in sql for sql, _params in conn.calls)
```

把 `"test_db_health_timeouts.py"` 加进 `_PURE_UNIT`。

- [ ] **Step 2: 运行测试，确认因新参数不存在而失败**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_db_health_timeouts.py -q
```

Expected: FAIL with `TypeError` mentioning unexpected `statement_timeout_ms`.

- [ ] **Step 3: 在 `db.py` 增加健康查询常量和事务内超时上下文**

在连接池常量附近增加：

```python
HEALTH_DB_ACQUIRE_TIMEOUT_SECONDS = 1.0
HEALTH_DB_STATEMENT_TIMEOUT_MS = 1000
```

在 `health_probe` 前增加：

```python
@contextmanager
def _local_statement_timeout(conn, timeout_ms: int | None):
    if timeout_ms is None:
        yield
        return
    with conn.transaction():
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{int(timeout_ms)}ms",),
        )
        yield
```

必须保留显式事务：生产连接池使用 `autocommit=True`；若不打开事务，`is_local=true` 的设置会在 `set_config` 语句结束后立即失效。

- [ ] **Step 4: 扩展 `health_probe`，保持默认调用兼容**

将签名和查询改成：

```python
def health_probe(
    timeout: float = 2.0,
    *,
    statement_timeout_ms: int | None = None,
) -> dict:
    t0 = time.perf_counter()
    try:
        with get_pool().connection(timeout=timeout) as conn:
            with _local_statement_timeout(conn, statement_timeout_ms):
                conn.execute("SELECT 1")
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 — health must never raise
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": str(e)[:200],
        }
```

- [ ] **Step 5: 扩展 runner 心跳查询的可选健康路径**

保持默认不传 pool timeout、不设置 statement timeout：

```python
def list_supervisor_instance_heartbeats(
    *,
    timeout: float | None = None,
    statement_timeout_ms: int | None = None,
) -> list[dict]:
    connection_kwargs = {"timeout": timeout} if timeout is not None else {}
    with get_pool().connection(**connection_kwargs) as conn:
        with _local_statement_timeout(conn, statement_timeout_ms):
            rows = conn.execute(
                "SELECT owner, host, shard_index, shard_count, max_children, "
                "       active_children, host_all, gateway, version, "
                "       extract(epoch FROM updated_at) AS ts, payload "
                "FROM agent_runtime_supervisor_heartbeats"
            ).fetchall()
    out = []
    for r in rows:
        # ``pi`` 没有独立列，只存在 payload 中；必须继续向 runner guard 暴露。
        payload = r[10] if isinstance(r[10], dict) else {}
        out.append({
            "owner": r[0],
            "host": r[1],
            "shard_index": r[2],
            "shard_count": r[3],
            "max_children": r[4],
            "active_children": r[5],
            "host_all": bool(r[6]),
            "gateway": bool(r[7]),
            "version": r[8],
            "ts": float(r[9]),
            "pi": bool(payload.get("pi")),
        })
    return out
```

- [ ] **Step 6: 跑新旧 DB 测试**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_db_health_timeouts.py \
  tests/test_asgi_healthz.py \
  tests/test_asgi_runner_health.py -q
```

Expected: all selected tests pass.

- [ ] **Step 7: 静态检查并提交**

```bash
.venv-test/bin/python -m pyflakes backend/db.py tests/test_db_health_timeouts.py
git diff --check
git add backend/db.py tests/test_db_health_timeouts.py tests/conftest.py
git commit -m "fix: bound health database queries"
```

Expected: checks exit 0; commit contains no route changes.

---

### Task 3: 将两个公开健康路由切到专用执行池

**Files:**
- Modify: `backend/asgi/health.py:29-150`
- Modify: `backend/asgi/runner_health.py:1-110`
- Modify: `tests/test_asgi_runner_health.py:48-130`
- Create: `tests/test_health_route_isolation.py`
- Modify: `tests/conftest.py:107-217`

**Interfaces:**
- Consumes: Task 1 的 `health_executor.run`、`HealthCheckTimeout`；Task 2 的两个 DB 超时常量及两个扩展查询接口。
- Produces: `/healthz` 的 `health_check_timeout` 503；`/healthz/runner` 的 `runner_health_check_timeout` 503；业务线程池饱和回归测试。

- [ ] **Step 1: 先写公开超时响应失败测试**

新建 `tests/test_health_route_isolation.py` 的基础装配和超时测试：

```python
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import asgi_app
from asgi import health_executor


async def _get(path: str):
    transport = httpx.ASGITransport(app=asgi_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


def test_health_routes_map_dedicated_deadline_to_structured_503(monkeypatch):
    async def exceed_deadline(*_args, **_kwargs):
        raise health_executor.HealthCheckTimeout("test deadline")

    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")
    monkeypatch.setattr(health_executor, "run", exceed_deadline)

    async def go():
        return await asyncio.gather(_get("/healthz"), _get("/healthz/runner"))

    api, runner = asyncio.run(go())

    assert api.status_code == 503
    assert api.json()["ok"] is False
    assert api.json()["status"] == "unhealthy"
    assert api.json()["checks"] == {
        "db": {"status": "down", "error": "health_check_timeout"},
    }
    assert set(api.json()) >= {"mode", "release", "uptime_s", "worker"}

    assert runner.status_code == 503
    assert runner.json()["checks"]["runner_fleet"]["reason"] == (
        "runner_health_check_timeout"
    )
```

把 `"test_health_route_isolation.py"` 加进 `_PURE_UNIT`。

- [ ] **Step 2: 运行超时测试，确认当前路由未调用专用执行器**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_health_route_isolation.py::test_health_routes_map_dedicated_deadline_to_structured_503 -q
```

Expected: FAIL because monkeypatched `health_executor.run` is never called and routes do not return the new timeout codes.

- [ ] **Step 3: 修改 `/healthz` 使用专用执行池**

在 `backend/asgi/health.py` 使用模块 import：

```python
from asgi import health_executor
```

让 `_gather_checks()` 调用 Task 2 的硬超时：

```python
probe = db.health_probe(
    timeout=db.HEALTH_DB_ACQUIRE_TIMEOUT_SECONDS,
    statement_timeout_ms=db.HEALTH_DB_STATEMENT_TIMEOUT_MS,
)
```

增加稳定 503 构造器：

```python
def _deadline_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "mode": "multi_tenant",
            "status": "unhealthy",
            "release": _release(),
            "uptime_s": round(time.time() - _STARTED_AT, 1),
            "worker": _worker(),
            "checks": {
                "db": {"status": "down", "error": "health_check_timeout"},
            },
        },
        status_code=503,
    )
```

路由入口改成：

```python
@router.get("/healthz")
async def healthz():
    try:
        checks = await health_executor.run(_gather_checks)
    except health_executor.HealthCheckTimeout:
        return _deadline_response()

    critical_ok = checks["db"]["status"] == "ok"
    degraded = (
        checks["db_pool"].get("status") == "saturated"
        or checks["registry"].get("status") == "empty"
        or checks["wake_bus"].get("status") == "not_listening"
    )
    if not critical_ok:
        status = "unhealthy"
    elif degraded:
        status = "degraded"
    else:
        status = "healthy"

    body = {
        "ok": critical_ok,
        "mode": "multi_tenant",
        "status": status,
        "release": _release(),
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "worker": _worker(),
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if critical_ok else 503)
```

- [ ] **Step 4: 修改 runner 路由使用专用执行池**

在 `backend/asgi/runner_health.py` 增加同样的模块 import，然后替换查询：

```python
try:
    instances = await health_executor.run(
        db.list_supervisor_instance_heartbeats,
        timeout=db.HEALTH_DB_ACQUIRE_TIMEOUT_SECONDS,
        statement_timeout_ms=db.HEALTH_DB_STATEMENT_TIMEOUT_MS,
    )
except health_executor.HealthCheckTimeout:
    return _unhealthy("runner_health_check_timeout", expected=expected)
except Exception:  # noqa: BLE001 - a probe must never expose DB internals
    logger.exception("runner health heartbeat query failed")
    return _unhealthy("runner_health_check_error", expected=expected)
```

更新 `tests/test_asgi_runner_health.py` 里 monkeypatch 的查询 fake，使其接受 `**_kwargs`，例如：

```python
monkeypatch.setattr(
    db,
    "list_supervisor_instance_heartbeats",
    lambda **_kwargs: [{"ts": 995.0, "host_all": True, "owner": "private-owner"}],
)
```

异常 fake 同样改成 `def raise_db_error(**_kwargs): ...`。不得在路由模块绑定 DB 裸函数，否则这些 monkeypatch 会失效。

- [ ] **Step 5: 跑超时契约及现有路由测试**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_health_route_isolation.py::test_health_routes_map_dedicated_deadline_to_structured_503 \
  tests/test_asgi_healthz.py \
  tests/test_asgi_runner_health.py -q
```

Expected: all selected tests pass; existing success and failure bodies remain green.

- [ ] **Step 6: 写普通线程池饱和回归测试**

在 `tests/test_health_route_isolation.py` 增加：

```python
import threading

import anyio.to_thread
import db
from accounts import registry
from asgi import runner_health
from core import wake_bus


class _PoolStats:
    def get_stats(self):
        return {
            "pool_size": 6,
            "pool_available": 5,
            "requests_waiting": 0,
            "pool_max": 16,
        }


def test_health_routes_ignore_saturated_ordinary_threadpool(monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_RUNNER_COUNT", "1")
    monkeypatch.setattr(
        db,
        "health_probe",
        lambda **_kwargs: {"ok": True, "latency_ms": 1.0, "error": None},
    )
    monkeypatch.setattr(db, "get_pool", lambda: _PoolStats())
    monkeypatch.setattr(
        db,
        "list_supervisor_instance_heartbeats",
        lambda **_kwargs: [{"ts": 995.0, "host_all": True}],
    )
    monkeypatch.setattr(registry, "_users", [{"user_id": "u1"}])
    monkeypatch.setattr(wake_bus, "_enabled", lambda: True)
    monkeypatch.setattr(wake_bus, "_listener_started", True)
    monkeypatch.setattr(runner_health.time, "time", lambda: 1000.0)

    entered = threading.Event()
    release = threading.Event()

    def occupy_ordinary_pool() -> None:
        entered.set()
        release.wait(timeout=2.0)

    async def go():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        blocker = asyncio.create_task(anyio.to_thread.run_sync(occupy_ordinary_pool))
        while not entered.is_set():
            await asyncio.sleep(0)
        try:
            transport = httpx.ASGITransport(app=asgi_app.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://t",
            ) as client:
                return await asyncio.wait_for(
                    asyncio.gather(
                        client.get("/healthz"),
                        client.get("/healthz/runner"),
                    ),
                    timeout=1.0,
                )
        finally:
            release.set()
            await blocker
            limiter.total_tokens = original_tokens

    api, runner = asyncio.run(go())
    assert api.status_code == 200
    assert runner.status_code == 200
```

- [ ] **Step 7: 运行隔离验收测试，确认红绿结果**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_health_executor.py \
  tests/test_db_health_timeouts.py \
  tests/test_health_route_isolation.py \
  tests/test_asgi_healthz.py \
  tests/test_asgi_runner_health.py -q
```

Expected: all selected tests pass; saturation test completes below its one-second test guard, independently of the production three-second deadline.

- [ ] **Step 8: 静态检查并提交路由隔离**

```bash
.venv-test/bin/python -m pyflakes \
  backend/asgi/health.py \
  backend/asgi/runner_health.py \
  tests/test_health_route_isolation.py \
  tests/test_asgi_runner_health.py
git diff --check
git add \
  backend/asgi/health.py \
  backend/asgi/runner_health.py \
  tests/test_health_route_isolation.py \
  tests/test_asgi_runner_health.py \
  tests/conftest.py
git commit -m "fix: isolate public health routes"
```

Expected: checks exit 0; commit does not modify monitoring configuration.

---

### Task 4: 同步公开契约与架构文档

**Files:**
- Modify: `tools/public_openapi_contracts.py:2364-2390`
- Modify: `tests/openapi/test_public_openapi.py:676-690`
- Modify: `docs-site/openapi/public.json:113-190` (generated)
- Modify: `docs-site/content/docs/architecture.mdx:509-518`
- Modify: `docs-site/content/docs/changelog.mdx:11-12`

**Interfaces:**
- Consumes: Task 3 的两个稳定公开错误码和三秒总截止行为。
- Produces: 与运行时一致的公开 503 描述、架构说明、Unreleased 记录和生成后的 OpenAPI 静态文件。

- [ ] **Step 1: 先增强公开 OpenAPI 契约测试**

在 `tests/openapi/test_public_openapi.py` 的 runner 503 测试旁增加：

```python
def test_health_503_descriptions_cover_bounded_probe_deadlines(
    operations: dict[tuple[str, str], dict[str, Any]],
) -> None:
    api_description = operations[("get", "/healthz")]["responses"]["503"]["description"]
    runner_description = operations[("get", "/healthz/runner")]["responses"]["503"]["description"]

    assert "three-second health-check deadline" in api_description
    assert "three-second health-check deadline" in runner_description
```

- [ ] **Step 2: 运行契约测试，确认描述尚未覆盖截止时间**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/openapi/test_public_openapi.py::test_health_503_descriptions_cover_bounded_probe_deadlines -q
```

Expected: FAIL because both existing descriptions omit `three-second health-check deadline`.

- [ ] **Step 3: 更新公开 503 描述**

在 `tools/public_openapi_contracts.py` 保留现有 schema 引用，只扩充 description：

```python
("get", "/healthz"): {
    "503": {
        "description": (
            "A critical dependency is unavailable, or the isolated probe "
            "exceeded its three-second health-check deadline. PostgreSQL "
            "connection acquisition and health SQL are independently bounded. "
            "The body has the same shape as the 200 response, with top-level "
            "\"status\": \"unhealthy\" and the failing entry under \"checks\"."
        ),
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/GenericJsonResponse"}}},
    },
},
```

Runner 描述同样加入 `three-second health-check deadline`，并保留“aggregate、同 200 shape、runner_fleet check”语义。

- [ ] **Step 4: 更新架构页和 Unreleased changelog**

在 `docs-site/content/docs/architecture.mdx` 的 Health 段增加：

```mdx
Health database work runs on a two-slot executor reserved per backend worker,
separate from ordinary ASGI blocking work. Connection acquisition and health
SQL each have a one-second bound, and the complete route has a three-second
deadline, so business-thread saturation cannot leave the public probe queued
until an external monitor times out.
```

在 `docs-site/content/docs/changelog.mdx` 的 `## Unreleased` 顶部增加：

```mdx
- **Public health probes keep reserved capacity under backend load.**
  `GET /healthz` and `GET /healthz/runner` now run their blocking database work
  outside the ordinary ASGI thread pool, with bounded connection, SQL, and
  three-second route deadlines. A real deadline failure returns a structured
  HTTP 503 instead of leaving external monitors to time out after 15 seconds.
```

- [ ] **Step 5: 生成公开 OpenAPI 并跑契约测试**

Run:

```bash
cd docs-site && npm run openapi:generate
cd ..
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/openapi/test_public_openapi.py -q
```

Expected: OpenAPI generation exits 0; all public contract tests pass; generated diff only changes the two health 503 descriptions.

- [ ] **Step 6: 验证文档站并提交**

Run:

```bash
cd docs-site
npm run types:check
npm run lint
npm run build
cd ..
git diff --check
```

Expected: all four commands exit 0.

```bash
git add \
  tools/public_openapi_contracts.py \
  tests/openapi/test_public_openapi.py \
  docs-site/openapi/public.json \
  docs-site/content/docs/architecture.mdx \
  docs-site/content/docs/changelog.mdx
git commit -m "docs: document isolated health probes"
```

---

## Final Verification

- [ ] **Step 1: 运行全部定向回归**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_health_executor.py \
  tests/test_db_health_timeouts.py \
  tests/test_health_route_isolation.py \
  tests/test_asgi_healthz.py \
  tests/test_asgi_runner_health.py \
  tests/openapi/test_public_openapi.py -q
```

Expected: all selected tests pass with zero failures and zero errors.

- [ ] **Step 2: 运行后端完整测试套件**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests -q
```

Expected: exit 0 with zero failures and zero errors. Do not treat skipped collection caused by an unavailable PostgreSQL as a full-suite pass; verify the output includes the PostgreSQL-backed suites.

- [ ] **Step 3: 检查最终 diff、提交序列和工作区**

```bash
git diff --check
git status --short
git log --oneline -6
```

Expected: `git diff --check` has no output, `git status --short` is empty, and the log contains the four implementation commits after the design/plan commits.

- [ ] **Step 4: 在 test 环境做部署后只读验收**

部署到 test 后并发请求两个端点：

```bash
curl -sS --max-time 5 https://test-api.feedling.app/healthz
curl -sS --max-time 5 https://test-api.feedling.app/healthz/runner
```

Expected: both return within five seconds; healthy dependencies return HTTP 200; `/healthz.release.git_commit` equals the deployed target commit. Do not promote to `main` until this evidence is recorded.
