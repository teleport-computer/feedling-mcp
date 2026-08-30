---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除退役 Redis backend 客户端

结论：`delete`，已实施。Redis CVM 部署包继续作为审计/恢复资产保留，本候选只删除
不可达的应用侧 Python 客户端和生产依赖。

## 范围与证据

- 审计基线：`f88086e23b6f07d97e44f469a5e89a299d2d9de0`。
- `backend/redis_pool.py` 共 154 行；`redis_configured()` 固定返回 `False`，
  `get_redis()` 固定抛 `redis_deprecated`，保留的 TLS/连接池 builder 不可达。
- 全仓只有 `tests/test_redis_pool.py` import 该模块；业务、lifespan、workflow 和运维脚本
  均不消费它。128 行测试主要验证不可达 builder，而不是现役行为。
- `redis.asyncio` 只在该模块 import；`redis>=5,<7` 因此只为死实现进入 backend
  requirements 和生产镜像。

## 实施边界

- 删除 `backend/redis_pool.py`、其专用测试和 `redis-py` 直接依赖，并重建 hash lock。
- 保留 `deploy/redis/`、Redis compose、verify 脚本、禁用 workflow、CVM id、
  `tests/test_redis_cvm_config.py` 和历史设计文档。
- 增加退役边界门禁：backend 不再携带 Redis 客户端/依赖，同时关键恢复资产必须仍在。
- 文档明确未来恢复必须先有新接入 spec，再重新实现和评审客户端；旧实现不能直接复活。

## 验证与回滚

- 运行退役边界测试、完整 Redis CVM 配置测试、requirements hash 安装校验和相关后端回归。
- 这项变更不修改数据库、wire、运行请求路径或 `tools/chat_resident_consumer.py`。
- 回滚时回退删除提交并重新构建镜像；Redis CVM 数据与部署资产未被改动。
