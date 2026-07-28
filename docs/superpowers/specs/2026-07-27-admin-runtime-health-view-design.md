# /admin/data-track 新增 Runtime 健康值班台 — 设计

- 日期：2026-07-27
- 状态：设计已确认，待出实施计划
- 相关：PR #94（`feat(runtime-v2): close release-critical lifecycle gaps`，merge commit `3b1b956b`，2026-07-24 merged into `pre`）

## 1. 背景

PR #94 给 Runtime V2 落了三层 telemetry：

1. **聚合层**（content-free）：`v2_turn_metrics` 汇总，渲染在 data-track users 视图的
   「运营 Telemetry」区块（commit `0255beb9`）。
2. **健康层**（content-free、有界）：`jobs_store.recent_chat_operational_health()`，
   经 `admin_core.v2_metrics()` 从 `GET /v1/admin/v2-metrics` 出 JSON。
3. **精确层**（default-off、runner-local、留审计）：exact-job trajectory inspector +
   `v2_trajectory_access_audit` 表。

2026-07-27 对三套环境的实测确认收集正常运行：

| 环境 | v2_turn_metrics 行数 | chat lane 7d jobs | trajectory 捕获 complete/partial/missing | token 上报覆盖率 |
| --- | --- | --- | --- | --- |
| test | 16 | 13 | 13 / 0 / 0 | 18/18 = 100% |
| pre | 131 | 28 | 28 / 0 / 0 | 成功回合 85.3% |
| prod | 66 | 61 | 60 / 1 / 0 | 整体 86.5%，成功回合 41/41 = 100% |

每个终态 job 都落了一行 metrics（prod chat 32 failed + 29 completed = 61，metrics 亦为
61），`missing` 全为 0，没有漏写。token 上报的缺口全部落在 failed 回合——调用本身失败自
然没有 usage 回传，属于设计内的降覆盖率而非伪装成 0。

**问题**：第 2 层数据只能以 JSON 形式访问。PR #94 自己在 "Remaining operational gates"
里留了一条 `wire alerts to turn_health`，至今未做。代价是实打实的：prod 2026-07-27 有
20 个 chat 回合全部失败（`turn_failed:providererror` / `turn_failed:responder_error`），
而 07-26 当天是 21 行零失败——数据全在表里，但没有任何人被叫醒。

本设计给出该 gate 的人工版：一个运行时健康值班台。

## 2. 目标与非目标

**目标**：回答「现在 Runtime V2 跑得好不好、哪里在炸」，一眼可判断要不要管。

**非目标**：

- 不做自动告警/通知（本页是人工值班面，告警是后续独立工作）。
- 不展示任何加密内容。上游原始错误（403 余额不足 / 429 / 超时）留在加密 trajectory 里，
  本页只到失败枚举码这一层，并指路 break-glass inspector。
- 不重复「Proactive 日报」页已覆盖的日报送达率口径（见 §7）。

## 3. 入口与路由

- **URL**：`/admin/data-track?view=runtime`，可带 `&hours=24|168|720`
- **不新增路由**，复用现有 `GET /admin/data-track`（`backend/admin/routes_asgi.py:207`）
- **导航**：`_render_data_track_view_nav()`（`data_track.py:2121`）加第 7 项「Runtime 健康」
- **view 白名单**：`data_track.py:1221` 的集合 `{users, dau, proactive, debug, events}`
  加 `runtime`
- **参数钳制**：`hours` 用枚举白名单（照 `view` 参数的现有写法，非 `read_int` 的范围钳制），
  只接受 24 / 168 / 720 三个值，其余一律回落 24
- **分发**：`admin_core.page_html` 加一条 `view == "runtime"` 分支

鉴权沿用现有 `_require_admin`（admin session cookie 或 `X-Admin-Key`），无新增鉴权面。

## 4. 依赖方向与分层

`CONTRIBUTING.md` §2 的依赖层级中 admin 位于 `model_api_runtime` 之上，但现有
`_runtime_token_usage_summary` 采用的是 asgi_app 装配段注入桩的写法。本设计保持一致：

- `backend/model_api_runtime/v2/jobs_store.py` 新增 `recent_runtime_health()`，
  V2 表结构知识不外泄。
- `backend/admin/data_track.py` 声明桩 `_runtime_health_summary`，
  `backend/asgi_app.py` 装配段注入真实实现（照 `asgi_app.py:144` 的现成模式）。
- admin 层不 import `model_api_runtime`。

渲染函数 `_render_runtime_health_page(payload)` 是纯函数：吃 payload 出 HTML，不碰 DB。
阈值判定与 N/A 逻辑全部在此，单测无需起 PostgreSQL。

## 5. 数据契约

`jobs_store.recent_runtime_health(within_hours: int = 24) -> dict`，内部 3 次查询：

1. `GROUP BY lane` 出各 lane outcome 计数 + 成功回合延迟分位数
2. trajectory 捕获覆盖（沿用 `recent_chat_operational_health` 已有的 `capture_status`
   分类 CASE，加 `GROUP BY lane`）
3. worker 池快照（复用 `inflight_job_count` / `pending_job_count` /
   `live_worker_count` / `live_worker_capacity`）

返回结构：

```python
{
  "window_hours": 24,
  "generated_at": <epoch>,
  "lanes": [                      # 每条 lane 一行，按样本量降序
    {"lane": "chat",
     "sampled_jobs": 61,          # 分母从 agent_jobs 起算
     "completed": 29, "failed": 30, "expired": 2,
     "superseded": 0,             # 单列，不进失败率分母
     "queue_expired": 0, "lease_expired": 0,
     "failure_rate": 0.52,        # (failed+expired) / (completed+failed+expired)
     "p50_ok_ms": 18500,
     "p95_ok_ms": 41000,          # 只取 status='ok' 的回合
     "capture": {"complete": 60, "partial": 1, "missing": 0, "open": 0},
     "top_failures": [{"code": "turn_failed:providererror", "count": 15}]},
  ],
  "pool": {"inflight": 0, "pending": 0, "live_workers": 2,
           "capacity": 8, "oldest_pending_age_sec": None},
}
```

三条口径约束，每条都有事故来由：

- **分母从 `agent_jobs` 起算**，不从 metrics 或 trajectory 起算。否则一次完全漏写会同时
  从分子和分母消失，报出虚假健康的机群。这与 `recent_chat_operational_health` 现有
  docstring 的理由一致。
- **`superseded` 单列，不进失败率分母**。运行时代际切换不是故障，混进去会稀释真实失败率。
- **p95/p50 只算 `status='ok'` 的回合**。实测 prod chat p95 = 296s、max 558s，是被当天
  一批 `providererror` 超时回合拉起来的；若不分离，一个故障会同时点亮「失败率」和「延迟」
  两盏灯，值班时看起来像两个独立故障。参照组：pre 健康态 chat p50 18.5s / p95 38.1s。

`top_failures` 的 code 取 `agent_jobs.last_error`，经三重处理：按**形状**（`scope:kind`
或裸 `snake_case`，只含小写字母/数字/下划线）放行、其余归入 `other` 桶、截断 64 字符、
HTML 转义。

**按形状而非按枚举前缀白名单，是最终 code review 修正的一处缺陷**：最初实现只放行精确
匹配的 `queue_timeout`/`lease_timeout`，以及 `turn_failed:` 前缀（无条件截断，不校验冒号
后内容的形状）。但 `agent_jobs.last_error` 的真实写入点远不止这三种——`mark_failed`/
`mark_expired` 落库的还有 `wake_failed:*`（`worker.py` 的 heartbeat/proactive lane）、
`extraction_failed:*`、`compaction_failed:*`、`mcp_mutation_outcome_unknown`、
`runtime_expired`。旧白名单下这些码在 chat 之外的每条 lane 上都会塌成 `other`——而
heartbeat 正是本页专门加了「（日报口径）」链接、明确要给人看的 lane。同时旧实现的
`turn_failed:` 前缀是无条件放行 + 截断，理论上允许冒号后跟任意自由文本（含空格/中文）
被截断显示，与 metadata-only 的设计意图相悖。修正后的清洗按形状收紧（`^[a-z0-9_]+
(:[a-z0-9_]+)?$`，admin 层自行定义常量，不 import `model_api_runtime`），且清洗后按
`(lane, code)` 重新合并计数——清洗前互不相同的两个原始码若都被清洗成同一个桶（多数情况
是 `other`），渲染前必须合并，否则会显示成两行都叫 `other`，看起来像两个独立故障。

## 6. 阈值与状态判定

阈值定义为模块级常量，集中一处便于调整。

| 指标 | 绿 | 黄 | 红 | 定阈依据 |
| --- | --- | --- | --- | --- |
| 失败率（含 expired） | <5% | 5–15% | ≥15% | prod 07-26 为 0%、07-27 为 100%，两端都能正确点亮 |
| p95（成功回合） | <60s | 60–120s | ≥120s | pre 健康态 38.1s，留约 1.5× 余量 |
| `oldest_pending_age_sec` | <60s | 60–180s | ≥180s | 对应 claim lag 退化；正常应为空 |
| trajectory `missing` | =0 | — | ≥1 | 漏写没有「轻微」档，一条即数据缺口 |
| `live_workers` | ≥1 | — | =0 | 池空即全站不可用 |
| lane `sampled_jobs==0` 且 `capture.open>0` | — | ≥1 个在飞 | — | claimed/running 卡死形态：worker 活着、job 全部不到终态，rate/p95/missing 全部为空，此前全部指标点跳过导致误判「正常」；见下方「卡死不是零样本」 |
| `pool.inflight > pool.capacity` | — | — | 触发即红 | 明确的矛盾态（池账目对不上），不分级 |

页顶总体结论 = 所有 lane 所有指标中最差的一档（红 > 黄 > 绿），文案「异常 / 注意 / 正常」。

**零样本一律显示 `N/A` 灰字，且不参与总体结论。** 这是 commit `2795537a` 那次 re-review
的直接教训：V2-only 合成行的 legacy 分母为 0 被渲染成红色 0%，3 条健康心跳看起来像全挂。
窗口切到 24h 在 test/pre 上大概率就是零样本情形（实测 test 近 7 天仅 13 个 chat job），
页面在此情形下提示「当前窗口无样本，可切 7 天」。

**「零样本」与「卡死」不是同一件事，最终 code review 修正了这处混淆。** 最初实现只用
「所有 lane 的 `sampled_jobs` 都为 0」一个条件判断要不要显示「这不是故障，是这条口径当天
没有数据」；但 worker 全死（`live_workers=0`）与 pending 排队过久（`oldest_pending_age_sec`
超阈）都有独立指标覆盖，唯独漏了 **worker 活着、job 卡在 `claimed`/`running`（回合卡住 /
lease reaper 失效）** 这一形态——这类 job 不在 pending 里、也不影响 `live_workers`，唯一
的痕迹是 `capture.open>0`（或 `pool.inflight>0`）。reviewer 实测过这个具体形状
（`inflight=57 / capacity=8`，全部 job 在飞、无 pending、worker 心跳还活着）：旧实现判定
`("ok", [])` 且显示「这不是故障」——数据在页面上，人被页面明确告知没事。

修正后，「这不是故障」这句话只在真正的空窗口（无终态样本 **且** `capture.open==0` **且**
`pool.inflight==0` **且** `pool.pending==0`）时才显示；否则显示「窗口内无终态 job，但有 N
个回合在飞——可能是卡死」。同时 `_runtime_health_level` 对 `sampled_jobs==0 and
capture.open>0` 至少判 `warn`，`pool.inflight>pool.capacity` 判 `bad`（见上表两行）。

## 7. 页面布局与口径分工

自上而下四块：

1. **总体结论条** — 一句话结论 + 窗口切换按钮（24h / 7天 / 30天，复用现有 `.sort-button`
   样式与 `_data_track_page_href` 拼参数）
2. **Worker 池** — inflight / pending / 存活 worker / 容量 / 最老 pending 年龄，5 个
   metric 格（复用 `.metrics` / `.metric` 样式）
3. **各 lane 健康表** — 一行一 lane：样本数、成功/失败/过期/superseded、失败率（带色）、
   p50/p95、捕获覆盖（complete/partial/missing）
4. **失败原因 Top** — lane × 失败码 × 计数，旁注一行：上游原始错需走 break-glass
   trajectory inspector，本页只有 metadata

**与既有页面的口径分工写在页面上**，避免两页打架：

- 本页 = **运行时视角**（job 生命周期，窗口可切）
- 「Proactive 日报」= **产品视角**（日报送达率，按天）

heartbeat lane 在两页都会出现但口径不同，故本页该行加链接指向日报页。

样式沿用 data-track 现有的 CSS 变量与 class（`.metrics` / `.metric` / `.pill.ok` /
`.pill.warn` / `.sort-button` / `.muted`），不引入新配色。

现有 6 个视图各自内联一份几乎相同的 `<style>`。本次新增的两个页面（健康页 + 降级页）
**共用一个模块级常量** `_RUNTIME_PAGE_CSS`，不再复制第 7、第 8 份；旧 6 页保持原样——
统一它们是独立重构，混进功能改动会把 diff 撑大、风险盖过新功能本身。一条测试钉住"两个
新页共用同一份样式"，防止将来又被复制出第三份。

## 8. 边界与错误处理

- `hours` 非法或缺失 → 回落 24，不报错
- 数据函数抛异常 → 页面渲染为错误卡片（保留 nav、不外泄异常细节、记后端日志），不影响
  其他视图。**注意这是本视图新增的降级行为，不是现状**：现有 `page_html` 没有 try/except，
  `routes_asgi` 只 catch `InvalidDauDay`，所以其他视图的数据函数抛异常时是 500。值班台是
  出事时才被打开的那一页，不能自己也 500，故单独兜住。
- 纯只读，无写路径，**无新增迁移**
- **索引不够用——这是最终 code review 指出的一处错误结论，原文写的是「索引够用」，实查
  并非如此**：
  - `0056_agent_jobs_hb_idx` 存在，但它是 `ON agent_jobs (created_at, user_id) WHERE
    lane='heartbeat'` 的 partial index，**只覆盖 heartbeat 一条 lane**。
  - `agent_jobs` 上其余索引只有 `ux_agent_jobs_singleflight`（partial，仅在飞态）与
    `ix_agent_jobs_claim (status, priority DESC, created_at)`。
  - **`finished_at` 上没有任何索引**——而 `recent_runtime_health()` 新增的 outcome /
    failure 两条 CTE 都是 `WHERE finished_at >= ... ORDER BY finished_at DESC LIMIT
    1000`，capture 那条 CTE 是 `WHERE created_at >= ... ORDER BY id DESC`；三条在全
    lane 范围上都拿不到有效索引，`LIMIT 1000` 只截结果、不减扫描行数。
  - `agent_jobs` **没有任何保留策略**（`grep -rn "DELETE FROM agent_jobs" backend/`
    为空），该表无限增长，这三条 CTE 会随时间推移越来越慢。
  - 而这恰恰是最不该慢的一页：值班台最可能在池被打满、DB 吃紧的时候被打开。
  - **本分支承诺不含迁移，因此不在本设计里加索引。** 已把
    `CREATE INDEX CONCURRENTLY ix_agent_jobs_finished_at ON agent_jobs (finished_at
    DESC) WHERE finished_at IS NOT NULL` 与 `agent_jobs` 保留策略记为明确的
    follow-up（见 §10「明确排除项」），并要求上线后在 prod 规模上跑一次 `EXPLAIN`
    验证实际代价。
- 采样上界沿用现有 `LIMIT 1000` 约定，避免长窗口下全表扫（但如上所述，对 `agent_jobs`
  当前索引形状而言只是限制返回行数，不改变全表扫描的量级）

## 9. 测试

对应 `docs/testing/TESTING.md` §2 决策矩阵的「backend 逻辑」+「路由」两类。

**渲染纯函数单测**（无需 PG）：

- 阈值三档各一例（绿/黄/红），总体结论取最差档
- 零样本显 N/A、不显红 0%、不参与总体结论
- `superseded` 不进失败率分母
- 失败码含自由文本时被归入 `other` 桶、截断并转义

**数据函数 DB 测**：

- 多 lane 混合数据下的聚合正确性
- p95/p50 只算成功回合（构造失败超时回合验证其不影响分位数）
- `missing` 计数从 `agent_jobs` 起算（构造一个有 job 无 trajectory 流的终态回合）

**路由测**：

- `?view=runtime` 返回 200 且 nav 高亮该项
- 非法 `hours` 回落 24
- 未授权返回 401

## 10. 明确排除项

- 自动告警/通知接线（PR #94 的 gate 原文是 wire alerts，本设计只做人工值班面）
- 精确 trajectory 下钻入口。break-glass inspector 保持「需特意去用」，不降级为值班顺手点。
  实测三环境 `v2_trajectory_access_audit` 均为 0 行，符合默认关闭设计，但也意味着该审计
  路径至今未经真实调用验证——这是独立的待办，不在本设计范围内。
- 在 `v2_turn_metrics` 增列存脱敏后的 provider 错误类型（需要迁移 + 改写入路径 + 先证明
  分类结果不含内容，独立立项）
- **`agent_jobs.finished_at` 索引 + 保留策略**（最终 code review 新增的 follow-up，见 §8）：
  `CREATE INDEX CONCURRENTLY ix_agent_jobs_finished_at ON agent_jobs (finished_at DESC)
  WHERE finished_at IS NOT NULL`，以及该表的行保留/归档策略；上线前需要一次迁移，故不在
  本次不含迁移的分支范围内。落地前应先在 prod 规模的 `agent_jobs` 上跑一次 `EXPLAIN`，
  确认 `recent_runtime_health()` 三条 CTE 的真实扫描代价，而不是凭索引形状猜测。
