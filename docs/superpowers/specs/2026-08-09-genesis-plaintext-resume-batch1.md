# Spec: Genesis plaintext 导入可恢复化 —— 第一批(地基 + 省钱 + 观测)

- 日期:2026-08-09
- 发起:Seven(usr_3b73f1cb0a9ec975 复盘,四步方案已拍板,本批是第 1 步 + 第 4 步 + 观测)
- 实现:codex3(backend)
- Gatekeep:claude3
- 分支:test

## 背景

usr_3b73f1cb0a9ec975,6.8MB 聊天记录导入(47,485 条 / 67 窗 / tier=large,蒸馏模型
`anthropic/claude-haiku-4.5` 走中转)。2026-08-06:map 阶段 67/67 全部完成、138 张卡已写入,
10:42 UTC 起断流,11:12 被 1800s 兜底收割器判终局失败,persona/完整身份没蒸出来,
onboarding 永远卡 7/8。用户换 DeepSeek V3 Flash 才成功。

**根因不是模型能力,是暴露时长 + 中断即死刑。** 决定性证据:plaintext 导入有独立心跳线程
每 **15 秒**给 job 打点(`_run_plaintext_job_heartbeat`,`backend/genesis/plaintext.py:2290`),
与 LLM 调用快慢无关;该 job 30 分钟零心跳 → **承载它的 API 进程整个死了**,不是模型挂死。

**结构性缺陷**:plaintext 导入跑在处理请求的 API 进程内的后台线程里
(`_start_plaintext_genesis_job`,`plaintext.py:2318`),素材与窗口都在该线程内存中。
进程一死,没有任何人知道该接着干什么 —— 两个收割器都只会标失败。

**前一版 spec 已作废**:`docs/superpowers/specs/2026-08-07-genesis-stale-reap-requeue.md`
(codex3 已实现 `9860dd7f`,gate **未通过**、未合并)。作废原因是 claude3 在该 spec 里
错误断言"plaintext 素材也是加密分块存服务端"——实际 `received_chunks` 只由封装分块上传路
(`db.genesis_put_chunk`)写,plaintext 路**永远是 0**,那个修复对本案完全不触发
(真 PG 实测 `reap action = ('failed','failed')`)。**本批不复用该分支**,其处置见文末。

## 目标终局(Seven 已定,分四步)

素材上传后用户不用管;进程死亡由系统在**同一次运行内**自动续跑,状态**全程不离开
`processing`、不闪失败**,进度只前进不倒退。
第 1 步地基 → 第 2 步能力(人工触发)→ 第 3 步自动化 → 第 4 步省钱。
**本 spec 只做第 1 步 + 第 4 步 + 观测。**

## 已核实的代码事实(本批地基,均已验证,勿再假设)

| 事实 | 位置 |
|---|---|
| `staged_id` **不在** `SAFE_JOB_METADATA_KEYS` 也不在 `INTERNAL_JOB_METADATA_KEYS` | `backend/genesis/service.py:355-374` |
| `create_import_job` 用 `_safe_job_metadata(payload["metadata"])` → **把 `staged_id` 过滤掉** | `service.py:731`(函数)/ `:749`(过滤行) |
| 只有**重试路**用 `db.genesis_patch_job_metadata` 直写 staged_id(绕过过滤) | `backend/genesis/genesis_core.py:811-813` |
| 首次创建处传 `trusted_metadata=_plaintext_worker_metadata()`,**未带 staged_id** | `genesis_core.py:855-863` |
| `consume_staged_for_completed_job` 读不到 `metadata["staged_id"]` 就 return | `service.py:~575` |
| **现有测试是假绿**:mock 掉 `service.create_import_job` 后断言**入参 dict** | `tests/test_genesis_plaintext_routes.py:806`(`fake_create`)、`:836`(断言) |
| checkpoint 文档**无 schema 校验/白名单**,未知顶层键原样保留 | `backend/genesis/checkpoint.py:108-111` |
| voice map **无 checkpoint**,每窗每次重跑;fact map 有 `resume_map_outputs`/`on_map_completed` | `backend/genesis/worker.py:1062-1080` vs `:1084-1109` |
| `_voice_reduce` 批次树依赖候选**数量**(batch 默认 24),幂等键用 `round_no`/`idx` | `worker.py:825-868` |
| trace 事件类型是自由字符串,无需注册/无 catalog | `worker.py:117` / `plaintext.py:77` |
| `_plaintext_owner_process_is_dead` 仅在"同机 + PID 不存在 + POSIX"判死 | `plaintext.py:339-365` |

推论:**首次提交的 plaintext job 行没有素材指针**,后续批次的自动续跑无从下手;
且 `consume_staged_for_completed_job` 对首次提交是**空转**(既有小 bug,本批顺带修好)。

## 改动

### 1. 持久化 `staged_id`

- 把 `"staged_id"` 加入 `INTERNAL_JOB_METADATA_KEYS`(`service.py:370`)。
  理由:它是 `core_util._new_public_id("staged")` 生成的**不透明服务端 id、非内容派生**,
  与既有 `plaintext_worker_pid` / `plaintext_worker_instance` 同类。
- 在首次创建处(`genesis_core.py:855-863`)把 `trusted_staged_id` 并入 `trusted_metadata=`。
- **安全边界不变**:客户端提交的 `staged_id` 仍一律不可信
  —— `test_plaintext_direct_import_ignores_client_staged_id`(routes:1049)必须继续绿。
- 副作用(期望):首次提交成功后 stage 真正被释放,不再依赖 TTL/下次 stage 的 reap。

### 2. voice map 纳入 checkpoint

- `_PlaintextCheckpointProgress`(`plaintext.py:562-704`)增 `resume_voice_outputs()` /
  `record_voice()`,与既有 `resume_outputs()` / `record_map()` **对称**;
  存 `doc["voice_outputs"]`,task_id 用**独立前缀**(如 `plaintext-voice:{pass}:{family}`),
  **不得**与 fact 的 task_id 共用 —— 否则两者的 `TASK_DONE` 标记互相污染。
- `_build_reducer_output`(`worker.py:961`)增 `resume_voice_outputs` /
  `on_voice_completed` 两参;voice 循环(`:1062-1080`)照抄 fact 循环的
  cache-then-callback 形状;**保留 voice 失败静默跳过的语义**(voice 是增强项)。
- 向后兼容:旧 checkpoint 无 `voice_outputs` → 全部重跑 = 今天的行为。
- **kill switch**:`FEEDLING_GENESIS_VOICE_CHECKPOINT_ENABLED`(默认 `"1"`),
  体积若成问题可不发版关掉。

⚠️ **本项最大暗礁**:`_voice_reduce` 的批次树依赖候选**数量**。缓存后数量应当与
全新跑**完全一致**(实际上比今天更稳,因为今天失败静默丢弃会让数量run-to-run漂移)。
测试必须显式钉住这一点,见验收 §3。

### 3. 中断原因埋点(纯观测,不改判定)

- 新 trace 事件 **`genesis.plaintext.interrupted`**(命名对齐既有 `genesis.plaintext.*`),
  在判定 plaintext job 卡死处发出(`_fail_stale_plaintext_job`,`plaintext.py:368` 一带)。
- `detail` 至少含:`cause`(`owner_pid_dead` / `heartbeat_aged` / `unknown`)、
  `windows_done` / `windows_total`、`elapsed_sec`、`history_tier`、`distill_model`、
  `checkpoint_bytes`。
- `checkpoint_bytes` 用来观察改动 2 带来的体积增长(67 窗量级)。
- **不含任何明文/素材内容**(遵循 `_trace_genesis` 既有约束)。

目的:目前只有**一个**中断样本且靠推断。有了 `cause` 分布,第 3 步的自动化才能对着
真实构成比例设计,而不是再猜一次。

## 不做

- 不加自动续跑(第 2、3 步),**不改任何收割/失败判定行为**。
- 不动 staged TTL(24h)、不动 1800s / 120s 阈值。
- 不碰 `9860dd7f` 分支。
- 不改 iOS/App。

## 验收(本批历史教训是"假绿",按 TESTING.md 总原则 #5 严格执行)

1. **必须替换假绿测试**:`staged_id` 的断言要打在**落库后的行**上(真 PG 读回
   `genesis_import_jobs.metadata`)。**不得**通过 mock `service.create_import_job`
   断言入参 —— 那是 mock 我们自己的产生方,测的是替身。
   `tests/test_genesis_plaintext_routes.py:806` 的 `fake_create` 是反面样板,
   请在 commit message 里点名说明为什么替换它。
2. **变异验证(真去改坏,不是自问)**:
   - 从 `INTERNAL_JOB_METADATA_KEYS` 去掉 `staged_id` → 对应用例必须**精确变红**;
   - 关掉 `FEEDLING_GENESIS_VOICE_CHECKPOINT_ENABLED` → voice 缓存用例转为期望重跑。
3. **voice checkpoint 正确性**(三条都要):
   - 续跑时被缓存窗的 voice LLM 调用数 **== 0**;
   - **voice 候选数量与全新跑一致**(钉住 `_voice_reduce` 批次树不漂移);
   - 旧 checkpoint(无 `voice_outputs`)照旧全跑,不报错。
4. **不得写重复卡**:扩展既有
   `test_plaintext_retry_uses_checkpoint_and_skips_completed_maps`(routes:1692),
   断言**卡片总数不翻倍**。本批虽不自动续跑,但手动重试续跑路今天就存在。
5. **既有回归判据 = 基线对照**:在本批改动的**父提交**开临时 worktree,跑同一组
   (`test_genesis_plaintext_routes.py` / `test_genesis_checkpoint.py` /
   `test_genesis_worker.py`),**diff 两边的失败集合**,要求逐条一致。
   不接受"看起来只有几个红、应该是既有的"这种判断。
   真 PG 用 `FEEDLING_TEST_PG`(本机 5432)。
6. **体积**:构造 67 窗 checkpoint,断言开启 voice 缓存后大小在可接受量级,
   且被 `checkpoint_bytes` 埋点如实记录。

### L3(部署到 test 后,claude3 执行)

- 真实大文件走 estimate→commit;从 admin data-track 确认**落库的 job 行**含 `staged_id`。
- 跑到一半 `kill -9` 承载进程 → 走**现有手动重试**路径 → 断言 fact 与 voice 两类窗
  都被跳过、卡片数不翻倍、`genesis.plaintext.interrupted` 带正确 `cause`。

## ⛔ 决定:第 2、3 批暂不做(Seven,2026-08-09)

四步方案原定 1→2→3→4 依次做完。第 1 批(地基 + voice checkpoint + 中断埋点)与
「手动重试三个洞」都已上线后,**Seven 拍板第 2、3 批暂缓,不排期**。理由:

1. **第 2 批(人工触发的重驱动)修不了真问题。** 它没有触发器 —— 做出来还是得
   靠我们盯着后台一个一个看,而我们不可能这么盯。**等我们发现的时候,用户已经
   卡了很久了。** 「发现延迟」才是这条路的痛点,而第 2 批不解决它,只是把一个
   我们几乎不会去用的工具做出来。
2. **第 3 批(自动化)风险收益此刻不划算。** 它要动收割器和失败判定 —— 是整个
   四步里最容易写出重复卡、双跑的地方,而**第 1 批已经把它的地基铺好了**
   (`staged_id` 已持久化、voice 已入 checkpoint),晚做的成本很低。

**更重要的前提变化**:「手动重试」这条兜底路今天已经修好了(诚实文案 + 真能用的
重试按钮 + 72h 窗口 + 一点就从 checkpoint 续跑)。用户不再会被静默卡死,自动续跑
从「止血」降级成「省一次点击」。

### 什么情况下重新拿出来

第 1 批埋的 `genesis.plaintext.interrupted`(带 `cause` / `windows_done` /
`elapsed_sec`)就是为此装的仪表。**看它的分布再决定**,别凭单个样本建自动化:

- 「进程死亡」类中断**频繁**,并且
- 这些用户**并没有**靠新的重试按钮自己恢复(即失败后长期停在 7/8)

两条同时成立,再启第 3 批;那时第 2 批可以直接并进去,不必单独做。

以下设计约束**原样保留**,重启时直接继承,不要重新推导:

## 后续批次的设计约束(冻结,重启时继承)

- **第 2 步(能力,人工触发)**:从 job 行重建素材并重驱动的函数;**先不接自动化**,
  用它把 usr_3b73 手动救回来,并在真实数据上验证"不写重复卡"。
- **第 3 步(自动化)**:守护进程按 **120s** 判据(而非 1800s)接管;预算
  **按"是否推进"计数**(完成窗数增加 = 不消耗预算;连续 2 次原地踏步 = 终局失败);
  **作业总时长上限 3 小时**,超过一律真失败、不复活。
  ⚠️ 必须**同时**改掉"App 查状态就把卡住的 job 标 failed"的懒惰路径
  (`genesis_core.py:520/747/782`),否则会变成"先闪失败再后台偷跑",不符合产品预期。
  ⚠️ **绝不能**把 plaintext job 放回 `uploaded` 队列(那是封装分块管道,会炸 `missing_chunks`)。
- **重复上传优先级**(Seven 定):用户显式上传**抢占**后台恢复;同一份素材
  (input_hash 相同)接上现有作业;正常续跑期间维持 409 `import_job_active`,
  但横幅文案从"失败请重传"改为"处理中请稍候"。抢占与续跑之间需数据库层原子条件更新 +
  真 PG 并发测试,**绝不允许两者同时跑**。
- **`9860dd7f` 处置**:并入第 3 批;它对封装分块导入仍有价值,但必须同时修正它引入的
  失实公开文档("resumes at reduction" —— 重排路径 `_process_job` 不传 `resume_map_outputs`)
  与被前一版 spec 改错的 db.py 注释。

## 升级规则(常设)

卡壳 >10 分钟、或发现 spec 与代码现实冲突:**立即回报,不猜、不绕、不造工具伪造结果**。
额度不足同样立即回报,不要硬撑到一半断掉。
