---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除失效的 `tests/e2e_model_api_test.py`

结论：`delete`。建议与 provider smoke harness 同一批验证、独立提交。

## 范围与证据

- gross 删除 253 行的 `tests/e2e_model_api_test.py`，并清理 `CONTRIBUTING.md`、
  `docs/testing/TESTING.md`、`backend/enclave_app.py` 和 `tests/test_api.py` 的 current
  特例/引用；历史文档保持 historical。
- 无生产、CI 或 pytest 消费者；`CONTRIBUTING.md` 反而要求所有 pytest 命令显式
  `--ignore` 它。
- 脚本仍要求 `/v1/model_api/chat/send` 返回同步 `200 + plaintext reply`。当前生产
  契约固定为 `202`，reply 已落地时也只返回密文消息引用，不返回明文。
- 它会把正确的 202 判为 FAIL，却在汇总后无条件 `return 0`，因此同时 false-red 与
  false-green。
- 用法写成不存在的 `python3 tools/e2e_model_api_test.py`，且每个 provider 注册账号后
  没有账号 teardown。
- current 替代面为 `tools/e2e/p0.py` + `tools/e2e/hosted.py`（真实 provider/runtime）、
  `tools/e2e_encryption_test.py`（本地 backend+enclave 加密链）和
  `tests/test_model_api_chat_send_routing.py`（202 路由契约）。

253 行是 gross，不包含 current 引用删除，也未扣除新增 contract glue；最终 net 由实施
PR 的 `git diff --stat` 复算。

## 兼容与取舍

- 不影响生产、schema、wire 或部署；删除的是已经无法正确判定 current 契约的脚本。
- 名义上会失去“本地 simulator + 真 provider”组合入口。若团队仍需要这组能力，应作为
  新功能并入 canonical E2E，而不是修补这个无 teardown 的旧入口。

## 红绿步骤与验证

1. 红：给 current 测试文档/仓库契约增加断言，禁止再把该文件列作 L2 入口；固定
   canonical L2/L3 命令。
2. 绿：删除脚本和 current 引用；标准 pytest 命令只保留仍需忽略的 `tests/test_api.py`。
3. 本地运行 `tests/test_model_api_chat_send_routing.py`、`tests/test_e2e_tools.py` 和
   `tools/e2e_encryption_test.py`。
4. test 环境至少跑两格 hosted P0，确认 202、客户端解密、continuity 与 teardown。

回滚方式：回退删除提交；无持久化恢复步骤。
