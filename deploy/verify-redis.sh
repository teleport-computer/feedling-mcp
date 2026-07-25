#!/bin/sh
# deploy/verify-redis.sh — Redis CVM 连通性冒烟。
#
# 用法:
#   REDIS_CA_FILE=./ca.crt REDISCLI_AUTH=<password> \
#     ./verify-redis.sh <app-id>-6379s.dstack-pha-prod9.phala.network 443
#
# 全绿 = TLS 握手 + 主机名校验 + AUTH + 读写 + TTL 语义 + INFO 均正常。
set -eu

fatal() { echo "[verify] FATAL: $*" >&2; exit 1; }

HOST="${1:?usage: verify-redis.sh <host> <port>}"
PORT="${2:?usage: verify-redis.sh <host> <port>}"

[ -n "${REDIS_CA_FILE:-}" ]  || fatal "REDIS_CA_FILE not set (ca.crt from gen-certs.sh)"
[ -f "${REDIS_CA_FILE}" ]    || fatal "CA file not found: ${REDIS_CA_FILE}"
# 口令从 REDISCLI_AUTH 读，绝不放 argv（会进 shell history 与进程列表）。
[ -n "${REDISCLI_AUTH:-}" ]  || fatal "REDISCLI_AUTH not set"

# --sni 是必须的，不是可选：dstack gateway 的 <app-id>-<port>s passthrough 靠
# TLS ClientHello 的 SNI 把连接路由到正确的后端 CVM。redis-cli --tls 默认不发
# SNI（只把 -h 当连接地址），gateway 因此找不到后端、在 TLS 握手时直接关闭连接，
# 客户端看到的就是 "unexpected eof while reading"（TCP 通、握手前对端 0 字节）。
# 实测 2026-07-25：不带 --sni 恒 eof，带上 = PONG。任何消费方（含 redis-monitor
# 及将来的应用客户端）连这个 gateway 都必须发 SNI = 完整 gateway 主机名。
R="redis-cli --tls --cacert ${REDIS_CA_FILE} --sni ${HOST} -h ${HOST} -p ${PORT}"
KEY="__verify_smoke_$(date -u +%s)"

echo "[verify] target ${HOST}:${PORT}"

[ "$($R PING)" = "PONG" ] || fatal "PING failed (TLS handshake or AUTH)"
echo "[verify] PING ok"

$R SET "$KEY" hello EX 60 >/dev/null || fatal "SET failed"
[ "$($R GET "$KEY")" = "hello" ]     || fatal "GET returned unexpected value"
TTL="$($R TTL "$KEY")"              || fatal "TTL query failed (redis-cli exited non-zero)"
[ "$TTL" -gt 0 ] 2>/dev/null         || fatal "TTL not honored (got ${TTL})"
$R DEL "$KEY" >/dev/null             || fatal "DEL failed"
echo "[verify] SET/GET/TTL/DEL ok"

# CONFIG 被 rename-command 禁用，容量信息只能从 INFO 读。
MEM="$($R INFO memory | tr -d '\r' | grep -E '^(used_memory|maxmemory):' | tr '\n' ' ')"
[ -n "$MEM" ] || fatal "INFO memory returned nothing"
echo "[verify] INFO memory: ${MEM}"

PERSIST="$($R INFO persistence | tr -d '\r' | grep -E '^(aof_enabled|aof_last_write_status|rdb_last_bgsave_status):' | tr '\n' ' ')"
echo "[verify] INFO persistence: ${PERSIST}"
case "$PERSIST" in
    *aof_enabled:1*) ;;
    *) fatal "AOF is not enabled — first persistence layer is missing" ;;
esac

# aof_last_write_status / rdb_last_bgsave_status: 只断言「不是 err」，不要求
# 等于 "ok"。rdb_last_bgsave_status 在一台从未跑过 bgsave 的全新实例上也
# 合法地读 "ok"（这是 Redis 的默认值，不代表已经成功存过盘），所以不能把
# "必须已经成功跑过一次" 当作及格线；但两个字段任一读到 "err" 都是明确
# 的写失败信号——上一次 AOF 落盘或 RDB 快照失败了，必须让 verify 炸掉。
case "$PERSIST" in
    *aof_last_write_status:err*) fatal "AOF last write failed (aof_last_write_status:err) — Redis is not durably persisting writes" ;;
esac
case "$PERSIST" in
    *rdb_last_bgsave_status:err*) fatal "RDB last bgsave failed (rdb_last_bgsave_status:err) — last snapshot attempt did not complete" ;;
esac
echo "[verify] persistence write status ok (no err in aof_last_write_status / rdb_last_bgsave_status)"

echo "[verify] ALL GREEN"
