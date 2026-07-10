# TEE 内明文 Postgres 迁移设计（feedling-mcp）

日期：2026-07-04（2026-07-07 三轮复核补充，见各节【补充】标记。
第一轮对照参考文档：监控最小权限角色、restore 演练环境、WAL 堆积磁盘告警、
WAL-G↔PG17 核验、TLS 证书生命周期、部署预检、max_connections 容量、
cron env 补全。第二轮对照 hivemind-core 源码：**prod9 gateway 不暴露
`-<port>s` 透传路由的实证（动摇 D2 首选+兜底，升为 Phase 0 第一步）**、
密码漂移 reset-password.sh、deploy.sh 三防线、WAL-G v3.0.8 版本基线。
第三轮对照本仓业务代码：用户内容明文化边界、provider key 等凭证例外、
chat thinking/caption envelope、frame R2 offload 双形态、明文双写补偿、
DB 角色最小权限）
状态：草案，待用户确认四个决策点（见 §1）
参考：`~/Downloads/on-disk-postgres-reference.md`（hivemind-core 生产经验蒸馏，含四条事故修正）

## 0. 目标与非目标

**目标（终态）：**

- 用户数据存在一个独立 TEE CVM 内的 Postgres 里，**明文存储**，落在 CVM 的
  FDE（LUKS2）加密磁盘上——静态加密由平台透明提供。
- 停掉外部 AWS RDS（prod + test 两套）。
- 删除 app（iOS）与后端的整个 v1 信封加解密层：`ContentEncryption.swift`、
  `backend/content_encryption.py`、`backend/core/envelope.py`、enclave 的
  content decrypt 路由、rewrap/swap 密文路径。
- 过渡期与现有 RDS 双写，任何时刻可回滚。

**替代性信任边界（照搬参考文档 §1）：明文永不离开 enclave。** 出边界的只有两样，各有保护：

| 出边界流量 | 保护 |
|---|---|
| SQL 跨 CVM（app/runner CVM ↔ pg CVM） | gateway TLS 透传，TLS 在 pg CVM 内终止 + PG 密码认证 |
| WAL/base backup → R2 | WAL-G libsodium 客户端加密（**强制，fail-closed**） |
| frame body → R2（若保留 offload） | TEE 内存储层对称加密（KMS 派生钥） |

**非目标：**

- 不改动托管 agent 运行时、litellm gateway、proactive 等业务逻辑（它们只透过
  db.py 读写，不感知底层换库）。
- 不做 RDS 内密文的原地解密（RDS 从头到尾只见过密文，无新增暴露）。

## 1. 决策点（按推荐值行文，均待用户确认）

| # | 决策 | 推荐值 | 备选 |
|---|---|---|---|
| D1 | `local_only` 隐私等级命运 | **取消，全部明文**。存量 local_only 服务端解不了，需 iOS 配合重传明文；重传不了的接受丢弃（prod 用户少且均为 tester） | 保留为密文特例列（iOS 永久保留加解密代码，与目标冲突）；或存量直接丢弃 |
| D2 | 部署拓扑 | **独立 pg CVM + 原生 PG wire over gateway TLS 透传**（`<pg-app-id>-5432s.<gw>`）。db.py 几乎不改，LISTEN/NOTIFY 保住，app 部署不碰 DB | 塞进主 app CVM（数据生命周期绑死主 CVM）；HTTP sql-proxy（db.py 重写 + wake bus 死，排除） |
| D3 | 切读窗口的旧客户端 | **协调 iOS 强更，短窗口**：backend 切读与 iOS 明文版同步发布，旧版信封写直接 400/426 提示升级。不碰 enclave 热路径 | 兼容期内旧信封写走 enclave 同步解密落库（单线程 enclave 是已知 502 瓶颈，低流量勉强可行） |
| D4 | R2 上的 frame body | **保留 R2 offload + 存储层加密**：TEE 内用 KMS 派生的服务端对称钥加解密。这是存储层加密（同 WAL-G 备份一个性质），不算 app 信封层 | 全部收回 PG 明文（磁盘与备份体积大涨） |

**关键教训（来自本仓库历史事故，必须遵守）：pg CVM 使用独立的 AppAuth 合约**，
绝不与主 app 或 runner 共享——上次 runner 复用主 AppAuth 直接导致主 enclave
KMS 内容钥被换（0a5578→2d642e 事故）。

## 2. 终态架构

```
┌─ 主 app CVM ─────────────┐      ┌─ pg CVM (feedling-pg-{test,prod}) ──┐
│ ingress/backend/mcp/     │ TLS  │ postgres:17 (direct TLS, :5432)     │
│ enclave                  ├─────►│   pgdata volume on FDE disk         │
│ DATABASE_URL=postgres…   │ 透传 │ WAL-G ──► R2 (libsodium 加密)       │
└──────────────────────────┘      │ cron: 每日 base backup 03:00 UTC    │
┌─ runner CVM (test/prod) ─┤      │ 独立 AppAuth 合约                    │
│ agent-runner / genesis   ├─────►│ 手动部署，排除在 app 自动部署外      │
└──────────────────────────┘      └─────────────────────────────────────┘
```

- 每个环境一个 pg CVM：`feedling-pg-test`、`feedling-pg-prod`（先 test 全流程走完再 prod）。
- 消费方共 3 个 CVM（主 app、test runner、prod runner），全部通过同一种连接串访问。
- backend 侧只换 `DATABASE_URL`；psycopg3 连接池、LISTEN/NOTIFY wake bus、
  Alembic 全部照旧工作（原生 wire 协议）。

### 网络路径（D2 的技术核心，Phase 0 spike 验证）

gateway 按 SNI 路由，`s` 后缀 = TLS 透传（gateway 只见密文，参考
`dstack-tutorial/04-gateways-and-tls`）。要求客户端 **TLS-first + SNI**：

**【补充·hivemind 实证，Phase 0 第一步】透传路由是否存在取决于 gateway
集群，不能想当然。** hivemind `deploy/phala/DEPLOY.md` 记录（2026-04）：
dstack-pha-prod9 的 gateway metadata **不暴露 `-8100s` 透传路由**，他们被迫
`HIVEMIND_ENCLAVE_TLS=0` 退回「gateway 终止 TLS + 明文 HTTP 上游」。
我们的首选（PG17 direct TLS）和兜底（stunnel）**都依赖同一个 `-<port>s`
透传能力**——若目标集群不提供，两条路一起死，只剩 HTTP proxy 重构（D2 的
排除项）。因此 spike 第一步：在目标集群实测任意 `-<port>s` 路由是否可达
（`phala cvms get --json` 看 gateway metadata + 实连），不通则立即升级
决策：换支持透传的集群/托管方，或重评 HTTP proxy 代价。
**好消息（本仓活证，风险降级）**：feedling 自己在 prod9 上已有工作中的
`s` 透传路由——runner CVM 经 `https://<app-id>-5003s.dstack-pha-prod9…`
直连 enclave 的自签 TLS（见 `deploy/DEPLOYMENTS.md`）。所以对我们而言
路由本身大概率可用，spike 的真正未知缩小为：PG wire 的 TLS-first 行为
（direct TLS / stunnel 的 SNI 握手）+ 长连接存活。

- **首选**：Postgres 17 支持 direct TLS（ALPN，跳过明文 SSLRequest 前导），
  libpq ≥17 客户端加 `sslnegotiation=direct sslmode=verify-full`。
  需确认 psycopg3 所带 libpq 版本 ≥17（psycopg[binary] wheel 或容器内装 libpq17）。
- **兜底**：每个消费 CVM 加一个 stunnel client sidecar
  （localhost:5432 → TLS+SNI → gateway → pg CVM 内 stunnel server → postgres），
  对 psycopg 完全透明，任何 libpq 版本可用。
- **必测项**：LISTEN/NOTIFY 专用长连接过 gateway 的空闲超时/keepalive 行为
  （wake bus 靠一条池外常驻连接，`backend/db.py:140-145`）；断线重连逻辑已有则确认，
  没有则补。另测跨 CVM RTT 对比现 RDS（现状本来就出 CVM 到 us-east-1，预期不变差）。
- 认证：PG 强密码（Phala 加密 env 注入）+ TLS 证书校验（客户端 pin pg CVM 证书
  或 verify-full）。数据库层面继续单库多 schema/表，不做多租户。
- **【补充】TLS 证书的签发与轮换必须在 Phase 0 定案**：`-5432s` 是透传，
  证书由 pg CVM 自己出（gateway 不代终止）。若证书/私钥派生自 CVM 身份，
  **重建 pg CVM 即换证书**（同「新建 CVM 换 KMS 钥」事故的机制），三个消费
  CVM 的 pin 会同时失效。Phase 0 产出需包含：签发方式（自签长效证书经加密
  env 注入 / RA-TLS / 其他）、客户端校验方式（pin 指纹 vs verify-full+自有 CA）、
  以及证书轮换的 runbook 步骤（先发新证书到客户端信任列表→再换服务端，
  避免一刀切断三个 CVM）。

## 3. pg CVM 构成（照搬 hivemind + 四条修正）

从 `hivemind-core` 复制并改造（源文件见参考文档末表）：

- **镜像** `deploy/postgres/Dockerfile`：pinned `postgres:17@sha256:…` digest
  （可复现 compose hash，attestation 需要）、WAL-G 静态二进制、cron。
  **【补充】WAL-G↔PG17 兼容性需显式核验**：hivemind 生产验证的是 postgres:16
  + `WALG_VERSION=v3.0.8`（`wal-g-pg-22.04-amd64` tarball，Ubuntu 构建装在
  Debian 基础镜像上可用），我们为 direct TLS 升到 17；选版本从 v3.0.8 起步、
  确认 release notes 支持 PG17，且 Phase 1 验收里 `backup-push`/`wal-push`/
  `backup-fetch` 三个动作都要在 PG17 上实测过（restore 演练覆盖 fetch）。
- **compose** `deploy/docker-compose.phala.postgres.yaml`：
  - 命名卷 `pgdata`（原地 compose 更新不丢数据）；
  - `pg_isready` healthcheck；
  - **修正 1**：archive 配置全部走 compose `command` 服务器旗标
    （`archive_mode=on`、`archive_command=wal-g wal-push %p`、
    `archive_timeout=60`、`wal_level=replica`），**不用** initdb 脚本
    （volume 早于备份配置时会静默不归档）；
  - direct TLS / stunnel server 配置；
  - **【补充】`max_connections` 显式设置**：RDS 是托管调优，自建 PG17 默认
    只有 100。按 3 个消费 CVM ×（worker 数 × 池上限）+ wake bus 每 worker
    一条池外常驻连接 + tee_replicator + 监控角色 + 余量算一遍，连同
    `shared_buffers` 等基础内存参数一起走 compose `command` 旗标
    （这也接上多 worker 上线遗留的「核对 PG max_connections」事项）。
- **entrypoint**（改造 hivemind 的 `entrypoint-wrapper.sh`）：
  - **修正 4（fail-closed）**：`WALG_S3_PREFIX` 已设而 `WALG_LIBSODIUM_KEY`
    未设 → exit 1。密钥 `openssl rand -hex 32` 生成一次，存密钥管理 **和**
    enclave 平台之外的 break-glass 位置；
  - **修正 2**：cron 环境写入 `PGHOST=/var/run/postgresql`、`PGUSER`、
    `PGPASSWORD`（hivemind 曾因缺这个，夜间 base backup 静默失败数月，
    桶里 1000+ WAL 零 base backup = 不可恢复）。
    **【补充】完整清单照参考文档 §3**：除 PG 三件套外，`WALG_*`、`AWS_*`、
    `PGDATA` 也要一并落进 `/etc/environment.walg`（chmod 600），别只写三个；
  - **修正 3**：每次启动后台检查 `wal-g backup-list`，无 base backup 立即补推；
  - **【补充·hivemind 实码】密码漂移对齐（照搬 `reset-password.sh`）**：
    `POSTGRES_PASSWORD` 只在 initdb 建卷时生效，之后轮换 env **不会**改角色
    密码（与修正 1 同类的 initdb-only 坑）——一旦部署 env 与建卷时的密码
    漂移，三个消费 CVM 全部认证失败。每次启动后台经 unix socket
    `ALTER USER ... WITH PASSWORD`（peer/trust 认证，无需旧密码）幂等对齐。
- **备份**：每日 03:00 UTC `backup-push.sh`（`wal-g backup-push` +
  `delete retain FULL 7`）；R2 新 bucket（如 `feedling-pg-backups`，
  与现有 io-user-logs 凭证分开）。
- **监控（没有监控 = 没有备份）**：外部 cron/CI 三项检查——
  `pg_stat_archiver.last_archived_time` 超 1h 告警（直连 SQL 查询即可，
  我们有原生 wire，不需要 hivemind 的 proxy）；R2 bucket 检查
  `wal_005/` 最新对象 <1h、`basebackups_005/` 非空且最新 sentinel <26h；
  **【补充】pg CVM 磁盘使用率告警**——`archive_command` 持续失败时 WAL 会在
  本地无限堆积直到 FDE 盘满、Postgres 停机，这比「备份静默失败」更快变成
  停服（两边监控互补：archiver 告警发现归档死了，磁盘告警兜住堆积后果）。
  runbook 里写明处置顺序：**先修归档，绝不手删 `pg_wal/`**。
  每月 restore 演练（fetch LATEST 进 scratch 容器 + `SELECT count(*)` 已知表）。
- **【补充】监控用最小权限角色（信任边界要求）**：库里是明文，监控跑在
  enclave 外（GitHub Actions/外部 cron）——如果它拿的是应用 DB 密码，等于给
  enclave 外开了一条读全量明文的通道，打穿「明文永不离开 enclave」。
  hivemind 走 HTTP proxy 时有 `X-Proxy-Key` 隔离数据面，我们走原生 wire
  没这层，必须在 DB 侧补：建专用 `monitoring` 角色，只授 `pg_monitor`
  （够读 `pg_stat_archiver`），密码与应用密码分开生成、分开存放，
  泄露也读不到业务表。
- **【补充】DB 角色按职责拆开**：明文库里不要让所有调用方共用 owner/superuser。
  至少拆 `app`（业务读写，非 owner）、`migration`（DDL/owner 权限，手动短时使用）、
  `tee_replicator`（写目标明文表与复制游标，按需只读旧库）、`monitoring`
  （`pg_monitor`）、`backup`/本地 socket（WAL-G base backup）。每个角色独立
  密码和轮换 runbook；Phase 1 验收里用 `app` 角色确认不能 `DROP TABLE` /
  `ALTER TABLE`，用 `monitoring` 角色确认不能读业务表。
  ⚠️ **与现状冲突，需决策**：现在是 app 每次启动用 `DATABASE_URL` 程序化跑
  `alembic upgrade head`（`backend/db.py:92-105`），app 非 owner 意味着 boot
  遇到 pending migration 直接失败。`alembic_tee` 链必须二选一：改成部署流程
  里的独立迁移步骤（用 `migration` 凭证，app 启动只做版本断言）；或接受
  app=owner、放弃这一条拆分（保留其余角色拆分不受影响）。
- **【补充】restore 演练在 TEE 内做**：备份解开即全量明文，且演练需要
  `WALG_LIBSODIUM_KEY`——在笔记本或普通 CI 容器里演练，等于每月把明文数据
  +备份钥搬出 enclave 一次（hivemind 数据敏感度不同，参考文档没管这点，
  我们必须管）。演练跑在一次性 scratch CVM（或 pg CVM 内的临时容器、
  不同端口/数据目录）里，完成即销毁，写进 runbook。
- **部署**：独立 AppAuth 合约；CI 排除在 merge 自动部署外
  （只手动 `target=postgres`）；`deploy/DEPLOYMENTS.md` 增补 runbook
  （含 restore.sh 灾难恢复流程）。
  **【补充】部署预检照搬 hivemind `deploy.sh` 的 `required_vars()`**：
  推 compose 前校验其引用的每个 `${VAR:?}` 在 env 文件里都存在——pg CVM 的
  env 特别多（`WALG_*`/`AWS_*`/PG 密码/TLS 材料），缺一个的结果是 CVM
  起来才炸，事后排查比事前预检贵得多。
  **【补充·hivemind 实码】deploy.sh 的另外三条防线也照搬**（源头事故：
  `phala deploy --cvm-id` 会用 -e 文件**整体替换** sealed env，缺 var →
  boot ERR_INTERPOLATION → gateway 空响应 curl 000，hivemind 踩过 3 次）：
  ① deploy 后显式 `phala envs update` 重 seal 全量 env 再 restart；
  ② health poll 用自己的超时（fresh deploy 实测 6–9 min，CLI `--wait`
  硬编码 300s 会假超时，其退出码只当参考），失败时自动 dump serial logs；
  ③ **create/update 双模式防呆**：更新模式下 pg CVM 名字查不到直接 die，
  绝不静默 create 一个全新空库 CVM（= 数据孤儿化 + 换钥，对 pg CVM 是
  最危险的误操作）；首次创建必须显式传 NODE_ID。

## 4. 明文 schema 设计

原则（参考文档 Phase 0）：**这是唯一一次把类型、索引、约束做对的机会，
不要照抄密文 schema。**

- 13 张本就明文的表（`users`、`user_blobs`、`user_logs`、`global_blobs`、
  `server_config`、`perception_daily`、`genesis_import_jobs/_outputs`、
  `copytext_*`、`agent_runtime_*`、心跳表）：DDL 原样复制。
  **【补充】这里的“明文表”不是“所有字段都可以裸奔”**：`server_config.value`
  里有 pepper 等二进制机密（`backend/accounts/registry.py:53`；心跳在独立的
  `supervisor_heartbeats` 表，不在这里），`user_blobs.model_api.api_key_envelope`
  存用户 provider key 的 v1 envelope（`backend/hosted/setup_core.py`），
  这类**凭证/密钥材料不是用户内容**，不能随 Phase 7 粗暴解成普通明文列。
  Phase 2 schema 设计必须出一张例外矩阵：用户内容（chat/memory/worldbook/
  frame/genesis import）迁成明文；provider key、runtime secret、pepper、
  外部 API key 等继续 envelope、迁 TEE-only secret table，或迁平台 sealed env/
  KMS 派生存储。Phase 7 删除 `content_encryption.py` 前，必须先替换
  `/v1/model_api/key_envelope` 与 agent-runner JIT decrypt 链路，否则托管
  agent/model_api 会被拆断。
- 6 类密文表改明文列：
  - `chat_messages` / `memory_moments` / `world_book_entries`：doc JSONB 里的
    信封字段（`body_ct`/`nonce`/`K_user`/`K_enclave`/`enclave_pk_fpr`）换成
    明文 `body`（text 或 jsonb）；保留 `visibility` 列（产品语义上
    「agent 是否可见」仍存在，只是不再由密码学强制）。顺手加过去做不了的
    索引（如 body 的全文/trigram 索引，按需）。
    **【补充】chat 不是只有主 envelope**：当前 `chat_messages.doc` 还可能有
    `thinking_body_ct`/`thinking_K_user`/`thinking_K_enclave`（agent reasoning
    envelope）以及图片 caption 的 `caption_body_ct`/`caption_K_enclave`
    等字段。明文 schema 至少要区分 `body`（主文本或图片 body 指针）、
    `thinking_body`（可空，保留 visibility/source 元数据）、`caption_body`
    （可空，图片附文），并给迁移 worker 单独解这三类 envelope；一致性验证
    也要抽样覆盖 thinking/caption，不能只比主 `body_ct`。
  - `frame_envelopes` → `frames`：行内存明文 meta；body 按 D4 存 R2
    （存储层加密）+ 行内存 R2 key 与存储钥版本。
    **【补充】对齐现有 R2 offload 双形态**：`0007_frame_body_to_r2` 之后，
    老行可能仍是 `doc.body_ct` inline，新行是 `env_meta + body_key + doc=NULL`。
    迁移 worker 要同时支持：inline 行直接取 `doc.body_ct` 解密；R2 行按
    `body_key` 拉密文、用 `env_meta` 重建 envelope 解密；新 TEE schema 记录
    `body_storage_key`、`body_storage_key_version`、`body_mime`、`body_sha256`
    /尺寸等校验字段。验收要分别覆盖 inline legacy frame 和 R2-backed frame。
    注意 inline 存量可能≈0：`backend/backfill_frames_to_r2.py` 是现成的一次性
    inline→R2 搬迁脚本，很可能早已跑过——Phase 3 前先
    `SELECT count(*) FROM frame_envelopes WHERE doc IS NOT NULL` 清点，
    决定 inline 分支的测试样本是用真数据还是手工构造 legacy 样本。
  - `perception_items`：photo 信封部分同 frames 处理。
  - `genesis_import_chunks`：**不复制**（2026-07-07 用户定案）——chunks 是
    上传 staging 数据，distill 完即无用；切读窗口冻结进行中的 import 即可。
    TEE 库不建此表的密文形态。
- **迁移框架**：TEE 库用独立的 Alembic 迁移链（新目录
  `backend/alembic_tee/`，独立 version table），因为两库 schema 在过渡期
  分叉；终态 RDS 链归档删除，TEE 链成为唯一权威。
- 序列/约束：沿用 `0012` 的 per-user `ON DELETE CASCADE`；回填后
  `setval` 校正 SERIAL（参考文档已知坑）。

## 5. 双写与复制（过渡期数据流）

**核心约束：backend 解不了密（只有 enclave 能解 shared，只有设备能解
local_only），且 enclave 单线程是已知 502 瓶颈——热路径绝不同步 decrypt。**

三条数据流，分别处理：

1. **明文表（13 张）：db.py 同步双写。**
   db.py 增加第二个连接池（`TEE_DATABASE_URL`），写 helper 内 fan-out：
   RDS 为主（失败语义照旧），TEE 侧失败只记日志 + 计数器（双写期 TEE 是影子库，
   不能影响主路径）。开关 `FEEDLING_TEE_DUAL_WRITE=1`。
   业务层 35 个模块零改动。
   **【补充】只记日志不够，必须有补偿闭环**：这 13 张表没有经过
   `tee_replicator` 解密复制，如果 TEE 双写失败只落日志，shadow 库会永久缺行。
   Phase 2 需加 durable outbox（RDS 主事务后记录待重放事件）或周期 reconciler
   （按表扫描 RDS→TEE upsert 校正，带水位/分页/校验）。验收不是“看到错误日志”，
   而是故意断开 TEE 连接制造失败，恢复后补偿任务能把两库行数重新拉平；
   停 RDS gate 必须要求 outbox 清空 / reconciler 最近一轮全绿。
   **【补充】明文表的存量回填也走这里**：同步双写只覆盖开关打开**之后**的
   新写入，13 张表里已有的存量行（明文，不需要 enclave，直接 copy）由
   reconciler 的全表扫描兼任首次回填——即开启双写后跑一轮全量 reconcile
   即完成明文表迁移，之后降为周期校正。这样明文表回填/补偿/校验是同一套
   代码，不必单独写一次性迁移脚本。

2. **密文表（6 类）：异步「解密复制」worker。**
   - 新模块 `backend/tee_replicator/`：按表扫 RDS（`updated_at`/id 水位游标，
     游标存 TEE 库），批量调 enclave decrypt（复用
     `/v1/content/rewrap-to-current-key` 已走通的「enclave 解密→重落库」编排，
     `backend/content/routes.py:341`），限速（可配 QPS，避免压垮 enclave），
     幂等 upsert 进 TEE 明文表，断点续传。
   - 同一机制覆盖**存量回填**和**过渡期旧客户端增量**（增量 = 水位之后的新行）。
   - `local_only` 行解不了：标记 `pending_device_migration`，等 D1 的
     iOS 重传流程；到停 RDS 的 gate 时还没重传的按 D1 决议处理。
   - frame body：从 R2 拉密文 → enclave 解 → 存储钥重加密 → 写 R2 新前缀，
     行内更新指针。
   - **执行防护**（参考文档 Phase 3）：手动触发的 CI workflow，默认
     `dry_run=true`（只出 plan + schema 校验），真跑需字面 `confirm: MIGRATE`；
     目标表非空需显式 `--truncate-dst`；与部署用不同 concurrency group。

3. **切读后的新写入：客户端发明文，backend 反向双写。**
   iOS 明文版发布后：明文同步写 TEE pg（成为主库），同时用现成的
   `build_envelope`（`backend/content_encryption.py:91`，加密只需公钥，
   backend 都有——rewrap 的加密侧就是这么干的）加密写回 RDS，维持回滚能力，
   直到停 RDS。开关 `FEEDLING_TEE_PRIMARY=1`。

**一致性验证 job**（切读 gate 的硬条件）：per-table per-user 行数对比 +
按比例抽样（enclave 解 RDS 行与 TEE 行逐字段比对）；差异输出报告。

## 6. iOS 侧改动（独立仓 feedling-mcp-ios，与 Phase 5 同步）

- 上传/下载走明文（TLS 终止在 TEE 内的现有通道不变）。
- 删除 `ContentEncryption.swift` 信封构造/解析、Keychain `content_sk`
  的内容层用途（注意：如果该钥还用于别的身份用途需先确认）。
- `local_only` 存量重传：登录后检测服务端 `pending_device_migration` 清单，
  本地能解的解密重传明文，一次性流程。
- 「decrypt failed」类僵尸数据（设备钥漂移救不回的）在此终局：服务端
  rewrap 能救的走复制 worker 顺带救，救不回的随 RDS 退役丢弃。

## 7. 分阶段实施计划

每阶段有明确验收标准；test 环境全流程（Phase 0–7）走完并 soak 后，Phase 8 在 prod 复刻。

**Phase 0 — 网络 spike（半天～1 天）**
**第一步：确认目标集群 gateway 暴露 `-<port>s` 透传路由**（hivemind 实录
prod9 不暴露，direct TLS 与 stunnel 都栽在这一条上，见 §2 补充）。
然后临时起一个最小 pg CVM，验证：gateway `-5432s` 透传 + PG17 direct TLS 下
psycopg3 连接、简单读写、**LISTEN/NOTIFY 长连接存活/重连**、RTT。
失败则落 stunnel sidecar 方案再测。
✅ 产出：可用的 DATABASE_URL 形态 + 连接参数写进设计文档；
**TLS 证书方案定案**（签发方式、客户端校验方式、轮换步骤，见 §2 补充）。

**Phase 1 — pg CVM 基建（test）（2–3 天）**
按 §3 建 `feedling-pg-test`：镜像/compose/entrypoint（四修正全数落实）、
独立 AppAuth、R2 backup bucket、监控两项、部署脚本 + DEPLOYMENTS.md runbook。
✅ 验收 = 参考文档 §7 检查单：`SHOW archive_mode`=on（活服务器上）、
bucket 有 base backup、桶内对象确认加密、归档告警可触发、
**restore 演练端到端成功一次**、app 部署不重启 pg CVM。
**【补充】追加验收项**：restore 演练在 TEE 内环境完成（§3 补充）；
WAL-G 在 PG17 上 `backup-push`/`wal-push`/`backup-fetch` 全部实测通过；
磁盘使用率告警可触发；监控走 `pg_monitor` 专用角色（用它连上后确认
读不了业务表）；`app`/`migration`/`tee_replicator`/`backup`/`monitoring`
角色权限按 §3 拆分并做负向权限测试；`max_connections` 按 §3 容量公式设置并验证；
部署预检 `required_vars()` 生效（故意缺一个 var 应拒绝部署）。

**Phase 2 — 明文 schema + 双写基建（2–3 天）**
`backend/alembic_tee/` 迁移链 + §4 明文 schema；db.py 第二池 +
明文表同步双写（`FEEDLING_TEE_DUAL_WRITE`）；双写失败计数器/日志 +
outbox/reconciler 补偿闭环；凭证/密钥例外矩阵定稿。
⚠️ 与 ASGI 迁移 worktree（backend-asgi-migration，动 db.py 周边）协调：
本阶段改动集中在 db.py 与新模块，二者需商定合并顺序（建议 ASGI 先合或
本工作基于其分支）。
✅ 验收：test 环境双写开启，明文表两库行数持平；故意制造 TEE 写失败后
补偿任务能追平；provider key/model_api 仍可配置、读取、agent-runner JIT
使用；关掉开关系统行为不变。

**Phase 3 — 解密复制 worker（3–5 天）**
§5.2 的 `tee_replicator` + 防护 workflow + 一致性验证 job。
在 test 环境完成存量回填 + 增量追平。
✅ 验收：验证 job 全绿（行数 + 抽样比对）；worker 可随时中断重启不丢不重；
enclave 负载可控（无 502 回归）；抽样覆盖 chat 主 body、thinking、图片
caption、inline legacy frame、R2-backed frame 五种密文形态。

**Phase 4 — 明文写入路径 + iOS 改造（并行，3–5 天后端 + iOS 独立排期）**
backend 接受明文写（内容 API 的 v2 形态：明文 body + visibility），
实现 §5.3 反向双写；iOS 按 §6 改造 + local_only 重传流程。
旧信封写路径此阶段仍正常工作（走 §5.2 增量复制）。
✅ 验收：test 环境新旧客户端并存均正常；明文写的行两库一致。

**Phase 5 — 切读 + 强更（1 天 + 观察窗口）**
同步发布：backend `FEEDLING_TEE_PRIMARY=1`（读写主库切 TEE pg，
反向双写 RDS 保持同步），iOS 明文版强更，旧版信封写返回 400/426 提示升级。
回滚 = 关开关（RDS 一直被双写，数据不落后）。
✅ 验收：全功能回归（chat/memory/perception/proactive/genesis/托管 agent）、
wake bus 跨 worker 唤醒正常、无 P95 恶化。

**Phase 6 — 停 RDS（gate 制，非时间制）**
Gate 全绿才动：≥1 次 base backup 成功 **且** ≥1 次 restore 演练成功、
归档监控在线、一致性验证通过、切读后 soak ≥7 天无回滚、
local_only 重传收尾（按 D1 处理残余）。
然后：关反向双写 → RDS final snapshot → 只读保留 2–4 周 → 终止实例
（test 和 prod 分别走）。
✅ 在 restore 演练成功前，pg CVM 磁盘是数据唯一副本——这段窗口越短越好
（Phase 1 已把备份前置，实际窗口≈0）。

**Phase 7 — 拆加解密层（2–3 天）**
- backend：删 `content_encryption.py`、`core/envelope.py`、enclave decrypt
  代理与路由、rewrap 端点；`/v1/content/swap` 退化为纯 visibility 字段更新；
  `tee_replicator` 归档。
  **【补充】删除前置条件**：provider key / 外部 API key / pepper / runtime
  secret 等凭证链路已从 v1 content envelope 迁走，并有替代的 TEE-only secret
  存储或 sealed env 方案；`/v1/model_api/key_envelope` 的调用方已改完或端点
  已安全退役。
- enclave：content_sk 派生与 decrypt 路由下线（enclave/KMS 身份保留，
  R2 存储钥派生迁入或保留在 enclave，按 D4）。
- iOS：删加解密代码。
- 文档：`docs/DESIGN_E2E.md`、`CONTENT_ENCRYPTION_INTERACTION_CURRENT.md`
  改写为 TEE 信任模型说明；`deploy/DEPLOYMENTS.md`、`CONTRIBUTING.md`、
  io-onboarding 三件套涉及加密表述的同步更新。
✅ 验收：全量测试基线绿；grep 无信封字段残留（`body_ct`/`K_user`/
`K_enclave`）除历史迁移归档。

**Phase 8 — prod 复刻**
`feedling-pg-prod` 走 Phase 1→6（代码已就绪，主要是基建 + 回填 + 切换 +
gate）。prod 回填量：主要是 frames 与 chat（参考 hivemind 实测吞吐
~750–820 rows/s 过 gateway 定超时）。

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| gateway 透传跑不通 PG wire（**hivemind 实证 prod9 不暴露 `-<port>s` 路由**，direct TLS 与 stunnel 同栽） | Phase 0 第一步先验证目标集群透传能力；stunnel sidecar 兜底；集群不支持则换集群/托管方或重评 HTTP proxy 代价（§2 补充） |
| PG 密码轮换后角色密码不跟（initdb-only 坑）→ 三个消费 CVM 集体认证失败 | 照搬 reset-password.sh 每次启动幂等 ALTER USER 对齐（§3 补充） |
| `phala deploy --cvm-id` 整体替换 sealed env / 误 create 全新 pg CVM | deploy.sh 三防线：重 seal+restart、自有超时 health poll+serial logs、create/update 双模式防呆（§3 补充） |
| LISTEN/NOTIFY 长连接被 gateway 掐 | spike 必测项；补 keepalive + 自动重连 |
| enclave 被复制 worker 压垮（历史 502） | 限速可配 + 只在低峰跑 + 随时可停（断点续传） |
| pg CVM 磁盘 = 唯一副本窗口 | 备份基建先于数据进入（Phase 1 验收含 restore 演练） |
| 备份静默失败（hivemind 全部四条事故） | 四修正 + fail-closed + 双监控 + 月度演练，一条不少 |
| 共享 AppAuth 换钥事故重演 | pg CVM 独立 AppAuth，写进 runbook |
| 与 ASGI 迁移冲突（都动 db.py 周边） | Phase 2 前商定合并顺序 |
| local_only 用户不重传 | D1 决议 + 停 RDS gate 里显式清点残余 |
| 明文库磁盘容量 | 建 CVM 时按 RDS 当前体积 ×（明文膨胀系数~1，密文≈明文长度）× 增长余量估；frames 走 R2 不占盘 |
| 归档持续失败 → WAL 堆积撑爆 FDE 盘 → PG 停机 | 磁盘使用率告警（与 archiver 告警互补）；runbook 写明先修归档、绝不手删 pg_wal/（§3 补充） |
| 外部监控凭证成为明文读取通道 | `pg_monitor` 专用角色，与应用密码分离（§3 补充） |
| restore 演练把明文+备份钥带出 enclave | 演练固定在 TEE 内 scratch 环境做（§3 补充） |
| pg CVM 重建换证书，三个消费 CVM pin 集体失效 | Phase 0 定证书签发/轮换方案，轮换先加信任再换服务端（§2 补充） |
| WAL-G 版本不支持 PG17 | 选型核对 release notes + Phase 1 三动作实测（§3 补充） |
| 删除 content envelope 时误伤 provider key / API key / pepper 等凭证链路 | Phase 2 出“用户内容明文 vs 凭证仍保护”例外矩阵；Phase 7 前先完成 provider key 替代存储与 agent-runner 调用改造（§4/§7 补充） |
| chat thinking/caption 等嵌套 envelope 漏迁 | 明文 schema 显式建 `thinking_body`/`caption_body` 等字段；迁移与抽样验证覆盖三类 chat envelope（§4/§7 补充） |
| frame inline 与 R2-backed 两种旧形态只迁了一种 | worker 同时支持 `doc.body_ct` inline 与 `env_meta + body_key` R2-backed；验收分别造样本（§4 补充） |
| 明文表 TEE 双写失败只打日志，shadow 库永久缺行 | Phase 2 加 outbox/reconciler 补偿，停 RDS gate 要求补偿队列清空/最近校验全绿（§5 补充） |
| 应用/迁移/复制共用高权 DB 用户，扩大明文库 blast radius | 拆 `app`/`migration`/`tee_replicator`/`backup`/`monitoring` 角色并做负向权限测试（§3 补充） |

## 9. 明确不做

- RDS→TEE 的逻辑复制/pg_dump 直搬（数据是密文，搬了也没用，必须过 enclave 解密）。
- 多租户 sql-proxy、admin HTTP 面（hivemind 需要，我们有原生 wire + Alembic，不需要）。
- 双主/双读（TEE 库切主前永远只是影子库，读永远单源，避免一致性泥潭）。
