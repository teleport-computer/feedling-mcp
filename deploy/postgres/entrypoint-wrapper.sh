#!/bin/bash
set -euo pipefail
# entrypoint-wrapper.sh — 校验→TLS 材料→cron env→后台任务→官方 entrypoint
# spec §3：修正 2/3/4 + 密码对齐 + fail-closed，全部在此落实。

fatal() { echo "[pg-init] FATAL: $*" >&2; exit 1; }

# 防陈旧 sentinel：/var/run 通常是 tmpfs（容器重启即清），但防御性删除一次，
# 免得极端情况下（bind-mount 之类）healthcheck 误读上一次生命周期留下的文件。
rm -f /var/run/postgresql/feedling-roles-ok

[ -n "${POSTGRES_PASSWORD:-}" ]    || fatal "POSTGRES_PASSWORD not set"
[ -n "${APP_DB_PASSWORD:-}" ]      || fatal "APP_DB_PASSWORD not set"
[ -n "${REPLICATOR_DB_PASSWORD:-}" ] || fatal "REPLICATOR_DB_PASSWORD not set"
[ -n "${MONITORING_DB_PASSWORD:-}" ] || fatal "MONITORING_DB_PASSWORD not set"
[ -n "${PG_SERVER_CERT_B64:-}" ]   || fatal "PG_SERVER_CERT_B64 not set"
[ -n "${PG_SERVER_KEY_B64:-}" ]    || fatal "PG_SERVER_KEY_B64 not set"

# --- 修正 4（fail-closed）：备份必须加密 ---
if [ -n "${WALG_S3_PREFIX:-}" ]; then
    [ -n "${WALG_LIBSODIUM_KEY:-}" ] || fatal "WALG_S3_PREFIX set but WALG_LIBSODIUM_KEY missing — refusing to ship plaintext WAL"
    [[ "${WALG_LIBSODIUM_KEY}" =~ ^[0-9a-fA-F]{64}$ ]] || fatal "WALG_LIBSODIUM_KEY must be 64 hex chars"
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
    install -m 600 /dev/null /etc/environment.walg
    printenv | grep -E '^(WALG_|AWS_|PGDATA)' >> /etc/environment.walg
    {
      echo "PGHOST=/var/run/postgresql"
      echo "PGUSER=${POSTGRES_USER}"
      echo "PGPASSWORD=${POSTGRES_PASSWORD}"
      # PGDATABASE 必填：wal-g backup-push 连库跑 pg_backup_start/stop，缺它时
      # libpq 把库名默认成用户名（feedling_owner），该库不存在 → base backup
      # FATAL "database feedling_owner does not exist"。WAL wal-push 只传 S3 不
      # 连库故不受影响，但没有 base backup 整条备份链不可恢复。cron backup-push
      # 经 BASH_ENV source 同一文件，一并修好。
      echo "PGDATABASE=${POSTGRES_DB}"
    } >> /etc/environment.walg
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
