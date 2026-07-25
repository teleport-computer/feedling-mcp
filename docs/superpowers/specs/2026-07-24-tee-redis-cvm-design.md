# TEE Redis CVM — 设计文档

- 日期：2026-07-24
- 状态：设计已确认，待写实施计划
- 范围：**只建基础设施**。三套独立 Redis CVM（test / pre / prod）跑起来、备份可恢复、
  监控在盯、冒烟能连通。**不接任何业务流量、不改任何业务代码。**

---

## 1. 背景与动机

仓库目前**零 Redis**（`grep -i redis` 的命中全部是 `redistill` 子串）。现有的排队、
唤醒总线、互斥全部走 Postgres：`LISTEN/NOTIFY` 广播 + `SELECT … FOR UPDATE SKIP LOCKED`
抢 job（Runtime V2）。

要引入 Redis 的目标用途有三类（**均不在本 spec 范围内实施**，各自另开 spec）：

1. 缓存 / 热数据 —— 减轻 PG 读压力
2. 队列 / 唤醒总线 —— 补充或替代 PG `LISTEN/NOTIFY` + `SKIP LOCKED`
3. 限流 / 分布式锁 —— proactive 退避、provider 熔断、多 worker 互斥

本 spec 只负责把承载它们的基础设施建成，且建成后**处于零流量待命状态**。

### 明确的成本与代价

多三台 CVM 意味着：三套密钥托管、三条备份链、一个新的运行时单点、
以及三份持续烧的 Phala 账号余额。prod 用户量刻意很小，这些成本是真实的。接受这些
成本的前提是后续接入分阶段进行，任何一个阶段都能独立回退到 PG 路径。

---

## 2. 拓扑与账号

三台**完全独立**的 CVM，互不共享任何东西（密钥、备份前缀、CVM id 文件）：

| | test | pre | prod |
|---|---|---|---|
| CVM 名 | `feedling-redis-test` | `feedling-redis-pre` | `feedling-redis-prod` |
| Phala 账号 | `amiller-user` | `amiller-user` | **`sxysun`** |
| 节点 | prod9 (`dstack-pha-prod9.phala.network`) | prod9 | prod9 |
| 规格 | 1 vCPU / 2 GB / 20 GB | 1 vCPU / 2 GB / 20 GB | 2 vCPU / 4 GB / 30 GB |
| `maxmemory` | 1 GB | 1 GB | 2560 MB |
| API key secret | `TEST_PHALA_CLOUD_API_KEY` | `TEST_PHALA_CLOUD_API_KEY` | `PHALA_CLOUD_API_KEY` |
| 资源 secret 前缀 | `TEST_REDIS_*` | `PRE_REDIS_*` | `PROD_REDIS_*` |
| cvm-id 文件 | `deploy/test-redis-cvm-id.txt` | `deploy/pre-redis-cvm-id.txt` | `deploy/prod-redis-cvm-id.txt` |
| 身份模型 | `--kms phala`（无链上合约） | 同左 | 同左 |
| R2 备份前缀 | `test/redis/` | `pre/redis/` | `prod/redis/` |

**账号归属的依据**：`deploy/DEPLOYMENTS.md:192/234` 记录 test 与 pre 主 CVM 同在
`amiller-user` 账号、共用 `TEST_PHALA_CLOUD_API_KEY`；prod 在 `sxysun` 账号、用无前缀的
`PHALA_CLOUD_API_KEY`。Redis 沿用同一映射——**GitHub Actions 里选环境即选账号**，
不需要额外的账号切换逻辑。

**pre 的 secret 惯例**：现有 CI 中 pre 只对真正需要隔离的资源用 `PRE_` 前缀
（`PRE_DATABASE_URL`、`PRE_AGENT_RUNTIME_USERS`），Phala 账号/key 与 test 共用。
Redis 的密码、TLS 材料、备份密钥、R2 前缀属于「需要隔离」，故三套独立。

**`maxmemory` 只吃掉一半物理内存是刻意的**：`BGSAVE` / `redis-cli --rdb` 走 fork +
copy-on-write，写入活跃时物理内存占用可能接近翻倍。留不出余量会在快照时 OOM。

### 决策 D1：`maxmemory-policy` 用 `noeviction`，不用 `allkeys-lru`

这台机器将来会同时装缓存、锁、队列。任何 `allkeys-*` 策略都会在内存压力下**静默驱逐掉
锁和队列数据**——那是丢消息 / 锁失效级别的事故，且没有任何日志痕迹。

`noeviction` 下内存打满的表现是**写入返回错误**：可观测、可告警、可定位。代价是缓存侧
必须自律——每个缓存 key 强制带 TTL 自然回收。这条约束写进后续每个接入 spec 的前置条件。

---

## 3. 容器与镜像结构

每台 CVM 的 compose 跑**两个容器**，共享两个 volume：

```
services:
  redis:    官方 redis:8-alpine（钉 digest）+ 我们注入的 redis.conf / TLS 材料
            挂 redisdata（数据）+ redissock（unix socket 目录，见 D3b）
  backup:   自建轻量 sidecar，跑快照循环
            挂 redissock（连本地 Redis）；不需要挂 redisdata——快照由
            redis-cli --rdb 生成到 sidecar 自己的临时目录，见 D4
volumes:
  redisdata     # AOF + RDB 落盘，redeploy 不丢
  redissock     # 仅存放 unix socket，容器间 IPC 通道
```

### 决策 D2：备份用 sidecar，不做内嵌镜像

| | A. 官方镜像 + sidecar **（采用）** | B. 自建镜像内嵌 cron（PG 的 wal-g 模式） | C. Redis replication 热备 |
|---|---|---|---|
| 优点 | Redis 升级只换 digest；备份脚本可独立测；职责清晰 | 与 PG 现状形态一致，单容器 | RPO≈0 |
| 缺点 | 多一个容器定义 | 改备份脚本要重建 Redis 镜像、魔改官方 entrypoint | CVM 数翻倍到 6 台，且从库同在 Phala，**同厂商同时失效就一起没**，不构成离场备份 |

选 A。C 解决的是可用性而非「防 Phala 丢数据」，离场备份只有 R2 这一条路。B 的耦合在 PG
那边已经付过代价（改备份逻辑要动数据库镜像）。

### 决策 D3：sidecar 不用 cron，用显式 sleep 循环

PG 那边 2026-07-14 的事故：cron 以精简 `PATH=/usr/bin:/bin` 执行，找不到 `/usr/local/bin/wal-g`，
**每日 base backup 静默失败了很久**，直到排查才发现 prod/test 都只剩建库时的那一份 base，
且 `delete retain` 从没跑成、WAL 在 R2 无限堆积。

sidecar 用 `while true; do backup-push.sh; sleep 3600; done` 的显式循环，进程直接继承容器
环境，绕开 cron 的环境隔离。失败在容器日志里可见，且被第 7 节的监控捕获。

sidecar 基础镜像需要：`redis-cli`（生成快照）、`aws-cli`（推 R2）、`age`（加密）。
基底刻意用**与 Redis 服务端完全相同的 digest**，使 `redis-cli` 与服务端同版本，
不会出现 cli 老于 server 的协议错配。

### 已验证的前提（2026-07-24 实测，不必重跑）

设计依赖的每个「它应该能行」都已经在本机 docker 里验过：

| 假设 | 结论 |
|---|---|
| 官方 `redis:8-alpine` 编译带 TLS | ✅ 8.8.0。缺证书时报 `No tls-cert-file configured!`（若无 TLS 支持会报 `Bad directive`）；`redis-cli` 也带 `--tls` |
| 该基底能装齐 sidecar 工具 | ✅ alpine 3.23.5，`apk add aws-cli age` → aws-cli 2.32.7、age 1.2.1、redis-cli 8.8.0 |
| unix socket + `requirepass` + `REDISCLI_AUTH` 可用 | ✅ `unixsocketperm 700` 下 `redis-cli -s` 正常读写 |
| `redis-cli --rdb` 经 socket 能拿一致性快照 | ✅ `SYNC sent to master… Transfer finished with success`，产出以 `REDIS0014` 魔数开头的合法 RDB |
| `rename-command` 在 Redis 8 仍有效 | ✅ `FLUSHALL`/`CONFIG`/`KEYS` 均变成 `ERR unknown command`，正常命令与数据不受影响 |
| 禁用 `CONFIG` 后监控仍拿得到容量 | ✅ `INFO memory` 的 `used_memory` / `maxmemory` 与 `INFO persistence` 的两个状态字段均可读 |

两个镜像共用的钉死 digest：
`redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005`。

### 决策 D3b：sidecar 与 Redis 之间走 unix socket，不走 TLS

第 5 节要求 `port 0` 关闭明文端口，所以 sidecar 不能走本地 TCP 明文。两个选项：让
sidecar 也做 TLS 握手（要分发 CA 到 sidecar），或者 Redis 额外监听一个 unix socket，
sidecar 通过共享 volume 访问。

采用 unix socket：`unixsocket /var/run/redis/redis.sock` + `unixsocketperm 700`，socket
目录作为第二个共享 volume 挂给 sidecar。它不跨越容器边界之外的任何网络，比在容器内
自己跟自己做 TLS 握手更简单，也省掉一份 CA 分发。sidecar 仍需 `REDIS_PASSWORD`
（`requirepass` 对 unix socket 同样生效），通过 `REDISCLI_AUTH` 环境变量传入——
**不要写在命令行参数里**，否则口令会出现在容器内的进程列表中。

同理，compose 的 healthcheck 用 `redis-cli -s /var/run/redis/redis.sock ping` 配合
`REDISCLI_AUTH`，避免为了健康检查再引入一份 TLS 材料。

---

## 4. 持久化与备份

三层，逐层扩大保护半径：

```
Redis 进程   appendonly yes                        进程崩溃 → 最多丢 1s
             appendfsync everysec
             save 900 1 / 300 10 / 60 10000        RDB 兜底
      ↓
docker volume（redisdata）                          容器重启 / redeploy → 零丢失
      ↓
sidecar 每小时：redis-cli --rdb → age 加密 → R2     整台 CVM 灭失 → 最多丢 1h
```

**RPO 汇总**：进程崩溃 ≤1s；容器/CVM redeploy 0；整台 CVM 灭失 ≤1h。

### 决策 D4：快照用 `redis-cli --rdb`，不拷卷内文件

`redis-cli --rdb` 走 replication 协议，要服务端生成一份**一致性快照**。直接拷 volume 里的
文件有拷到写了一半状态的风险——Redis 7+ 的 AOF 是 `appenddirname` 下的多文件 + manifest，
文件级拷贝更容易拿到互相不一致的组合。

### 决策 D5：备份加密用 age 非对称，公钥进 CVM、私钥离线冷存

PG 用 wal-g 内建的 libsodium 对称密钥（`WALG_LIBSODIUM_KEY`）。Redis 侧改用 age 非对称：

- **公钥**（`REDIS_BACKUP_AGE_RECIPIENT`）是非机密，注入 CVM 用于加密
- **私钥**离线冷存，只在灾难恢复 / 演练时取出

好处：备份机被攻破也无法解密历史备份（对称钥方案做不到这点）。代价：与 PG 的形态不一致，
且恢复演练要取出离线私钥。若后续要与 PG 完全对齐，可退回 `openssl enc` 对称方案——但
本 spec 定的是 age 非对称。

### fail-closed 规则（逐字照抄 PG 的 `entrypoint-wrapper.sh`）

配了 `REDIS_BACKUP_S3_PREFIX` 却缺 `REDIS_BACKUP_AGE_RECIPIENT` → **拒绝启动**，
绝不把明文快照推出 TEE 边界。同理缺 `REDIS_PASSWORD` / TLS 材料 → 拒绝启动。

### 保留策略

- 小时快照保留最近 **24 份**
- 每日 03:00 UTC 那一份额外保留 **7 天**
- 删除由 sidecar 自己执行（对应 wal-g 的 `delete retain FULL 7`）

### boot 兜底

sidecar 启动后若发现 R2 前缀下**没有任何快照**，立刻推一份，不等第一个小时周期。
这是 PG 那次事故的直接解药（`entrypoint-wrapper.sh` 的「修正 3」同款逻辑）。

---

## 5. 安全模型

### TLS

复制 `deploy/postgres/gen-certs.sh` 为 `deploy/redis/gen-certs.sh`，保持同样的形态：

- 自签 CA，**CA 私钥离线冷存，绝不进任何 CVM / CI**
- server 证书 SAN 通配 `*.dstack-pha-prod9.phala.network`——正好覆盖 gateway 的
  `<app-id>-6379s.dstack-pha-prod9.phala.network`，因此客户端可以做到 **verify-full**
  级别的主机名校验，而不是降级成「只加密不校验」
- `ca.crt` 作为非机密分发给消费方，客户端用 `rediss://` + `ssl_ca_certs` 指向它

Redis 侧配置 `tls-port 6379` + **`port 0`（彻底关闭明文端口）**。

### AUTH

`requirepass`，口令用 `openssl rand -hex 32` 生成。

**必须是十六进制**：PG 的 runbook 明确记着这条教训——引号 / `$` / 反引号等字符会破坏
SQL 与 compose 环境注入。Redis 侧同理（`redis.conf` 与 `-e` 注入都吃这个亏）。

### 暴露面（已知限制，必须接受）

dstack CVM 之间**没有私网**，跨 CVM 通信只能走 gateway passthrough
`<app-id>-6379s.…:443`。也就是说 **Redis 端口在公网可达**，只靠 TLS + AUTH 保护。
TEE Postgres 现在就是这个模型，Redis 无法做得更好。

配套缓解：
- 口令 32 字节随机
- `rename-command` 禁用高危命令：`FLUSHALL`、`FLUSHDB`、`CONFIG`、`KEYS`、`DEBUG`
- 明文端口关闭（`port 0`），非 TLS 连接在协议层就失败

### 身份模型：`--kms phala`，无链上 AppAuth

**绝不复用主 app 的 AppAuth 合约**——依据是「新建 runner CVM 换掉主 enclave 钥」
那次事故（复用主 app 合约导致主 enclave 的内容钥被换掉）。

但也**不需要为 Redis 单独部署链上合约**。`docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`
§0 记录了 TEE Postgres 的实际做法：用 `--kms phala` 时 Phala 默认 KMS 按**部署账号**
授权，这类数据存储 CVM 不需要链上 AppAuth（与主 app 不同）。Redis 是同类 CVM，
沿用同一模型。

因此部署流程里**没有** `publish-compose-hash` 步骤，也没有
`*_REDIS_APP_AUTH_CONTRACT` secret。「独立身份」由独立 CVM + Phala KMS 的
账号级授权提供。

### secret 清单（每环境一套）

| 变量 | 机密 | 说明 |
|---|---|---|
| `REDIS_PASSWORD` | ✅ | `openssl rand -hex 32` |
| `REDIS_TLS_CERT_B64` | ✅ | server.crt base64 |
| `REDIS_TLS_KEY_B64` | ✅ | server.key base64 |
| `REDIS_BACKUP_AGE_RECIPIENT` | ❌ | age 公钥（非机密，但随 secret 一起注入便于管理） |
| `REDIS_BACKUP_S3_PREFIX` | ❌ | `<env>/redis/` |
| `REDIS_BACKUP_R2_ENDPOINT` | ✅ | 复用 PG 备份桶的 R2 账号 |
| `REDIS_BACKUP_R2_ACCESS_KEY_ID` | ✅ | |
| `REDIS_BACKUP_R2_SECRET_ACCESS_KEY` | ✅ | |

R2 桶复用 `io-in-enclave-db`（PG 备份用的同一个），靠前缀隔离。R2 token 的 scope 必须
覆盖新前缀——PG 那边 `io-user-attachments` 就踩过 token scope 不够导致 PUT `AccessDenied`
的坑（`DEPLOYMENTS.md:303`）。

---

## 6. 部署流程

### 6.1 首次开通（一次性，本地手工，**不走 workflow**）

`pg-deploy.yml` 是 fail-closed 的：cvm-id 文件读不到就直接失败，报「首次开通走
DEPLOYMENTS.md runbook，不走本 workflow」。这是刻意设计——workflow 只做
`phala deploy --cvm-id <已存在的 id>`（原地更新），**绝不允许它 create**。
一次误触新建 CVM 就是一次换钥事故。Redis 沿用同一纪律。

首次开通顺序（每环境各跑一遍）：

1. 切 Phala profile：test/pre 用 miller 的；**prod 必须先切到 `sxysuns` profile**（易忘）
2. `phala cvms create --kms phala` 建 CVM，拿到 `app_id` 与 `cvm_id`
3. 用 `gen-certs.sh` 签 TLS 材料（SAN 通配已覆盖 gateway 域名），CA 私钥立即离线冷存
4. 生成 age 密钥对，私钥离线冷存，公钥进 secret
5. 首次 `phala deploy` 注入全部加密 env
6. 把 `cvm_id` 写进 `deploy/<env>-redis-cvm-id.txt` 并提交
7. 跑 `deploy/verify-redis.sh` 冒烟
8. 跑第 8 节的 restore 演练——**这是开通的硬 gate**

### 6.2 日常更新（`.github/workflows/redis-deploy.yml`）

逐条复刻 `pg-deploy.yml` 的结构与它踩过的坑（**以 2026-07-24 更新后的版本为准**，
它比原始版多了三条血泪教训）：

- `workflow_dispatch`，`environment` 三选一 `[test, pre, prod]`
- 防误触：test/pre 输入 `DEPLOY-REDIS`，**prod 要求更长的 `DEPLOY-REDIS-PROD`**
  （prod 打的是另一个账号下的真实用户数据机器）
- **绝不用 `${{ env == 'prod' && secrets.PROD_X || secrets.TEST_X }}` 三元选机密**：
  `PROD_X` 恰好为空时会短路 fallback 到 `TEST_X`，于是非空预检通过、注进 prod 的
  却是 test 的密码。正解是两套机密都注入 job env，在 shell 里按环境名前缀间接取值
  （`pick()`），挑错只会挑到空值 → fail-closed。
- **镜像 tag 用 `git rev-parse HEAD`，不用 `${{ github.sha }}`**：workflow_dispatch 下
  后者指向触发时所在 ref 的 sha，而 checkout 的是 test/pre/main，tag 会与镜像内容对不上。
- **机密落 0600 临时 env 文件再 `-e "$ENVFILE"`**，不拼进命令行——`ps` 与日志里
  不该出现明文。用完即删。
- **原地更新必须重带整份机密**，漏一个就被清空 → entrypoint fail-closed 起不来。
  故先整份非空校验再动 CVM。
- 读 cvm-id 时先 `test -f` 再 `grep -v '^#' | tr -d '[:space:]' | head -1 || true`：
  `|| true` 吸收 grep 的 no-match 退出码，否则 GHA 的 `bash -eo pipefail` 会在这里
  裸退出，永远走不到那句明确的报错。
- cvm-id 为空 → fail，绝不静默新建
- checkout ref 按环境：prod → `main`、pre → `pre`、test → `test`
- 部署后自检：`phala cvms get` 确认状态回到 `running`
- `concurrency: redis-deploy-<env>`，与 app 部署不同组
- **永不并入 merge 自动部署**
- **无 `publish-compose-hash` 步骤**（见第 5 节的身份模型）

---

## 7. 监控与告警

`.github/workflows/redis-monitor.yml`，每 30 分钟，监控 **prod + pre**（test 数据可弃，
不监控——与 `pg-monitor.yml` 只监控 prod 的理由一致）。

检查项：

| 检查 | 阈值 | 失败含义 |
|---|---|---|
| R2 最新快照 age | < 2h | 备份链断了（1h 周期留一次失败的余量） |
| `INFO persistence` → `rdb_last_bgsave_status` | `ok` | 快照生成失败 |
| `INFO persistence` → `aof_last_write_status` | `ok` | AOF 写失败，第一层保护已失效 |
| `INFO memory` → `used_memory / maxmemory` | < 80% | `noeviction` 下这是写入即将开始报错的前兆 |

必须复刻 `pg-monitor.yml` 已经踩过的**三个**坑：

- `aws s3api list-objects-v2` **会自动分页**，一个前缀 >1000 对象时每页各吐一个「本页最新」
  时间戳，下游解析直接炸。用 `sort | tail -n1` 取跨页全局最新。
- **不能用 `--no-paginate`**——那只取第一页（最旧的 key），反而误报 stale。
- **`aws` 的退出码必须自己接住**：`aws … | sort | tail -n1` 的退出码取自 `tail`，
  恒为 0。R2 在分页途中限流（`ServiceUnavailable: Reduce your concurrent request rate`）
  时 aws 中断退出，但前几页已经打印出来，于是函数照常返回一个「偏旧的最大值」，
  下游把它当成备份陈旧误报。2026-07-24 11:15 prod 实测过一次假警报。正解是
  `raw=$(aws …) && rc=0 || rc=$?`，失败则退避重试三次，连续失败才报错，
  且措辞要区分「R2 查不了」与「备份真的陈旧」。

空前缀时 aws 返回单行 `None`，要当作「EMPTY → 报错」处理，而不是解析失败。

---

## 8. 灾难恢复与验收

### `deploy/redis/restore.sh`

从 R2 拉最新加密快照 → age 解密（需离线私钥）→ 灌进空 Redis 实例 → 校验。
fail-closed：缺私钥或缺 S3 前缀直接退出，不做「无加密」的可选分支（同 PG 的 `restore.sh`）。

### 演练是开通的硬 gate

**不做完不算开通**（对应 PG 的 Phase 1 验收）：

1. 往目标 Redis 写入一组已知数据（含带 TTL 与不带 TTL 的 key）
2. 等一个备份周期，确认 R2 出现新快照
3. 在一个**空**实例上跑 `restore.sh`
4. 校验：key 总数一致、抽样值逐字一致、TTL 语义保留

### Definition of Done

参照 `docs/testing/TESTING.md` §2 的决策矩阵，本 spec 的完成判据是：

- [ ] 三台 CVM 均 healthy，`verify-redis.sh` 三环境全绿（TLS + AUTH + SET/GET/TTL + INFO）
- [ ] 三个 R2 前缀下均有 ≥2 份快照（证明周期循环在跑，不只是 boot 那一份）
- [ ] restore 演练三环境各做一次并通过校验
- [ ] `redis-monitor.yml` 手动触发一次全绿
- [ ] 明文端口验证：非 TLS 连接被拒绝
- [ ] fail-closed 验证：故意缺 `REDIS_BACKUP_AGE_RECIPIENT` 部署一次，确认容器拒绝启动
- [ ] 三份 cvm-id 文件已提交，三个 app_id 已记入 `DEPLOYMENTS.md`
- [ ] **零业务流量**——没有任何生产代码引用 Redis

---

## 9. 交付物清单

```
deploy/redis/
  Dockerfile              # Redis 服务端：官方镜像 + 我们的 conf/entrypoint
  Dockerfile.backup       # backup sidecar：同 digest 基底 + aws-cli + age
  redis.conf              # tls / appendonly / maxmemory / noeviction / rename-command
  entrypoint-wrapper.sh   # fail-closed 校验 + TLS 材料落盘 + 启动 redis-server
  backup-push.sh          # 单次快照 → age 加密 → R2 → 保留策略（可独立调用）
  backup-loop.sh          # sidecar 主进程：boot 兜底 + 每小时循环
  restore.sh              # 灾难恢复 + 演练脚本
  gen-certs.sh            # 一次性 TLS 材料生成（CA 私钥离线冷存）
  docker-compose.e2e.yaml # 本地端到端演练（MinIO 冒充 R2），不用于部署
  e2e-drill.sh            # 备份→恢复→校验的可重复演练
deploy/docker-compose.phala.redis.yaml    # 三环境共用，差异全走加密 env 注入
deploy/test-redis-cvm-id.txt
deploy/pre-redis-cvm-id.txt
deploy/prod-redis-cvm-id.txt
deploy/verify-redis.sh                    # 连通性冒烟
.github/workflows/redis-deploy.yml
.github/workflows/redis-monitor.yml
tests/test_redis_cvm_config.py            # conf/compose/workflow 静态不变量
tests/test_redis_backup_scripts.py        # 备份/恢复脚本行为（PATH stub + 真 age）
deploy/DEPLOYMENTS.md                     # 新增「TEE Redis」章节 + 首次开通 runbook
docs/CHANGELOG.md                         # landmark 记录
```

**备份逻辑拆成 `backup-push.sh` + `backup-loop.sh` 是刻意的**：前者「一次调用 =
一次备份」且可独立执行，于是测试、手动补推、循环调度走同一条码路，核心路径
不必等一小时才能验证。

### 明确不做（各自另开 spec）

- 任何业务代码接入 Redis
- Python 客户端封装 / 连接池 / 依赖注入接线
- 把 PG 的 `LISTEN/NOTIFY` 或 `SKIP LOCKED` 迁到 Redis
- Redis 高可用（Sentinel / Cluster）——单实例 + 备份，与当前用户规模匹配

### 公开文档同步

按 `CLAUDE.md` 的规则核对：本 spec **不改变**公开 API 契约、信任边界或用户可见行为
（零流量待命）。但它**改变部署拓扑**，因此需要评估 `docs-site/content/docs/` 下的
架构页与自托管信任模型是否需要补充新组件。评估结论写进实施计划的最后一个 Task；
若判定需要改，则同 PR 更新并跑 `npm run types:check` / `lint` / `build`。

---

## 10. 风险与已知限制

| 风险 | 影响 | 缓解 |
|---|---|---|
| **prod 账号余额** | test 的老 CVM 就是在 `sxysun` 账号下余额耗尽被废弃（2026-06-18），导致 app_id 报废、内容钥全换、测试库不可解密行被清空。prod Redis 挂在同一账号下，多一台就多一份烧钱速率 | 开通前确认余额与告警；把 Redis CVM 的月成本记入 `DEPLOYMENTS.md` |
| Redis 端口公网可达 | 只靠 TLS + AUTH 保护 | 32 字节随机口令 + `port 0` + `rename-command` 禁高危命令；与 TEE PG 同一模型 |
| 新增运行时单点 | Redis 挂了会影响所有接入它的路径 | 本 spec 阶段零流量；后续每个接入 spec 必须自带「Redis 不可用时退回 PG 路径」的降级设计 |
| 单实例无 HA | 实例故障需人工恢复 | 与当前用户规模匹配的取舍；RPO ≤1h 由备份保证；HA 留待规模变化后重新评估 |
| age 私钥离线冷存丢失 | 历史备份全部不可解密 | 私钥按双钥托管流程分存（同 PG 的「内容钥 + 备份钥」托管） |
| `noeviction` 内存打满 | 写入报错 | 监控 80% 阈值告警；缓存 key 强制 TTL |
| Redis 8 采用 AGPLv3 | 我们自用不分发，影响有限 | 记录在案；若后续有分发需求可切 Valkey（协议兼容） |

---

## 11. 与现有系统的关系

本 spec 完成后，系统状态是：

- PG 仍然是唯一的权威数据存储与队列/唤醒机制，**行为零变化**
- 三台 Redis 空转待命，有备份、有监控、可恢复
- 任何时候可以直接销毁三台 CVM 而不影响任何现有功能

这是刻意的——把「基础设施可用」与「业务依赖它」两件事在时间上彻底分开，
使得每个后续接入 spec 都能独立评估收益与回退成本。
