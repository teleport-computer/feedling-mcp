#!/bin/sh
# deploy/redis/backup-loop.sh — sidecar 主进程。
#
# D3：刻意不用 cron 触发备份。PG 那边 2026-07-14 的事故
# 就是 cron 以精简 PATH=/usr/bin:/bin 执行，找不到
# /usr/local/bin/wal-g，每日 base backup 静默失败很久，直到排查才发现
# 只剩建库时那一份 base、retain 从没跑成、WAL 在 R2 无限堆积。显式
# sleep 循环直接继承容器环境，失败也进容器日志。
set -eu

fatal() { echo "[backup-loop] FATAL: $*" >&2; exit 1; }

# fail-closed：缺任何必需机密就立刻退出，绝不带着一个必定失败的配置
# 静默转圈。set -u 只抓「未设置」，加密注入的缺失机密实际表现为「设置成
# 空字符串」，所以要显式判空——这面镜子照的是 backup-push.sh 里同样的
# 七个检查，两边必须保持一致，否则循环会启动然后每小时喂给 push 一个它
# 自己也会拒绝的配置，把致命错误伪装成「等下一轮」。
[ -n "${REDIS_BACKUP_S3_PREFIX:-}" ]     || fatal "REDIS_BACKUP_S3_PREFIX not set"
[ -n "${REDIS_BACKUP_AGE_RECIPIENT:-}" ] || fatal "REDIS_BACKUP_AGE_RECIPIENT not set — refusing to ship plaintext snapshot"
[ -n "${REDIS_BACKUP_BUCKET:-}" ]        || fatal "REDIS_BACKUP_BUCKET not set"
[ -n "${AWS_ENDPOINT_URL:-}" ]           || fatal "AWS_ENDPOINT_URL not set"
[ -n "${AWS_ACCESS_KEY_ID:-}" ]          || fatal "AWS_ACCESS_KEY_ID not set"
[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]      || fatal "AWS_SECRET_ACCESS_KEY not set"
[ -n "${REDISCLI_AUTH:-}" ]              || fatal "REDISCLI_AUTH not set"

PUSH="${BACKUP_PUSH_BIN:-/usr/local/bin/backup-push.sh}"
INTERVAL="${BACKUP_INTERVAL_SEC:-3600}"

echo "[backup-loop] starting (interval=${INTERVAL}s)"

# --- boot 兜底：前缀下没有任何快照就立刻推一份，不等第一个周期 ---
# （对应 PG entrypoint-wrapper.sh 的「修正 3」。）
EXISTING="$(aws s3api list-objects-v2 \
    --bucket "${REDIS_BACKUP_BUCKET}" \
    --prefix "${REDIS_BACKUP_S3_PREFIX}" \
    --query 'Contents[].Key' --output text 2>/dev/null || echo "LIST_FAILED")"

if [ "$EXISTING" = "LIST_FAILED" ]; then
    echo "[backup-loop] WARNING: initial listing failed — proceeding to normal schedule" >&2
elif [ -z "$EXISTING" ] || [ "$EXISTING" = "None" ]; then
    echo "[backup-loop] no snapshot found — pushing one now"
    # 这里不做 `|| echo` 吞掉——boot 阶段跑到这一步，必需机密都已经校验
    # 通过，redis 也已经 healthy（compose 里 depends_on: condition:
    # service_healthy），push 失败说明的是真实的运行时故障（凭证错误、
    # R2 不可达等），不是「等下一轮就好」的抖动。这个取舍依赖 compose 的
    # `restart: unless-stopped`语义，分两种情形看：
    #   - 抖动是暂时的：fatal 退出触发立刻重启重试，比静默等一个
    #     BACKUP_INTERVAL_SEC（生产是 3600s）之后才补救快得多——严格优于
    #     「等下一轮」。
    #   - 抖动是持续的（凭证真的坏了、R2 真的不可达）：容器会反复重启崩溃，
    #     这是有意为之的可见故障信号；换成 `|| echo` 吞掉，容器会一直
    #     显示健康、日志安安静静，实际上从没有成功过一次备份——那才是
    #     更危险的状态。
    "$PUSH" || fatal "initial push failed"
fi

while true; do
    # INTERVAL=0 是测试钩子：跑一轮就退出。
    if [ "$INTERVAL" = "0" ]; then
        exit 0
    fi
    sleep "$INTERVAL"
    # 单轮失败不能杀掉循环，否则一次网络抖动就永久停掉备份。
    "$PUSH" || echo "[backup-loop] ERROR: scheduled push failed" >&2
done
