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
      "cache_hit_ratio": 0.493,    # read / (read + miss)；read/miss 任一为 None，
                                   # 或两者皆非 None 但分母为 0 → None（与 users
                                   # 页既有算法对齐，见 §6）
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
静默少报总量（"最新 1000 条的 token 和"不是任何人想要的数字）——这个决策本身仍然正确。
但扫描量**不是**由 `ix_v2_turn_metrics_lane_created_at` 控制：该索引是 `(lane, created_at
DESC)`，`lane` 是前缀这件事恰恰意味着它**服务不了**本查询——本查询没有 `WHERE lane = ...`
等值谓词，只有 `created_at >= ...` + `GROUP BY lane`，PG 16 没有 B-tree skip scan（PG 18
才有），前导列无等值谓词时用不上索引。本地 PG 16（50 万行、5 个 lane）实测：本查询走
Parallel Seq Scan（`Rows Removed by Filter` 随窗口内行数线性增长）；有 `lane = 'chat'` 等
值谓词的既有 `recent_token_usage_summary` 才真正吃到该索引的 Bitmap Index Scan。
`v2_turn_metrics` 是 append-only 表，本查询的扫描量因此随表增长单调变大——增长到不可接受
时需要补一条单列索引 `CREATE INDEX CONCURRENTLY ix_v2_turn_metrics_created_at ON
v2_turn_metrics (created_at DESC)`（见 §9 明确排除项，本次不加）。

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
读 `request.args`，因此**调用方**不会造成窗口不一致。但这句话只覆盖了调用方，没覆盖**被
调方各自的钳制上界**：`recent_runtime_health` 把 `within_hours` 钳到 `24 * 30`（720），
`recent_token_usage_by_lane` 钳到 `24 * 366`。今天 `max(_RUNTIME_HEALTH_WINDOWS) == 720
== 24 * 30`，两边钳制结果恰好相同——**这是巧合，不是不变量**。任何人往
`_RUNTIME_HEALTH_WINDOWS` 加一个超过 720 小时的档位（比如 90 天 = 2160），健康列会被静默
钳到 720、token 列却查满新值，同一行两个窗口，页顶的「窗口 N 小时」还只反映健康侧（渲染只
读 `payload["window_hours"]`，从不看 `tokens["window_hours"]`）。
`tests/test_data_track_runtime_view.py::test_runtime_health_windows_stay_within_jobs_store_health_clamp`
是把这个巧合钉成显式约束的守卫：`assert max(_RUNTIME_HEALTH_WINDOWS) <= 24 * 30`——白名单
一旦越过这条线就会红，而不是像今天这样"没有任何测试会红"。

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

数值格式化复用既有 `_fmt_count`（千分位）与 `_fmt_ratio`（百分比，`None → —`）。大数用 k/M/B
缩写以控制列宽——新增一个 `_fmt_tokens_compact(value)` 纯函数：`None → "—"`、`< 1000 → "951"`、
`< 999_950 → "951.2k"`、`< 999_950_000 → "1.2M"`、否则 `"1.2B"`。真实进位边界是 999_950
（`.1f` 在 999.95 处四舍五入），不是天真猜测的 999_500——先除后 `.1f` 在
`[999_950, 10**6)` / `[999_950_000, 10**9)` 这两个区间会错误显示成上一档的 `"1000.0k"` /
`"1000.0M"`，按格式化后的结果收紧两处阈值即可修正。

某条 lane 在 `tokens["lanes"]` 中不存在时（该 lane 有 job 但无任何 turn metric 行），两列均
渲染 `—`——不得因缺键抛 `KeyError`，也不得渲染成 0。

**反过来，某条 lane 只在 `tokens["lanes"]` 里存在、不在 `payload["lanes"]` 里时，也必须
出现在渲染结果里**——这是与上一条同样重要但容易漏掉的方向。`recent_runtime_health` 的每条
子查询共享 `LIMIT 1000` 的采样上界（全 lane 共享这一个配额），而 `recent_token_usage_by_lane`
是窗口内全量聚合、无 LIMIT；只遍历 `payload["lanes"]` 会让"窗口内有 token 开销、但 job 没
挤进最近 1000 条"的 lane 不显示也不报错——这正是本功能要消灭的盲区（`jobs_store` 里
`recent_runtime_health` 自己的 `all_lanes` 就是取并集的哲学：防止 worker 卡死时该 lane 从
健康视图消失；渲染层要跟上同样的哲学）。做法：渲染层遍历的 lane 集合改为 `payload["lanes"]`
的 lane ∪ `tokens["lanes"]` 的键；token-only 的合成行不带任何健康数据（不是 0），健康各列
走既有的"无数据"渲染逻辑显 `—`；这些合成行不参与 `_runtime_health_level` 的判定（那是纯
job 结局层面的判断，token-only 的 lane 没有 job 结局信息可供判定）。

页顶说明区补三句：

- token 含**失败回合**（失败也烧钱），与上方失败率不是同一批样本的筛选口径
- 健康列还各自共享**最近 1000 个 job 的采样上界**，token 列是窗口内全量、无采样上界——
  长窗口下两者覆盖的时间跨度可能不同（这条比上面的筛选口径差异数量级更大，必须单独点出）
- `prompt_tokens` 已包含 cache read/write，**不要**与缓存列相加，否则重复计数

## 6. 与 users 页的口径差异

两处并存且窗口口径不同（users 固定 30 天、本页跟随窗口），因此**两处标题都必须写明窗口**。
users 页现有写法已是「近 30 天」，本页的列头与说明须同样标注当前窗口，避免同一指标在两页
显示不同数字时被当成 bug。

窗口切到 30 天时两页 chat lane 的数字应当一致——这也是一条可人工核对的自洽性检查。**这条
自洽性检查要求两页对同一批底层数据的算法完全一致**，不只是窗口。`cache_hit_ratio` 因此
必须与 users 页现有算法对齐：`cache_read` / `cache_miss` **任一为 `None`** 时 ratio 为
`None`（显 `—`，意为"不知道"），只有两者都非 `None` 时才计算 `read / (read + miss)`。
之前的实现用 `(cache_read or 0) + (cache_miss or 0)` 当分母，会把"只有一侧上报"误算成一个
具体数字——`cache_read=None, cache_miss=500` 显 `0.0%`（看起来像"缓存完全没生效"）、反向
`cache_read=500, cache_miss=None` 显 **`100.0%`**（看起来像"缓存完美命中"，而真相是 miss
根本没上报）；reviewer 核过 `provider_client.py:721-780`，Anthropic 只有 cache write、无
cache read 的回合确实会产出这种组合，是真实路径。**这个对齐比本节开头说的"两处窗口不同"
更根本**：页顶那句"两处数字不一致是窗口不同、不是 bug"的前提是两页对同一批数据算法一致
——算法本身不一致时，这句话会反过来把真实的算法差异误导成"窗口问题"而被值班放过。

## 7. 边界与错误处理

- 纯只读，无写路径，**无新增迁移**。
- 某 lane 有 job 无 metric 行 → 该 lane 的 token 列显示 `—`（见 §5）。
- 某 lane 有 token 开销但没被健康侧的 `LIMIT 1000` 采样看见 → 渲染层与
  `tokens["lanes"]` 取并集补上这条 lane，健康各列显 `—`（见 §5）。
- 数据函数抛异常 → 降级页（沿用既有行为，覆盖两次调用）。
- `window_hours` 的白名单与回落逻辑不变，仍由 `_runtime_health_window_hours()` 统一处理；
  其上界受 `recent_runtime_health` 的 `24 * 30` 钳制约束（见 §4），由测试守卫钉住。

## 8. 测试

**数据层 DB 测**（需 PostgreSQL）：
- 按 lane 正确分组，多 lane 混合数据下互不串扰
- **失败回合的 token 计入**（构造一个 `failed=True` 且有 token 的行，断言它在总量里）
- 无任何 usage 上报时 `prompt_tokens is None`（不是 0）、`usage_coverage` 反映缺口
- `model_calls == 0` 时 `usage_coverage is None`
- `cache_read` 与 `cache_miss` 都为 `None`，或两者都非 `None` 但和为 0 时，
  `cache_hit_ratio is None`
- `cache_read=None, cache_miss=500`（只上报了 miss）→ `cache_hit_ratio is None`，不是 `0.0`
- `cache_read=500, cache_miss=None`（只上报了 read）→ `cache_hit_ratio is None`，不是 `1.0`
  （这两条是 review 补的：真实的部分上报路径，不是理论构造）
- 窗口过滤生效（窗口外的行不计入）

**渲染纯函数测**（无需 DB）：
- 两列正常渲染紧凑格式
- lane 不在 `tokens["lanes"]` 中时渲染 `—` 且不抛异常
- lane 只在 `tokens["lanes"]` 中、不在 `payload["lanes"]` 中时**必须出现在渲染结果里**，
  token 列显真实数字、健康列显 `—`；且不参与总体健康结论的判定
- `None` 值渲染 `—` 而非 0
- `_fmt_tokens_compact` 的五个分支（None / 三位数 / k / M / B）+ 真实进位边界 999_950 /
  999_950_000（不是天真猜测的 999_500 / 999_500_000）
- 白名单守卫：`max(_RUNTIME_HEALTH_WINDOWS) <= 24 * 30`（§4 的不变量钉住，而不是巧合）

**路由测**：
- `?view=runtime&hours=168` 时**两个数据函数都收到 `within_hours=168`**（方案 B 的核心风险，
  必须钉住）
- 任一数据函数抛异常时返回 200 降级页，异常细节不外泄

## 9. 明确排除项

- token → 金额的成本换算（各 provider 计价不同，独立立项）
- 趋势图与历史对比
- 修改 `recent_token_usage_summary` 或 users 页现有区块的行为
- 统一两页的窗口口径（本次刻意保留差异，仅要求标注清楚）

## Follow-up（本次不做，明确记录）

- **`v2_turn_metrics` 补一条 `(created_at DESC)` 单列索引**：
  `CREATE INDEX CONCURRENTLY ix_v2_turn_metrics_created_at ON v2_turn_metrics
  (created_at DESC)`。§3 的 `recent_token_usage_by_lane` 目前走 Seq Scan（无 lane 等值
  谓词，`ix_v2_turn_metrics_lane_created_at` 的 `lane` 前缀用不上），随表增长扫描量单调
  变大；表增长到不可接受时补这条索引，需走 alembic 迁移（本分支承诺无 schema 变更，故本次
  不加）。
