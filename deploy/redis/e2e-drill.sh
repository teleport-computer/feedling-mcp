#!/bin/bash
# deploy/redis/e2e-drill.sh — 本地端到端备份/恢复演练。
#
# 这是 spec 第 8 节「restore 演练」的可重复版本：写已知数据 → 真实
# redis-cli --rdb 快照 → 真实 age 加密 → 推到 S3 兼容存储（MinIO 冒充
# R2）→ 在一个从未见过这份数据的空 Redis 上，用镜像里自带的 restore.sh
# 恢复 → 逐项比对（key 数量、抽样值逐字节、TTL 语义）。CVM 首次开通时
# 用同样的流程对着真 R2 跑一遍（见 Task 12 / deploy/DEPLOYMENTS.md）。
#
# 用镜像自带的 restore.sh（Dockerfile.backup 已把它 COPY 进
# /usr/local/bin/restore.sh），不再额外 bind-mount 脚本进容器——这样
# 演练验证的是真正会出厂的那一份，不是本地工作区里可能已经漂移的副本。
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.e2e.yaml -p feedling-redis-e2e"
NETWORK="feedling-redis-e2e-net"
WORK="$(mktemp -d)"

# 一次性容器名字集中记录，cleanup 时逐个尝试删除；即使脚本在中途某一步
# 失败退出，trap 也要能把它们连同 compose 服务一起收干净。
ONE_OFF_CONTAINERS="redis-e2e-restore-verify"

cleanup() {
    local rc=$?
    echo "== 清理 =="
    for c in $ONE_OFF_CONTAINERS; do
        docker rm -f "$c" >/dev/null 2>&1 || true
    done
    $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
    if [ "$rc" -ne 0 ]; then
        echo "== DRILL FAILED (exit $rc) ==" >&2
    fi
    return "$rc"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "== 0. 构建镜像 =="
# 显式 build（而不是让 up 隐式建）：镜像构建失败要在这里就吵出来，
# 报出的是 build 步骤的名字，不是后面某个 depends_on 超时的谜语。
$COMPOSE build
# 不用 `compose images -q backup`——那条命令只列举「已有容器」对应的
# 镜像，build（不 up）之后 backup 服务还没有容器，会静默返回空字符串。
# 直接用 compose 项目的默认镜像命名规则 `<project>-<service>:latest`
# （已用 `docker images` 实测确认），构建后立刻校验它真的存在。
BACKUP_IMAGE="feedling-redis-e2e-backup:latest"
docker image inspect "$BACKUP_IMAGE" >/dev/null 2>&1 \
    || fail "backup 镜像构建后在本地找不到: ${BACKUP_IMAGE}"

echo "== 1. 生成 age 密钥对与 TLS 材料 =="
# age 只存在于 backup 镜像里（host 上没装），用刚构建出的镜像自己的
# age-keygen 生成——这样连密钥生成都是"真实链路"的一部分，不需要
# 额外从网络给某个临时容器装包。
docker run --rm --entrypoint age-keygen "$BACKUP_IMAGE" > "$WORK/age-key.txt" 2>/dev/null \
    || fail "age-keygen 失败"
E2E_AGE_RECIPIENT="$(grep -i 'public key:' "$WORK/age-key.txt" | sed 's/.*: //')"
[ -n "$E2E_AGE_RECIPIENT" ] || fail "没能从 age-keygen 输出里解析出公钥"
grep -v '^#' "$WORK/age-key.txt" > "$WORK/identity.txt"
export E2E_AGE_RECIPIENT
echo "age 公钥: ${E2E_AGE_RECIPIENT}"

./gen-certs.sh feedling-redis-e2e "$WORK/certs" >/dev/null
export E2E_CERT_B64
export E2E_KEY_B64
E2E_CERT_B64="$(base64 < "$WORK/certs/server.crt" | tr -d '\n')"
E2E_KEY_B64="$(base64 < "$WORK/certs/server.key" | tr -d '\n')"

echo "== 2. 起 MinIO + Redis，建桶 =="
# --wait：等 healthcheck 真正过，而不是猜一个 sleep 时长。
$COMPOSE up -d --wait minio redis
$COMPOSE exec -T minio \
    sh -c 'mc alias set local http://127.0.0.1:9000 e2eaccess e2esecret123 >/dev/null && mc mb -p local/e2e-bucket' \
    || fail "MinIO 建桶失败"

echo "== 3. 写入已知数据 =="
REDIS_EXEC="$COMPOSE exec -T redis redis-cli -s /var/run/redis/redis.sock"
for i in $(seq 1 100); do
    $REDIS_EXEC SET "drill:key:$i" "value-$i" >/dev/null
done
$REDIS_EXEC SET "drill:with-ttl" "expires-soon" EX 3600 >/dev/null
# 顺带验证跨 SYNC快照/age/S3 全链路的字节保真度，不只是纯 ASCII。
$REDIS_EXEC SET "drill:unicode" 'unicode 测试 emoji 🎉 punctuation: comma, period. dash-under_score "quoted"' >/dev/null

BEFORE_COUNT="$($REDIS_EXEC DBSIZE | tr -d '\r')"
echo "写入完成，DBSIZE=${BEFORE_COUNT}"
test "$BEFORE_COUNT" -eq 102 || fail "期望 102 个 key，实际 ${BEFORE_COUNT}"

BEFORE_KEY42="$($REDIS_EXEC GET drill:key:42 | tr -d '\r')"
BEFORE_UNICODE="$($REDIS_EXEC GET drill:unicode | tr -d '\r')"
test "$BEFORE_KEY42" = "value-42" || fail "写入后立即读回 drill:key:42 就已经不对，测试前置条件有问题"

echo "== 4. 触发备份（sidecar 的 boot 兜底那一轮，BACKUP_INTERVAL_SEC=0 跑完即退出）=="
# --exit-code-from：backup-loop.sh 任何一步 fatal 都必须让这条命令
# 非零退出——不然「秘密校验失败/推送失败」会被 up 命令的默认退出码
# 悄悄吞掉，脚本却继续往下走去检查一个从未真正产生的快照。
$COMPOSE up --exit-code-from backup --abort-on-container-exit backup \
    || fail "backup sidecar 非零退出——见上面它自己的日志"

echo "== 4b. 独立核实 R2(MinIO) 上确实落了快照（不经过 restore.sh，避免自证）=="
SNAPSHOT_KEYS="$(docker run --rm --network "$NETWORK" \
    -e AWS_ACCESS_KEY_ID=e2eaccess -e AWS_SECRET_ACCESS_KEY=e2esecret123 \
    -e AWS_REGION=auto -e AWS_DEFAULT_REGION=auto \
    -e AWS_ENDPOINT_URL=http://minio:9000 \
    --entrypoint aws "$BACKUP_IMAGE" \
    s3api list-objects-v2 --bucket e2e-bucket --prefix e2e/redis/ \
    --query 'Contents[].Key' --output text)" \
    || fail "列举 MinIO 里的快照失败"
echo "R2(MinIO) 快照列表: ${SNAPSHOT_KEYS}"
[ -n "$SNAPSHOT_KEYS" ] && [ "$SNAPSHOT_KEYS" != "None" ] || fail "没有快照落地"
case "$SNAPSHOT_KEYS" in
    *e2e/redis/redis-*.rdb.age*) ;;
    *) fail "落地的对象 key 名字不符合预期格式(e2e/redis/redis-<ts>.rdb.age): ${SNAPSHOT_KEYS}" ;;
esac

echo "== 5. 在空目录上恢复（镜像自带的 restore.sh，不省略 OBJECT_KEY——顺带测它自动挑最新一份的逻辑）=="
mkdir -p "$WORK/restored"
docker run --rm --network "$NETWORK" \
    -v "$WORK/restored:/data" \
    -v "$WORK/identity.txt:/identity.txt:ro" \
    -e REDIS_BACKUP_AGE_IDENTITY_FILE=/identity.txt \
    -e REDIS_BACKUP_BUCKET=e2e-bucket \
    -e REDIS_BACKUP_S3_PREFIX=e2e/redis/ \
    -e AWS_ENDPOINT_URL=http://minio:9000 \
    -e AWS_ACCESS_KEY_ID=e2eaccess \
    -e AWS_SECRET_ACCESS_KEY=e2esecret123 \
    -e AWS_REGION=auto -e AWS_DEFAULT_REGION=auto \
    -e RESTORE_DIR=/data \
    --entrypoint /usr/local/bin/restore.sh \
    "$BACKUP_IMAGE" \
    || fail "restore.sh 非零退出"

[ -f "$WORK/restored/dump.rdb" ] || fail "restore.sh 报告成功，但 ${WORK}/restored/dump.rdb 并不存在"
[ -s "$WORK/restored/dump.rdb" ] || fail "恢复出来的 dump.rdb 是空文件"
head -c 5 "$WORK/restored/dump.rdb" | grep -q "REDIS" || fail "恢复出来的文件不是有效 RDB（缺 REDIS 魔数）"

echo "== 6. 在一个从未见过这份数据的空 Redis 上校验 =="
# appendonly no 是硬要求：生产 redis.conf 硬编码 appendonly yes，这个
# 设置本身（不需要真的已经存在 appendonlydir/）就会让 Redis 把数据集当
# 成空、直接不读 dump.rdb（详见 deploy/DEPLOYMENTS.md「TEE Redis」恢复
# 流程 + restore.sh 自己打印的提醒）。这里显式关掉 AOF 让它走 RDB 加载
# 路径。同时特意不用我们的生产 entrypoint-wrapper.sh / Dockerfile 起这
# 个校验实例——校验的是「dump.rdb 里的数据对不对」，不是又测一遍
# fail-closed 启动校验（那部分已由 Task 1-6 的单测覆盖），用官方镜像
# 直接起更干净。
docker run --rm -d --name redis-e2e-restore-verify \
    -v "$WORK/restored:/data" \
    redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005 \
    redis-server --appendonly no --dir /data --dbfilename dump.rdb >/dev/null

READY=0
for _ in $(seq 1 20); do
    if docker exec redis-e2e-restore-verify redis-cli ping 2>/dev/null | grep -q PONG; then
        READY=1
        break
    fi
    sleep 0.5
done
[ "$READY" -eq 1 ] || fail "校验用的 Redis 20 次探测后仍未就绪——恢复出来的 dump.rdb 可能根本无法加载"

AFTER_COUNT="$(docker exec redis-e2e-restore-verify redis-cli DBSIZE | tr -d '\r')"
AFTER_KEY42="$(docker exec redis-e2e-restore-verify redis-cli GET drill:key:42 | tr -d '\r')"
AFTER_UNICODE="$(docker exec redis-e2e-restore-verify redis-cli GET drill:unicode | tr -d '\r')"
TTL="$(docker exec redis-e2e-restore-verify redis-cli TTL drill:with-ttl | tr -d '\r')"

echo "恢复前 DBSIZE=${BEFORE_COUNT}  恢复后 DBSIZE=${AFTER_COUNT}"
echo "抽样 drill:key:42:    恢复前=[${BEFORE_KEY42}] 恢复后=[${AFTER_KEY42}]"
echo "抽样 drill:unicode:   恢复前=[${BEFORE_UNICODE}] 恢复后=[${AFTER_UNICODE}]"
echo "TTL drill:with-ttl=${TTL}（期望 >0 且 <=3600）"

test "$AFTER_COUNT" -eq "$BEFORE_COUNT"    || fail "key 数量不一致：恢复前 ${BEFORE_COUNT}，恢复后 ${AFTER_COUNT}"
test "$AFTER_KEY42" = "$BEFORE_KEY42"      || fail "抽样值 drill:key:42 不一致"
test "$AFTER_KEY42" = "value-42"           || fail "抽样值 drill:key:42 不等于预期的 value-42（说明比较对象本身就错了）"
test "$AFTER_UNICODE" = "$BEFORE_UNICODE"  || fail "unicode/多字节抽样值不一致——字节保真度没过"
test -n "$TTL"                             || fail "TTL 查询没有返回值"
test "$TTL" -gt 0                          || fail "TTL 语义未保留（<=0 意味着 key 已过期或从未设置 TTL）"
test "$TTL" -le 3600                       || fail "TTL 大于写入时设置的 3600，恢复逻辑可疑"

echo "== DRILL PASSED =="
