#!/bin/sh
# deploy/redis/entrypoint-wrapper.sh — 校验 → TLS 材料 → 机密配置 → 启动。
# fail-closed：缺任何必需机密就退出，绝不降级成明文或无备份运行。
set -eu

# 材料落盘路径可覆盖，默认值与线上一致——测试借此把落盘目标
# 重定向进 tmp_path，生产行为字节不变。
REDIS_TLS_DIR="${REDIS_TLS_DIR:-/etc/redistls}"
REDIS_CONF_DIR="${REDIS_CONF_DIR:-/etc/redis}"
REDIS_RUN_DIR="${REDIS_RUN_DIR:-/var/run/redis}"

fatal() { echo "[redis-init] FATAL: $*" >&2; exit 1; }

[ -n "${REDIS_PASSWORD:-}" ]      || fatal "REDIS_PASSWORD not set"
[ -n "${REDIS_TLS_CERT_B64:-}" ]  || fatal "REDIS_TLS_CERT_B64 not set"
[ -n "${REDIS_TLS_KEY_B64:-}" ]   || fatal "REDIS_TLS_KEY_B64 not set"
[ -n "${REDIS_MAXMEMORY:-}" ]     || fatal "REDIS_MAXMEMORY not set"

# 备份 fail-closed：配了目的地就必须有加密公钥，绝不把明文快照推出 TEE。
# （对应 PG 的 entrypoint-wrapper.sh「修正 4」。）
if [ -n "${REDIS_BACKUP_S3_PREFIX:-}" ]; then
    [ -n "${REDIS_BACKUP_AGE_RECIPIENT:-}" ] \
        || fatal "REDIS_BACKUP_S3_PREFIX set but REDIS_BACKUP_AGE_RECIPIENT missing — refusing to ship plaintext snapshots"
    echo "${REDIS_BACKUP_AGE_RECIPIENT}" | grep -Eq '^age1[0-9a-z]{58}$' \
        || fatal "REDIS_BACKUP_AGE_RECIPIENT is not a valid age public key"
    echo "[redis-init] backups configured (age-encrypted → ${REDIS_BACKUP_S3_PREFIX})"
else
    echo "[redis-init] WARNING: backups NOT configured — acceptable only for local/scratch" >&2
fi

# --- TLS 材料落盘 ---
# busybox 的 `base64 -d` 对非法输入照样 exit 0（只把随机字节写进文件），
# 所以 `base64 -d ... || fatal` 这种写法测不出任何东西——必须检查解码
# 出来的内容本身长得像不像 PEM。镜像里没有 openssl（已用 pinned digest
# 实测确认），所以只能靠 grep 做结构校验；解到临时文件、校验通过才
# mv 到最终路径，校验失败时临时文件被删除，保证 fail-closed 时文件系统
# 上不会留下半成品。
decode_tls_material() {
    var_name="$1" out_path="$2" kind="$3"  # kind: cert | key
    eval "b64_val=\"\${$var_name}\""
    tmp_path="${out_path}.tmp"
    # `|| true`：不同 base64 实现对非法输入的退出码不一致（busybox 恒
    # 0，BSD/GNU 可能非 0），我们本来就不信任这个退出码、只信任下面对
    # 解码内容的结构校验，所以要把它从 `set -e` 里摘出来，否则退出码
    # 非 0 时脚本会在这里被 `set -e` 直接杀掉，连 fatal() 的报错信息
    # 都不会打印。
    printf '%s' "$b64_val" | base64 -d > "$tmp_path" 2>/dev/null || true

    # 注意：grep 必须包在 if 里而不是裸跑——裸跑在 `set -e` 下一旦非零
    # 会让整个脚本直接终止，绕过下面的 fatal()，丢失指名出错变量的
    # 报错信息。
    valid=0
    case "$kind" in
        cert)
            if grep -q -- '-----BEGIN CERTIFICATE-----' "$tmp_path" \
                && grep -q -- '-----END CERTIFICATE-----' "$tmp_path"; then
                valid=1
            fi
            ;;
        key)
            # 私钥 PEM 头有好几种（RSA/EC/PKCS8 通用私钥），统一按
            # "BEGIN ... PRIVATE KEY" 匹配。
            if grep -Eq -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "$tmp_path" \
                && grep -Eq -- '-----END [A-Z ]*PRIVATE KEY-----' "$tmp_path"; then
                valid=1
            fi
            ;;
    esac

    if [ "$valid" -ne 1 ]; then
        rm -f "$tmp_path"
        fatal "${var_name} did not decode to a valid PEM ${kind}"
    fi

    mv "$tmp_path" "$out_path"
}

mkdir -p "${REDIS_TLS_DIR}"
decode_tls_material REDIS_TLS_CERT_B64 "${REDIS_TLS_DIR}/server.crt" cert
decode_tls_material REDIS_TLS_KEY_B64  "${REDIS_TLS_DIR}/server.key" key
chmod 600 "${REDIS_TLS_DIR}/server.key"
chmod 644 "${REDIS_TLS_DIR}/server.crt"

# --- 机密配置：绝不走命令行参数，否则口令出现在容器内进程列表 ---
mkdir -p "${REDIS_CONF_DIR}"
umask 077
{
    echo "requirepass ${REDIS_PASSWORD}"
    echo "maxmemory ${REDIS_MAXMEMORY}"
} > "${REDIS_CONF_DIR}/secret.conf"
umask 022

# --- sidecar 的 socket 目录（共享 volume 挂载点）---
mkdir -p "${REDIS_RUN_DIR}"

# 官方镜像以 root 启动、由 docker-entrypoint.sh 降权到 redis 用户运行，
# 故这些文件的属主要跟着改，否则降权后读不到 key、写不了 socket。
if id redis >/dev/null 2>&1; then
    chown redis:redis "${REDIS_TLS_DIR}/server.crt" "${REDIS_TLS_DIR}/server.key" \
                      "${REDIS_CONF_DIR}/secret.conf" "${REDIS_RUN_DIR}"
fi

# DRY_RUN 供测试用：走完全部校验与落盘，停在 exec 之前。
[ -n "${DRY_RUN:-}" ] && { echo "[redis-init] dry run OK"; exit 0; }

exec docker-entrypoint.sh redis-server "${REDIS_CONF_DIR}/redis.conf"
