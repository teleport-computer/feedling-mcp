# Runtime 值班台按 lane 的 token 统计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/admin/data-track?view=runtime` 的各 lane 健康表里，增加每条 lane 的 token 开销与缓存效率两列，窗口跟随页面切换。

**Architecture:** 数据层新增一个独立的按 lane 聚合函数（不改既有的 `recent_token_usage_summary`，users 页保持原样）；`page_html` 的 runtime 分支把窗口算一次、传给两个数据函数，消除窗口不同步的风险；渲染函数增加一个带默认值的 `tokens` 参数，因此接线完成前中间状态仍可运行。

**Tech Stack:** Python 3.11 / psycopg3 (`db.get_pool()`) / PostgreSQL / FastAPI-Starlette（admin 走 `asgi_test_client.make_client()` 的 Flask-like shim）/ pytest

**Spec:** `docs/superpowers/specs/2026-07-29-runtime-token-by-lane-design.md`

**分支:** `feat/runtime-token-by-lane`，从 `origin/test`（`29d10892`）开叉

## Global Constraints

- **依赖方向**（`CONTRIBUTING.md` §2）：`backend/admin/` 不得 import `model_api_runtime`。需要向上调用时声明桩、由 `backend/asgi_app.py` 末尾装配段注入。
- **跨模块调用写法**（`CONTRIBUTING.md` §3）：一律 `from pkg import module` + `module.func()`，禁止 `from module import func`。
- **纯只读**：无写路径、无 DDL、**不新增 alembic 迁移**。
- **token 统计全部回合，不过滤 `failed`** —— 失败回合照样烧 token。这也是它不能并入既有那条延迟查询的原因（延迟只算成功回合，过滤条件相反）。
- **无上报是 `None` 不是 `0`**；`usage_coverage` / `cache_hit_ratio` 分母为 0 时也是 `None`。unknown 与 zero 必须可区分。
- **不加 `LIMIT`** —— sum 聚合加采样上界会静默少报总量；与既有 `recent_token_usage_summary` 保持同口径，两页数字才能对账。
- **不修改** `recent_token_usage_summary`，不动 users 页现有区块。
- **两处标题都要写明窗口** —— users 页固定 30 天、本页跟随窗口，不标注的话同一指标显示两个数字会被当成 bug。
- **本仓库 commit 规则**：commit 需用户明确要求。各任务末尾的 commit 步骤在获得授权后再执行。
- **测试基线**：跑 DB 测必须先起 PostgreSQL，否则 DB 用例静默跳过、绿色是假象。测试容器 `feedling-test-pg` 已在跑（端口 55432）。本机 macOS **没有 `timeout` 命令**。

## File Structure

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `backend/model_api_runtime/v2/jobs_store.py` | 修改（在 `recent_runtime_health` 之后追加） | 新增 `recent_token_usage_by_lane()`——按 lane 的 token 聚合 |
| `backend/admin/data_track.py` | 修改 | 新增 `_fmt_tokens_compact()` 与注入桩 `_runtime_token_by_lane`；`_render_runtime_health_page` 增加 `tokens` 参数与两列 |
| `backend/admin/admin_core.py` | 修改 `page_html`（`:97-105`） | runtime 分支改为窗口算一次、调两个数据函数 |
| `backend/asgi_app.py` | 修改装配段（`:149` 之后） | 注入 `_runtime_token_by_lane = _v2_jobs_store.recent_token_usage_by_lane` |
| `tests/test_v2_runtime_health.py` | 追加 | 数据层 DB 测 |
| `tests/test_data_track_runtime_view.py` | 追加 | 渲染纯函数测 + 路由测 |

---

### Task 1: `recent_token_usage_by_lane()` —— 按 lane 的 token 聚合

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`（在 `recent_runtime_health` 函数之后追加，该函数起始于 `:3912`）
- Test: `tests/test_v2_runtime_health.py`（追加）

**Interfaces:**
- Consumes: 现有 `jobs_store._pool()`、`jobs_store.record_whole_turn_metric(job_id, user_id, lane, *, prompt_tokens, completion_tokens, latency_ms, model_calls, retries, failed, status, cache_read_tokens=None, cache_write_tokens=None, cache_miss_tokens=None, usage_reported_calls=0, cache_reported_calls=0, ...)`；测试辅助 `conftest.seed_user`
- Produces: `jobs_store.recent_token_usage_by_lane(*, within_hours: int = 24) -> dict`，形如 `{"window_hours": int, "lanes": {"<lane>": {"model_calls": int, "usage_reported_calls": int, "usage_coverage": float|None, "prompt_tokens": int|None, "completion_tokens": int|None, "total_tokens": int|None, "cache_read_tokens": int|None, "cache_miss_tokens": int|None, "cache_hit_ratio": float|None}}}`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_v2_runtime_health.py` 末尾。注意该文件已有 `_clean_tables` autouse fixture（清 `v2_turn_metrics` 与 `agent_jobs`）与 `_add_job` 辅助函数，**沿用它们，不要重写**：

```python
def _add_metric(
    user_id: str,
    lane: str,
    *,
    prompt: int | None,
    completion: int | None,
    failed: bool = False,
    model_calls: int = 1,
    usage_reported: int = 1,
    cache_read: int | None = None,
    cache_miss: int | None = None,
    age_hours: int = 0,
) -> None:
    """直接写一行 v2_turn_metrics。job_id 传 None——该列的唯一索引允许多个 NULL。"""
    seed_user(user_id)
    jobs_store.record_whole_turn_metric(
        None, user_id, lane,
        prompt_tokens=prompt, completion_tokens=completion, latency_ms=1000,
        model_calls=model_calls, retries=0, failed=failed,
        status="turn_failed:providererror" if failed else "ok",
        cache_read_tokens=cache_read, cache_miss_tokens=cache_miss,
        usage_reported_calls=usage_reported,
    )
    if age_hours:
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE v2_turn_metrics SET created_at=clock_timestamp()"
                "-make_interval(hours => %s) WHERE user_id=%s",
                (age_hours, user_id),
            )


def test_token_usage_by_lane_groups_by_lane():
    _add_metric("u_tok_chat_1", "chat", prompt=1000, completion=100)
    _add_metric("u_tok_chat_2", "chat", prompt=2000, completion=200)
    _add_metric("u_tok_hb", "heartbeat", prompt=500, completion=50)

    lanes = jobs_store.recent_token_usage_by_lane(within_hours=24)["lanes"]

    assert lanes["chat"]["prompt_tokens"] == 3000
    assert lanes["chat"]["completion_tokens"] == 300
    assert lanes["chat"]["total_tokens"] == 3300
    assert lanes["heartbeat"]["prompt_tokens"] == 500
    assert lanes["heartbeat"]["total_tokens"] == 550


def test_token_usage_by_lane_counts_failed_turns():
    # 失败回合照样烧 token（provider 已经算过钱了），必须计入——这是它与延迟
    # 分位数（只算成功回合）口径相反的地方。
    _add_metric("u_tok_ok", "chat", prompt=1000, completion=100, failed=False)
    _add_metric("u_tok_bad", "chat", prompt=3000, completion=0, failed=True)

    lanes = jobs_store.recent_token_usage_by_lane()["lanes"]

    assert lanes["chat"]["prompt_tokens"] == 4000     # 两条都算
    assert lanes["chat"]["model_calls"] == 2


def test_token_usage_by_lane_reports_none_not_zero_without_usage():
    # provider 没回 usage 时不得记成 0 token 混进总量假装正常
    _add_metric("u_tok_nousage", "chat", prompt=None, completion=None, usage_reported=0)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["prompt_tokens"] is None
    assert chat["completion_tokens"] is None
    assert chat["total_tokens"] is None
    assert chat["model_calls"] == 1
    assert chat["usage_reported_calls"] == 0
    assert chat["usage_coverage"] == pytest.approx(0.0)


def test_token_usage_by_lane_coverage_is_none_without_calls():
    # model_calls 为 0 → 覆盖率没有分母，必须是 None 而非 0.0
    _add_metric("u_tok_nocalls", "chat", prompt=None, completion=None,
                model_calls=0, usage_reported=0)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["model_calls"] == 0
    assert chat["usage_coverage"] is None


def test_token_usage_by_lane_cache_hit_ratio():
    _add_metric("u_tok_cache", "chat", prompt=1000, completion=100,
                cache_read=600, cache_miss=400)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["cache_read_tokens"] == 600
    assert chat["cache_miss_tokens"] == 400
    assert chat["cache_hit_ratio"] == pytest.approx(0.6)


def test_token_usage_by_lane_cache_ratio_is_none_without_cache_data():
    _add_metric("u_tok_nocache", "chat", prompt=1000, completion=100,
                cache_read=None, cache_miss=None)

    chat = jobs_store.recent_token_usage_by_lane()["lanes"]["chat"]

    assert chat["cache_hit_ratio"] is None


def test_token_usage_by_lane_respects_window():
    _add_metric("u_tok_recent", "chat", prompt=1000, completion=100)
    _add_metric("u_tok_old", "chat", prompt=9000, completion=900, age_hours=48)

    lanes_24 = jobs_store.recent_token_usage_by_lane(within_hours=24)["lanes"]
    lanes_168 = jobs_store.recent_token_usage_by_lane(within_hours=168)["lanes"]

    assert lanes_24["chat"]["prompt_tokens"] == 1000
    assert lanes_168["chat"]["prompt_tokens"] == 10000


def test_token_usage_by_lane_is_empty_without_history():
    out = jobs_store.recent_token_usage_by_lane()
    assert out["lanes"] == {}
    assert out["window_hours"] == 24
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_runtime_health.py -v -k token_usage_by_lane
```

Expected: FAIL —— `AttributeError: module 'model_api_runtime.v2.jobs_store' has no attribute 'recent_token_usage_by_lane'`

- [ ] **Step 3: 写实现**

在 `backend/model_api_runtime/v2/jobs_store.py` 的 `recent_runtime_health` 函数之后追加：

```python
def recent_token_usage_by_lane(*, within_hours: int = 24) -> dict:
    """按 lane 的 token 开销汇总（content-free），喂 admin 值班台。

    与 ``recent_runtime_health`` 的延迟分位数口径**相反**：那里只算成功回合
    （失败超时会把 p95 拉到与故障同源的高位），这里算全部回合——失败回合照样
    烧 token，provider 已经算过钱了。

    刻意不加 ``LIMIT``：sum 聚合加采样上界会静默少报总量（"最新 N 条的 token
    和"不是任何人想要的数字）。扫描量由 ``ix_v2_turn_metrics_lane_created_at``
    控制，其前缀正是 ``lane``。

    token 为空一律 ``None`` 而非 ``0``：provider 未回 usage 的调用应当降低
    ``usage_coverage``，而不是被记成零 token 混进总量假装正常。
    """
    safe_hours = max(1, min(int(within_hours), 24 * 366))

    with _pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT lane,"
                "  coalesce(sum(model_calls), 0)::bigint AS model_calls,"
                "  coalesce(sum(usage_reported_calls), 0)::bigint"
                "    AS usage_reported_calls,"
                "  sum(prompt_tokens)::bigint AS prompt_tokens,"
                "  sum(completion_tokens)::bigint AS completion_tokens,"
                "  sum(cache_read_tokens)::bigint AS cache_read_tokens,"
                "  sum(cache_miss_tokens)::bigint AS cache_miss_tokens "
                "FROM v2_turn_metrics "
                "WHERE created_at >= now() - make_interval(hours => %s) "
                "GROUP BY lane",
                (safe_hours,),
            )
            rows = cur.fetchall()

    def _optional_int(row, key):
        value = row.get(key)
        return int(value) if value is not None else None

    lanes: dict[str, dict] = {}
    for row in rows:
        model_calls = int(row["model_calls"] or 0)
        usage_calls = int(row["usage_reported_calls"] or 0)
        prompt_tokens = _optional_int(row, "prompt_tokens")
        completion_tokens = _optional_int(row, "completion_tokens")
        cache_read = _optional_int(row, "cache_read_tokens")
        cache_miss = _optional_int(row, "cache_miss_tokens")
        cache_denominator = (cache_read or 0) + (cache_miss or 0)
        lanes[str(row["lane"] or "unknown")] = {
            "model_calls": model_calls,
            "usage_reported_calls": usage_calls,
            "usage_coverage": (
                float(usage_calls) / float(model_calls) if model_calls else None
            ),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": (
                prompt_tokens + completion_tokens
                if prompt_tokens is not None and completion_tokens is not None
                else None
            ),
            "cache_read_tokens": cache_read,
            "cache_miss_tokens": cache_miss,
            "cache_hit_ratio": (
                float(cache_read or 0) / float(cache_denominator)
                if cache_denominator
                else None
            ),
        }

    return {"window_hours": safe_hours, "lanes": lanes}
```

实现注意：
- `dict_row` 在该文件顶部已 import，无需新增。
- 不过滤 `failed`——这是刻意的，见 docstring。
- `safe_hours` 上界取 `24 * 366`，与该函数按小时接参的语义匹配（`recent_runtime_health` 用的是 `24 * 30`，那是健康快照的合理上界，此处是开销统计，允许更长窗口）。

- [ ] **Step 4: 跑测试确认通过**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_runtime_health.py -v
```

Expected: 8 个新用例全过；该文件既有用例全部仍过

- [ ] **Step 5: 跑既有 V2 回归**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_v2_turn_metrics.py tests/test_v2_jobs_store.py \
  tests/test_v2_metrics_endpoint.py -q
python -m pyflakes backend/model_api_runtime/v2/jobs_store.py
```

Expected: 全过；pyflakes 无输出

- [ ] **Step 6: Commit（需用户授权）**

```bash
git add backend/model_api_runtime/v2/jobs_store.py tests/test_v2_runtime_health.py
git commit -m "feat(v2): recent_token_usage_by_lane 按 lane 的 token 聚合"
```

---

### Task 2: 渲染两列 token

**Files:**
- Modify: `backend/admin/data_track.py`（`_fmt_ratio` 在 `:2251`，新函数加在其后；`_render_runtime_health_page` 在 `:2425`；lane 表头在 `:2581`）
- Test: `tests/test_data_track_runtime_view.py`（追加）

**Interfaces:**
- Consumes: Task 1 的返回结构；现有 `_fmt_ratio(value) -> str`（`None → "—"`）、`_fmt_count`、`html.escape`
- Produces:
  - `data_track._fmt_tokens_compact(value) -> str` —— `None → "—"`、`< 1000 → "951"`、`< 1e6 → "951.2k"`、`>= 1e6 → "1.2M"`
  - `data_track._render_runtime_health_page(payload: dict, tokens: dict | None = None) -> str` —— **第二个参数带默认值 `None`**，因此本任务完成后 `admin_core.page_html` 的既有单参数调用仍然可用；Task 3 才传实参

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_data_track_runtime_view.py`。该文件已有 `_lane()` / `_payload()` 辅助与 `bound_request` fixture（**非 autouse，渲染测试需显式声明**），沿用它们：

```python
def _tokens(lane_name: str = "chat", **overrides) -> dict:
    base = {
        "model_calls": 118,
        "usage_reported_calls": 103,
        "usage_coverage": 0.873,
        "prompt_tokens": 951_161,
        "completion_tokens": 40_473,
        "total_tokens": 991_634,
        "cache_read_tokens": 469_353,
        "cache_miss_tokens": 482_000,
        "cache_hit_ratio": 0.493,
    }
    base.update(overrides)
    return {"window_hours": 24, "lanes": {lane_name: base}}


def test_fmt_tokens_compact_covers_all_branches():
    assert _dt._fmt_tokens_compact(None) == "—"
    assert _dt._fmt_tokens_compact(951) == "951"
    assert _dt._fmt_tokens_compact(951_161) == "951.2k"
    assert _dt._fmt_tokens_compact(1_200_000) == "1.2M"


def test_render_runtime_health_page_shows_token_columns(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "951.2k" in html_out          # prompt
    assert "40.5k" in html_out           # completion
    assert "49.3%" in html_out           # cache 命中率
    assert "87.3%" in html_out           # 上报覆盖率
    assert "token 入/出" in html_out      # 表头
    assert "缓存命中 · 上报" in html_out  # 表头


def test_render_runtime_health_page_token_columns_are_dash_without_data(bound_request):
    # 某 lane 有 job 但无任何 turn metric 行——两列显 —，且不得抛 KeyError。
    # payload 里的 lane 是 chat，tokens 里只有 heartbeat，所以 chat 行取不到数据。
    html_out = _dt._render_runtime_health_page(_payload(), _tokens(lane_name="heartbeat"))
    assert "token 入/出" in html_out
    # 精确断言：token 与 cache 两列都渲染成 muted 的 —。
    # 只写 `assert "—" in html_out` 是无效断言——页面别处本来就有 —。
    assert html_out.count("<td class='muted'>—</td>") >= 2
    # heartbeat 的数字绝不能串到 chat 行上
    assert "951.2k" not in html_out


def test_render_runtime_health_page_tolerates_missing_tokens_arg(bound_request):
    # 不传 tokens（Task 3 接线前的中间状态）必须仍可渲染
    html_out = _dt._render_runtime_health_page(_payload())
    assert "各 lane 健康" in html_out
    assert "token 入/出" in html_out


def test_render_runtime_health_page_explains_token_scope(bound_request):
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "失败回合" in html_out        # token 含失败回合
    assert "不要与缓存列相加" in html_out  # prompt 已含 cache read/write
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v -k "token or compact"
```

Expected: FAIL —— `AttributeError: module 'admin.data_track' has no attribute '_fmt_tokens_compact'`

- [ ] **Step 3a: 新增 `_fmt_tokens_compact`**

在 `backend/admin/data_track.py` 的 `_fmt_ratio`（`:2251`）之后插入：

```python
def _fmt_tokens_compact(value) -> str:
    """Token 计数的紧凑写法。lane 健康表已有 10 列，千分位会把列宽撑爆。"""
    if value is None:
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
```

- [ ] **Step 3b: 渲染函数加参数与两列**

把 `_render_runtime_health_page` 的签名（`:2425`）改为：

```python
def _render_runtime_health_page(payload: dict, tokens: dict | None = None) -> str:
```

在函数内 `lane_rows` 循环里，`capture_cell` 之后（现位于 `:2483-2488`）、`lane_label` 之前插入：

```python
        # 某 lane 有 job 但无 turn metric 行时（例如全部回合都还没终态），
        # tokens["lanes"] 里没有这个键——两列显 —，不得 KeyError、也不得显 0。
        lane_tokens = ((tokens or {}).get("lanes") or {}).get(name) or {}
        prompt_tok = lane_tokens.get("prompt_tokens")
        completion_tok = lane_tokens.get("completion_tokens")
        if prompt_tok is None and completion_tok is None:
            token_cell = "<td class='muted'>—</td>"
        else:
            token_cell = (
                f"<td>{_fmt_tokens_compact(prompt_tok)} / "
                f"{_fmt_tokens_compact(completion_tok)}</td>"
            )
        hit_ratio = lane_tokens.get("cache_hit_ratio")
        coverage = lane_tokens.get("usage_coverage")
        if hit_ratio is None and coverage is None:
            cache_cell = "<td class='muted'>—</td>"
        else:
            cache_cell = (
                f"<td>{_fmt_ratio(hit_ratio)} · {_fmt_ratio(coverage)}</td>"
            )
```

在同一循环末尾拼接 `lane_rows.append(...)` 的表达式里，把 `+ capture_cell` 之后改为 `+ capture_cell + token_cell + cache_cell`。

- [ ] **Step 3c: 更新表头与说明**

表头（`:2581`）在 `<th>捕获 完整/部分/漏写/在飞</th>` 之后追加两列：

```html
<th>token 入/出</th><th>缓存命中 · 上报</th>
```

页顶 `note-box` 的说明文字末尾追加三句（在既有那段"延迟只算成功回合…"之后）：

```
token 含<b>失败回合</b>——失败也烧钱，与上方失败率不是同一批样本的筛选口径。
prompt token 已包含 cache read/write，<b>不要与缓存列相加</b>，否则重复计数。
本页 token 跟随上方窗口；users 页「运营 Telemetry」固定近 30 天，
两处数字不一致是<b>窗口不同</b>，不是 bug——切到 30 天时应当一致。
```

第三句是 spec §6 的要求：两处并存且窗口口径不同，不标注的话同一指标显示两个数字会被
当成 bug。页顶已有「窗口 N 小时」（`data_track.py:2566`），此处补的是与另一页的关系。

对应补一条测试到 Step 1 的测试批次里：

```python
def test_render_runtime_health_page_declares_window_difference(bound_request):
    # spec §6：两页口径不同必须写明，否则数字对不上会被当成 bug
    html_out = _dt._render_runtime_health_page(_payload(), _tokens())
    assert "固定近 30 天" in html_out
    assert "不是 bug" in html_out
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_data_track_runtime_view.py -v
python -c "import sys; sys.path.insert(0,'backend'); from admin import data_track; print('import ok')"
python -m pyflakes backend/admin/data_track.py
```

Expected: 全过（含该文件既有用例）；import 成功；pyflakes 无输出

- [ ] **Step 5: Commit（需用户授权）**

```bash
git add backend/admin/data_track.py tests/test_data_track_runtime_view.py
git commit -m "feat(admin): Runtime 值班台 lane 表增加 token 两列"
```

---

### Task 3: 接线 —— 窗口算一次传两处

**Files:**
- Modify: `backend/admin/data_track.py`（在 `_runtime_health_summary` 桩 `:2379` 之后加新桩）
- Modify: `backend/admin/admin_core.py:97-105`（`page_html` 的 runtime 分支）
- Modify: `backend/asgi_app.py:149` 之后（装配段）
- Test: `tests/test_data_track_runtime_view.py`（追加路由测）

**Interfaces:**
- Consumes: Task 1 的 `jobs_store.recent_token_usage_by_lane(*, within_hours=24)`；Task 2 的 `_render_runtime_health_page(payload, tokens=None)`；现有 `_runtime_health_window_hours() -> int`、`_runtime_health_summary(*, within_hours=24)`、`_render_runtime_health_error_page()`
- Produces: `data_track._runtime_token_by_lane(*, within_hours: int = 24) -> dict` 注入桩；接线后 `GET /admin/data-track?view=runtime` 的页面含 token 两列

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_data_track_runtime_view.py`。该文件已有 `client` fixture 与 `_admin_headers()`（返回 `{"X-Admin-Token": "admin-test-token"}`），沿用：

```python
def _fake_tokens(**kw) -> dict:
    return {
        "window_hours": kw.get("within_hours", 24),
        "lanes": {"chat": {
            "model_calls": 10, "usage_reported_calls": 9, "usage_coverage": 0.9,
            "prompt_tokens": 500_000, "completion_tokens": 20_000,
            "total_tokens": 520_000, "cache_read_tokens": 300_000,
            "cache_miss_tokens": 200_000, "cache_hit_ratio": 0.6,
        }},
    }


def test_runtime_view_passes_same_window_to_both_data_functions(client, monkeypatch):
    # 方案 B 的核心风险：两个数据函数的窗口必须同步。窗口在 page_html 里算一次、
    # 传给两处，因此不可能出现一个 24 小时、一个 720 小时。
    seen = {}

    def _health(**kw):
        seen["health"] = kw.get("within_hours")
        return _fake_summary(**kw)

    def _tokens(**kw):
        seen["tokens"] = kw.get("within_hours")
        return _fake_tokens(**kw)

    monkeypatch.setattr(_dt, "_runtime_health_summary", _health)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _tokens)

    client.get("/admin/data-track?view=runtime&hours=168", headers=_admin_headers())
    assert seen["health"] == 168
    assert seen["tokens"] == 168

    client.get("/admin/data-track?view=runtime&hours=99999", headers=_admin_headers())
    assert seen["health"] == 24      # 非法值两处一起回落
    assert seen["tokens"] == 24


def test_runtime_view_renders_token_columns_end_to_end(client, monkeypatch):
    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _fake_tokens)
    page = client.get("/admin/data-track?view=runtime", headers=_admin_headers()).get_data(as_text=True)
    assert "token 入/出" in page
    assert "500.0k" in page
    assert "60.0%" in page


def test_runtime_view_degrades_when_token_function_fails(client, monkeypatch):
    # 任一数据源炸掉都走同一个降级页，且不外泄异常细节
    def _boom(**_kw):
        raise RuntimeError("token pool exhausted")

    monkeypatch.setattr(_dt, "_runtime_health_summary", _fake_summary)
    monkeypatch.setattr(_dt, "_runtime_token_by_lane", _boom)
    res = client.get("/admin/data-track?view=runtime", headers=_admin_headers())
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "Runtime 健康数据暂时取不到" in body
    assert "token pool exhausted" not in body


def test_runtime_token_by_lane_is_wired_to_jobs_store():
    # 装配段必须把桩换成真实实现，否则 token 列永远空白而不报任何错
    import asgi_app  # noqa: F401
    from model_api_runtime.v2 import jobs_store

    assert _dt._runtime_token_by_lane is jobs_store.recent_token_usage_by_lane
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_data_track_runtime_view.py -v -k "window or token or wired"
```

Expected: FAIL —— `AttributeError: module 'admin.data_track' has no attribute '_runtime_token_by_lane'`

- [ ] **Step 3a: 加注入桩**

在 `backend/admin/data_track.py` 的 `_runtime_health_summary` 桩（`:2379`）之后插入：

```python
# Injected by the assembly layer (asgi_app.py); the real implementation is
# model_api_runtime.v2.jobs_store.recent_token_usage_by_lane.
def _runtime_token_by_lane(*, within_hours: int = 24) -> dict:
    return {"window_hours": within_hours, "lanes": {}}
```

- [ ] **Step 3b: 改 `page_html` 的 runtime 分支**

把 `backend/admin/admin_core.py:97-105` 替换为：

```python
        if view == "runtime":
            # 窗口算一次、传给两个数据函数——两处各自读 request.args 会让窗口
            # 有机会不一致（同页一个 24 小时、一个 720 小时）。
            hours = data_track._runtime_health_window_hours()
            try:
                payload = data_track._runtime_health_summary(within_hours=hours)
                tokens = data_track._runtime_token_by_lane(within_hours=hours)
            except Exception:
                logging.exception("runtime health summary failed")
                return data_track._render_runtime_health_error_page()
            return data_track._render_runtime_health_page(payload, tokens)
```

- [ ] **Step 3c: 装配段注入**

在 `backend/asgi_app.py:149`（`_admin_data_track._runtime_health_summary = ...`）之后追加一行：

```python
_admin_data_track._runtime_token_by_lane = _v2_jobs_store.recent_token_usage_by_lane
```

- [ ] **Step 4: 跑测试确认通过**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_data_track_runtime_view.py -v
python -c "import sys; sys.path.insert(0,'backend'); import asgi_app; print('no cycle')"
python -m pyflakes backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py
```

Expected: 全过；`import asgi_app` 无异常；pyflakes 只剩全仓恒有的那 1 条 unused

- [ ] **Step 5: admin 定向回归**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/test_data_track.py tests/test_data_track_debug.py \
  tests/test_data_track_runtime_view.py tests/test_v2_runtime_health.py \
  tests/test_v2_metrics_endpoint.py -q
```

Expected: 全过，既有 admin 行为未被破坏

- [ ] **Step 6: L1 全量**

```bash
DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
  python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
```

Expected: 失败数不高于分支起点基线，通过数 = 基线 + 本计划新增用例数。**若 passed 只有几百，说明 PostgreSQL 没起、DB 用例被静默跳过 —— 那份绿是假象。**

- [ ] **Step 7: Commit（需用户授权）**

```bash
git add backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py \
  tests/test_data_track_runtime_view.py
git commit -m "feat(admin): 接线 Runtime 值班台的 token 数据源"
```

---

## 完成后的验证

- [ ] 手工看一眼页面：三个窗口按钮切换时 token 两列跟着变；无数据的 lane 显 `—` 不显 0
- [ ] 窗口切到 30 天时，chat lane 的 token 数字应与 users 页「运营 Telemetry」区块一致（两页口径自洽性检查）
- [ ] `docs/CHANGELOG.md` 补一条（本仓库把 CHANGELOG 当"什么时候上了什么、为什么"的事实源）
- [ ] 按 `docs/testing/TESTING.md` §2 决策矩阵复核：本次属「backend 逻辑 + 路由」类，L1 全量 + admin 定向已覆盖；无 schema/compose/CVM/iOS 改动
- [ ] 不触碰公开 API 契约与架构，`docs-site/` 无需更新

## 明确不在本计划内

- token → 金额的成本换算（各 provider 计价不同，独立立项）
- 趋势图与历史对比
- 修改 `recent_token_usage_summary` 或 users 页现有区块
- 统一两页的窗口口径（刻意保留差异，仅要求标注清楚）

## Post-review 修正记录（2026-07-29，整分支 code review 后）

本计划 Task 1 / Task 3 里贴出的代码片段照抄进了实现，但下列内容后来被 review 证伪或判定
不够充分。**这里不改历史代码片段本身**（那是当时怎么写的忠实记录），只记录哪里错了、真相
是什么——完整的修正版本见
`docs/superpowers/specs/2026-07-29-runtime-token-by-lane-design.md`（已同步更新）与实际
提交的代码。

- **Task 1 Step 3 的 docstring**（"扫描量由 `ix_v2_turn_metrics_lane_created_at` 控制，
  其前缀正是 `lane`"）**方向反了**：该索引是 `(lane, created_at DESC)`，`lane` 打头恰恰
  意味着它服务不了这条无 lane 等值谓词的查询（PG 16 无 skip scan）。本地 PG 16 实测走
  Parallel Seq Scan。"不加 LIMIT"的决策本身没错，只是原先的依据错了。见 design §3。
- **Task 1 Step 3 的 `cache_hit_ratio` 算法**（`cache_denominator = (cache_read or 0) +
  (cache_miss or 0)`）与 users 页既有同名指标算法不一致：`cache_read=None,
  cache_miss=500` 会显 `0.0%`，反向 `cache_read=500, cache_miss=None` 会显
  **`100.0%`**（假装缓存完美命中，真相是 miss 没上报）——Anthropic 只有 cache write 无
  cache read 的回合确实会产出这种组合。已改为与 users 页对齐：任一为 `None` → ratio 为
  `None`。见 design §6。
- **Task 3 Step 3b 注释**（"两个函数不各自读 `request.args`，因此不可能出现窗口不一致"）
  只覆盖了调用方，没覆盖被调方各自的钳制上界（`recent_runtime_health` 钳 `24*30`，
  `recent_token_usage_by_lane` 钳 `24*366`）。今天两者巧合相等（`720 == 24*30`），不是
  不变量。已加白名单守卫测试把这个巧合钉死。见 design §4。
- **Task 2 的渲染逻辑**遍历的 lane 集合只来自 `payload["lanes"]`，没有与
  `tokens["lanes"]` 取并集——一条"窗口内有 token 开销、但 job 没挤进健康侧
  `LIMIT 1000` 采样"的 lane 会不显示也不报错。已补并集逻辑，见 design §5。
- **Task 2 的 `_fmt_tokens_compact`** 在 `[999_950, 1_000_000)` 与
  `[999_950_000, 1_000_000_000)` 两个区间会因"先除后 `.1f`"错误显示成上一档的
  `"1000.0k"` / `"1000.0M"`。真实边界是 999_950，不是 999_500。已按格式化结果收紧阈值。
