---
document_lifecycle: current
canonical_owner: self
---
# 候选：旧 provider smoke harness 的去留

结论：`feature-decision`。当前不能删除；先补齐 canonical Hosted Resident E2E，再重新审计。

## 范围与证据

- 删除 `tools/provider_smoke/`（738 行）、`tests/test_providers_manual.py`（84 行）以及
  `.github/pytest-uncovered-baseline.txt` 对应条目，共约 822 行。
- 生产代码消费者为零。包内 `tools/provider_smoke/tests/` 只测试自身；主 pytest ratchet
  只收集顶层 `tests/test_*.py`，CI 和 workflow 均未点名该包。
- `tools/provider_smoke/run_smoke.py` 仍执行
  `identity/init -> model_api/driver -> verify_loop`，但 current hosted 流程是
  `register -> setup -> async send`。`tools/e2e/unlock.py` 还明确警告不要复制旧流程。
- provider/model truth-map 已迁到 `tools/e2e/config.py::HOSTED_CELLS`；旧 matrix 与
  `tests/test_providers_manual.py` 已明显分叉。
- 旧编排器注册账号后不调用已经定义的 `delete_config()`；current `E2EClient` 才有
  fail-site 保留、orphan manifest 和可靠 teardown。

这些事实足以证明工具陈旧，但还不足以证明可删：当前
`FEEDLING_HOSTED_RUNTIME_POLICY=dual` 且默认 desired runtime 是 Resident；旧 smoke
只接受 `feedling_agent_runtime` 响应，并覆盖同账号换 provider 后的 consumer respawn。
`tools/e2e/hosted.py` 当前明确只验证 Runtime V2。因此旧 smoke 仍可能是唯一的 Hosted
Resident provider/continuity/respawn 手工验证面。

## 兼容与取舍

- 不影响生产代码、数据库、wire、部署或 `chat_resident_consumer.py`。
- 不能仅因 onboarding/gate 步骤陈旧就放弃 `--reuse` 的“同账号切 provider + resident
  consumer respawn”意图；Hosted Resident 仍是活跃生产路径。
- `scripts/provider_probe/probe.py` 保留；它验证原始 provider tool-call wire，
  `tools/e2e/` 不能替代该职责。
- 仓库外值班脚本是唯一未能用源码排除的消费者；实施 PR 需在说明中显式提示。

## 重新取得 `delete` 结论的门禁

1. 先给 `tools/e2e/` 增加显式 Resident runtime pin/attribution，并覆盖 provider matrix、
   同账号切 route、runner respawn、两轮 continuity 和可靠 teardown。
2. 在 test 环境至少跑 official provider 与 relay 各一格 Resident P0，并核对 exact
   deployed SHA、runner identity 与切换后的新配置确实生效。
3. 新门禁稳定后，精确搜索 repo 外 runbook/值班入口；再把本候选改回 `delete`，删除
   822 行 gross 表面和 uncovered baseline 条目。
4. 最终 net 必须扣除新增 canonical E2E/contract glue；目前为 TBD，不能把 822 gross
   行数写成净收益。

若未来实施，回滚方式是回退删除提交；不涉及 schema 或数据恢复。
