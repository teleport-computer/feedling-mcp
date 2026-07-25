# TEE Redis CVM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phala 上建成三套独立的 Redis CVM（test / pre / prod），带离场加密备份、监控与可验证的恢复流程，且**零业务流量**。

**Architecture:** 每台 CVM 跑两个容器——官方 `redis:8-alpine`（TLS-only 对外，unix socket 对内）+ 自建 backup sidecar（每小时 `redis-cli --rdb` 快照 → age 加密 → R2）。部署纪律逐条复刻 TEE Postgres：`--kms phala` 身份（无链上 AppAuth）、独立手动 workflow、cvm-id 文件 fail-closed、永不并入 merge 自动部署。

**Tech Stack:** Redis 8.8 (官方 alpine 镜像)、age 1.2.1、aws-cli 2.32.7、Phala dstack CVM、GitHub Actions、pytest（基础设施用静态断言 + subprocess 跑真脚本）

**Spec:** `docs/superpowers/specs/2026-07-24-tee-redis-cvm-design.md`

## Global Constraints

- **Redis 镜像钉 digest**：`redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005`（已验证 Redis 8.8.0，编译带 TLS）。sidecar 用**同一 digest** 作基底，使 `redis-cli` 与服务端版本天然一致。
- **口令一律 `openssl rand -hex`**：十六进制无特殊字符。引号 / `$` / 反引号会破坏 compose env 注入（PG runbook 的既有教训）。
- **fail-closed 是硬规则**：缺任何必需机密（密码、TLS 材料、备份公钥）→ 容器**拒绝启动**，绝不降级成明文或无备份运行。
- **GitHub Actions 里绝不用三元表达式选机密**：`${{ env == 'prod' && secrets.PROD_X || secrets.TEST_X }}` 在 `PROD_X` 恰好为空时会短路 fallback 到 `TEST_X`——非空预检照样通过，注进 prod 的却是 test 的密码。三套机密全部注入 job env，在 shell 里按环境名前缀间接取值（`pick()`），挑错只会挑到空值 → fail-closed。（`pg-deploy.yml` 2026-07-24 修正的真实 bug。）
- **镜像 tag 用 `git rev-parse HEAD`，不用 `${{ github.sha }}`**：`workflow_dispatch` 下后者指向触发时所在 ref 的 sha，而 checkout 的是 test/pre/main，tag 会与镜像内容对不上。
- **`CONFIG` 命令被 `rename-command` 禁用**：监控与脚本**只能从 `INFO` 读取** `maxmemory` / `used_memory`，不得用 `CONFIG GET`（已实测：`ERR unknown command 'CONFIG'`）。
- **`maxmemory-policy noeviction`**：三环境一致，不得改成任何 `allkeys-*`（会静默驱逐锁与队列数据）。
- **本仓 commit 纪律**：仓库约定「只在用户明确要求时 commit」。各 Task 末尾的 commit 步骤在执行时须遵循用户当时的指示；若用户未授权，则把改动留在工作树并在报告中说明。
- **平台**：CVM 是 linux/amd64。本地在 arm64 Mac 上构建镜像时须 `docker build --platform linux/amd64`，否则推上去的镜像在 CVM 起不来。
- **测试基线**：本仓完整测试需要本地 Postgres（否则 conftest 静默少收集约 2000 用例）。本计划新增的测试**全部不依赖 DB**，可单独运行；但最终验收仍须在有 PG 的环境跑全量。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `deploy/redis/redis.conf` | 非机密的 Redis 配置：TLS-only、unix socket、AOF、noeviction、禁高危命令 |
| `deploy/redis/entrypoint-wrapper.sh` | fail-closed 校验 → TLS 材料落盘 → 生成含机密的 `secret.conf` → 启动 redis-server |
| `deploy/redis/Dockerfile` | Redis 服务端镜像：官方镜像 + 我们的 conf/entrypoint |
| `deploy/redis/Dockerfile.backup` | backup sidecar 镜像（同 digest 基底 + aws-cli + age） |
| `deploy/redis/backup-push.sh` | 单次快照：`--rdb` → age 加密 → 上传 R2 → 执行保留策略 |
| `deploy/redis/backup-loop.sh` | sidecar 主进程：boot 兜底 + 每小时循环调用 `backup-push.sh` |
| `deploy/redis/restore.sh` | 从 R2 取快照 → age 解密 → 落到目标 `dir` 供 Redis 加载 |
| `deploy/redis/gen-certs.sh` | 一次性生成 CA + server TLS 材料（CA 私钥离线冷存） |
| `deploy/docker-compose.phala.redis.yaml` | 三环境共用的 CVM compose；差异全走加密 env 注入 |
| `deploy/{test,pre,prod}-redis-cvm-id.txt` | 各环境 CVM id（fail-closed 的依据） |
| `deploy/verify-redis.sh` | 连通性冒烟：TLS + AUTH + SET/GET/TTL + INFO |
| `deploy/redis/docker-compose.e2e.yaml` | 本地端到端演练（MinIO 冒充 R2），不用于部署 |
| `deploy/redis/e2e-drill.sh` | 备份→恢复→校验的可重复演练 |
| `.github/workflows/redis-deploy.yml` | 手动部署 workflow（环境三选一 → 自动映射账号） |
| `.github/workflows/redis-monitor.yml` | 每 30 分钟备份链与内存监控（prod + pre） |
| `tests/test_redis_cvm_config.py` | redis.conf / compose / workflow 的静态不变量断言 |
| `tests/test_redis_backup_scripts.py` | backup/restore 脚本的行为测试（PATH stub + 真实 age 往返） |

**责任边界**：`backup-push.sh` 只做「一次快照」且必须可独立调用（测试与手动补推都用它）；循环与兜底逻辑隔离在 `backup-loop.sh`。这样备份的核心路径能在不等一小时的前提下被测试。

---

## Task 1: Redis 配置与 fail-closed entrypoint

**Files:**
- Create: `deploy/redis/redis.conf`
- Create: `deploy/redis/entrypoint-wrapper.sh`
- Test: `tests/test_redis_cvm_config.py`

**Interfaces:**
- Consumes: 无（第一个 Task）
- Produces:
  - `redis.conf` 末尾 `include /etc/redis/secret.conf`（后读覆盖先读，机密项必须在最后）
  - entrypoint 读取的环境变量：`REDIS_PASSWORD`、`REDIS_TLS_CERT_B64`、`REDIS_TLS_KEY_B64`、`REDIS_MAXMEMORY`、`REDIS_BACKUP_S3_PREFIX`、`REDIS_BACKUP_AGE_RECIPIENT`
  - 运行时路径：socket `/var/run/redis/redis.sock`、TLS 材料 `/etc/redistls/server.{crt,key}`、数据目录 `/data`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_redis_cvm_config.py`：

```python
"""TEE Redis CVM 的配置不变量。

这些断言保护的是「配错了会静默变得不安全」的项：明文端口没关、
驱逐策略被改成会吃掉锁和队列的 allkeys-*、高危命令没禁。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
REDIS_CONF = ROOT / "deploy" / "redis" / "redis.conf"
ENTRYPOINT = ROOT / "deploy" / "redis" / "entrypoint-wrapper.sh"


def _conf_directives(text: str) -> list[tuple[str, str]]:
    """Redis 配置是「指令 参数」的行序列，同名指令可重复（如 save）。"""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        out.append((name, rest.strip()))
    return out


def test_plaintext_port_is_closed_and_tls_is_the_only_tcp_listener():
    # port 0 关闭明文监听；gateway passthrough 让 Redis 端口在公网可达，
    # 明文监听等于把无认证前的握手暴露出去。
    directives = dict(_conf_directives(REDIS_CONF.read_text()))
    assert directives["port"] == "0"
    assert directives["tls-port"] == "6379"
    assert directives["tls-cert-file"] == "/etc/redistls/server.crt"
    assert directives["tls-key-file"] == "/etc/redistls/server.key"
    # 我们用密码认证而非双向 TLS；要求客户端证书会让所有消费方连不上。
    assert directives["tls-auth-clients"] == "no"


def test_eviction_policy_never_silently_drops_locks_or_queue_entries():
    # D1：这台机器将来同时装缓存、锁、队列。任何 allkeys-* 策略都会在内存
    # 压力下静默驱逐锁和队列数据——丢消息级事故且无日志痕迹。
    directives = dict(_conf_directives(REDIS_CONF.read_text()))
    assert directives["maxmemory-policy"] == "noeviction"


def test_dangerous_commands_are_disabled():
    # 已实测 Redis 8.8 仍支持 rename-command：被禁命令返回
    # "ERR unknown command"。CONFIG 在列表内 → 监控只能从 INFO 读 maxmemory。
    renamed = {
        args.split(" ", 1)[0]
        for name, args in _conf_directives(REDIS_CONF.read_text())
        if name == "rename-command"
    }
    assert {"FLUSHALL", "FLUSHDB", "CONFIG", "KEYS", "DEBUG"} <= renamed


def test_persistence_is_aof_everysec_plus_rdb_fallback():
    directives = _conf_directives(REDIS_CONF.read_text())
    d = dict(directives)
    assert d["appendonly"] == "yes"
    assert d["appendfsync"] == "everysec"
    assert d["dir"] == "/data"
    # RDB 兜底：三档 save 全在
    saves = {args for name, args in directives if name == "save"}
    assert saves == {"900 1", "300 10", "60 10000"}


def test_unix_socket_is_exposed_for_the_backup_sidecar():
    # D3b：明文端口关闭后 sidecar 只能走 unix socket；perm 700 保证
    # 只有同 uid 进程可连。
    d = dict(_conf_directives(REDIS_CONF.read_text()))
    assert d["unixsocket"] == "/var/run/redis/redis.sock"
    assert d["unixsocketperm"] == "700"


def test_secret_include_is_the_last_directive():
    # Redis 后读的配置覆盖先读的。含 requirepass / maxmemory 的
    # secret.conf 必须在最后 include，否则被前面的默认值盖掉。
    directives = _conf_directives(REDIS_CONF.read_text())
    assert directives[-1] == ("include", "/etc/redis/secret.conf")


def _run_entrypoint(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # DRY_RUN=1 让 entrypoint 走完全部校验与落盘后停在 exec 之前，
    # 这样测试不需要真起一个 Redis。
    return subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env={"PATH": "/usr/bin:/bin", "DRY_RUN": "1", **env},
        text=True,
        capture_output=True,
    )


def test_entrypoint_refuses_to_start_without_password():
    result = _run_entrypoint({})
    assert result.returncode != 0
    assert "REDIS_PASSWORD" in result.stderr


def test_entrypoint_refuses_backup_prefix_without_age_recipient():
    # fail-closed 的核心：配了备份目的地却没有加密公钥，绝不能
    # 退化成把明文快照推出 TEE。
    result = _run_entrypoint(
        {
            "REDIS_PASSWORD": "a" * 64,
            "REDIS_TLS_CERT_B64": "eA==",
            "REDIS_TLS_KEY_B64": "eA==",
            "REDIS_MAXMEMORY": "1gb",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
        }
    )
    assert result.returncode != 0
    assert "REDIS_BACKUP_AGE_RECIPIENT" in result.stderr


def test_entrypoint_rejects_malformed_age_recipient():
    result = _run_entrypoint(
        {
            "REDIS_PASSWORD": "a" * 64,
            "REDIS_TLS_CERT_B64": "eA==",
            "REDIS_TLS_KEY_B64": "eA==",
            "REDIS_MAXMEMORY": "1gb",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_AGE_RECIPIENT": "not-an-age-key",
        }
    )
    assert result.returncode != 0
    assert "REDIS_BACKUP_AGE_RECIPIENT" in result.stderr
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_cvm_config.py -v`
Expected: 全部 FAIL —— 前几个报 `FileNotFoundError`（`redis.conf` 不存在）。

- [ ] **Step 3: 写 `deploy/redis/redis.conf`**

```
# deploy/redis/redis.conf — TEE Redis CVM 的非机密配置。
# 机密项（requirepass / maxmemory）由 entrypoint-wrapper.sh 生成到
# /etc/redis/secret.conf，在本文件末尾 include（Redis 后读覆盖先读）。

# --- 网络：TLS-only。gateway passthrough 让本端口在公网可达，
#     故明文监听必须彻底关闭。---
port 0
tls-port 6379
tls-cert-file /etc/redistls/server.crt
tls-key-file /etc/redistls/server.key
# 我们用 requirepass 认证，不做双向 TLS；要求客户端证书会让消费方全连不上。
tls-auth-clients no

# --- backup sidecar 的本地通道（D3b）：明文 TCP 已关，sidecar 经
#     共享 volume 上的 unix socket 连接，省掉容器内自己跟自己做 TLS。---
unixsocket /var/run/redis/redis.sock
unixsocketperm 700

# --- 持久化第一层：AOF everysec（进程崩溃最多丢 1s）+ RDB 兜底 ---
dir /data
appendonly yes
appendfsync everysec
save 900 1
save 300 10
save 60 10000

# --- D1：绝不用 allkeys-*。这台机器将来同时装缓存、锁、队列，
#     LRU 策略会在内存压力下静默驱逐锁和队列数据（丢消息级事故，
#     且没有任何日志痕迹）。noeviction 下内存打满表现为写入报错：
#     可观测、可告警。缓存侧靠每个 key 强制带 TTL 自然回收。---
maxmemory-policy noeviction

# --- 高危命令禁用（已实测 Redis 8.8 仍支持 rename-command）。
#     注意 CONFIG 在列表内 → 监控只能从 INFO 读 maxmemory/used_memory，
#     不能用 CONFIG GET。---
rename-command FLUSHALL ""
rename-command FLUSHDB ""
rename-command CONFIG ""
rename-command KEYS ""
rename-command DEBUG ""

# --- 机密项必须最后 include ---
include /etc/redis/secret.conf
```

- [ ] **Step 4: 写 `deploy/redis/entrypoint-wrapper.sh`**

```sh
#!/bin/sh
# deploy/redis/entrypoint-wrapper.sh — 校验 → TLS 材料 → 机密配置 → 启动。
# fail-closed：缺任何必需机密就退出，绝不降级成明文或无备份运行。
set -eu

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
mkdir -p /etc/redistls
echo "${REDIS_TLS_CERT_B64}" | base64 -d > /etc/redistls/server.crt \
    || fatal "REDIS_TLS_CERT_B64 is not valid base64"
echo "${REDIS_TLS_KEY_B64}"  | base64 -d > /etc/redistls/server.key \
    || fatal "REDIS_TLS_KEY_B64 is not valid base64"
chmod 600 /etc/redistls/server.key
chmod 644 /etc/redistls/server.crt

# --- 机密配置：绝不走命令行参数，否则口令出现在容器内进程列表 ---
mkdir -p /etc/redis
umask 077
{
    echo "requirepass ${REDIS_PASSWORD}"
    echo "maxmemory ${REDIS_MAXMEMORY}"
} > /etc/redis/secret.conf
umask 022

# --- sidecar 的 socket 目录（共享 volume 挂载点）---
mkdir -p /var/run/redis

# 官方镜像以 root 启动、由 docker-entrypoint.sh 降权到 redis 用户运行，
# 故这些文件的属主要跟着改，否则降权后读不到 key、写不了 socket。
if id redis >/dev/null 2>&1; then
    chown redis:redis /etc/redistls/server.crt /etc/redistls/server.key \
                      /etc/redis/secret.conf /var/run/redis
fi

# DRY_RUN 供测试用：走完全部校验与落盘，停在 exec 之前。
[ -n "${DRY_RUN:-}" ] && { echo "[redis-init] dry run OK"; exit 0; }

exec docker-entrypoint.sh redis-server /etc/redis/redis.conf
```

- [ ] **Step 5: 跑测试确认通过**

Run: `chmod +x deploy/redis/entrypoint-wrapper.sh && python -m pytest tests/test_redis_cvm_config.py -v`
Expected: 全部 PASS（10 个测试）。

- [ ] **Step 6: 真容器里验证 entrypoint 能起 Redis**

这一步验证测试覆盖不到的东西：官方 entrypoint 的降权链路、TLS 证书真的被加载。

```bash
cd deploy/redis
openssl req -new -x509 -days 1 -nodes -keyout /tmp/t.key -out /tmp/t.crt -subj "/CN=t" 2>/dev/null
docker run --rm -d --name redis-t1 \
  -e REDIS_PASSWORD="$(openssl rand -hex 32)" \
  -e REDIS_TLS_CERT_B64="$(base64 < /tmp/t.crt | tr -d '\n')" \
  -e REDIS_TLS_KEY_B64="$(base64 < /tmp/t.key | tr -d '\n')" \
  -e REDIS_MAXMEMORY="256mb" \
  -v "$PWD/redis.conf:/etc/redis/redis.conf:ro" \
  -v "$PWD/entrypoint-wrapper.sh:/entrypoint-wrapper.sh:ro" \
  --entrypoint sh \
  redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005 \
  /entrypoint-wrapper.sh
sleep 3
docker logs redis-t1 2>&1 | tail -5
docker rm -f redis-t1
```

Expected: 日志出现 `Ready to accept connections tls`，**不出现** `Failed to configure TLS`。

- [ ] **Step 7: Commit**

```bash
git add deploy/redis/redis.conf deploy/redis/entrypoint-wrapper.sh tests/test_redis_cvm_config.py
git commit -m "feat(redis): TLS-only redis.conf + fail-closed entrypoint for TEE Redis CVM"
```

---

## Task 2: CVM compose 文件

**Files:**
- Create: `deploy/docker-compose.phala.redis.yaml`
- Create: `deploy/test-redis-cvm-id.txt`, `deploy/pre-redis-cvm-id.txt`, `deploy/prod-redis-cvm-id.txt`
- Modify: `tests/test_redis_cvm_config.py`（追加 compose 断言）

**Interfaces:**
- Consumes: Task 1 的 `redis.conf` + `entrypoint-wrapper.sh`（打进镜像的路径 `/usr/local/bin/entrypoint-wrapper.sh`、`/etc/redis/redis.conf`）
- Produces:
  - service 名：`redis`、`backup`
  - volume 名：`redisdata`（`/data`）、`redissock`（`/var/run/redis`）
  - sidecar 镜像引用：`ghcr.io/teleport-computer/feedling-redis-backup:REPLACE_SHA`
  - Redis 镜像引用：`ghcr.io/teleport-computer/feedling-redis:REPLACE_SHA`

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_cvm_config.py` 末尾）**

```python
import yaml

REDIS_COMPOSE = ROOT / "deploy" / "docker-compose.phala.redis.yaml"


def _redis_compose() -> dict:
    return yaml.safe_load(REDIS_COMPOSE.read_text())


def test_compose_has_exactly_redis_and_backup_services():
    compose = _redis_compose()
    assert set(compose["services"]) == {"redis", "backup"}


def test_compose_secrets_all_go_through_optional_env_substitution():
    # 本仓约定：机密走 "${VAR:-}"（加密 env 注入，不烧 compose_hash）；
    # 必填校验在 entrypoint fail-closed，不在 compose 里用 ${VAR:?}。
    source = REDIS_COMPOSE.read_text()
    for var in (
        "REDIS_PASSWORD",
        "REDIS_TLS_CERT_B64",
        "REDIS_TLS_KEY_B64",
        "REDIS_MAXMEMORY",
        "REDIS_BACKUP_AGE_RECIPIENT",
        "REDIS_BACKUP_S3_PREFIX",
        "REDIS_BACKUP_R2_ENDPOINT",
        "REDIS_BACKUP_R2_ACCESS_KEY_ID",
        "REDIS_BACKUP_R2_SECRET_ACCESS_KEY",
    ):
        assert f'"${{{var}:-}}"' in source, f"{var} must use ${{VAR:-}} form"


def test_backup_sidecar_shares_only_the_socket_volume():
    # D4：快照由 redis-cli --rdb 生成到 sidecar 自己的临时目录，
    # sidecar 不该挂数据卷——挂了就有人会图省事去直接拷 AOF 文件，
    # 那正是我们要避免的不一致读法。
    services = _redis_compose()["services"]
    backup_mounts = {m.split(":")[0] for m in services["backup"]["volumes"]}
    assert backup_mounts == {"redissock"}
    redis_mounts = {m.split(":")[0] for m in services["redis"]["volumes"]}
    assert redis_mounts == {"redisdata", "redissock"}


def test_only_the_tls_port_is_published():
    services = _redis_compose()["services"]
    assert services["redis"]["ports"] == ["6379:6379"]
    # sidecar 绝不暴露端口——它只经 unix socket 与 Redis 通信。
    assert "ports" not in services["backup"]


def test_healthcheck_uses_socket_and_never_puts_password_on_argv():
    # 口令写进命令行会出现在容器内进程列表；redis-cli 认 REDISCLI_AUTH。
    redis = _redis_compose()["services"]["redis"]
    test_cmd = " ".join(redis["healthcheck"]["test"])
    assert "/var/run/redis/redis.sock" in test_cmd
    assert "-a " not in test_cmd
    assert "REDISCLI_AUTH" in redis["environment"]


def test_both_services_restart_unless_stopped():
    for name, svc in _redis_compose()["services"].items():
        assert svc["restart"] == "unless-stopped", name


def test_cvm_id_files_exist_for_all_three_environments():
    # workflow 的 fail-closed 依据。首次开通前内容是纯注释（无 id），
    # 那时 workflow 必须失败而不是静默新建 CVM。
    for env in ("test", "pre", "prod"):
        path = ROOT / "deploy" / f"{env}-redis-cvm-id.txt"
        assert path.exists(), f"missing {path}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k compose or cvm_id`
Expected: FAIL，`FileNotFoundError` 指向 `docker-compose.phala.redis.yaml`。

- [ ] **Step 3: 写 `deploy/docker-compose.phala.redis.yaml`**

```yaml
# deploy/docker-compose.phala.redis.yaml — feedling-redis-{test,pre,prod} CVM
#
# 部署（首次开通见 DEPLOYMENTS.md runbook；日常用 redis-deploy workflow）：
#   phala deploy --cvm-id <id> -c deploy/docker-compose.phala.redis.yaml -e KEY=VAL ... --wait
#
# 约定（与本仓其它 phala compose 一致）：
#  - 所有机密走 "${VAR:-}"（加密 env 注入，不烧 compose_hash）；必填校验在
#    entrypoint fail-closed（deploy/redis/entrypoint-wrapper.sh）。
#  - 独立 CVM + `--kms phala` 身份（按部署账号授权，无链上 AppAuth，
#    同 TEE Postgres，见 docs/TEE_POSTGRES_SHADOW_PROVISIONING.md §0）；
#    绝不复用主 app 的 AppAuth 合约。不进 merge 自动部署。
#  - 三环境共用本文件，差异（maxmemory / 备份前缀 / 口令）全在注入的 env 里。
name: feedling-redis
services:
  redis:
    image: ghcr.io/teleport-computer/feedling-redis:REPLACE_SHA
    restart: unless-stopped
    environment:
      REDIS_PASSWORD: "${REDIS_PASSWORD:-}"
      REDIS_TLS_CERT_B64: "${REDIS_TLS_CERT_B64:-}"
      REDIS_TLS_KEY_B64: "${REDIS_TLS_KEY_B64:-}"
      REDIS_MAXMEMORY: "${REDIS_MAXMEMORY:-}"
      # 备份公钥在这里只用于 entrypoint 的 fail-closed 校验：配了备份前缀
      # 就必须有公钥，否则拒绝启动（真正做加密的是 backup 容器）。
      REDIS_BACKUP_S3_PREFIX: "${REDIS_BACKUP_S3_PREFIX:-}"
      REDIS_BACKUP_AGE_RECIPIENT: "${REDIS_BACKUP_AGE_RECIPIENT:-}"
      # healthcheck 用；写进 argv 会让口令出现在容器内进程列表。
      REDISCLI_AUTH: "${REDIS_PASSWORD:-}"
    volumes:
      - redisdata:/data
      - redissock:/var/run/redis
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -s /var/run/redis/redis.sock ping | grep -q PONG"]
      interval: 10s
      timeout: 3s
      retries: 6
      # 冷启动要加载 AOF；大数据集下比 PG 的 initdb 快得多，30s 足够。
      start_period: 30s

  backup:
    image: ghcr.io/teleport-computer/feedling-redis-backup:REPLACE_SHA
    restart: unless-stopped
    depends_on:
      redis:
        condition: service_healthy
    environment:
      REDISCLI_AUTH: "${REDIS_PASSWORD:-}"
      REDIS_BACKUP_S3_PREFIX: "${REDIS_BACKUP_S3_PREFIX:-}"
      REDIS_BACKUP_AGE_RECIPIENT: "${REDIS_BACKUP_AGE_RECIPIENT:-}"
      REDIS_BACKUP_BUCKET: "${REDIS_BACKUP_BUCKET:-io-in-enclave-db}"
      AWS_ENDPOINT_URL: "${REDIS_BACKUP_R2_ENDPOINT:-}"
      AWS_ACCESS_KEY_ID: "${REDIS_BACKUP_R2_ACCESS_KEY_ID:-}"
      AWS_SECRET_ACCESS_KEY: "${REDIS_BACKUP_R2_SECRET_ACCESS_KEY:-}"
      AWS_REGION: "auto"
      AWS_DEFAULT_REGION: "auto"
    volumes:
      # 只挂 socket 目录：快照走 redis-cli --rdb（一致性快照），
      # 绝不直接读 /data 里写到一半的 AOF/RDB 文件。
      - redissock:/var/run/redis

volumes:
  redisdata:
  redissock:
```

- [ ] **Step 4: 建三个 cvm-id 文件**

三个文件内容相同（把 `<env>` 换成对应环境）：

```bash
for env in test pre prod; do
  cat > "deploy/${env}-redis-cvm-id.txt" <<EOF
# feedling-redis-${env} CVM id — 首次开通后填在下一行（去掉注释）。
# 空文件是刻意的 fail-closed：redis-deploy workflow 读不到 id 就报错退出，
# 绝不静默新建 CVM（新建 CVM = 换钥事故，见 DEPLOYMENTS.md）。
EOF
done
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_redis_cvm_config.py -v`
Expected: 全部 PASS（17 个测试）。

- [ ] **Step 6: 用 docker compose 验证语法**

Run: `docker compose -f deploy/docker-compose.phala.redis.yaml config -q && echo COMPOSE-OK`
Expected: 输出 `COMPOSE-OK`（未设置的 `${VAR:-}` 会解析成空串，不报错——这正是 `:-` 形式的用意）。

- [ ] **Step 7: Commit**

```bash
git add deploy/docker-compose.phala.redis.yaml deploy/test-redis-cvm-id.txt \
        deploy/pre-redis-cvm-id.txt deploy/prod-redis-cvm-id.txt tests/test_redis_cvm_config.py
git commit -m "feat(redis): CVM compose + fail-closed cvm-id placeholders"
```

---

## Task 3: TLS 材料生成脚本

**Files:**
- Create: `deploy/redis/gen-certs.sh`
- Modify: `tests/test_redis_cvm_config.py`（追加证书断言）

**Interfaces:**
- Consumes: 无
- Produces: `gen-certs.sh <cvm-name> <outdir>` → 在 outdir 生成 `ca.crt`、`ca.key`、`server.crt`、`server.key`，并打印 `REDIS_TLS_CERT_B64=` / `REDIS_TLS_KEY_B64=` 供注入

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_cvm_config.py`）**

```python
GEN_CERTS = ROOT / "deploy" / "redis" / "gen-certs.sh"


def test_gen_certs_produces_a_cert_valid_for_the_gateway_hostname(tmp_path):
    # gateway passthrough 的域名是 <app-id>-6379s.dstack-pha-prod9.phala.network，
    # app_id 要首次部署后才知道 → 用通配 SAN 一次覆盖，使客户端仍能做
    # verify-full 主机名校验（而不是降级成「只加密不校验」）。
    result = subprocess.run(
        ["sh", str(GEN_CERTS), "feedling-redis-test", str(tmp_path)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr

    text = subprocess.run(
        ["openssl", "x509", "-in", str(tmp_path / "server.crt"), "-noout", "-text"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "DNS:*.dstack-pha-prod9.phala.network" in text

    # server 证书必须由生成的 CA 签发，否则消费方钉 ca.crt 时校验失败。
    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca.crt"), str(tmp_path / "server.crt")],
        text=True,
        capture_output=True,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_gen_certs_prints_injectable_env_values(tmp_path):
    result = subprocess.run(
        ["sh", str(GEN_CERTS), "feedling-redis-test", str(tmp_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "REDIS_TLS_CERT_B64=" in result.stdout
    assert "REDIS_TLS_KEY_B64=" in result.stdout
    # base64 必须是单行，否则粘进 -e 注入会被换行截断。
    for line in result.stdout.splitlines():
        if line.startswith("REDIS_TLS_KEY_B64="):
            assert "\n" not in line[len("REDIS_TLS_KEY_B64="):]


def test_gen_certs_locks_down_private_key_permissions(tmp_path):
    subprocess.run(
        ["sh", str(GEN_CERTS), "feedling-redis-test", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    for name in ("ca.key", "server.key"):
        mode = (tmp_path / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} has mode {oct(mode)}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k gen_certs`
Expected: FAIL —— `sh: can't open .../gen-certs.sh`。

- [ ] **Step 3: 写 `deploy/redis/gen-certs.sh`**

```sh
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `chmod +x deploy/redis/gen-certs.sh && python -m pytest tests/test_redis_cvm_config.py -v -k gen_certs`
Expected: 3 个测试 PASS。

注意：脚本用了 bash 的进程替换 `<(...)`，所以 shebang 是 `#!/bin/bash` 而非 `sh`；测试里也用 `sh` 调用是可以的，因为 macOS/Linux 的 `sh` 在这里由测试显式调用 `sh <path>`——若在纯 dash 环境失败，改用 `["bash", str(GEN_CERTS), ...]`。

- [ ] **Step 5: Commit**

```bash
git add deploy/redis/gen-certs.sh tests/test_redis_cvm_config.py
git commit -m "feat(redis): TLS material generator with wildcard SAN for gateway passthrough"
```

---

## Task 4: 备份 sidecar 镜像与单次快照脚本

**Files:**
- Create: `deploy/redis/Dockerfile.backup`
- Create: `deploy/redis/backup-push.sh`
- Test: `tests/test_redis_backup_scripts.py`

**Interfaces:**
- Consumes: Task 2 定义的 sidecar 环境变量（`REDIS_BACKUP_S3_PREFIX`、`REDIS_BACKUP_AGE_RECIPIENT`、`REDIS_BACKUP_BUCKET`、`AWS_*`、`REDISCLI_AUTH`）
- Produces:
  - `backup-push.sh` 无参调用 = 做一次完整快照并上传；退出码 0 = 成功
  - R2 对象命名：`s3://<bucket>/<prefix>redis-<YYYYmmddTHHMMSSZ>.rdb.age`
  - 环境变量 `REDIS_SOCKET`（默认 `/var/run/redis/redis.sock`）供测试覆盖

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_redis_backup_scripts.py`：

```python
"""backup-push.sh 的行为测试。

策略：把 redis-cli / age / aws 换成记录调用的 stub 放进 PATH 最前面，
这样能在毫秒级验证脚本的编排逻辑（顺序、参数、fail-closed），
不需要真起 Redis 或连 R2。真实的端到端在 Task 10 用 docker + MinIO 跑。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
BACKUP_PUSH = ROOT / "deploy" / "redis" / "backup-push.sh"


def _make_stubs(tmp_path: Path, *, aws_ls_output: str = "") -> Path:
    """建一个 bin 目录，内含记录调用的 stub。调用记录写进 calls.log。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    # redis-cli --rdb <path> 要真的产出一个文件，否则后续步骤无从验证。
    (bin_dir / "redis-cli").write_text(
        f'#!/bin/sh\n'
        f'echo "redis-cli $*" >> {log}\n'
        f'for a in "$@"; do\n'
        f'  if [ "$prev" = "--rdb" ]; then printf "REDIS0014fake" > "$a"; fi\n'
        f'  prev="$a"\n'
        f'done\n'
        f'exit 0\n'
    )
    # age -r <recipient> -o <out> <in>
    (bin_dir / "age").write_text(
        f'#!/bin/sh\n'
        f'echo "age $*" >> {log}\n'
        f'prev=""\n'
        f'for a in "$@"; do\n'
        f'  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
        f'  prev="$a"\n'
        f'done\n'
        f'printf "encrypted" > "$out"\n'
        f'exit 0\n'
    )
    (bin_dir / "aws").write_text(
        f'#!/bin/sh\n'
        f'echo "aws $*" >> {log}\n'
        f'case "$*" in\n'
        f'  *list-objects-v2*) printf "%s" "{aws_ls_output}" ;;\n'
        f'esac\n'
        f'exit 0\n'
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def _run_backup(tmp_path: Path, env_overrides: dict[str, str] | None = None,
                aws_ls_output: str = "") -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = _make_stubs(tmp_path, aws_ls_output=aws_ls_output)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "REDIS_BACKUP_S3_PREFIX": "test/redis/",
        "REDIS_BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
        "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
        "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
        "REDISCLI_AUTH": "secret",
        "REDIS_SOCKET": "/tmp/fake.sock",
        "BACKUP_TMPDIR": str(tmp_path / "work"),
    }
    env.update(env_overrides or {})
    result = subprocess.run(
        ["sh", str(BACKUP_PUSH)], env=env, text=True, capture_output=True
    )
    log = tmp_path / "calls.log"
    return result, (log.read_text() if log.exists() else "")


def test_snapshot_is_taken_over_the_unix_socket(tmp_path):
    result, calls = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    # D4：一致性快照走 --rdb，绝不拷卷内文件。
    assert "--rdb" in calls
    assert "-s /tmp/fake.sock" in calls


def test_snapshot_is_encrypted_before_it_ever_reaches_r2(tmp_path):
    result, calls = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    lines = [l for l in calls.splitlines() if l.startswith(("age ", "aws "))]
    # 顺序是安全属性：加密必须发生在上传之前。
    assert lines[0].startswith("age "), lines
    assert "age1" + "q" * 58 in lines[0]
    upload = next(l for l in lines if "s3 cp" in l)
    assert upload.endswith(".rdb.age") or ".rdb.age" in upload


def test_uploaded_object_key_is_timestamped_under_the_env_prefix(tmp_path):
    result, calls = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    upload = next(l for l in calls.splitlines() if "s3 cp" in l)
    assert "s3://io-in-enclave-db/test/redis/redis-" in upload
    assert upload.rstrip().endswith(".rdb.age")


def test_plaintext_snapshot_is_removed_after_upload(tmp_path):
    # 明文 RDB 是最敏感的中间产物；留在磁盘上等于把 TEE 内的数据
    # 摊在卷里。脚本必须自己清掉。
    result, _ = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    work = tmp_path / "work"
    leftovers = [p.name for p in work.rglob("*.rdb")] if work.exists() else []
    assert leftovers == [], f"plaintext snapshot left behind: {leftovers}"


def test_refuses_to_run_without_age_recipient(tmp_path):
    result, calls = _run_backup(tmp_path, {"REDIS_BACKUP_AGE_RECIPIENT": ""})
    assert result.returncode != 0
    assert "REDIS_BACKUP_AGE_RECIPIENT" in result.stderr
    assert "s3 cp" not in calls   # 绝不能已经传了才发现没加密


def test_refuses_to_run_without_s3_prefix(tmp_path):
    result, calls = _run_backup(tmp_path, {"REDIS_BACKUP_S3_PREFIX": ""})
    assert result.returncode != 0
    assert "REDIS_BACKUP_S3_PREFIX" in result.stderr
    assert "s3 cp" not in calls


def test_failed_snapshot_never_uploads_anything(tmp_path):
    # redis-cli 挂了却继续上传，会往 R2 塞一个空/损坏的「备份」，
    # 把监控刷成绿色——比没有备份更危险。
    bin_dir = _make_stubs(tmp_path)
    (bin_dir / "redis-cli").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "redis-cli").chmod(0o755)
    result = subprocess.run(
        ["sh", str(BACKUP_PUSH)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
            "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
            "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "REDIS_SOCKET": "/tmp/fake.sock",
            "BACKUP_TMPDIR": str(tmp_path / "work"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "s3 cp" not in calls
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_backup_scripts.py -v`
Expected: 全部 FAIL —— `sh: can't open .../backup-push.sh`。

- [ ] **Step 3: 写 `deploy/redis/backup-push.sh`**

```sh
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

SOCKET="${REDIS_SOCKET:-/var/run/redis/redis.sock}"
WORK="${BACKUP_TMPDIR:-/tmp/redis-backup}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
PLAIN="${WORK}/redis-${TS}.rdb"
SEALED="${PLAIN}.age"
KEY="s3://${REDIS_BACKUP_BUCKET}/${REDIS_BACKUP_S3_PREFIX}redis-${TS}.rdb.age"

mkdir -p "$WORK"
# 明文快照即使只在磁盘上存在几秒，也不该让别的 uid 读到。
chmod 700 "$WORK"

# 明文中间产物必须清理，无论成功失败。
cleanup() { rm -f "$PLAIN"; }
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
echo "[backup] done $(date -u +%Y%m%dT%H%M%SZ)"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `chmod +x deploy/redis/backup-push.sh && python -m pytest tests/test_redis_backup_scripts.py -v`
Expected: 7 个测试全 PASS。

- [ ] **Step 5: 写 `deploy/redis/Dockerfile.backup`**

```dockerfile
# deploy/redis/Dockerfile.backup — 备份 sidecar。
#
# 基底刻意用与 Redis 服务端完全相同的 digest：redis-cli 与服务端同版本
# （8.8.0），不会出现 cli 老于 server 的 RDB/协议错配。
# 已实测该基底可 apk 装到 aws-cli 2.32.7 与 age 1.2.1。
FROM redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005

RUN apk add --no-cache aws-cli age ca-certificates

COPY redis/backup-push.sh redis/backup-loop.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup-push.sh /usr/local/bin/backup-loop.sh

ENTRYPOINT ["/usr/local/bin/backup-loop.sh"]
```

注意 `COPY` 的路径前缀是 `redis/`，因为构建 context 是 `deploy/`（与 `deploy/postgres/Dockerfile` 同约定）。`backup-loop.sh` 在 Task 5 创建——本 Task 先不构建镜像。

- [ ] **Step 6: Commit**

```bash
git add deploy/redis/backup-push.sh deploy/redis/Dockerfile.backup tests/test_redis_backup_scripts.py
git commit -m "feat(redis): single-shot encrypted snapshot push to R2"
```

---

## Task 5: 备份循环、boot 兜底与保留策略

**Files:**
- Create: `deploy/redis/backup-loop.sh`
- Modify: `deploy/redis/backup-push.sh`（追加保留策略）
- Modify: `tests/test_redis_backup_scripts.py`

**Interfaces:**
- Consumes: Task 4 的 `backup-push.sh`
- Produces:
  - `backup-loop.sh` 为 sidecar 的 ENTRYPOINT：boot 兜底 + 每 `BACKUP_INTERVAL_SEC`（默认 3600）一轮
  - 保留策略：小时快照保留最近 24 份；`T03` 的每日快照额外保留 7 天

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_backup_scripts.py`）**

```python
BACKUP_LOOP = ROOT / "deploy" / "redis" / "backup-loop.sh"


def _ls_output(keys: list[str]) -> str:
    """模拟 aws s3api list-objects-v2 --query 'Contents[].Key' --output text
    的输出：制表符分隔的一行。"""
    return "\t".join(keys)


def test_retention_keeps_the_24_most_recent_hourly_snapshots(tmp_path):
    # 造 30 个小时快照，全在同一天的非 03 点，故只受「最近 24」规则保护。
    keys = [f"test/redis/redis-20260701T{h:02d}0000Z.rdb.age" for h in range(0, 24)]
    keys += [f"test/redis/redis-20260702T{h:02d}0000Z.rdb.age" for h in range(4, 10)]
    result, calls = _run_backup(tmp_path, aws_ls_output=_ls_output(keys))
    assert result.returncode == 0, result.stderr

    deleted = [l for l in calls.splitlines() if "rm " in l and "s3" in l]
    deleted_keys = " ".join(deleted)
    # 最老的必须被删
    assert "20260701T000000Z" in deleted_keys
    # 最新的必须留着
    assert "20260702T090000Z" not in deleted_keys


def test_retention_protects_daily_03z_snapshots_for_seven_days(tmp_path):
    # 每日 03:00 UTC 那份额外保留 7 天——即使它早已掉出「最近 24」窗口。
    keys = [f"test/redis/redis-2026070{d}T030000Z.rdb.age" for d in range(1, 5)]
    keys += [f"test/redis/redis-20260705T{h:02d}0000Z.rdb.age" for h in range(0, 24)]
    result, calls = _run_backup(tmp_path, aws_ls_output=_ls_output(keys))
    assert result.returncode == 0, result.stderr

    deleted_keys = " ".join(l for l in calls.splitlines() if "rm " in l and "s3" in l)
    for d in range(1, 5):
        assert f"2026070{d}T030000Z" not in deleted_keys, "daily snapshot must survive"


def test_retention_never_deletes_when_listing_fails(tmp_path):
    # 列表失败时把「没列到」当成「没有对象」，会把整个备份历史删光。
    bin_dir = _make_stubs(tmp_path)
    (bin_dir / "aws").write_text(
        '#!/bin/sh\n'
        f'echo "aws $*" >> {tmp_path / "calls.log"}\n'
        'case "$*" in\n'
        '  *list-objects-v2*) exit 1 ;;\n'
        'esac\n'
        'exit 0\n'
    )
    (bin_dir / "aws").chmod(0o755)
    result = subprocess.run(
        ["sh", str(BACKUP_PUSH)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
            "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
            "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "REDIS_SOCKET": "/tmp/fake.sock",
            "BACKUP_TMPDIR": str(tmp_path / "work"),
        },
        text=True,
        capture_output=True,
    )
    calls = (tmp_path / "calls.log").read_text()
    assert "s3 rm" not in calls and "rm --recursive" not in calls


def test_loop_does_not_use_cron():
    # PG 那边 2026-07-14 的事故：cron 以精简 PATH 执行，找不到
    # /usr/local/bin/wal-g，每日备份静默失败很久，只剩建库时那一份 base。
    source = BACKUP_LOOP.read_text()
    assert "cron" not in source.lower()
    assert "sleep" in source


def test_loop_pushes_immediately_when_the_prefix_is_empty(tmp_path):
    # boot 兜底：R2 前缀下什么都没有时立刻推一份，不等第一个小时周期。
    bin_dir = _make_stubs(tmp_path, aws_ls_output="")
    log = tmp_path / "calls.log"
    result = subprocess.run(
        ["sh", str(BACKUP_LOOP)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
            "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
            "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "REDIS_SOCKET": "/tmp/fake.sock",
            "BACKUP_TMPDIR": str(tmp_path / "work"),
            "BACKUP_INTERVAL_SEC": "0",     # 0 = 只跑一轮就退出（测试用）
            "BACKUP_PUSH_BIN": str(BACKUP_PUSH),
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "s3 cp" in log.read_text()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_backup_scripts.py -v -k retention or loop`
Expected: FAIL —— 保留策略相关的断言失败（脚本还没有删除逻辑），loop 相关报文件不存在。

- [ ] **Step 3: 给 `backup-push.sh` 追加保留策略**

在 `echo "[backup] uploaded ${KEY}"` 之后、最后那行 `echo "[backup] done ..."` 之前插入：

```sh
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
```

- [ ] **Step 4: 写 `deploy/redis/backup-loop.sh`**

```sh
#!/bin/sh
# deploy/redis/backup-loop.sh — sidecar 主进程。
#
# D3：刻意不用 cron。PG 那边 2026-07-14 的事故就是 cron 以精简
# PATH=/usr/bin:/bin 执行，找不到 /usr/local/bin/wal-g，每日 base backup
# 静默失败很久，直到排查才发现只剩建库时那一份 base、retain 从没跑成、
# WAL 在 R2 无限堆积。显式 sleep 循环直接继承容器环境，失败也进容器日志。
set -eu

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
    "$PUSH" || echo "[backup-loop] ERROR: initial push failed" >&2
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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `chmod +x deploy/redis/backup-loop.sh && python -m pytest tests/test_redis_backup_scripts.py -v`
Expected: 12 个测试全 PASS。

- [ ] **Step 6: 构建 sidecar 镜像验证**

Run: `docker build --platform linux/amd64 -f deploy/redis/Dockerfile.backup -t feedling-redis-backup:local deploy`
Expected: 构建成功。再验证三个工具都在：

```bash
docker run --rm --entrypoint sh feedling-redis-backup:local -c 'redis-cli --version; age --version; aws --version'
```
Expected: `redis-cli 8.8.0`、`v1.2.1`、`aws-cli/2.x`。

- [ ] **Step 7: Commit**

```bash
git add deploy/redis/backup-loop.sh deploy/redis/backup-push.sh tests/test_redis_backup_scripts.py
git commit -m "feat(redis): hourly backup loop with boot fallback and fail-safe retention"
```

---

## Task 6: 恢复脚本

**Files:**
- Create: `deploy/redis/restore.sh`
- Modify: `tests/test_redis_backup_scripts.py`

**Interfaces:**
- Consumes: Task 4/5 产出的 R2 对象命名约定 `<prefix>redis-<TS>.rdb.age`
- Produces: `restore.sh [OBJECT_KEY]`（省略则取最新）→ 解密后的 `dump.rdb` 落到 `$RESTORE_DIR`（默认 `/data`）

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_backup_scripts.py`）**

```python
import shutil

RESTORE = ROOT / "deploy" / "redis" / "restore.sh"


def test_restore_refuses_without_identity_file(tmp_path):
    # fail-closed：我们的备份必然加密，没私钥就是配置错了。
    result = subprocess.run(
        ["sh", str(RESTORE)],
        env={
            "PATH": "/usr/bin:/bin",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
            "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "RESTORE_DIR": str(tmp_path),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "REDIS_BACKUP_AGE_IDENTITY_FILE" in result.stderr


def test_restore_picks_the_newest_object_when_none_specified(tmp_path):
    keys = [
        "test/redis/redis-20260701T030000Z.rdb.age",
        "test/redis/redis-20260724T110000Z.rdb.age",
        "test/redis/redis-20260703T030000Z.rdb.age",
    ]
    bin_dir = _make_stubs(tmp_path, aws_ls_output="\t".join(keys))
    # age -d -i <identity> -o <out> <in>
    (bin_dir / "age").write_text(
        f'#!/bin/sh\n'
        f'echo "age $*" >> {tmp_path / "calls.log"}\n'
        f'prev=""\n'
        f'for a in "$@"; do\n'
        f'  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
        f'  prev="$a"\n'
        f'done\n'
        f'printf "REDIS0014restored" > "$out"\n'
    )
    (bin_dir / "age").chmod(0o755)
    identity = tmp_path / "id.txt"
    identity.write_text("AGE-SECRET-KEY-1FAKE\n")

    result = subprocess.run(
        ["sh", str(RESTORE)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
            "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "REDIS_BACKUP_AGE_IDENTITY_FILE": str(identity),
            "RESTORE_DIR": str(tmp_path / "out"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text()
    # 字典序 = 时间序，最新的是 20260724T110000Z
    download = next(l for l in calls.splitlines() if "s3 cp" in l)
    assert "20260724T110000Z" in download
    assert (tmp_path / "out" / "dump.rdb").exists()


def test_restore_roundtrip_with_real_age(tmp_path):
    # 真 age 往返：验证我们用的加解密参数确实互逆。stub 测不出参数写错
    # （比如 -r 与 -R 混用、忘了 -d）。
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        import pytest
        pytest.skip("age not installed locally")

    identity = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)], check=True, text=True, capture_output=True
    ).stdout.strip()

    plain = tmp_path / "plain.rdb"
    plain.write_bytes(b"REDIS0014" + b"payload" * 100)
    sealed = tmp_path / "plain.rdb.age"

    subprocess.run(
        ["age", "-r", recipient, "-o", str(sealed), str(plain)], check=True, capture_output=True
    )
    out = tmp_path / "out.rdb"
    subprocess.run(
        ["age", "-d", "-i", str(identity), "-o", str(out), str(sealed)],
        check=True,
        capture_output=True,
    )
    assert out.read_bytes() == plain.read_bytes()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_backup_scripts.py -v -k restore`
Expected: 前两个 FAIL（脚本不存在），第三个（真 age 往返）PASS 或 skip。

- [ ] **Step 3: 写 `deploy/redis/restore.sh`**

```sh
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
set -eu

fatal() { echo "[restore] FATAL: $*" >&2; exit 1; }

[ -n "${REDIS_BACKUP_AGE_IDENTITY_FILE:-}" ] \
    || fatal "REDIS_BACKUP_AGE_IDENTITY_FILE required (offline age private key)"
[ -f "${REDIS_BACKUP_AGE_IDENTITY_FILE}" ] \
    || fatal "identity file not found: ${REDIS_BACKUP_AGE_IDENTITY_FILE}"
[ -n "${REDIS_BACKUP_BUCKET:-}" ]    || fatal "REDIS_BACKUP_BUCKET required"
[ -n "${REDIS_BACKUP_S3_PREFIX:-}" ] || fatal "REDIS_BACKUP_S3_PREFIX required"
[ -n "${AWS_ENDPOINT_URL:-}" ]       || fatal "AWS_ENDPOINT_URL required"

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
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

aws s3 cp "s3://${REDIS_BACKUP_BUCKET}/${OBJECT}" "${WORK}/snap.rdb.age" \
    || fatal "download failed: ${OBJECT}"

age -d -i "$REDIS_BACKUP_AGE_IDENTITY_FILE" -o "${WORK}/dump.rdb" "${WORK}/snap.rdb.age" \
    || fatal "decryption failed — wrong identity file?"

# RDB 文件必须以 REDIS 魔数开头；解出来的不是 RDB 就别往 dir 里放，
# 免得 Redis 启动时报一个难懂的错。
head -c 5 "${WORK}/dump.rdb" | grep -q "REDIS" \
    || fatal "decrypted file is not an RDB snapshot"

mv "${WORK}/dump.rdb" "${RESTORE_DIR}/dump.rdb"
echo "[restore] wrote ${RESTORE_DIR}/dump.rdb"
echo "[restore] NOTE: 目标实例的 appendonly 必须先关掉再启动，否则 Redis"
echo "[restore]       会优先加载 AOF 而忽略这份 dump.rdb。演练步骤见 plan Task 10。"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `chmod +x deploy/redis/restore.sh && python -m pytest tests/test_redis_backup_scripts.py -v`
Expected: 15 个测试全 PASS（若本机没装 age，最后一个 skip）。

- [ ] **Step 5: Commit**

```bash
git add deploy/redis/restore.sh tests/test_redis_backup_scripts.py
git commit -m "feat(redis): fail-closed restore from encrypted R2 snapshot"
```

---

## Task 7: 连通性冒烟脚本

**Files:**
- Create: `deploy/verify-redis.sh`
- Modify: `tests/test_redis_cvm_config.py`

**Interfaces:**
- Consumes: Task 3 产出的 `ca.crt`
- Produces: `verify-redis.sh <host> <port>` → 退出码 0 = TLS + AUTH + 读写 + TTL + INFO 全通

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_cvm_config.py`）**

```python
VERIFY = ROOT / "deploy" / "verify-redis.sh"


def test_verify_script_requires_ca_and_password():
    result = subprocess.run(
        ["sh", str(VERIFY), "example.com", "443"],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "REDIS_CA_FILE" in result.stderr or "REDISCLI_AUTH" in result.stderr


def test_verify_script_never_passes_password_on_argv():
    # 冒烟脚本会在 runbook 里被复制粘贴；口令进 argv 就会进 shell history。
    source = VERIFY.read_text()
    assert "-a $" not in source
    assert "--pass" not in source
    assert "REDISCLI_AUTH" in source


def test_verify_script_checks_tls_and_ttl_and_info():
    source = VERIFY.read_text()
    assert "--tls" in source
    assert "--cacert" in source
    # CONFIG 被 rename-command 禁用 → 只能从 INFO 读，别用 CONFIG GET。
    assert "CONFIG GET" not in source
    assert "INFO" in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k verify`
Expected: FAIL —— 文件不存在。

- [ ] **Step 3: 写 `deploy/verify-redis.sh`**

```sh
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

R="redis-cli --tls --cacert ${REDIS_CA_FILE} -h ${HOST} -p ${PORT}"
KEY="__verify_smoke_$(date -u +%s)"

echo "[verify] target ${HOST}:${PORT}"

[ "$($R PING)" = "PONG" ] || fatal "PING failed (TLS handshake or AUTH)"
echo "[verify] PING ok"

$R SET "$KEY" hello EX 60 >/dev/null || fatal "SET failed"
[ "$($R GET "$KEY")" = "hello" ]     || fatal "GET returned unexpected value"
TTL="$($R TTL "$KEY")"
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

echo "[verify] ALL GREEN"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `chmod +x deploy/verify-redis.sh && python -m pytest tests/test_redis_cvm_config.py -v -k verify`
Expected: 3 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add deploy/verify-redis.sh tests/test_redis_cvm_config.py
git commit -m "feat(redis): connectivity smoke script"
```

---

## Task 8: 部署 workflow

**Files:**
- Create: `.github/workflows/redis-deploy.yml`
- Create: `deploy/redis/Dockerfile`（Redis 服务端镜像：官方基底 + 我们的 conf/entrypoint）
- Modify: `tests/test_redis_cvm_config.py`

**Interfaces:**
- Consumes: Task 1-7 的全部文件
- Produces: 手动 workflow，环境三选一 → 自动映射 Phala 账号与 secret 组

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_cvm_config.py`）**

```python
REDIS_DEPLOY_WF = ROOT / ".github" / "workflows" / "redis-deploy.yml"


def _deploy_workflow() -> dict:
    return yaml.safe_load(REDIS_DEPLOY_WF.read_text())


def test_deploy_workflow_is_manual_only_and_covers_three_environments():
    wf = _deploy_workflow()
    # PyYAML 把裸 `on:` 解析成布尔 True 而不是字符串 "on"。
    triggers = wf[True] if True in wf else wf["on"]
    assert set(triggers) == {"workflow_dispatch"}, "绝不并入 merge 自动部署"
    options = triggers["workflow_dispatch"]["inputs"]["environment"]["options"]
    assert options == ["test", "pre", "prod"]


def test_deploy_workflow_maps_each_environment_to_the_right_phala_account():
    # test/pre 在 amiller-user 账号（TEST_ key）；prod 在 sxysun 账号（无前缀 key）。
    source = REDIS_DEPLOY_WF.read_text()
    assert "secrets.TEST_PHALA_CLOUD_API_KEY" in source
    assert "secrets.PHALA_CLOUD_API_KEY" in source


def test_deploy_workflow_never_selects_secrets_with_a_ternary():
    # pg-deploy.yml 2026-07-24 修正的真实安全 bug：
    #   ${{ env == 'prod' && secrets.PROD_X || secrets.TEST_X }}
    # 在 PROD_X 恰好为空时短路 fallback 到 TEST_X —— 非空预检照样通过，
    # 但注进 prod CVM 的是 test 的密码。正解是两套都注入 job env，
    # 在 shell 里按环境名前缀间接取值：挑错只会挑到空值 → fail-closed。
    source = REDIS_DEPLOY_WF.read_text()
    assert "&& secrets.PROD_" not in source, "ternary secret selection is unsafe"
    assert "&& secrets.PRE_" not in source
    # 两套机密都以 <ENV>_ 前缀出现在 job env 里
    assert "TEST_REDIS_PASSWORD" in source and "PROD_REDIS_PASSWORD" in source
    assert "PRE_REDIS_PASSWORD" in source


def test_deploy_workflow_has_typo_guard_with_a_longer_prod_confirmation():
    source = REDIS_DEPLOY_WF.read_text()
    assert "DEPLOY-REDIS" in source
    # prod 打的是另一个账号下的真实用户数据机器 → 更长的确认串
    # （与 pg-deploy.yml 的 DEPLOY-PG-PROD 同款）。
    assert "DEPLOY-REDIS-PROD" in source


def test_deploy_workflow_absorbs_grep_no_match_before_the_empty_check():
    # pg-deploy.yml 的坑：GHA 以 `bash -eo pipefail` 跑 run 步骤，
    # 纯注释的 cvm-id.txt 让 `grep -v '^#'` 退出 1，pipefail 把它当成整条
    # 管道的退出码 → 脚本在这里裸退出，永远走不到那句明确的报错。
    source = REDIS_DEPLOY_WF.read_text()
    assert "|| true" in source
    assert "test -n" in source
    # 文件根本不存在时也要给出明确报错，而不是让 grep 自己去炸。
    assert "test -f" in source
    # id 两侧的空白会让 --cvm-id 收到一个带换行的值。
    assert "tr -d '[:space:]'" in source


def test_deploy_workflow_never_creates_a_cvm():
    # 新建 CVM = 换钥事故。workflow 只允许原地更新。
    source = REDIS_DEPLOY_WF.read_text()
    assert "phala deploy" in source
    assert "--cvm-id" in source
    assert "cvms create" not in source


def test_deploy_workflow_has_no_onchain_appauth_step():
    # 与 TEE Postgres 同一身份模型：--kms phala 按部署账号授权，
    # 这类数据存储 CVM 不需要链上 AppAuth
    # （docs/TEE_POSTGRES_SHADOW_PROVISIONING.md §0）。
    source = REDIS_DEPLOY_WF.read_text()
    # 断言的是「没有调用该脚本的步骤」，不是「字面不出现」——workflow 顶部
    # 的注释会解释为什么没有这一步，那段文字必须允许存在。
    assert "publish-compose-hash.sh" not in source
    assert "FEEDLING_APP_AUTH_CONTRACT" not in source


def test_deploy_workflow_derives_image_tag_from_the_checked_out_head():
    # pg-deploy.yml 的坑：workflow_dispatch 下 github.sha 指向触发时所在
    # ref 的 sha，而 checkout 的是 test/pre/main —— tag 会与镜像内容对不上。
    source = REDIS_DEPLOY_WF.read_text()
    assert "git rev-parse HEAD" in source
    assert "feedling-redis:${{ github.sha }}" not in source


def test_deploy_workflow_passes_secrets_through_a_file_not_argv():
    # 机密拼进命令行会出现在 runner 的 ps 与日志里。
    source = REDIS_DEPLOY_WF.read_text()
    assert 'chmod 600' in source
    assert '-e "$ENVFILE"' in source


def test_deploy_workflow_prechecks_every_required_secret():
    # 本仓 compose 用 ${VAR:-}，无法 grep :? 检出缺失 → 显式清单预检。
    # 原地更新必须重带整份机密：漏一个就被清空 → entrypoint fail-closed 起不来。
    source = REDIS_DEPLOY_WF.read_text()
    for var in (
        "REDIS_PASSWORD",
        "REDIS_TLS_CERT_B64",
        "REDIS_TLS_KEY_B64",
        "REDIS_MAXMEMORY",
        "REDIS_BACKUP_AGE_RECIPIENT",
        "REDIS_BACKUP_S3_PREFIX",
        "REDIS_BACKUP_R2_ENDPOINT",
        "REDIS_BACKUP_R2_ACCESS_KEY_ID",
        "REDIS_BACKUP_R2_SECRET_ACCESS_KEY",
    ):
        assert var in source, f"{var} missing from workflow"


def test_deploy_workflow_checks_out_the_right_branch_per_environment():
    source = REDIS_DEPLOY_WF.read_text()
    assert "'main'" in source and "'pre'" in source and "'test'" in source


def test_deploy_workflow_uses_a_separate_concurrency_group_per_environment():
    source = REDIS_DEPLOY_WF.read_text()
    assert "concurrency: redis-deploy-" in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k deploy_workflow`
Expected: FAIL —— workflow 文件不存在。

- [ ] **Step 3: 写 `deploy/redis/Dockerfile`（Redis 服务端镜像）**

```dockerfile
# deploy/redis/Dockerfile — TEE Redis 服务端。
# 官方镜像原封不动（已实测 8.8.0 编译带 TLS），只叠加我们的配置与 entrypoint。
FROM redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005

COPY redis/redis.conf /etc/redis/redis.conf
COPY redis/entrypoint-wrapper.sh /usr/local/bin/entrypoint-wrapper.sh
RUN chmod +x /usr/local/bin/entrypoint-wrapper.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
```

- [ ] **Step 4: 写 `.github/workflows/redis-deploy.yml`**

```yaml
# .github/workflows/redis-deploy.yml — redis CVM 手动部署（绝不并入 merge 自动部署）
#
# 环境三选一 → 自动映射 Phala 账号：
#   test / pre → amiller-user 账号（TEST_PHALA_CLOUD_API_KEY）
#   prod       → sxysun 账号（PHALA_CLOUD_API_KEY，无前缀）
#
# redis CVM 用 `--kms phala`（部署账号授权），**不需要链上 AppAuth**，故本
# workflow 没有 publish-compose-hash 步骤（同 pg-deploy.yml，见
# docs/TEE_POSTGRES_SHADOW_PROVISIONING.md §0）。
#
# 首次开通不走本 workflow：cvm-id 文件读不到就失败，绝不静默新建 CVM
# （新建 CVM = 换钥事故，见 DEPLOYMENTS.md「新建 runner CVM 换掉主 enclave 钥」）。
name: Deploy Redis CVM
on:
  workflow_dispatch:
    inputs:
      environment:
        type: choice
        options: [test, pre, prod]
        required: true
      confirm:
        description: '输入 DEPLOY-REDIS（prod 需输入 DEPLOY-REDIS-PROD）确认（防误触）'
        required: true
permissions:
  contents: read
  packages: write
jobs:
  deploy:
    runs-on: ubuntu-latest
    concurrency: redis-deploy-${{ github.event.inputs.environment }}
    steps:
      - name: Typo guard
        env:
          CONFIRM: ${{ inputs.confirm }}
          ENVIRONMENT: ${{ inputs.environment }}
        run: |
          # prod 要求更长的确认串：它打的是另一个账号下的真实用户数据机器。
          if [ "$ENVIRONMENT" = "prod" ]; then WANT=DEPLOY-REDIS-PROD; else WANT=DEPLOY-REDIS; fi
          test "$CONFIRM" = "$WANT" || { echo "::error::confirm mismatch — ${ENVIRONMENT} 需要输入 ${WANT}"; exit 1; }

      - uses: actions/checkout@v4
        # 各环境部署各自的分支，与 app 的发布流向一致。
        with:
          ref: ${{ inputs.environment == 'prod' && 'main' || (inputs.environment == 'pre' && 'pre' || 'test') }}

      - name: Resolve image tag from checked-out HEAD
        id: img
        # 不能用 ${{ github.sha }}：workflow_dispatch 下它指向触发时所在 ref 的 sha，
        # 而上面 checkout 的是 test/pre/main，两者可以不同——tag 会与镜像内容对不上。
        run: echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"

      - name: Resolve CVM id
        id: cvm
        env:
          ENVIRONMENT: ${{ inputs.environment }}
        run: |
          F="deploy/${ENVIRONMENT}-redis-cvm-id.txt"
          test -f "$F" || { echo "::error::${F} 不存在 — 首次开通走 DEPLOYMENTS.md runbook，不走本 workflow"; exit 1; }
          # GHA 以 `bash -eo pipefail` 跑 run 步骤：纯注释的 cvm-id.txt 让
          # `grep -v '^#'` 退出 1，pipefail 把它当成整条管道的退出码，脚本会在
          # 这里裸退出、永远走不到下面那句明确的报错。`|| true` 吸收它，让
          # CVM_ID 正当地为空，由下一行的显式检查来 fail-closed。
          CVM_ID=$(grep -v '^#' "$F" | tr -d '[:space:]' | head -1 || true)
          test -n "$CVM_ID" || { echo "::error::${F} 为空 — 首次开通走 DEPLOYMENTS.md runbook，不走本 workflow"; exit 1; }
          echo "id=$CVM_ID" >> "$GITHUB_OUTPUT"

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Derive lowercase owner   # ghcr rejects mixed-case image paths
        id: owner
        run: echo "owner=$(echo '${{ github.repository_owner }}' | tr '[:upper:]' '[:lower:]')" >> "$GITHUB_OUTPUT"

      - name: Build & push redis image
        uses: docker/build-push-action@v6
        with:
          context: deploy
          file: deploy/redis/Dockerfile
          platforms: linux/amd64
          push: true
          tags: ghcr.io/${{ steps.owner.outputs.owner }}/feedling-redis:${{ steps.img.outputs.sha }}

      - name: Build & push backup sidecar image
        uses: docker/build-push-action@v6
        with:
          context: deploy
          file: deploy/redis/Dockerfile.backup
          platforms: linux/amd64
          push: true
          tags: ghcr.io/${{ steps.owner.outputs.owner }}/feedling-redis-backup:${{ steps.img.outputs.sha }}

      - name: Pin image shas into compose
        run: |
          sed -i -E "s|ghcr\.io/[^/]+/feedling-redis:[A-Za-z0-9_.-]+|ghcr.io/${{ steps.owner.outputs.owner }}/feedling-redis:${{ steps.img.outputs.sha }}|" \
            deploy/docker-compose.phala.redis.yaml
          sed -i -E "s|ghcr\.io/[^/]+/feedling-redis-backup:[A-Za-z0-9_.-]+|ghcr.io/${{ steps.owner.outputs.owner }}/feedling-redis-backup:${{ steps.img.outputs.sha }}|" \
            deploy/docker-compose.phala.redis.yaml
          grep -n 'feedling-redis' deploy/docker-compose.phala.redis.yaml

      - name: Deploy (update in place; 名字查不到就失败，绝不静默新建)
        env:
          ENVIRONMENT: ${{ inputs.environment }}
          CVM_ID: ${{ steps.cvm.outputs.id }}
          # 三套机密全都注入、在 shell 里按环境挑，而不是用
          # `${{ inputs.environment == 'prod' && secrets.PROD_X || secrets.TEST_X }}`：
          # 那种三元在 PROD_X 恰好为空时会短路 fallback 到 TEST_X，于是「非空校验」
          # 通过、注进 prod CVM 的却是 test 的密码。这里挑错只会挑到空值 → fail-closed。
          TEST_PHALA_TOKEN: ${{ secrets.TEST_PHALA_CLOUD_API_KEY }}
          PRE_PHALA_TOKEN: ${{ secrets.TEST_PHALA_CLOUD_API_KEY }}   # pre 与 test 同账号
          PROD_PHALA_TOKEN: ${{ secrets.PHALA_CLOUD_API_KEY }}       # prod 在 sxysun workspace，key 无前缀
          TEST_REDIS_PASSWORD: ${{ secrets.TEST_REDIS_PASSWORD }}
          TEST_REDIS_TLS_CERT_B64: ${{ secrets.TEST_REDIS_TLS_CERT_B64 }}
          TEST_REDIS_TLS_KEY_B64: ${{ secrets.TEST_REDIS_TLS_KEY_B64 }}
          TEST_REDIS_BACKUP_AGE_RECIPIENT: ${{ secrets.TEST_REDIS_BACKUP_AGE_RECIPIENT }}
          TEST_REDIS_BACKUP_R2_ENDPOINT: ${{ secrets.TEST_PG_BACKUP_R2_ENDPOINT }}
          TEST_REDIS_BACKUP_R2_ACCESS_KEY_ID: ${{ secrets.TEST_PG_BACKUP_R2_ACCESS_KEY_ID }}
          TEST_REDIS_BACKUP_R2_SECRET_ACCESS_KEY: ${{ secrets.TEST_PG_BACKUP_R2_SECRET_ACCESS_KEY }}
          PRE_REDIS_PASSWORD: ${{ secrets.PRE_REDIS_PASSWORD }}
          PRE_REDIS_TLS_CERT_B64: ${{ secrets.PRE_REDIS_TLS_CERT_B64 }}
          PRE_REDIS_TLS_KEY_B64: ${{ secrets.PRE_REDIS_TLS_KEY_B64 }}
          PRE_REDIS_BACKUP_AGE_RECIPIENT: ${{ secrets.PRE_REDIS_BACKUP_AGE_RECIPIENT }}
          PRE_REDIS_BACKUP_R2_ENDPOINT: ${{ secrets.TEST_PG_BACKUP_R2_ENDPOINT }}
          PRE_REDIS_BACKUP_R2_ACCESS_KEY_ID: ${{ secrets.TEST_PG_BACKUP_R2_ACCESS_KEY_ID }}
          PRE_REDIS_BACKUP_R2_SECRET_ACCESS_KEY: ${{ secrets.TEST_PG_BACKUP_R2_SECRET_ACCESS_KEY }}
          PROD_REDIS_PASSWORD: ${{ secrets.PROD_REDIS_PASSWORD }}
          PROD_REDIS_TLS_CERT_B64: ${{ secrets.PROD_REDIS_TLS_CERT_B64 }}
          PROD_REDIS_TLS_KEY_B64: ${{ secrets.PROD_REDIS_TLS_KEY_B64 }}
          PROD_REDIS_BACKUP_AGE_RECIPIENT: ${{ secrets.PROD_REDIS_BACKUP_AGE_RECIPIENT }}
          PROD_REDIS_BACKUP_R2_ENDPOINT: ${{ secrets.PROD_PG_BACKUP_R2_ENDPOINT }}
          PROD_REDIS_BACKUP_R2_ACCESS_KEY_ID: ${{ secrets.PROD_PG_BACKUP_R2_ACCESS_KEY_ID }}
          PROD_REDIS_BACKUP_R2_SECRET_ACCESS_KEY: ${{ secrets.PROD_PG_BACKUP_R2_SECRET_ACCESS_KEY }}
        run: |
          UP=$(echo "$ENVIRONMENT" | tr '[:lower:]' '[:upper:]')
          pick() { local n="${UP}_$1"; printf '%s' "${!n-}"; }

          # 非机密的按环境派生值。maxmemory 见 spec 第 2 节：只吃掉一半物理内存，
          # 给 BGSAVE 的 fork + copy-on-write 留余量。
          if [ "$ENVIRONMENT" = "prod" ]; then MAXMEM=2560mb; else MAXMEM=1gb; fi
          PREFIX="${ENVIRONMENT}/redis/"

          # 原地更新必须重带整份机密，漏一个就被清空 → entrypoint fail-closed
          # 起不来。先整份校验再动 CVM。
          VARS="REDIS_PASSWORD REDIS_TLS_CERT_B64 REDIS_TLS_KEY_B64
                REDIS_BACKUP_AGE_RECIPIENT REDIS_BACKUP_R2_ENDPOINT
                REDIS_BACKUP_R2_ACCESS_KEY_ID REDIS_BACKUP_R2_SECRET_ACCESS_KEY"
          missing=0
          for v in $VARS; do
            [ -n "$(pick "$v")" ] || { echo "::error::MISSING secret: ${UP}_${v}"; missing=1; }
          done
          TOKEN=$(pick PHALA_TOKEN)
          [ -n "$TOKEN" ] || { echo "::error::MISSING phala api token for ${ENVIRONMENT}"; missing=1; }
          [ "$missing" = 0 ] || exit 1

          # 机密落 0600 env 文件而不是拼进命令行：ps/日志里不会出现明文。
          ENVFILE=$(mktemp); chmod 600 "$ENVFILE"
          for v in $VARS; do printf '%s=%s\n' "$v" "$(pick "$v")" >> "$ENVFILE"; done
          printf 'REDIS_MAXMEMORY=%s\n' "$MAXMEM" >> "$ENVFILE"
          printf 'REDIS_BACKUP_S3_PREFIX=%s\n' "$PREFIX" >> "$ENVFILE"

          npm install -g phala@1.1.19
          phala deploy --api-token "$TOKEN" --cvm-id "$CVM_ID" \
            -c deploy/docker-compose.phala.redis.yaml \
            -e "$ENVFILE" --wait
          rm -f "$ENVFILE"

      - name: Post-deploy — 状态自检
        env:
          ENVIRONMENT: ${{ inputs.environment }}
          CVM_ID: ${{ steps.cvm.outputs.id }}
          TEST_PHALA_TOKEN: ${{ secrets.TEST_PHALA_CLOUD_API_KEY }}
          PRE_PHALA_TOKEN: ${{ secrets.TEST_PHALA_CLOUD_API_KEY }}
          PROD_PHALA_TOKEN: ${{ secrets.PHALA_CLOUD_API_KEY }}
        run: |
          UP=$(echo "$ENVIRONMENT" | tr '[:lower:]' '[:upper:]')
          eval "TOKEN=\${${UP}_PHALA_TOKEN}"
          phala cvms get "$CVM_ID" --api-token "$TOKEN" --json > cvm.json
          python3 - <<'PY'
          import json, sys
          d = json.load(open('cvm.json'))
          status = d.get('status')
          print('status:', status)
          print('compose_hash:', d.get('compose_hash'))
          if status != 'running':
              sys.exit(f'CVM 未回到 running: {status}')
          PY
          echo "提醒：sidecar 的首次快照在容器起来后立即推（boot 兜底），之后每小时一次；由 redis-monitor 的 2h 陈旧断言把关。"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k deploy_workflow`
Expected: 8 个测试 PASS。

- [ ] **Step 6: 校验 workflow 语法并构建服务端镜像**

```bash
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/redis-deploy.yml')); print('YAML-OK')"
docker build --platform linux/amd64 -f deploy/redis/Dockerfile -t feedling-redis:local deploy && echo BUILD-OK
```
Expected: `YAML-OK` 和 `BUILD-OK`。

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/redis-deploy.yml deploy/redis/Dockerfile tests/test_redis_cvm_config.py
git commit -m "feat(redis): manual deploy workflow with per-environment Phala account mapping"
```

---

## Task 9: 备份监控 workflow

**Files:**
- Create: `.github/workflows/redis-monitor.yml`
- Modify: `tests/test_redis_cvm_config.py`

**Interfaces:**
- Consumes: Task 4/5 的 R2 对象命名；Task 8 的 secret 命名
- Produces: 每 30 分钟跑一次的监控，检查 prod + pre

- [ ] **Step 1: 写失败的测试（追加到 `tests/test_redis_cvm_config.py`）**

```python
REDIS_MONITOR_WF = ROOT / ".github" / "workflows" / "redis-monitor.yml"


def test_monitor_runs_on_a_schedule_and_covers_prod_and_pre():
    wf = yaml.safe_load(REDIS_MONITOR_WF.read_text())
    triggers = wf[True] if True in wf else wf["on"]
    assert "schedule" in triggers
    assert triggers["schedule"][0]["cron"] == "*/30 * * * *"
    source = REDIS_MONITOR_WF.read_text()
    assert "prod/redis/" in source
    assert "pre/redis/" in source
    # test 数据可弃，不监控（与 pg-monitor.yml 同理）。
    assert "test/redis/" not in source


def test_monitor_handles_aws_pagination_correctly():
    # pg-monitor.yml 踩过的坑：list-objects-v2 自动分页，>1000 对象时
    # 每页各吐一个「本页最新」，下游解析直接炸；而 --no-paginate 只取
    # 第一页（最旧）反而误报 stale。正解是 sort | tail 取跨页全局最新。
    source = REDIS_MONITOR_WF.read_text()
    assert "sort" in source and "tail -n1" in source
    assert "--no-paginate" not in source


def test_monitor_captures_the_aws_exit_code_instead_of_trusting_the_pipe():
    # pg-monitor.yml 2026-07-24 修正的假警报：`aws … | sort | tail -n1` 的
    # 退出码取自 tail，恒为 0。R2 在分页途中限流时 aws 中断退出，但前几页
    # 已经打印出来，函数于是返回一个「偏旧的最大值」→ 误报备份陈旧。
    # 当天 11:15 prod 实测：吐出 09:54 报 stale 4950s，而库里
    # last_archived_time 只有 1s 前。正解是自己接退出码 + 退避重试。
    source = REDIS_MONITOR_WF.read_text()
    assert "rc=0 || rc=$?" in source
    assert "sleep" in source          # 退避
    # 措辞必须区分「R2 查不了」与「备份真的陈旧」，否则下一个人照着
    # 假信号去查备份链。
    assert "不等于备份陈旧" in source


def test_monitor_reads_memory_from_info_not_config():
    # CONFIG 被 rename-command 禁用；用 CONFIG GET 会永远报 unknown command。
    source = REDIS_MONITOR_WF.read_text()
    assert "CONFIG GET" not in source
    assert "INFO memory" in source


def test_monitor_checks_all_four_documented_signals():
    source = REDIS_MONITOR_WF.read_text()
    assert "rdb_last_bgsave_status" in source
    assert "aof_last_write_status" in source
    assert "used_memory" in source
    # 快照新鲜度阈值：1h 周期 + 一次失败的余量 = 2h
    assert "7200" in source
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k monitor`
Expected: FAIL —— 文件不存在。

- [ ] **Step 3: 写 `.github/workflows/redis-monitor.yml`**

```yaml
# .github/workflows/redis-monitor.yml — 没有监控 = 没有备份。
#
# 监控 prod + pre 的 Redis 备份链与内存水位。test 数据可弃，不监控
# （与 pg-monitor.yml 同理）。
name: Redis backup monitor
on:
  schedule: [{ cron: '*/30 * * * *' }]
  workflow_dispatch: {}
jobs:
  check:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - env_name: prod
            prefix: prod/redis/
          - env_name: pre
            prefix: pre/redis/
    steps:
      - name: R2 snapshot freshness
        env:
          AWS_ACCESS_KEY_ID: ${{ matrix.env_name == 'prod' && secrets.PROD_PG_BACKUP_R2_ACCESS_KEY_ID || secrets.TEST_PG_BACKUP_R2_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ matrix.env_name == 'prod' && secrets.PROD_PG_BACKUP_R2_SECRET_ACCESS_KEY || secrets.TEST_PG_BACKUP_R2_SECRET_ACCESS_KEY }}
          # GitHub runner 没有默认 region；即使 --endpoint-url 指向 R2，
          # aws CLI 仍会在客户端侧因缺 region 报错。
          AWS_DEFAULT_REGION: auto
          AWS_REGION: auto
          ENDPOINT: ${{ matrix.env_name == 'prod' && secrets.PROD_PG_BACKUP_R2_ENDPOINT || secrets.TEST_PG_BACKUP_R2_ENDPOINT }}
          PREFIX: ${{ matrix.prefix }}
        run: |
          # aws CLI 自动分页：一个前缀 >1000 对象时每页各输出一个「本页最新」
          # 时间戳，直接解析会炸。用 sort|tail 取跨页全局最新（对象 key 与
          # ISO 时间戳的字典序都等于时间序）。不能用 --no-paginate——那只取
          # 第一页（最旧的 key），反而误报 stale。
          #
          # aws 的退出码必须自己接：`aws … | sort | tail -n1` 的退出码取自 tail，
          # 恒为 0。R2 在分页途中限流（ServiceUnavailable: Reduce your concurrent
          # request rate）时 aws 中断退出，但前几页已经打印出来，函数于是返回一个
          # 「偏旧的最大值」，下游把它当成备份陈旧误报——2026-07-24 11:15 pg-monitor
          # 实测过一次。限流是瞬时的，故先退避重试；连续失败才报错，且措辞明确
          # 区分「R2 查不了」与「备份真的陈旧」。
          newest() {
            local prefix="$1" raw rc attempt
            for attempt in 1 2 3; do
              raw=$(aws s3api list-objects-v2 --endpoint-url "$ENDPOINT" \
                --bucket io-in-enclave-db --prefix "$prefix" \
                --query 'sort_by(Contents,&LastModified)[-1].LastModified' \
                --output text) && rc=0 || rc=$?
              if [ "$rc" -eq 0 ]; then printf '%s\n' "$raw" | sort | tail -n1; return 0; fi
              echo "::warning::ListObjectsV2 '${prefix}' 第 ${attempt} 次失败 (rc=${rc})，退避重试" >&2
              sleep $((attempt * 10))
            done
            echo "::error::ListObjectsV2 '${prefix}' 连续 3 次失败 — R2 侧问题，本次无法判定备份新鲜度（不等于备份陈旧）" >&2
            return 1
          }
          TS=$(newest "$PREFIX")
          echo "newest snapshot: $TS"
          if [ -z "$TS" ] || [ "$TS" = "None" ]; then
            echo "::error::${PREFIX} EMPTY — 一份备份都没有，不可恢复"; exit 1
          fi
          AGE=$(python3 -c "import sys, datetime as dt; s=sys.argv[1].strip(); s=s[:-1]+'+00:00' if s.endswith('Z') else s; d=dt.datetime.fromisoformat(s); d=d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d; print(int((dt.datetime.now(dt.timezone.utc)-d).total_seconds()))" "$TS")
          echo "snapshot age: ${AGE}s"
          # 备份周期 1h，留一次失败的余量 → 2h 才报警。
          test "$AGE" -lt 7200 || { echo "::error::snapshot stale ${AGE}s (>7200s) — 备份链断了"; exit 1; }

      - name: Redis persistence + memory headroom
        env:
          REDIS_HOST: ${{ matrix.env_name == 'prod' && secrets.PROD_REDIS_HOST || secrets.PRE_REDIS_HOST }}
          REDISCLI_AUTH: ${{ matrix.env_name == 'prod' && secrets.PROD_REDIS_PASSWORD || secrets.PRE_REDIS_PASSWORD }}
          REDIS_CA_B64: ${{ matrix.env_name == 'prod' && secrets.PROD_REDIS_CA_B64 || secrets.PRE_REDIS_CA_B64 }}
        run: |
          sudo apt-get update -qq && sudo apt-get install -y -qq redis-tools
          echo "$REDIS_CA_B64" | base64 -d > /tmp/redis-ca.crt
          R="redis-cli --tls --cacert /tmp/redis-ca.crt -h ${REDIS_HOST} -p 443"

          PERSIST=$($R INFO persistence | tr -d '\r')
          echo "$PERSIST" | grep -E '^(rdb_last_bgsave_status|aof_last_write_status):'
          echo "$PERSIST" | grep -q '^rdb_last_bgsave_status:ok' \
            || { echo "::error::rdb_last_bgsave_status != ok"; exit 1; }
          echo "$PERSIST" | grep -q '^aof_last_write_status:ok' \
            || { echo "::error::aof_last_write_status != ok — 第一层持久化已失效"; exit 1; }

          # CONFIG 被 rename-command 禁用，容量只能从 INFO memory 读。
          MEM=$($R INFO memory | tr -d '\r')
          USED=$(echo "$MEM" | grep '^used_memory:' | cut -d: -f2)
          MAX=$(echo "$MEM" | grep '^maxmemory:' | cut -d: -f2)
          echo "used_memory=${USED} maxmemory=${MAX}"
          export USED MAX
          # noeviction 下内存打满 = 写入开始报错，80% 是提前量。
          # heredoc 用 <<'PY'（引号=不做 shell 展开），值经环境变量传进去：
          # 直接把 ${USED} 插进 Python 源码会在值为空时变成语法错误，
          # 报一个与真实问题无关的 SyntaxError。
          python3 - <<'PY'
          import os, sys
          used, mx = int(os.environ["USED"] or 0), int(os.environ["MAX"] or 0)
          if mx == 0:
              sys.exit('::error::maxmemory is 0 — 未设上限，noeviction 下会一直吃到 OOM')
          pct = used * 100 // mx
          print(f'memory usage: {pct}%')
          if pct >= 80:
              sys.exit(f'::error::memory at {pct}% of maxmemory (>=80%) — noeviction 下写入即将开始报错')
          PY
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_redis_cvm_config.py -v -k monitor`
Expected: 4 个测试 PASS。

- [ ] **Step 5: 跑本 Task 之前的全部新测试**

Run: `python -m pytest tests/test_redis_cvm_config.py tests/test_redis_backup_scripts.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/redis-monitor.yml tests/test_redis_cvm_config.py
git commit -m "feat(redis): backup freshness + persistence + memory headroom monitor"
```

---

## Task 10: 本地端到端演练（真 Redis + 真 MinIO + 真 age）

这个 Task 验证前面所有 stub 测试覆盖不到的东西：真实的 SYNC 快照、真实的 age 加解密、真实的 S3 上传下载，以及**恢复出来的数据确实等于原数据**。

**Files:**
- Create: `deploy/redis/docker-compose.e2e.yaml`
- Create: `deploy/redis/e2e-drill.sh`

**Interfaces:**
- Consumes: Task 1-6 的全部脚本
- Produces: `e2e-drill.sh` 退出码 0 = 备份→恢复全链路可用

- [ ] **Step 1: 写 `deploy/redis/docker-compose.e2e.yaml`**

```yaml
# deploy/redis/docker-compose.e2e.yaml — 本地端到端演练用（不用于部署）。
# 用 MinIO 冒充 R2（同为 S3 兼容），跑通「写数据 → 备份 → 恢复 → 校验」。
name: feedling-redis-e2e
services:
  minio:
    image: minio/minio:latest
    command: server /data --address ":9000"
    environment:
      MINIO_ROOT_USER: e2eaccess
      MINIO_ROOT_PASSWORD: e2esecret123
    ports:
      - "19000:9000"
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 3s
      timeout: 3s
      retries: 20

  redis:
    build:
      context: ..
      dockerfile: redis/Dockerfile
    environment:
      REDIS_PASSWORD: e2epassword0123456789abcdef
      # 自签证书在 e2e-drill.sh 里生成后注入
      REDIS_TLS_CERT_B64: "${E2E_CERT_B64}"
      REDIS_TLS_KEY_B64: "${E2E_KEY_B64}"
      REDIS_MAXMEMORY: 256mb
      REDIS_BACKUP_S3_PREFIX: e2e/redis/
      REDIS_BACKUP_AGE_RECIPIENT: "${E2E_AGE_RECIPIENT}"
      REDISCLI_AUTH: e2epassword0123456789abcdef
    volumes:
      - redisdata:/data
      - redissock:/var/run/redis
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -s /var/run/redis/redis.sock ping | grep -q PONG"]
      interval: 3s
      timeout: 3s
      retries: 20

  backup:
    build:
      context: ..
      dockerfile: redis/Dockerfile.backup
    depends_on:
      redis:
        condition: service_healthy
      minio:
        condition: service_healthy
    environment:
      REDISCLI_AUTH: e2epassword0123456789abcdef
      REDIS_BACKUP_S3_PREFIX: e2e/redis/
      REDIS_BACKUP_AGE_RECIPIENT: "${E2E_AGE_RECIPIENT}"
      REDIS_BACKUP_BUCKET: e2e-bucket
      AWS_ENDPOINT_URL: http://minio:9000
      AWS_ACCESS_KEY_ID: e2eaccess
      AWS_SECRET_ACCESS_KEY: e2esecret123
      AWS_REGION: auto
      AWS_DEFAULT_REGION: auto
      # 演练不等一小时：跑完 boot 兜底那一轮就退出。
      BACKUP_INTERVAL_SEC: "0"
    volumes:
      - redissock:/var/run/redis

volumes:
  redisdata:
  redissock:
```

- [ ] **Step 2: 写 `deploy/redis/e2e-drill.sh`**

```sh
#!/bin/bash
# deploy/redis/e2e-drill.sh — 本地端到端备份/恢复演练。
#
# 这是 spec 第 8 节「restore 演练」的可重复版本：写已知数据 → 备份到
# S3 兼容存储 → 在空实例上恢复 → 逐项校验。CVM 首次开通时用同样的
# 流程对着真 R2 跑一遍（见 Task 12）。
set -euo pipefail
cd "$(dirname "$0")"

WORK="$(mktemp -d)"
cleanup() {
    docker compose -f docker-compose.e2e.yaml down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "== 1. 生成 age 密钥对与 TLS 材料 =="
docker run --rm --entrypoint sh redis:8-alpine -c \
  'apk add --no-cache age >/dev/null 2>&1 && age-keygen 2>/dev/null' > "$WORK/age-key.txt"
E2E_AGE_RECIPIENT="$(grep 'public key:' "$WORK/age-key.txt" | sed 's/.*: //')"
grep -v '^#' "$WORK/age-key.txt" > "$WORK/identity.txt"
export E2E_AGE_RECIPIENT

./gen-certs.sh feedling-redis-e2e "$WORK/certs" >/dev/null
export E2E_CERT_B64="$(base64 < "$WORK/certs/server.crt" | tr -d '\n')"
export E2E_KEY_B64="$(base64 < "$WORK/certs/server.key" | tr -d '\n')"

echo "== 2. 起 MinIO + Redis，建桶 =="
docker compose -f docker-compose.e2e.yaml up -d minio redis
sleep 5
docker compose -f docker-compose.e2e.yaml exec -T minio \
  sh -c 'mc alias set local http://127.0.0.1:9000 e2eaccess e2esecret123 >/dev/null && mc mb -p local/e2e-bucket'

echo "== 3. 写入已知数据 =="
REDIS_EXEC="docker compose -f docker-compose.e2e.yaml exec -T redis redis-cli -s /var/run/redis/redis.sock"
for i in $(seq 1 100); do
    $REDIS_EXEC SET "drill:key:$i" "value-$i" >/dev/null
done
$REDIS_EXEC SET "drill:with-ttl" "expires" EX 3600 >/dev/null
BEFORE_COUNT="$($REDIS_EXEC DBSIZE | tr -d '\r')"
echo "写入完成，DBSIZE=${BEFORE_COUNT}"
test "$BEFORE_COUNT" -eq 101 || { echo "FAIL: 期望 101 个 key"; exit 1; }

echo "== 4. 触发备份（sidecar 的 boot 兜底那一轮）=="
docker compose -f docker-compose.e2e.yaml up backup
OBJECTS="$(docker compose -f docker-compose.e2e.yaml exec -T minio \
  sh -c 'mc ls --recursive local/e2e-bucket/e2e/redis/' | wc -l | tr -d ' ')"
echo "R2(MinIO) 中的快照数：${OBJECTS}"
test "$OBJECTS" -ge 1 || { echo "FAIL: 没有快照落地"; exit 1; }

echo "== 5. 在空实例上恢复 =="
# 关键：目标实例必须先关 appendonly，否则 Redis 优先加载 AOF、忽略 dump.rdb。
docker run --rm -d --name redis-restore-target \
  -v "${WORK}/restored:/data" \
  --entrypoint sh redis:8-alpine -c 'sleep 300'

docker run --rm --network "feedling-redis-e2e_default" \
  -v "${WORK}/restored:/data" \
  -v "${WORK}/identity.txt:/identity.txt:ro" \
  -v "${PWD}/restore.sh:/restore.sh:ro" \
  -e REDIS_BACKUP_AGE_IDENTITY_FILE=/identity.txt \
  -e REDIS_BACKUP_BUCKET=e2e-bucket \
  -e REDIS_BACKUP_S3_PREFIX=e2e/redis/ \
  -e AWS_ENDPOINT_URL=http://minio:9000 \
  -e AWS_ACCESS_KEY_ID=e2eaccess \
  -e AWS_SECRET_ACCESS_KEY=e2esecret123 \
  -e AWS_REGION=auto -e AWS_DEFAULT_REGION=auto \
  -e RESTORE_DIR=/data \
  --entrypoint sh \
  "$(docker compose -f docker-compose.e2e.yaml images -q backup)" /restore.sh

docker rm -f redis-restore-target >/dev/null 2>&1 || true

echo "== 6. 校验恢复结果 =="
docker run --rm -d --name redis-verify \
  -v "${WORK}/restored:/data" \
  redis:8-alpine redis-server --appendonly no --dir /data --dbfilename dump.rdb
sleep 3
AFTER_COUNT="$(docker exec redis-verify redis-cli DBSIZE | tr -d '\r')"
SAMPLE="$(docker exec redis-verify redis-cli GET drill:key:42 | tr -d '\r')"
TTL="$(docker exec redis-verify redis-cli TTL drill:with-ttl | tr -d '\r')"
docker rm -f redis-verify >/dev/null

echo "恢复后 DBSIZE=${AFTER_COUNT}（原 ${BEFORE_COUNT}）"
echo "抽样 drill:key:42=${SAMPLE}（期望 value-42）"
echo "TTL drill:with-ttl=${TTL}（期望 >0）"

test "$AFTER_COUNT" -eq "$BEFORE_COUNT" || { echo "FAIL: key 数量不一致"; exit 1; }
test "$SAMPLE" = "value-42"             || { echo "FAIL: 抽样值不一致"; exit 1; }
test "$TTL" -gt 0                       || { echo "FAIL: TTL 语义未保留"; exit 1; }

echo "== DRILL PASSED =="
```

- [ ] **Step 3: 跑演练**

Run: `chmod +x deploy/redis/e2e-drill.sh && ./deploy/redis/e2e-drill.sh`
Expected: 最后一行 `== DRILL PASSED ==`，退出码 0。

若第 5 步报网络名找不到，用 `docker network ls | grep redis-e2e` 查实际名称并修正脚本里的 `--network` 值。

- [ ] **Step 4: Commit**

```bash
git add deploy/redis/docker-compose.e2e.yaml deploy/redis/e2e-drill.sh
git commit -m "test(redis): end-to-end backup/restore drill with MinIO"
```

---

## Task 11: 文档

**Files:**
- Modify: `deploy/DEPLOYMENTS.md`（新增「TEE Redis」章节）
- Modify: `docs/CHANGELOG.md`
- Test: 无新测试（文档任务）

**Interfaces:**
- Consumes: Task 1-10 的全部产出
- Produces: 可照着执行的首次开通 runbook

- [ ] **Step 1: 在 `deploy/DEPLOYMENTS.md` 的「TEE Postgres」章节之后新增**

```markdown
## TEE Redis — 待开通（test + pre + prod）

设计文档 `docs/superpowers/specs/2026-07-24-tee-redis-cvm-design.md`，
实施计划 `docs/superpowers/plans/2026-07-24-tee-redis-cvm.md`。

**当前状态**：代码已就绪，三台 CVM 尚未开通（三个 `deploy/*-redis-cvm-id.txt`
仍是纯注释 → `redis-deploy` workflow 会 fail-closed 拒绝运行，这是刻意的）。

| | test | pre | prod |
|---|---|---|---|
| CVM 名 | `feedling-redis-test` | `feedling-redis-pre` | `feedling-redis-prod` |
| Phala 账号 | `amiller-user` | `amiller-user` | **`sxysun`** |
| 规格 | 1 vCPU / 2 GB / 20 GB | 1 vCPU / 2 GB / 20 GB | 2 vCPU / 4 GB / 30 GB |
| `maxmemory` | 1 GB | 1 GB | 2560 MB |
| API key secret | `TEST_PHALA_CLOUD_API_KEY` | `TEST_PHALA_CLOUD_API_KEY` | `PHALA_CLOUD_API_KEY` |
| 机密 secret 前缀 | `TEST_REDIS_*` | `PRE_REDIS_*` | `PROD_REDIS_*` |
| 身份模型 | `--kms phala`（无链上 AppAuth） | 同左 | 同左 |
| 部署分支 | `test` | `pre` | `main` |
| R2 前缀 | `test/redis/` | `pre/redis/` | `prod/redis/` |
| 监控 | 不监控（数据可弃） | ✅ | ✅ |

R2 桶复用 PG 备份的 `io-in-enclave-db`，靠前缀隔离。**R2 token 的 scope 必须
覆盖新前缀**（PG 那边 `io-user-attachments` 踩过 token scope 不够导致 PUT
`AccessDenied` 的坑）。

### 首次开通 runbook（每环境各跑一遍，不走 workflow）

1. **切 Phala profile**：test/pre 用 miller 的；**prod 必须先切到 `sxysuns` profile**
   （最容易忘的一步）。
2. `phala cvms create` 建 CVM，按上表选规格，记下 `app_id` 与 `cvm_id`。
3. `deploy/redis/gen-certs.sh feedling-redis-<env> <outdir>` 生成 TLS 材料。
   **`ca.key` 立即移到离线冷存**。
4. 生成 age 密钥对：`age-keygen -o <env>-redis-backup.key`。**私钥离线冷存**
   （按 PG 的「内容钥 + 备份钥」双钥托管流程分存），公钥填进
   `<PREFIX>_REDIS_BACKUP_AGE_RECIPIENT` secret。
5. 口令：`openssl rand -hex 32` → `<PREFIX>_REDIS_PASSWORD`。
   **必须是十六进制**：引号 / `$` / 反引号会破坏 compose env 注入。
6. **身份模型**：建 CVM 时用 `--kms phala`（Phala 默认 KMS 按部署账号授权）。
   redis CVM **不需要链上 AppAuth**，与 TEE Postgres 同（见
   `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` §0）。**切勿复用主 app 的 AppAuth
   合约**——那会翻掉主 enclave 的钥。
7. 首次 `phala deploy` 注入全部加密 env（参数照抄 `redis-deploy.yml` 的 Deploy 步骤）。
8. 把 `cvm_id` 写进 `deploy/<env>-redis-cvm-id.txt` 并提交。
9. 冒烟（占位值先设成变量——直接把 `<app-id>` 写进命令行时 `<` `>` 会被
   shell 当成重定向）：
   ```bash
   APP_ID="<填入本环境 app_id>"        # 只有这一行需要手改（引号别去掉）
   REDIS_HOST="${APP_ID}-6379s.dstack-pha-prod9.phala.network"
   read -rs REDIS_PW                   # 交互输入，不进 shell history
   REDIS_CA_FILE=./ca.crt REDISCLI_AUTH="$REDIS_PW" \
     ./deploy/verify-redis.sh "$REDIS_HOST" 443
   ```
   期望最后一行 `[verify] ALL GREEN`。
10. **restore 演练（硬 gate，不做完不算开通）**：本地先跑
    `./deploy/redis/e2e-drill.sh` 确认链路，再对着真 R2 前缀重跑一次
    第 5-6 步的恢复与校验。
11. 把 `<app-id>-6379s.…` 主机名填进 `<PREFIX>_REDIS_HOST`、`ca.crt` 的 base64
    填进 `<PREFIX>_REDIS_CA_B64`（监控 workflow 要用）。
12. 手动触发一次 `redis-monitor` workflow，确认全绿。

### 已知限制

- **Redis 端口在公网可达**。dstack CVM 之间没有私网，跨 CVM 只能走 gateway
  passthrough `<app-id>-6379s.…:443`，只靠 TLS + AUTH 保护。TEE Postgres 现在
  也是这个模型。
- **`CONFIG` 命令被禁用**：查容量只能 `INFO memory`，不能 `CONFIG GET maxmemory`。
- **单实例无 HA**：实例故障需人工恢复，RPO ≤1h 由备份保证。
- **prod 账号余额**：test 的老 CVM 就是在 `sxysun` 账号下余额耗尽被废弃
  （2026-06-18，app_id 报废 + 内容钥全换）。多一台 CVM 多一份烧钱速率。
```

- [ ] **Step 2: 在 `docs/CHANGELOG.md` 顶部加一条**

```markdown
## 2026-07-24 — TEE Redis CVM 基础设施（未开通）

三套独立 Redis CVM（test/pre/prod）的全部代码就绪：官方 `redis:8-alpine`
TLS-only + backup sidecar（每小时 `redis-cli --rdb` 快照 → age 非对称加密 → R2）。
部署纪律复刻 TEE Postgres：`--kms phala` 身份（无链上 AppAuth）、手动 workflow、
cvm-id fail-closed、永不并入 merge 自动部署。

**当前零流量**：没有任何业务代码引用 Redis，三台 CVM 也尚未开通
（cvm-id 文件为空 → workflow 拒绝运行）。缓存 / 队列 / 锁的接入各自另开 spec。

关键决策见 `docs/superpowers/specs/2026-07-24-tee-redis-cvm-design.md`：
`noeviction`（避免静默驱逐锁与队列）、sidecar 而非内嵌镜像、显式 sleep 循环
而非 cron（PG 2026-07-14 cron PATH 静默失败的教训）、`redis-cli --rdb` 而非
拷卷文件、age 非对称加密（备份机被攻破也解不了历史备份）。
```

- [ ] **Step 3: 评估公开文档是否需要同步**

按 `CLAUDE.md` 的规则判断：本次改动**不涉及**公开 API 契约、用户可见行为、
信任边界（零流量待命），但**改变部署拓扑**。

执行：
```bash
grep -rn "架构\|architecture\|topology\|Postgres\|CVM" docs-site/content/docs/self-hosting.mdx | head -20
```

判断标准：若自托管页或架构页列举了「组成系统的 CVM 清单」，则需补充 Redis；
若只描述数据流与信任边界（Redis 零流量时不参与），则不需要改。

把判断结论（改了什么 / 为什么不用改）写进本 Task 的完成报告。若判定需要改，
则在同一 commit 里更新，并从 `docs-site` 跑：
```bash
npm run types:check && npm run lint && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add deploy/DEPLOYMENTS.md docs/CHANGELOG.md
git commit -m "docs(redis): TEE Redis provisioning runbook + changelog"
```

---

## Task 12: 三环境开通与验收

这个 Task **不写代码**，是照着 runbook 执行运维操作并逐项勾验收。它需要
Phala 账号凭证与离线密钥托管流程，**必须由人执行或在人的监督下执行**。

**Files:** 只改 `deploy/{test,pre,prod}-redis-cvm-id.txt`（填入真实 CVM id）

**Interfaces:**
- Consumes: Task 11 的 runbook
- Produces: 三台运行中的 CVM + 完整的 secret 组 + 验收记录

- [ ] **Step 1: 按 runbook 开通 test**

执行 `deploy/DEPLOYMENTS.md` 新增章节的第 1-12 步（环境 = test）。

- [ ] **Step 2: test 环境验收**

逐项确认并记录输出：

```bash
# 先把这几个设成变量：命令行里裸写 <app-id> 会被 shell 当成重定向。
APP_ID="<填入本环境 app_id>"      # 引号别去掉：裸 <> 会被当成重定向
ENV_NAME=test
R2_ENDPOINT="<填入 R2 endpoint>"
GW=dstack-pha-prod9.phala.network
read -rs REDIS_PW          # 交互输入，不进 shell history

# a) 冒烟全绿
REDIS_CA_FILE=./ca.crt REDISCLI_AUTH="$REDIS_PW" \
  ./deploy/verify-redis.sh "${APP_ID}-6379s.${GW}" 443

# b) 明文端口确实关闭：不带 --tls 连接必须失败
redis-cli -h "${APP_ID}-6379.${GW}" -p 443 PING || echo "PLAINTEXT-REJECTED-OK"

# c) 备份真的在跑：等两个周期后 R2 前缀下应有 ≥2 份快照
aws s3 ls "s3://io-in-enclave-db/${ENV_NAME}/redis/" --endpoint-url "$R2_ENDPOINT"
```

Expected:（a）`ALL GREEN`；（b）`PLAINTEXT-REJECTED-OK`；（c）≥2 个 `.rdb.age` 对象。

- [ ] **Step 3: test 环境 fail-closed 验证**

故意用**缺 `REDIS_BACKUP_AGE_RECIPIENT`** 的参数部署一次，确认容器拒绝启动。
机密走 0600 env 文件而不是命令行——手工执行时同样不该让口令进 shell history：

```bash
ENVFILE=$(mktemp); chmod 600 "$ENVFILE"
cat > "$ENVFILE" <<EOF
REDIS_PASSWORD=<pw>
REDIS_TLS_CERT_B64=<cert-b64>
REDIS_TLS_KEY_B64=<key-b64>
REDIS_MAXMEMORY=1gb
REDIS_BACKUP_S3_PREFIX=test/redis/
EOF
# 刻意不写 REDIS_BACKUP_AGE_RECIPIENT —— 这正是要触发的 fail-closed 分支
phala deploy --api-token "$TEST_PHALA_CLOUD_API_KEY" --cvm-id <test-cvm-id> \
  -c deploy/docker-compose.phala.redis.yaml -e "$ENVFILE" --wait
rm -f "$ENVFILE"
```

Expected: 容器日志出现
`FATAL: REDIS_BACKUP_S3_PREFIX set but REDIS_BACKUP_AGE_RECIPIENT missing`，
容器不进入 healthy。**验证完立刻用完整参数重新部署把 test 恢复正常。**

- [ ] **Step 4: test 环境 restore 演练（硬 gate）**

对着真 R2 的 `test/redis/` 前缀跑一次恢复，校验 key 数量、抽样值、TTL 语义。
步骤同 `e2e-drill.sh` 的第 5-6 步，但 `AWS_ENDPOINT_URL` 指向真 R2、
`REDIS_BACKUP_AGE_IDENTITY_FILE` 用离线冷存取出的私钥。

Expected: key 总数一致、抽样值逐字一致、TTL >0。**这一步不通过就不要开通 pre/prod。**

- [ ] **Step 5: 填 cvm-id 并 commit**

```bash
# 把真实 id 填进文件（保留注释行）
git add deploy/test-redis-cvm-id.txt
git commit -m "chore(redis): record feedling-redis-test CVM id"
```

- [ ] **Step 6: 开通 pre，重复 Step 1-5**

环境 = pre。额外一步：把 `PRE_REDIS_HOST` / `PRE_REDIS_CA_B64` 填进 secrets
（监控要用）。

- [ ] **Step 7: 开通 prod，重复 Step 1-5**

环境 = prod。**开通前先确认 `sxysun` 账号余额充足**（老 test CVM 就是在这个
账号下余额耗尽被废弃的）。额外一步：填 `PROD_REDIS_HOST` / `PROD_REDIS_CA_B64`。

- [ ] **Step 8: 手动触发监控确认全绿**

在 GitHub Actions 里手动跑一次 `Redis backup monitor`。

Expected: prod 与 pre 两个 matrix 分支都绿。

- [ ] **Step 9: Definition of Done 逐项勾验**

对照 spec 第 8 节：

- [ ] 三台 CVM 均 healthy，`verify-redis.sh` 三环境全绿
- [ ] 三个 R2 前缀下均有 ≥2 份快照（证明周期循环在跑，不只是 boot 那一份）
- [ ] restore 演练三环境各做一次并通过校验
- [ ] `redis-monitor.yml` 手动触发一次全绿
- [ ] 明文端口验证：非 TLS 连接被拒绝
- [ ] fail-closed 验证：缺备份公钥时容器拒绝启动
- [ ] 三份 cvm-id 文件已提交，三个 app_id 已记入 `DEPLOYMENTS.md`
- [ ] **零业务流量**：`grep -ri "redis" backend/ tools/ --include="*.py"` 无实质命中
      （只应命中 `redistill` 之类的子串）

- [ ] **Step 10: 更新 DEPLOYMENTS.md 状态并 commit**

把「待开通」改成「✅ 已开通（test + pre + prod）」，补上三个 app_id、
三个 gateway 主机名、开通日期。

```bash
git add deploy/DEPLOYMENTS.md deploy/pre-redis-cvm-id.txt deploy/prod-redis-cvm-id.txt
git commit -m "chore(redis): record pre/prod CVM ids and mark TEE Redis live"
```

---

## 附：全量测试

每个 Task 结束时至少跑本计划新增的测试：

```bash
python -m pytest tests/test_redis_cvm_config.py tests/test_redis_backup_scripts.py -v
```

**最终验收**须在有 Postgres 的环境跑全量（否则 conftest 会静默少收集约 2000 用例，
「全绿」是假象）：

```bash
docker run -d --name pg-test -p 55432:5432 -e POSTGRES_PASSWORD=postgres postgres:17
DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres python -m pytest -q
```

对照 `docs/testing/TESTING.md` §2 的决策矩阵：本计划属于「compose / CVM / 部署」
变更类，要求 = compose 静态断言 + 脚本行为测试 + 真容器烟测 + 部署后 live 验证，
以上均已编入 Task 1-12。
