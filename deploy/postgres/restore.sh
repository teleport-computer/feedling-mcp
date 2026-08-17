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
# pg_ctl / pg_controldata 不一定在 PATH 上（官方镜像里在 /usr/lib/postgresql/*/bin）。
# 演练时踩过：pg_isready 有、pg_ctl 没有。
PGBIN="$(dirname "$(command -v pg_controldata || command -v postgres || echo /usr/lib/postgresql/18/bin/x)")"
export PATH="$PGBIN:$PATH"

echo "[restore] available backups:"; wal-g backup-list
if pg_isready -q 2>/dev/null; then pg_ctl -D "$PGDATA" stop -m fast || true; sleep 2; fi
rm -rf "${PGDATA:?}"/*
wal-g backup-fetch "$PGDATA" "$BACKUP_NAME"
touch "$PGDATA/recovery.signal"

# PG 硬要求：恢复实例的下列参数必须 **≥ 主库当时的值**，否则回放直接
#   FATAL: recovery aborted because of insufficient parameter settings
# 而线上这些值是**部署参数注入的**（compose: `-c max_connections=400`），
# **不在备份带出来的 postgresql.conf 里** —— 恢复端默认 100，必炸。2026-07-28
# 的 restore 演练就是靠手工追加参数才继续的。
#
# 不写死数值：直接从刚取回的 backup 的 pg_control 里读主库当时的真实值，
# 主库调参后本脚本自动跟上，不会漂移。
_ctl_setting() {
    pg_controldata "$PGDATA" \
        | awk -F: -v k="^$1 setting:" '$0 ~ k { gsub(/[ \t]/, "", $2); print $2 }'
}

{
    echo
    echo "# --- Recovery configuration (added by restore.sh) ---"
    echo "restore_command = 'wal-g wal-fetch %f %p'"
    echo "recovery_target_action = 'promote'"
    echo
    echo "# 从备份的 pg_control 读出的主库当时取值（PG 要求恢复端 >= 主库）"
    # pg_controldata 的标签与 postgresql.conf 的参数名并不一一同名，逐对映射。
    for pair in \
        "max_connections:max_connections" \
        "max_worker_processes:max_worker_processes" \
        "max_wal_senders:max_wal_senders" \
        "max_prepared_xacts:max_prepared_transactions" \
        "max_locks_per_xact:max_locks_per_transaction"
    do
        label="${pair%%:*}"; param="${pair##*:}"
        value="$(_ctl_setting "$label")"
        if [ -n "$value" ]; then
            echo "$param = $value"
        else
            echo "[restore] WARN: pg_control 未给出 $label，$param 保持默认" >&2
        fi
    done
} >> "$PGDATA/postgresql.conf"

echo "[restore] recovery parameters taken from backup:"
grep -E '^(max_connections|max_worker_processes|max_wal_senders|max_prepared_transactions|max_locks_per_transaction) =' \
    "$PGDATA/postgresql.conf" | sed 's/^/[restore]   /'
echo "[restore] configured; start postgres to replay WAL to latest and promote"
echo "[restore] NOTE: 恢复实例没有 postgres 角色，用 -U <owner> 连（演练记录见"
echo "[restore]       docs/TEE_POSTGRES_SHADOW_PROVISIONING.md §5）"
