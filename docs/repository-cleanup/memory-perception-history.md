---
document_lifecycle: current
canonical_owner: self
---
# Memory/Perception 历史文档审计

本页记录 Memory/Perception 历史材料的归档依据。当前 Memory Garden 供应链与信任边界由
public architecture、requirements lock、CI 和聚焦测试共同持有；长期 Garden/IO 分层由
[`MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md`](../MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md)
及 [Garden/IO decision](../superpowers/specs/2026-08-17-garden-io-boundary.md) 持有。

## 批次 1：已实施的 Memory Garden 内核提取

审计日期：2026-08-27。内核提取实施计划在归档前没有仓库引用方，其核心提取范围已归档为
`historical` / `implemented`。它描述的本地包布局已经先由 `5746b24f` 重命名，再由
`4d25dbfb` 的外部依赖形态取代，不能继续作为当前文件图或执行入口。

| 原文档 | current owner | 实现证据 | 当前兼容义务 | 归档位置 |
|---|---|---|---|---|
| Memory Garden 内核提取实施计划 | 提取 decision、public architecture、requirements/CI/tests | `1fa56d68` 确立方向；`2b1585fd` 建包骨架；`5e50e79e` 切换调用方；`3bda1af4` 修正混合语料语言；`70903b4a` / `ec3d6cb1` 收敛 Garden/IO 边界；`5746b24f` 改名；`4d25dbfb` 外置依赖 | V1、V2、Genesis 共用判断力；IO 继续持有加密、存储、ownership、锁、审计、调度与模型 effect；不得恢复遮蔽 wheel 的本地副本。通用 storage adapter 切流和 CLI/MCP 壳未交付，只在 current decision 中保留为需重新立项的 deferred scope | [archive plan](../archive/superpowers/plans/2026-08-14-memory-garden-kernel.md) |

该批次识别出的测试方案阻塞项已由批次 2 处理：旧 envelope 工具不再作为未验证入口留在
current 搜索面，历史测试方案和已完成的批次 1 实施计划均已归档。

## 批次 2：修复 envelope 测试入口并归档提取期验收材料

审计日期：2026-08-27。`tools/v1_envelope_roundtrip_test.py` 与同类
`tools/frame_envelope_roundtrip_test.py` 原先仍使用提取期的 BoxSeal 派生方式：
HKDF salt 绑定 ephemeral/recipient 公钥并使用全零 nonce；当前 backend、enclave 与 iOS
契约则使用 `salt=None` 和 `sha256(ephemeral_pub || recipient_pub)[:12]`。两个脚本现已对齐
当前契约，并把服务访问移入显式 `main()`，因此导入工具模块不会再读文件或连接网络。

| 材料 / 入口 | 处理结果 | current owner / 守卫 | 保留义务 |
|---|---|---|---|
| V1 chat envelope round-trip 工具 | 修复 BoxSeal salt/nonce；保留本地 register→write→history→user/enclave decrypt 工作流 | `backend/content_encryption.py`、`backend/enclave/envelope.py`；`tests/test_v1_envelope_roundtrip_tool.py` 双向互操作与无副作用导入测试 | 工具不是协议事实源；任何算法变化必须先更新权威实现，再让互操作守卫证明工具未漂移 |
| Frame envelope round-trip 工具 | 修复同族 BoxSeal 漂移；将网络工作流隔离到 `main()` | 同上；`tests/test_frame_envelope_roundtrip_tool.py` 验证 enclave 可打开工具产物及无副作用导入 | 保留真实 WebSocket ingest 与 frame persistence 验证；不能用纯函数测试冒充本地服务 E2E |
| Memory Garden 内核提取测试方案 | `historical` / `implemented`；[archive test plan](../archive/superpowers/plans/2026-08-14-memory-garden-test-plan.md) | 当前测试矩阵、CI、聚焦回归和 Memory Garden decision | 历史失败数、分支、一次性上线关卡与旧工具警告仅作实施证据，不得当作当前基线重跑 |
| Memory/Perception 历史文档批次 1 实施计划 | `historical` / `implemented`；[archive cleanup plan](../archive/superpowers/plans/2026-08-27-memory-perception-history-batch-1.md) | repository cleanup 总计划与本审计页 | 保留批次 1 的范围、供应链判断和验证证据；不把旧预期计数或路径当作当前命令 |

current 测试规范和工具索引已同步到两个现役 round-trip 工具，并明确区分“无服务协议漂移守卫”
与“本地服务端到端链路”。本批不修改 backend、公开 API、部署拓扑或
`tools/chat_resident_consumer.py`。

## 当前边界与供应链义务

- managed image 中，`memgarden` 与 `agent-protocol-core` 同时由 requirements 声明，
  lock 中的 versioned Release wheel + SHA-256 和 `--require-hashes` 固定构建输入；冷构建
  依赖 GitHub Release asset 可用性。链上 compose hash 只绑定 manifest/tag/config，未通过
  image digest 直接证明镜像或 wheel 字节。
- 用户 VPS resident 自更新读取 checkout 中的 `backend/requirements.txt`，普通 `pip -r`
  不执行 lock hash gate，安装失败目前也不会阻止后续 re-exec。依赖变更必须保持旧 VPS 的
  checkout/install/re-exec 兼容，不能把 managed image 的保证套到 VPS。
- 仓库内不得出现 `backend/memgarden`、`backend/memory_garden` 或
  `backend/agent_protocol_core`，否则 `PYTHONPATH` 会静默遮蔽安装包。
- CI 的 GitHub artifact-attestation 验证是可见的 best-effort 证据，当前非阻断；hash pin
  才是构建时字节一致性的硬 gate。两者的保证强度不能混写。
- 内核只接收宿主显式传入的字符串/参数；网络、数据库、文件、密钥、加解密、ownership、
  锁、审计、调度和模型调用留在 IO。归档不授权删除现有兼容 re-export 或运行时调用方。
- `tools/chat_resident_consumer.py` 继续保持单文件分发；它使用已安装内核不意味着可以拆分
  consumer 或改变用户 VPS 的更新/import 契约。

## 后续

Perception 的提取计划仍留在当前搜索面。本批不归档它：需要先核对 2026-08-26 freshness
修复、现行 `perception_kernel` owner、V1/V2 共用面和 wake 语义，再由独立批次转移理由。
