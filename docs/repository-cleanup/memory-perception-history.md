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

## 批次 3：已实施的 Perception 内核提取

审计日期：2026-08-27。Step 1 实施计划的主干提取已落地，且归档前没有生产代码、
部署配置、current runbook 或其他文档把该计划路径当作执行入口。设计文档保留为
`decision`，架构与 prompt 资产表扶正为 current owner，并校准掉“尚待提取”、旧 IO
owner 和过期行号所代表的当前事实。

| 材料 | 状态与 current owner | 实现 / 修复证据 | 当前兼容义务 | 处理结果 |
|---|---|---|---|---|
| Perception Step 1 实施计划 | `historical` / `superseded`（主干已实施，剩余直连不可重放）；[architecture](../PERCEPTION_ARCHITECTURE.zh.md)、[boundary decision](../PERCEPTION_EXTRACTION_DESIGN.zh.md)、[prompt assets](../PERCEPTION_PROMPT_ASSETS.zh.md) | `ac7afd62` / `b043ae8d` 建立 prompt golden；`85f0046f` 建内核与纯度守卫；`1e3c6677`、`bcf3612d`、`42e93471`、`94fd56af`、`6959a3aa`、`1ad68346` 迁移 catalog、projection/glance、history、V2/V1 prompt 与已采用的 wake 判据；`27625742`、`73d99e7d`、`d97ece15` 完成命名和评审修正 | `perception_kernel` 保持纯函数、零 IO；V1/V2 共用判断 owner，但各自保留 role、投递、安全和工具预算协议；`perception/` 的数据库、加解密、鉴权、事务、metrics、入队及兼容 re-export 不得因归档而删除；`PERCEPTION_WAKE_SOURCES`、`is_significant_change`、`should_wake` 仍由未接线守卫保护，reason 映射与信号语义未决前不得直接接入 IO | [archive plan](../archive/superpowers/plans/2026-08-19-perception-extraction-step1.md) |
| Perception 插件设计 | `decision`；current 文件图由 [architecture](../PERCEPTION_ARCHITECTURE.zh.md) 持有 | 提取实现证明 Step 1 边界可行；`c7cdae93` 又把 stale digest trend 收敛为 `last_known`，避免旧值伪装成当前值 | 保留 `wake ≠ 该开口了`、精确定位原始值/IO 边界、可信说明书与不可信事实分层；不得把定位 resolver 的丢弃保证套到 app Shortcut——后者会持久化 app/bundle alias；正文中的未来插件 API、CLI/MCP、新仓库和开源步骤不是当前能力 | retain: [decision](../PERCEPTION_EXTRACTION_DESIGN.zh.md) |
| Perception 架构与 prompt 资产 | `current` / self | 生产模块、兼容壳、purity/catalog/projection/wake/history/prompt golden 测试 | current owner 使用稳定模块/符号，不把提取期行号当契约；V1/V2 文案差异可保留，但唯一出处和逐字节 golden 不得漂移 | retain: [architecture](../PERCEPTION_ARCHITECTURE.zh.md)、[prompt assets](../PERCEPTION_PROMPT_ASSETS.zh.md) |
| Memory/Perception 历史文档批次 3 实施计划 | `historical` / `implemented`；本审计页 | repository cleanup 总计划与本审计页 | 仅保留本批范围和验证路径，不作为新的 runtime 修改入口 | [archive cleanup plan](../archive/superpowers/plans/2026-08-27-memory-perception-history-batch-3.md) |

本批只变更内部 Markdown 生命周期、current-state 校准和生成的 inventory；不修改 backend、
公开 API/OpenAPI、数据库、部署拓扑或 `tools/chat_resident_consumer.py`。归档不授权拆分
consumer，也不改变用户 VPS 的 checkout/install/re-exec 或 import 契约。

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

Perception Step 1 已退出 current 搜索面。设计里的 CLI/MCP 壳、新仓库和开源发布仍未
实施，也不自动构成 backlog；只有出现明确产品需求、owner、供应链方案和独立验收计划后
才重新立项。任何 Perception 运行时清理仍须分别验证 V1/V2、兼容 re-export、wake 语义、
freshness/权限遮蔽以及 IO 的事务与加密边界。
