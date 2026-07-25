"""backup-push.sh 的行为测试。

策略：把 redis-cli / age / aws 换成记录调用的 stub 放进 PATH 最前面，
这样能在毫秒级验证脚本的编排逻辑（顺序、参数、fail-closed），
不需要真起 Redis 或连 R2。真实的端到端在 Task 10 用 docker + MinIO 跑。
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
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
        # `s3 cp <src> <dst>`: when <src> is an s3:// URI this is a
        # download — materialize <dst> so callers (restore.sh) can't
        # silently proceed against a file that was never written. When
        # <src> is a local path (backup-push.sh's upload direction) there
        # is nothing to create locally, so leave it alone.
        f'if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then\n'
        f'  case "$3" in\n'
        f'    s3://*) printf "s3-download-stub" > "$4" ;;\n'
        f'  esac\n'
        f'fi\n'
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
        "AWS_ACCESS_KEY_ID": "test-access-key-id",
        "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
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
            "AWS_ACCESS_KEY_ID": "test-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
            "REDISCLI_AUTH": "secret",
            "REDIS_SOCKET": "/tmp/fake.sock",
            "BACKUP_TMPDIR": str(tmp_path / "work"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "s3 cp" not in calls


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
    # 日期相对「现在」生成而非写死日历日期：保留窗口的语义本就是相对
    # 当前时间的（脚本用真实 wall clock 算 cutoff），写死日期迟早会漂出
    # 7 天窗口而让测试假性失败，那是测试数据的问题，不是脚本的行为回归。
    now = datetime.now(timezone.utc)
    daily_days = [now - timedelta(days=d) for d in (6, 5, 4, 3)]
    recent_day = now - timedelta(days=2)
    keys = [f"test/redis/redis-{d.strftime('%Y%m%d')}T030000Z.rdb.age" for d in daily_days]
    keys += [
        f"test/redis/redis-{recent_day.strftime('%Y%m%d')}T{h:02d}0000Z.rdb.age"
        for h in range(0, 24)
    ]
    result, calls = _run_backup(tmp_path, aws_ls_output=_ls_output(keys))
    assert result.returncode == 0, result.stderr

    deleted_keys = " ".join(l for l in calls.splitlines() if "rm " in l and "s3" in l)
    for d in daily_days:
        assert f"{d.strftime('%Y%m%d')}T030000Z" not in deleted_keys, "daily snapshot must survive"


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
            "AWS_ACCESS_KEY_ID": "test-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
            "REDISCLI_AUTH": "secret",
            "REDIS_SOCKET": "/tmp/fake.sock",
            "BACKUP_TMPDIR": str(tmp_path / "work"),
        },
        text=True,
        capture_output=True,
    )
    # Without this assertion the test can't tell "the never-delete-blind
    # guard ran and exited 0" apart from "the script died early under
    # set -eu at the failed LISTING= assignment" — both leave `calls`
    # free of any `s3 rm`. Pinning the exit code to 0 (the guard's
    # explicit `exit 0`) makes the test fail if the guard is ever removed,
    # since set -eu would then abort with the stubbed aws failure's
    # non-zero exit code instead.
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert "s3 rm" not in calls and "rm --recursive" not in calls


def test_loop_does_not_use_cron():
    # PG 那边 2026-07-14 的事故：cron 以精简 PATH 执行，找不到
    # /usr/local/bin/wal-g，每日备份静默失败很久，只剩建库时那一份 base。
    source = BACKUP_LOOP.read_text()
    # 剔除注释行再检查：脚本头注释解释*为什么*不用 cron，那个说明必须
    # 可被搜索到。这里检查的是可执行代码不依赖 cron 调度。
    lines = [line for line in source.split('\n') if not line.lstrip().startswith('#')]
    executable_code = '\n'.join(lines)
    assert "cron" not in executable_code.lower()
    assert "sleep" in executable_code


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
            "AWS_ACCESS_KEY_ID": "test-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
            "REDISCLI_AUTH": "secret",
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


def test_loop_fatals_when_the_boot_push_fails(tmp_path):
    # boot 兜底推送失败必须让整个循环 fatal 退出（非零），而不是像
    # 定时轮次那样 `|| echo` 吞掉——此时必需机密都已校验通过、redis 也已
    # healthy，失败说明的是真实的运行时故障。容器配的是
    # `restart: unless-stopped`：暂时性抖动会被立刻重启重试，比静默等一
    # 整个 BACKUP_INTERVAL_SEC 更快；持续性故障则表现为可见的重启循环，
    # 而不是一个看起来健康、实际上从没备份成功过的容器。
    bin_dir = _make_stubs(tmp_path, aws_ls_output="")
    push_stub = tmp_path / "failing-push.sh"
    push_stub.write_text("#!/bin/sh\necho 'push stub: forced failure' >&2\nexit 1\n")
    push_stub.chmod(0o755)
    result = subprocess.run(
        ["sh", str(BACKUP_LOOP)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "REDIS_BACKUP_S3_PREFIX": "test/redis/",
            "REDIS_BACKUP_AGE_RECIPIENT": "age1" + "q" * 58,
            "REDIS_BACKUP_BUCKET": "io-in-enclave-db",
            "AWS_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
            "AWS_ACCESS_KEY_ID": "test-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
            "REDISCLI_AUTH": "secret",
            "REDIS_SOCKET": "/tmp/fake.sock",
            "BACKUP_TMPDIR": str(tmp_path / "work"),
            "BACKUP_INTERVAL_SEC": "0",
            "BACKUP_PUSH_BIN": str(push_stub),
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "initial push failed" in result.stderr


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
    bin_dir = _make_stubs(tmp_path, aws_ls_output=_ls_output(keys))
    # age -d -i <identity> -o <out> <in>. The last positional arg is the
    # input (encrypted) file; fail if it doesn't exist so a script bug
    # that passes `age -d` a different filename than what `aws s3 cp`
    # actually downloaded is caught here instead of silently producing
    # dump.rdb from nothing.
    (bin_dir / "age").write_text(
        f'#!/bin/sh\n'
        f'echo "age $*" >> {tmp_path / "calls.log"}\n'
        f'prev=""\n'
        f'input=""\n'
        f'out=""\n'
        f'for a in "$@"; do\n'
        f'  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
        f'  input="$a"\n'
        f'  prev="$a"\n'
        f'done\n'
        f'if [ ! -f "$input" ]; then\n'
        f'  echo "age stub: input file not found: $input" >&2\n'
        f'  exit 1\n'
        f'fi\n'
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
            "AWS_ACCESS_KEY_ID": "test-access-key-id",
            "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
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
