#!/bin/sh
# deploy/redis/backup-push.sh — 做一次快照并推到 R2。
#
# 刻意设计成「一次调用 = 一次备份」且可独立执行：测试、手动补推、
# 循环调度都走同一条码路。循环与 boot 兜底在 backup-loop.sh。
#
# fail-closed：缺加密公钥或目的地就退出，绝不推明文快照出 TEE。
set -eu

fatal() { echo "[backup] FATAL: $*" >&2; exit 1; }

[ -n "${REDIS_BACKUP_S3_PREFIX:-}" ]     || fatal "REDIS_BACKUP_S3_PREFIX not set"
[ -n "${REDIS_BACKUP_AGE_RECIPIENT:-}" ] || fatal "REDIS_BACKUP_AGE_RECIPIENT not set — refusing to ship plaintext snapshot"
[ -n "${REDIS_BACKUP_BUCKET:-}" ]        || fatal "REDIS_BACKUP_BUCKET not set"
[ -n "${AWS_ENDPOINT_URL:-}" ]           || fatal "AWS_ENDPOINT_URL not set"
# R2 凭证跟上面几个一样走 "${VAR:-}" 加密注入，注入失败同样表现为空
# 字符串而非未设置，set -u 逮不到。缺了就是 aws s3 cp 必定认证失败，
# 跟上面三个一样必须在这里 fatal，而不是留给 aws 命令自己报错。
[ -n "${AWS_ACCESS_KEY_ID:-}" ]          || fatal "AWS_ACCESS_KEY_ID not set"
[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]      || fatal "AWS_SECRET_ACCESS_KEY not set"
# entrypoint-wrapper.sh 强制要求 REDIS_PASSWORD，无条件写进 secret.conf
# 当 requirepass——这条对 unix socket 同样生效，本仓没有「不需要认证」
# 的部署形态，缺了就必然认证失败，跟上面几个一样必须 fatal。
[ -n "${REDISCLI_AUTH:-}" ]              || fatal "REDISCLI_AUTH not set"

SOCKET="${REDIS_SOCKET:-/var/run/redis/redis.sock}"
WORK="${BACKUP_TMPDIR:-/tmp/redis-backup}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PLAIN="${WORK}/redis-${TS}.rdb"
SEALED="${PLAIN}.age"
KEY="s3://${REDIS_BACKUP_BUCKET}/${REDIS_BACKUP_S3_PREFIX}redis-${TS}.rdb.age"

mkdir -p "$WORK"
# 明文快照即使只在磁盘上存在几秒，也不该让别的 uid 读到。
chmod 700 "$WORK"

# 明文中间产物必须清理，无论成功失败。SEALED 也要清——之前只清 PLAIN，
# aws s3 cp 失败时加密后的 blob 会留在 $WORK 里，循环里失败几次就堆积到
# 容器重启才清掉。
cleanup() { rm -f "$PLAIN" "$SEALED"; }
trap cleanup EXIT INT TERM

echo "[backup] start ${TS}"

# D4：一致性快照走 replication 协议（SYNC），不读卷内写到一半的 AOF/RDB。
# redis-cli 从 REDISCLI_AUTH 取口令，不放 argv（容器内进程列表可见）。
redis-cli -s "$SOCKET" --rdb "$PLAIN" \
    || fatal "snapshot failed — nothing uploaded"
[ -s "$PLAIN" ] || fatal "snapshot is empty — nothing uploaded"

# D5：非对称加密。备份机只有公钥，历史备份即使连同这台机器一起失窃
# 也解不开（对称钥方案做不到）。
age -r "$REDIS_BACKUP_AGE_RECIPIENT" -o "$SEALED" "$PLAIN" \
    || fatal "age encryption failed — nothing uploaded"

aws s3 cp "$SEALED" "$KEY" \
    || fatal "upload failed: ${KEY}"

rm -f "$SEALED"
echo "[backup] uploaded ${KEY}"

# --- 保留策略 ---
# 小时快照留最近 24 份；每日 03:00 UTC 那份额外留 7 天。
# 对应 wal-g 的 `delete retain FULL 7`。
#
# 关键的安全属性：列表失败时必须放弃删除。把「没列到」当成「没有对象」
# 会把整个备份历史删光——这比不做保留策略危险得多。
LISTING="$(aws s3api list-objects-v2 \
    --bucket "$REDIS_BACKUP_BUCKET" \
    --prefix "$REDIS_BACKUP_S3_PREFIX" \
    --query 'Contents[].Key' --output text 2>/dev/null)" || {
    echo "[backup] WARNING: listing failed — skipping retention (never delete blind)" >&2
    exit 0
}

if [ -n "$LISTING" ] && [ "$LISTING" != "None" ]; then
    # key 形如 <prefix>redis-20260724T030000Z.rdb.age，字典序 = 时间序。
    KEEP_HOURLY=24
    KEEP_DAILY_DAYS=7
    CUTOFF="$(date -u -d "@$(( $(date -u +%s) - KEEP_DAILY_DAYS * 86400 ))" +%Y%m%d 2>/dev/null \
              || date -u -r $(( $(date -u +%s) - KEEP_DAILY_DAYS * 86400 )) +%Y%m%d)"

    # 最近 N 份的名单（保护窗口一）
    RECENT="$(printf '%s\n' $LISTING | sort | tail -n "$KEEP_HOURLY")"

    for key in $LISTING; do
        case "$key" in
            *.rdb.age) ;;
            *) continue ;;                       # 不认识的对象一律不碰
        esac
        # 保护窗口一：最近 24 份
        if printf '%s\n' "$RECENT" | grep -Fxq "$key"; then
            continue
        fi
        # 保护窗口二：7 天内的每日 03Z 快照
        stamp="${key##*redis-}"; stamp="${stamp%%.rdb.age}"   # 20260724T030000Z
        day="${stamp%%T*}"
        hhmmss="${stamp#*T}"
        if [ "${hhmmss%%????Z}" = "03" ] && [ "$day" -ge "$CUTOFF" ] 2>/dev/null; then
            continue
        fi
        aws s3 rm "s3://${REDIS_BACKUP_BUCKET}/${key}" >/dev/null \
            && echo "[backup] pruned ${key}"
    done
fi

echo "[backup] done $(date -u +%Y%m%dT%H%M%SZ)"
