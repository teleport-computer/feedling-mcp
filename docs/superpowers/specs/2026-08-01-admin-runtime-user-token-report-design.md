# Admin Runtime 用户 Token 用量报表设计

## 目标

在 `/admin/data-track?view=runtime` 的 Runtime 健康页底部增加按 `user_id`
聚合的 Token 用量报表，让值班人员能在同一窗口内定位主要消耗账号，同时保留
provider usage 缺报这一观测边界。

本功能只提供内部 Admin 可见的、content-free 的 Runtime V2 计量信息。不读取或
展示 prompt、reply、tool 参数、密钥或其他用户内容。

## 展示位置与窗口

报表直接放在 Runtime 健康页的各 lane 健康表之后、失败原因 Top 之前，沿用页面
当前选中的窗口：

- 24 小时
- 7 天（168 小时）
- 30 天（720 小时）

报表统计窗口内所有 Runtime V2 lane，包括 `chat`、`heartbeat`、`capture`、
`dream`、`maintenance` 以及将来新增的 lane。它不限定为 `chat`，因为后台唤醒和
维护任务同样会产生真实模型费用。

## 归因与排序

归因维度固定为 `user_id`，不按 `principal_id` 合并。原因是
`v2_turn_metrics` 的原始计量和账号删除边界都是 `user_id`；重新注册产生的多个
账号必须保持可区分，避免报表暗中改变数据语义。

每个窗口内至少有一行 `v2_turn_metrics` 的用户都必须出现在报表中，包括所有模型
调用均缺失 usage 的用户。默认排序规则为：

1. 已知总 Token 降序；
2. 模型调用数降序；
3. `user_id` 升序，保证相同数据下结果稳定。

“已知总 Token”只累加 provider 实际上报的输入和输出 Token。缺失 telemetry 不
补零，也不估算。

## 表格字段

每行展示：

| 字段 | 定义 |
| --- | --- |
| 用户 | `user_id`，链接到现有 `/admin/data-track/users/<user_id>` 详情页，并保留当前 Admin 凭证查询参数 |
| Turns | 窗口内该用户的 `v2_turn_metrics` 行数 |
| 模型调用 | `model_calls` 总和，包括未返回 usage 的调用 |
| Token 入 / 出 | 已上报的 `prompt_tokens` / `completion_tokens` 总和；完全缺报时显示 `— / —` |
| 已知总 Token | 输入与输出均可得时二者相加；完全缺报时显示 `—` |
| Cache read | 已上报的 `cache_read_tokens` 总和；完全无 cache telemetry 时显示 `—` |
| Cache 命中 | `cache_read / (cache_read + cache_miss)`；分母不可得或为零时显示 `—` |
| Usage 覆盖率 | `usage_reported_calls / model_calls`；无模型调用时显示 `—` |

Token 数值使用现有紧凑格式，例如 `12.4k`、`3.1M`。用户链接中的路径和 HTML
均严格转义。

## 数据接口

在 `backend/model_api_runtime/v2/jobs_store.py` 新增：

```python
def recent_token_usage_by_user(*, within_hours: int = 24) -> dict:
    ...
```

返回结构：

```python
{
    "window_hours": 24,
    "users": [
        {
            "user_id": "usr_...",
            "turns": 12,
            "model_calls": 18,
            "usage_reported_calls": 17,
            "usage_coverage": 17 / 18,
            "prompt_tokens": 12000,
            "completion_tokens": 900,
            "total_tokens": 12900,
            "cache_read_tokens": 8000,
            "cache_miss_tokens": 4000,
            "cache_hit_ratio": 2 / 3,
        }
    ],
}
```

查询直接按 `user_id` 聚合完整时间窗口，不使用 Top-N 或 LIMIT 截断。这样“每个用户”
的语义不会退化成未标注的排行榜。现有 Runtime 健康页已经对相同窗口执行全量 Token
聚合；新增查询沿用同一 `created_at` 时间边界和 366 天安全钳制。

SQL 的 nullable 规则与现有 `recent_token_usage_by_lane()` 保持一致：只有存在对应
telemetry 时才返回 Token 总和；完全缺报保持 `NULL`。`usage_reported_calls` 和
`model_calls` 独立汇总，用 coverage 暴露部分缺报。

## Admin 装配与失败隔离

`backend/admin/data_track.py` 声明注入桩 `_runtime_token_by_user()`，由
`backend/asgi_app.py` 装配为 `jobs_store.recent_token_usage_by_user`。

`backend/admin/admin_core.py` 在 Runtime 视图中独立调用该接口。用户报表与现有健康、
按 lane Token、端到端交付属于四个独立失败域：

- 核心健康查询失败时仍使用现有整页降级行为；
- 用户 Token 查询失败时只把 `user_tokens` 传为 `None`；
- 渲染器显示“用户 Token 用量暂时取不到”，不能显示为零，也不能影响其他区块。

`_render_runtime_health_page()` 增加可选参数 `user_tokens: dict | None = None`，保持
现有调用方兼容。

## 页面说明与边界

报表旁明确说明：

- 仅统计本实例可见的托管 Runtime V2 回合；
- 不包含 V1 resident 和离线 self-host 模型调用；
- Token 是 provider telemetry 的已知值，不是覆盖率不足时的估算账单；
- 同一真人重新注册形成多个 `user_id` 时会显示为多行；
- 点击用户 ID 可进入现有用户详情页继续排查。

本功能不提供导出、不新增筛选器、不按 provider/model/lane 展开，也不增加客户端
JavaScript 排序。这些能力只有在真实运营需求出现后再单独设计。

## 测试与验收

数据库测试覆盖：

- 多用户按 `user_id` 正确分组；
- 所有 lane 都计入同一用户总量；
- usage 部分缺报时总量和 coverage 正确；
- usage 完全缺报的用户仍存在，Token 字段为 `None`；
- 时间窗口过滤生效；
- 排序稳定且无 LIMIT 截断。

Admin 纯渲染与装配测试覆盖：

- 表格显示用户链接和所有计量列；
- 缺失 Token 不伪装成零；
- `user_tokens=None` 时只显示该区块不可用；
- Runtime 路由把同一个 `within_hours` 传给用户聚合查询；
- 用户查询抛错时健康页仍正常返回；
- 注入桩连接到真实 jobs store 函数；
- 页面包含 V2/self-host/`user_id` 归因边界说明。

验收时运行相关 PostgreSQL 测试、Admin 渲染测试、静态检查和受影响模块的回归测试。
本变更不修改公开 API、架构信任边界或部署拓扑，因此不需要修改 public OpenAPI 或
`docs-site/content/docs/`。
