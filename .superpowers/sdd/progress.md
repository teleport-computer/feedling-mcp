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
Task 11: complete (356323f4 + 8151874a 硬化, review clean;opus 审信任边界过;密文边界实证)
  硬化: socket 目录 0700+属主校验、泄漏测试 fail-closed、material-text ps 提示
Task 12: has_content 修复中(1c7c3491 + 待修)。
  ⚠️ 待 hx 拍板(Minor,非阻塞): 蒸馏合并语义扩张到云端 update_identity/name-writeback——
     改为 merge-onto-latest,后果=不能再靠"重新上传"清空某字段(与"缺失=不变"原则一致,
     但属既有 replace 调用的行为改变,需 hx 明确背书)。
Task 12: complete (1c7c3491 + 7948609e, review clean 2nd round;opus 审"没提字段永不丢"实证;Phase 4 完成)
Task 13: complete (653cea2d + 5c270ca9, review clean;顺带堵 identity-redistill 漏进云端目录;回退文案补齐 18 verb;Phase 5 完成)
Task 14: complete(docs-site changelog Unreleased + self-hosting 信任模型;OpenAPI 重生成核对无 diff
  (identity/genesis 两端点契约本就是 generic schema,字段级增补不改公共 schema;redistill IPC
  本地-only 不入 OpenAPI);types:check/lint/build 三项全绿;Phase 6 完成)
Task 14: complete (f5a21e21, review clean 1st round;docs-site 全检通过 npm ci/types/lint/build/openapi;每条文案溯源到代码实证)
Task 15: complete (33159cee + 57257b9c, review clean 2nd round;opus 逐条对 origin/pre 核实;
  Critical=拒绝闸落错函数会无限重试,已改到 patch() retryable=False。迁移手册可执行。Phase 6 剩 T16)
Task 16: complete (error_code/error_hint 分类 + processing 卡点字段,纯增量,write_genesis_state
  单落点覆盖 mark_failed 与 3 个 reaper 直写路径;consumer_offline 定义未接线、decrypt_failed 找到
  真实抛点已接线;27 新测试(_PURE_UNIT)+ 修复 4 处旧测试 lambda 签名;回归 252 全绿;Phase 6 完成)
Task 16: complete (d391e7a1, review clean 1st round;真实抛出串核实;consumer_offline 留定义未接线诚实标注)
  Minor 待终审: test_genesis_v2_orchestration.py:614 一处旧签名 lambda 目前不触发,潜在 trap。
=== 全部 16 任务完成。整分支终审(final whole-branch review)开始。===

=== 整分支终审(内部 opus): READY TO MERGE, 0C/0I。4 Minor + 3 待签字残留 ===
Minor(待合并 Codex 结果后一轮修): 
  M-a init_identity_if_absent 也绕过 CAS(未随 relationship_days_set 一起留档)——补注释
  M-b 云端回退目录里 identity-write 仍列旧 3 flag(仅 build_catalog 失败时可见)——补全
  M-c shadow"逐字节"有一个刻意例外:成对闸三档全开会拦未配对改名(设计如此,需向 hx 讲明)
  M-d 成对闸在 VPS(api key 无 runtime token)只有 consumer 漏斗挡着(coherence 非授权;
      真危险的 replace 服务端 403 硬挡)——设计如此
待 hx 签字残留: T12 合并语义扩到云端 update_identity(不能靠重传清空字段)/ >10 动作服务端静默截断 / relationship_days_set 绕 CAS
未验证(合前 DoD): 加密 e2e(红线:redistill 信封复用 v1 AAD,须真 enclave 部署跑)/ 真 2-worker CAS 竞态 / 真模型 e2e
=== 等 Codex 终审 ===

=== Codex 终审: NOT PASS 1C+7I+1M。逐条裁定 ===
过夜就修(明确正确性/契约 bug,安全):
  C1 rewrap_to_current_key 用普通 _save_identity 覆盖→绕过 CAS 可丢 CAS 胜出的写。
     修=该写也走 set_blob_if_unchanged(只改写条件性,不动 AAD/envelope,不触加密红线)。
  I4 _clean_list_items 先 raw[:12] 再校验→空列表 add 13 静默截断成 12,而 changelog 承诺报错。
     修=add/replace 先全清洗去重再查长度整体拒绝。
  I6 T16 分类器:fact-map 的 401/429/timeout 全被吞成 all_fact_maps_failed→误报 model_empty_output。
     修=reducer 保留底层分类,全败时按优先级保 bad_api_key/quota/timeout。
  I2 D3 来源护栏只在动态目录头,生成失败即消失、未写各 mutating verb help(D2 又取消了确认)。
     修=D3 进静态 system prompt + 共享常量写入所有写命令 help + fallback 带全字段面。
  I5 redistill job 与 chunk 两事务,崩溃窗口→同 request_id 重试判"chunk 已存"跳过→空任务卡死。
     修=job+首 chunk 同事务,或重试时检查 chunk0 缺则补写。
  I1b T10 服务端信客户端自报 job_kind,省略即退回 update_identity 绕过 0023 排他。
     修=redistill 路径服务端强制 source_kind,不信客户端。
  Minor 迁移手册 alembic 图述不准(0049 已合 0022+0048)+ 重复数据清理其实 0023 自己做。修文案。
  opus M-a init_identity_if_absent 也绕 CAS 未留档→补; M-b 云端 fallback identity-write 仍旧 3 flag→补全
  I7(部分) resident 蒸馏模板/parser 缺 5 字段,"全量字段"名不副实。
     过夜修=措辞改准 + 把 user-authored 字段(custom_persona_prompt 等)显式标为不可蒸馏(合 D3)。
留 hx 拍板(架构/信任边界/推翻既有决定,不过夜动):
  I1a VPS 用长期 api key 而非 scoped runtime token→成对闸等在服务端对 api-key 来源不生效
     (coherence 非授权;真危险的 replace 有 403 硬挡)。修法=给 VPS agent 发 scoped runtime token,
     属 auth 模型架构改动(zhihao),迁移手册已列方向。
  I3 服务端 >10 动作返 200 静默截前 10——与我们先前"不动服务端 slice(App 兼容)"的明确决定冲突。
     Codex 建议改 400;但这是推翻既有决定,hx 定。
  I7(产品契约) 那 5 个字段到底该不该允许蒸馏——产品决定,hx 定。
修复分三簇串行(避免同分支并发提交撞 index): P=genesis(I5/I6/I1b/I7措辞) → Q=identity/content(C1/I4/M-a) → R=prompt+docs(I2/M-b/Minor)

Cluster Q: complete.
  C1 rewrap_to_current_key 的 identity 写改走 _save_identity_cas(读取时机不变,只把最终写
     变成条件写);CAS 冲突→重读最新卡→用最新明文重建信封→有界重试(3 次,同
     identity/actions.py 的 _IDENTITY_WRITE_MAX_ATTEMPTS)。未动 envelope id/AAD/K_enclave —
     加密语义逐位不变。新增 2 条真实 DB(make_client)测试覆盖两种时序:
     (a) 并发写在 rewrap 解密期间落地→rewrap CAS 失败重读保留并发写的内容;
     (b) rewrap 先落地→profile_patch 用自己既有 CAS 正常succeed。
  I4 _clean_list_items 先 raw[:12] 再查长度的顺序 bug:新增 _clean_list_items_uncapped,
     add_*/replace_* 都改成"先清洗去重(不截断)→查真实长度→超 12 整体拒绝"。仅遗留的直接
     赋值(legacy 裸字段名,如 `signature`)保留截断行为。改写了一条被本次修复推翻的旧测试
     (test_replace_and_legacy_paths_still_silently_truncate_not_reject 拆成两条,legacy 保留
     截断、replace_ 改断言为拒绝),changelog 措辞同步更新。
  M-a init_identity_if_absent 补 KNOWN RESIDUAL 注释(同 relationship_days_set 的写法),
     两处注释互相引用形成"清单"。未改行为(纯文档,任务要求不连夜重做 onboarding init)。
  回归: test_identity_list_ops/test_identity_actions/test_asgi_identity/test_content_rewrap*
     /test_genesis_*/test_identity_concurrency_baseline 全绿(--collect-only 核对新纯单元测试
     未被 _PURE_UNIT 白名单以外的规则吞掉)。

=== 终审修复波合并复核(opus,喂了台账): 全部 fixed✅,C1 未碰加密红线,3 延期项确实仍开。
    VERDICT: READY TO OFFER FOR MERGE TO test。===
最终全绿(post-fix-wave): 392 + consumer 456 + distill 16 passed, 0 fail。
分支未推、未合——等 hx 拍板。合前 DoD 未做项(需部署): 真加密 e2e / 真 2-worker CAS 竞态 / 真模型 e2e。

=== 追加 Task 17 (B2, hx 2026-07-23 拍板): 用户层 5 字段补齐生产链 ===
决定:custom_persona_prompt / user_preferred_name / language_preference / relationship_anchor /
  stable_definitions 这 5 个"用户层"字段,此前全链无生产入口(onboarding/二次蒸馏/做梦都不产,
  只有对话 identity-write 能写)。B2=两个蒸馏器都补上,让新建卡与重新总结都能从素材抽出它们。
  **这反转了 T7/ef8e393d(I7)"5 字段刻意不蒸馏"的决定**——那批注释/迁移手册需同步改。
  做梦机制确认只整理记忆、不碰身份(9747),不在本次范围。
  风险:动 onboarding 建卡主流程 + prompt 行为,单测抓不到,合前必须真模型 e2e。
