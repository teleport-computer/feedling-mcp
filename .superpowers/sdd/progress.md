# SDD 进度 — 聊天 provider 失败可见性（第一批 / spec §2）

计划：docs/superpowers/plans/2026-07-18-chat-provider-failure-visibility.md
后端分支：fix/provider-error-notice-blame-throttle（起点 2eb4047d）
iOS 分支：fix/provider-error-preserve-code（worktree /Users/hx/Projects/io/feedling-mcp-ios-provider-errors）

Pre-flight：修了计划里两处自造缺陷（测试 inline __import__("uuid")；
/v1/chat/send 返回体只有 id，去掉误导性的 message_id 回退）。
已确认 chat_core.py 无模块级 log，Task 2 需新增。

## 任务状态
