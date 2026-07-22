# SDD 进度 — 聊天 provider 失败可见性（第一批 / spec §2）

计划：docs/superpowers/plans/2026-07-18-chat-provider-failure-visibility.md
后端分支：fix/provider-error-notice-blame-throttle（起点 2eb4047d）
iOS 分支：fix/provider-error-preserve-code（worktree /Users/hx/Projects/io/feedling-mcp-ios-provider-errors）

Pre-flight：修了计划里两处自造缺陷（测试 inline __import__("uuid")；
/v1/chat/send 返回体只有 id，去掉误导性的 message_id 回退）。
已确认 chat_core.py 无模块级 log，Task 2 需新增。

## 任务状态
Task 1: complete (5aa5e448, 3 tests, chat regression 658 passed)
Task 2: complete (ef2884b0, +2 tests, 5 passed; chat regression 后台跑)
Task 3: complete (62374ad4, consumer 34 passed, 端到端 since 实证 6 passed)
Task 4-7: complete (iOS 71a45ab/a20eb00/7c000f4, contract 64789dfb)

# SDD 进度 — io_cli 能力补全(2026-07-22 计划)
计划:docs/superpowers/plans/2026-07-22-io-cli-capability-completion.md
分支:feat/io-cli-capability-completion(起点 86a317a1)
Task 1: complete (commits 1f0ad35c..a026b476, 4 commits, review clean after 3 fix rounds:
  空名判定同源化→弯引号字符集抄丢→守卫测试字面量转义;22 passed)
Task 2-15: 未开始(hx 2026-07-23 指示 Task 1 后暂停,插入 onboarding 失败暴露调研)
