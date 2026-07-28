# TEE 库扶正 + 明文默认/可选加密 — 主实施计划（v3）

> **For agentic workers:** 这是**主计划（program plan）**。每个 Phase 开工时用
> superpowers:writing-plans 基于本计划的该节另立带完整代码的细案（子计划），再用
> superpowers:subagent-driven-development 或 executing-plans 执行。本文锁定阶段
> 边界、门禁（gate）、顺序依赖和验收标准。步骤用 `- [ ]` 跟踪。
>
> **v2 变更（2026-07-23 用户拍板）**：加密从「全部移除」改为「**默认明文、
> 按用户全局开关可选加密**」；存量 local_only **原地保留为加密**（客户端解密
> 路径保留）。因此：私钥收集整个删除（原 v1 Phase 1）、local_only 迁移删除
> （原 v1 Task 2.3）、服务端双格式从「过渡态」升级为「**永久常态**」。
>
> **v3 修订（2026-07-25，对照 test 分支两天 517 commits 的实际状态）**：
> Runtime V2 + TEE Redis 落地带来四个调整——① V2 成为新的最大加密面（
> `serve_worker.py` 15+ 调用点、迁移 0043 表级 CHECK 强制 K_enclave），新增
> 全局约束「冻结加密面扩张」与 Phase 0 协调任务；② RDS 表 29→55（19 张
> `v2_*` + V2 队列/归档表），TEE 全未镜像，Phase 1 新增 v2 表迁移策略任务；
> ③ V1 supervisor 已在退役（`agent_runtime/supervisor.py` 已删、TEE 镜像已
> 撤 V1 表），原 Phase 2 清单中对应调用点作废；④ 各改动点清单降级为
> 「快照」——两天 5.5 万行的变更速度下，**每个细案开工时必须以 grep 重扫
> 现场为准**，不得信任本文冻结的行号。
>
> **v3.1 复审（2026-07-27）**：⑤ TEE 同步是三层白名单手工登记制且
> alembic_tee 无 CI 落地通道（0002 至今未在实库执行）→ 新增 Task 0.6
> （先还账再建通道）；⑥ 冻结约束从人工周扫升级为 CI 守卫测试（Task 0.5）；
> ⑦ Phase 1 出口 gate 明确 verify 表范围必须随新增表扩展。

**Goal:** 把 TEE 明文库扶正为唯一主库、移除 RDS 与 enclave；内容默认明文直写
直读，保留客户端信封加密作为按用户开关的可选行为（服务端对加密内容只存不读）。

**Architecture:** 信封从双收件人退化为单收件人（**v2 信封 = v1 去掉
`K_enclave`**）：明文用户走 plaintext doc，加密用户走 K_user-only 信封，服务端
按行自识别格式路由，对信封永远只做校验与搬运、零解密代码。enclave 只需活到
存量服务端可解密文（BYOK 凭证、R2 storage-key/K_enclave 对象）重写完成，之后
与链上信任组件一起退役。客户端加解密路径（`ContentEncryption`/`ContentKeyStore`）
保留，仅摘除 K_enclave 收件人与 rewrap 自愈。

**Tech Stack:** Python/FastAPI backend、psycopg3、Phala dstack CVM（pg 17.10）、
WAL-G→R2、iOS Swift（CryptoKit X25519/ChaChaPoly）、alembic（cutover 后
alembic_tee 升格为唯一迁移链）。

## Global Constraints

- **enclave 死于 Phase 1 之后、Phase 5 之内**：`/v1/envelope/decrypt` 与 KMS
  storage key 是存量（BYOK/R2）解密唯一通道，Phase 1 完成前不得下线 enclave
  任何解密端点、不得重建主 CVM（翻钥即永久丢失）。
- **服务端对加密内容永远只存不读**：任何新代码不得引入服务端解开 K_user 的
  能力；「可选加密」的承诺就是服务端读不了。
- **冻结加密面扩张（v3 新增，立即生效）**：本计划执行期间，新增代码不得再
  引入「强制信封」的 schema 约束（如 0043 的 `ck_v2_trajectory_envelope` 要求
  K_enclave）或新的 enclave 解密依赖；V2 等在途特性新增存储一律做成「格式
  自识别」（明文/信封双形状），否则 Phase 2 永远在追移动靶。此约束需要与
  V2 开发线明确对齐，是 Phase 0 的协调任务（Task 0.5）。
- **v2 信封 = v1 信封去掉 `K_enclave` 字段**；读侧对存量 v1 信封（带
  K_enclave）原样兼容——K_enclave 从此只是无人能用的死字段，不迁移不清洗。
- **fail-open 不变**：Phase 4 cutover 前对 TEE 库的写入沿用 mirror fail-open
  语义，TEE 故障不得传染主路径。
- **每个 Phase 出口必须过自己的 gate 才能进下一 Phase**；回滚手段在各 Phase 注明。
- **用户全局规则**：绝不主动 git commit/add；改动留工作区由用户提交。
- 公开文档同步义务（CLAUDE.md）：改 API 契约/架构/信任边界的 Phase 必须同
  commit 更新 `docs-site/content/docs/` 与 io-onboarding 三件套。
- 测试基线：起本地 PG 55432 后 `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
  ≈ 2440 passed / 7 pre-existing 红；没起 PG 会静默少跑 ~2000 用例。

## 已确认的事实基线（2026-07-23 实测 + 2026-07-25/26 v3 复核）

⚠️ 行号/清单是快照，细案开工时以 grep 重扫为准。

- prod：users=656、chat≈55k、memory≈12k、db_size≈1.3GB；TEE pg CVM 2vCPU/4GB/
  100GB、max_connections=400（已生效）；test 同规格 400/30GB。
- prod RDS：db.t3.medium 4GB/100GB gp3/**单 AZ 无热备**/PG 17.9；30 天连接峰值
  119；TEE 侧 pg 17.10。
- **（v3，07-25 四库实连核对）test RDS=55 张基表、prod RDS=60 张**（多出的
  5 张是 `bak_20260710_usr450_*`/`usr5d4a_*` 事故期手工备份表，处置进
  Phase 4）；TEE 影子库 test/prod 均 20 张。**RDS→TEE 未镜像共 39 张**：
  19 张 `v2_*`、`agent_jobs`/`agent_action_queue`/`agent_status_events`/
  `runtime_state`（V2 队列与运行时）、`chat_message_archive`（**归档聊天，
  用户数据必迁**）、`chat_r2_cleanup`/`chat_r2_lifecycle`（R2 GC 簿记）、
  `model_api_credentials`/`model_api_routes`、`notify_relay_configs`/`_logs`、
  `dau/growth/retention` 三张、`genesis_import_chunks`、`tee_reconcile_*` ×2、
  `tee_sync_runs`、`alembic_version`、`frame_envelopes`（TEE 侧对应新形状
  `frames`）。alembic 主链到 0057；alembic_tee 到 0003（0002 撤 V1 supervisor
  两表镜像，**尚未在 test TEE 生效**——实库仍有这两张）。
- **（v3）Runtime V2 是当前最大的新增加密面**：`model_api_runtime/v2/
  serve_worker.py` 15+ 个信封读写/enclave 解密调用点（含
  `purpose="v2_caption_read"` 等 runtime_token 形态）；迁移
  `0043_v2_encrypted_trajectories.py` 用表级 CHECK **强制** `payload_envelope`
  带 K_enclave（`v2_trajectory_events`/`v2_conversation_summary`/
  `v2_workspace_entries` 同模式）。V2 信封是**服务端自封**（运行时持有明文再
  封存）——按本计划「服务端自产内容一律明文」原则，这些列在 Phase 2 转明文，
  CHECK 约束随之改写。
- **（v3）V1 supervisor 在退役中**：`agent_runtime/supervisor.py` 已删除
  （原引用 supervisor.py:527 作废），RDS 的 `agent_runtime_instances`/
  `agent_runtime_supervisor_heartbeats` 表尚存（灰度残留）；V1↔V2 以
  allowlist 灰度共存。Phase 2 细案按当时的 V1 退役进度决定 V1 调用点是
  「改造」还是「随 V1 一起删」。
- **（v3）新增 TEE Redis CVM**（`backend/redis_pool.py` + `docs/REDIS_USAGE.md`，
  cache/lock/rate-limit/queue，key 前缀 `IO:`）：内容易失可重建，**与切库
  正交**，不进迁移范围；但进 Phase 4 的存储清单核对（确认无持久业务状态）。
- 影子库已镜表列结构与 RDS 逐列对齐；`chat_messages.seq` 的 cutover
  setval 逻辑已内建（`tee_replicator/worker.py:765-777`）。
- **（v3，07-26 确认）TEE 同步是三层白名单手工登记制，且迁移落地是人工的**：
  ① DDL 靠手写 alembic_tee revision（与 RDS alembic 链零派生关系）；② 数据
  流靠 db.py 写点显式加 `mirror.execute` / replicator 表清单 / reconciler
  `TABLES` 白名单，三处都没有 v2；③ **alembic_tee 无任何 CI 钩子**，靠人工
  `python -m backend.alembic_tee`（`TEE_MIGRATION_DATABASE_URL` owner 角色）
  执行——已合并的 0002 至今未在 test/prod 实库执行。V2 新表「没同步」是这套
  机制的必然结果，不是故障。
- 存量密文分层：① shared 带 K_enclave → **已被 replicator 解进 TEE**（DB 侧
  无需再动），其 R2 重体需重写（见 Phase 1）；② local_only / 无 K_enclave →
  **原地保留为加密，不动**；③ `tee_pending_device_migration` 里
  `decrypt_failed:` 行（prod ~790）→ 设备也解不开，确定丢失（D1）。
- R2 四桶：frames（E2E 密文 + frames-tee/ storage-key 密文）、chat-files（E2E
  密文）、io-user-logs（明文，不动）、WAL-G 备份桶（libsodium，保留）。
- enclave 与账号体系零耦合（注册/api_key/whoami/runtime-token 全在 backend）；
  唯二要搬的计算：帧 VLM caption（`enclave/routes/frames.py:165`）、memory
  index 筛选（`enclave/routes/memory.py:37`）——**只对明文内容服务**。
- iOS 设备内容钥 = 账号所有权凭证（register 公钥 + recover 解挑战），**钥保留**
  （既是认证凭证也是可选加密的内容钥）；BoxSeal 挑战信封由 backend 用公钥封，
  不依赖 enclave（`accounts_core.py:218` 保留）。
- 已知慢性病：`tee_sync_runs.reconcile_ok` 全 false（07-25 复核 test/prod 均
  未修，Phase 0 必修）。

## 产品级后果（写进 D2 告知口径，工程无法绕开）

**开了加密开关的用户 = 存储级服务**：聊天正文/记忆/帧对服务端不可见 ⇒ hosted
agent 回合、记忆蒸馏、proactive 感知、genesis 导入、VLM caption 对该用户全部
不可用或退化（BYOK provider key 除外——它是服务端运行时必须读的，不随开关加密，
见 Phase 2 Task 2.3）。开关 UI 文案必须如实陈述这一点。

---

## Phase 0 — 决策拍板 + 侦察修病 + 备份演练（不写业务代码）

**入口 gate：** 无。
**出口 gate：** 三个决策有书面结论；`reconcile_ok=true` 连续 3 个 tick；restore
演练成功且 RTO 落账；尾账表（附录 A）填上真实数字。

### Task 0.1: 三个决策（需要用户拍板，agent 只准备材料）

- [ ] **D1 丢弃面**：确认 `decrypt_failed:` 隔离行（~790）永久丢失可接受。
- [ ] **D2 信任承诺与告知**：docs-site 新叙事口径（「默认明文 + 可选客户端加密
      + TEE 磁盘加密的托管库」替代「E2E+attested decrypt」）；加密开关的功能
      降级矩阵文案；对现有用户的告知方式与时点。
- [ ] **D3 账号恢复机制**：确认保留现有钥恢复（推荐，改动为零：backend 封挑战
      不依赖 enclave）。
- [ ] （v1 的 D4 私钥留存已随私钥收集一并删除。）

### Task 0.5: 与 V2 开发线对齐「冻结加密面扩张」（v3 新增，优先级最高）

- [ ] 与 V2 负责人确认：后续 V2 新表/新列不再加「强制信封」约束，存储形状
      做成明文/信封自识别；已有的 `ck_v2_trajectory_envelope` 类约束列入
      Phase 2 改写清单而不是继续复制该模式。
- [ ] **把冻结约束做成 CI 守卫测试（比人工周扫强一档）**：照
      `test_no_flask_anywhere` 的既有模式写一个
      `tests/test_encryption_surface_frozen.py`——grep 源码里
      `_decrypt_envelope_via_enclave` / `_build_shared_envelope_for_store` /
      `K_enclave` 的出现位置，与仓库里登记的 allowlist 文件比对，新增调用点
      直接红——想加就必须显式登记，登记即触发归类（Phase 2 待改 / 违反冻结
      需回退）。人工「加密面增量对照」降级为每次 V2 大合并后的抽查。

### Task 0.2: 修 reconcile_ok 慢性 false

- [ ] 取证：`select * from tee_sync_runs order by ran_at desc limit 5`（RDS），
      对照 backend 日志 `tee_sync_scheduler` 的 reconcile 段报错；疑点方向：
      单表独占窗口饥饿（memory `tee-replicate-poison-row-headofline-quarantine`）
      或 reconcile 在 tick 预算内跑不完只标 false。
- [ ] 根因修复（另立细案），验收：连续 3 个 tick `reconcile_ok=t` 且
      `unconverged_tables` 为空。

### Task 0.3: WAL-G restore 演练（扶正的硬前置）

- [ ] test 环境按 `deploy/postgres/restore.sh` 从 R2 全量恢复到一次性容器，
      对比行数与 `alembic_tee_version`。
- [ ] 记录 RTO；写进 `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` §4。
- [ ] prod 备份链健康核查：`wal-g backup-list` 最新 base backup < 24h。

### Task 0.6: TEE 迁移落地机制（v3.1 新增，07-26 发现的机制缺口）

- [x] 先还账：把已合并未执行的 alembic_tee 0002/0003 应用到 test 与 prod TEE
      实库（撤 V1 supervisor 两表镜像），核对 `alembic_tee_version`。—— 由
      `2026-07-27-tee-full-table-alignment` 完成：0004 落地时一并把两个实库
      从 0001 推到 0004 head（各 54 张表），`alembic_tee_version` 已核对。
- [x] 再建通道：alembic_tee 目前**无 CI 钩子、纯人工执行**——Phase 1 会新增
      多个 revision，必须先定落地机制（二选一）：接一个手动触发的 GitHub
      workflow（仿 pg-deploy，带 typo guard），或写成 runbook 固定步骤进
      `TEE_POSTGRES_SHADOW_PROVISIONING.md`。没有这个通道，Phase 1 的建表
      任务全是空转。—— 由 `2026-07-27-tee-full-table-alignment` 完成：新增
      `.github/workflows/tee-migrate.yml`（手动触发、typo guard、owner 角色
      direct-TLS、落地后强制断言 `alembic_tee_version == 代码 head`），用法
      见 `TEE_POSTGRES_SHADOW_PROVISIONING.md` §"迁移落地通道"。

### Task 0.4: 尾账量化（只读查询，填附录 A）

- [ ] TEE：`select table_name, count(*) from tee_pending_device_migration group by 1`
      按 reason 前缀分列。
- [ ] R2：frames-tee/ 对象数（storage-key 密文，Phase 1 要重写的工作量）；
      chat R2 对象里带 K_enclave 的行数（从 RDS doc 统计）。
- [ ] 存量 local_only 行数（各表 `doc->>'visibility'='local_only'`）——只为
      D2 告知口径提供数字，**不迁移**。

---

## Phase 1 — 存量服务端解密重写（enclave 在世期间全部做完）

**入口 gate：** Phase 0 出口 gate 全过。
**出口 gate：** R2 无 storage-key 密文对象残留；带 K_enclave 的 R2 聊天重体全部
重写为明文；BYOK 凭证明文列迁移完成且新旧对账一致；`verify` 全绿
（`rds == tee + pending`，requeue_backlog=0），**且 verify 的表范围已扩到
Task 1.1/1.4/1.5 新增的全部必迁表**（verify 只查白名单，不扩范围的全绿是
假象）。

### Task 1.1: BYOK 凭证明文化迁移

- [ ] alembic_tee 新 revision：`model_api_credentials`（列同 RDS，
      `api_key_envelope JSONB` → `api_key TEXT` 明文列）+ `model_api_routes`
      （纯照抄）。
- [ ] 一次性迁移工具（`tools/`，dry-run 优先）：逐行经
      `_decrypt_envelope_via_enclave(purpose="model_api_provider_key")` 解出
      明文 → 写 TEE；失败行落清单人工跟进。
- [ ] 读侧改造在 Phase 2（此处只迁数据，RDS 侧照旧供线上使用）。

### Task 1.2: R2 storage-key 密文重写（frames-tee/）

- [ ] 工具遍历 `frames-tee/<user_id>/<frame_id>` → enclave storage key `open_`
      解密 → 明文写回（同 key 或新前缀，细案定）→ TEE `frames` 表指针列同步。
- [ ] 断点续跑（R2 list 游标 + 已处理标记）；限速防打爆 enclave（502 前科）。
- [ ] **范围排除**：`frames/` 前缀的 E2E `body_ct`（K_user only）不动——那是
      local_only 存量，原地保留为加密。

### Task 1.3: R2 聊天重体重写（带 K_enclave 的）

- [ ] 从 RDS `chat_messages.doc` 找出 `body_key` 指向 R2 且信封带 `K_enclave`
      的行 → 经 `/v1/envelope/decrypt` 解密 → R2 明文重写 + TEE 行指针核对
      （TEE 明文行 replicator 已产出，此处只补 R2 对象）。
- [ ] 无 K_enclave 的对象跳过（= 加密存量，保留）。

### Task 1.4: 分析表与杂项

- [ ] `dau/growth/retention` 三张快照表建到 TEE + 一次性 INSERT SELECT 搬数。
- [ ] 确认 `genesis_import_chunks` 无在途 job 后弃。

### Task 1.5: v2_* 表 + V2 队列/归档表的镜像/迁移策略（v3 新增）

> **由 `2026-07-27-tee-full-table-alignment` 完成**：39 张未镜像表已全部登记进
> `backend/tee_shadow/table_registry.py`（新增的单一真源），分入 MIRROR/
> CIPHERTEXT/SNAPSHOT 三条 lane（`v2_*` 队列/运行时状态大多落 SNAPSHOT 整表
> 快照替换；`chat_message_archive`/`model_api_credentials`/`v2_conversation_
> summary(+_segments)`/`v2_trajectory_events`/`v2_trajectory_reviews`/
> `v2_workspace_entries` 落 CIPHERTEXT），alembic_tee 0004 已建表、`verify`
> 覆盖范围已随之扩到全部 51 张非 SKIP 表。以下按持久性分类的初判清单保留作为
> 历史决策记录，实际 lane 归类以 `table_registry.py` 为准。

- [ ] 按持久性给 39 张未镜像表中的 V2/运行时部分分类（细案逐表定，初判）：
      **新发现必迁**——`chat_message_archive`（归档聊天，用户数据）；
      **随 V2 队列语义定**——`agent_action_queue`/`agent_status_events`/
      `runtime_state`（排空或搬未完成项）；**GC 簿记**——`chat_r2_cleanup`/
      `chat_r2_lifecycle`（在途清理排空后重建）；其余：
      **用户数据必迁**——`v2_trajectory_streams/events/reviews/access_audit`、
      `v2_conversation_summary(+_segments)`、`v2_workspace_entries`、
      `v2_capture_batches`；**运行时状态可弃/可重建**——`v2_worker_heartbeats`、
      `v2_runtime_control/state`、`v2_turn_metrics`、`v2_sandbox_usage_events`、
      各 outbox（排空后弃）；**配置小表直接搬**——`v2_user_allowlist`、
      `v2_wake_schedule`；`agent_jobs` 队列在冻结窗口排空后只搬未完成 job。
- [ ] 「必迁」类补进 alembic_tee（明文形状：envelope 列建为明文/信封自识别
      JSONB，**不带** K_enclave CHECK）+ 接入 mirror 双写或 reconciler。
- [ ] 此任务与 Phase 2 的 V2 明文化（Task 2.2/2.3）联动：先定形状再迁数，
      避免二次迁移。

---

## Phase 2 — 服务端双格式常态化（明文默认 + 信封透传）

**入口 gate：** Phase 1 Task 1.1 完成（BYOK 明文列就绪）；1.2/1.3 可并行推进。
**出口 gate：** test 环境四象限回归（明文用户/加密用户 × 新 App/旧 App）全绿；
L1 测试基线全绿；加密开关端到端（改偏好 → 新写走信封 → 服务端透传 → 客户端
可读）跑通。

> 本 Phase 改动点最多，**必须另立细案**；以下锁定改动清单与设计决定。

### Task 2.1: 用户级加密开关偏好

- [ ] 新偏好 `content_encryption`（`on|off`，默认 `off`），照抄
      `archive_language`/`timezone` 一等偏好模式：preferences 写、whoami 读
      （memory `timezone-first-class-shipped-not-committed` 的既有套路）。
- [ ] 开关语义：**只影响新写入**；存量不批量转换（历史行混格式是常态，读侧
      按行自识别）。whoami 不再下发 `enclave_content_public_key_hex`。

### Task 2.2: 写闸改格式路由（各 *_core.py）

- [ ] `chat/chat_core.py:51,488-495,607-632`（含 v3 新增的 `caption_envelope`
      字段，commit 08d5b122 的公开契约）、`memory/memory_core.py:275`、
      `identity/identity_core.py:86-111,192`、`worldbook/worldbook_core.py:42`、
      `genesis/genesis_core.py:71-91`：接受两种 body 形状——明文
      `{body: ...}` 与 v2 信封 `{body_ct, nonce, K_user, ...}`；按行存储原样
      形状，不做服务端转换。校验规则：v2 信封不得带 K_enclave（新写）；
      形状与用户偏好不匹配只记指标不拒绝（容忍开关切换窗口）。
- [ ] 服务端封装点（`_build_shared_envelope_for_store` 调用：chat/service.py、
      memory/actions.py、hosted/setup_core.py 等，07-23 快照 14 处，细案重扫）
      改明文直写——**服务端自产内容（agent 回复、蒸馏产物、V2 轨迹/摘要/
      工作区）一律明文**；加密用户本就不产生这些（功能降级矩阵）。
- [ ] **（v3）V2 存储明文化**：`0043_v2_encrypted_trajectories` 等迁移的
      `payload_envelope`/`summary_envelope`/`content_envelope` 列转明文形状，
      删除/改写 `ck_v2_trajectory_envelope` 类 K_enclave 强制约束（新迁移）；
      `serve_worker.py` 的信封封装/解封调用点（07-25 快照 15+ 处）改直读直写。
      与 Task 1.5 的表形状决定联动。

### Task 2.3: 读侧去 enclave

- [ ] `core/enclave.py` 的全部调用点改为：明文行直读；信封行按调用方语义
      跳过/返回不可读标记（memory/actions.py:116、hosted/config_store.py:315、
      genesis/plaintext.py:578、**model_api_runtime/v2/serve_worker.py 全部
      解密点** 等；~~agent_runtime/supervisor.py:527~~ 已随 V1 supervisor 删除
      作废——细案开工时 grep 重扫为准，V1 残余调用点按当时退役进度决定改造
      还是随 V1 删）。BYOK 读侧改读 TEE 明文列（Task 1.1 产出）。
- [ ] 两处计算搬家：VLM caption 搬 backend（只对明文帧）、memory index 筛选搬
      backend（只对明文 moment）。
- [ ] `content/content_core.py`：rewrap 端点删除；swap 保留但改为「客户端提交
      新形状（明文↔信封），服务端原地替换」——这就是开关切换后逐条转换的通道。
- [ ] `tee_replicator/worker.py` 增量适配：新的明文行走 mirror 双写即可，
      replicator 只剩消化存量密文表的职责，Phase 4 cutover 后整体退役。

---

## Phase 3 — iOS：明文默认 + 开关 UI

**入口 gate：** Phase 2 部署 test 且双格式验证通过。
**出口 gate：** 发版；强制升级窗口启动（服务端对低版本返回升级提示）；信封
新写流量中 v1（带 K_enclave）占比降到 0。

- [ ] 写侧：默认明文直传；`content_encryption=on` 时走 v2 信封（复用
      `ContentEncryption.envelope` 去掉 enclave 收件人分支，
      `ContentEncryption.swift:52` 的 K_enclave 封装删除）。
- [ ] 读侧：按行自识别——有 `body_ct` 走 `unseal`（保留），有 `body` 直读。
      存量 local_only 与加密用户历史照常可读。
- [ ] 删：rewrap 自愈全套（`FeedlingAPI.swift:1572-1636` 与四处触发）、
      `refreshEnclaveAttestation` 的 enclave_content_pk 拉取（:4713）、
      `FrameEnvelope.swift` 的 K_enclave 分支（帧跟随开关：明文用户明文上帧）。
- [ ] 保留：`ContentKeyStore` 全部（认证 + 可选加密内容钥）、register/recover
      流程原样、Audit 卡按 D2 口径改造或移除。
- [ ] 新增：设置页加密开关 UI + 功能降级告知文案（D2 产出）；写 whoami
      偏好接口。
- [ ] pbxproj 手工登记新文件（历史坑）。

---

## Phase 4 — Cutover 切库（维护窗口执行）

**入口 gate：** Phase 1 出口 gate 全过；Phase 3 强制升级窗口结束（v1 信封写入
流量 = 0，以 Task 2.2 的格式指标为准）；restore 演练复跑过一次（<30 天）。
**出口 gate：** 全站在 TEE 主库上 5xx=0 运行 48h；RDS 转只读观察。

- [ ] 预热：`python -m backend.tee_shadow reconcile` 全量收敛 + `verify` 全绿 +
      `requeue_backlog=0` + 三张 seq 表 setval 核对。
- [ ] 冻结窗口（分钟级）：停写 → 最后一轮增量 replicate/reconcile → verify →
      切 `DATABASE_URL` 指向 TEE pg（backend、runner、consumer 全部）→ 停
      `FEEDLING_TEE_DUAL_WRITE`、停 scheduler/replicator → 解冻。
- [ ] 存量密文行随库带走：RDS 密文内容表的 local_only/加密行在 TEE 侧本无明文
      镜像——cutover 前最后一轮 reconcile 必须把这些行**原样信封搬进 TEE**
      （细案确认 reconciler 对密文表的搬运语义；如无则 cutover 细案补一个
      「密文行原样搬运」步骤，这是 v2 新增的关键差异点）。
- [ ] **（v3）存储清单核对扩展**：Redis CVM（`IO:` 前缀 cache/lock/rl/queue）
      确认无持久业务状态、切库无需处理；`agent_jobs`/`agent_action_queue`
      队列冻结窗口排空；各 `v2_*_outbox` 排空；`v2_*` 必迁表已按 Task 1.5
      收敛（verify 覆盖）；prod 的 5 张 `bak_20260710_*` 事故备份表在切库前
      决定去留（建议 pg_dump 归档后弃，不迁）。
- [ ] 连接容量已备好（max_connections=400 已生效）；cutover 后连续 3 天盯
      `pg_stat_activity` 峰值与 CVM `free -m`（available<1000MB 告警线）。
- [ ] 回滚预案：观察期内 RDS 保持只读在线，任何 P0 → 切回 RDS DSN。
- [ ] 观察 7-14 天后 RDS 快照留档 → 停实例。

---

## Phase 5 — 退役与文档

**入口 gate：** Phase 4 观察期结束。
**出口 gate：** enclave 容器/链上组件下线；docs 重写并 build 通过；RDS 终删。

- [ ] enclave：全部路由下线 → 容器移出 compose → `backend/enclave/` 删除
      （`/attestation` 按 D2 去留；BoxSeal 挑战封装逻辑先抽到 backend 侧——
      recover 依赖它，不随 enclave 死）。
- [ ] 链上：AppAuth 停止 addComposeHash、CI publish-compose-hash 步骤移除；
      KMS 钥随 enclave 死（storage key 死前确认 Task 1.2 无残留）。
- [ ] 代码清理：`core/enclave.py`、`tee_shadow/`、`tee_replicator/`、
      `alembic/`（RDS 链）删除；`alembic_tee` 升格唯一迁移链。
      **保留** `content_encryption.py` 的信封校验/BoxSeal（写闸校验 + recover
      挑战用），删除其中服务端解密相关部分。
- [ ] 文档：docs-site architecture/self-hosting/api-keys/index 按「默认明文 +
      可选客户端加密」重写 + OpenAPI `npm run openapi:generate` + changelog；
      io-onboarding 三件套；`deploy/DEPLOYMENTS.md`、
      `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` 改写为主库运维手册。
- [ ] 收尾运维：TEE 主库监控告警（连接数、available 内存、WAL-G 新鲜度）；
      restore 演练排成季度例行。

---

## 风险登记簿

| 风险 | 等级 | 缓解 |
|---|---|---|
| 单实例 TEE pg 成唯一主库后 CVM 硬故障（RCU stall 前科） | 高 | Phase 0 restore 演练 + RTO 落账；观察期 RDS 只读兜底；季度演练 |
| enclave 过早失能（误下线/主 CVM 重建翻钥）致 Phase 1 存量解不开 | 高 | Global Constraint 第一条；Phase 1 完成前禁碰 enclave/主 CVM |
| 混格式行（明文/v1 信封/v2 信封共存）读侧漏判 | 中 | Task 2.2 格式指标 + 四象限回归；读侧统一走单一格式判别函数 |
| 加密用户功能降级沟通不足引发投诉 | 中 | D2 降级矩阵进开关 UI 文案；默认 off 限制影响面 |
| cutover 时密文行未随库搬运造成加密存量丢失 | 高 | Phase 4 专项步骤：密文行原样信封搬进 TEE，verify 覆盖 |
| reconcile 慢性病掩盖真实不收敛 | 中 | Phase 0 Task 0.2 前置修复，verify 才可信 |
| **加密面是移动靶**（V2 两天新增 15+ 调用点 + 表级 K_enclave 约束） | 高 | 全局约束「冻结加密面扩张」+ Task 0.5 与 V2 线对齐 + 每次大合并后 grep 重扫 |
| v2_* 用户数据表（轨迹/摘要/工作区）漏迁 | 高 | Task 1.5 逐表分类 + Phase 4 verify 覆盖 v2 必迁表 |
| 迁移工具打爆 enclave（502 前科） | 中 | Task 1.2/1.3 限速 + 断点续跑 |
| alembic_tee 无落地通道，Phase 1 建表任务空转（0002 未执行的前科） | 中 | Task 0.6 先还账再建通道（workflow 或 runbook 固定步骤） |
| verify 白名单不扩范围 → 新增表「全绿假象」 | 中 | Phase 1 出口 gate 明确 verify 范围扩展要求 |

## 附录 A：尾账表模板（Phase 0 Task 0.4 填数）

| 数据域 | 服务端可解（Phase 1 重写） | 保留为加密（不动） | 确定丢失 |
|---|---|---|---|
| frames-tee/ R2 对象 | | — | |
| chat R2 对象（带 K_enclave） | | | |
| chat/memory/frames/perception local_only DB 行 | — | | |
| quarantine（decrypt_failed:） | — | — | ~790 |
