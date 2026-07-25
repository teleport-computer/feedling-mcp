#!/bin/sh
# deploy/redis/restore.sh — 灾难恢复 / 演练。
#
# 用法:
#   REDIS_BACKUP_AGE_IDENTITY_FILE=<离线私钥> REDIS_BACKUP_BUCKET=... \
#   REDIS_BACKUP_S3_PREFIX=<env>/redis/ AWS_ENDPOINT_URL=... AWS_ACCESS_KEY_ID=... \
#   AWS_SECRET_ACCESS_KEY=... RESTORE_DIR=/data ./restore.sh [OBJECT_KEY]
#
# 省略 OBJECT_KEY 则取前缀下最新的一份（key 字典序 = 时间序）。
# 产出 ${RESTORE_DIR}/dump.rdb，启动 Redis 即加载。
#
# fail-closed：我们的备份必然加密，没私钥就是配置错了——不抄任何
# 「未加密也能恢复」的可选分支（同 deploy/postgres/restore.sh）。
#
# 运行方式：本镜像的 sidecar 服务从不挂载 redisdata 卷（快照走
# redis-cli --rdb，从不读卷内文件，见 backup-push.sh 的 D4）。恢复时要
# 从这个镜像单独起一个一次性容器，显式挂上 redisdata 卷和离线身份文件，
# 例如：
#   docker run --rm -v redisdata:/data -v /offline/identity.txt:/id.txt:ro \
#     -e REDIS_BACKUP_AGE_IDENTITY_FILE=/id.txt -e REDIS_BACKUP_BUCKET=... \
#     -e REDIS_BACKUP_S3_PREFIX=<env>/redis/ -e AWS_ENDPOINT_URL=... \
#     -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... \
#     --entrypoint restore.sh feedling-redis-backup:local
# 完整演练 runbook 见后续文档任务。
set -eu

fatal() { echo "[restore] FATAL: $*" >&2; exit 1; }

[ -n "${REDIS_BACKUP_AGE_IDENTITY_FILE:-}" ] \
    || fatal "REDIS_BACKUP_AGE_IDENTITY_FILE required (offline age private key)"
[ -f "${REDIS_BACKUP_AGE_IDENTITY_FILE}" ] \
    || fatal "identity file not found: ${REDIS_BACKUP_AGE_IDENTITY_FILE}"
[ -n "${REDIS_BACKUP_BUCKET:-}" ]    || fatal "REDIS_BACKUP_BUCKET required"
[ -n "${REDIS_BACKUP_S3_PREFIX:-}" ] || fatal "REDIS_BACKUP_S3_PREFIX required"
[ -n "${AWS_ENDPOINT_URL:-}" ]       || fatal "AWS_ENDPOINT_URL required"
# R2 凭证跟上面几个一样走 "${VAR:-}" 加密注入，注入失败同样表现为空
# 字符串而非未设置，set -u 逮不到（同 backup-push.sh 的同一条注释）。
# 灾难恢复现场最不需要的就是把这个疏漏留给 aws 命令去报一个不知所云的
# 认证错误。
[ -n "${AWS_ACCESS_KEY_ID:-}" ]      || fatal "AWS_ACCESS_KEY_ID required"
[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]  || fatal "AWS_SECRET_ACCESS_KEY required"

RESTORE_DIR="${RESTORE_DIR:-/data}"
OBJECT="${1:-}"

if [ -z "$OBJECT" ]; then
    echo "[restore] available backups:"
    LISTING="$(aws s3api list-objects-v2 \
        --bucket "$REDIS_BACKUP_BUCKET" \
        --prefix "$REDIS_BACKUP_S3_PREFIX" \
        --query 'Contents[].Key' --output text)" \
        || fatal "listing failed"
    [ -n "$LISTING" ] && [ "$LISTING" != "None" ] || fatal "no backups under ${REDIS_BACKUP_S3_PREFIX}"
    printf '%s\n' $LISTING | sort
    # key 形如 redis-20260724T110000Z.rdb.age：字典序 = 时间序，故末行最新。
    OBJECT="$(printf '%s\n' $LISTING | sort | tail -n1)"
fi

echo "[restore] restoring from ${OBJECT}"
mkdir -p "$RESTORE_DIR"
WORK="$(mktemp -d)"
DEST_TMP=""
# 注意：这里不能写成 `[ -n "$DEST_TMP" ] && rm -f "$DEST_TMP"`——在 set -e
# 下，EXIT trap 最后一条命令的退出码会成为脚本最终退出码，条件为假时
# `&&` 整体判 1，会把一次成功的 restore 冒充成失败。`if` 分支缺 else 时
# 恒为 0，不会有这个副作用。
cleanup() { rm -rf "$WORK"; if [ -n "$DEST_TMP" ]; then rm -f "$DEST_TMP"; fi; }
trap cleanup EXIT INT TERM

aws s3 cp "s3://${REDIS_BACKUP_BUCKET}/${OBJECT}" "${WORK}/snap.rdb.age" \
    || fatal "download failed: ${OBJECT}"

# 解密直接写到 RESTORE_DIR 内部的临时名，而不是 WORK（mktemp -d 落在
# /tmp，跟 RESTORE_DIR 通常不是同一个挂载点——compose 里 RESTORE_DIR=
# /data 是独立的 redisdata 卷）。这样最后落地只需要同目录内的 rename，
# 而不是跨设备 mv（copy+unlink，中途失败会在目的地留下截断文件）。
# 顺带一提：这一步失败也说明 RESTORE_DIR 是否可写，比等到最后 mv 才
# 发现更早暴露问题。
DEST_TMP="$(mktemp "${RESTORE_DIR}/.dump.rdb.XXXXXX")" \
    || fatal "could not create temp file in ${RESTORE_DIR} — is it writable?"

age -d -i "$REDIS_BACKUP_AGE_IDENTITY_FILE" -o "$DEST_TMP" "${WORK}/snap.rdb.age" \
    || fatal "decryption failed — wrong identity file?"

# RDB 文件必须以 REDIS 魔数开头；解出来的不是 RDB 就别往 dir 里放，
# 免得 Redis 启动时报一个难懂的错。
head -c 5 "$DEST_TMP" | grep -q "REDIS" \
    || fatal "decrypted file is not an RDB snapshot"

# 目的地文件名与 DEST_TMP 同在 RESTORE_DIR 下 → 同一文件系统 → rename(2)
# 是原子的，不会有「拷贝到一半」的中间态。仍然守卫失败（权限在两步之间
# 被收回、只读重挂等边缘情况），失败时 trap 会清掉 DEST_TMP，不留残留。
mv "$DEST_TMP" "${RESTORE_DIR}/dump.rdb" \
    || fatal "failed to place restored snapshot at ${RESTORE_DIR}/dump.rdb"
DEST_TMP=""
echo "[restore] wrote ${RESTORE_DIR}/dump.rdb"
echo "[restore] NOTE: 生产镜像的 redis.conf 硬编码 appendonly yes 且用"
echo "[restore]       rename-command 禁用了 CONFIG——不能靠 CONFIG SET"
echo "[restore]       appendonly no 绕过。卷里没有 appendonlydir/ 时 Redis 会把"
echo "[restore]       这当成数据集为空，直接忽略这份 dump.rdb，下一轮定时"
echo "[restore]       BGSAVE 还会用空数据把它覆盖掉。完整恢复步骤（用不带这层"
echo "[restore]       加固的临时容器把 dump.rdb 转成合法 AOF，再启动生产服务）"
echo "[restore]       见 deploy/DEPLOYMENTS.md「TEE Redis」的恢复流程。"
