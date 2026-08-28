---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除失效的 `tests/e2e_model_api_test.py`

结论：`delete`，已在本批实施。provider smoke harness 保留，只收敛了它对旧脚本的说明。

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

253 行是被删脚本的 gross；实施批次还清理了 current 引用并修正了测试文档中与双运行时
策略冲突的旧描述。实施 diff 为 48 行新增、287 行删除，net 删除 239 行。

## 兼容与取舍

- 不影响生产、schema、wire 或部署；删除的是已经无法正确判定 current 契约的脚本。
- 名义上会失去“本地 simulator + 真 provider”组合入口。若团队仍需要这组能力，应作为
  新功能并入 canonical E2E，而不是修补这个无 teardown 的旧入口。

## 实施与验证

1. 已删除脚本和 current 引用；标准 pytest 命令只保留仍需忽略的 `tests/test_api.py`。
2. 未增加“文件名不能出现”的源码文本断言：这类测试只固化清理决策，不能保护运行行为；
   当前契约继续由 202 路由、E2E 工具和 current 文档生命周期测试保护。
3. 本地 targeted suite 覆盖 `tests/test_model_api_chat_send_routing.py`、
   `tests/test_e2e_tools.py`、current 文档测试和 `tools/provider_smoke/tests`。
   命令为 `pytest tests/test_model_api_chat_send_routing.py tests/test_e2e_tools.py
   tests/test_current_state_docs.py tests/test_document_lifecycle.py
   tools/provider_smoke/tests -q`，结果为 105 passed。标准完整命令
   `pytest tests -q --ignore=tests/test_api.py` 的结果为 12086 passed、3 skipped、
   9 xfailed。
4. test 环境（部署 SHA `6559558410749e32907c9abd4d73fa0f29fcea53`）的 canonical
   P0 结果：`anthropic-official` 和 `gemini-official` 均通过 setup、202 异步聊天、客户端
   解密、continuity、memory、零错误气泡与 teardown。OpenRouter 格在 setup 时被外部
   provider 以 `403 Key limit exceeded` 拒绝；本地 key pool 未配置两个
   `openai_compatible` relay 格所需的 key，因此本批不把 relay 记为已覆盖。

回滚方式：回退删除提交；无持久化恢复步骤。
