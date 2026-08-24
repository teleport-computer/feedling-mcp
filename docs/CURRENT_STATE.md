# Feedling 当前状态

> 本文只描述当前仓库中的运行时与部署接线。它是 agent 和工程师的 current-state 入口，但源码配置不等于 live 环境已经部署；线上结论还必须核对目标环境返回的 exact `release.git_commit`。

更新日期：2026-08-24。

## 事实优先级

遇到冲突时按以下顺序判断：

1. test/pre/prod 健康信息、进程/trace 证据和 exact deployed commit；
2. 该 commit 中的 compose、workflow 和环境接线；
3. 生产代码、数据库、持久化格式和 wire/API 契约；
4. 契约测试与部署测试；
5. 本文以及明确标为 current 的架构、运维和测试文档；
6. decision；
7. changelog、历史 spec/plan、incident 和 git 历史。

历史材料可以解释原因，不能覆盖更高层对“现在跑什么”的描述。

## 当前运行时选择

生产、test 和 pre 的主 compose 都配置：

- `FEEDLING_HOSTED_RUNTIME_POLICY=dual`；
- `FEEDLING_RUNTIME_DEFAULT_DESIRED=resident`。

因此 hosted Model API 用户不是统一走 V2。每个用户的持久化 runtime control 决定执行路径；没有 desired-runtime allowlist row 时，reconciler 的缺省 desired 为 Resident。用户切换期间或 control tuple 不一致时请求 fail closed，而不是静默换到另一条 runtime。

当前有两个 hosted 执行引擎，以及一个用户自建部署形态：

| 形态 | 当前 owner | 入口与部署 |
|---|---|---|
| Pooled Runtime V2 | `backend/model_api_runtime/v2/` | 主 CVM 的 `serve-worker` 从 PostgreSQL durable queue 领取任务 |
| active hosted Resident | `backend/agent_runtime/` + `tools/chat_resident_consumer.py` | 独立 runner CVM 的 `agent-runner` 执行 `backend/agent_runtime/supervisor.py`，为用户托管同一份 resident consumer |
| Self-hosted Resident / VPS | `tools/chat_resident_consumer.py` + `tools/io_cli.py` | 用户机器由 systemd/手工命令直接运行 consumer，轮询 `/v1/chat/*` |

`backend/hosted/chat_send_core.py` 在 `dual` policy 下按严格的 `(mode, state)` tuple 分流；`backend/hosted/runtime_reconciler.py` 让实际状态向 allowlist/default desired 收敛。`v2_only` 仍是代码支持的强制策略和历史退役阶段行为，但不是当前三套主 compose 的配置。

## 当前部署拓扑

- 主 CVM：ingress、ASGI backend、enclave、pooled `serve-worker` 以及观测辅助服务，具体以对应环境的 `deploy/docker-compose.phala*.yaml` 为准。
- 独立 V1 runner CVM：`deploy/docker-compose.phala.runner.yaml`、`deploy/docker-compose.phala.pre.runner.yaml`、`deploy/docker-compose.phala.prod.runner.yaml` 中的 `agent-runner`。
- 用户 VPS：固定执行 `python tools/chat_resident_consumer.py`；这个单文件分发边界不拆分。

仓库 compose 只证明“准备部署什么”。确认“实际运行什么”时，先读环境 `/healthz` 的 `release.git_commit`，再对照该 SHA 的 compose/代码并执行受影响 lane 的真实探针。

## 数据与信任边界

- PostgreSQL 是持久化权威源；不要把进程内缓存、runner checkpoint 或历史 migration 当作可独立删除的数据源。
- encrypted envelope 的内容私钥由 enclave 持有。Resident 通过 `FEEDLING_ENCLAVE_URL` 使用受认证的 decrypt/history 路径；独立 runner CVM 不持有主 enclave 内容私钥。
- Runtime V2 worker 在 TDX 信任边界内组装本轮上下文，并只把用户授权的 prompt 发给其选择的 provider。
- 各环境的 plaintext-write/TEE 迁移状态可能不同，必须读取 exact deployed commit 和环境 gate；不要把某一环境的内容 shape 外推到全部环境。
- Alembic RDS/TEE 历史不可作为普通清理目标；字段和 reader 的移除需要持久化、回滚和兼容证据。

## 排查入口

- 运行时名词与两侧符号映射：`docs/testing/RUNTIME_MAP.md`。
- 测试选择入口：`docs/testing/README.md`；完成标准与 L1/L2/L3 矩阵：`docs/testing/TESTING.md`。
- 部署流程和环境记录：`deploy/DEPLOYMENTS.md`。其中已明确标为 superseded/historical 的段落不是当前指令。
- 当前源接线：生产 `deploy/docker-compose.phala.yaml`，以及对应 test/pre 和独立 runner compose。
- 当前 hosted 路由：`backend/hosted/chat_send_core.py`、`backend/hosted/config_store.py`、`backend/hosted/runtime_reconciler.py`。

若这些入口再次互相矛盾，应先修复事实入口和一致性测试，再进行“旧代码”删除。
