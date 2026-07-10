#!/bin/bash
# restore.sh — 灾难恢复（在 TEE 内 scratch 环境跑，spec §3【补充】）
# 用法: WALG_LIBSODIUM_KEY=... WALG_S3_PREFIX=... AWS_*=... ./restore.sh [BACKUP_NAME]
set -euo pipefail
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
BACKUP_NAME="${1:-LATEST}"
# fail-closed：我们的备份必然加密，没钥就是配置错了（不抄 hivemind 的可选分支）
[ -n "${WALG_LIBSODIUM_KEY:-}" ] || { echo "[restore] FATAL: WALG_LIBSODIUM_KEY required"; exit 1; }
[[ "${WALG_LIBSODIUM_KEY}" =~ ^[0-9a-fA-F]{64}$ ]] || { echo "[restore] FATAL: WALG_LIBSODIUM_KEY must be 64 hex chars"; exit 1; }
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
