"""TEE Redis CVM 的配置不变量。

这些断言保护的是「配错了会静默变得不安全」的项：明文端口没关、
驱逐策略被改成会吃掉锁和队列的 allkeys-*、高危命令没禁。
"""

from __future__ import annotations

import base64
import functools
import re
import stat
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).parent.parent
REDIS_CONF = ROOT / "deploy" / "redis" / "redis.conf"
ENTRYPOINT = ROOT / "deploy" / "redis" / "entrypoint-wrapper.sh"


@functools.lru_cache(maxsize=1)
def _self_signed_pem_pair() -> tuple[str, str]:
    """一次性生成一对能通过 wrapper 结构校验的自签 cert/key（PEM 文本）。

    用 `cryptography` 库而非 shell 出 openssl，测试就不依赖本机是否装了
    openssl、也不依赖网络。EC 曲线是为了让每次跑测试都够快。
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "redis-cvm-test")])
    import datetime

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


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


def _valid_material_env(tmp_path: Path) -> dict[str, str]:
    # entrypoint 写死了 /etc/redistls、/etc/redis、/var/run/redis——普通用户
    # 在本机（尤其 macOS）跑测试没权限创建这些路径，所以落盘目录必须能
    # 被环境变量重定向进 tmp_path。默认值（不传这三个变量时）与生产
    # 硬编码路径完全一致，这里显式覆盖只是为了让测试能落地。
    cert_pem, key_pem = _self_signed_pem_pair()
    return {
        "REDIS_PASSWORD": "a" * 64,
        "REDIS_TLS_CERT_B64": base64.b64encode(cert_pem.encode("ascii")).decode("ascii"),
        "REDIS_TLS_KEY_B64": base64.b64encode(key_pem.encode("ascii")).decode("ascii"),
        "REDIS_MAXMEMORY": "256mb",
        "REDIS_TLS_DIR": str(tmp_path / "redistls"),
        "REDIS_CONF_DIR": str(tmp_path / "redis"),
        "REDIS_RUN_DIR": str(tmp_path / "run" / "redis"),
    }


def test_entrypoint_happy_path_reaches_dry_run_success(tmp_path):
    # 全部材料合法、没配备份变量 → 应该走完校验+落盘，停在 DRY_RUN 出口。
    result = _run_entrypoint(_valid_material_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "[redis-init] dry run OK" in result.stdout


def test_entrypoint_write_path_produces_correct_material_and_secret_conf(tmp_path):
    # Finding 1 之所以被漏掉，就是因为没有测试真正走到落盘这一步——
    # 这个测试直接检查解出来的 cert/key 内容、secret.conf 的内容与权限。
    env = _valid_material_env(tmp_path)
    result = _run_entrypoint(env)
    assert result.returncode == 0, result.stderr

    tls_dir = tmp_path / "redistls"
    cert_pem, key_pem = _self_signed_pem_pair()
    assert (tls_dir / "server.crt").read_text() == cert_pem
    assert (tls_dir / "server.key").read_text() == key_pem
    # 私钥只有属主可读；证书任何人可读。
    assert stat.S_IMODE((tls_dir / "server.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tls_dir / "server.crt").stat().st_mode) == 0o644

    secret_conf = tmp_path / "redis" / "secret.conf"
    text = secret_conf.read_text()
    assert f"requirepass {env['REDIS_PASSWORD']}" in text
    assert f"maxmemory {env['REDIS_MAXMEMORY']}" in text
    # secret.conf 装着口令，权限必须是 600——umask 077 就是为此设的。
    assert stat.S_IMODE(secret_conf.stat().st_mode) == 0o600

    assert (tmp_path / "run" / "redis").is_dir()

    # 校验失败时不留半成品：happy path 里不该有残留的 .tmp 文件。
    assert not (tls_dir / "server.crt.tmp").exists()
    assert not (tls_dir / "server.key.tmp").exists()


def test_entrypoint_rejects_malformed_base64_tls_cert(tmp_path):
    # 复现 review 发现的问题：busybox 的 base64 -d 对非法输入照样 exit 0，
    # 所以校验必须落在 wrapper 自己对解码内容的结构检查上，而不是
    # base64 -d 的返回码。
    env = _valid_material_env(tmp_path)
    env["REDIS_TLS_CERT_B64"] = "not-valid-base64!!"
    result = _run_entrypoint(env)
    assert result.returncode != 0
    assert "REDIS_TLS_CERT_B64" in result.stderr
    # fail-closed：校验失败不能在文件系统上留下垃圾证书或半成品临时文件。
    assert not (tmp_path / "redistls" / "server.crt").exists()
    assert not (tmp_path / "redistls" / "server.crt.tmp").exists()


def test_entrypoint_rejects_malformed_base64_tls_key(tmp_path):
    # 同上，但针对私钥变量——确认报错信息点名的是 KEY 不是 CERT。
    env = _valid_material_env(tmp_path)
    env["REDIS_TLS_KEY_B64"] = base64.b64encode(b"this is not a PEM private key").decode("ascii")
    result = _run_entrypoint(env)
    assert result.returncode != 0
    assert "REDIS_TLS_KEY_B64" in result.stderr


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


GEN_CERTS = ROOT / "deploy" / "redis" / "gen-certs.sh"


def test_gen_certs_produces_a_cert_valid_for_the_gateway_hostname(tmp_path):
    # gateway passthrough 的域名是 <app-id>-6379s.dstack-pha-prod9.phala.network，
    # app_id 要首次部署后才知道 → 用通配 SAN 一次覆盖，使客户端仍能做
    # verify-full 主机名校验（而不是降级成「只加密不校验」）。
    result = subprocess.run(
        ["bash", str(GEN_CERTS), "feedling-redis-test", str(tmp_path)],
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
        ["bash", str(GEN_CERTS), "feedling-redis-test", str(tmp_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "REDIS_TLS_CERT_B64=" in result.stdout
    assert "REDIS_TLS_KEY_B64=" in result.stdout
    # base64 必须是单行，否则粘进 -e 注入会被换行截断（原始 base64 每 76 列
    # 换行一次，脚本靠 `| tr -d '\n'` 压平；这里在原始 stdout 上匹配
    # 「直到下一个换行符」的内容，而不是先 splitlines() 再检查——
    # splitlines() 会把每一行的换行符都剥掉，导致「不含 \n」恒成立、
    # 测试永远绿而检测不出 tr -d '\n' 被删掉的回归。
    # 顺带解码校验内容与脚本落盘的 server.crt/server.key 字节一致，
    # 这样既证明了整段确实在同一行，也证明了值本身没被截断/写错。
    cert_match = re.search(r"REDIS_TLS_CERT_B64=([^\n]*)\n", result.stdout)
    key_match = re.search(r"REDIS_TLS_KEY_B64=([^\n]*)\n", result.stdout)
    assert cert_match, "REDIS_TLS_CERT_B64 一行内没有跟着换行符（可能跨了多行）"
    assert key_match, "REDIS_TLS_KEY_B64 一行内没有跟着换行符（可能跨了多行）"
    assert base64.b64decode(cert_match.group(1)) == (tmp_path / "server.crt").read_bytes()
    assert base64.b64decode(key_match.group(1)) == (tmp_path / "server.key").read_bytes()


def test_gen_certs_locks_down_private_key_permissions(tmp_path):
    subprocess.run(
        ["bash", str(GEN_CERTS), "feedling-redis-test", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    for name in ("ca.key", "server.key"):
        mode = (tmp_path / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} has mode {oct(mode)}"


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
    # TTL 检查本身：真正发出 TTL 命令，并且 command substitution 失败时
    # 不能被 set -e 无声吞掉（Finding 2）——赋值那一行必须自己挂 fatal，
    # 不能指望紧随其后的 `[ "$TTL" -gt 0 ]` 分支来兜底（那一行在 set -e
    # 下永远执行不到）。用正则而非逐字符串匹配，免得空格数一变测试就炸。
    assert re.search(r'TTL="\$\(\$R TTL "\$KEY"\)"\s*\|\|\s*fatal', source), (
        "TTL query's command substitution must be guarded by || fatal on the "
        "same line, or set -e silently kills the script with no diagnostic"
    )


def test_verify_script_checks_persistence_write_status_is_not_err():
    # Finding 1 的回归哨兵：脚本曾经只把 aof_last_write_status /
    # rdb_last_bgsave_status 拉进 PERSIST 变量并 echo 出来，从不断言，
    # 两个字段都是 err 也能拿到 ALL GREEN。这里钉住两个 err 分支都存在，
    # 防止未来重构把断言又悄悄删掉。
    source = VERIFY.read_text()
    assert "aof_last_write_status:err" in source
    assert "rdb_last_bgsave_status:err" in source


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
    #
    # 之前版本只 grep 字面量 "&& secrets.PROD_"：换行/空格写法
    #   ${{ inputs.environment == 'prod' &&
    #       secrets.PROD_X ||
    #       secrets.TEST_X }}
    # 或 GHA 的动态下标写法 secrets[format('{0}_X', ...)] 都能绕过那条
    # 字符串检查却达到同样的短路效果。这里改成结构性检查：把每个
    # ${{ ... }} 表达式整体取出（DOTALL 跨行），只要表达式里同时出现
    # "secrets" 和 &&/|| 就判定为短路选择机密，不管中间的空白/换行怎么写；
    # 另外单独禁止 secrets[ 这种动态下标写法。
    source = REDIS_DEPLOY_WF.read_text()
    for expr in re.findall(r"\$\{\{(.*?)\}\}", source, re.DOTALL):
        if "secrets" in expr:
            assert "&&" not in expr and "||" not in expr, (
                f"ternary/short-circuit secret selection is unsafe: {expr!r}"
            )
    assert "secrets[" not in source, "dynamic secrets[...] indexing is unsafe"
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
    #
    # 之前版本只 grep 字面量 "feedling-redis:${{ github.sha }}"：换成
    # "feedling-redis-backup:${{ github.sha }}"（多了 -backup，子串就不同了）
    # 或者把 github.sha 塞进「Pin image shas into compose」步骤任一条
    # sed 表达式里，都不会撞上那条字符串，却是同一个缺陷。这个文件对
    # github.sha 这个 GHA 表达式没有任何合法用途（唯一合法引用见上面
    # Resolve 步骤里的注释，那里刻意没有写成 ${{ }} 语法，见该注释本身
    # 的说明）——所以直接钉住整份 workflow 里都不出现这个表达式，
    # 不管它出现在 tag、sed 还是别的什么地方。
    source = REDIS_DEPLOY_WF.read_text()
    assert "git rev-parse HEAD" in source
    assert not re.search(r"\$\{\{\s*github\.sha\s*\}\}", source), (
        "github.sha GHA expression must never be used for the image tag or "
        "the compose-pinning sed — use steps.img.outputs.sha instead"
    )


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


def test_deploy_workflow_validates_environment_input_before_anything_else():
    # Finding 3：ref 选择（== 'prod' && ...）和 typo guard（= "prod"）都是
    # 大小写敏感的精确比较；API dispatch 能把 environment 设成 "Prod" 这种
    # `type: choice` UI 之外的值，届时会静默 checkout test 分支、又只需要
    # 输入较弱的 DEPLOY-REDIS 确认串——两个问题都被「cvm-id 文件不存在」
    # 这个附带的下游 gate 兜住了，但那不是这几行代码本身的属性。这里钉住
    # job 最前面就有一个显式、fail-closed 的白名单校验步骤。
    wf = _deploy_workflow()
    steps = wf["jobs"]["deploy"]["steps"]
    assert steps, "deploy job has no steps"
    first_step = steps[0]
    assert first_step["name"] == "Validate environment input", (
        "environment must be validated against the exact allowed set as the "
        "very first step of the job, before ref selection or the typo guard"
    )
    run = first_step["run"]
    assert re.search(r"case\s+\"\$ENVIRONMENT\"\s+in", run)
    assert "test|pre|prod" in run
    assert "exit 1" in run


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

    # 上面这几行全在 newest() 函数体内部，与调用点是否真的检查了
    # newest() 的返回值完全无关——一个把 `TS=$(newest "$PREFIX")` 的
    # 结果不做任何判断就直接拿去比较 EMPTY 的调用点，能让这个测试
    # 全绿，却仍然带着 Finding 2 的缺陷（R2 查询失败被误报成「一份
    # 备份都没有，不可恢复」）。这里改成结构性检查调用点本身：
    #
    # 1) 调用点必须自己接 newest() 的退出码，手法与 newest() 内部接
    #    aws 退出码一样（rc=0 || rc=$?），否则命令替换的失败会被
    #    悄悄吞掉。
    call_site = re.search(
        r'TS=\$\(newest "\$PREFIX"\)\s*&&\s*(\w+)=0\s*\|\|\s*\1=\$\?',
        source,
    )
    assert call_site, (
        "call site must capture newest()'s own exit status the same way "
        "newest() captures aws's (e.g. `TS=$(newest \"$PREFIX\") && RC=0 || "
        "RC=$?`) — otherwise a failed R2 query and a genuinely empty prefix "
        "are indistinguishable at the point that matters"
    )
    rc_var = call_site.group(1)

    # 2) 那个退出码必须在「$TS 是否为空」判断之前就被检查并 fail-closed，
    #    且报错措辞不能沿用 EMPTY 分支的「不可恢复」——那是两种不同的
    #    故障，混在一起就是 Finding 2 本身。
    tail = source[call_site.end():]
    empty_check_pos = tail.index('[ -z "$TS" ]')
    before_empty_check = tail[:empty_check_pos]
    assert re.search(rf'"\${rc_var}"\s*-ne\s*0', before_empty_check), (
        "the call site must branch on the captured exit code before it "
        "ever looks at whether $TS is empty"
    )
    assert "exit 1" in before_empty_check
    assert "不可恢复" not in before_empty_check, (
        "the R2-query-failed branch must not borrow the EMPTY branch's "
        "'不可恢复' wording — an unreachable R2 is not proof there are no "
        "backups"
    )


def test_monitor_never_selects_secrets_with_a_ternary():
    # 与 test_deploy_workflow_never_selects_secrets_with_a_ternary 保护的
    # 是同一类 bug，同一个 sibling task 的另一个文件。redis-monitor.yml
    # 原先六处都用
    #   ${{ matrix.env_name == 'prod' && secrets.PROD_X || secrets.TEST_X }}
    # 选机密。PROD_X 恰好为空（轮换空窗/手误删）时会短路 fallback 到右边
    # 那份，且不会报错——prod 那条腿会打着 prod 的名义，实际检查 pre 的
    # Redis、test 的 R2 桶，这个 workflow 存在的唯一目的（盯住 prod）反而
    # 悄悄失效。
    #
    # 结构性检查，不是字面量子串匹配：把每个 ${{ ... }} 表达式整体取出
    # （DOTALL 跨行，能扛住不寻常的换行/空格写法），只要表达式里同时出现
    # "secrets" 和 &&/|| 就判定为短路选择机密；另外单独禁止 GHA 的动态
    # 下标写法 secrets[format(...)]，那是达到同样短路效果的另一种语法。
    source = REDIS_MONITOR_WF.read_text()
    for expr in re.findall(r"\$\{\{(.*?)\}\}", source, re.DOTALL):
        if "secrets" in expr:
            assert "&&" not in expr and "||" not in expr, (
                f"ternary/short-circuit secret selection is unsafe: {expr!r}"
            )
    assert "secrets[" not in source, "dynamic secrets[...] indexing is unsafe"
    # 两套机密都以前缀区分的名字注入 job env（R2 那份用 PROD_/TEST_ 前缀，
    # 因为 pre 与 test 共享同一个 R2 bucket；Redis 那份用 PROD_/PRE_ 前缀，
    # 因为 prod 与 pre 是各自独立的 Redis 实例）。
    assert "PROD_R2_ACCESS_KEY_ID" in source and "TEST_R2_ACCESS_KEY_ID" in source
    assert "PROD_REDIS_HOST" in source and "PRE_REDIS_HOST" in source


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
