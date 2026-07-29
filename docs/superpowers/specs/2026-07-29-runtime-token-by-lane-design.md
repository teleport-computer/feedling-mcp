# Runtime 健康值班台增加按 lane 的 token 统计 — 设计

- 日期：2026-07-29
- 状态：设计已确认，待出实施计划
- 前置：PR #124（`/admin/data-track?view=runtime` 值班台，已合入 `test`）
- 分支：`feat/runtime-token-by-lane`，从 `origin/test` 开叉

## 1. 背景与目标

值班台（`?view=runtime`）现在只回答"跑得好不好"，不回答"花了多少"。token 统计目前只存在于
`/admin/data-track` 默认 users 页的「运营 Telemetry」区块，且是**全站 chat lane、固定 30 天**
的单一口径。

两个缺口：

1. **非 chat lane 的开销完全不可见。** `recent_token_usage_summary(lane="chat")` 只统计
   chat。而心跳 lane 烧闲置用户 BYOK 是出过事的——`usr_57c24d0d` 零聊天、65 个回合全是
   sleep，credit 被烧穿；当前口径看不到这类消耗。
2. **健康与开销分处两页、两种窗口。** 值班时要在"哪条 lane 在炸"和"哪条 lane 在烧钱"之间
   来回切页面，且两页窗口口径不同。

**目标**：在值班台的各 lane 健康表里直接看到每条 lane 的 token 开销与缓存效率，与健康指标
同窗口、同一屏。

**非目标**：不做成本换算（token → 金额，涉及各 provider 计价，独立立项）；不做趋势图；不动
users 页现有区块的行为。

## 2. 已确认的三个决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 时间窗口 | **跟随页面窗口切换**（24h / 7天 / 30天） | 同页数字口径必须一致，否则会把 24 小时的失败率和 30 天的 token 当成同一回事 |
| users 页现有区块 | **保留，不动** | 不打扰现有使用习惯 |
| lane 维度 | **按 lane 拆** | 心跳/维护 lane 的开销正是当前盲区 |
| 数据层形态 | **新增独立函数**（方案 B） | 职责边界清晰；代价是窗口需两处同步，见 §4 的消除办法 |

## 3. 数据层

新增 `jobs_store.recent_token_usage_by_lane(*, within_hours: int = 24) -> dict`。

返回结构：

```python
{
  "window_hours": 24,
  "lanes": {                       # 以 lane 名为键，便于渲染层按行查表
    "chat": {
      "model_calls": 118,
      "usage_reported_calls": 103,
      "usage_coverage": 0.873,     # usage_reported_calls / model_calls；分母 0 → None
      "prompt_tokens": 951161,     # 无任何上报 → None
      "completion_tokens": 40473,
      "total_tokens": 991634,      # prompt + completion；任一为 None → None
      "cache_read_tokens": 469353,
      "cache_miss_tokens": 482000,
      "cache_hit_ratio": 0.493,    # read / (read + miss)；分母 0 → None
    },
  },
}
```

### 三条口径约束

**token 统计全部回合，不过滤 `failed`。** 失败回合照样烧 token（provider 已经算过钱了）。
这也是它**不能**并入现有那条延迟查询的原因——延迟只算成功回合（`failed IS NOT TRUE`），
两者过滤条件相反，合并会少算失败轮的开销。

**无上报是 `None`，不是 `0`。** 沿用 `recent_token_usage_summary` 的既有语义：provider 未回
usage 的调用降低 `usage_coverage`，而不是被记成零 token 混进总量假装正常。`prompt_tokens`
用 `sum()` 的天然 NULL 传播实现——全无上报时 sum 返回 NULL → `None`。

**不加 `LIMIT` 采样上界。** 与既有 `recent_token_usage_summary` 一致。sum 聚合加 LIMIT 会
静默少报总量（"最新 1000 条的 token 和"不是任何人想要的数字）。扫描量由索引控制：
`ix_v2_turn_metrics_lane_created_at` 的前缀就是 `lane`，`GROUP BY lane` + 时间范围走得到它。

窗口直接用 `make_interval(hours => %s)`，不换算成天——比 `within_days` 精确，且与
`recent_runtime_health` 的窗口参数同形。

### 与既有函数的关系

`recent_token_usage_summary` **保持不变**，继续给 users 页供数。两个函数查同一张表但口径
不同（单 lane/固定天数 vs 全 lane/小时窗口），刻意不合并——合并意味着改动正在服务 users 页
的函数签名。

## 4. 接线

`admin_core.page_html` 的 `view == "runtime"` 分支改为：

```python
if view == "runtime":
    hours = data_track._runtime_health_window_hours()
    try:
        payload = data_track._runtime_health_summary(within_hours=hours)
        tokens = data_track._runtime_token_by_lane(within_hours=hours)
    except Exception:
        logging.exception("runtime health summary failed")
        return data_track._render_runtime_health_error_page()
    return data_track._render_runtime_health_page(payload, tokens)
```

**窗口一处计算、两处传参** —— 这是方案 B「窗口需在两处同步」风险的消除办法。两个函数不各自
读 `request.args`，因此不可能出现窗口不一致。

`try/except` 覆盖**两次**调用：任一数据源失败都走同一个降级页。降级页本身不变。

`data_track` 侧新增注入桩 `_runtime_token_by_lane(*, within_hours: int = 24) -> dict`，返回
空结构；`asgi_app.py` 装配段注入 `_v2_jobs_store.recent_token_usage_by_lane`，紧跟现有
`_runtime_health_summary` 那一行，写法一致。admin 层仍不 import `model_api_runtime`。

## 5. 渲染

`_render_runtime_health_page(payload, tokens)` 增加第二个参数。各 lane 健康表增加 2 列
（10 → 12 列）：

| 列 | 内容 | 无数据 |
| --- | --- | --- |
| token 入/出 | `951.2k / 40.5k` | `—` |
| 缓存命中 · 上报 | `49% · 87%` | `—` |

紧凑格式的理由：表格已有 10 列，token 相关有 5 个数值指标，全部展开会把表撑到不可读。入/出
分子分母同格、命中率与覆盖率同格，各占一列。

数值格式化复用既有 `_fmt_count`（千分位）与 `_fmt_ratio`（百分比，`None → —`）。大数用 k/M
缩写以控制列宽——新增一个 `_fmt_tokens_compact(value)` 纯函数：`None → "—"`、`< 1000 → "951"`、
`< 1e6 → "951.2k"`、`>= 1e6 → "1.2M"`。

某条 lane 在 `tokens["lanes"]` 中不存在时（该 lane 有 job 但无任何 turn metric 行），两列均
渲染 `—`——不得因缺键抛 `KeyError`，也不得渲染成 0。

页顶说明区补两句：

- token 含**失败回合**（失败也烧钱），与上方失败率不是同一批样本的筛选口径
- `prompt_tokens` 已包含 cache read/write，**不要**与缓存列相加，否则重复计数

## 6. 与 users 页的口径差异

两处并存且窗口口径不同（users 固定 30 天、本页跟随窗口），因此**两处标题都必须写明窗口**。
users 页现有写法已是「近 30 天」，本页的列头与说明须同样标注当前窗口，避免同一指标在两页
显示不同数字时被当成 bug。

窗口切到 30 天时两页 chat lane 的数字应当一致——这也是一条可人工核对的自洽性检查。

## 7. 边界与错误处理

- 纯只读，无写路径，**无新增迁移**。
- 某 lane 有 job 无 metric 行 → 该 lane 的 token 列显示 `—`（见 §5）。
- 数据函数抛异常 → 降级页（沿用既有行为，覆盖两次调用）。
- `window_hours` 的白名单与回落逻辑不变，仍由 `_runtime_health_window_hours()` 统一处理。

## 8. 测试

**数据层 DB 测**（需 PostgreSQL）：
- 按 lane 正确分组，多 lane 混合数据下互不串扰
- **失败回合的 token 计入**（构造一个 `failed=True` 且有 token 的行，断言它在总量里）
- 无任何 usage 上报时 `prompt_tokens is None`（不是 0）、`usage_coverage` 反映缺口
- `model_calls == 0` 时 `usage_coverage is None`
- `cache_read + cache_miss == 0` 时 `cache_hit_ratio is None`
- 窗口过滤生效（窗口外的行不计入）

**渲染纯函数测**（无需 DB）：
- 两列正常渲染紧凑格式
- lane 不在 `tokens["lanes"]` 中时渲染 `—` 且不抛异常
- `None` 值渲染 `—` 而非 0
- `_fmt_tokens_compact` 的四个分支（None / 三位数 / k / M）

**路由测**：
- `?view=runtime&hours=168` 时**两个数据函数都收到 `within_hours=168`**（方案 B 的核心风险，
  必须钉住）
- 任一数据函数抛异常时返回 200 降级页，异常细节不外泄

## 9. 明确排除项

- token → 金额的成本换算（各 provider 计价不同，独立立项）
- 趋势图与历史对比
- 修改 `recent_token_usage_summary` 或 users 页现有区块的行为
- 统一两页的窗口口径（本次刻意保留差异，仅要求标注清楚）
