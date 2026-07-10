# TEE Postgres Phase 0–1（spike + pg CVM 基建）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建起 `feedling-pg-test`（TEE 内明文 Postgres CVM），带完整备份/监控/角色/部署护栏，产出可用的 `TEE_DATABASE_URL`，为 Phase 2–3（双写+解密复制）铺路。

**Architecture:** 独立 pg CVM（postgres:17 direct TLS + WAL-G→R2 连续归档），消费方经 prod9 gateway `-5432s` TLS 透传直连（本仓已有 `-5003s` 活证）。所有基建产物照搬 hivemind-core 并落实参考文档四修正 + spec 全部【补充】。

**Tech Stack:** postgres:17（digest pin）、WAL-G（v3.0.8 起步，须核验 PG17）、Phala dstack（prod9, `phala@1.1.19`）、Cloudflare R2、GitHub Actions（手动 deploy + cron 监控）。

**Spec:** `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md`（以下简称 spec）。hivemind 源文件在 `~/Projects/teleport/hivemind-core/deploy/postgres/`，**以 spec 和本 plan 为准，源文件里 fail-open 的分支一律不抄**。

## Global Constraints

- **实施分支基线 = `test`**（ASGI 已合入 test；不要基于 main）。
- pg CVM 用**独立 AppAuth 合约**，绝不与主 app / runner 共享（0a5578→2d642e 换钥事故）。
- compose env 一律 `${VAR:-}` 形态（本仓约定：值走 phala 加密 env，不烧进 compose_hash，见 `deploy/docker-compose.phala.yaml:184` 注释）；**必填校验在 entrypoint 里 fail-closed**，不用 `${VAR:?}`。
- `WALG_S3_PREFIX` 已设而 `WALG_LIBSODIUM_KEY` 未设 → entrypoint exit 1（fail-closed，不许可选）。
- pg CVM **不进** ci.yml 的 merge 自动部署（paths-filter 不加 pg compose）；只有手动 `workflow_dispatch`。
- 镜像 digest pin（attestation 需要可复现 compose_hash）。
- gateway 透传路由端口：客户端连 `<pg-app-id>-5432s.dstack-pha-prod9.phala.network` 的 **443 端口**（gateway 对外恒为 443，转发到容器 5432）。
- 测试环境 Phala 账号 = `amiller-user`（`TEST_PHALA_CLOUD_API_KEY`）；R2 备份桶独立（`feedling-pg-backups`，凭证与 io-user-logs/frames 分开）。
- 提交遵循用户规则：**每个 Task 末尾的 commit 步骤需用户明确授权后才执行**；未授权则停在 working tree。

---

### Task 0: Phase 0 网络 spike（手动 runbook，半天）

**Files:**
- Create: `deploy/postgres/spike/docker-compose.spike.yaml`
- Create: `deploy/postgres/spike/soak_listen_notify.py`
- Modify: `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md`（回填 spike 结果）

**Interfaces:**
- Produces: 定案的 `TEE_DATABASE_URL` 形态（host/port/TLS 参数）、direct TLS vs stunnel 决策、LISTEN/NOTIFY keepalive 参数 —— Task 2/3 的 compose 与 Plan 2 的 db.py 连接参数都消费它。

- [ ] **Step 1: 先验证 s 路由活证（不花钱的 sanity check）**

```bash
# 主 test CVM 的 enclave -5003s 透传路由（自签证书，-k 必须）
curl -skv https://173c7f49aeb54acb424676b17b17f78e5e2b2938-5003s.dstack-pha-prod9.phala.network/attestation -o /dev/null 2>&1 | grep -E "SSL connection|subject"
```
Expected: TLS 握手成功（self-signed subject 可见）。这证明 prod9 对本账号暴露 `s` 透传；若失败，停下重评（spec §2 风险升级路径）。

- [ ] **Step 2: 写 spike compose**

```yaml
# deploy/postgres/spike/docker-compose.spike.yaml — 一次性 spike，用完删 CVM
name: feedling-pg-spike
services:
  db:
    image: postgres:17
    entrypoint:
      - bash
      - -c
      - |
        set -e
        mkdir -p /etc/pgtls
        openssl req -new -x509 -days 30 -nodes -text \
          -out /etc/pgtls/server.crt -keyout /etc/pgtls/server.key \
          -subj "/CN=feedling-pg-spike"
        chown postgres:postgres /etc/pgtls/server.key && chmod 600 /etc/pgtls/server.key
        exec docker-entrypoint.sh postgres \
          -c listen_addresses='*' \
          -c ssl=on \
          -c ssl_cert_file=/etc/pgtls/server.crt \
          -c ssl_key_file=/etc/pgtls/server.key
    environment:
      POSTGRES_DB: spike
      POSTGRES_USER: spike
      POSTGRES_PASSWORD: "${SPIKE_DB_PASS:-}"
    ports:
      - "5432:5432"
```

- [ ] **Step 3: 部署 spike CVM（test 账号）**

```bash
export PHALA_KEY=<TEST_PHALA_CLOUD_API_KEY>
phala deploy --api-token "$PHALA_KEY" --name feedling-pg-spike \
  --instance-type tdx.small --kms phala \
  -c deploy/postgres/spike/docker-compose.spike.yaml \
  -e "SPIKE_DB_PASS=$(openssl rand -hex 16)" --wait
phala cvms list --api-token "$PHALA_KEY" --json | jq -r '.[] | select(.name=="feedling-pg-spike") | .app_id'
```
Expected: 输出 spike 的 app_id（下面记为 `$APP`）。

- [ ] **Step 4: direct TLS 连接实测（核心验证）**

需要 libpq ≥17 的 psql（mac: `brew install libpq`，用 `$(brew --prefix libpq)/bin/psql`）：

```bash
psql "host=${APP}-5432s.dstack-pha-prod9.phala.network port=443 \
  dbname=spike user=spike password=<SPIKE_DB_PASS> \
  sslmode=require sslnegotiation=direct" -c "SELECT version();"
```
Expected: `PostgreSQL 17.x ...`。若握手失败（gateway 不认 ALPN/SNI），换 stunnel 方案再测：pg CVM 内加 stunnel server（accept 5433 → connect 5432），客户端 `stunnel` client 配 `sni = ${APP}-5433s...`，psql 连 localhost。**记录哪条路通**。

- [ ] **Step 5: LISTEN/NOTIFY 长连接 soak（wake bus 的命根）**

```python
# deploy/postgres/spike/soak_listen_notify.py
# 用法: DSN="host=... port=443 ... sslnegotiation=direct" python soak_listen_notify.py 3600
import os, sys, time
import psycopg

dsn, duration = os.environ["DSN"], int(sys.argv[1] if len(sys.argv) > 1 else 3600)
listener = psycopg.connect(dsn, autocommit=True)
listener.execute("LISTEN spike_chan")
sender = psycopg.connect(dsn, autocommit=True)
start, last_ok = time.time(), time.time()
while time.time() - start < duration:
    sender.execute("NOTIFY spike_chan, 'ping'")
    got = None
    for n in listener.notifies(timeout=10):
        got = n
        break
    now = time.time()
    if got is None:
        print(f"[{now-start:7.0f}s] LOST after {now-last_ok:.0f}s idle-ok", flush=True)
        sys.exit(1)
    last_ok = now
    print(f"[{now-start:7.0f}s] ok", flush=True)
    time.sleep(120)  # 2min 间隔试探空闲超时
print("SOAK PASS", flush=True)
```

Run: `DSN="host=${APP}-5432s... port=443 dbname=spike user=spike password=... sslmode=require sslnegotiation=direct" python deploy/postgres/spike/soak_listen_notify.py 3600`
Expected: 1 小时无 LOST。若在某个间隔被掐，记录空闲阈值 → Plan 2 的 wake bus 连接要配 `keepalives_idle` 低于该值。

- [ ] **Step 6: RTT 对比 + 记录结果**

```bash
psql "$DSN" -c '\timing' -c 'SELECT 1;' -c 'SELECT 1;' -c 'SELECT 1;'
# 对照现 RDS：psql "$DATABASE_URL" 同样跑三次
```
把结果（URL 形态、TLS 参数、keepalive 阈值、RTT、direct-vs-stunnel 决策）回填进 spec §2「网络路径」，然后删 spike CVM：
`phala cvms stop <spike-uuid> && phala cvms delete <spike-uuid>`（delete 子命令名以 `phala cvms --help` 为准）。

- [ ] **Step 7: Commit（须用户授权）**

```bash
git add deploy/postgres/spike/ docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md
git commit -m "spike(tee-pg): prod9 -5432s passthrough + direct TLS + LISTEN/NOTIFY soak results"
```

---

### Task 1: TLS 证书材料与生成脚本

**Files:**
- Create: `deploy/postgres/gen-certs.sh`

**Interfaces:**
- Produces: 本地生成的 `feedling-pg-ca.crt`（分发给消费方，非机密）、`server.crt`/`server.key`（经加密 env 注入 pg CVM）。Task 2 entrypoint 消费 `PG_SERVER_CERT_B64`/`PG_SERVER_KEY_B64` env；Plan 2 的客户端 DSN 消费 `sslrootcert=<ca 路径> sslmode=verify-full`。

按 spec §2【补充】选型：**自签长效 CA（10 年）+ 服务端证书（825 天），CA 私钥离线保存**。证书不派生自 CVM 身份 → 重建 CVM 不换证书，轮换只在证书到期或泄露时做（先发新 CA 给客户端、再换服务端）。

- [ ] **Step 1: 写生成脚本**

```bash
#!/bin/bash
# deploy/postgres/gen-certs.sh — 一次性生成 pg CVM 的 TLS 材料。
# CA 私钥 (ca.key) 离线冷存，绝不进任何 CVM/CI。
# 用法: ./gen-certs.sh feedling-pg-test <输出目录>
set -euo pipefail
NAME="${1:?usage: gen-certs.sh <cvm-name> <outdir>}"
OUT="${2:?usage: gen-certs.sh <cvm-name> <outdir>}"
mkdir -p "$OUT" && cd "$OUT"

# CN 必须等于客户端连接的主机名（verify-full 校验它）
# app_id 在首次 phala deploy 后才知道 → 先用 SAN 通配 + 部署后按实际 app_id 重签一次 server 证书
openssl req -new -x509 -days 3650 -nodes -keyout ca.key -out ca.crt \
  -subj "/CN=${NAME}-ca"
openssl req -new -nodes -keyout server.key -out server.csr \
  -subj "/CN=${NAME}"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -out server.crt \
  -extfile <(printf "subjectAltName=DNS:*.dstack-pha-prod9.phala.network")
rm server.csr
chmod 600 ca.key server.key
echo "== 注入 pg CVM 的加密 env 值 =="
echo "PG_SERVER_CERT_B64=$(base64 < server.crt | tr -d '\n')"
echo "PG_SERVER_KEY_B64=$(base64 < server.key | tr -d '\n')"
echo "== 分发给消费方（非机密） =="
echo "ca.crt → 各消费 CVM 镜像内 /etc/feedling/pg-ca.crt（DSN 用 sslrootcert 指向它）"
echo "== ca.key 立即移到离线冷存，从 ${OUT} 删除 =="
```

- [ ] **Step 2: 本地跑一遍验证自洽**

Run: `bash deploy/postgres/gen-certs.sh feedling-pg-test /tmp/pgcerts-selftest && openssl verify -CAfile /tmp/pgcerts-selftest/ca.crt /tmp/pgcerts-selftest/server.crt`
Expected: `server.crt: OK`。然后 `rm -rf /tmp/pgcerts-selftest`。

- [ ] **Step 3: Commit（须用户授权）**

```bash
git add deploy/postgres/gen-certs.sh
git commit -m "feat(tee-pg): TLS cert generation script (offline CA, verify-full)"
```

---

### Task 2: pg 镜像（Dockerfile + entrypoint 全家桶）

**Files:**
- Create: `deploy/postgres/Dockerfile`
- Create: `deploy/postgres/entrypoint-wrapper.sh`
- Create: `deploy/postgres/backup-push.sh`
- Create: `deploy/postgres/restore.sh`
- Create: `deploy/postgres/ensure-roles.sh`
- Create: `deploy/postgres/pg_hba.conf`

**Interfaces:**
- Consumes: Task 1 的 `PG_SERVER_CERT_B64`/`PG_SERVER_KEY_B64` env 约定。
- Produces: 镜像 `ghcr.io/teleport-computer/feedling-postgres:<sha>`；env 契约（下表）供 Task 3 compose 和 Task 5 部署 workflow 使用；DB 角色 `feedling_owner`（=POSTGRES_USER，migration/owner）、`app`、`tee_replicator`、`monitoring` 供 Plan 2 使用。

| env | 必填 | 用途 |
|---|---|---|
| `POSTGRES_USER=feedling_owner` / `POSTGRES_DB=feedling` | 常量（compose 内） | owner/migration 角色 |
| `POSTGRES_PASSWORD` | 是（entrypoint 校验） | owner 密码，每 boot 幂等对齐 |
| `APP_DB_PASSWORD` / `REPLICATOR_DB_PASSWORD` / `MONITORING_DB_PASSWORD` | 是 | 三个业务角色密码 |
| `PG_SERVER_CERT_B64` / `PG_SERVER_KEY_B64` | 是 | TLS 材料 |
| `WALG_S3_PREFIX` | 是（生产） | `s3://feedling-pg-backups/wal-g` |
| `WALG_LIBSODIUM_KEY` | **WALG_S3_PREFIX 设则强制** | 64 hex，backup 加密 |
| `AWS_ENDPOINT` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | 是（生产） | R2 |

- [ ] **Step 1: Dockerfile**

```dockerfile
# deploy/postgres/Dockerfile
# postgres:17 digest 待实施时解析：docker manifest inspect postgres:17 取 amd64 digest 后钉死
FROM postgres:17@sha256:REPLACE_WITH_RESOLVED_DIGEST

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl jq cron \
    && rm -rf /var/lib/apt/lists/*

# WAL-G：v3.0.8 起步；实施时核验 release notes 支持 PG17，不支持则升到最低支持版并回填此处
ARG WALG_VERSION=v3.0.8
RUN curl -fsSL "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-amd64.tar.gz" \
    | tar xz -C /usr/local/bin/ \
    && mv /usr/local/bin/wal-g-pg-22.04-amd64 /usr/local/bin/wal-g \
    && chmod +x /usr/local/bin/wal-g

ENV AWS_S3_FORCE_PATH_STYLE="true"
ENV AWS_REGION="auto"

RUN echo "0 3 * * * root /usr/local/bin/backup-push.sh >> /var/log/walg-backup.log 2>&1" > /etc/cron.d/walg-backup \
    && chmod 0644 /etc/cron.d/walg-backup

COPY postgres/pg_hba.conf /etc/postgresql/pg_hba.conf
COPY postgres/backup-push.sh postgres/restore.sh postgres/ensure-roles.sh \
     postgres/entrypoint-wrapper.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup-push.sh /usr/local/bin/restore.sh \
    /usr/local/bin/ensure-roles.sh /usr/local/bin/entrypoint-wrapper.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
CMD ["postgres"]
```

注意：**没有** `/docker-entrypoint-initdb.d/` 归档脚本（修正 1：archive 配置全走 compose command 旗标）。

- [ ] **Step 2: pg_hba.conf（TLS-only + 容器内 socket trust）**

```
# deploy/postgres/pg_hba.conf — 经 compose command 的 -c hba_file= 加载（避开 initdb-only 坑）
# 容器内 unix socket：cron 备份 / ensure-roles 用，trust（容器即边界）
local   all   all                    trust
# 网络一律 TLS + scram；没有明文 host 行
hostssl all   all   0.0.0.0/0        scram-sha-256
hostssl all   all   ::0/0            scram-sha-256
```

- [ ] **Step 3: entrypoint-wrapper.sh（四修正 + 全部 fail-closed）**

```bash
#!/bin/bash
set -euo pipefail
# entrypoint-wrapper.sh — 校验→TLS 材料→cron env→后台任务→官方 entrypoint
# spec §3：修正 2/3/4 + 密码对齐 + fail-closed，全部在此落实。

fatal() { echo "[pg-init] FATAL: $*" >&2; exit 1; }

[ -n "${POSTGRES_PASSWORD:-}" ]    || fatal "POSTGRES_PASSWORD not set"
[ -n "${APP_DB_PASSWORD:-}" ]      || fatal "APP_DB_PASSWORD not set"
[ -n "${REPLICATOR_DB_PASSWORD:-}" ] || fatal "REPLICATOR_DB_PASSWORD not set"
[ -n "${MONITORING_DB_PASSWORD:-}" ] || fatal "MONITORING_DB_PASSWORD not set"
[ -n "${PG_SERVER_CERT_B64:-}" ]   || fatal "PG_SERVER_CERT_B64 not set"
[ -n "${PG_SERVER_KEY_B64:-}" ]    || fatal "PG_SERVER_KEY_B64 not set"

# --- 修正 4（fail-closed）：备份必须加密 ---
if [ -n "${WALG_S3_PREFIX:-}" ]; then
    [ -n "${WALG_LIBSODIUM_KEY:-}" ] || fatal "WALG_S3_PREFIX set but WALG_LIBSODIUM_KEY missing — refusing to ship plaintext WAL"
    [ ${#WALG_LIBSODIUM_KEY} -eq 64 ] || fatal "WALG_LIBSODIUM_KEY must be 64 hex chars, got ${#WALG_LIBSODIUM_KEY}"
    echo "[pg-init] WAL-G configured (encrypted → ${WALG_S3_PREFIX})"
else
    echo "[pg-init] WARNING: WAL-G NOT configured — acceptable only for spike/scratch" >&2
fi

# --- TLS 材料落盘 ---
mkdir -p /etc/pgtls
base64 -d <<<"${PG_SERVER_CERT_B64}" > /etc/pgtls/server.crt
base64 -d <<<"${PG_SERVER_KEY_B64}"  > /etc/pgtls/server.key
chown postgres:postgres /etc/pgtls/server.key /etc/pgtls/server.crt
chmod 600 /etc/pgtls/server.key

# --- 修正 2（完整清单）：cron 的 libpq + WAL-G 环境 ---
if [ -n "${WALG_S3_PREFIX:-}" ]; then
    printenv | grep -E '^(WALG_|AWS_|PGDATA)' > /etc/environment.walg
    {
      echo "PGHOST=/var/run/postgresql"
      echo "PGUSER=${POSTGRES_USER}"
      echo "PGPASSWORD=${POSTGRES_PASSWORD}"
    } >> /etc/environment.walg
    chmod 600 /etc/environment.walg
    sed -i '1i BASH_ENV=/etc/environment.walg' /etc/cron.d/walg-backup
    cron
fi

# --- 修正 3：boot 时无 base backup 立即补推 ---
if [ -n "${WALG_S3_PREFIX:-}" ]; then
(
  set -a; . /etc/environment.walg; set +a
  until su postgres -c "pg_isready -h /var/run/postgresql -q"; do sleep 2; done
  if ! wal-g backup-list 2>/dev/null | grep -q base_; then
      echo "[backup] no base backup found — pushing one now"
      wal-g backup-push "${PGDATA:-/var/lib/postgresql/data}" \
        || echo "[backup] FATAL: initial base backup failed" >&2
  fi
) &
fi

# --- 角色 + 密码幂等对齐（initdb-only 坑的解药） ---
/usr/local/bin/ensure-roles.sh &

exec docker-entrypoint.sh "$@"
```

- [ ] **Step 4: ensure-roles.sh（角色拆分 + 密码漂移对齐）**

```bash
#!/bin/bash
# ensure-roles.sh — 每次启动幂等：对齐 owner 密码；建/对齐 app、tee_replicator、
# monitoring 三角色。POSTGRES_PASSWORD 只在 initdb 生效（spec §3 密码漂移坑），
# 这里是唯一让轮换真正生效的地方。
set -uo pipefail
log() { echo "[ensure-roles] $*"; }

for _ in $(seq 1 60); do
    su postgres -c "pg_isready -h /var/run/postgresql -q" && break
    sleep 2
done
su postgres -c "pg_isready -h /var/run/postgresql -q" || { log "pg never ready"; exit 1; }

run_sql() { su postgres -c "psql -h /var/run/postgresql -U \"${POSTGRES_USER}\" -d \"${POSTGRES_DB}\" -v ON_ERROR_STOP=1 -c \"$1\""; }

run_sql "ALTER USER \\\"${POSTGRES_USER}\\\" WITH PASSWORD '${POSTGRES_PASSWORD}';" \
    && log "owner password aligned" || log "owner ALTER failed"

ensure_role() {  # $1=role $2=password $3=extra grant SQL（可空）
    run_sql "DO \\\$\\\$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$1') THEN
            CREATE ROLE \\\"$1\\\" LOGIN;
        END IF; END \\\$\\\$;"
    run_sql "ALTER ROLE \\\"$1\\\" WITH LOGIN PASSWORD '$2';"
    [ -n "$3" ] && run_sql "$3"
    log "role $1 ensured"
}

# app：业务 CRUD，非 owner（DDL 会被拒 → Phase 1 负向验收）
ensure_role app "${APP_DB_PASSWORD}" \
  "GRANT USAGE ON SCHEMA public TO app;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO app;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app;"

# tee_replicator：与 app 同权（写明文表 + 游标表；游标表由 alembic_tee 建）
ensure_role tee_replicator "${REPLICATOR_DB_PASSWORD}" \
  "GRANT USAGE ON SCHEMA public TO tee_replicator;
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tee_replicator;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO tee_replicator;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tee_replicator;
   ALTER DEFAULT PRIVILEGES FOR ROLE \\\"${POSTGRES_USER}\\\" IN SCHEMA public
     GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO tee_replicator;"

# monitoring：只有 pg_monitor（读 pg_stat_archiver / pg_ls_waldir），读不了业务表
ensure_role monitoring "${MONITORING_DB_PASSWORD}" \
  "GRANT pg_monitor TO monitoring;"
```

（alembic_tee 迁移用 `POSTGRES_USER`=owner 凭证、由部署流程/手动跑——spec §3 角色拆分决策取「独立迁移步骤」分支，Plan 2 Task 1 实现。）

- [ ] **Step 5: backup-push.sh 与 restore.sh**

```bash
#!/bin/bash
# backup-push.sh — cron 每日 03:00 UTC
set -euo pipefail
if [ -f /etc/environment.walg ]; then set -a; . /etc/environment.walg; set +a; fi
: "${PGDATA:=/var/lib/postgresql/data}"
echo "[backup] start $(date -u +%FT%TZ)"
wal-g backup-push "$PGDATA"
wal-g delete retain FULL 7 --confirm
echo "[backup] done $(date -u +%FT%TZ)"
```

```bash
#!/bin/bash
# restore.sh — 灾难恢复（在 TEE 内 scratch 环境跑，spec §3【补充】）
# 用法: WALG_LIBSODIUM_KEY=... WALG_S3_PREFIX=... AWS_*=... ./restore.sh [BACKUP_NAME]
set -euo pipefail
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
BACKUP_NAME="${1:-LATEST}"
# fail-closed：我们的备份必然加密，没钥就是配置错了（不抄 hivemind 的可选分支）
[ -n "${WALG_LIBSODIUM_KEY:-}" ] || { echo "[restore] FATAL: WALG_LIBSODIUM_KEY required"; exit 1; }
[ ${#WALG_LIBSODIUM_KEY} -eq 64 ] || { echo "[restore] FATAL: key must be 64 hex"; exit 1; }
[ -n "${WALG_S3_PREFIX:-}" ] || { echo "[restore] FATAL: WALG_S3_PREFIX required"; exit 1; }
export AWS_S3_FORCE_PATH_STYLE="true" AWS_REGION="${AWS_REGION:-auto}"
echo "[restore] available backups:"; wal-g backup-list
if pg_isready -q 2>/dev/null; then pg_ctl -D "$PGDATA" stop -m fast || true; sleep 2; fi
rm -rf "${PGDATA:?}"/*
wal-g backup-fetch "$PGDATA" "$BACKUP_NAME"
touch "$PGDATA/recovery.signal"
cat >> "$PGDATA/postgresql.conf" <<EOF

# --- Recovery configuration (added by restore.sh) ---
restore_command = 'wal-g wal-fetch %f %p'
recovery_target_action = 'promote'
EOF
echo "[restore] configured; start postgres to replay WAL to latest and promote"
```

- [ ] **Step 6: 本地构建 + PG17×WAL-G 冒烟**

```bash
docker manifest inspect postgres:17 | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest'
# 把 digest 填进 Dockerfile 的 REPLACE_WITH_RESOLVED_DIGEST，然后：
docker build -t feedling-postgres:dev -f deploy/postgres/Dockerfile deploy/
docker run --rm feedling-postgres:dev wal-g --version
```
Expected: 构建成功；`wal-g version v3.0.8`（或核验后升级的版本）。**此步同时核验 WAL-G↔PG17**：查 `github.com/wal-g/wal-g/releases` 确认所选版本列出 PG17 支持；本地起容器（带 MinIO 或真 R2 scratch prefix）实测 `backup-push` + `wal-push` 成功，不支持则升版本并回填 Dockerfile。

- [ ] **Step 7: Commit（须用户授权）**

```bash
git add deploy/postgres/
git commit -m "feat(tee-pg): postgres:17 image with WAL-G, fail-closed entrypoint, role split"
```

---

### Task 3: pg CVM compose

**Files:**
- Create: `deploy/docker-compose.phala.postgres.yaml`

**Interfaces:**
- Consumes: Task 2 的镜像与 env 契约。
- Produces: pg CVM 的 compose（Task 5 部署 workflow 与首次开通 runbook 使用）。

- [ ] **Step 1: 写 compose**

```yaml
# deploy/docker-compose.phala.postgres.yaml — feedling-pg-{test,prod} CVM
#
# 部署（首次开通见 DEPLOYMENTS.md runbook；日常用 pg-deploy workflow）：
#   phala deploy --cvm-id <id> -c deploy/docker-compose.phala.postgres.yaml -e KEY=VAL ... --wait
#
# 约定（与本仓其它 phala compose 一致）：
#  - 所有机密走 "${VAR:-}"（加密 env 注入，不烧 compose_hash）；必填校验在
#    entrypoint fail-closed（deploy/postgres/entrypoint-wrapper.sh）。
#  - 独立 AppAuth 合约；不进 merge 自动部署。
name: feedling-pg
services:
  db:
    image: ghcr.io/teleport-computer/feedling-postgres:REPLACE_SHA
    command:
      - postgres
      - -c
      - listen_addresses=*
      - -c
      - hba_file=/etc/postgresql/pg_hba.conf
      - -c
      - ssl=on
      - -c
      - ssl_cert_file=/etc/pgtls/server.crt
      - -c
      - ssl_key_file=/etc/pgtls/server.key
      # --- 修正 1：归档配置走服务器旗标，每次 boot 生效（不用 initdb 脚本） ---
      - -c
      - archive_mode=on
      - -c
      - archive_command=wal-g wal-push %p
      - -c
      - archive_timeout=60
      - -c
      - wal_level=replica
      # --- 容量（spec §3【补充】）：3 CVM × (4 workers × pool 10) + wake bus
      #     每 worker 1 条 + replicator + monitoring + 余量 ≈ 160，取 200。
      #     实施 Plan 2 时按 db.py 实际池参数复核。 ---
      - -c
      - max_connections=200
      - -c
      - shared_buffers=512MB
    environment:
      POSTGRES_DB: feedling
      POSTGRES_USER: feedling_owner
      POSTGRES_PASSWORD: "${PG_OWNER_PASSWORD:-}"
      APP_DB_PASSWORD: "${APP_DB_PASSWORD:-}"
      REPLICATOR_DB_PASSWORD: "${REPLICATOR_DB_PASSWORD:-}"
      MONITORING_DB_PASSWORD: "${MONITORING_DB_PASSWORD:-}"
      PG_SERVER_CERT_B64: "${PG_SERVER_CERT_B64:-}"
      PG_SERVER_KEY_B64: "${PG_SERVER_KEY_B64:-}"
      WALG_S3_PREFIX: "${WALG_S3_PREFIX:-}"
      WALG_LIBSODIUM_KEY: "${WALG_LIBSODIUM_KEY:-}"
      AWS_ENDPOINT: "${PG_BACKUP_R2_ENDPOINT:-}"
      AWS_ACCESS_KEY_ID: "${PG_BACKUP_R2_ACCESS_KEY_ID:-}"
      AWS_SECRET_ACCESS_KEY: "${PG_BACKUP_R2_SECRET_ACCESS_KEY:-}"
      AWS_S3_FORCE_PATH_STYLE: "true"
      AWS_REGION: "auto"
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U feedling_owner -d feedling"]
      interval: 5s
      timeout: 3s
      retries: 10

volumes:
  pgdata:
```

- [ ] **Step 2: 本地合成校验**

Run: `docker compose -f deploy/docker-compose.phala.postgres.yaml config >/dev/null && echo OK`
Expected: `OK`（未设 env 也不许报错——全是 `:-` 形态）。

- [ ] **Step 3: Commit（须用户授权）**

```bash
git add deploy/docker-compose.phala.postgres.yaml
git commit -m "feat(tee-pg): pg CVM compose (archive flags, TLS, capacity)"
```

---

### Task 4: R2 备份桶 + 密钥生成（手动 runbook）

**Files:**
- Modify: `deploy/DEPLOYMENTS.md`（记录桶名/前缀/密钥存放位置，不记录密钥值）

**Interfaces:**
- Produces: R2 桶 `feedling-pg-backups` + 专用 API token；`WALG_LIBSODIUM_KEY`（密钥管理 + break-glass 双存）。Task 5 部署时注入。

- [ ] **Step 1: 建桶与专用凭证**

Cloudflare dashboard（或 wrangler）：建桶 `feedling-pg-backups`；创建**仅限该桶** Object Read & Write 的 R2 API token（与 io-user-logs/io-image-frames 的 token 分开——最小权限，互不横向）。记下 `PG_BACKUP_R2_ENDPOINT/ACCESS_KEY_ID/SECRET_ACCESS_KEY`。

- [ ] **Step 2: 生成备份加密钥 + break-glass**

```bash
openssl rand -hex 32
```
存两处：GitHub secrets（`TEST_WALG_LIBSODIUM_KEY`）**和** enclave 平台之外的 break-glass 位置（用户的密码管理器/离线介质——丢钥=备份全废，丢 CVM+丢钥=数据没了）。同时把 Task 1 的 `ca.key` 也放进同一 break-glass 位置。

- [ ] **Step 3: 在 DEPLOYMENTS.md「Enclave configuration」区补一节**

写明：桶名、`WALG_S3_PREFIX=s3://feedling-pg-backups/wal-g`、token 权限范围、密钥存放位置（指向而非值）、ca.key 冷存位置指向。

- [ ] **Step 4: Commit（须用户授权）**

```bash
git add deploy/DEPLOYMENTS.md
git commit -m "docs(tee-pg): R2 backup bucket + key custody record"
```

---

### Task 5: 部署 workflow（手动 dispatch）+ 首次开通 runbook

**Files:**
- Create: `.github/workflows/pg-deploy.yml`
- Create: `deploy/test-pg-cvm-id.txt`（首次开通后填）
- Modify: `deploy/DEPLOYMENTS.md`（首次开通 runbook + CVM 表新行）

**Interfaces:**
- Consumes: Task 2 镜像、Task 3 compose、Task 4 secrets。
- Produces: 可重复的 pg CVM 更新部署通道；`deploy/test-pg-cvm-id.txt` 供 workflow 解析。

- [ ] **Step 1: 写 workflow**

```yaml
# .github/workflows/pg-deploy.yml — pg CVM 手动部署（绝不并入 merge 自动部署）
name: Deploy Postgres CVM
on:
  workflow_dispatch:
    inputs:
      environment:
        type: choice
        options: [test]        # prod 待 Phase 8 加
        required: true
      confirm:
        description: '输入 DEPLOY-PG 确认（防误触）'
        required: true
concurrency: pg-deploy-${{ inputs.environment }}   # 与 app 部署不同组
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Typo guard
        run: test "${{ inputs.confirm }}" = "DEPLOY-PG" || { echo "confirm mismatch"; exit 1; }
      - uses: actions/checkout@v4
        with: { ref: test }
      - name: Required secrets pre-check   # 本仓 compose 用 ${VAR:-}，无法 grep :?，用显式清单
        env:
          PG_OWNER_PASSWORD: ${{ secrets.TEST_PG_OWNER_PASSWORD }}
          APP_DB_PASSWORD: ${{ secrets.TEST_APP_DB_PASSWORD }}
          REPLICATOR_DB_PASSWORD: ${{ secrets.TEST_REPLICATOR_DB_PASSWORD }}
          MONITORING_DB_PASSWORD: ${{ secrets.TEST_MONITORING_DB_PASSWORD }}
          PG_SERVER_CERT_B64: ${{ secrets.TEST_PG_SERVER_CERT_B64 }}
          PG_SERVER_KEY_B64: ${{ secrets.TEST_PG_SERVER_KEY_B64 }}
          WALG_S3_PREFIX: ${{ secrets.TEST_WALG_S3_PREFIX }}
          WALG_LIBSODIUM_KEY: ${{ secrets.TEST_WALG_LIBSODIUM_KEY }}
          PG_BACKUP_R2_ENDPOINT: ${{ secrets.TEST_PG_BACKUP_R2_ENDPOINT }}
          PG_BACKUP_R2_ACCESS_KEY_ID: ${{ secrets.TEST_PG_BACKUP_R2_ACCESS_KEY_ID }}
          PG_BACKUP_R2_SECRET_ACCESS_KEY: ${{ secrets.TEST_PG_BACKUP_R2_SECRET_ACCESS_KEY }}
        run: |
          missing=0
          for v in PG_OWNER_PASSWORD APP_DB_PASSWORD REPLICATOR_DB_PASSWORD \
                   MONITORING_DB_PASSWORD PG_SERVER_CERT_B64 PG_SERVER_KEY_B64 \
                   WALG_S3_PREFIX WALG_LIBSODIUM_KEY PG_BACKUP_R2_ENDPOINT \
                   PG_BACKUP_R2_ACCESS_KEY_ID PG_BACKUP_R2_SECRET_ACCESS_KEY; do
            [ -n "${!v}" ] || { echo "MISSING secret: $v"; missing=1; }
          done
          exit $missing
      - name: Build & push pg image
        uses: docker/build-push-action@v6
        with:
          context: deploy
          file: deploy/postgres/Dockerfile
          push: true
          tags: ghcr.io/${{ github.repository_owner }}/feedling-postgres:${{ github.sha }}
      - name: Pin image sha into compose
        run: |
          sed -i -E "s|ghcr\.io/[^/]+/feedling-postgres:[A-Za-z0-9_]+|ghcr.io/${{ github.repository_owner }}/feedling-postgres:${{ github.sha }}|" \
            deploy/docker-compose.phala.postgres.yaml
      - name: Deploy (update in place; 名字查不到就失败，绝不静默新建)
        env: { PHALA_CLOUD_API_KEY: '${{ secrets.TEST_PHALA_CLOUD_API_KEY }}' }
        run: |
          npm install -g phala@1.1.19
          CVM_ID=$(grep -v '^#' deploy/test-pg-cvm-id.txt | head -1)
          test -n "$CVM_ID" || { echo "test-pg-cvm-id.txt empty — 首次开通走 DEPLOYMENTS.md runbook，不走本 workflow"; exit 1; }
          phala deploy --api-token "$PHALA_CLOUD_API_KEY" --cvm-id "$CVM_ID" \
            -c deploy/docker-compose.phala.postgres.yaml \
            -e "PG_OWNER_PASSWORD=${{ secrets.TEST_PG_OWNER_PASSWORD }}" \
            -e "APP_DB_PASSWORD=${{ secrets.TEST_APP_DB_PASSWORD }}" \
            -e "REPLICATOR_DB_PASSWORD=${{ secrets.TEST_REPLICATOR_DB_PASSWORD }}" \
            -e "MONITORING_DB_PASSWORD=${{ secrets.TEST_MONITORING_DB_PASSWORD }}" \
            -e "PG_SERVER_CERT_B64=${{ secrets.TEST_PG_SERVER_CERT_B64 }}" \
            -e "PG_SERVER_KEY_B64=${{ secrets.TEST_PG_SERVER_KEY_B64 }}" \
            -e "WALG_S3_PREFIX=${{ secrets.TEST_WALG_S3_PREFIX }}" \
            -e "WALG_LIBSODIUM_KEY=${{ secrets.TEST_WALG_LIBSODIUM_KEY }}" \
            -e "PG_BACKUP_R2_ENDPOINT=${{ secrets.TEST_PG_BACKUP_R2_ENDPOINT }}" \
            -e "PG_BACKUP_R2_ACCESS_KEY_ID=${{ secrets.TEST_PG_BACKUP_R2_ACCESS_KEY_ID }}" \
            -e "PG_BACKUP_R2_SECRET_ACCESS_KEY=${{ secrets.TEST_PG_BACKUP_R2_SECRET_ACCESS_KEY }}" \
            --wait
      - name: Publish compose hash (independent AppAuth)
        env:
          PHALA_CLOUD_API_KEY: ${{ secrets.TEST_PHALA_CLOUD_API_KEY }}
          PRIVATE_KEY: ${{ secrets.ETH_DEPLOYER_KEY }}
          ETH_SEPOLIA_RPC_URL: ${{ secrets.ETH_SEPOLIA_RPC_URL }}
          FEEDLING_APP_AUTH_CONTRACT: ${{ secrets.TEST_PG_APP_AUTH_CONTRACT }}
          FEEDLING_COMPOSE_FILE: deploy/docker-compose.phala.postgres.yaml
        run: |
          export FEEDLING_CVM_ID=$(grep -v '^#' deploy/test-pg-cvm-id.txt | head -1)
          ./deploy/publish-compose-hash.sh eth_sepolia
```

- [ ] **Step 2: DEPLOYMENTS.md 首次开通 runbook（照本仓 runner 先例改写）**

在「First-time provisioning」区新增小节，内容含：
1. 部署独立 AppAuth：`cd contracts && make deploy CHAIN=eth_sepolia`（owner = ETH_DEPLOYER_KEY 地址），合约地址存 GitHub secret `TEST_PG_APP_AUTH_CONTRACT`，并登记进 DEPLOYMENTS.md On-chain 表。
2. 首次 create（**唯一一次不带 --cvm-id**）：
   ```bash
   phala deploy --api-token "$TEST_PHALA_CLOUD_API_KEY" \
     --name feedling-pg-test --instance-type tdx.small --kms phala \
     -c deploy/docker-compose.phala.postgres.yaml \
     -e "PG_OWNER_PASSWORD=..." \
     -e <上面 workflow 同款全套 env> --wait
   ```
   磁盘规格：先跑 `psql "$TEST_DATABASE_URL" -c "SELECT pg_size_pretty(pg_database_size(current_database()));"` 按 spec §8 容量公式选型。
3. 记录 `phala cvms list --json` 里的 uuid → 写入 `deploy/test-pg-cvm-id.txt`；app_id → 拼出 `<app-id>-5432s.dstack-pha-prod9.phala.network`。
4. 按实际 app_id 用 Task 1 的 CA 重签 server 证书（CN/SAN 精确匹配），更新 `TEST_PG_SERVER_CERT_B64`，跑一次 pg-deploy workflow 让 verify-full 严格生效。
5. `publish-compose-hash.sh` 首次发布 + CVM 表新增一行（name/app_id/uuid/gateway/合约）。

- [ ] **Step 3: 触发一次 pg-deploy workflow 验证全链**

Run: GitHub Actions → Deploy Postgres CVM → environment=test, confirm=DEPLOY-PG。
Expected: 全绿；`psql "host=<app-id>-5432s.dstack-pha-prod9.phala.network port=443 dbname=feedling user=app password=... sslmode=verify-full sslrootcert=ca.crt sslnegotiation=direct" -c "SELECT 1"` 返回 1。

- [ ] **Step 4: Commit（须用户授权）**

```bash
git add .github/workflows/pg-deploy.yml deploy/test-pg-cvm-id.txt deploy/DEPLOYMENTS.md
git commit -m "feat(tee-pg): manual pg CVM deploy workflow + provisioning runbook"
```

---

### Task 6: 监控 workflow（cron）

**Files:**
- Create: `.github/workflows/pg-monitor.yml`

**Interfaces:**
- Consumes: Task 2 的 `monitoring` 角色、Task 4 的 R2 凭证。
- Produces: 归档新鲜度 / base backup / WAL 堆积磁盘三项告警（workflow 失败 = 告警，GitHub 自动通知）。

- [ ] **Step 1: 写 workflow**

```yaml
# .github/workflows/pg-monitor.yml — 没有监控 = 没有备份（spec §3）
name: PG backup monitor
on:
  schedule: [{ cron: '*/30 * * * *' }]
  workflow_dispatch: {}
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Install psql 17 (direct TLS 需要 libpq>=17)
        run: |
          sudo sh -c 'echo "deb https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
          sudo apt-get update -qq && sudo apt-get install -y -qq postgresql-client-17
      - name: Archiver freshness + WAL 堆积（monitoring 角色，pg_monitor-only）
        env:
          MON_DSN: ${{ secrets.TEST_PG_MONITORING_DSN }}   # host=<app-id>-5432s... port=443 user=monitoring ... sslnegotiation=direct sslmode=require
        run: |
          PSQL=/usr/lib/postgresql/17/bin/psql
          AGE=$($PSQL "$MON_DSN" -tAc "SELECT COALESCE(EXTRACT(EPOCH FROM now()-last_archived_time)::int, 999999) FROM pg_stat_archiver;")
          echo "archiver age: ${AGE}s"
          test "$AGE" -lt 3600 || { echo "::error::archiver stale ${AGE}s (>1h) — 归档死了"; exit 1; }
          WAL_MB=$($PSQL "$MON_DSN" -tAc "SELECT COALESCE(sum(size)/1024/1024,0)::int FROM pg_ls_waldir();")
          echo "pg_wal size: ${WAL_MB}MB"
          test "$WAL_MB" -lt 4096 || { echo "::error::WAL 堆积 ${WAL_MB}MB (>4GB) — 先修归档，绝不手删 pg_wal/"; exit 1; }
      - name: R2 bucket freshness
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.TEST_PG_BACKUP_R2_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.TEST_PG_BACKUP_R2_SECRET_ACCESS_KEY }}
          ENDPOINT: ${{ secrets.TEST_PG_BACKUP_R2_ENDPOINT }}
        run: |
          newest() { aws s3api list-objects-v2 --endpoint-url "$ENDPOINT" \
            --bucket feedling-pg-backups --prefix "$1" \
            --query 'sort_by(Contents,&LastModified)[-1].LastModified' --output text; }
          WAL_TS=$(newest "wal-g/wal_005/");  BASE_TS=$(newest "wal-g/basebackups_005/")
          echo "newest wal: $WAL_TS | newest base sentinel: $BASE_TS"
          for pair in "WAL:$WAL_TS:3600" "BASE:$BASE_TS:93600"; do
            IFS=: read -r label ts_a ts_b max <<<"$pair"; ts="$ts_a:$ts_b"   # LastModified 自带冒号
            [ "$ts" != "None:" ] || { echo "::error::$label prefix EMPTY — WAL 无 base backup 不可恢复"; exit 1; }
            AGE=$(( $(date +%s) - $(date -d "${ts%%:*}:${ts#*:}" +%s) ))
            test "$AGE" -lt "$max" || { echo "::error::$label stale ${AGE}s"; exit 1; }
          done
```

- [ ] **Step 2: 手动 dispatch 验证三项检查都能过 + 能报警**

Run: workflow_dispatch 一次 → 全绿。再做负向验证：临时把 archiver 阈值改成 `-lt 1` dispatch 一次 → 红（证明告警通道真的会响），改回。
Expected: 一绿一红。

- [ ] **Step 3: Commit（须用户授权）**

```bash
git add .github/workflows/pg-monitor.yml
git commit -m "feat(tee-pg): backup/archiver/disk cron monitor via pg_monitor role"
```

---

### Task 7: restore 演练（TEE 内）+ Phase 1 验收清单

**Files:**
- Modify: `deploy/DEPLOYMENTS.md`（restore 演练 runbook + 验收记录）

**Interfaces:**
- Consumes: Task 2 restore.sh、Task 4 备份钥。
- Produces: Phase 1 完成宣告；Plan 2 的前置条件（`TEE_DATABASE_URL` 可用且已有可恢复备份）。

- [ ] **Step 1: 演练 runbook 写进 DEPLOYMENTS.md**

在 pg CVM 内起 scratch 容器演练（不出 enclave，spec §3【补充】）——SSH 进 pg CVM 后：

```bash
docker run --rm --network host \
  -e WALG_S3_PREFIX=... -e WALG_LIBSODIUM_KEY=... \
  -e AWS_ENDPOINT=... -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
  -e PGDATA=/tmp/drill-pgdata \
  ghcr.io/teleport-computer/feedling-postgres:<sha> \
  bash -c '/usr/local/bin/restore.sh LATEST && \
    su postgres -c "pg_ctl -D /tmp/drill-pgdata -o \"-p 5433 -c archive_mode=off\" start" && \
    sleep 5 && su postgres -c "psql -p 5433 -d feedling -c \"SELECT count(*) FROM users;\"" && \
    su postgres -c "pg_ctl -D /tmp/drill-pgdata stop"'
```
Expected: count 返回与主库一致的行数（Phase 1 时库还空，先造一张已知表+已知行数再演练）。演练完成日期记入 DEPLOYMENTS.md；设月度日历提醒。

- [ ] **Step 2: 跑 Phase 1 验收清单（spec §7 全项）并逐项记录**

```
[ ] SHOW archive_mode = on（活服务器：psql -c "SHOW archive_mode;"）
[ ] R2 basebackups_005/ 非空（boot 补推或首个 03:00 cron）
[ ] 桶内对象确认加密（aws s3 cp 一个 wal 对象下来，file 输出非 gzip/明文；无钥 wal-g backup-list 失败）
[ ] pg-monitor 三项检查全绿 + 负向报警实测过
[ ] restore 演练在 TEE 内端到端成功一次（Step 1）
[ ] WAL-G 在 PG17 上 push/wal-push/fetch 三动作实测过（Task 2 Step 6 + 本演练）
[ ] app 角色 DROP TABLE / ALTER TABLE 被拒；monitoring 角色 SELECT 业务表被拒（负向权限测试）
[ ] max_connections=200 生效（SHOW max_connections）
[ ] pg-deploy 预检生效（临时删掉一个 secret dispatch 一次 → 预检红）
[ ] 主 app CVM 部署一次，pg CVM 不重启（对照 phala cvms get 的 uptime）
```

- [ ] **Step 3: Commit（须用户授权）**

```bash
git add deploy/DEPLOYMENTS.md
git commit -m "docs(tee-pg): restore drill runbook + Phase 1 acceptance record"
```

---

## Self-Review 记录

- Spec 覆盖：§2 网络路径→Task 0/1；§3 全部 bullet（含 8 条【补充】）→Task 2/3/6/7；§7 Phase 0–1 验收→Task 0 Step 6 / Task 7 Step 2。角色拆分决策取「独立迁移步骤」分支（Task 2 Step 4 注明，Plan 2 Task 1 承接）。
- 已知留白（有意，非 placeholder）：postgres:17 digest 与 WAL-G 最终版本在 Task 2 Step 6 现场解析回填；server 证书在首次开通拿到 app_id 后重签（Task 5 Step 2.4）。
- 本 plan 与 ci.yml 的 paths-filter 无交集：pg compose 故意不加入自动部署清单。
