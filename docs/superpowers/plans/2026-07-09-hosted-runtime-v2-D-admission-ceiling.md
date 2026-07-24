# §6 Admission Ceiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** send 路径在持久化前估算排队等待时长，超 SLA 则返回独立 busy 响应且不落库。

**Architecture:** 纯函数 `admission.estimate_wait_sec`/`should_admit`（住 `v2/`）+ 三个纯读 `jobs_store` 查询喂它 + `chat_send_core` 在存活闸之后 persist 之前调用，任何异常 fail-open 放行。

**Tech Stack:** Python，psycopg（jobs_store `_pool()` autocommit），pytest，Docker PG `127.0.0.1:55432`。

## Global Constraints

- **NO-COMMIT**：全程不 `git commit`、不 `git add`。实现完成后停手，用户自己提交。
- **worktree**：只在 `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2` 工作，绝不碰主 checkout（`pre` 分支）。每 task 核文件落点。
- **依赖方向**（`tests/test_v2_dependency_direction.py`）：`backend/model_api_runtime/v2/*` 不得 import `hosted`/`agent_runtime`。`admission.py` 保持纯（无 DB、无 hosted import）。
- **fail-open**：admission 计算的任何异常 → admit（放行），绝不阻断用户。
- **busy 响应**：`({"error": "busy", "reason": "queue_over_sla", "est_wait_sec": <int>}, 503)`，不用 429。
- **常量默认**：`SLA_SEC=60.0`（env `V2_ADMISSION_SLA_SEC`）、`DEFAULT_SERVICE_SEC=20.0`（env `V2_ADMISSION_DEFAULT_SERVICE_SEC`）、`SERVICE_SAMPLE_N=50`（env `V2_ADMISSION_SAMPLE_N`）、心跳窗口 `within_sec=30`。
- **测试基线**：7 个 pre-existing 失败与本工作无关，不得新增回归。

---

### Task 1: 纯 admission 模块

**Files:**
- Create: `backend/model_api_runtime/v2/admission.py`
- Test: `tests/test_v2_admission.py`

**Interfaces:**
- Produces:
  - `estimate_wait_sec(*, inflight: int, workers: int, mean_service_sec: float | None, default_service_sec: float) -> float`
  - `should_admit(est_wait_sec: float, *, sla_sec: float) -> bool`
  - 模块常量 `SLA_SEC: float`、`DEFAULT_SERVICE_SEC: float`、`SERVICE_SAMPLE_N: int`（各读对应 env，带默认）

- [ ] **Step 1: 写失败测试** `tests/test_v2_admission.py`

```python
import math
from model_api_runtime.v2 import admission


def test_estimate_uses_default_when_no_history():
    # 2 在飞, 1 worker, 无历史 → ceil(2/1)*20 = 40
    assert admission.estimate_wait_sec(
        inflight=2, workers=1, mean_service_sec=None, default_service_sec=20.0
    ) == 40.0


def test_estimate_uses_rolling_mean_when_present():
    # 4 在飞, 2 worker, 均服务 15 → ceil(4/2)*15 = 30
    assert admission.estimate_wait_sec(
        inflight=4, workers=2, mean_service_sec=15.0, default_service_sec=20.0
    ) == 30.0


def test_estimate_ceils_partial_batch():
    # 3 在飞, 2 worker → ceil(3/2)=2 批 → 2*10 = 20
    assert admission.estimate_wait_sec(
        inflight=3, workers=2, mean_service_sec=10.0, default_service_sec=20.0
    ) == 20.0


def test_estimate_zero_inflight_is_zero_wait():
    assert admission.estimate_wait_sec(
        inflight=0, workers=1, mean_service_sec=15.0, default_service_sec=20.0
    ) == 0.0


def test_estimate_zero_or_negative_workers_never_divides_by_zero():
    # 防御：workers<=0 → 返回 0（等价放行，交给上游存活闸）
    assert admission.estimate_wait_sec(
        inflight=5, workers=0, mean_service_sec=15.0, default_service_sec=20.0
    ) == 0.0


def test_should_admit_boundary_equal_sla_admits():
    assert admission.should_admit(60.0, sla_sec=60.0) is True


def test_should_admit_over_sla_rejects():
    assert admission.should_admit(60.1, sla_sec=60.0) is False


def test_should_admit_under_sla_admits():
    assert admission.should_admit(0.0, sla_sec=60.0) is True


def test_module_constants_have_documented_defaults(monkeypatch):
    import importlib
    monkeypatch.delenv("V2_ADMISSION_SLA_SEC", raising=False)
    monkeypatch.delenv("V2_ADMISSION_DEFAULT_SERVICE_SEC", raising=False)
    monkeypatch.delenv("V2_ADMISSION_SAMPLE_N", raising=False)
    mod = importlib.reload(admission)
    assert mod.SLA_SEC == 60.0
    assert mod.DEFAULT_SERVICE_SEC == 20.0
    assert mod.SERVICE_SAMPLE_N == 50
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_admission.py -q`
Expected: FAIL（`No module named 'model_api_runtime.v2.admission'`）

- [ ] **Step 3: 写实现** `backend/model_api_runtime/v2/admission.py`

```python
"""§6 admission ceiling —— 纯函数：估算 send 排队等待、判定是否放行。

无 DB、无 hosted import（守依赖方向）。DB 读由 jobs_store 提供、chat_send_core 注入。
当前 prod 用户量极小，此闸是安全阀、几乎不触发；刻意保持最小近似（不按 priority 加权）。
"""
from __future__ import annotations

import math
import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


SLA_SEC: float = _env_float("V2_ADMISSION_SLA_SEC", 60.0)
DEFAULT_SERVICE_SEC: float = _env_float("V2_ADMISSION_DEFAULT_SERVICE_SEC", 20.0)
SERVICE_SAMPLE_N: int = _env_int("V2_ADMISSION_SAMPLE_N", 50)


def estimate_wait_sec(
    *,
    inflight: int,
    workers: int,
    mean_service_sec: float | None,
    default_service_sec: float,
) -> float:
    """est-wait = ceil(inflight / workers) × 服务时长。

    workers<=0 → 0（防除零；供给死交给上游存活闸，不在这里拦）。
    inflight<=0 → 0。mean_service_sec None → 用 default_service_sec。
    """
    if workers <= 0 or inflight <= 0:
        return 0.0
    service = mean_service_sec if mean_service_sec is not None else default_service_sec
    batches = math.ceil(inflight / workers)
    return float(batches) * float(service)


def should_admit(est_wait_sec: float, *, sla_sec: float) -> bool:
    """est_wait ≤ sla → 放行（边界相等放行）。"""
    return est_wait_sec <= sla_sec
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_admission.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5:（不 commit，记 report）**

---

### Task 2: jobs_store 三个纯读查询

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Test: `tests/test_v2_jobs_store.py`（若不存在则 Create）

**Interfaces:**
- Consumes: 现有 `_pool()`（autocommit connection）、`agent_jobs`、`v2_worker_heartbeats` 表。
- Produces:
  - `live_worker_count(*, within_sec: int = 30) -> int`
  - `inflight_job_count() -> int`
  - `recent_mean_service_sec(*, lane: str = "chat", limit: int = 50) -> float | None`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_v2_jobs_store.py`；DB 测试，需 PG）

```python
import time

import pytest

from model_api_runtime.v2 import jobs_store

pytestmark = pytest.mark.usefixtures("_db")  # 复用本仓 DB fixture；若命名不同见下方 note


def test_live_worker_count_counts_only_recent(db_conn):
    jobs_store.record_worker_heartbeat("w-fresh-1")
    jobs_store.record_worker_heartbeat("w-fresh-2")
    # 塞一个陈旧心跳（beat_at 在窗口外）
    with jobs_store._pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) "
            "VALUES (%s, now() - make_interval(secs => %s)) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = EXCLUDED.beat_at",
            ("w-stale", 120),
        )
    assert jobs_store.live_worker_count(within_sec=30) >= 2
    # 陈旧的不计入
    n_wide = jobs_store.live_worker_count(within_sec=300)
    n_narrow = jobs_store.live_worker_count(within_sec=30)
    assert n_wide > n_narrow


def test_inflight_job_count_counts_active_states(db_conn):
    before = jobs_store.inflight_job_count()
    uid = _make_user()  # 见 note：用本仓既有 helper 造 user 行
    jobs_store.enqueue_job(uid, "chat", reason="t")
    assert jobs_store.inflight_job_count() == before + 1


def test_recent_mean_service_sec_none_without_history(db_conn):
    # 全新 lane，无 completed job
    assert jobs_store.recent_mean_service_sec(lane="no-such-lane") is None


def test_recent_mean_service_sec_averages_completed(db_conn):
    uid = _make_user()
    with jobs_store._pool().connection() as conn:
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, started_at, finished_at) "
            "VALUES (%s,'svc-test','completed', now() - make_interval(secs=>10), now())",
            (uid,),
        )
        conn.execute(
            "INSERT INTO agent_jobs (user_id, lane, status, started_at, finished_at) "
            "VALUES (%s,'svc-test','completed', now() - make_interval(secs=>20), now())",
            (uid,),
        )
    mean = jobs_store.recent_mean_service_sec(lane="svc-test", limit=50)
    assert mean is not None
    assert 14.0 <= mean <= 16.0  # (10+20)/2 = 15
```

> **Note（给实现者）**：先读 `tests/test_v2_jobs_store.py` 现有 import / fixture（`_db` / `db_conn` / 造 user 的 helper）并**照抄**，不要新造。若该文件不存在，参照 `tests/test_v2_summary_store.py` 的 DB fixture 与 user-造行方式（同一套 jobs_store DB 测试基础设施）搭起来。`_make_user()` 用现有 helper 或最小 `INSERT INTO users`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/test_v2_jobs_store.py -q -k "live_worker_count or inflight_job_count or recent_mean_service"`
Expected: FAIL（`AttributeError: ... has no attribute 'live_worker_count'`）

- [ ] **Step 3: 写实现**（加到 `jobs_store.py`，紧邻 `workers_alive` 之后）

```python
def live_worker_count(*, within_sec: int = 30) -> int:
    """窗口内有心跳的 serve_worker 数（workers_alive 的计数版，喂 admission ceiling）。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM v2_worker_heartbeats "
                "WHERE beat_at > now() - make_interval(secs => %s)",
                (int(within_sec),),
            )
            return int(cur.fetchone()[0])


def inflight_job_count() -> int:
    """在飞 job 数（pending/claimed/running）。单飞唯一索引 → 约等活跃用户数。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM agent_jobs "
                "WHERE status IN ('pending','claimed','running')"
            )
            return int(cur.fetchone()[0])


def recent_mean_service_sec(*, lane: str = "chat", limit: int = 50) -> float | None:
    """最近 limit 条 completed job 的均服务时长（finished_at−started_at，秒）。
    无历史 → None（调用方用默认常量）。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT avg(EXTRACT(EPOCH FROM (finished_at - started_at))) "
                "FROM (SELECT finished_at, started_at FROM agent_jobs "
                "      WHERE status='completed' AND lane=%s "
                "      AND finished_at IS NOT NULL AND started_at IS NOT NULL "
                "      ORDER BY finished_at DESC LIMIT %s) recent",
                (str(lane), int(limit)),
            )
            row = cur.fetchone()
            return None if row is None or row[0] is None else float(row[0])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/test_v2_jobs_store.py -q -k "live_worker_count or inflight_job_count or recent_mean_service"`
Expected: PASS

- [ ] **Step 5:（不 commit，记 report）**

---

### Task 3: chat_send_core 接线 admission ceiling

**Files:**
- Modify: `backend/hosted/chat_send_core.py`（在 `workers_alive()` 存活闸之后、`append_chat` 之前）
- Test: `tests/`（chat_send_core 的既有 v2 测试文件；实现者先 grep 定位 `workers_unavailable` 的测试所在文件并同文件追加）

**Interfaces:**
- Consumes: `jobs_store.live_worker_count` / `inflight_job_count` / `recent_mean_service_sec`（Task 2）、`admission.estimate_wait_sec` / `should_admit` / 常量（Task 1）。
- Produces: send 路径超 SLA 时的 busy 拒绝分支。

- [ ] **Step 1: 写失败测试**

先 `grep -rn "workers_unavailable" tests/` 找到覆盖 V2 存活闸的测试文件，同文件加：
1. **超 SLA 拒且不落库**：mock/patch 使 `inflight_job_count` 返回大值、`live_worker_count` 返回 1、`recent_mean_service_sec` 返回 None（→ est_wait = big×20 ≫ 60）；断言 send 返回 `(_, 503)` 且 body `error=="busy"` / `reason=="queue_over_sla"`，并断言 `store.append_chat` **未被调用**（用 spy / 或断言 chat 表无新用户消息行）。
2. **fail-open**：patch `jobs_store.inflight_job_count` 抛异常；断言 send 仍 **正常 persist + 返回 202 processing**（闸不阻断）。
3. **正常放行**：inflight 小（如 0）→ 走原路径，202。

（沿用该文件既有的 store/mock 构造与 `db_action_v2` 置位方式——实现者照抄现有 `workers_unavailable` 测试的脚手架。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest ../tests/<该文件> -q -k "admission or busy or over_sla or failopen"`
Expected: FAIL

- [ ] **Step 3: 写实现**（`chat_send_core.py`，紧接 `workers_unavailable` 的 `return ... 503` 块之后）

```python
    # §6 admission ceiling：存活闸已保证 ≥1 活 worker；再估排队等待，超 SLA 就在
    # persist 之前回独立 busy（区别于 workers_unavailable=供给死）。任何计算异常
    # fail-open（放行）——此闸绝不能自身变成故障源。
    if _v2_mode:
        try:
            _workers = jobs_store.live_worker_count(within_sec=30)
            _inflight = jobs_store.inflight_job_count()
            _mean = jobs_store.recent_mean_service_sec(lane="chat", limit=admission.SERVICE_SAMPLE_N)
            _est = admission.estimate_wait_sec(
                inflight=_inflight, workers=_workers,
                mean_service_sec=_mean, default_service_sec=admission.DEFAULT_SERVICE_SEC,
            )
            _admit = admission.should_admit(_est, sla_sec=admission.SLA_SEC)
        except Exception as exc:  # fail-open
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided", actor="host_agent_runtime",
                status="ok", summary="admission_failopen",
                detail={"mode": "admit", "error": str(exc)[:120]},
            )
            _admit = True
            _est = 0.0
        if not _admit:
            debug_trace.trace_event(
                store, subsystem="route", type="route.decided", actor="host_agent_runtime",
                status="gated", summary="admission_over_sla",
                detail={"mode": "blocked", "reason": "queue_over_sla",
                        "est_wait_sec": int(_est), "inflight": _inflight, "workers": _workers},
            )
            return {"error": "busy", "reason": "queue_over_sla", "est_wait_sec": int(_est)}, 503
```

并在文件顶部 import 区加 `from model_api_runtime.v2 import admission`（与既有 `from model_api_runtime.v2 import jobs_store` 并列）。

> **Note（给实现者）**：`_inflight`/`_workers` 在 except 分支未赋值——debug_trace 的 detail 只在 `not _admit`（try 成功）分支用它们，fail-open 分支不引用，安全。但为稳妥，在 try 前先 `_inflight = _workers = 0` 初始化。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest ../tests/<该文件> -q -k "admission or busy or over_sla or failopen"`
Expected: PASS

- [ ] **Step 5: 跑 v2 + send 回归**

Run: `cd backend && python -m pytest ../tests/test_v2_admission.py ../tests/test_v2_jobs_store.py ../tests/<该文件> -q`
Expected: 全绿（除既有 pre-existing 无关失败）

- [ ] **Step 6:（不 commit，记 report）**

---

## Self-Review

- **spec 覆盖**：算法（T1）/ 三查询（T2）/ 接线+fail-open+busy 响应（T3）全覆盖。
- **placeholder**：无 TBD；测试代码、实现代码、命令、期望输出给全。T2/T3 的「照抄既有 fixture / grep 定位文件」是刻意让实现者对齐现有脚手架，非占位。
- **类型一致**：`estimate_wait_sec`/`should_admit` 签名、`recent_mean_service_sec -> float|None`、busy body 形态在 T1/T2/T3 间一致；常量名 `SLA_SEC`/`DEFAULT_SERVICE_SEC`/`SERVICE_SAMPLE_N` 一致。
