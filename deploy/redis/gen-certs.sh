#!/bin/bash
# deploy/redis/gen-certs.sh — 一次性生成 redis CVM 的 TLS 材料。
# CA 私钥 (ca.key) 离线冷存，绝不进任何 CVM/CI。
# 用法: ./gen-certs.sh feedling-redis-test <输出目录>
#
# 与 deploy/postgres/gen-certs.sh 同形态：app_id 在首次 phala deploy 后才知道，
# 故 SAN 用通配覆盖 gateway 的 <app-id>-6379s.<domain>，客户端因此仍可 verify-full。
set -euo pipefail
NAME="${1:?usage: gen-certs.sh <cvm-name> <outdir>}"
OUT="${2:?usage: gen-certs.sh <cvm-name> <outdir>}"
mkdir -p "$OUT" && cd "$OUT"

# 拒绝在已有 CA 材料的目录上重跑：静默覆盖 ca.key/ca.crt 会让这把 CA
# 已经签发过的每一张证书（分发给消费方的 ca.crt、任何已重签的 server
# 证书）全部作废，且不会有任何报错提示——出问题只会在下次 TLS 握手时
# 才表现出来。要重签 server 证书就换一个空目录跑（同一把 CA 得先把
# 离线冷存的 ca.key/ca.crt 放回这个目录）；真要轮换 CA 才手动删掉重跑。
if [ -f ca.key ] || [ -f ca.crt ]; then
    echo "gen-certs.sh: ${OUT} 已有 ca.key/ca.crt，拒绝覆盖。" >&2
    echo "  换一个空目录重跑；如果是要用同一把 CA 重签 server 证书，" >&2
    echo "  先把离线冷存的 ca.key/ca.crt 放回这个目录再跑。" >&2
    echo "  真要轮换 CA：手动删掉这两个文件后再重跑（会让旧证书全部失效）。" >&2
    exit 1
fi

openssl req -new -x509 -days 3650 -nodes -keyout ca.key -out ca.crt \
  -subj "/CN=${NAME}-ca"
openssl req -new -nodes -keyout server.key -out server.csr \
  -subj "/CN=${NAME}"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -out server.crt \
  -extfile <(printf "subjectAltName=DNS:*.dstack-pha-prod9.phala.network")
rm server.csr
chmod 600 ca.key server.key
echo "== 注入 redis CVM 的加密 env 值 =="
echo "REDIS_TLS_CERT_B64=$(base64 < server.crt | tr -d '\n')"
echo "REDIS_TLS_KEY_B64=$(base64 < server.key | tr -d '\n')"
echo "== 分发给消费方（非机密） =="
echo "ca.crt → 各消费方镜像内 /etc/feedling/redis-ca.crt（客户端用 ssl_ca_certs 指向它）"
echo "== ca.key 立即移到离线冷存，从 ${OUT} 删除 =="
