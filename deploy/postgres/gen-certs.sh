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
