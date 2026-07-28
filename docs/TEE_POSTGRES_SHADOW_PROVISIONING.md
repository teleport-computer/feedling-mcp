# TEE Postgres 影子库 —— 开通到部署的可复制流程

> 一台跑在 TEE CVM（dstack / Phala）里的 PostgreSQL，作为主库（RDS）的**明文影子**：
> 主库写入时 best-effort 双写到这里，密文表经 enclave 解密后复制成明文，为「切读 →
> 停 RDS → 拆加解密层」的迁移做准备。本文记录 `feedling-io-db-{test,prod}` 的实际
> 开通流程 + 踩过的坑，参数化以便别的项目参考。
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
`dstack-pha-prod9.phala.network`）、`<ENV>`=`test|prod`、`<IMG_TAG>`=pg 镜像 tag、
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

一整套 `<ENV_PREFIX>_*`（照 test 的 `TEST_*` 命名）：`PG_OWNER/APP/REPLICATOR/MONITORING_DB_PASSWORD`、
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

---

## 3. 关键决策与坑（血泪）

- **独立 AppAuth，绝不复用主 app 合约** → 否则翻主 enclave 内容钥。`--kms phala` 下
  pg CVM 靠默认 KMS 授权、**不需要链上 addComposeHash**（和主 app 不同）。
- **镜像 tag = 完整 40 位 `github.sha`**；镜像环境无关，test/prod 复用同一 tag。
- **WAL-G 可选起步**：`WALG_S3_PREFIX` 不设就不要求备份钥，空库先起来。但
  `archive_mode=on` + archive 失败会让 WAL 不回收 → **装数据前必须接上备份**。
- **redeploy 会替换整份 env**：`--cvm-id` 更新时**所有既有机密都要重带**，只带新增会
  把 PG 密码清空、角色崩。
- **同桶隔离 test/prod**：`s3://<BUCKET>/<ENV>/wal-g`，前缀不可互为父。
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

---

## 4. 停用 / 回滚

- **停双写+回填**：`FEEDLING_TEE_DUAL_WRITE` 置空 → 重部署。主服务不受影响（fail-open）。
- **停 CVM**：`phala cvms stop <CVM_ID>`（数据卷保留）；彻底删要连磁盘一起，注意
  restore 演练成功前 CVM 磁盘是数据唯一副本。
- **重签证书**：用冷存的 `ca.key` 重跑 gen-certs 的 server 证书部分，redeploy 注入新
  `PG_SERVER_CERT_B64/KEY_B64`。
