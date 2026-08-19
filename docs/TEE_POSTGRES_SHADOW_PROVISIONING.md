# TEE Postgres 影子库 —— 开通到部署的可复制流程

> 一台跑在 TEE CVM（dstack / Phala）里的 PostgreSQL，作为主库（RDS）的**明文影子**：
> 主库写入时 best-effort 双写到这里，密文表经 enclave 解密后复制成明文，为「切读 →
> 停 RDS → 拆加解密层」的迁移做准备。本文记录 `feedling-io-db-{test,prod}` 的实际
> 开通流程 + 踩过的坑，并于 2026-07-31 用同一流程开通独立的
> `feedling-io-db-pre`，参数化以便别的项目参考。
>
> 本仓相关构件：`deploy/postgres/`（镜像全家桶）、`deploy/docker-compose.phala.postgres.yaml`
> （test/prod 共用 compose）、`backend/alembic_tee/`（明文 schema）、`backend/tee_shadow/`
> （双写 mirror + reconcile + verify）、`backend/tee_replicator/`（密文→明文复制 worker）、
> `backend/admin/tee_sync_scheduler.py`（in-process 自动同步）。

---

## 0. 架构与连接模型（先理解这个）

- **CVM 内一台 PG**，磁盘加密（TEE），业务表 = 主库明文子集。
- **连接走网关 direct-TLS**：dstack/Phala 网关把容器的 `5432` 暴露成
  `<app_id>-5432s.<gateway-domain>:443`。客户端用 **libpq ≥ 17** 的
  `sslnegotiation=direct`（psycopg-binary 3.3.x 自带 libpq 18）。
  - `sslmode=require`：加密不验服务端证书（省去 CA 分发，起步用这个）。
  - `sslmode=verify-full sslrootcert=<ca.crt>`：验服务端，需把 CA 证书分发到消费方镜像。
- **独立 CVM + 独立身份**：绝不复用主 app 的 AppAuth 合约（否则会翻主 enclave 的钥，
  血泪教训）。用 `--kms phala` 时 Phala 默认 KMS 按部署账号授权，**pg CVM 不需要
  链上 AppAuth**（这点和主 app 不同）。
- **两条同步路径**（都在后端进程内，不是 CI workflow）：
  - **双写（mirror）**：`db.py` 写主库后镜像明文表到影子库，`tee_shadow.mirror` 永不
    raise、失败只计数（fail-open），绝不拖垮主路径。
  - **复制（scheduler + worker）**：`tee_sync_scheduler` 选主单例（advisory-lock），
    reconcile 明文表 + 经 enclave 解密复制密文表，游标驱动、可随时中断重启不丢不重。

---

## 1. 前置条件

- 目标集群网关**暴露 `-<port>s` 透传路由**（Phase 0 spike 先确认；某些节点不暴露，
  direct-TLS 和 stunnel 都会栽在这条）。
- Phala 账号 + `phala` CLI（`phala login` / `phala switch <profile>`）。
- 一个 S3 兼容对象存储（本项目用 Cloudflare R2）做 WAL-G 备份，凭证在 `.env`
  （`R2_ENDPOINT` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`）。
- `feedling-postgres` 镜像已构建并推到 GHCR（见 `deploy/postgres/Dockerfile` +
  `.github/workflows/pg-deploy.yml`；tag = `github.sha` **完整 40 位**）。镜像**环境无关**，
  test/prod 复用同一 tag，差异全在注入的机密。
- 本地 `psycopg`（libpq ≥ 17）用于验证连通。

> 磁盘在创建时定死、事后扩容麻烦 → 一次留够。影子库 ≠ 主库逻辑大小（大表搬 R2 / 明文化
> 后缩水）。本项目 prod 数据 ~700MB、月增 ~400MB → 建议 **50GB**（含 OS/WAL/膨胀，数年跑道）。

---

## 2. 开通流程（逐步，参数化）

约定占位：`<CVM>`=CVM 名（如 `feedling-io-db-prod`）、`<GW>`=网关域名（如
`dstack-pha-prod9.phala.network`）、`<ENV>`=`test|pre|prod`、`<IMG_TAG>`=pg 镜像 tag、
`<BUCKET>`=备份桶。

### 2.1 生成 TLS 证书（独立 CA，CA 私钥冷存）

```bash
bash deploy/postgres/gen-certs.sh <CVM> ./certs
# 输出 PG_SERVER_CERT_B64 / PG_SERVER_KEY_B64（注入 CVM）
# ca.crt 分发给消费方（非机密）；⚠️ ca.key 立刻移到离线冷存，从工作目录删除
```
证书 CN=`<CVM>`、SAN=`*.<GW>`（通配匹配 `<app_id>-5432s.<GW>`）。首次部署前不知道
app_id，SAN 通配就够；日后要 verify-full 再按实际 app_id 重签 server 证书。

### 2.2 生成 4 组角色密码

```bash
for r in OWNER APP REPLICATOR MONITORING; do echo "$r=$(openssl rand -hex 32)"; done
```
**必须用 `openssl rand -hex`**（纯十六进制）——引号 / `$` / 反引号会破坏 ensure-roles 的
SQL 与 compose 环境注入。角色：`feedling_owner`(owner) / `app`(读写业务表,无 DDL) /
`tee_replicator` / `monitoring`(pg_monitor,读不了业务表)。

### 2.3 钉镜像 + `phala deploy` 创建 CVM

```bash
sed 's/feedling-postgres:REPLACE_SHA/feedling-postgres:<IMG_TAG>/' \
  deploy/docker-compose.phala.postgres.yaml > compose.prod.yaml

phala switch <prod-profile>          # 确认身份!别误部署到错账号
phala deploy --name <CVM> --compose compose.prod.yaml \
  --kms phala --instance-type tdx.medium --disk-size 50G \
  -e "PG_OWNER_PASSWORD=…"  -e "APP_DB_PASSWORD=…" \
  -e "REPLICATOR_DB_PASSWORD=…" -e "MONITORING_DB_PASSWORD=…" \
  -e "PG_SERVER_CERT_B64=…" -e "PG_SERVER_KEY_B64=…" \
  --wait
# 记下输出的 CVM ID + App ID
```
**先不注入 WAL-G**（`WALG_S3_PREFIX` 不设 → entrypoint 不要求备份钥 → 空库先健康起来，
无副本风险；备份 2.5 再接）。机密走加密 env 通道，**不烧 compose_hash**（compose 里是
`"${VAR:-}"` 字面）。

### 2.4 验证（health / 连通 / 角色 / schema）

```bash
phala ps <APP_ID>          # 等 feedling-pg-db-1 变 (healthy)（冷 initdb ~30-90s）
```
direct-TLS 连通 + 角色 + 应用 schema（用 owner 跑 alembic_tee）：
```bash
# 连通（psycopg，libpq≥17）
host=<APP_ID>-5432s.<GW> port=443 dbname=feedling user=feedling_owner password=…
  sslmode=verify-full sslrootcert=./certs/ca.crt sslnegotiation=direct
# 应用明文 schema（SQLAlchemy 要 URL 形式,psycopg3 驱动）
export TEE_MIGRATION_DATABASE_URL="postgresql+psycopg://feedling_owner:<PW>@<APP_ID>-5432s.<GW>:443/feedling?sslmode=verify-full&sslnegotiation=direct&sslrootcert=<url-encoded ca 路径>"
python3 -c "from alembic_tee import upgrade_head; upgrade_head()"
```
验收清单：`archive_mode=on`、`max_connections` 按容量公式、4 角色齐全、`app` 能读业务表、
`monitoring` 读业务表被拒（负向权限）、`public` 表数 = alembic_tee 全量（本项目 20 张，
版本表叫 **`alembic_tee_version`** 不是 `alembic_version`）。

### 2.5 接 WAL-G 备份（原地 redeploy，不重建）

```bash
# 备份钥(prod 专属) + 前缀(同桶,加 <ENV> 路径层隔离 test/prod)
WALG_KEY=$(openssl rand -hex 32)
phala deploy --cvm-id <CVM_ID> --compose compose.prod.yaml \
  -e "PG_OWNER_PASSWORD=…" … （2.3 全套 PG 机密都要重带,否则会被清空!） \
  -e "WALG_S3_PREFIX=s3://<BUCKET>/<ENV>/wal-g" \
  -e "WALG_LIBSODIUM_KEY=$WALG_KEY" \
  -e "PG_BACKUP_R2_ENDPOINT=$R2_ENDPOINT" \
  -e "PG_BACKUP_R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
  -e "PG_BACKUP_R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY"
```
重启后 entrypoint 自动跑首次 base backup。**验证 R2 真有对象**（别信日志）：
`s3://<BUCKET>/<ENV>/wal-g/basebackups_005/…` + `wal_005/*.lz4`（lz4 压缩 + libsodium 加密）。
开通前跑一次 **restore 演练**（`deploy/postgres/restore.sh`）确认备份可用。

### 2.6 机密入库（GitHub Secrets）

一整套 `<ENV_PREFIX>_*`（照 test 的 `TEST_*` 命名，pre 使用 `PRE_*`）：`PG_OWNER/APP/REPLICATOR/MONITORING_DB_PASSWORD`、
`PG_SERVER_CERT_B64/KEY_B64`、`WALG_S3_PREFIX/LIBSODIUM_KEY`、`PG_BACKUP_R2_ENDPOINT/ACCESS_KEY_ID/SECRET_ACCESS_KEY`、
`TEE_DATABASE_URL`（app 角色 DSN）、`FEEDLING_TEE_DUAL_WRITE`、`PHALA_CLOUD_API_KEY`。
`gh secret set <NAME> --repo <owner>/<repo>`（值从变量引用、别回显）。CVM ID 写进
`deploy/<env>-pg-cvm-id.txt`（pg-deploy workflow fail-closed 需要）。

### 2.7 接后端双写（compose + CI 注入）

后端 compose 的 backend service 加两个 env（加密注入、不烧 compose_hash）：
```yaml
TEE_DATABASE_URL: "${TEE_DATABASE_URL:-}"
FEEDLING_TEE_DUAL_WRITE: "${FEEDLING_TEE_DUAL_WRITE:-}"
```
CI 部署步骤把 `<ENV_PREFIX>_TEE_DATABASE_URL` / `_FEEDLING_TEE_DUAL_WRITE` 经 `-e` 注入。
两个都空 = 双写 OFF（`mirror.enabled()` 需 `FEEDLING_TEE_DUAL_WRITE=1` 且
`TEE_DATABASE_URL` 非空）。DSN 用 **app 角色**（无 DDL）+ `sslmode=require`（起步；
日后加 CA 分发再升 verify-full）。

### 2.8 开双写 + 回填 + 盯健康

设 `FEEDLING_TEE_DUAL_WRITE=1` → 下次部署即开。开后：
- 双写立刻镜像**新写入**（fail-open，旁路）。
- **in-process 调度器随即启动全量回填**（gated on `FEEDLING_ASGI_BACKGROUND` +
  `mirror.enabled()`，**无 prod/Phase8 门**）：reconcile 明文表 + 经 **enclave** 解密
  复制密文表（qps 限流）。首轮是**小时级**、打 enclave。
- **盯**：`GET /v1/admin/tee-replication/status` 看 `dual_write_enabled` / `health` /
  游标推进 / `tee_sync_runs`（`replicate_errors` / `replicate_table_failures` / `duration_ms`）；
  同时看主服务 5xx 率 + enclave 是否被争用。**kill switch**：`FEEDLING_TEE_DUAL_WRITE`
  置空 + 重部署 → 双写和回填立即停（不影响主服务）。

### 2.9 迁移落地通道（alembic_tee revision 上线）

在 Phase 4 strict verify 前若发现历史游标已经越过缺失行，使用 `TEE replicate`
workflow 的受保护恢复动作，不要手工改 `tee_replication_cursors`：

1. `action=reflow` 仅支持 `chat_messages`、`memory_moments`、
   `world_book_entries`、`identity`、`voice_transcripts`。先保持
   `dry_run=true` 查看 `would_copy`、`pending_cleared`、`orphan_pending`、
   `stale` 和 `failures`；真跑时关闭 dry-run、填写 `confirm=MIGRATE`，可用
   `qps` 限速（`0` 表示不额外 sleep）。reflow 从真实下界扫描但不回退持久游标，
   成功 upsert 与旧 pending 清理同事务；源行不存在的 pending 只在确认缺失后删除。
2. `action=prune` 用于只删除 TEE 中源库已不存在的孤儿行，例如
   `v2_trajectory_events`。先 dry-run 记录 `stale=N`；若 N 超过默认安全阈值，apply
   必须同时填写同一个 `expected_stale=N`。服务端会重新按“先 TEE 快照、后 RDS
   快照”计算差集，实时数不再等于 N 就拒绝整次删除。
3. 任一响应 `ok=false`、`failures>0`、`refused` 非空或 `prune_error` 非空，都表示
   未收敛；操作幂等，可排障后用新的 dry-run 结果重试。dry-run 不写 RDS、TEE，
   包括不落持久错误日志。

终态 pending 若源记录仍在，代表设备专有密文或确定性坏信封；reflow 不会伪装成
成功并删掉标记，仍需设备重传或单独的数据处置决策。

alembic_tee 曾经**无 CI 钩子、纯人工执行**——0002/0003 合并后从未在 test/prod 实库
跑过，两库停在 0001 直到 2026-07-27 才发现（见 Task 0.6）。现在有了固定通道：

```bash
gh workflow run "TEE migrate" -f environment=test -f confirm=MIGRATE-TEE
# prod 需要 confirm=MIGRATE-TEE-PROD（typo guard，防误触）
```

`.github/workflows/tee-migrate.yml`（`workflow_dispatch`，仿 `pg-deploy.yml` 的
typo-guard 模式）：
- test 跑 `test` 分支、prod 跑 `main`——与 app 发布流向一致。
- 在 **GitHub runner**（公网）上直连 TEE，用 **owner 角色**
  `TEE_MIGRATION_DATABASE_URL`（CVM 里的 backend 只有 app 角色，没有 DDL 权限，
  所以不能走 `tee-replicate.yml` 那种「admin 端点遥控 CVM 内进程」的模式）。
- 连接**强制 `sslmode=verify-full` + CA**（`<ENV>_TEE_PG_CA_PEM` secret），不照抄
  生产 backend 的 `sslmode=require`——那是因为 backend 与 TEE 同在 Phala 内网且只有
  无 DDL 的 app 角色；这里是公网 + owner 角色执行 DDL，必须验证服务端身份。
- 两套环境的机密（`TEST_*`/`PROD_*`）**都注入，在 shell 里按 `environment` 挑**，
  绝不用 GitHub 表达式 `${{ environment == 'prod' && secrets.PROD_X || secrets.TEST_X }}`
  ——GH 的 `&&`/`||` 是 JS 语义（空串是 falsy），`PROD_X` 恰好为空时会静默 fallback 到
  `TEST_X`，一次标记为 prod 的 dispatch 会实际跑在 test 库上而 job 仍然绿灯（`pg-deploy.yml`
  已有过同款教训：注进 prod CVM 的却是 test 的密码）。按环境在 shell 里挑错只会挑到
  空值，`test -n "$DSN"` 会 fail-closed。
- **最后一步强制断言** `alembic_tee_version == 代码里 ScriptDirectory 的 head`——
  这条 assert 就是为了根治「revision 合并了但从未在实库执行」这类问题，对不上直接红。

2026-07-27 全量对齐落地时，Step 7.1（prod）已经在本地手动跑完（先于本通道成型），
两个 TEE 实库都已 `0001→0004`、各 54 张表；日后的新 revision 一律走这个通道，不再
手动执行。

> ⚠️ **通道写好 ≠ 通道能用。** `tee-migrate.yml` 依赖 4 个 repo secret
> （`{TEST,PROD}_TEE_MIGRATION_DATABASE_URL`、`{TEST,PROD}_TEE_PG_CA_PEM`），
> **截至 2026-07-29 这 4 个还没建**，所以 workflow 目前跑不起来，`alembic_tee`
> 实际仍是手工执行。
>
> 这个缺口已经吃过一次：`0007_chat_activity_snapshot` 在 07-29 随新功能合进
> `test`，登记、迁移、SKIP 判定全都写对了，但**没有人执行它**。TEE 库停在 0006，
> `chat_turn_activity_events` 在 TEE 侧根本不存在，snapshot lane 每个 tick 报一次
> `两侧无公共列，拒绝整表清空`（护栏正确拦住了整表清空，没误删数据），一直到
> 巡检时才发现。同一批里 `model_api_routes` 的 4 个 vision 列更隐蔽——见 §3 的
> 「加列漂移没有红灯」。

**写了 alembic_tee revision 之后，必须做的一步**（secret 建好之前）：

```bash
# 本地手工执行（owner 凭证，libpq≥17 才支持 direct-TLS）
cd backend
export TEE_MIGRATION_DATABASE_URL="postgresql://feedling_owner:<PW>@<APP_ID>-5432s.dstack-pha-prod9.phala.network:443/feedling?sslmode=verify-full&sslnegotiation=direct&sslrootcert=<CA>"
python -c "import alembic_tee; alembic_tee.upgrade_head()"
```

上真库前先在临时库演练整条链，这一步能挡住绝大多数低级错误：

```bash
psql "postgresql://postgres:test@127.0.0.1:55432/postgres" -c "CREATE DATABASE tee_dryrun;"
TEE_MIGRATION_DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/tee_dryrun" \
  python -c "import alembic_tee; alembic_tee.upgrade_head()"
```

**新建表的话，跑完要验角色权限**——`ALTER DEFAULT PRIVILEGES` 只对配过的角色生效：

```sql
SELECT p.priv, has_table_privilege('app','<新表>',p.priv)
FROM (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE')) p(priv);
```

`app` 五项应当全 `t`（2026-07-29 实测 `chat_turn_activity_events` 在两库都自动继承，
说明 `ensure-roles.sh` 的 default privileges 对新表生效）。⚠️ 同一次实测顺带发现
**`monitoring` 角色对 55 张表全部零权限**——`pg_default_acl` 里只有 `app` 和
`tee_replicator`，从来没配过 `monitoring`，这个"只读角色"实际是废的。不是新表的
问题，是既有状态，尚未处理。

**迁移落地后核对 SNAPSHOT/新密文表的收敛情况**：`verify.run()` 对这些新 lane 用的
是 advisory 判据（只抓"RDS 有行、TEE 一行都没有"，见 `tee_shadow/verify.py` 的
`_rows_ok_advisory`），`verify_ok=true` 不等于两侧行数逐 tick 精确相等——那个更严格
的结果存在每张表报告的 `strict_rows_ok` 字段里，只进日志和 `tee_sync_runs.report`
的 JSONB，没有专门的扁平列。排障时（比如怀疑某张 SNAPSHOT 表的 TRUNCATE+COPY 一直
没生效、TEE 侧留着孤儿行）用这条 SQL 从最近一次**真正跑过 verify 的** tick 的
JSONB 里捞出 strict 判据为假的表。

⚠️ `WHERE` 里的 `verify_ran` 不能省：sync tick 每 `FEEDLING_TEE_SYNC_INTERVAL_SEC`
（默认 300s）落一行，而 verify 只在 reconcile tick 才跑（`FEEDLING_TEE_RECONCILE_INTERVAL_SEC`
默认 86400s），即约 288 行里只有 1 行的 `report` 带 `'verify'` 键。按 `max(ran_at)`
取最近一行，绝大多数时候 `report->'verify'->'tables'` 是 NULL，`jsonb_each(NULL)`
静默返回 0 行——查询不报错、看着像"全绿"，正是这套改动一直在防的那个病。

```sql
SELECT ran_at, t.key AS table_name,
       t.value->>'rds_rows' AS rds_rows,
       t.value->>'tee_rows' AS tee_rows,
       t.value->>'strict_rows_ok' AS strict_rows_ok
FROM tee_sync_runs, jsonb_each(report->'verify'->'tables') AS t(key, value)
WHERE ran_at = (SELECT max(ran_at) FROM tee_sync_runs WHERE verify_ran)
  AND (t.value->>'strict_rows_ok') = 'false'
ORDER BY table_name;
```

`verify.run()` 的返回值也带了一个顶层 `strict_ok`（全表严格判据是否全过）和一行
`[verify]` 日志里的 `strict_fail=[...]` 列表，二者与上面这条 SQL 是同一份数据的
不同视角，任选其一即可。

### 2.10 pre 实例（2026-07-31）

- CVM：`feedling-io-db-pre`，UUID `dc5c8593-0e44-43a9-b018-fe0431ff44d5`，
  App ID `ade3cabf133ec3e9ee6220265843c4ac993e1e63`。
- 拓扑：prod9 node 18，`tdx.medium`（2 vCPU / 4GB），30GB ZFS；不要省略
  `--node-id 18`，否则 Phala 默认调度可能落到 prod7，导致 prod9 SAN 和直连域名失配。
- 备份：`s3://io-in-enclave-db/pre/wal-g`，独立 libsodium key；首次 base backup 与
  强制 WAL switch 已验证，`archived_count=1`、`failed_count=0`。
- schema：`0009_provider_latency`，55 张 public 表；app/replicator 的 CRUD +
  TRUNCATE 为 55/55，monitoring 读取业务表被拒。
- 日常部署、迁移和监控分别走 `pg-deploy.yml`、`tee-migrate.yml`、
  `pg-monitor.yml` 的 pre lane。应用双写在所有验证完成前保持关闭。

---

## 3. 关键决策与坑（血泪）

- **独立 AppAuth，绝不复用主 app 合约** → 否则翻主 enclave 内容钥。`--kms phala` 下
  pg CVM 靠默认 KMS 授权、**不需要链上 addComposeHash**（和主 app 不同）。
- **镜像 tag = 完整 40 位 `github.sha`**；镜像环境无关，test/pre/prod 复用同一 tag。
- **WAL-G 可选起步**：`WALG_S3_PREFIX` 不设就不要求备份钥，空库先起来。但
  `archive_mode=on` + archive 失败会让 WAL 不回收 → **装数据前必须接上备份**。
- **redeploy 会替换整份 env**：`--cvm-id` 更新时**所有既有机密都要重带**，只带新增会
  把 PG 密码清空、角色崩。
- **同桶隔离 test/pre/prod**：`s3://<BUCKET>/<ENV>/wal-g`，前缀不可互为父。
- **连接池陈旧 → SSL eof**：网关会静默掐断空闲连接；`min_size` 越大常驻热连接越多、
  越易变陈，下次大写（chat 行最大）撞死连接报 `unexpected eof` / `connection is lost`。
  修法（见 `tee_shadow/mirror.py` + `tee_replicator/worker.py`）：池 `max_lifetime`
  主动回收 + TCP keepalive + worker 遇连接断**换新连接重试整批**（区别于毒行逐行跳）。
- **毒行**：解密出的明文含 NUL 等 PG 不接受的内容 → 批写失败降级逐行、跳过毒行
  （计 `skipped`），别让一行拖垮整表；NUL 在 transform 阶段递归 scrub。
- **复制不是 workflow**：`tee-replicate.yml` 是 `workflow_dispatch` 手动工具（test-only,
  Phase 8 才加 prod）；真正的自动同步是 **in-process 调度器**，别把两者搞混。
- **首个 tick 慢/游标 quirk**：调度器首 tick 用 `monotonic()` 判是否 reconcile，宿主
  uptime < reconcile 间隔时首 tick 不 reconcile；首轮回填大表（`user_logs` / `chat`）
  是小时级，`tee_sync_runs` 迟迟不落行 ≠ 没在跑（游标推进才是判据）。
- **验证走真信号**：备份看 R2 对象、复制看游标 `updated_at`、健康看 5xx 率——别只信
  日志/心跳。
- **`TRUNCATE` 是独立权限，不含在 DML 四件套里**（2026-07-28 实测）：SNAPSHOT lane
  用 `TRUNCATE + COPY` 做整表原子替换，而 `ensure-roles.sh` 原先只授
  `SELECT, INSERT, UPDATE, DELETE` → 27 张表全数失败，报的是
  `permission denied for table X`。**排查时特别容易误判**：
  `has_table_privilege(role, tbl, 'INSERT')` 查出来全绿，只有 `TRUNCATE` 那一项是 0，
  逐权限查才看得见：
  ```sql
  select r, p, count(*) filter (where has_table_privilege(r,'public.'||tablename,p))
  from pg_tables, unnest(array['app','tee_replicator']) r,
       unnest(array['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p
  where schemaname='public' group by r,p order by r,p;
  ```
  本地 pytest 跑的是 `postgres` 超级用户，**这类角色权限缺口在本地永远绿**，只有真
  环境才暴露。`ensure-roles.sh` 已补 `TRUNCATE`（含 `ALTER DEFAULT PRIVILEGES`），但
  它只在 PG CVM 启动时跑——**给既有库补权限要直接连库执行 GRANT**，不能等重部署。
- **列漂移会让整张表永久失败**（2026-07-28 实测）：`COPY (FORMAT BINARY)` 按列位置
  严格匹配，两侧列集差一列就报 `row field count is N, expected M`，而且是**每个 tick
  都失败**。两种来源都是常态、不是异常：①滚动部署时间窗（新列先落 RDS，TEE 的
  `alembic_tee` 还没跟上）；②环境自身的历史残留列（test RDS 的
  `model_api_routes.thinking_fallback` 全仓 grep 零命中，没有任何代码创建它）。
  `snapshot.py` 现在用两侧列集的**交集**做 COPY，差异逐列报进
  `tee_sync_runs.report` 的 `missing_in_tee` / `missing_in_rds`，并对
  `missing_in_tee` 打 `log.warning`。**排查列漂移看这两个字段**：
  ```sql
  select ran_at, t->>'table', t->'missing_in_tee', t->'missing_in_rds'
  from tee_sync_runs, jsonb_array_elements(report->'snapshot') t
  where jsonb_array_length(coalesce(t->'missing_in_tee','[]'::jsonb)) > 0
     or jsonb_array_length(coalesce(t->'missing_in_rds','[]'::jsonb)) > 0
  order by ran_at desc limit 20;
  ```
  `missing_in_tee` 非空 = **有一列的数据没在同步**，该补 `alembic_tee` revision；
  除非那列是某个环境长歪的产物（如上面的 `thinking_fallback`），那就该让它一直报着，
  TEE 不跟着歪。
- **⚠️ 交集 COPY 修好了「整表永久失败」，代价是加列漂移从此没有红灯**
  （2026-07-29 实测）。上一条描述的 `row field count is N, expected M` 是**修复前**的
  行为；交集逻辑落地后，两类漂移的可见性天差地别：

  | 漂移 | 表现 | 可见性 |
  |---|---|---|
  | RDS 新建表、TEE 没有 | `not common` → `snapshot_failures = 1`，每 tick 一次 | **有红灯** |
  | RDS 加列、TEE 没有 | 交集照常 COPY，`ok: true` | **只在 `missing_in_tee` 里** |

  实例：RDS `0066` 给 `model_api_routes` 加了 4 个 vision 列，TEE 没跟上。整表一直在
  同步、行数一直是 27、`snapshot_failures` 一直是 0、CI 全绿——只有那 4 列的数据静静
  地没进 TEE，潜伏到巡检才发现（修法 `alembic_tee 0008`）。**加列不建表就撞不上
  「无公共列」护栏，在失败计数和 CI 上都是静默的。`missing_in_tee` 是这类漂移唯一的
  信号，必须有人定期看**——上面那条 SQL 应当进值班巡检，而不是只在排障时才跑。
  （建表漂移不需要靠它：`snapshot_failures` 会一直响。）

---

## 4. 停用 / 回滚

- **停双写+回填**：`FEEDLING_TEE_DUAL_WRITE` 置空 → 重部署。主服务不受影响（fail-open）。
- **停 CVM**：`phala cvms stop <CVM_ID>`（数据卷保留）；彻底删要连磁盘一起，注意
  restore 演练成功前 CVM 磁盘是数据唯一副本。
- **重签证书**：用冷存的 `ca.key` 重跑 gen-certs 的 server 证书部分，redeploy 注入新
  `PG_SERVER_CERT_B64/KEY_B64`。

## 5. 恢复演练与 RTO（2026-07-28 首次实测，test 环境）

> 对应主计划 `docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
> Phase 0 Task 0.3。**演练结论：备份链可恢复、RPO≈0，但 `restore.sh` 现状下无法
> 一把跑通**——三处需要人工干预（见 §5.3），扶正为唯一主库前应先修 §5.3 第 1 条。

### 5.1 演练方法

test 备份 → 本机一次性容器（**非** TEE 内，只验证备份可恢复性）：

```bash
docker run -d --name feedling-restore-drill --entrypoint /bin/bash \
  -e WALG_LIBSODIUM_KEY=… -e WALG_S3_PREFIX=… \
  -e AWS_ENDPOINT=… -e AWS_ACCESS_KEY_ID=… -e AWS_SECRET_ACCESS_KEY=… \
  -e AWS_S3_FORCE_PATH_STYLE=true -e AWS_REGION=auto \
  -e PGDATA=/var/lib/postgresql/data \
  ghcr.io/teleport-computer/feedling-postgres:<prod 同款 tag> -c 'sleep infinity'
docker exec -u postgres feedling-restore-drill restore.sh          # 取 LATEST
docker exec -u postgres feedling-restore-drill \
  /usr/lib/postgresql/17/bin/pg_ctl -D "$PGDATA" -l /tmp/pg.log start -w
```

机密取自 `~/documents/teleport/feedling-pg-test-secrets.txt` 的
`TEST_WALG_*` / `TEST_PG_BACKUP_R2_*`（注意有 `TEST_` 前缀，且 compose 侧变量名是
`PG_BACKUP_R2_*` → 容器内要映射成 wal-g 认的 `AWS_*`）。

### 5.2 RTO / RPO 实测

| 阶段 | 耗时 | 说明 |
|---|---|---|
| `wal-g backup-fetch`（130M） | **141s** | 从 R2 拉最新 base backup + 解密解压 |
| WAL 回放至 promote | **240s** | PG 自报 `redo done … elapsed: 240.37 s`，跨 ~1 天 WAL |
| 启动 + 参数修正 | ~30s | 含 §5.3 的人工干预 |
| **RTO 合计** | **≈ 7 分钟** | ⚠️ 本机为 arm64 跑 amd64 镜像（Rosetta 模拟），**属上限**；原生 x86 应更快 |
| **RPO** | **≈ 0** | 恢复点 `13:34:36 UTC`，演练启动为 `13:25` —— WAL 归档及时，几乎无数据损失 |

### 5.3 演练发现的三个卡点（都需人工干预，建议修掉）

1. **⚠️ `max_connections` 不足会让回放直接 FATAL（唯一的真缺陷，应修）**

   ```
   FATAL:  recovery aborted because of insufficient parameter settings
   DETAIL: max_connections = 100 is a lower setting than on the primary server,
           where its value was 400.
   ```

   线上 `max_connections=400` 是**部署参数注入的，不在备份的 `postgresql.conf` 里**，
   恢复端起来是默认 100 → PG 拒绝回放。演练中靠手工往 `postgresql.conf` 追加
   `max_connections = 400` 才继续。**建议 `restore.sh` 在写 recovery 配置那段
   一并写入 `max_connections`（以及同类的 `max_worker_processes` /
   `max_prepared_transactions` / `max_locks_per_transaction`，PG 对这些都有
   "≥ primary" 的硬要求）**，否则真出事时会在这里卡住。

2. **`pg_ctl` 不在默认 PATH**：交互式 `docker exec` 拿不到 `/usr/lib/postgresql/17/bin`，
   直接敲 `pg_ctl` 得到 `exit 127`。必须用绝对路径。（与 §3 记的 cron PATH 坑同源。）

3. **恢复出来的实例没有 `postgres` 角色**：备份来自 TEE 库，角色是
   `feedling_owner` / `app` / `monitoring`。用 `psql -U postgres` 会得到
   `FATAL: role "postgres" does not exist`；本地 socket 要用
   `psql -U feedling_owner -d feedling`。**写恢复脚本/健康探针时别默认 postgres 角色。**

### 5.4 一致性核对结果

| 核对项 | 实库（test） | 恢复库 | 结论 |
|---|---|---|---|
| `alembic_tee_version` | `0005_snapshot_column_catchup` | 同 | ✅ 一致 |
| public 表数 | 54 | 54 | ✅ 一致 |
| 逐表行数 | — | — | ✅ **50/54 逐行一致** |

有差异的 4 张全部是持续写入表，差量与「实库仍在写、恢复库停在 13:34:36」完全吻合，
非数据丢失：`user_logs`(16123→16082)、`v2_worker_heartbeats`(130→128)、
`agent_runtime_supervisor_heartbeats`(4→3)、`chat_r2_lifecycle`(227→226)。
全部用户内容表（`chat_messages` 1097、`memory_moments` 133、`users` 69、`frames` 115
等）逐行一致。

### 5.5 收尾

演练容器是一次性的，核完即删：`docker rm -f feedling-restore-drill`。
**不要**把演练容器留着——它持有可解密备份的 `WALG_LIBSODIUM_KEY`。

## 6. 本地原地重部署（机密轮换用；2026-07-29 test 实测通过）

轮换 R2 / WAL-G 机密**不必走 GitHub Actions**——本地 `phala deploy` 直接注入即可
（`pg-deploy.yml` 的本地等价物）。test 上完整跑通一次，**服务中断 < 1 分钟**
（容器 `Exited (0)` → `Up (healthy)`，两次 15s 轮询之内）。

### 6.1 四个必须守住的前提

1. **镜像 tag 抄现役值，绝不抄旧的**。compose 里是 `REPLACE_SHA` 占位符，本地部署
   要 sed 成 CVM 内 `docker ps` 显示的现役 tag（test 与 prod 的 tag **不同**）。
   2026-07-24 base backup 全断就是重部署抄了旧 tag、丢掉 PATH 修复。替换后要断言
   无 `REPLACE_SHA` 残留。
2. **整份机密重带（11 个）**：原地更新漏一个就会被清空 → entrypoint fail-closed
   起不来。部署前断言「11 行、无空值、无换行破坏」（证书是单行 base64，约
   1579 / 2272 字符；`WALG_LIBSODIUM_KEY` 恰 64 hex）。
3. **机密只落 0600 临时文件**，用 `phala deploy -e` 读，不拼进命令行（`ps`/日志
   里不留明文）。
4. **先确认 phala profile 与 CVM 身份**。test 在 `amiller-users-projects`、prod 在
   `sxysuns-projects`；`phala switch` 可能没生效（实测踩到过停在 prod profile）。
   脚本里把目标 uuid **写死**、并用 `phala cvms get <uuid>` 核对回显的 Name/App ID
   再动手——不要依赖「当前选中的 profile 是对的」。

### 6.2 命令

```bash
sed -E "s|feedling-postgres:REPLACE_SHA|feedling-postgres:${TAG}|" \
  deploy/docker-compose.phala.postgres.yaml > /tmp/compose.yaml
# 11 个机密写进 0600 的 /tmp/env（去掉机密文件里的 TEST_/PROD_ 前缀）
phala deploy --cvm-id "$CVM_UUID" -c /tmp/compose.yaml -e /tmp/env
```

本地**不加** `--wait`（会假报超时），改为轮询 `phala ps <cvm>` 等 `healthy`。

### 6.3 部署后验证清单（缺一不可）

| 检查 | 命令 | 通过判据 |
|---|---|---|
| 容器恢复 | `phala ps <cvm>` | `Up … (healthy)`，且**镜像 tag 未变** |
| **WAL 归档**（最关键） | `select * from pg_stat_archiver` | `last_archived_time` 是**部署之后**的时刻，`failed_count` 无新增 |
| 备份链 | `wal-g backup-list` | 最新 base backup 与部署前一致，链未断 |
| 库可用 | 连库查 | 表数、`alembic_tee_version`、`max_connections` 与部署前基线一致 |

⚠️ `pg_stat_archiver` 是判断「机密注入是否正确」的**硬证据**——容器 healthy 只说明
postgres 起来了，不代表 wal-g 拿到了可用的 R2 凭证。2026-07-24 那次就是容器健康、
归档却在静默失败。轮换机密后必须看这一项。
