# Resident / Self-host Usage 上报到现有 RDS 设计

## 目标与决策

第三阶段让 resident/self-host consumer 把 content-free provider attempt usage 通过现有
Feedling backend API 上报到当前业务 RDS。默认开启，operator 可显式关闭。

本阶段不新增基础设施：

- 不增加 SQLite 或本地 PostgreSQL。
- 不增加 RDS 实例、Redis、Kafka、CVM 或独立 telemetry service。
- 复用现有 ASGI backend、业务 RDS、用户 API key 和 resident consumer 进程。

所有 telemetry 必须 fail-open。API 失败、超时、RDS 失败、内存队列满或后台线程
崩溃都不得影响 provider 调用、回复、heartbeat、重试决策和业务 job 终态。

## 数据流

```text
resident/self-host consumer
  -> bounded in-memory batch queue
  -> POST /v1/usage/provider-attempts
  -> existing ASGI backend
  -> existing business RDS llm_provider_attempts
  -> Admin Usage + user-scoped Usage API
```

Consumer 使用 P0-B 的 logical attempt schema，但不直接连接 RDS。API 验证用户
API key、consumer identity、payload schema、batch 大小和 content-free allowlist，再调用 P0-B
的幂等 upsert。

## 默认开启与 opt-out

- `FEEDLING_USAGE_TELEMETRY_ENABLED` 默认为 `true`。
- 设为 `false` 时不构造上报 payload、不启动上报线程、不发网络请求。
- Consumer 的健康信息和文档明示 enabled/disabled，不把 opt-out 显示为零用量。
- 这是中央 RDS telemetry，不宣称 local-only 或 offline report。

## 身份和幂等

Consumer 复用已有、必填的 `CONSUMER_ID`、agent entry signature 和用户认证，并为每次进程
启动生成 `consumer_boot_id`。每个 attempt 使用确定性 ID：

```text
hash(user_id, consumer_id, parent_message_id, trigger,
     logical_call_ordinal, outer_attempt_ordinal, inner_attempt_ordinal)
```

`consumer_boot_id` 和进程内单调 `attempt_seq` 用于发现同一次启动中的批次缺口，
不进入幂等键。重试同一 batch 不会重复计费。
`CONSUMER_ID` 缺失时禁用 usage 上报并显示配置错误，不生成不稳定的临时
installation identity。这个禁用仅影响 telemetry，consumer 业务功能继续运行。

## Fail-open 交付

- 热路径只尝试 `put_nowait`；不等待 API 或 RDS。
- 队列有界，满时丢 telemetry 并增加本进程 dropped counter。
- 后台线程短超时批量 POST，只在进程存活期内做有界退避重试。
- 不使用本地磁盘 outbox；进程退出或长时间离线可以丢数。
- Complete payload 包含完整 attempt 事实，因此不依赖 start payload 成功。
- 上报成功的 RDS 行由幂等键和 revision 处理重放与 late correction。

由于没有本地持久化，页面必须把未观测区间暴露为 coverage gap，不得宣称
self-host 账本绝对完整。

## API 合同

`POST /v1/usage/provider-attempts` 使用现有用户 API key 和 consumer headers，单批最多
64 条。请求只允许 P0-B schema 中的 content-free 字段；未知字段和以下任一
类别都使整批返回 400：prompt、reply、conversation、memory、reasoning/tool content、raw
provider body、headers、credential、cookie、authorization、endpoint 或 raw error。

相同 attempt/revision 的重复上报返回成功 no-op。较旧 revision 返回成功 no-op；较新
revision 追加 correction 并更新主行。接口不回传其他用户的记录或 fleet 数据。

`GET /v1/usage/report` 是用户范围的 JSON 查询，复用 P0-A/P0-B 指标合同，但强制
`user_id = authenticated user`。它不需要 Admin token。`tools/resident_usage_report.py`
使用现有 API key 获取 JSON，生成临时 content-free HTML 报表供 operator 在浏览器
打开；它不保存 usage 数据，不是新服务或部署单元。

## Admin 与 coverage

中央 Usage 页将 resident/self-host 标记为 `observed self-host`，并分开展示：

- 上报 usage 的 installations/users。
- last upload、最近上报时间和进程内 sequence gap。
- 后端可观测 resident completed turns 与上报 attempts 的 coverage/reconciliation。
- Hosted、observed self-host 和 unobserved/disabled 数量。

Fleet total 只能标注为“Hosted + 默认开启上报且实际可观测的 self-host”。
未上报、被 opt-out 或网络不可达的调用不得记为零。

## 删除、保留与安全

上报 attempt 写入 P0-B 的 RDS 表并按 `user_id` 级联删除。不存在需要远程删除的
本地 usage DB。服务端按 API key、batch 大小、请求体大小和用户做限流；日志只记
safe counts/slug，不打印 payload。

新增的公开用户 API 必须更新 OpenAPI、self-hosting 文档、隐私/信任边界说明和
`Unreleased` changelog。

## 验收

1. 默认开启，显式 opt-out 时不生成任何上报请求。
2. API 超时、RDS 异常、队列满和上报线程异常不改变业务结果或重试次数。
3. 重复 batch、重启后重送和 late correction 不重复计费。
4. 进程崩溃前未上报数据被显示为 coverage gap，不伪装为零。
5. 恶意或带 content 的 payload 整批拒绝且不进日志。
6. 用户只能查询自己的 Usage，无法更改 `user_id` 过滤。
7. 用户删除后中央 RDS 中的 self-host usage 同步删除。
8. 不新增基础设施、数据库实例或部署单元。

## Stacked PR 交付

本 spec 对应第三层 stacked PR。分支 `feat/resident-usage-rds-upload`
从 P0-B HEAD 创建，PR 先以 P0-B 分支为 base；P0-B 合入 `test` 后改为
`test`。
