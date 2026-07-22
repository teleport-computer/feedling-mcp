# SDD 进度 — 双运行时共存（V1+V2+allowlist）

计划：docs/superpowers/plans/2026-07-21-dual-runtime-v1-v2-coexistence.md
分支：feat/dual-runtime（起点 ec377440）
授权：每-task commit（不 push）。Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

Pre-flight：plan 自审已修 5 处（perception lane task、镜像测试体、
reconcile 死条件、genesis 单认领结论、daemon.py 免恢复说明）。无 task 间矛盾。

## 任务状态
Task 1: in progress（分支已建，基线后台跑）
Task 1: complete（分支 feat/dual-runtime 建于 ec377440；基线实测 5006 passed/2 known-env-fail 语义等价：跑毕）
Task 2: complete (commits ec377440..9416fc4f, review clean/Approved; 2 Minor 备查：
  - test_v2_jobs_migration/test_v2_summary_watermark_seq 加了 0050 down_revision 断言（模式跟随，非 scope creep）
  - db.py 恢复注释引用的 wedge guard 到 Task 6 才存在（verbatim 恢复所致）
  计数核对：5008-17(retirement)+3(new)=4994 ✓)
Task 3: complete (commits 9416fc4f..a26ed44d, review clean/Approved, 零 issue;
  228 V1 测试全绿 + 1 skip[TODO(dual-runtime Task 4), resident_contract 等 consumer hosted 形态];
  跨 task 备忘: conftest 的 FEEDLING_HOST_ALL setdefault 未恢复→属 Task 6 cutover gate;
  Task 4 须解除 test_agent_runtime_resident_contract.py 的 skip)
Task 4: complete (commits a26ed44d..1ff59bdb, review clean/Approved;
  graft: io_cli runtime-v2-status/repair-runtime-v2 (byte 级核对);
  ⚠️决策留痕→给用户最终汇报: 丢弃 pre 侧 ~600 行 checkpoint 崩溃恢复子系统
  (5de01cc1 et al; 全仓零引用+与 origin/test reply 架构不兼容; prod runner 跑的
  就是无此系统的 test 血脉=现状 parity; 建议未来独立 task 移植);
  wire-compat 已验: chat_core.py:799-805 resident_delivery_id 等全可选;
  un-skip resident_contract 6 passed; 全量 5241 passed 0 新失败)
Task 5: complete (commit ef768eae; config_store 双值 policy + 双向
  set_hosted_runtime_mode + admin 去闸; forced_hosted_runtime_mode() 恢复
  str|None 语义, dual→None 让 setup_core._runtime_should_restore_v2 的既有
  None 分支首次跑活; admin_core.set_runtime_mode 逐字恢复 2b294a1f^ 原形;
  三份 brief 测试文件 +16 用例(policy+4/mode+10/admin净+1) 恢复 pre-era 双向
  覆盖, resident-被拒断言限定 v2_only env;
  ⚠️跨 task 事故留痕: default 翻 dual 后 4 个 send happy-path 测试文件
  (test_asgi_hosted_chat_send/test_model_api_chat_send_routing/
  test_model_api_path/test_model_api_runtime_token_decrypt) 隐式依赖
  setup 启动物化强制 V2 而集体 503 runtime_policy_not_ready——根因是
  chat_send_core 仍只走精确 V2 fence(该文件本 task 不许动，双向路由是后续
  task)，而这些文件的 fixture 从未显式 flip，全靠旧 v2_only-only 默认值
  兜底；修法=在各文件 client/env fixture 里钉
  FEEDLING_HOSTED_RUNTIME_POLICY=v2_only（未碰 chat_send_core/setup 语义）；
  全量重跑 5256 passed(+15)/1 known e2b fail/3 skipped/9 xfailed，0 新失败;
  Task 6 (chat_send_core 双向路由) 需知悉此默认值翻转的影响面)
Task 5: complete (commits 1ff59bdb..ef768eae, review clean/Approved;
  执行插曲: 实现者后台套件卡死→控制器接管跑全量→16 个 v2 happy-path send 测试失败
  (根因=dual 默认下 apply_hosted_runtime_policy no-op, 新用户 fence 落 resident_cli,
  chat_send_core 硬要 v2 tuple→503)→修法=4 文件 fixture 级钉 v2_only(P7 回归网语义);
  admin_core 恢复字节级核对通过; forced_hosted_runtime_mode 5 调用点全记录;
  2 Minor: set_hosted_runtime_mode 双读 policy(退役前原形)/policy_status 在 dual 下
  target_ready 恒真(信号价值降,P7 或状态面板改造再看);
  Task 6 注意: 4 个 send 测试文件 fixture 已钉 v2_only→dual 路由矩阵测试放新文件)
Task 6: implementation approved (ef768eae..810d6e06); 一个 plan-mandated 偏差经用户拍板
  →修复中: wedge 503 body 恢复历史值 hosting_runtime_unavailable(canary:235 依赖它判可重试;
  plan 文本三处已由控制器改正); 修复 commit 待落, re-review 后才标 complete。
  reviewer 全五项 named-risk PASS: cutover 11 函数 AST 级 byte-identical、v2 路径零 hunks、
  _send_resident 忠实移植、draining 先于持久化 503、verify_loop 门 byte-match。
Task 6: complete (commits ef768eae..ed23e820 [810d6e06 主体 + ed23e820 body 修复], review clean/Approved;
  用户拍板 wedge body=hosting_runtime_unavailable(历史) / summary=supervisor_unavailable;
  canary:235 可重试集自动兼容; 全量 5302 passed 0 新失败; +46 用例含新文件
  tests/test_dual_runtime_send_routing.py 三态矩阵 10 用例)
Task 7: complete (commits ed23e820..c6bd29c1, review clean/Approved;
  roster NOT EXISTS 排除 v2/draining、缺省行=resident 已验(0025 schema);
  perception flag 零写点→option(b) 委托 fence(3 读点零适配);
  2 Minor 备查: PERCEPTION_INGRESS_RUNTIME_V2_FLAG 常量已死(留)、
  get_hosted_runtime_mode_strict 只读 mode 依赖 patch_blob_strict 的 mode+state 同事务原子性
  (今日唯一写者满足,防未来新写者破坏→给最终 review 关注);
  Task 8 须回补 test_perception_flag_follows_fence_after_flip 的 reconciler 版断言)
Task 8: complete (commits c6bd29c1..bda95682, review clean/Approved;
  真实 leader API=run_singleton(job-scoped), 接线 asgi/lifespan.py:189 同胞模式;
  P6 scope union 语义核对无 gap; +12 用例(5 reconciler+7 admin);
  2 Minor 备查: admin_core.get_runtime_allowlist 两个冗余局部 import、
  _failures dict 对离场用户理论性增长(canary 规模无害,毕业时加注释))
Task 9: complete (commits bda95682..39c05a65, review clean/Approved; 6 契约用例一次全绿=Tasks 2-8 完整性证明)
Task 10: complete (commits 39c05a65..927f4f04, review clean/Approved+reviewer本地复测3/3;
  P0 硬门证明: 双向 flip 零丢失(真 DB 断言: chat行数/job行数/generation/trace_id)、
  draining 503 先于任何持久化; 发现+核实一个生产级恢复机制
  db.reconcile_unenqueued_v2_message_for_user(runtime_cutover_recovery:
  flip 时未回复的 resident 消息自动 pin 到新 generation 入队——比 plan 预期更强的保障);
  brief 假想字段已适配真 schema(expected_runtime_generation/trace_id);
  Minor 备查: 并发 single-claim 压测非本 gate 范围(已有 jobs_store/reconcile/worker 测试覆盖))
Task 11: complete (commits 927f4f04..7a009134 [efe74966 主体 + 7a009134 修复], re-review Approved;
  修复: main-CVM guard 无条件化+组合回归测试、test runner compose byte 恢复 origin/test
  (reviewer 前提纠正: test 环境一直是 V1 托管)、CI test-runner job V1 适配、
  三环境同构披露; 6 compose config -q 全过; 全量 5328 passed 0 新失败;
  留意: 一次 test_v2_effect_outbox 顺序依赖 flake(不可复现,pre-existing))
Task 12: complete（全量 5328→5330 passed 0 新失败; pyflakes 全 pre-existing; spec §5 12项+§6 lane 表全勾）
Final review: With fixes → 修复 db178b0f（allowlist removal 不回滚 v2 用户的应急路径坑,
  option(b) scope union nonresident_controls + 2 回归测试）→ re-review **Ready to merge: Yes**
全部完成。13 commits: 9416fc4f..db178b0f。未 push（需用户指令,push 触发 CI 部署）。
