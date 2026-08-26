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

原测试方案暂不归档。它仍保存一个未转移的有效警告：
`tools/v1_envelope_roundtrip_test.py` 使用旧 salt + zero-nonce 封装，而 current envelope
实现采用不同派生方式；current testing/tool 文档仍推荐该工具。应先在独立批次修复或隔离
工具并更新 current owner，再决定测试方案生命周期。

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
