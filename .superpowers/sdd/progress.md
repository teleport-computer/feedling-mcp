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
Task 6: complete (65f454be + 22897dc4, review clean 2nd round;修复顺带救回 cancel-wake --wake-id;Phase 2 完成)
Task 7: complete (2c26217c + 17f52210, review clean 2nd round;审查为 opus 级,打回 1C+3I+3M 全修)
  待 hx 拍/终审汇总: ①I4 残留——失败标记可能被同轮后续 completed 状态覆盖(可观测性,非阻塞,建议后续补 flag) ②proactive 轮附带 noop 说明句的文案取舍
Task 8: complete (ad91fdfa, review clean 1st round)
  待终审汇总: ①新会话前两轮会重复注入一次(有界、自愈,已引 bridged 模式为后续优化) ②sys.path 防御性插入建议改到测试层解决
Task 9: complete (02e069b9 + c8bfa6b5, review clean 2nd round;Phase 3 完成)
=== Codex 中场 review(T1-T9)开始,期间不动代码 ===
=== Codex 中场(T1-T9): BLOCK, 2C+8I+2M。裁定:9 采纳、1 部分采纳 ===
采纳: C1 跨 worker CAS(进程内锁不够,2 gunicorn worker)/ C2 4xx 丢弃逐条结果致重试双写 /
  I3 夹带漏斗补成对校验(spec 承诺未实现,被 Codex 抓包) / I5 英文用户收中文附注(语言取自用户消息) /
  I6 目录漏必填位置参数 / I7 add 突破 12 上限 / I8 list_ops 测试假纯(真 UserStore) /
  I9 修法句改"报告给用户"口吻(与 D3 自洽;既有 348 行诱导执行属既有问题,留档) /
  I10 注入标记应成功后提交 / M11 严格 applied / M12 空白字符
部分采纳: I4 超 10 条动作服务端静默截断——服务端不改(共享入口改 400 = 动 App 既有行为,T1 教训),
  CLI 侧硬校验总数≤10;服务端行为留档为已知怪癖+迁移备注。
修复批次: B(consumer C2/I5/I3/M11)→D(T8 I10)→A(backend C1/I7/I8)→C(io_cli I4/I6)→E(I9/M12),串行防 git 冲突。
=== Codex 中场闭环: 5 修复批(2c44ba49/2e598018/1d038d39/e5f9167c/e1d5a488)+ 合并复审(opus)
    抓 2 残留 → C1 快照时序 75cc8aaa(回退验证法实证)+ I3 语义对齐 72e9b481。全部闭环。===
恢复主线: T10 起。
Task 10: complete (f0d2ba2c, review clean 1st round;真库验证 409 路径活的,索引 0023 单头)
  Minor 待终审: 极窄竞态窗下 409 的 active_job_id 可能为空串(不误判,仅信息量)
Task 11: complete (io_cli identity-redistill + consumer 本机 IPC;复用 T10 sealed 车道 + 既有
  update_identity 蒸馏管线,无新蒸馏逻辑;FEEDLING_HOME 无既有约定,借用 CHECKPOINT_FILE 的
  fingerprint 配方新增默认值;15 新测试全绿,回归 456+9 全绿)
  Minor 待终审: catalog 生成器对 mutually_exclusive_group(required) 的 usage 解析留个 `( | )`
  装饰性残影(功能不受影响,未动共享解析器)
