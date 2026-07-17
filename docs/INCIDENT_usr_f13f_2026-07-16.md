# 事故排查 → 给志豪:usr_f13f922a9ab518ba（2026-07-16）

## 一句话
一个 Windows 自托管用户同时踩了三个**独立**故障：①名字一直显示 `[encrypted — decrypt failed]`、②她发消息报错、③AI 每分钟刷屏"(在)(等着)"。

**分工**：②③ 我们（Claude 这边）自己处理，**只有 ①「身份卡解密失败」这条加密/enclave 的活想请你接**——这块动了风险最高，交给你更稳。下面主要讲 ①，②③ 只在末尾给你个全貌，不用你管。

（代码位置基于 `feedling-mcp` `test` 分支当前 HEAD，麻烦先 `git pull`。）

---

## 想请你修的：内容密钥轮换后没有自动重加封（导致身份卡解不开）

**现象**：名字/身份卡显示 `[encrypted — decrypt failed]`，但**聊天消息能正常解密**（分裂状态）。

**根因**：
- 信封是**双封**的（user_pk + enclave key，`backend/core/envelope.py:70` `_build_shared_envelope_for_store`）。
- 她的设备内容密钥**轮换过**（重注册/重生成 `user_pk`；whoami 自愈会重注册 key）。**聊天持续写** → 都封到新 key，能解；**身份卡极少重写**（只在 genesis/改身份时），一直封在**旧 key** 下 → 解不开。
- 缺口：**重注册/轮换时没有触发 rewrap**。whoami 自愈换了 `user_pk`，却没把已存的 envelope 重新加封。代码注释其实已经预警过这个搁浅风险：`backend/content/content_core.py:102-103`（"rotation guard 放行 key 变更却没 rewrap，把 body 搁浅"）。

**好消息：修复机制已经存在，不用造新东西。**
- `backend/content/content_core.py` 已有 `/v1/content/rewrap-to-current-key`，enclave `backend/enclave/storage_crypto.py` 已能"解密 v1 信封 → 重新加封到当前 key"。因为 enclave 有自己那份 recipient 副本，**能代解代封，不需要用户旧私钥**。

**建议修法**：
1. 把已有的 `rewrap-to-current-key` **接进重注册/密钥轮换路径**，确认它**覆盖身份卡**（不只 chat/memory/world_book）。这样密钥一换所有 envelope 自动重加封，永不搁浅。
2. **查根因**：她的 content key **为什么会轮换**？正常重注册还是 bug？设备已有可用 key 时不该覆盖 —— 这是防止再发生的关键。
3. **对这个用户的即时补救**：先看她的身份卡信封在 **enclave 侧还解不解得开**：
   - 若能解 → 手动跑一次 rewrap 就能把她的**原身份卡**恢复（无需她的旧 key）。
   - 若 enclave 也解不开（比如 CVM 迁移换过 enclave key）→ 原文找不回，只能让她**重建一张新身份卡**。

**麻烦你**：修的时候两边 confirm 最终结果、做好测试再上；有结论同步我们一声（尤其上面第 3 点她到底能不能救回原卡，得你在后台/enclave 侧确认）。

---

## 附：②③ 我们自己处理（给你个全貌，不用你动）

- **③ 主动消息刷屏**（我们改）：presence 心跳绕过抑制、只受 Ambient 管、agent 自排 wake 被每 60 秒 fire 循环点燃、无频率闸/无连续失败熔断（只有 402 冷却）。我们会加：每用户 proactive 频率上限 + 自排 wake 最小间隔地板 + 连续失败退避。相关文件 `backend/proactive/gate.py:67`、`controls_v2.py:257`、`tools/chat_resident_consumer.py:269/367-392/7654`。
- **② Windows MCP 配置坑**（我们改）：她的 `AGENT_CLI_CMD` 写死 `--mcp-config C:\Users\Administrator\...` 指向 consumer 从不生成的文件 → claude exit 1，只挂前台聊天。我们改自托管模板用 `{mcp}` 占位 + consumer 落空文件兜底 + 启动预检 + 更新 quickstart/troubleshooting。

> 注：③ 有一半在后端（gate/controls），我们改的时候会 gatekeep 并同步你，避免和你那边撞车。
