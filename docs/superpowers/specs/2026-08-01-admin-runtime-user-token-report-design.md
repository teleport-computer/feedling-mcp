# Admin Runtime 用户 Token / Model 与交付可靠性报表设计

> **2026-08-02 信息架构更新：**本文的交付可靠性部分继续属于
> Runtime Health；用户 Token/model analytics 已迁移到独立 Usage 页。新的
> 约束与分阶段交付以
> [`2026-08-02-hosted-v2-usage-p0a-design.md`](./2026-08-02-hosted-v2-usage-p0a-design.md)、
> [`2026-08-02-provider-attempt-accounting-p0b-design.md`](./2026-08-02-provider-attempt-accounting-p0b-design.md)
> 和
> [`2026-08-02-resident-usage-rds-upload-design.md`](./2026-08-02-resident-usage-rds-upload-design.md)
> 为准。本文保留为 PR #146 已实现交付可靠性语义的历史记录。

## 目标

在 `/admin/data-track?view=runtime` 的 Runtime 健康页底部增加按 `user_id`
归因的运行报表，让值班人员在同一处回答两组问题：

1. 这个用户通过哪个 provider / model / route 发起了多少次调用、重试并消耗了
   多少 Token；
2. 这个用户的 reply、status、error 和其他 effect 是否可靠送达，是否存在 pending
   或 `needs_reconciliation`。

Token/model 和交付可靠性都是本次验收的硬要求，任何一块缺失都不算完成。Token
部分保留 provider usage 缺报这一观测边界；交付部分不能把“job completed”误当成
“产物已到达用户”。

本功能只提供内部 Admin 可见的、content-free 的 Runtime V2 计量信息。不读取或
展示 prompt、reply、tool 参数、密钥或其他用户内容。

## 展示位置与窗口

报表直接放在 Runtime 健康页的各 lane 健康表之后、失败原因 Top 之前，沿用页面
当前选中的窗口：

- 24 小时
- 7 天（168 小时）
- 30 天（720 小时）

Token/model 统计窗口内所有 Runtime V2 lane，包括 `chat`、`heartbeat`、
`capture`、`dream`、`maintenance` 以及将来新增的 lane。它不限定为 `chat`，
因为后台唤醒和维护任务同样会产生真实模型费用。

交付可靠性包含两种时钟，页面必须标清，不能混为同一个窗口：

- 已完成 effect 的计数跟随当前 24h / 7d / 30d 窗口；
- 当前仍为 `pending` / `needs_reconciliation` 的 effect，以及 terminal failure
  reply/status/error 未投递义务，是当前状态量，不受窗口裁剪。否则一条已经堵了
  31 天的交付会在 30 天窗口中消失，恰好隐藏最严重的问题。

## 归因与排序

归因维度固定为 `user_id`，不按 `principal_id` 合并。原因是
`v2_turn_metrics` 的原始计量和账号删除边界都是 `user_id`；重新注册产生的多个
账号必须保持可区分，避免报表暗中改变数据语义。

每个窗口内至少有一行 `v2_turn_metrics` 的用户都必须出现在报表中，包括所有模型
调用均缺失 usage 的用户。此外，即使用户在窗口内没有 Token metric，只要当前存在
未完成交付义务，也必须显示，避免“报表里没有这个人”被误读成健康。

用户级默认排序规则为：

1. 已知总 Token 降序，未知值排在已知值之后；
2. 模型调用数降序；
3. `user_id` 升序，保证相同数据下结果稳定。

“已知总 Token”只累加 provider 实际上报的输入和输出 Token。缺失 telemetry 不
补零，也不估算。

每个用户内部按 `(provider, model, cache_route_fingerprint)` 分组并按已知总 Token
降序展示。不能只把多个模型名拼成一个列表再给出用户总 Token，因为那样无法判断
费用由哪个模型产生。空 provider/model/route 显示 `unknown`，不丢弃该组。

## 表格字段

报表按用户分组，每个用户包含 Token/model 明细和一份用户级交付可靠性摘要。

Token/model 明细每个 `(provider, model, route)` 一行：

| 字段 | 定义 |
| --- | --- |
| 用户 | `user_id`，链接到现有 `/admin/data-track/users/<user_id>` 详情页，并保留当前 Admin 凭证查询参数；同用户多模型时只在首行显示 |
| Provider / model / route | `v2_turn_metrics` 中的三元组；route 指 `cache_route_fingerprint`，不得展示密钥或 endpoint |
| Lanes | 该模型组出现过的 lane，稳定排序后紧凑展示 |
| Turns | 窗口内该用户、该模型组的 `v2_turn_metrics` 行数 |
| 模型调用 | `model_calls` 总和，包括未返回 usage 的调用 |
| Retries | `retries` 总和，包括 provider adapter 上报的内部重试 |
| Token 入 / 出 | 已上报的 `prompt_tokens` / `completion_tokens` 总和；完全缺报时显示 `— / —` |
| 已知总 Token | 输入与输出均可得时二者相加；完全缺报时显示 `—` |
| Cache R / W / M | 已上报的 `cache_read_tokens` / `cache_write_tokens` / `cache_miss_tokens` 总和；对应字段完全缺报时分别显示 `—` |
| Cache 命中 | `cache_read / (cache_read + cache_miss)`；分母不可得或为零时显示 `—` |
| Usage / cache 覆盖率 | `usage_reported_calls / model_calls` 和 `cache_reported_calls / model_calls`；无模型调用时显示 `—` |

Token 数值使用现有紧凑格式，例如 `12.4k`、`3.1M`。用户链接中的路径和 HTML
均严格转义。

用户级交付可靠性摘要展示：

| 字段 | 定义 |
| --- | --- |
| Reply effects | 窗口内 `reply` / `reply_final_fenced_v1` 的 applied 数，以及当前 pending / needs-reconciliation 数 |
| Status effects | 窗口内 `status` effect 的 applied 数，以及当前 pending / needs-reconciliation 数 |
| All effects | 窗口内所有 effect 的 applied / discarded 数，以及当前 pending / needs-reconciliation 数 |
| Failure reply/status/error | `v2_terminal_failure_outbox` 中三个独立投递义务各自的窗口内 delivered / 当前 undelivered 数 |
| 最老未完成 | 该用户所有 pending、needs-reconciliation、terminal failure 未投递义务中的最老年龄 |
| 可靠性结论 | `正常` / `注意` / `异常`，规则见下节 |

不能把 reply/status/error 合成一个布尔值：三者是独立交付义务，可能部分成功。也不能
只展示 effect outbox；用户可见的终态失败走 `v2_terminal_failure_outbox`，遗漏它会
让“失败回包永远没送到”的用户显示为健康。

### 逐用户交付结论

渲染层的纯函数复用 Runtime 健康页已有的年龄阈值：

- 当前存在 `needs_reconciliation`：`异常`，因为需要人工判定，不能自行恢复；
- 最老未完成交付达到 6 小时：`异常`；
- 最老未完成交付达到 1 小时但不足 6 小时：`注意`；
- 未达到上述条件：`正常`。

瞬时 pending 数量不单独降级，高吞吐下刚入队尚未 apply 是正常状态；只有年龄或
`needs_reconciliation` 参与结论。`discarded` 是 generation fence 的合法终态，展示
但不自动判故障。

## 数据接口

在 `backend/model_api_runtime/v2/jobs_store.py` 新增：

```python
def recent_runtime_user_report(*, within_hours: int = 24) -> dict:
    ...
```

返回结构：

```python
{
    "window_hours": 24,
    "users": [
        {
            "user_id": "usr_...",
            "known_total_tokens": 12900,
            "model_calls": 18,
            "models": [
                {
                    "provider": "anthropic",
                    "model": "claude-example",
                    "route": "route-fingerprint",
                    "lanes": ["chat", "heartbeat"],
                    "turns": 12,
                    "model_calls": 18,
                    "retries": 2,
                    "usage_reported_calls": 17,
                    "cache_reported_calls": 16,
                    "usage_coverage": 17 / 18,
                    "cache_coverage": 16 / 18,
                    "prompt_tokens": 12000,
                    "completion_tokens": 900,
                    "total_tokens": 12900,
                    "cache_read_tokens": 8000,
                    "cache_write_tokens": 500,
                    "cache_miss_tokens": 4000,
                    "cache_hit_ratio": 2 / 3,
                }
            ],
            "delivery": {
                "reply_effects": {
                    "applied_in_window": 10,
                    "pending": 0,
                    "needs_reconciliation": 0,
                },
                "status_effects": {
                    "applied_in_window": 4,
                    "pending": 0,
                    "needs_reconciliation": 0,
                },
                "all_effects": {
                    "applied_in_window": 24,
                    "discarded_in_window": 1,
                    "pending": 0,
                    "needs_reconciliation": 0,
                },
                "terminal_failure": {
                    "reply_delivered_in_window": 2,
                    "reply_undelivered": 0,
                    "status_delivered_in_window": 2,
                    "status_undelivered": 0,
                    "runtime_error_delivered_in_window": 2,
                    "runtime_error_undelivered": 0,
                },
                "oldest_unfinished_age_sec": None,
            },
        }
    ],
}
```

Token/model 查询按 `(user_id, provider, model, cache_route_fingerprint)` 聚合完整时间
窗口，不使用 Top-N 或 LIMIT 截断。这样“每个用户、每个模型”的语义不会退化成未标注
的排行榜。现有 Runtime 健康页已经对相同窗口执行全量 Token 聚合；新增查询沿用同一
`created_at` 时间边界和 366 天安全钳制。

SQL 的 nullable 规则与现有 `recent_token_usage_by_lane()` 保持一致：只有存在对应
telemetry 时才返回 Token 总和；完全缺报保持 `NULL`。`usage_reported_calls` 和
`cache_reported_calls` 都与 `model_calls` 独立汇总，用两种 coverage 分别暴露 Token
usage 和 cache telemetry 的部分缺报。

用户级 `known_total_tokens` 只累加非 `None` 的模型组 `total_tokens`；所有模型组都
未知时保持 `None`。不能用 `0` 作为未知用户的排序值。

交付查询按 `user_id` 聚合 `v2_effect_outbox` 和
`v2_terminal_failure_outbox`：

- 已完成 effect 只统计 `created_at` 落在窗口内的行；reply 与 status effect 另设
  子聚合；
- 当前 `pending` / `needs_reconciliation` 和 terminal failure 未投递义务不加时间
  条件；
- reply effect 类型固定为 `reply` 与 `reply_final_fenced_v1`；
- terminal failure 的 reply/status/runtime-error 三个时间戳分别计数；已投递数只算
  `created_at` 在窗口内的 marker，当前未投递数不加时间条件；
- 用户集合取 Token/model 用户与当前未完成交付用户的并集。

数据接口只返回事实计数和年龄，不返回 `正常` / `注意` / `异常`。交付档位由
`backend/admin/data_track.py` 的纯函数根据同页现有阈值计算，避免把展示策略写进
jobs store。

## Admin 装配与失败隔离

`backend/admin/data_track.py` 声明注入桩 `_runtime_user_report()`，由
`backend/asgi_app.py` 装配为 `jobs_store.recent_runtime_user_report`。

`backend/admin/admin_core.py` 在 Runtime 视图中独立调用该接口。核心健康、按 lane
Token、全局端到端交付、逐用户运行报表属于四个独立失败域：

- 核心健康查询失败时仍使用现有整页降级行为；
- 逐用户报表查询失败时只把 `user_report` 传为 `None`；
- 渲染器显示“用户 Token/model 与交付可靠性暂时取不到”，不能显示为零，也不能
  影响其他区块。

`_render_runtime_health_page()` 增加可选参数 `user_report: dict | None = None`，保持
现有调用方兼容。

## 页面说明与边界

报表旁明确说明：

- 仅统计本实例可见的托管 Runtime V2 回合；
- 不包含 V1 resident 和离线 self-host 模型调用；
- Token 是 provider telemetry 的已知值，不是覆盖率不足时的估算账单；
- provider/model/route 来自每回合落库的 content-free route identity，不代表当前
  配置，也不展示 endpoint 或凭证；
- “交付正常”只证明本系统记录的 delivery obligations 没有超龄或进入人工
  reconciliation，不证明客户端在线或用户已经阅读；
- 同一真人重新注册形成多个 `user_id` 时会显示为多行；
- 点击用户 ID 可进入现有用户详情页继续排查。

本功能不提供导出、不新增筛选器，也不增加客户端 JavaScript 排序。模型维度只按现有
provider/model/route identity 展开；不提供进一步的单回合或内容 drill-down，这些能力
只有在真实运营需求出现后再单独设计。

## 测试与验收

数据库测试覆盖：

- 多用户按 `user_id` 正确分组；
- 同一用户的不同 provider/model/route 分开聚合；
- 所有 lane 都计入对应模型组并稳定列出；
- retries 正确汇总；
- usage 部分缺报时总量和 coverage 正确；
- usage 完全缺报的用户仍存在，Token 字段为 `None`；
- 时间窗口过滤生效；
- 用户和用户内模型排序稳定且无 LIMIT 截断；
- 窗口内 applied/discarded effect 与当前 pending/reconciliation 使用正确的不同
  时间口径；
- reply effect 与其他 effect 分开统计；
- reply、status 与全部 effect 的交付统计可互相对账；
- terminal failure 的 reply/status/runtime-error 三个义务分开统计，窗口内 delivered
  与当前 undelivered 使用正确的不同时间口径；
- 只有当前交付积压、没有窗口 Token 的用户仍出现在报表；
- 逐用户交付档位按 reconciliation 和 1h/6h 年龄阈值判定。

Admin 纯渲染与装配测试覆盖：

- 表格显示用户链接、provider/model/route、retries 和所有 Token 计量列；
- 每个用户显示 reply/effect/terminal-failure 交付摘要和可靠性档位；
- 缺失 Token 不伪装成零；
- `user_report=None` 时只显示该区块不可用；
- Runtime 路由把同一个 `within_hours` 传给用户聚合查询；
- 用户报表查询抛错时健康页仍正常返回；
- 注入桩连接到真实 jobs store 函数；
- 页面包含 V2/self-host/`user_id`、model identity 和交付结论边界说明。

验收时运行相关 PostgreSQL 测试、Admin 渲染测试、静态检查和受影响模块的回归测试。
本变更不修改公开 API、架构信任边界或部署拓扑，因此不需要修改 public OpenAPI 或
`docs-site/content/docs/`。
