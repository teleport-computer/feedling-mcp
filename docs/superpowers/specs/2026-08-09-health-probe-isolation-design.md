---
document_lifecycle: decision
canonical_owner: self
---
# 健康探针隔离设计

**状态：已实现；当前决策以本文的现行边界为准。**

日期：2026-08-09；现行状态校准：2026-08-27。

## 现行决策

Feedling 把进程存活与数据库支持的 runner fleet 健康拆成两个不同契约：

1. `GET /healthz` 是无数据库 I/O 的进程内 liveness/readiness probe。只要 worker 能
   响应就返回 HTTP 200；registry 为空或 wake bus 未监听通过 `status=degraded` 和
   `checks.*` 表达，不与业务数据库连接池争抢容量。
2. `GET /healthz/runner` 才查询 runner heartbeat。它使用独立于普通 ASGI work 的
   `health_executor`，以及独立于业务连接池的 health DB pool。
3. runner probe 的边界保持为：每进程 2 个执行线程、最多 4 个 outstanding work，
   DB acquire 1 秒、transaction-local PostgreSQL statement timeout 1 秒、route deadline
   3 秒。超限或饱和映射为稳定的 `runner_health_check_timeout`。
4. runner fleet 的 expected/observed/healthy 聚合语义保持不变；公开响应不返回 runner
   identifier，也不暴露数据库异常细节。其他查询错误使用
   `runner_health_check_error`。
5. 普通业务任务不得提交到 health executor，也不得使用 health DB pool。外部
   health-server 的请求间隔、失败阈值和路由拓扑不由本决策改变。

## 演进说明

最初方案试图让 `/healthz` 和 `/healthz/runner` 都通过专用执行器执行数据库查询，
以解决普通 AnyIO 阻塞线程池饱和时探针排队的问题。实施期间，`b20a6d63` 先把
`/healthz` 收敛为纯进程内探针，因此只有 `/healthz/runner` 继续跨数据库边界。
`1c8f7dd0` 又把 runner heartbeat 查询从普通 application pool 移到独立的 1–2 连接
health pool，避免业务连接池饱和造成假性 runner outage。

这两次收敛保留了原设计的核心目标——健康检查不排在普通业务容量之后——但放弃了
“两个路由都查询数据库”和“runner probe 使用普通 DB pool”的早期细节。旧实施步骤、
历史行号和预实施响应草案只保留在归档计划中，不能覆盖现行代码和公开契约。

## 选择理由

- 单纯增大外部监控超时只会延后故障发现，不能解除探针与业务队列的竞争。
- 独立健康服务或端口能提供更强隔离，但会增加部署、路由和运维复杂度；当前进程内
  executor 加独立小型 DB pool 已覆盖已观测故障模式。
- `/healthz` 不访问外部依赖，可稳定回答“worker 是否仍能服务”；runner 数据库健康由
  `/healthz/runner` 单独表达，避免把业务池压力误报为整个 backend 不存活。

## 当前 owner 与验证

- 进程内 probe：`backend/asgi/health.py`。
- runner route 与稳定错误：`backend/asgi/runner_health.py`。
- 有界执行器：`backend/asgi/health_executor.py`。
- 独立 health pool 与 bounded query：`backend/db.py`、`backend/gunicorn_conf.py`。
- 回归守卫：`tests/test_health_executor.py`、`tests/test_health_route_isolation.py`、
  `tests/test_db_health_timeouts.py`、`tests/test_asgi_runner_health.py`。
- 对外响应与信任边界：public OpenAPI、public architecture、self-hosting 文档和
  changelog；本文不替代这些 current owners。

验证必须同时覆盖：业务线程池/连接池饱和时的隔离、executor admission/deadline、独立
health pool 生命周期、1 秒 acquire/statement timeout、3 秒 route deadline、聚合隐私、
`/healthz` 无数据库 I/O 且保持 HTTP 200。环境上线状态仍以 live endpoint 和 exact
deployed release 为准。

## 回滚边界

本决策不增加 schema migration、持久化状态、密钥、端口或独立容器。回滚通过正常镜像
发布流程完成；不能用把 `/healthz` 重新绑定业务数据库或把 runner query 放回普通 pool
作为“简化”，因为那会重新引入已验证的容量耦合。
