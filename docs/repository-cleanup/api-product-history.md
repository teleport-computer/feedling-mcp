---
document_lifecycle: current
canonical_owner: self
---
# API/Product 历史文档审计

本页记录 API 与产品能力历史材料的归档依据。公开 API 契约以
`tools/public_openapi_contracts.py`、生成的 OpenAPI、public docs、稳定错误表及现行
路由测试为准；运行时内部产品能力由 retained decision、生产代码和聚焦测试共同持有。

## 批次 1：V2 推送与按需照片观察

审计日期：2026-08-27。两份 implementation plan 已完整落地，且归档前没有生产代码、
部署配置、current runbook 或其他文档把 plan 路径当作执行入口。配套 design 继续保留为
`decision`，并补充 current-state reconciliation，避免历史环境快照和旧行号被误当现状。

| 原文档 | 状态与 current owner | 实现证据 | 当前兼容义务 | 归档位置 |
|---|---|---|---|---|
| Runtime V2 推送能力补齐实施计划 | `historical` / `implemented`；[push parity decision](../superpowers/specs/2026-07-25-v2-push-parity-design.md) | `614489d5` 增加 backend 内部 push endpoint；`02e6652c` / `8a40d14a` 接入 wake/chat 回合；`23a1be59` 接通生产 transport；`2dbbacad` 修正 lane 与超时；`e494a38b` 加入全局通知开关 | 回复明文只在单回合内存短暂存在；每回合至多一次 best-effort push；APNs 凭据不进入 worker；失败不能翻转回合结果；chat、manual wake 与其他 wake lane 的回复统一经过全局 system notifications 硬 gate，lane 只保留来源信息，不参与当前 endpoint 的投递分支 | [archive plan](../archive/superpowers/plans/2026-07-25-v2-push-parity.md) |
| Runtime V2 `photo_read` 视觉观察实施计划 | `historical` / `implemented`；[`photo_read` observation decision](../superpowers/specs/2026-07-31-v2-photo-read-vision-observation-design.md) | `a2d72117` 建立按需观察和 blob stripping；`e4210e26` 接入视觉 route；`e283b9ab` 将存量照片读取收紧为 visual-by-default | `photo_added` wake 不自动解密图片；只有模型选择 `photo_read` 才能跨过观察边界；省略 `include_image` 等价于 `true`，显式 `false` 被拒绝，`photo_recent` 是 metadata-only surface；base64、凭据、provider URL 和原始错误不得进入工具 transcript；V2 core 继续只依赖窄 observer callback | [archive plan](../archive/superpowers/plans/2026-07-31-v2-photo-read-vision-observation.md) |

### Current owners and guards

- V2 reply push：`backend/push/push_core.py`、`backend/push/routes_asgi.py`、
  `backend/model_api_runtime/v2/worker.py`、`backend/model_api_runtime/v2/serve_worker.py`，
  以及 `tests/test_v2_push_endpoint.py`、`tests/test_v2_push_delivery.py`、
  `tests/test_v2_atomic_reply_cursor.py`。
- Stored-photo observation：`backend/capabilities/photo.py`、
  `backend/capabilities/tool_schema.py`、`backend/model_api_runtime/v2/executor.py`、
  worker/serve-worker assembly，以及 `tests/test_capabilities_photo.py`、
  `tests/test_v2_dispatch_tool_calls.py`、`tests/test_v2_worker_tool_loop.py`、
  `tests/test_v2_wake_tool_loop.py`。
- 对外信任边界与错误语义继续由 public architecture、Chat workflow、self-hosting 文档和
  `docs/API_ERRORS.md` 持有；archive plan 不能替代这些 owner。

本批不修改 backend、公开 API、OpenAPI、部署拓扑、`docs-site` 或
`tools/chat_resident_consumer.py`。

## Deferred scope

BYOK model catalog 的 backend implementation plan 与跨仓 iOS 产品设计暂不进入本批。
当前仓库能证明 `POST /v1/model_api/models` 后端已经落地，但不能仅凭本仓状态断言 iOS
两处目录 UI 已完成迁移；需在独立批次核对 iOS owner、公开契约及跨仓引用后再分类。
