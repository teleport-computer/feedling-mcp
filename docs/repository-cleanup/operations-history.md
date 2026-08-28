---
document_lifecycle: current
canonical_owner: self
---
# Operations 历史文档审计

本页记录运维与发布保障材料的归档依据。仓库中的 source topology 只能证明配置契约；
某个 test/pre/prod 环境是否正在运行、是否健康以及 exact deployed commit，仍须通过对应
环境的实时证据确认。

## 批次 1：健康探针隔离与 CPU recorder cycle timeout

审计日期：2026-08-27。两份一次性实施计划已落地，且归档前没有生产代码、workflow、
部署配置、current runbook 或其他文档把其路径当作执行入口。配套 design 保留为
`decision` 并校准到当前实现。

| 原文档 | 状态与 current owner | 实现证据 | 当前兼容义务 | 归档位置 |
|---|---|---|---|---|
| 健康探针隔离实施计划 | `historical` / `implemented`；[health-probe isolation decision](../superpowers/specs/2026-08-09-health-probe-isolation-design.md) | `fe97f2a7` 建立专用有界执行器；`056bb74c` 接入 runner probe；`528bd610` 限制 outstanding work；`b20a6d63` 将 `/healthz` 收敛为无 DB 的进程内探针；`1c8f7dd0` 为 `/healthz/runner` 增加独立 DB pool | `/healthz` 不访问数据库且 worker 可响应时保持 HTTP 200，通过进程内 checks 表达 degraded；`/healthz/runner` 独享 2-worker/4-outstanding 执行器和 1–2 连接 DB pool，保持 1s acquire、1s statement、3s route deadline、稳定错误码与 runner identity 不泄露 | [archive plan](../archive/superpowers/plans/2026-08-09-health-probe-isolation.md) |
| CPU recorder 完整 cycle timeout 修复计划 | `historical` / `implemented`；[cycle-timeout decision](../superpowers/specs/2026-08-14-cpu-recorder-cycle-timeout-design.md) | `705926fc` 拆分 Docker 单请求配置与完整顺序采样周期预算，并同步 managed test/prod Compose；`8c0d1179` 将每次实际请求收敛为 `min(client request timeout, cycle remaining)`，同时补强真实 HTTP timeout 与周期耗尽后基线原子性的回归测试 | 单请求上限保持 10s，完整 cycle 保持 30s；采样仍为 sequential、private、read-only、content-free；周期耗尽时不得写部分数据；业务服务不得依赖 recorder/proxy | [archive plan](../archive/superpowers/plans/2026-08-14-cpu-recorder-cycle-timeout.md) |

### Current owners and guards

- 健康探针：`backend/asgi/health.py`、`backend/asgi/runner_health.py`、
  `backend/asgi/health_executor.py`、`backend/db.py`、`backend/gunicorn_conf.py`，以及
  `tests/test_health_executor.py`、`tests/test_health_route_isolation.py`、
  `tests/test_db_health_timeouts.py`、`tests/test_asgi_runner_health.py`。
- CPU recorder timeout：`ops/cpu_recorder.py`、managed test/prod Compose，
  `tests/test_cpu_recorder.py` 与 `tests/test_cpu_recorder_compose.py`。
- 对外健康响应、架构和信任边界继续由 public OpenAPI、public architecture、
  self-hosting 文档与 changelog 持有；archive plan 不是现行运维入口。

本批不修改 backend、ops、公开 API/OpenAPI、`docs-site`、部署配置或
`tools/chat_resident_consumer.py`。

## Deferred scope

- `2026-08-13-phala-cvm-cpu-recorder.md` 暂不归档：计划要求的 24 小时 soak report
  不在当前树中，且 `deploy/DEPLOYMENTS.md` 明确说明 source topology 不能证明某环境已
  实际部署或健康。需补齐现场证据与 current runbook owner 后再分类。
- `2026-08-01-branch-promotion-guard.md` 暂不继续处理：canonical `AGENTS.md` /
  `CONTRIBUTING.md` 写明 `main` 只接受 `test` 或 `pre`，而当前 guard 脚本还允许受限的
  `hotfix/*`。这是发布策略决策，不是普通文档归档；应由维护者先明确预期规则。
