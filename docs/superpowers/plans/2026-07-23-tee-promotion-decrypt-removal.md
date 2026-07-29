# TEE 库扶正 + 加密改可选（默认明文，enclave 保留）— 主实施计划（v6）

> **For agentic workers:** 这是**主计划（program plan）**。每个 Phase 开工时用
> superpowers:writing-plans 基于本计划的该节另立带完整代码的细案（子计划），再用
> superpowers:subagent-driven-development 或 executing-plans 执行。本文锁定阶段
> 边界、门禁（gate）、顺序依赖和验收标准。步骤用 `- [ ]` 跟踪。
>
> **修订史（浓缩）：**
> - v2–v5（07-23~28）：一路把方案从「全部去加密 + enclave 退役」逐步收窄，
>   曾走过「收私钥解 local_only」等弯路，后经尾账实测（local_only 仅 3 用户
>   7 条）删除私钥方案。
> - **v6（07-28，方向性澄清）**：**加密不是"纯存储降级"，而是保留现有 enclave
>   加密路线作为可选项**。加密开关 on = 双收件人信封（K_user + K_enclave，
>   enclave 在 TEE 内可解）→ **加密用户功能不降级**（agent/记忆/proactive 照常，
>   靠 enclave 解密投影）。默认 off = 明文直读。**enclave 与 TEE 信任链保留**，
>   从"所有人的必经解密路径"降为"加密用户的专用路径"。本质是「**加密从强制
>   改可选、默认明文**」+「TEE 库扶正、移除 RDS」两件正交的事——不再是"去
>   加密"。现有用户 cutover 默认迁明文（enclave 解存量），加密 opt-in。

**Goal:** 把 TEE 明文库扶正为唯一主库、移除 RDS；内容加密从**强制**改为**按用户
全局开关可选**——默认明文直写直读（服务端可读、agent 全功能、不经 enclave），
开关打开则走现有 enclave 双收件人加密路线（DB/R2 密文、TEE 内解密、功能不降级）；
enclave 与链上信任链**保留**，专服务加密用户；取消 local_only 可见性。

**Architecture:** 加密维度从「强制 shared（人人 K_enclave）+ 罕用 local_only」
简化为「per-user 全局开关」两态：

| 档位 | 存储形态 | 服务端可读性 | 功能 | 路径 |
|---|---|---|---|---|
| **明文（默认）** | doc 明文 | 直读 | 全 | 不经 enclave（快） |
| **加密（opt-in）** | 双收件人信封 K_user+K_enclave | 仅 TEE enclave 解 | 全（enclave 投影） | 经 enclave（现状路径） |

加密档的隐私保证 = 产品现有的 attested-decrypt 模型（内容钥在被 attest 的 TEE
飞地内，DB/运维层看不到明文，但飞地能解来服务 agent）——**docs 的 E2E/attested
叙事对加密档保留**，只新增「默认明文」这一档。信封**仍是双收件人**（不去
K_enclave）。local_only（K_user-only、谁都读不到）这一 per-content 维度取消。
TEE 库扶正与加密可选化是两件正交的事：库照扶、RDS 照移除，enclave 不退役。
客户端加解密路径（`ContentEncryption`/`ContentKeyStore`）**保留服务加密用户**。

**Tech Stack:** Python/FastAPI backend、psycopg3、Phala dstack CVM（pg 17.10）、
enclave（TDX + KMS + attestation，保留）、WAL-G→R2、iOS Swift（CryptoKit
X25519/ChaChaPoly）、alembic（cutover 后 alembic_tee 升格为唯一迁移链）。

## Global Constraints

- **enclave 永久保留（v6）**：服务加密档用户。KMS 内容钥、storage key、
  attestation、链上 AppAuth、iOS pinning 全部保留。Phase 1 也依赖 enclave 解
  现有 shared 存量为明文——**Phase 1 完成前不得重建主 CVM**（翻钥即丢现有
  加密存量的可解性）。
- **服务端不持有用户私钥**：enclave 持有的是 KMS 派生的 K_enclave 内容钥（信封
  第二收件人），**不是**用户设备的 K_user 私钥。加密档的信任对象是「TEE 硬件 +
  attestation」，与现状 shared 模型一致；不建任何用户私钥收集通道。
- **默认明文、加密可选（v6）**：新写按用户 `content_encryption` 偏好——off 写
  明文、on 写双收件人信封。现有用户 cutover 默认迁明文（存量 enclave 解密）。
- **去 local_only（v6）**：写侧不再接受/产生 `visibility=local_only`；存量
  3 用户 7 条由 iOS swap 自解为明文或入丢弃。
- **冻结"强制加密"扩张（v6 调整）**：在途特性（尤其 V2）不得再引入「表级强制
  信封」约束（如 0043 的 `ck_v2_trajectory_envelope` 要求所有行带 K_enclave）
  ——存储必须按用户偏好支持明文/信封两形状，否则明文档用户的该类数据无法
  直读。做成 CI 守卫（Task 0.5）。
- **fail-open 不变**：Phase 4 cutover 前对 TEE 库的写入沿用 mirror fail-open
  语义，TEE 故障不得传染主路径。
- **每个 Phase 出口必须过自己的 gate 才能进下一 Phase**；回滚手段在各 Phase 注明。
- **用户全局规则**：绝不主动 git commit/add；改动留工作区由用户提交。
- 公开文档同步义务（CLAUDE.md）：改 API 契约/架构/信任边界的 Phase 必须同
  commit 更新 `docs-site/content/docs/` 与 io-onboarding 三件套。
- 测试基线：起本地 PG 55432 后 `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
  ≈ 2440 passed / 7 pre-existing 红；没起 PG 会静默少跑 ~2000 用例。

## 已确认的事实基线（07-23 实测 + 07-25/26/27/28 复核）

⚠️ 行号/清单是快照，细案开工时以 grep 重扫为准。

- prod：users=656、chat≈55k、memory≈12k、db_size≈1.3GB；TEE pg CVM 2vCPU/4GB/
  100GB、max_connections=400（已生效）；test 同规格 400/30GB。
- prod RDS：db.t3.medium 4GB/100GB gp3/**单 AZ 无热备**/PG 17.9；30 天连接峰值
  119；TEE 侧 pg 17.10。
- **表同步现状**：RDS prod=61 / test=56、TEE 各 20；缺口 45 张、约 3189 行。TEE
  同步是三层白名单手工登记制，alembic_tee 无 CI 钩子（0002/0003 合了从未在
  实库执行）。**这部分由独立工作流承接**（见
  `docs/superpowers/specs/2026-07-27-tee-full-table-alignment-design.md`，权威
  事实源），本文 Task 0.6/1.5 不再重复维护其数字。
- **Runtime V2 强制加密**：`serve_worker.py` 15+ 信封调用点；迁移 0043 用表级
  CHECK 强制 `payload_envelope` 带 K_enclave。v6 下需改为**按偏好**（明文档
  用户 V2 轨迹存明文），CHECK 放宽为「允许明文或信封」。
- **V1 supervisor 在退役中**：`agent_runtime/supervisor.py` 已删除；RDS 两张
  `agent_runtime_*` 表尚存；V1↔V2 allowlist 灰度共存。
- **新增 TEE Redis CVM**（`IO:` 前缀）：易失可重建，与切库正交，仅进 Phase 4
  存储清单核对。
- **存量密文分层（v6）**：① 现有全体 shared（带 K_enclave）→ 默认迁明文，
  Phase 1 由 enclave 解密进 TEE（影子库本来就在做）；② BYOK / R2 storage-key
  → Phase 1 enclave 明文化；③ local_only 孤岛（只 K_user）→ 仅 prod 3 用户
  7 条 chat、test 1 条，其余内容域全 0，iOS swap 自解或丢弃；④ quarantine
  `decrypt_failed:` 797/13 用户 → 确定丢失（D1）。
- R2 四桶：frames、chat-files（密文）、io-user-logs（明文）、WAL-G 备份桶
  （libsodium）。frame_envelopes island=0 → R2 帧 body 全 enclave 可解、无
  K_user-only 孤岛。
- enclave 与账号体系零耦合（注册/api_key/whoami/runtime-token 全在 backend）；
  两处 enclave 内计算（帧 VLM caption `enclave/routes/frames.py:165`、memory
  index `enclave/routes/memory.py:37`）**保留**——它们服务加密用户。
- iOS 设备内容钥 = 账号所有权凭证 + 内容加密钥，私钥始终只在设备；BoxSeal 挑战
  信封由 backend 用公钥封，不依赖 enclave。
- 已知慢性病：`tee_sync_runs.reconcile_ok` 全 false，prod tick 11 分钟、
  `requeue_backlog` 增长（717→776→3028）。Phase 0 Task 0.2 必修，且必须在表
  同步铺开前完成。

## 产品级后果（写进 D2 告知口径）

- **默认（明文）用户**：新数据 + 全部存量（含现有 shared 解密后）明文，服务端
  可读，全功能，读路径不经 enclave（更快）。绝大多数用户。
- **加密（opt-in）用户**：内容 DB/R2 密文、TEE 内解密、**功能不降级**；隐私
  保证 = attested-decrypt（信任 TEE 硬件而非"服务端无钥"）。读经 enclave、
  稍慢。这是现状 shared 模型的延续。
- **D2 冲击面比 v5 小得多**：加密档保留原 E2E/attested 叙事，只需新增「默认
  明文」一档的说明 + 加密开关是"更强隐私、稍慢"而非"功能全废"。

---

## Phase 0 — 决策拍板 + 侦察修病 + 备份演练（不写业务代码）

**入口 gate：** 无。
**出口 gate：** 决策有书面结论；`reconcile_ok=true` 连续 3 个 tick；restore 演练
成功且 RTO 落账；尾账（附录 A）已实测完成。

### Task 0.1: 决策（已基本拍定，剩 D2 文案）

- [x] **D1 丢弃面**：quarantine `decrypt_failed:` 797/13 用户丢弃；3 用户 7 条
      local_only 走 iOS swap 自解、够不着则丢弃。**（07-28 确认）**
- [x] **D2 信任承诺与告知**：docs 新增「默认明文」档说明 + 加密开关定位为
      「更强隐私（TEE 保护）、功能不减、稍慢」；加密档保留现有 attested-decrypt
      叙事；现有用户默认迁明文的告知方式与时点。
      **（2026-07-28 文案初稿 + 告知方案已拍板 A-1）**
      文案见 `docs/superpowers/specs/2026-07-28-D2-trust-disclosure-draft.md`。
      - **关键结论：这不是信任承诺的回退。** 现网公开文档
        `architecture.mdx:153` 原文已写明 "**not a claim that no server-side
        component ever sees plaintext**"，并列举 5 处显式明文边界（Perception
        早有明文分支）。v6 只是把明文边界从「若干例外」扩成「默认档」，
        叙事类型没变、变的是默认值 → D2 冲击面远小于计划原先的假设。
      - **已拍板 A-1（事前告知 + 先行 opt-in，调整 Phase 顺序）**：Phase 3 发
        iOS 开关时先告知，让在意的用户在存量迁明文**之前**就能打开加密档。
        选 A-1 而非 A-2 的理由：方案 A 的全部价值就在「迁移前 opt-in」，A-2
        （保持顺序、事后告知）与该理由自相矛盾；且一旦迁了明文，用户再回加密
        档要重新加密存量，成本高得多。
      - 剩余待定（不阻塞开工）：两档的对外命名、加密档「稍慢」是否给量化数字
        （需 Phase 2 实测）、以及 D1 那 797 条 /13 用户是否单独告知。
- [x] **D3 账号恢复机制**：保留现有钥恢复（改动为零）。**（07-28 确认）**
- [x] **存量默认态**：现有用户 cutover 默认迁明文，加密 opt-in。**（07-28 确认）**

### Task 0.5: 与 V2 开发线对齐 + 冻结「强制加密」CI 守卫

- [ ] 与 V2 负责人确认：V2 新表/新列不再加「表级强制信封」约束，改为按用户
      `content_encryption` 偏好支持明文/信封两形状；已有
      `ck_v2_trajectory_envelope` 类约束列入 Phase 2 改写清单。
- [x] **CI 守卫测试**：照 `test_no_flask_anywhere` 模式写
      `tests/test_encryption_surface_frozen.py`——扫「表级强制 K_enclave 的
      CHECK 约束」与「无偏好分支的强制信封写入点」，新增违规直接红。
      **（2026-07-28 完成，6 tests，端到端验证过「造违规必红、移除即绿」）**
      - CHECK 面：扫 `alembic` **与 `alembic_tee` 两条链**（后者是表同步工作流
        正在建 30+ 张表的地方，最可能引入新约束），识别 4 种等价写法
        `? 'K_enclave'` / `length(->>)` / `IS NOT NULL` / `jsonb_exists`——
        只堵第一种等于没堵，**0043 自己就同时用了前两种**。allowlist = 0043。
      - 写入点面：per-file 计数基线 **44 处 / 19 文件**，只许减不许增，另有防
        基线失效的 stale 检查。⚠️ 计划正文记的「14 处」是 07-23 快照，**三周
        翻了三倍**——这是「加密面仍在扩张」的量化证据，Phase 2 Task 2.2 的改造
        面比原估大得多。

### Task 0.2: 修 verify 毒行卡死（~~⚠️ 表同步前必完成~~ 排序约束已撤销）

> ~~prod 单趟 tick 已 11 分钟、`verify_ran=f`、`requeue_backlog` 增长~~
> ~~（717→776→3028）。表同步工作流会再挂 30+ 张表，本任务必须先完成。~~
>
> **2026-07-28 取证推翻了上述病症描述与两个疑点方向**，细案见
> `docs/superpowers/specs/2026-07-28-tee-sync-verify-poison-row-design.md`。
> 实测 24h / 139 趟 tick：`reconcile_ok` = true 1 次 / false 49 次 / **NULL 89 次**，
> `verify_ran=t` **0 次**，均值 306s（非 11 分钟），`requeue_backlog` 恒 NULL。
> **排序约束方向是反的**：不是 0.2 挡着表同步，而是表同步（Task 0.6）修好了
> 0.2 的一半——故撤销「表同步前必完成」。

- [x] 取证：`tee_sync_runs` + prod backend 日志。**（2026-07-28 完成）**
      两条互相独立的根因，都不是原疑点方向：
      - **根因 1（已自愈，无需修）**：`reconcile 失败: relation
        "notify_relay_configs" does not exist` —— TEE 侧缺表让整趟 reconcile 抛
        异常。表已补齐（TEE prod 现 **54 张表**），`02:51` 那趟
        `reconcile_copied=850445`（85 万行）是真成功，不是 `AlreadyRunning` 假成功。
      - **根因 2（唯一剩余真问题）**：`verify 失败:
        enclave_http_403:{"error":"decrypt_failed: envelope missing body_ct"}`
        —— `tee_shadow/verify.py:306` 只 catch `PendingDeviceMigration`，enclave
        403 直接冒泡冲垮整趟 verify，再被 `tee_sync_scheduler.py:211` 静默吞掉。
        **一条坏行 = verify 永久瘫痪**；与 replicate 侧 2026-07-15 已修的毒行
        队头阻塞是同一模式，只是 verify 这条路径当时没跟着加 quarantine。
- [x] 根因修复（细案见上）。**（2026-07-28 完成，严格 TDD，两轮红→绿）**
      - `tee_shadow/verify.py`：抽样解密 `except` 分两级——`PendingDeviceMigration`
        维持跳过；**其它任何解密异常记为 `field="<decrypt-failed>"` 的 mismatch
        并 continue**。坏行**记成 mismatch 而非跳过**：跳过等于宣称「两库一致」，
        用虚假全绿掩盖真问题，比崩掉更危险。
      - `admin/tee_sync_scheduler.py`：`report.verify` 增 `decrypt_failures`
        计数并进 log——「解不开」与「内容不一致」处置不同，混在一个 mismatches
        总数里等于把告警埋掉（修好 verify 只是把一种静默换成另一种）。
      - 测试：`test_undecryptable_row_is_reported_not_fatal`、
        `test_sync_tick_surfaces_verify_decrypt_failures`，均先验证过 RED
        （分别是 `RuntimeError` 冒泡、`KeyError: 'decrypt_failures'`）。
      - 未加 DB 列：计数走已有的 `report` JSONB，避免与在途分支抢 alembic 版本号。
- [ ] ⚠️ **验收标准需改写**（原标准不可执行）：`FEEDLING_TEE_RECONCILE_INTERVAL_SEC`
      默认 86400s，reconcile 成功后 24h 内 `reconcile_ok` 恒为 NULL——「连续 3 个
      tick `reconcile_ok=t`」要等 3 天，且只在**反复失败重试**时才可能连续出现，
      自相矛盾。改判见细案 §4：连续 3 趟 `did_reconcile=t` 的 tick 全 ok，**并
      新增 verify 侧验收**「`verify_ran=t` 且 `decrypt_failures=0`、
      `unconverged_tables=0`」——后者才是本 Task 真正要保证的东西。

### Task 0.6: TEE 迁移落地机制（**已由独立工作流承接**）

> 本任务与 Task 1.5 由
> `docs/superpowers/specs/2026-07-27-tee-full-table-alignment-design.md`（设计
> 已获批）承接。交接提醒：v6 下加密用户的密文行仍要能进 TEE（双写/复制原样
> 搬运信封），新建表 envelope 列做成明文/信封自识别、**不带强制 K_enclave
> CHECK**；verify 白名单随新表扩展。

**✅ 已完成**（承接方 `2026-07-27-tee-full-table-alignment` 交付，状态自 test 分支
`c756ec2a` 合入 v6，2026-07-29）：

- [x] **先还账**：已合并未执行的 alembic_tee 0002/0003 应用到 test 与 prod 实库
      （撤 V1 supervisor 两表镜像）。0004 落地时一并把两个实库从 0001 推到 0004
      head（**各 54 张表**），`alembic_tee_version` 已核对。
- [x] **再建通道**：新增 `.github/workflows/tee-migrate.yml`（手动触发、typo
      guard、owner 角色 direct-TLS、落地后强制断言 `alembic_tee_version == 代码
      head`），alembic_tee 从此不再是「纯人工执行、无 CI 钩子」。

> 这两条正是 Task 0.2 根因 1（reconcile 撞 TEE 缺表）得以自愈的原因——排序约束
> 「0.2 必须先于表同步」的方向因此被证反，见 Task 0.2。

### Task 0.3: WAL-G restore 演练（扶正的硬前置）

- [x] test 环境按 `deploy/postgres/restore.sh` 从 R2 全量恢复到一次性容器，
      对比行数与 `alembic_tee_version`。**（2026-07-28 完成，演练成功）**
      `alembic_tee_version` 与表数（54）与实库**完全一致**；逐表行数
      **50/54 逐行一致**，有差异的 4 张全是持续写入表（`user_logs`
      16123→16082、`v2_worker_heartbeats` 130→128、
      `agent_runtime_supervisor_heartbeats` 4→3、`chat_r2_lifecycle` 227→226），
      差量与「实库仍在写、恢复库停在 13:34:36」吻合，非数据丢失。全部用户内容表
      （chat_messages/memory_moments/users/frames…）逐行一致。
- [x] 记录 RTO；写进 `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` **§5**（§4 是
      「停用/回滚」，演练记录另起一节更清晰）。**（2026-07-28 完成）**
      **RTO ≈ 7 分钟**（backup-fetch 141s + WAL 回放 240s + 启动/修正 ~30s；
      本机 arm64 跑 amd64 镜像走模拟，属**上限**）；**RPO ≈ 0**（恢复点
      13:34:36，演练 13:25 启动 —— WAL 归档及时）。
- [ ] ⚠️ **演练发现 `restore.sh` 有一处真缺陷，扶正前应修**：线上
      `max_connections=400` 是部署参数注入的、**不在备份的 `postgresql.conf` 里**，
      恢复端默认 100 → 回放直接 `FATAL: recovery aborted because of insufficient
      parameter settings`。演练靠手工追加参数才继续。建议 `restore.sh` 写 recovery
      配置时一并写入 `max_connections`（及同类 `max_worker_processes` /
      `max_prepared_transactions` / `max_locks_per_transaction`，PG 对这些都有
      「≥ primary」硬要求）。另两处非阻塞坑（`pg_ctl` 不在 PATH、恢复实例无
      `postgres` 角色）见 §5.3。
- [x] prod 备份链健康核查：`wal-g backup-list` 最新 base backup < 24h。
      **（2026-07-28 完成）** 最新 `base_00000001000001E3000000D3 @
      2026-07-28T03:00:22Z`，距核查约 8.8h；07-25/26/27/28 每天 03:00 各一条，
      cron 稳定 → 07-24 那次「重部署抄旧 tag 丢 PATH 修复导致 base backup 全断」
      确认已闭环。当前镜像 `35f2a9eb…`，容器 healthy。

### Task 0.4: 尾账量化 ✅（2026-07-28 完成，见附录 A）

- [x] local_only 孤岛：prod 3 用户 7 条 chat、test 1 条，其余内容域全 0。
- [x] quarantine `decrypt_failed:` 797/13 用户；R2 帧无 K_user-only 孤岛。

---

## Phase 1 — 现有 shared 存量迁明文 + BYOK/R2 明文化（enclave 解密）

**入口 gate：** Phase 0 出口 gate 全过。

> ⚠️ **D2 拍板 A-1 后新增的顺序约束（2026-07-28）**
>
> 本 Phase 里**触及用户内容**的存量迁明文（Task 1.2/1.3/1.4 以及 chat/memory
> 等内容域）**不得早于 Phase 3**（iOS 开关发版 + 告知）——A-1 的全部价值就在
> 于让用户**在自己的存量被迁成明文之前**就能选择打开加密档；迁完再告知，用户
> 要回加密档就得重新加密存量，代价高得多。
>
> 直接照搬「Phase 3 先于 Phase 1」会形成循环依赖（Phase 2 入口依赖本 Phase 的
> Task 1.1，Phase 3 又依赖 Phase 2）。按**内容性质**拆解即可解开：
> **Task 1.1（BYOK provider 凭证明文化）不是用户内容、不触发告知义务，维持原位**；
> 其余触及用户内容的子任务后移到 Phase 3 之后。
>
> 实际执行顺序：**1.1 → 2 → 3（发版 + 告知）→ 1.2 / 1.3 / 1.4 → 4 → 5**。
> Phase 4 的入口 gate（「Phase 1 出口 gate 全过」）因此自然满足，无需再改。
**出口 gate：** 现有用户 shared 存量在 TEE 侧为明文；R2 无 storage-key 密文残留、
带 K_enclave 的 R2 重体已明文化；BYOK 凭证明文列迁移完成且对账一致；`verify`
全绿且表范围已扩到全部新增必迁表。

> 说明：TEE 影子库现在做的「shared 经 enclave 解密进 TEE」正是 Phase 1 的主体
> ——现有全体用户默认迁明文，与此一致。opt-in 加密是 Phase 2 上线后用户主动
> 选择、届时新写才是密文。

### Task 1.1: BYOK 凭证明文化迁移

> **2026-07-29 侦察后本 Task 范围已重定**，细案见
> `docs/superpowers/plans/2026-07-29-byok-credential-plaintext-read-routing.md`。
> 实测：RDS(test) 25 行**全密文**、TEE(test) 同表 25 行**全明文**
> （key 集合 `body,id,owner_user_id,visibility`，`body_ct`=0）——**表对齐工作流的
> CIPHERTEXT lane 已在复制时解密**（`tee_replicator/worker.py:627-670`），
> 与 Task 0.2 根因 1 是同一模式：表同步已经替本 Task 做掉了数据侧。
> **真正的缺口在读侧**：`hosted/config_store.py:378` 等 7 处**无条件**打 enclave
> 解密，cutover 后遇到 TEE 的明文行会全线返回 `model_api_key_decrypt_failed`。

- [x] ~~一次性迁移工具（`tools/`，dry-run 优先）~~ **不再需要**：replicator 持续
      在做，且已收敛（TEE 侧 25/25 明文）。原设想的「逐行解密→写 TEE→失败行落清单」
      与现有复制链重复。
- [x] **读侧按形状路由**（本 Task 的实际主体）。**（2026-07-29 完成，严格 TDD，
      细案 3 个 Task 全绿；9 个新测试）**
      - 新增 `core.envelope.decrypt_provider_key_envelope`：`body_ct` 优先走
        enclave、只有 `body` 则**本地直读**（明文分支绝不联网）。
      - **5 处** Python 直调点改调它：`hosted/config_store.py`(1) +
        `hosted/setup_core.py`(4)。⚠️ 细案初稿写的「7 处」与
        `hosted/vision_observer.py` 均来自**主仓** grep；worktree 基线
        `ed6f2053` 无该文件，行号也全不同——合并回 test 时若 `vision_observer.py`
        已存在，那一处需照改（守卫测试会自动抓出来）。
      - **2 处 HTTP 路径**短路：`agent_runtime/supervisor.py::_decrypt_provider_key`
        与 `genesis/worker.py::_provider_key_from_envelope`（**后者是细案漏项**，
        靠全仓重扫 `model_api_provider_key` 发现——它走自己的 `_decrypt_envelope`）。
      - 守卫测试 `test_no_unrouted_provider_key_decrypt_sites_remain`：今后新增
        「无形状路由」的 provider-key 解密点直接红。
      - **过程中发现并修好两处既有问题**：①helper 初版无脑透传空 `runtime_token`，
        破坏了「api-key 调用方入参不变」的既有语义（被
        `test_model_api_profiles_config_store.py` 抓住，已加测试锁死）；
        ②`test_model_api_models_route.py` 的假信封是 `{"ciphertext": "x"}` 这种
        不真实形状，导致 `..._decrypt_failure_is_400` 实际**从未走到 enclave**
        就返回 400（测试空转），已改成真实的 `{"body_ct": "x"}`。
      - `hosted/setup_core.py` 的 `core_enclave` import 替换后成 unused，但模块
        docstring 写明「preserved so tests can monkeypatch」→ **加 noqa 保留不删**
        （memory `autoflake-kills-module-attr-reexports`）。
- [ ] ~~alembic_tee 新 revision（`api_key_envelope JSONB` → `api_key TEXT`）~~
      **推迟到 Phase 5**：TEE 侧现存形状是 `{body,…}` 而非裸文本，读侧路由后已可
      直读；列改名要同时动 replicator upsert、verify 表登记与 alembic_tee，收益仅
      「清爽」，风险却压在 cutover 关键路径上。留到 RDS 退役、不必维护双形状时再做。

### Task 1.2: R2 storage-key 密文重写（frames-tee/）

- [ ] 工具遍历 `frames-tee/<user_id>/<frame_id>` → enclave storage key `open_`
      解密 → 明文写回 → TEE `frames` 表指针列同步。断点续跑 + 限速（502 前科）。

### Task 1.3: R2 聊天重体明文化（带 K_enclave 的）

- [ ] 从 RDS `chat_messages.doc` 找 `body_key` 指 R2 且带 `K_enclave` 的行 →
      `/v1/envelope/decrypt` 解密 → R2 明文重写 + TEE 行指针核对。

### Task 1.4: 分析表与杂项

- [ ] `dau/growth/retention` 建到 TEE + 一次性搬数。确认 `genesis_import_chunks`
      无在途 job 后弃。

### Task 1.5: v2_* 表 + V2 队列/归档表（**已由表同步工作流承接**，见 Task 0.6）

---

## Phase 2 — 明文快路径 + 加密开关（按行格式路由，enclave 保留）

**入口 gate：** Phase 1 Task 1.1 完成；1.2/1.3 可并行。
**出口 gate：** test 四象限回归（明文用户/加密用户 × 新 App/旧 App）全绿；L1
基线全绿；加密开关端到端跑通（开关 on → enclave 加密路径全功能；off → 明文
直读）；写侧确认不再产生 local_only。

> 本 Phase 改动点最多，**必须另立细案**。核心是「按行格式路由」：明文行走新
> 快路径，信封行走**保留的** enclave 路径——不是删 enclave，是给它加一条明文旁路。

### Task 2.1: 用户级加密开关偏好（取代 local_only）

- [x] 新偏好 `content_encryption`（`on|off`，默认 `off`），照抄
      `archive_language`/`timezone` 一等偏好模式。**（2026-07-29 完成，TDD，
      15 个新测试；细案 `2026-07-29-content-encryption-preference.md`）**
      `registry._get/_set_user_content_encryption`（含**值未变即 no-op**，防
      `persist_user` 触发全表重载风暴）+ prefs 入口接受该字段 + whoami 下发
      （未设置显式下发 `"off"`，不让客户端猜默认）。
- [ ] ⚠️ ~~whoami 按偏好决定是否下发 `enclave_content_public_key_hex`~~
      **推迟到 Phase 3 之后**：现役 iOS 用该公钥封双收件人信封，这是它**唯一**
      的写入路径。Phase 2 上线时 Phase 3 尚未发生，默认 `off` + 停发公钥 =
      现役 App 立刻写不进任何内容。下发一个明文档用不到的**公钥**没有安全代价
      （它本就在 `/attestation` 公开），停发的代价却是全量写入中断。待 iOS 开关
      发版且旧版本淘汰后另立小细案。
- [ ] 开关只影响新写入；切换走 swap 通道逐条转换（Task 2.3）。
- [ ] **去 local_only**：写侧不再接受 `visibility=local_only`；旧客户端仍传则
      按偏好归一。

### Task 2.2: 写侧格式路由

- [ ] 各写闸（`chat/chat_core.py` 含 `caption_envelope`、`memory/memory_core.py`、
      `identity/identity_core.py`、`worldbook/worldbook_core.py`、
      `genesis/genesis_core.py`，07-23 快照行号细案重扫）：明文档用户接受
      `{body:...}` 明文直存；加密档用户接受双收件人信封（K_user+K_enclave）
      原样存。按行存原样，服务端不转换。
- [ ] 服务端封装点：**按目标用户偏好** —— 明文用户直写明文，加密用户仍封双收件人
      信封（服务端自产内容如 agent 回复，对加密用户经 enclave 公钥封存）。
      > **2026-07-29 侦察：不必逐个改。** 实测 43 个封装点里 **40 处都经
      > `core.envelope._build_shared_envelope_for_store(store, plaintext, *, item_id)`**，
      > 而该函数**已经接收 `store`**——用户偏好唾手可得。**在这一个函数内部按偏好
      > 路由即可收口 40 处**：`off` 返回明文形状
      > `{body, id, owner_user_id, visibility}`（与 TEE 侧实测形状一致，读侧
      > Task 1.1 的判据已能识别），`on` 维持现有双收件人信封。
      > 只有 **3 处直调 `build_envelope`** 绕过 helper，需单独处理：
      > `accounts/accounts_core.py`、`content/content_core.py`、
      > `model_api_runtime/v2/extraction.py`。
      > ⚠️ 计划正文原写「14 处」是 07-23 快照，实测已 43 处——但因为可以一处收口，
      > 这条**服务端自产内容**的路径改动面反而比原估小得多。
      >
      > ⚠️⚠️ **但「一处收口」只覆盖服务端自产内容，不要误以为整个 2.2 都收口了。**
      > **客户端上传的内容走写闸，是另一条独立路径，且写闸有硬形状校验**：
      > `chat/chat_core.py:51` `_ENVELOPE_REQUIRED = ["body_ct","nonce","K_user",
      > "visibility","owner_user_id"]`、`memory/memory_core.py:284` 同款 `required`
      > 清单——明文形状会被这些写闸直接拒掉。所以上面那条 bullet（各写闸按偏好
      > 接受明文/信封两形状）**必须逐个改，无法收口**，范围以开工时 grep
      > `_ENVELOPE_REQUIRED`/`required = \[` 重扫为准。
      > 好消息：**DB 层不拦**——`chat_messages` 等表的 `doc` 列没有 body_ct 级
      > CHECK 约束（只有 V2 的 0043 有，见下条）。
      >
      > 🔴 **2026-07-29 实测：一刀切收口会造成安全退化，已回退。**
      > 在 helper 里只按「用户偏好」路由是**错的**——该 helper 被 V2 trajectory /
      > effect payload 等**本该始终加密**的系统内部路径共用。改完之后
      > `test_v2_encrypted_effect_payload::test_tool_effect_payload_real_crypto_round_trip`
      > 直接抓到**明文出现在存储内容里**，L1 从 2 failed 涨到 16 failed。
      > **正确做法是按两个维度路由**：先逐条把 40 个封装点分成「用户内容（可按
      > 偏好明文）」与「系统内部必须加密」（V2 轨迹/effect/review 等），再叠加
      > 用户偏好；且必须**先**放宽 0043 的表级 CHECK。
      > 规格已留在 `tests/test_write_side_format_routing.py`（当前整文件
      > `pytest.mark.skip`，含回退原因），下一轮开工直接启用。
      >
      > ✅ **V2 的 13 处已按调用点逐条核实完（2026-07-29），结论与按文件的粗分类
      > 几乎相反——13 处里 11 处是用户内容：**
      >
      > | 调用点 | 写的是什么 | 类别 |
      > |---|---|---|
      > | `serve_worker.py` 1163 / 1207 / 1221 / 1261 / 1271 | 对话摘要的 leaf / head / parent CAS 封装 | **A 用户内容** |
      > | `serve_worker.py` 1699 | 记忆卡（`memory.actions` 入口） | **A 用户内容** |
      > | `worker.py` 3914 / 3991 | thinking / chain-of-thought 子信封 | **A 用户内容** |
      > | `worker.py` 4024 / 4063 | AI 回复、附件卡 | **A 用户内容** |
      > | `jobs_store.py` 2494 | 兜底回复 `_TERMINAL_FAILURE_FALLBACK_REPLY` | **A 用户内容** |
      > | `serve_worker.py` 2991 | flight-recorder（"Seal flight-recorder content"，轨迹诊断） | ⚠️ **待判** |
      > | `worker.py` 4112 | tool effect payload（`_tool_effect_item_id`）——**上一轮回退的肇事者** | ⚠️ **待判** |
      >
      > **待判 2 处的初步结论（2026-07-29 查证，⚠️ 尚未经实现验证）：两处都倾向 A 类。**
      > 依据：`test_v2_encrypted_effect_payload::test_tool_effect_payload_real_crypto_round_trip`
      > 的断言是 `plaintext_payload["signature"] not in json.dumps(stored)`
      > **配合** `json.loads(decrypted) == plaintext_payload`——它测的是
      > **「加密确实生效且能还原」**（函数名 `real_crypto_round_trip` 也是这个意思），
      > **不是**「无论偏好都必须加密」。它之所以在上一轮变红，只是因为其建的用户
      > 没设偏好（默认 `off`）而走了明文分支。
      > 同理 flight-recorder 是对话轨迹的诊断副本，明文档用户本就该服务端可读
      > （v6 的卖点之一就是便于排查）。
      >
      > **若结论成立，V2 这 13 处全属 A 类**，Task 2.2 的 V2 部分可整体按偏好路由，
      > 相关测试改为**显式设 `content_encryption=on`** 后其断言依然成立。
      > ⚠️ **但必须先验证再动手**：先只改测试（给它显式设 on）跑一遍确认它仍绿，
      > 证明「该测试与偏好无关、只验加密正确性」，再改实现。上一轮的教训就是
      > 跳过验证直接动实现。另需确认 `0043` 的表级 CHECK 已放宽，否则明文行会被
      > DB 拒掉（`v2_trajectory_events` / `v2_trajectory_reviews` 两个约束）。
      >
      > ## 🔑 实现前必须先解决的设计问题（2026-07-29 验证时发现）
      >
      > **`registry._get_user_content_encryption` 对「用户不存在」与「用户存在但
      > 未设偏好」都返回 `None`，两者无法区分**——而它们的正确处置**相反**：
      >
      > | 情形 | 正确行为 | 理由 |
      > |---|---|---|
      > | 用户存在、未设偏好 | **明文** | v6 默认就是明文档 |
      > | 用户查不到记录 | **加密**（fail-safe） | 查不到就写明文 = 任何 registry 未命中都静默降级成明文，这是安全事故 |
      >
      > 实证：`test_v2_encrypted_effect_payload::test_tool_effect_payload_real_crypto_round_trip`
      > 的用户 `u_effect_real_crypto` 是**纯单元测试用户、不在 registry 里**（只
      > monkeypatch 了公钥与 enclave info）。所以它既不能靠「设 `content_encryption=on`」
      > 走加密分支，也正好是「查不到记录」这一类的活样本——上一轮回退时它变红，
      > 根因其实就是这个未区分的缺口，而不只是「测试没设偏好」。
      >
      > **实现方案（二选一，动手前定）**：
      > 1. 新增 `registry.user_exists(user_id)`，helper 先判存在性：不存在 → 加密；
      >    存在且偏好 != "on" → 明文。
      > 2. 让 `_get_user_content_encryption` 返回三态（`"on"` / `"off"` / `None`=用户
      >    不存在），helper 对 `None` 走加密。**推荐这个**——改动集中在一个函数，
      >    且调用方被迫显式处理第三态，不会像布尔那样被无意中吞掉。
      >
      > ⚠️ 无论选哪个，都要给「用户不存在 → 加密」单独写一条测试锁死。这是
      > fail-safe 方向的落点：**写侧任何拿不准的情况都必须偏向加密**。
      >
      > ## ✅ 40 处分类已全部完成（2026-07-29，全部逐调用点核实）
      >
      > **结论：A 类 34 处 / B 类 6 处。**
      >
      > **B 类（6 处，始终加密，绝不按偏好降级）**——它们封的是**凭证**不是用户内容：
      > `hosted/mcp_core.py`(3，`json.dumps(secret_doc)` 含鉴权头)、
      > `hosted/setup_core.py`(3，provider 凭证)。用户关掉加密开关 ≠ 愿意把自己的
      > API key 明文存在服务端。
      >
      > **A 类（34 处，按 `content_encryption` 偏好路由）**：
      > - V2 13 处：对话摘要 CAS 5、记忆卡 1、thinking 子信封 2、AI 回复与附件卡 2、
      >   兜底回复 1、flight-recorder 1、tool effect payload 1
      > - 聊天 6 处：`chat_send_core` 的 `user_env`/`caption_env`/`cap_env`
      >   （`model_api_chat_send_core` 与 `_send_resident` 各 3）
      > - 入住 5 处：`genesis/service` 的 `init_identity_if_absent` /
      >   `replace_identity_preserving_anchor` / `write_persona_artifact` /
      >   `write_voice_artifact`、`persona_backfill.run_persona_backfill`
      > - 历史导入 3 处：`_append_import_memory_cards` / `_store_identity_payload` /
      >   `_append_model_api_onboarding_greeting`
      > - 身份 3 处：`identity/actions` 的 `_save/_create_identity_action_payload`、
      >   `identity_core.init_identity`
      > - 其余 4 处：`chat/service._chat_plaintext_thinking_extra_for_store`、
      >   `chat/resident_maintenance._append_maintenance_message`、
      >   `memory/actions._build_memory_envelope_for_store`、
      >   `workspace/service.seal`
      > - （另有 test 侧新增的 `voice/routes_asgi.py` 1 处，已登记进守卫基线，同属 A）
      >
      > ⚠️ **`workspace/service.seal` 有配对约束**：同类里唯一一个有对称 `open()`
      > 的——`open()` 目前**无条件**走 `_decrypt_envelope_via_enclave`（`service.py:49`
      > 附近），**改 seal 必须同时给 open 加形状路由**，否则写下去的明文行读不回来。
      > 其余 A 类调用点的读侧走各自的读路径（Task 2.3 统一处理）。
      >
      > **非-V2 的 27 处明细（保留原三档标注以备追溯；🟡 档已于同日补齐为逐条核实）：**
      >
      > | 处数 | 调用点 | 类别 | 核实深度 |
      > |---|---|---|---|
      > | 6 | `hosted/mcp_core.py`(3)：封 `json.dumps(secret_doc)`（**含鉴权头**）；`hosted/setup_core.py`(3)：封 **provider 凭证** | **B 始终加密** | 🟢 **已读实际代码**——这两处是凭证不是用户内容，用户关掉加密开关≠愿意把 API key 明文存服务端 |
      > | 20 | `chat_send_core`(6，其中 2 处是 caption)、`genesis/service`(4)、`history_import`(3)、`identity/actions`(2)、`chat/service`(1，`_chat_plaintext_thinking_extra_for_store`)、`chat/resident_maintenance`(1，`_append_maintenance_message`)、`memory/actions`(1)、`identity/identity_core`(1)、`genesis/persona_backfill`(1) | **A 用户内容**（倾向） | 🟡 **仅凭函数名与所在模块语义判定，未逐行读**——聊天正文 / 图片 caption / thinking / 维护消息 / 记忆 / 身份卡 / 入住产出。动手前需照 V2 那样逐条核实 |
      > | 1 | `workspace/service.py` 的 `seal(self, path, plaintext)` | ⚠️ **待判** | 🔴 只看到签名，未确认写入内容与目标表 |
      >
      > 🟡 **以下是最初按文件的粗分类，已被上面两张表取代，仅作历史留存：**
      > 复核时发现：`v2/serve_worker.py` 的 7 处写的是 `summary` / `turn` / `payload`
      > （对话摘要与回合，**用户内容衍生**）、`v2/worker.py` 的 5 处含 `reply`
      > （AI 回复，也是用户内容）与 `effect_*`（工具效果，可能含凭证）——**V2 内部
      > 本身就是混合的**。把 V2 一律归入「始终加密」会正中风险登记簿那条高风险
      > 「V2 强制加密未改按偏好 → 明文档用户 V2 数据无法直读」。
      > 正确粒度是**逐调用点判定**，尤其要把 `effect payload`（上一轮回退的肇事者）
      > 与 `summary`/`turn`/`reply` 分开。
      >
      > **40 个封装点的初步分类（2026-07-29，按文件；A/B 边界仅供起步参考）：**
      >
      > | 类别 | 文件（处数） | 处置 |
      > |---|---|---|
      > | **A：用户内容**——可按 `content_encryption` 偏好选明文/信封 | `hosted/chat_send_core.py`(6)、`genesis/service.py`(4)、`hosted/history_import.py`(3)、`identity/actions.py`(2)、`memory/actions.py`(1)、`identity/identity_core.py`(1)、`genesis/persona_backfill.py`(1)、`chat/service.py`(1)、`chat/resident_maintenance.py`(1) | 合计 **20 处**，按偏好路由 |
      > | **B：凭证 / 系统内部**——**始终加密，绝不按偏好降级** | `model_api_runtime/v2/serve_worker.py`(7)、`v2/worker.py`(5)、`v2/jobs_store.py`(1)（V2 轨迹/effect，另受 0043 表级 CHECK 约束）；`hosted/setup_core.py`(3)（**provider 凭证**）；`hosted/mcp_core.py`(3)（**MCP secret，含鉴权头**）；`workspace/service.py`(1)（V2 工作区 `seal()`） | 合计 **20 处**，维持信封 |
      >
      > 判据不是「谁写的」而是「写的是什么」：B 类里 `mcp_core` 封的是
      > `json.dumps(secret_doc)`（鉴权头）、`setup_core` 封的是 provider 凭证——
      > 这些是**凭证**不是用户内容，用户把加密开关关掉不代表他要把自己的
      > API key 明文存在服务端。上一轮回退正是因为漏了这层区分。
      >
      > 实现建议：helper 增加显式参数（如 `system_internal: bool = False`）由调用点
      > 声明类别，**默认 False 但 B 类 20 处必须显式传 True**；或反过来默认加密、
      > A 类显式声明可明文（更 fail-safe，推荐后者）。
- [ ] **V2 存储改按偏好**：`0043` 等迁移的 envelope 列放宽 CHECK 为「明文或
      信封」；`serve_worker.py` 写路径按用户偏好选明文/信封。与 Task 1.5 联动。

### Task 2.3: 读侧明文旁路（enclave 保留给信封行）

- [ ] `core/enclave.py` 调用点改「按行格式路由」：明文行直读 doc；**信封行仍走
      `_decrypt_envelope_via_enclave`**（enclave 保留，服务加密用户）。这是加
      快路径，不是删 enclave。（memory/actions.py、hosted/config_store.py、
      genesis/plaintext.py、`serve_worker.py` 等；~~supervisor.py:527~~ 随 V1
      删除——grep 重扫。）BYOK 读侧改读 TEE 明文列（Task 1.1）。
- [ ] 两处 enclave 内计算（VLM caption、memory index）**保留**——服务加密用户；
      明文用户可选走 backend 内联明文版（性能优化，细案定，非必须）。
- [ ] `content/content_core.py`：rewrap **保留**（加密用户设备钥漂移自愈仍需）；
      swap 保留，作为加密开关切换 + 那 7 条 local_only 转明文的通道。
- [ ] `tee_replicator/worker.py`：明文行走 mirror 双写；密文行（加密用户）继续
      经 enclave 解密复制进 TEE 或原样搬运信封（细案定 cutover 后 TEE 主库里
      加密用户存密文、明文用户存明文的共存形态，见 Task 2.4）。

### Task 2.4: 加密用户在 TEE 主库的存储形态（v6 关键设计）

- [ ] 定 cutover 后 TEE 主库里加密用户数据的形态：**原样双收件人信封**（DB 密文，
      读时 enclave 解）。这要求 TEE 库能存信封行（不只是明文）——影响 alembic_tee
      表形状（envelope 列保留）与 replicator/mirror 的密文行搬运语义。与 Task
      0.6 表同步工作流对齐。

---

## Phase 3 — iOS：明文默认 + 开关 UI + local_only 自解（加密代码保留）

**入口 gate：** Phase 2 部署 test 且双格式验证通过。
**出口 gate：** 发版；强制升级窗口启动；那 3 用户的 local_only 经 swap 转明文
（或到期归丢弃）。

- [ ] 写侧：默认明文直传；`content_encryption=on` 走**双收件人信封**（保留
      `ContentEncryption.envelope` 的 K_enclave 封装，**不删**
      `ContentEncryption.swift:52`）；**不再产生 local_only**。
- [ ] 读侧：按行自识别——有 `body_ct` 走 `unseal`（保留），有 `body` 直读。
- [ ] **local_only 存量自解**：App 检测到本地 local_only 历史 → 设备端解密 →
      swap 通道明文重传。目标仅 3 用户 7 条，无覆盖率压力。
- [ ] **保留**（服务加密用户）：`ContentKeyStore` 全部、`ContentEncryption`
      信封构建/解封、`FrameEnvelope` 的 K_enclave 分支、rewrap 自愈、
      attestation 拉取与 Audit 卡、register/recover。
- [ ] 新增：设置页加密开关 UI + 文案（加密 = 更强隐私/稍慢，非功能降级）；写
      whoami 偏好。
- [ ] pbxproj 手工登记新文件（历史坑）。

> v6 下 iOS 几乎不删加密代码——现状是人人加密，新方案是加密变可选、默认明文，
> 主要工作是「加一条明文快路径 + 一个开关」，风险远小于删整个加密体系。

---

## Phase 4 — Cutover 切库（维护窗口执行）

**入口 gate：** Phase 1 出口 gate 全过；Phase 3 强制升级窗口结束（local_only 已
清或入丢弃）；restore 演练复跑过一次（<30 天）。
**出口 gate：** 全站在 TEE 主库上 5xx=0 运行 48h；RDS 转只读观察。

- [ ] 预热：`python -m backend.tee_shadow reconcile` 全量收敛 + `verify` 全绿 +
      `requeue_backlog=0` + 三张 seq 表 setval 核对。
- [ ] 冻结窗口（分钟级）：停写 → 最后一轮增量 replicate/reconcile → verify →
      切 `DATABASE_URL` 指向 TEE pg（backend、runner、consumer；**enclave 回调
      的 backend URL 不变**）→ 停 `FEEDLING_TEE_DUAL_WRITE`、停 scheduler/
      replicator → 解冻。
- [ ] **加密用户密文行随库带走（v6 必做）**：opt-in 加密用户的双收件人信封行
      要原样在 TEE 主库（cutover 前最后一轮 reconcile 确认密文行搬运语义已就位，
      见 Task 2.4）。
- [ ] 存储清单核对：Redis 无持久业务状态；`agent_jobs`/`agent_action_queue`
      排空；`v2_*_outbox` 排空；`v2_*` 必迁表按 Task 1.5 收敛；prod 5 张
      `bak_20260710_*` pg_dump 归档后弃。
- [ ] 连接容量已备好（400）；cutover 后连续 3 天盯 `pg_stat_activity` 峰值与
      CVM `free -m`（available<1000MB 告警线）。
- [ ] 回滚预案：观察期内 RDS 只读在线，任何 P0 → 切回 RDS DSN。
- [ ] 观察 7-14 天后 RDS 快照留档 → 停实例。

---

## Phase 5 — 退役 RDS + 文档（enclave 保留）

**入口 gate：** Phase 4 观察期结束。
**出口 gate：** RDS 终删；docs 重写并 build 通过。**enclave 与链上组件保留。**

- [ ] 代码清理：`tee_shadow/`、`tee_replicator/`（若加密用户密文复制不再需要则
      删、否则保留其密文行搬运部分——细案定）、`alembic/`（RDS 链）删除；
      `alembic_tee` 升格唯一迁移链。**enclave 包、`core/enclave.py`、
      `content_encryption.py` 全部保留**（服务加密用户）。
- [ ] **enclave 保留项确认**：`/v1/envelope/decrypt`、storage 重加密、attestation、
      KMS 内容钥、链上 AppAuth/compose_hash、iOS pinning——全部继续运行。仅确认
      它们的 backend 依赖在 cutover 后指向 TEE 主库。
- [ ] 文档：docs-site architecture/self-hosting/api-keys/index **新增「默认明文
      档」说明**（加密档的 E2E/attested 叙事保留）+ OpenAPI 重生成 + changelog；
      io-onboarding 三件套；`deploy/DEPLOYMENTS.md`、
      `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` 改写为主库运维手册。
- [ ] **R2 凭证收尾**：轮换 R2 access key + 最小权限化；清理
      `scripts/user_logs.py:56` 硬编码只读 token。
- [ ] 收尾运维：TEE 主库监控告警（连接数、available 内存、WAL-G 新鲜度）；
      restore 演练排成季度例行。

---

## 风险登记簿

| 风险 | 等级 | 缓解 |
|---|---|---|
| 单实例 TEE pg 成唯一主库后 CVM 硬故障（RCU stall 前科） | 高 | Phase 0 restore 演练 + RTO 落账；观察期 RDS 只读兜底；季度演练 |
| Phase 1 前重建主 CVM 翻钥 → 现有 shared 存量解不开 | 高 | Global Constraint 第一条；Phase 1 完成前禁碰主 CVM |
| reconcile 慢性病掩盖真实不收敛 + 表同步放大负载 | 高 | Phase 0 Task 0.2 表同步前必修 |
| V2 强制加密未改按偏好 → 明文档用户 V2 数据无法直读 | 高 | Task 0.5 CI 守卫 + 与 V2 线对齐 + Task 2.2 放宽 CHECK |
| v2_* 用户数据表漏迁 | 高 | 表同步工作流逐表分类 + Phase 4 verify 覆盖 |
| 加密用户密文行在 TEE 主库的搬运/共存语义没定 | 中 | Task 2.4 专项设计 + Phase 4 verify 覆盖密文行 |
| 混格式行（明文/信封）读侧漏判 | 中 | Task 2.3 按行格式路由 + 四象限回归；统一格式判别函数 |
| alembic_tee 无落地通道，建表任务空转 | 中 | 表同步工作流建通道 |
| 迁移工具打爆 enclave（502 前科） | 中 | Task 1.2/1.3 限速 + 断点续跑 |

## 附录 A：尾账实测（Task 0.4，2026-07-28 完成）

判据：孤岛 = 有 K_user 但 K_enclave 为空（服务端与 enclave 都解不开、只有设备
私钥能解）。全表扫 prod/test 六张内容表。

| 数据域 | enclave 可解（Phase 1 迁明文） | 需设备（iOS swap） | 确定丢失 |
|---|---|---|---|
| chat_messages 孤岛 | — | **prod 7（3 用户）/ test 1** | — |
| memory/frames/perception/worldbook/user_blobs 孤岛 | — | **0（全表实测）** | — |
| frames/ R2 E2E 对象（K_user only） | — | **0**（frame_envelopes island=0） | — |
| quarantine `decrypt_failed:` | — | — | **797（13 用户）** |

**prod 那 7 条归属**：usr_d980a3a2（4，04-24）、usr_994f8891（2，07-10）、
usr_23b73597（1，07-21）。处置：iOS swap 自解，或入丢弃。

**注**：附录判据针对「local_only（K_user-only）孤岛」。v6 下现有全体 shared
用户（带 K_enclave）默认迁明文由 enclave 解密（Phase 1 主体），不属此表——它们
是"enclave 可解"，不是孤岛。
