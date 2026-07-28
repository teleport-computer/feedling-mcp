# Admin Runtime 健康值班台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/admin/data-track` 加一个 `?view=runtime` 视图，把 Runtime V2 的全 lane 运行时健康（失败率、成功回合延迟、trajectory 捕获覆盖、worker 池、失败码 Top）渲染成带红黄绿判定的值班页面。

**Architecture:** 数据层在 `model_api_runtime/v2/jobs_store.py` 新增一个 `recent_runtime_health()` 聚合函数（`GROUP BY lane`，3 次查询）；admin 层通过 `asgi_app.py` 装配段注入桩调用它（照 `_runtime_token_usage_summary` 的现成模式，admin 不 import model_api_runtime）；渲染与阈值判定是不碰 DB 的纯函数，单测无需 PostgreSQL。不新增路由、不新增迁移。

**Tech Stack:** Python 3.11 / psycopg3 (`db.get_pool()`) / PostgreSQL / FastAPI-Starlette（admin 走 `asgi_test_client.make_client()` 的 Flask-like shim）/ pytest

**Spec:** `docs/superpowers/specs/2026-07-27-admin-runtime-health-view-design.md`

## Global Constraints

- **依赖方向**（`CONTRIBUTING.md` §2）：`backend/admin/` 不得 import `model_api_runtime`。需要向上调用时声明桩、由 `backend/asgi_app.py` 末尾装配段注入。
- **跨模块调用写法**（`CONTRIBUTING.md` §3）：一律 `from pkg import module` + `module.func()`，禁止 `from module import func`（否则 monkeypatch 失效）。
- **本页只读**：无写路径、无 DDL、**不新增 alembic 迁移**。
- **metadata-only 边界**：页面不得渲染任何加密内容。失败原因只到枚举码这一层。
- **零样本不得渲染成 0%**：任何分母为 0 的指标显示 `—` 或 `N/A`，且不参与总体结论（教训来源 commit `2795537a`）。
- **窗口枚举**：`hours` 只接受 `24` / `168` / `720`，其余一律回落 `24`。
- **本仓库 commit 规则**：commit 需用户明确要求。各任务末尾的 commit 步骤在获得用户授权后再执行；未授权时把改动留在工作树并向用户报告。
- **测试基线**：本地跑 DB 测必须先起 PostgreSQL，否则 DB 用例静默跳过、绿色是假象。
  ```bash
  docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
  ```

## File Structure

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `backend/model_api_runtime/v2/jobs_store.py` | 修改（文件尾部追加） | 新增 `recent_runtime_health()`——全 lane 健康聚合，V2 表结构知识只留在这里 |
| `backend/admin/data_track.py` | 修改 | 桩 `_runtime_health_summary`、阈值常量、失败码清洗、`_runtime_health_level()` 判定、`_render_runtime_health_page()` 渲染、nav 加项、view 白名单加 `runtime`、`_data_track_qs` 加 `hours` |
| `backend/admin/admin_core.py` | 修改 `page_html`（`:85-104`） | 加 `view == "runtime"` 分发分支 |
| `backend/asgi_app.py` | 修改装配段（`:144` 附近） | 注入 `_runtime_health_summary = _v2_jobs_store.recent_runtime_health` |
| `tests/test_v2_runtime_health.py` | 新建 | `recent_runtime_health()` 的 DB 测（需 PG） |
| `tests/test_data_track_runtime_view.py` | 新建 | 阈值判定与渲染纯函数单测 + 路由测（注入假 payload，无需 PG） |

---

### Task 1: `recent_runtime_health()` —— 全 lane outcome 聚合

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（在 `recent_chat_operational_health` 之后追加新函数）
- Test: `tests/test_v2_runtime_health.py`（新建）

**Interfaces:**
- Consumes: 现有 `jobs_store._pool()`、`jobs_store.enqueue_job(user_id, lane) -> (job_id, bool)`、`jobs_store.record_whole_turn_metric(...)`
- Produces: `jobs_store.recent_runtime_health(*, within_hours: int = 24, limit: int = 1000) -> dict`。本任务只实现返回值中的 `window_hours` / `generated_at` / `lanes[*]` 的 outcome 字段（`lane`/`sampled_jobs`/`completed`/`failed`/`expired`/`superseded`/`queue_expired`/`lease_expired`/`failure_rate`）。`capture` / `p50_ok_ms` / `p95_ok_ms` / `top_failures` / `pool` 在 Task 2 补齐。

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_v2_runtime_health.py`：

```python
"""recent_runtime_health：全 lane 运行时健康聚合。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store

from conftest import seed_user, set_v2_runtime_owner

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DB-backed runtime-health tests require the PostgreSQL test fixture",
)

_TERMINAL = {"completed", "failed", "expired", "superseded"}


@pytest.fixture(autouse=True)
def _clean_tables():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_turn_metrics")
        conn.execute("DELETE FROM agent_jobs")
    yield


def _add_job(
    user_id: str,
    lane: str,
    status: str,
    *,
    age_hours: int = 0,
    last_error: str | None = None,
) -> int:
    """一个 job 一个用户——agent_jobs 有单飞唯一索引，同用户同 lane 不能并存两条在飞行中。"""
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    job_id, _ = jobs_store.enqueue_job(user_id, lane)
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET status=%s,"
            "created_at=clock_timestamp()-make_interval(hours => %s),"
            "finished_at=CASE WHEN %s THEN "
            "clock_timestamp()-make_interval(hours => %s) ELSE NULL END,"
            "last_error=%s WHERE id=%s",
            (status, age_hours, status in _TERMINAL, age_hours, last_error, job_id),
        )
    return job_id


def test_recent_runtime_health_groups_outcomes_by_lane():
    _add_job("u_rh_chat_ok", "chat", "completed")
    _add_job("u_rh_chat_ok2", "chat", "completed")
    _add_job("u_rh_chat_bad", "chat", "failed")
    _add_job("u_rh_hb_ok", "heartbeat", "completed")

    health = jobs_store.recent_runtime_health(within_hours=24)

    assert health["window_hours"] == 24
    lanes = {row["lane"]: row for row in health["lanes"]}
    assert lanes["chat"]["completed"] == 2
    assert lanes["chat"]["failed"] == 1
    assert lanes["chat"]["sampled_jobs"] == 3
    assert lanes["chat"]["failure_rate"] == pytest.approx(1 / 3)
    assert lanes["heartbeat"]["completed"] == 1
    assert lanes["heartbeat"]["failure_rate"] == pytest.approx(0.0)
    # lanes 按样本量降序：chat(3) 在 heartbeat(1) 前
    assert [row["lane"] for row in health["lanes"]] == ["chat", "heartbeat"]


def test_recent_runtime_health_excludes_superseded_from_failure_rate():
    # 运行时代际切换不是故障：superseded 单列，既不进分子也不进分母。
    _add_job("u_rh_sup_ok", "chat", "completed")
    _add_job("u_rh_sup_1", "chat", "superseded")
    _add_job("u_rh_sup_2", "chat", "superseded")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["superseded"] == 2
    assert lanes["chat"]["completed"] == 1
    assert lanes["chat"]["failure_rate"] == pytest.approx(0.0)


def test_recent_runtime_health_splits_expiry_reasons():
    _add_job("u_rh_q", "chat", "expired", last_error="queue_timeout")
    _add_job("u_rh_l", "chat", "expired", last_error="lease_timeout")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["expired"] == 2
    assert lanes["chat"]["queue_expired"] == 1
    assert lanes["chat"]["lease_expired"] == 1
    assert lanes["chat"]["failure_rate"] == pytest.approx(1.0)


def test_recent_runtime_health_respects_window():
    _add_job("u_rh_recent", "chat", "completed")
    _add_job("u_rh_old", "chat", "failed", age_hours=48)

    lanes_24 = {r["lane"]: r for r in jobs_store.recent_runtime_health(within_hours=24)["lanes"]}
    lanes_168 = {r["lane"]: r for r in jobs_store.recent_runtime_health(within_hours=168)["lanes"]}

    assert lanes_24["chat"]["sampled_jobs"] == 1
    assert lanes_24["chat"]["failed"] == 0
    assert lanes_168["chat"]["sampled_jobs"] == 2
    assert lanes_168["chat"]["failed"] == 1


def test_recent_runtime_health_is_empty_without_history():
    health = jobs_store.recent_runtime_health()
    assert health["lanes"] == []
    assert health["window_hours"] == 24
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_runtime_health.py -v
```

Expected: FAIL —— `AttributeError: module 'model_api_runtime.v2.jobs_store' has no attribute 'recent_runtime_health'`

- [ ] **Step 3: 写最小实现**

在 `backend/model_api_runtime/v2/jobs_store.py` 里 `recent_chat_operational_health` 函数之后追加：

```python
def recent_runtime_health(
    *,
    within_hours: int = 24,
    limit: int = 1000,
) -> dict:
    """全 lane 运行时健康快照（content-free），喂 admin 值班台。

    分母刻意从 ``agent_jobs`` 起算而非从 metrics/trajectory 起算：一次完全漏写
    若同时消失于分子和分母，就会报出虚假健康的机群。``superseded`` 单列、不进
    失败率——运行时代际切换不是故障，混进去会稀释真实失败率。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 1000))

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH recent AS ("
                "  SELECT lane,status,last_error FROM agent_jobs "
                "  WHERE status IN ('completed','failed','expired','superseded') "
                "    AND finished_at >= now() - make_interval(hours => %s) "
                "  ORDER BY finished_at DESC,id DESC LIMIT %s"
                ") SELECT lane,"
                "  COUNT(*) FILTER (WHERE status='completed')::int AS completed,"
                "  COUNT(*) FILTER (WHERE status='failed')::int AS failed,"
                "  COUNT(*) FILTER (WHERE status='expired')::int AS expired,"
                "  COUNT(*) FILTER (WHERE status='superseded')::int AS superseded,"
                "  COUNT(*) FILTER (WHERE status='expired' "
                "    AND last_error='queue_timeout')::int AS queue_expired,"
                "  COUNT(*) FILTER (WHERE status='expired' "
                "    AND last_error='lease_timeout')::int AS lease_expired "
                "FROM recent GROUP BY lane",
                (safe_hours, safe_limit),
            )
            outcome_rows = cur.fetchall()

    lanes = []
    for row in outcome_rows:
        completed = int(row["completed"] or 0)
        failed = int(row["failed"] or 0)
        expired = int(row["expired"] or 0)
        resolved = completed + failed + expired
        lanes.append({
            "lane": str(row["lane"] or "unknown"),
            "sampled_jobs": resolved,
            "completed": completed,
            "failed": failed,
            "expired": expired,
            "superseded": int(row["superseded"] or 0),
            "queue_expired": int(row["queue_expired"] or 0),
            "lease_expired": int(row["lease_expired"] or 0),
            "failure_rate": (
                float(failed + expired) / float(resolved) if resolved else None
            ),
        })

    lanes.sort(key=lambda item: (item["sampled_jobs"], item["lane"]), reverse=True)
    return {
        "window_hours": safe_hours,
        "generated_at": time.time(),
        "lanes": lanes,
    }
```

实现注意：
- `dict_row` 与 `time` 在 `jobs_store.py` 顶部已 import，无需新增 import。跑 Step 4 前先确认（`grep -n "^import time\|dict_row" backend/model_api_runtime/v2/jobs_store.py`），缺哪个补哪个。
- `LIMIT` 在 CTE 里是**全局**采样上界，不是 per-lane —— 与现有 `recent_chat_operational_health` 的约定一致。
- `sampled_jobs` 不含 `superseded`，因此它等于失败率的分母。

- [ ] **Step 4: 跑测试确认通过**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_runtime_health.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit（需用户授权）**

```bash
git add backend/model_api_runtime/v2/jobs_store.py tests/test_v2_runtime_health.py
git commit -m "feat(v2): recent_runtime_health 全 lane outcome 聚合"
```

---

### Task 2: 补齐延迟分位数、捕获覆盖、失败码 Top、worker 池

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（Task 1 新增的 `recent_runtime_health`）
- Test: `tests/test_v2_runtime_health.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 的 `recent_runtime_health(*, within_hours=24, limit=1000)`；现有 `jobs_store.inflight_job_count() -> int`、`jobs_store.live_worker_count(*, within_sec=30) -> int`、`jobs_store.live_worker_capacity(*, within_sec=30) -> int`
- Produces: `recent_runtime_health()` 返回值补全为 spec §5 的完整结构 —— `lanes[*]` 增加 `p50_ok_ms`/`p95_ok_ms`/`capture`（`{complete,partial,missing,open}`）/`top_failures`（`[{code, count}]`），顶层增加 `pool`（`{inflight,pending,live_workers,capacity,oldest_pending_age_sec}`）

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_v2_runtime_health.py`（沿用文件里已有的 `_add_job` 辅助函数）：

```python
def test_recent_runtime_health_latency_ignores_failed_turns():
    # 只算成功回合：一批失败超时回合不得把 p95 拉高，否则一个故障会同时点亮
    # 「失败率」和「延迟」两盏灯，值班时看起来像两个独立故障。
    ok_job = _add_job("u_rh_lat_ok", "chat", "completed")
    bad_job = _add_job("u_rh_lat_bad", "chat", "failed")
    jobs_store.record_whole_turn_metric(
        ok_job, "u_rh_lat_ok", "chat",
        prompt_tokens=10, completion_tokens=5, latency_ms=20_000,
        model_calls=1, retries=0, failed=False, status="ok",
    )
    jobs_store.record_whole_turn_metric(
        bad_job, "u_rh_lat_bad", "chat",
        prompt_tokens=None, completion_tokens=None, latency_ms=550_000,
        model_calls=1, retries=0, failed=True,
        status="turn_failed:providererror",
    )

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["p95_ok_ms"] == pytest.approx(20_000)
    assert lanes["chat"]["p50_ok_ms"] == pytest.approx(20_000)


def test_recent_runtime_health_latency_is_none_without_successful_turns():
    bad_job = _add_job("u_rh_lat_none", "chat", "failed")
    jobs_store.record_whole_turn_metric(
        bad_job, "u_rh_lat_none", "chat",
        prompt_tokens=None, completion_tokens=None, latency_ms=99_000,
        model_calls=1, retries=0, failed=True,
        status="turn_failed:responder_error",
    )

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    # 无成功样本 → None（页面显 N/A），绝不能拿失败回合的延迟冒充
    assert lanes["chat"]["p95_ok_ms"] is None
    assert lanes["chat"]["p50_ok_ms"] is None


def test_recent_runtime_health_counts_missing_capture_from_jobs():
    # 有终态 job 但没有 trajectory 流 → missing，必须从 agent_jobs 起算才看得见
    _add_job("u_rh_cap_missing", "chat", "completed")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert lanes["chat"]["capture"]["missing"] == 1
    assert lanes["chat"]["capture"]["complete"] == 0


def test_recent_runtime_health_top_failures_are_enumerated_codes():
    _add_job("u_rh_tf_1", "chat", "failed", last_error="turn_failed:providererror")
    _add_job("u_rh_tf_2", "chat", "failed", last_error="turn_failed:providererror")
    _add_job("u_rh_tf_3", "chat", "failed", last_error="turn_failed:responder_error")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}
    top = lanes["chat"]["top_failures"]

    assert top[0] == {"code": "turn_failed:providererror", "count": 2}
    assert {"code": "turn_failed:responder_error", "count": 1} in top


def test_recent_runtime_health_reports_pool_and_pending_age():
    _add_job("u_rh_pool_pending", "chat", "pending", age_hours=1)

    pool = jobs_store.recent_runtime_health()["pool"]

    assert pool["pending"] == 1
    assert pool["oldest_pending_age_sec"] >= 3_500  # ~1h
    assert pool["inflight"] >= 1
    assert pool["live_workers"] >= 0
    assert pool["capacity"] >= 0


def test_recent_runtime_health_pool_pending_age_is_none_when_idle():
    _add_job("u_rh_pool_idle", "chat", "completed")

    pool = jobs_store.recent_runtime_health()["pool"]

    assert pool["pending"] == 0
    assert pool["oldest_pending_age_sec"] is None


def test_recent_runtime_health_keeps_lane_with_only_inflight_jobs():
    # 一条 lane 的 job 全部还在飞（worker 卡死就是这个形状）：它不在 outcome 查询的
    # 结果里，但必须仍然出现在 lanes[] 中。若只用 outcome_rows 驱动合并，这条 lane
    # 会静默消失——而它恰恰是值班台最该喊出来的故障。
    _add_job("u_rh_inflight_only", "chat", "pending")

    lanes = {r["lane"]: r for r in jobs_store.recent_runtime_health()["lanes"]}

    assert "chat" in lanes
    assert lanes["chat"]["sampled_jobs"] == 0
    assert lanes["chat"]["failure_rate"] is None
    assert lanes["chat"]["capture"]["open"] >= 1
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_runtime_health.py -v
```

Expected: 新增 7 个用例 FAIL（`KeyError: 'p95_ok_ms'` / `KeyError: 'capture'` / `KeyError: 'pool'`），Task 1 的 6 个仍 PASS

- [ ] **Step 3: 写实现**

把 `recent_runtime_health` 扩成下面这版（替换 Task 1 的函数体，`with _pool().connection()` 块内追加三次查询，返回值补齐）：

```python
def recent_runtime_health(
    *,
    within_hours: int = 24,
    limit: int = 1000,
) -> dict:
    """全 lane 运行时健康快照（content-free），喂 admin 值班台。

    分母刻意从 ``agent_jobs`` 起算而非从 metrics/trajectory 起算：一次完全漏写
    若同时消失于分子和分母，就会报出虚假健康的机群。``superseded`` 单列、不进
    失败率——运行时代际切换不是故障。延迟分位数只取成功回合（``failed IS NOT
    TRUE``）：失败超时回合会把 p95 拉到与故障同源的高位，让一个故障看起来像两个。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 30))
    safe_limit = max(1, min(int(limit), 1000))
    terminal_statuses = ("completed", "failed", "expired", "superseded")

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "WITH recent AS ("
                "  SELECT lane,status,last_error FROM agent_jobs "
                "  WHERE status IN ('completed','failed','expired','superseded') "
                "    AND finished_at >= now() - make_interval(hours => %s) "
                "  ORDER BY finished_at DESC,id DESC LIMIT %s"
                ") SELECT lane,"
                "  COUNT(*) FILTER (WHERE status='completed')::int AS completed,"
                "  COUNT(*) FILTER (WHERE status='failed')::int AS failed,"
                "  COUNT(*) FILTER (WHERE status='expired')::int AS expired,"
                "  COUNT(*) FILTER (WHERE status='superseded')::int AS superseded,"
                "  COUNT(*) FILTER (WHERE status='expired' "
                "    AND last_error='queue_timeout')::int AS queue_expired,"
                "  COUNT(*) FILTER (WHERE status='expired' "
                "    AND last_error='lease_timeout')::int AS lease_expired "
                "FROM recent GROUP BY lane",
                (safe_hours, safe_limit),
            )
            outcome_rows = cur.fetchall()

            cur.execute(
                "WITH recent AS ("
                "  SELECT lane,latency_ms FROM v2_turn_metrics "
                "  WHERE failed IS NOT TRUE AND latency_ms IS NOT NULL "
                "    AND latency_ms >= 0 "
                "    AND created_at >= now() - make_interval(hours => %s) "
                "  ORDER BY created_at DESC,id DESC LIMIT %s"
                ") SELECT lane,"
                "  percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms "
                "FROM recent GROUP BY lane",
                (safe_hours, safe_limit),
            )
            latency_rows = cur.fetchall()

            cur.execute(
                "WITH recent_jobs AS ("
                "  SELECT id,lane,status FROM agent_jobs "
                "  WHERE created_at >= now() - make_interval(hours => %s) "
                "  ORDER BY id DESC LIMIT %s"
                "), classified AS ("
                "  SELECT job.lane,"
                "    CASE "
                "      WHEN stream.job_id IS NULL "
                "        AND job.status=ANY(%s::text[]) THEN 'missing' "
                "      WHEN stream.job_id IS NULL THEN 'open' "
                "      WHEN EXISTS (SELECT 1 FROM v2_trajectory_events gap "
                "        WHERE gap.job_id=job.id "
                "          AND gap.event_kind='capture_gap') THEN 'partial' "
                "      WHEN EXISTS (SELECT 1 FROM v2_trajectory_events terminal "
                "        WHERE terminal.job_id=job.id "
                "          AND terminal.event_kind='turn_terminal') THEN 'complete' "
                "      WHEN job.status=ANY(%s::text[]) THEN 'partial' "
                "      ELSE 'open' "
                "    END AS capture_status "
                "  FROM recent_jobs job "
                "  LEFT JOIN v2_trajectory_streams stream ON stream.job_id=job.id"
                ") SELECT lane,"
                "  COUNT(*) FILTER (WHERE capture_status='complete')::int AS complete,"
                "  COUNT(*) FILTER (WHERE capture_status='partial')::int AS partial,"
                "  COUNT(*) FILTER (WHERE capture_status='missing')::int AS missing,"
                "  COUNT(*) FILTER (WHERE capture_status='open')::int AS open "
                "FROM classified GROUP BY lane",
                (
                    safe_hours,
                    safe_limit,
                    list(terminal_statuses),
                    list(terminal_statuses),
                ),
            )
            capture_rows = cur.fetchall()

            cur.execute(
                "WITH recent AS ("
                "  SELECT lane,last_error FROM agent_jobs "
                "  WHERE status IN ('failed','expired') AND last_error IS NOT NULL "
                "    AND finished_at >= now() - make_interval(hours => %s) "
                "  ORDER BY finished_at DESC,id DESC LIMIT %s"
                ") SELECT lane,last_error,COUNT(*)::int AS count "
                "FROM recent GROUP BY lane,last_error ORDER BY count DESC",
                (safe_hours, safe_limit),
            )
            failure_rows = cur.fetchall()

            cur.execute(
                "SELECT COUNT(*)::int AS pending,"
                "  EXTRACT(EPOCH FROM "
                "    (clock_timestamp()-MIN(created_at))) AS oldest_pending_age_sec "
                "FROM agent_jobs WHERE status='pending'"
            )
            pending_row = cur.fetchone()

    latency_by_lane = {str(row["lane"] or ""): row for row in latency_rows}
    capture_by_lane = {str(row["lane"] or ""): row for row in capture_rows}
    failures_by_lane: dict[str, list[dict]] = {}
    for row in failure_rows:
        failures_by_lane.setdefault(str(row["lane"] or ""), []).append({
            "code": str(row["last_error"] or ""),
            "count": int(row["count"] or 0),
        })

    def _optional_ms(row, key):
        if row is None or row.get(key) is None:
            return None
        return float(row[key])

    # lane 集合取四份结果的并集，不能只用 outcome_rows：一条 lane 若窗口内所有 job
    # 都还没到终态（worker 卡死正是这种情形），它不在 outcome_rows 里，只用 outcome
    # 驱动循环会把它整个丢掉——而那恰恰是值班台最该喊出来的故障。
    outcome_by_lane = {str(row["lane"] or "unknown"): row for row in outcome_rows}
    all_lanes = (
        set(outcome_by_lane)
        | set(latency_by_lane)
        | set(capture_by_lane)
        | set(failures_by_lane)
    )

    lanes = []
    for lane in sorted(all_lanes):
        row = outcome_by_lane.get(lane) or {}
        completed = int(row.get("completed") or 0)
        failed = int(row.get("failed") or 0)
        expired = int(row.get("expired") or 0)
        resolved = completed + failed + expired
        capture = capture_by_lane.get(lane)
        lanes.append({
            "lane": lane,
            "sampled_jobs": resolved,
            "completed": completed,
            "failed": failed,
            "expired": expired,
            "superseded": int(row.get("superseded") or 0),
            "queue_expired": int(row.get("queue_expired") or 0),
            "lease_expired": int(row.get("lease_expired") or 0),
            "failure_rate": (
                float(failed + expired) / float(resolved) if resolved else None
            ),
            "p50_ok_ms": _optional_ms(latency_by_lane.get(lane), "p50_ms"),
            "p95_ok_ms": _optional_ms(latency_by_lane.get(lane), "p95_ms"),
            "capture": {
                "complete": int((capture or {}).get("complete") or 0),
                "partial": int((capture or {}).get("partial") or 0),
                "missing": int((capture or {}).get("missing") or 0),
                "open": int((capture or {}).get("open") or 0),
            },
            "top_failures": failures_by_lane.get(lane, [])[:5],
        })

    lanes.sort(key=lambda item: (item["sampled_jobs"], item["lane"]), reverse=True)
    oldest_pending = (
        pending_row.get("oldest_pending_age_sec") if pending_row else None
    )
    return {
        "window_hours": safe_hours,
        "generated_at": time.time(),
        "lanes": lanes,
        "pool": {
            "inflight": inflight_job_count(),
            "pending": int((pending_row or {}).get("pending") or 0),
            "live_workers": live_worker_count(),
            "capacity": live_worker_capacity(),
            "oldest_pending_age_sec": (
                float(oldest_pending) if oldest_pending is not None else None
            ),
        },
    }
```

实现注意：
- `top_failures` 的 `code` 在数据层**原样返回**，清洗（前缀白名单 + 截断 + 转义）在渲染层做（Task 3）。数据层保真、展示层设防，这样数据函数的测试断言的是真实值。
- 每 lane 只留前 5 条失败码。
- `pool` 的 `inflight` / `live_workers` / `capacity` 复用已有函数，它们各自取连接；`pending` 与 `oldest_pending_age_sec` 合并成一次查询。

- [ ] **Step 4: 跑测试确认通过**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_runtime_health.py -v
```

Expected: 13 passed

- [ ] **Step 5: 跑既有 V2 回归，确认没碰坏邻居**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_jobs_store.py tests/test_v2_turn_metrics.py \
  tests/test_v2_trajectory_db.py tests/test_v2_metrics_endpoint.py -q
python -m pyflakes backend/model_api_runtime/v2/jobs_store.py
```

Expected: 全部 passed；pyflakes 无输出

- [ ] **Step 6: Commit（需用户授权）**

```bash
git add backend/model_api_runtime/v2/jobs_store.py tests/test_v2_runtime_health.py
git commit -m "feat(v2): recent_runtime_health 补齐延迟/捕获/失败码/池"
```

---

### Task 3: 阈值判定与失败码清洗（admin 层纯函数）

**Files:**
- Modify: `backend/admin/data_track.py`（在 `_fmt_ratio` 之后、`_render_admin_login_page` 之前插入）
- Test: `tests/test_data_track_runtime_view.py`（新建）

**Interfaces:**
- Consumes: Task 2 产出的 payload 结构（`{"window_hours", "generated_at", "lanes": [...], "pool": {...}}`）
- Produces:
  - `data_track._RUNTIME_HEALTH_WINDOWS: tuple[int, ...]` = `(24, 168, 720)`
  - `data_track._runtime_failure_code(raw) -> str`
  - `data_track._runtime_health_level(payload: dict) -> tuple[str, list[str]]` 返回 `("ok"|"warn"|"bad", [中文原因])`
  - `data_track._runtime_health_window_hours() -> int` —— 读 `request.args` 的 `hours`，
    白名单外一律回落 24。**它不是纯函数**（依赖请求上下文），单测需用
    `admin_core.bind(query_string)` 包裹；Task 5 的 `page_html` 分支消费它。
  - `data_track._runtime_health_summary(*, within_hours: int = 24) -> dict` 注入桩

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_data_track_runtime_view.py`：

```python
"""Runtime 健康值班台：阈值判定与失败码清洗（纯函数，无需 PostgreSQL）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from admin import data_track as _dt  # noqa: E402


def _lane(**overrides) -> dict:
    base = {
        "lane": "chat",
        "sampled_jobs": 100,
        "completed": 100,
        "failed": 0,
        "expired": 0,
        "superseded": 0,
        "queue_expired": 0,
        "lease_expired": 0,
        "failure_rate": 0.0,
        "p50_ok_ms": 18_500,
        "p95_ok_ms": 38_100,
        "capture": {"complete": 100, "partial": 0, "missing": 0, "open": 0},
        "top_failures": [],
    }
    base.update(overrides)
    return base


def _payload(lanes=None, **pool_overrides) -> dict:
    pool = {
        "inflight": 0, "pending": 0, "live_workers": 2,
        "capacity": 8, "oldest_pending_age_sec": None,
    }
    pool.update(pool_overrides)
    return {
        "window_hours": 24,
        "generated_at": 1_800_000_000.0,
        "lanes": lanes if lanes is not None else [_lane()],
        "pool": pool,
    }


def test_runtime_health_level_green_on_healthy_fleet():
    level, reasons = _dt._runtime_health_level(_payload())
    assert level == "ok"
    assert reasons == []


def test_runtime_health_level_warns_between_thresholds():
    # 失败率 8% 落在 5%~15% 的黄区
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(failure_rate=0.08, failed=8, completed=92)])
    )
    assert level == "warn"
    assert any("失败率" in r for r in reasons)


def test_runtime_health_level_red_on_high_failure_rate():
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(failure_rate=1.0, failed=20, completed=0)])
    )
    assert level == "bad"
    assert any("失败率" in r for r in reasons)


def test_runtime_health_level_red_on_missing_trajectory():
    # 漏写没有「轻微」档：一条就是数据缺口
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(capture={"complete": 9, "partial": 0, "missing": 1, "open": 0})])
    )
    assert level == "bad"
    assert any("捕获" in r or "missing" in r for r in reasons)


def test_runtime_health_level_red_on_empty_worker_pool():
    level, reasons = _dt._runtime_health_level(_payload(live_workers=0))
    assert level == "bad"
    assert any("worker" in r.lower() for r in reasons)


def test_runtime_health_level_uses_p95_thresholds():
    warn, _ = _dt._runtime_health_level(_payload([_lane(p95_ok_ms=90_000)]))
    bad, _ = _dt._runtime_health_level(_payload([_lane(p95_ok_ms=300_000)]))
    assert warn == "warn"
    assert bad == "bad"


def test_runtime_health_level_uses_pending_age_thresholds():
    warn, _ = _dt._runtime_health_level(_payload(pending=1, oldest_pending_age_sec=90))
    bad, _ = _dt._runtime_health_level(_payload(pending=1, oldest_pending_age_sec=600))
    assert warn == "warn"
    assert bad == "bad"


def test_runtime_health_level_ignores_empty_samples():
    # 零样本不得判红——2795537a 的教训：分母为 0 曾被渲染成红 0%，
    # 3 条健康心跳看起来像全挂。
    level, reasons = _dt._runtime_health_level(
        _payload([_lane(
            sampled_jobs=0, completed=0, failure_rate=None,
            p50_ok_ms=None, p95_ok_ms=None,
            capture={"complete": 0, "partial": 0, "missing": 0, "open": 0},
        )])
    )
    assert level == "ok"
    assert reasons == []


def test_runtime_health_level_takes_worst_across_lanes():
    level, _ = _dt._runtime_health_level(_payload([
        _lane(lane="chat"),
        _lane(lane="heartbeat", failure_rate=0.9, failed=9, completed=1),
    ]))
    assert level == "bad"


def test_runtime_failure_code_keeps_known_enumerations():
    assert _dt._runtime_failure_code("turn_failed:providererror") == "turn_failed:providererror"
    assert _dt._runtime_failure_code("queue_timeout") == "queue_timeout"
    assert _dt._runtime_failure_code("lease_timeout") == "lease_timeout"


def test_runtime_failure_code_buckets_unknown_free_text():
    # 将来若有人往 last_error 写自由文本（含用户内容），页面不得渗出
    leaked = "Traceback: user said 我的身份证号是 1234"
    assert _dt._runtime_failure_code(leaked) == "other"
    assert _dt._runtime_failure_code("") == "other"
    assert _dt._runtime_failure_code(None) == "other"


def test_runtime_failure_code_truncates_long_known_prefix():
    long_code = "turn_failed:" + ("x" * 200)
    assert len(_dt._runtime_failure_code(long_code)) == 64
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
```

Expected: FAIL —— `AttributeError: module 'admin.data_track' has no attribute '_runtime_health_level'`

- [ ] **Step 3: 写实现**

在 `backend/admin/data_track.py` 的 `_fmt_ratio`（`:2069-2076`）之后插入：

```python
# ---- Runtime 健康值班台 ----------------------------------------------------
# 阈值集中在此，便于以后一处调整。定阈依据（2026-07-27 三环境实测）：
#   失败率 —— prod 07-26 为 0%、07-27 为 100%，两端都能正确点亮
#   p95    —— pre 健康态 chat p95 = 38.1s，留约 1.5× 余量
#   pending 年龄 —— 对应 claim lag 退化；健康时该值为空
_RUNTIME_HEALTH_WINDOWS = (24, 168, 720)
_RUNTIME_HEALTH_FAILURE_WARN = 0.05
_RUNTIME_HEALTH_FAILURE_BAD = 0.15
_RUNTIME_HEALTH_P95_WARN_MS = 60_000
_RUNTIME_HEALTH_P95_BAD_MS = 120_000
_RUNTIME_HEALTH_PENDING_WARN_SEC = 60
_RUNTIME_HEALTH_PENDING_BAD_SEC = 180
_RUNTIME_FAILURE_CODE_MAX = 64
_RUNTIME_FAILURE_KNOWN_EXACT = frozenset({"queue_timeout", "lease_timeout"})
_RUNTIME_FAILURE_KNOWN_PREFIX = "turn_failed:"


def _runtime_failure_code(raw) -> str:
    """失败码白名单化：只放行已知枚举形状，其余归入 other。

    实测 agent_jobs.last_error 当前全是干净枚举码，本函数是为将来有人往该字段
    写自由文本时页面仍不渗内容——data-track 是 metadata-only 面。
    """
    code = str(raw or "").strip()
    if not code:
        return "other"
    if code in _RUNTIME_FAILURE_KNOWN_EXACT:
        return code
    if code.startswith(_RUNTIME_FAILURE_KNOWN_PREFIX):
        return code[:_RUNTIME_FAILURE_CODE_MAX]
    return "other"


def _runtime_health_level(payload: dict) -> tuple[str, list[str]]:
    """(总体档位, 中文原因列表)。档位取所有指标里最差的一档。

    分母为 0 的指标一律跳过、不参与判定：零样本不是故障。commit 2795537a 的
    re-review 教训——V2-only 合成行的 legacy 分母为 0 曾被渲染成红 0%，3 条健康
    心跳看起来像全挂。
    """
    rank = {"ok": 0, "warn": 1, "bad": 2}
    worst = "ok"
    reasons: list[str] = []

    def escalate(level: str, reason: str) -> None:
        nonlocal worst
        if rank[level] > rank[worst]:
            worst = level
        if level != "ok":
            reasons.append(reason)

    for lane in payload.get("lanes") or []:
        name = str(lane.get("lane") or "unknown")
        rate = lane.get("failure_rate")
        if rate is not None and int(lane.get("sampled_jobs") or 0) > 0:
            if rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                escalate("bad", f"{name} 失败率 {rate * 100:.0f}%")
            elif rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                escalate("warn", f"{name} 失败率 {rate * 100:.0f}%")

        p95 = lane.get("p95_ok_ms")
        if p95 is not None:
            if p95 >= _RUNTIME_HEALTH_P95_BAD_MS:
                escalate("bad", f"{name} 成功回合 p95 {p95 / 1000:.0f}s")
            elif p95 >= _RUNTIME_HEALTH_P95_WARN_MS:
                escalate("warn", f"{name} 成功回合 p95 {p95 / 1000:.0f}s")

        missing = int((lane.get("capture") or {}).get("missing") or 0)
        if missing > 0:
            escalate("bad", f"{name} trajectory 漏写 {missing} 条")

    pool = payload.get("pool") or {}
    if int(pool.get("live_workers") or 0) <= 0:
        escalate("bad", "无存活 worker")

    age = pool.get("oldest_pending_age_sec")
    if age is not None:
        if age >= _RUNTIME_HEALTH_PENDING_BAD_SEC:
            escalate("bad", f"最老 pending 已排队 {age / 60:.0f} 分钟")
        elif age >= _RUNTIME_HEALTH_PENDING_WARN_SEC:
            escalate("warn", f"最老 pending 已排队 {age:.0f} 秒")

    return worst, reasons


def _runtime_health_window_hours() -> int:
    """窗口枚举白名单（照 view 参数的写法），非法值一律回落 24。"""
    try:
        value = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        return 24
    return value if value in _RUNTIME_HEALTH_WINDOWS else 24


# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.recent_runtime_health.
def _runtime_health_summary(*, within_hours: int = 24) -> dict:
    return {
        "window_hours": within_hours,
        "generated_at": 0.0,
        "lanes": [],
        "pool": {
            "inflight": 0, "pending": 0, "live_workers": 0,
            "capacity": 0, "oldest_pending_age_sec": None,
        },
    }
```

> **⚠️ 最终 code review 修正（2026-07-28）**：上面这版 `_runtime_failure_code` 与
> `_runtime_health_level` 是最初照抄进代码的版本，review 后发现两处缺陷，实现已改，
> 这份计划的代码块**不再是当前实现**（仅保留作历史记录）：
>
> 1. **`_runtime_failure_code` 白名单太窄**（I-2）：只放行精确匹配 `queue_timeout` /
>    `lease_timeout` 和无条件的 `turn_failed:` 前缀（不校验冒号后内容形状）。真实写入点
>    还有 `wake_failed:*` / `extraction_failed:*` / `compaction_failed:*` /
>    `mcp_mutation_outcome_unknown` / `runtime_expired`，旧白名单下这些码在 chat 之外
>    每条 lane 都塌成 `other`——heartbeat lane 的失败原因因此永远只显示 `other`。修正为
>    按形状放行（`_RUNTIME_FAILURE_CODE_RE = re.compile(r"^[a-z0-9_]+(:[a-z0-9_]+)?$")`，
>    admin 层自行定义，不 import `model_api_runtime`），移除了 `_RUNTIME_FAILURE_KNOWN_EXACT`
>    / `_RUNTIME_FAILURE_KNOWN_PREFIX` 两个常量。渲染层（Task 4）清洗后还需按
>    `(lane, code)` 重新合并计数，否则两个不同原始码清洗成同一个桶会渲染成两行。
> 2. **`_runtime_health_level` 漏了「卡死」形态**（I-3）：job 全部卡在
>    `claimed`/`running`（无 pending、worker 心跳还活着）时，`rate`/`p95`/`missing`
>    全部为空或 0，函数对这条 lane 全部跳过，误判 `("ok", [])`。修正后加了两条判定：
>    lane 的 `sampled_jobs==0 and capture["open"]>0` → 至少 `warn`；
>    `pool["inflight"] > pool["capacity"]` → `bad`（矛盾态，不分级）。
>
> 当前实现见 `backend/admin/data_track.py` 的 `_runtime_failure_code` /
> `_runtime_health_level`。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
python -m pyflakes backend/admin/data_track.py
```

Expected: 12 passed；pyflakes 无输出

- [ ] **Step 5: Commit（需用户授权）**

```bash
git add backend/admin/data_track.py tests/test_data_track_runtime_view.py
git commit -m "feat(admin): Runtime 健康阈值判定 + 失败码白名单"
```

---

### Task 4: 渲染页面

**Files:**
- Modify: `backend/admin/data_track.py`（在 Task 3 插入的代码块之后追加 `_render_runtime_health_page`）
- Test: `tests/test_data_track_runtime_view.py`（追加用例）

**Interfaces:**
- Consumes: Task 3 的 `_runtime_health_level()`、`_runtime_failure_code()`、`_RUNTIME_HEALTH_WINDOWS`；现有 `_render_metric(label, value)`、`_fmt_count(value)`、`_fmt_ratio(value)`、`_fmt_duration_sec(value)`、`_bj_iso(value)`、`_data_track_page_href(**updates)`、`_render_data_track_view_nav(active)`
- Produces: `data_track._RUNTIME_PAGE_CSS: str`（本次两个新页共用的样式常量）、
  `data_track._render_runtime_health_page(payload: dict) -> str`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_data_track_runtime_view.py`（沿用文件里已有的 `_lane` / `_payload`）：

```python
def test_render_runtime_health_page_shows_conclusion_and_lanes():
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(lane="chat"),
        _lane(lane="heartbeat", sampled_jobs=12, completed=12),
    ]))
    assert "Runtime 健康" in html_out
    assert "正常" in html_out
    assert "chat" in html_out
    assert "heartbeat" in html_out
    assert "Worker 池" in html_out


def test_render_runtime_health_page_renders_na_not_fake_zero():
    html_out = _dt._render_runtime_health_page(_payload([_lane(
        sampled_jobs=0, completed=0, failure_rate=None,
        p50_ok_ms=None, p95_ok_ms=None,
        capture={"complete": 0, "partial": 0, "missing": 0, "open": 0},
    )]))
    # 断言必须只针对渲染出的指标值。`assert "0%" not in html_out` 是错的：
    # 样式表里的 width:100% 含有 "0%" 子串，将来页面出现 10%/20%/50% 也会误伤，
    # 逼得人去改生产 CSS 迁就测试。零样本时不该出现任何 pill，这才是准确的判据。
    assert "pill" not in html_out
    assert "当前窗口无样本" in html_out


def test_render_runtime_health_page_escapes_and_buckets_failure_codes():
    leaked = "<script>alert(1)</script> 我的身份证号是 1234"
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=0.5, failed=1, completed=1,
              top_failures=[{"code": leaked, "count": 1}]),
    ]))
    assert "<script>" not in html_out
    assert "身份证号" not in html_out
    assert "other" in html_out


def test_render_runtime_health_page_points_at_break_glass_inspector():
    html_out = _dt._render_runtime_health_page(_payload([
        _lane(failure_rate=0.5, failed=1, completed=1,
              top_failures=[{"code": "turn_failed:providererror", "count": 1}]),
    ]))
    assert "上游原始错" in html_out
    assert "trajectory inspector" in html_out


def test_render_runtime_health_page_offers_window_switches():
    html_out = _dt._render_runtime_health_page(_payload())
    assert "hours=24" in html_out
    assert "hours=168" in html_out
    assert "hours=720" in html_out


def test_render_runtime_health_page_declares_scope_split():
    # 与 Proactive 日报页的口径分工必须写在页面上，否则两页数字打架时无从判断
    html_out = _dt._render_runtime_health_page(_payload())
    assert "运行时视角" in html_out
    assert "Proactive 日报" in html_out
```

**注意**：这些测试调用 `_render_runtime_health_page` 时会间接用到 `_data_track_page_href`，后者读 `request.args`。测试需要在 `admin_core.bind()` 上下文里跑。在文件顶部补 import 并加一个**显式（非 autouse）** fixture：

```python
import pytest  # noqa: E402

from admin import admin_core as _admin_core  # noqa: E402


@pytest.fixture()
def bound_request():
    """渲染纯函数需要 request 上下文才能拼 href。刻意不设 autouse——
    Task 5 的 client 测试自己会 bind，嵌套 bind 会让请求上下文互相覆盖。"""
    with _admin_core.bind(""):
        yield
```

然后给上面 6 个渲染用例每个都加 `bound_request` 参数，例如：

```python
def test_render_runtime_health_page_shows_conclusion_and_lanes(bound_request):
    ...
```

（6 个用例的函数签名全部改为接受 `bound_request`：`test_render_runtime_health_page_shows_conclusion_and_lanes`、
`test_render_runtime_health_page_renders_na_not_fake_zero`、
`test_render_runtime_health_page_escapes_and_buckets_failure_codes`、
`test_render_runtime_health_page_points_at_break_glass_inspector`、
`test_render_runtime_health_page_offers_window_switches`、
`test_render_runtime_health_page_declares_scope_split`）

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
```

Expected: 新增 6 个 FAIL（`AttributeError: ... has no attribute '_render_runtime_health_page'`），Task 3 的 12 个仍 PASS

- [ ] **Step 3a: 抽共享 CSS 常量**

现有 6 个视图各自内联一份几乎相同的 `<style>`。本次新增的两个页面（本任务的主页 +
Task 5 的降级页）**共用一个模块级常量**，不再第 7、第 8 次复制。旧 6 页不动 —— 那是独立
重构，会把本次 diff 撑大、风险盖过新功能本身。

常量是普通字符串（不是 f-string），因此 CSS 里的花括号**不需要**写成 `{{`。

在 `backend/admin/data_track.py` 里 Task 3 那段之后追加：

```python
# 本次新增的两个 Runtime 视图页共用这一份样式。刻意没有去改造既有 6 个视图页
# 各自内联的 style——那是独立重构，不该混在功能改动里。
# 普通字符串（非 f-string），花括号无需转义。
_RUNTIME_PAGE_CSS = """
    :root { color-scheme: light; --fg:#191613; --muted:#736963; --line:#e6ddd5; --bg:#fbf8f4; --card:#fffdfa; --accent:#b7352b; --ok:#1d7a4d; --warn:#a05a00; --bad:#b7352b; }
    body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { max-width:1280px; margin:0 auto; padding:28px 24px 48px; }
    h1 { font-size:26px; margin:0 0 4px; }
    h2 { font-size:16px; margin:28px 0 12px; }
    .muted { color:var(--muted); }
    .ok { color:var(--ok); }
    .warn { color:var(--warn); }
    .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:22px 0; }
    .metric { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .metric-value { font-size:24px; font-weight:700; }
    .metric-label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
    .viewbar,.sortbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:10px 0 18px; }
    .sort-button { display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:6px; padding:7px 10px; background:var(--card); color:var(--fg); font-size:13px; }
    .sort-button.active { border-color:var(--accent); color:var(--accent); background:#fff1ed; }
    .note-box { background:#fff8ef; border:1px solid #e8d8be; border-radius:8px; padding:12px 14px; margin:16px 0 4px; font-size:13px; line-height:1.6; color:#5a4d3c; }
    table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:18px; }
    th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
    th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; background:#f4ece5; }
    tr:last-child td { border-bottom:0; }
    a { color:var(--accent); text-decoration:none; }
    code { font-size:12px; }
    .pill { display:inline-flex; border-radius:999px; padding:2px 8px; font-size:12px; background:#efe7df; color:var(--muted); }
    .pill.ok { color:var(--ok); background:#e7f3ed; }
    .pill.warn { color:var(--warn); background:#fff1db; }
    .pill.bad { color:var(--bad); background:#fdeceb; }
    .bad { color:var(--bad); }
"""
```

加一条测试到 `tests/test_data_track_runtime_view.py`，钉住"两个新页共用同一份样式"：

```python
def test_runtime_pages_share_one_stylesheet(bound_request):
    # 两个新页共用 _RUNTIME_PAGE_CSS；这条测试防止将来有人又复制粘贴一份出来。
    main_page = _dt._render_runtime_health_page(_payload())
    error_page = _dt._render_runtime_health_error_page()
    assert _dt._RUNTIME_PAGE_CSS in main_page
    assert _dt._RUNTIME_PAGE_CSS in error_page
```

（这条测试引用了 Task 5 才实现的 `_render_runtime_health_error_page`，所以**放到 Task 5
的测试批次里写**，不要在 Task 4 就加。）

- [ ] **Step 3b: 写渲染函数**

在 `_RUNTIME_PAGE_CSS` 之后追加：

```python
def _render_runtime_health_page(payload: dict) -> str:
    """Runtime V2 全 lane 运行时健康值班台（?view=runtime）。

    本页 = 运行时视角（job 生命周期，窗口可切）；「Proactive 日报」= 产品视角
    （日报送达率，按天）。heartbeat lane 两页都出现但口径不同，故本页该行给出
    指向日报页的链接。
    """
    level, reasons = _runtime_health_level(payload)
    window_hours = int(payload.get("window_hours") or 24)
    lanes = payload.get("lanes") or []
    pool = payload.get("pool") or {}

    level_text = {"ok": "正常", "warn": "注意", "bad": "异常"}[level]
    # ⚠️ 最终 code review 修正（2026-07-28，I-1）：这里原来写的是
    # {"ok": "ok", "warn": "warn", "bad": "warn"}——"bad" 被映射成 CSS class
    # "warn"，页顶总体结论永远不会真正变红，100% 失败率与 6% 失败率会显示成
    # 同一个橙色。正确映射是三档对三档：
    level_cls = {"ok": "ok", "warn": "warn", "bad": "bad"}[level]
    reason_text = ("：" + "；".join(html.escape(r) for r in reasons)) if reasons else ""

    window_labels = {24: "24 小时", 168: "7 天", 720: "30 天"}
    window_links = "".join(
        f"<a class='sort-button{' active' if hours == window_hours else ''}' "
        f"href='{html.escape(_data_track_page_href(view='runtime', hours=hours), quote=True)}'>"
        f"{html.escape(window_labels[hours])}</a>"
        for hours in _RUNTIME_HEALTH_WINDOWS
    )

    pool_age = pool.get("oldest_pending_age_sec")
    pool_metrics = "".join([
        _render_metric("在飞 job", _fmt_count(pool.get("inflight"))),
        _render_metric("排队 pending", _fmt_count(pool.get("pending"))),
        _render_metric("存活 worker", _fmt_count(pool.get("live_workers"))),
        _render_metric("可执行槽位", _fmt_count(pool.get("capacity"))),
        _render_metric(
            "最老 pending 年龄",
            _fmt_duration_sec(pool_age) if pool_age is not None else "—",
        ),
    ])

    def _ms_cell(value) -> str:
        if value is None:
            return "<td class='muted'>—</td>"
        return f"<td>{value / 1000:.1f}s</td>"

    lane_rows = []
    for lane in lanes:
        name = str(lane.get("lane") or "unknown")
        rate = lane.get("failure_rate")
        if rate is None:
            rate_cell = "<td class='muted'>—</td>"
        else:
            # 三档，先判 bad 再判 warn——顺序反了 bad 会被 warn 吃掉。阈值表规定
            # <5% 绿 / 5–15% 黄 / ≥15% 红；只做两档的话 100% 失败率与 6% 会显示成
            # 同一个橙 pill，per-lane 表就失去了分诊能力。
            if rate >= _RUNTIME_HEALTH_FAILURE_BAD:
                cls = "bad"
            elif rate >= _RUNTIME_HEALTH_FAILURE_WARN:
                cls = "warn"
            else:
                cls = "ok"
            rate_cell = f"<td><span class='pill {cls}'>{rate * 100:.0f}%</span></td>"
        capture = lane.get("capture") or {}
        missing = int(capture.get("missing") or 0)
        # 四个桶都要显示。open 看似无关紧要，但一条"全部 job 未终态"的卡死 lane
        # 其余各列都是 0 或 —，open 是它在表格里唯一的存在证据（见 Task 2 的
        # lane 并集修复）。missing 用 bad 档：阈值表里 missing≥1 是红、无黄档。
        capture_cell = (
            f"<td>{int(capture.get('complete') or 0)} / "
            f"{int(capture.get('partial') or 0)} / "
            f"<b class='{'bad' if missing else ''}'>{missing}</b> / "
            f"{int(capture.get('open') or 0)}</td>"
        )
        lane_label = html.escape(name)
        if name == "heartbeat":
            hb_href = _data_track_page_href(view="proactive", hours=None, offset=0)
            lane_label += (
                f" <a class='muted' style='font-size:12px' "
                f"href='{html.escape(hb_href, quote=True)}'>（日报口径）</a>"
            )
        lane_rows.append(
            "<tr>"
            f"<td><b>{lane_label}</b></td>"
            f"<td>{_fmt_count(lane.get('sampled_jobs'))}</td>"
            f"<td>{_fmt_count(lane.get('completed'))}</td>"
            f"<td>{_fmt_count(lane.get('failed'))}</td>"
            f"<td>{_fmt_count(lane.get('expired'))}</td>"
            f"<td class='muted'>{_fmt_count(lane.get('superseded'))}</td>"
            + rate_cell
            + _ms_cell(lane.get("p50_ok_ms"))
            + _ms_cell(lane.get("p95_ok_ms"))
            + capture_cell
            + "</tr>"
        )

    failure_rows = []
    for lane in lanes:
        name = html.escape(str(lane.get("lane") or "unknown"))
        for item in lane.get("top_failures") or []:
            code = _runtime_failure_code(item.get("code"))
            failure_rows.append(
                "<tr>"
                f"<td>{name}</td>"
                f"<td><code>{html.escape(code)}</code></td>"
                f"<td>{_fmt_count(item.get('count'))}</td>"
                "</tr>"
            )

    empty_note = (
        "<div class='muted'>当前窗口无样本——这不是故障，是这条口径当天没有数据。"
        "可切到 7 天或 30 天。</div>"
        if not any(int(l.get("sampled_jobs") or 0) for l in lanes)
        else ""
    )

    # ⚠️ 最终 code review 修正（2026-07-28，I-3）：上面这个条件只看
    # "所有 lane 的 sampled_jobs 都为 0"，把「真的没数据」和「job 全部卡在
    # claimed/running 没到终态」两种情形混为一谈。reviewer 实证过后者
    # （inflight=57/capacity=8，全部 job 在飞、无 pending、worker 心跳还活着）：
    # 这版代码显示「这不是故障」+ 总体结论「正常」——数据在页面上，人被页面
    # 告知没事。修正后 empty_note 只在 "sampled_jobs 全 0 且 capture.open 全 0
    # 且 pool.inflight==0 且 pool.pending==0" 时才说「这不是故障」，否则显示
    # 「窗口内无终态 job，但有 N 个回合在飞——可能是卡死」。当前实现见
    # `backend/admin/data_track.py::_render_runtime_health_page`。

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Runtime 健康 · Feedling Data Track</title>
  <style>{_RUNTIME_PAGE_CSS}</style>
</head>
<body>
<main>
  <h1>Runtime 健康</h1>
  <div class="muted">Generated {html.escape(_bj_iso(payload.get("generated_at")))}. Metadata only; encrypted content is not read or rendered.</div>
  <h2>总体结论：<span class="{level_cls}">{html.escape(level_text)}</span></h2>
  <div class="muted">窗口 {window_hours} 小时{reason_text}</div>
  <div class="sortbar">{window_links}</div>
  {_render_data_track_view_nav("runtime")}
  <div class="note-box">
    <b>口径分工：</b>本页是<b>运行时视角</b>——按 job 生命周期统计，窗口可切。
    「<b>Proactive 日报</b>」是产品视角——按天统计日报送达率。
    heartbeat lane 两页都会出现，但口径不同，别直接对数。
    分母一律从 agent_jobs 起算，因此「完全没写 metrics」的回合不会从统计里消失。
    延迟只算成功回合：失败超时会把 p95 拉高，混在一起会让一个故障看起来像两个。
  </div>
  {empty_note}
  <h2>Worker 池</h2>
  <section class="metrics">{pool_metrics}</section>
  <h2>各 lane 健康</h2>
  <table>
    <thead><tr><th>Lane</th><th>样本</th><th>成功</th><th>失败</th><th>过期</th><th>superseded</th><th>失败率</th><th>p50(成功)</th><th>p95(成功)</th><th>捕获 完整/部分/漏写/在飞</th></tr></thead>
    <tbody>{''.join(lane_rows) if lane_rows else "<tr><td colspan='10' class='muted'>当前窗口无 job。</td></tr>"}</tbody>
  </table>
  <h2>失败原因 Top</h2>
  <table>
    <thead><tr><th>Lane</th><th>失败码</th><th>次数</th></tr></thead>
    <tbody>{''.join(failure_rows) if failure_rows else "<tr><td colspan='3' class='muted'>当前窗口无失败。</td></tr>"}</tbody>
  </table>
  <div class="muted">失败码只是分类，不含上游细节。<b>上游原始错</b>（403 余额不足 / 429 / 超时）
  留在加密 trajectory 里，需走 default-off 的 break-glass trajectory inspector 查看，
  且每次访问都会写审计。本页只有 metadata。</div>
</main>
</body>
</html>"""
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
python -m pyflakes backend/admin/data_track.py
```

Expected: 18 passed；pyflakes 无输出

- [ ] **Step 5: Commit（需用户授权）**

```bash
git add backend/admin/data_track.py tests/test_data_track_runtime_view.py
git commit -m "feat(admin): Runtime 健康值班台页面渲染"
```

---

### Task 5: 接线 —— nav、view 白名单、分发、装配注入

**Files:**
- Modify: `backend/admin/data_track.py:1221-1222`（view 白名单）、`:2121-2136`（nav）、`:93-108`（`_data_track_qs` 的 key 列表）
- Modify: `backend/admin/admin_core.py:85-104`（`page_html` 分发）
- Modify: `backend/asgi_app.py:144` 附近（装配段注入）
- Test: `tests/test_data_track_runtime_view.py`（追加路由测）

**Interfaces:**
- Consumes: Task 4 的 `_render_runtime_health_page(payload)`、Task 3 的 `_runtime_health_summary(*, within_hours)` 桩与 `_runtime_health_window_hours()`；Task 2 的 `jobs_store.recent_runtime_health(*, within_hours, limit)`
- Produces: `GET /admin/data-track?view=runtime` 返回 200 HTML；nav 上出现「Runtime 健康」；
  `data_track._render_runtime_health_error_page() -> str` 降级页

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_data_track_runtime_view.py`。这批用例要真起 client，所以在文件顶部补 import：

```python
import base64  # noqa: E402
import itertools  # noqa: E402

import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


_route_pk_counter = itertools.count(9_000)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "admin-test-token")
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Key": "admin-test-token"}


def _fake_summary(**_kw) -> dict:
    return {
        "window_hours": _kw.get("within_hours", 24),
        "generated_at": 1_800_000_000.0,
        "lanes": [{
            "lane": "chat", "sampled_jobs": 10, "completed": 9, "failed": 1,
            "expired": 0, "superseded": 0, "queue_expired": 0, "lease_expired": 0,
            "failure_rate": 0.1, "p50_ok_ms": 18_000, "p95_ok_ms": 38_000,
            "capture": {"complete": 10, "partial": 0, "missing": 0, "open": 0},
            "top_failures": [{"code": "turn_failed:providererror", "count": 1}],
        }],
        "pool": {
            "inflight": 1, "pending": 0, "live_workers": 2,
            "capacity": 8, "oldest_pending_age_sec": None,
        },
    }


def test_runtime_view_renders_and_highlights_nav(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    page = client.get(
        "/admin/data-track?view=runtime", headers=_admin_headers()
    ).get_data(as_text=True)
    assert "Runtime 健康" in page
    assert "各 lane 健康" in page
    assert "turn_failed:providererror" in page


def test_runtime_view_appears_in_nav_of_other_views(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    page = client.get("/admin/data-track", headers=_admin_headers()).get_data(as_text=True)
    assert "view=runtime" in page


def test_runtime_view_falls_back_on_invalid_hours(client, monkeypatch):
    seen = {}

    def _capture(**kw):
        seen.update(kw)
        return _fake_summary(**kw)

    monkeypatch.setattr(_dt, "_runtime_health_summary", _capture)
    client.get("/admin/data-track?view=runtime&hours=99999", headers=_admin_headers())
    assert seen["within_hours"] == 24
    client.get("/admin/data-track?view=runtime&hours=abc", headers=_admin_headers())
    assert seen["within_hours"] == 24
    client.get("/admin/data-track?view=runtime&hours=168", headers=_admin_headers())
    assert seen["within_hours"] == 168


def test_runtime_view_requires_admin(client):
    res = client.get("/admin/data-track?view=runtime")
    assert res.status_code in (401, 302, 303)


def test_runtime_health_summary_is_wired_to_jobs_store():
    # 装配段必须把桩换成真实实现，否则页面永远空白（asgi-lifespan 漏接线的老坑）
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store

    assert _dt._runtime_health_summary is jobs_store.recent_runtime_health


def test_runtime_view_degrades_to_error_card_not_500(client, monkeypatch):
    # 数据函数炸了不能把整页打成 500——值班台恰恰是出事时才被打开的那一页。
    def _boom(**_kw):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(_dt, "_runtime_health_summary", _boom)
    res = client.get("/admin/data-track?view=runtime", headers=_admin_headers())
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "Runtime 健康数据暂时取不到" in body
    assert "view=users" in body          # nav 仍在，其他视图还能点
    assert "pool exhausted" not in body  # 异常细节不外泄到页面


def test_runtime_pages_share_one_stylesheet(bound_request):
    # 两个新页共用 _RUNTIME_PAGE_CSS（Task 4 Step 3a 抽出的常量）；
    # 这条测试防止将来有人又复制粘贴出第三份。
    main_page = _dt._render_runtime_health_page(_payload())
    error_page = _dt._render_runtime_health_error_page()
    assert _dt._RUNTIME_PAGE_CSS in main_page
    assert _dt._RUNTIME_PAGE_CSS in error_page
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
```

Expected: 新增 7 个 FAIL（`view=runtime` 落回 users 页 → 断言 "各 lane 健康" 找不到；装配断言失败；错误卡片与共享样式常量不存在）

- [ ] **Step 3a: view 白名单加 runtime**

`backend/admin/data_track.py:1221-1222`，把：

```python
    raw_view = (request.args.get("view") or "users").strip().lower()
    if raw_view not in {"users", "dau", "proactive", "debug", "events"}:
        raw_view = "users"
```

改成：

```python
    raw_view = (request.args.get("view") or "users").strip().lower()
    if raw_view not in {"users", "dau", "proactive", "debug", "events", "runtime"}:
        raw_view = "users"
```

- [ ] **Step 3b: nav 加项**

`backend/admin/data_track.py:2127-2136`，在 `nav_item('events', ...)` 与 `nav_item('debug', ...)` 之间插入一行：

```python
        f"{nav_item('runtime', 'Runtime 健康')}"
```

- [ ] **Step 3c: `_data_track_qs` 保留 hours 参数**

`backend/admin/data_track.py:95-99` 的 key 元组末尾加 `"hours"`，否则窗口切换按钮与 nav 跳转会丢掉窗口选择：

```python
        "mode", "reveal", "page", "event", "day", "events_limit", "hours",
```

- [ ] **Step 3d: 错误卡片渲染函数**

在 `backend/admin/data_track.py` 里 `_render_runtime_health_page` 之后追加。值班台是出事时才被打开的那一页，数据函数炸了不能把整页打成 500：

```python
def _render_runtime_health_error_page() -> str:
    """数据取不到时的降级页：保留 nav，不外泄异常细节。"""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Runtime 健康 · Feedling Data Track</title>
  <style>{_RUNTIME_PAGE_CSS}</style>
</head>
<body>
<main>
  <h1>Runtime 健康</h1>
  {_render_data_track_view_nav("runtime")}
  <div class="note-box">
    <b>Runtime 健康数据暂时取不到。</b>
    多半是数据库连接池或 V2 表访问出了问题——这本身就是一个值得查的信号。
    其他视图不受影响，可继续使用上面的导航。具体异常见后端日志。
  </div>
</main>
</body>
</html>"""
```

- [ ] **Step 3e: `page_html` 加分发分支**

`backend/admin/admin_core.py`，在 `if view == "events":` 那段之前插入：

```python
        if view == "runtime":
            try:
                payload = data_track._runtime_health_summary(
                    within_hours=data_track._runtime_health_window_hours()
                )
            except Exception:
                logging.exception("runtime health summary failed")
                return data_track._render_runtime_health_error_page()
            return data_track._render_runtime_health_page(payload)
```

确认 `admin_core.py` 顶部已 import `logging`；没有就补上（`grep -n "^import logging" backend/admin/admin_core.py`）。

- [ ] **Step 3f: 装配段注入**

`backend/asgi_app.py`，在 `_admin_data_track._runtime_token_usage_summary = _v2_jobs_store.recent_token_usage_summary` 之后追加一行：

```python
_admin_data_track._runtime_health_summary = _v2_jobs_store.recent_runtime_health
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
python -c "import sys; sys.path.insert(0, 'backend'); import asgi_app"
python -m pyflakes backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py
```

Expected: 25 passed；`import asgi_app` 无异常（证明没成环）；pyflakes 只剩全仓恒有的那 1 条 unused

- [ ] **Step 5: 跑 admin 全量回归**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_data_track.py tests/test_data_track_debug.py \
  tests/test_data_track_runtime_view.py tests/test_v2_runtime_health.py \
  tests/test_v2_metrics_endpoint.py -q
```

Expected: 全部 passed，无既有用例被破坏

- [ ] **Step 6: 跑完整 L1 基线**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
```

Expected: 通过数不低于改动前基线，失败只剩已知的 pre-existing 红。**若 passed 数只有几百，说明 PostgreSQL 没起、DB 用例被静默跳过 —— 那份绿是假象，先起 PG 再跑。**

- [ ] **Step 7: Commit（需用户授权）**

```bash
git add backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py \
  tests/test_data_track_runtime_view.py
git commit -m "feat(admin): 接线 ?view=runtime 值班台入口"
```

---

## 完成后的验证（部署前）

- [ ] 本地起 client 手工看一眼页面：三个窗口按钮都能切、nav 高亮正确、失败码不渗内容
- [ ] 确认 `docs/CHANGELOG.md` 需要补一条（本仓库把 CHANGELOG 当"什么时候上了什么、为什么"的事实源）
- [ ] 按 `docs/testing/TESTING.md` §2 决策矩阵复核：本次改动属「backend 逻辑 + 路由」类，L1 全量 + admin 定向已覆盖；无 schema/compose/CVM/iOS 改动，无需额外档位
- [ ] 本改动不触碰公开 API 契约与架构，`docs-site/` 无需更新（CLAUDE.md 的公开文档同步规则不适用）

## 明确不在本计划内

- 自动告警接线（PR #94 的 `wire alerts to turn_health` gate 的机器版）
- 精确 trajectory 下钻入口 —— break-glass inspector 保持"需特意去用"
- 在 `v2_turn_metrics` 增列存脱敏后的 provider 错误类型（需迁移 + 改写入路径，独立立项）
- `v2_trajectory_access_audit` 审计路径的真实调用验证（三环境均 0 行，独立待办）
