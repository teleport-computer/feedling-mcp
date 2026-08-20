from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "postgres" / "restore-start-and-wait.sh"
PREPARE_SCRIPT = ROOT / "deploy" / "postgres" / "restore.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run_wait_script(
    tmp_path: Path,
    states: list[str],
    *,
    timeout: int,
    poll_interval: int = 1,
    discover_with_pg_config: bool = False,
    times: list[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pg_bin = tmp_path / "pg-bin" if discover_with_pg_config else fake_bin
    pg_bin.mkdir(exist_ok=True)
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    state_file = tmp_path / "recovery-states"
    state_file.write_text("\n".join(states) + "\n")

    _write_executable(
        pg_bin / "pg_ctl",
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RESTORE_TEST_PG_CTL_ARGS\"\n",
    )
    _write_executable(
        pg_bin / "psql",
        """#!/bin/sh
set -eu
state="$(head -n 1 "$RESTORE_TEST_STATE_FILE")"
tail -n +2 "$RESTORE_TEST_STATE_FILE" > "$RESTORE_TEST_STATE_FILE.next"
mv "$RESTORE_TEST_STATE_FILE.next" "$RESTORE_TEST_STATE_FILE"
printf '%s\n' "$state"
""",
    )
    _write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    if times is not None:
        time_file = tmp_path / "times"
        time_file.write_text("\n".join(str(value) for value in times) + "\n")
        _write_executable(
            fake_bin / "date",
            """#!/bin/sh
set -eu
value="$(head -n 1 "$RESTORE_TEST_TIME_FILE")"
tail -n +2 "$RESTORE_TEST_TIME_FILE" > "$RESTORE_TEST_TIME_FILE.next"
mv "$RESTORE_TEST_TIME_FILE.next" "$RESTORE_TEST_TIME_FILE"
printf '%s\n' "$value"
""",
        )
    if discover_with_pg_config:
        _write_executable(
            fake_bin / "pg_config",
            f"#!/bin/sh\n[ \"$1\" = --bindir ]\nprintf '%s\\n' '{pg_bin}'\n",
        )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PGDATA": str(pgdata),
            "RESTORE_OWNER_USER": "feedling_owner",
            "RESTORE_DATABASE": "feedling",
            "RESTORE_SOCKET_DIR": str(tmp_path / "socket"),
            "RESTORE_LOG_PATH": str(tmp_path / "postgres.log"),
            "RESTORE_RECOVERY_TIMEOUT_SEC": str(timeout),
            "RESTORE_POLL_INTERVAL_SEC": str(poll_interval),
            "RESTORE_TEST_STATE_FILE": str(state_file),
            "RESTORE_TEST_PG_CTL_ARGS": str(tmp_path / "pg-ctl-args"),
        }
    )
    if times is not None:
        env["RESTORE_TEST_TIME_FILE"] = str(tmp_path / "times")
    if discover_with_pg_config:
        env.pop("PG_BIN_DIR", None)
    else:
        env["PG_BIN_DIR"] = str(pg_bin)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_restore_wait_does_not_finish_while_postgres_is_still_in_recovery(tmp_path: Path):
    result = _run_wait_script(tmp_path, ["t", "t", "f"], timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTORE_RECOVERY_COMPLETE" in result.stdout
    assert (tmp_path / "recovery-states").read_text() == ""
    assert "-t\n30\n" in (tmp_path / "pg-ctl-args").read_text()


def test_restore_wait_rejects_zero_timeout(tmp_path: Path):
    result = _run_wait_script(tmp_path, ["t"], timeout=0)

    assert result.returncode == 2
    assert "RESTORE_RECOVERY_TIMEOUT_SEC must be a positive integer" in result.stderr


def test_restore_wait_fails_closed_when_recovery_exceeds_timeout(tmp_path: Path):
    result = _run_wait_script(tmp_path, ["t"], timeout=1, times=[100, 101])

    assert result.returncode == 1
    assert "RESTORE_RECOVERY_TIMEOUT elapsed_seconds=1" in result.stderr
    assert "RESTORE_RECOVERY_COMPLETE" not in result.stdout


def test_restore_wait_rejects_zero_poll_interval(tmp_path: Path):
    result = _run_wait_script(tmp_path, ["f"], timeout=30, poll_interval=0)

    assert result.returncode == 2
    assert "RESTORE_POLL_INTERVAL_SEC must be a positive integer" in result.stderr


def test_restore_wait_discovers_postgres_bin_with_pg_config(tmp_path: Path):
    result = _run_wait_script(
        tmp_path,
        ["f"],
        timeout=30,
        discover_with_pg_config=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTORE_RECOVERY_COMPLETE" in result.stdout


def test_restore_prepare_discovers_postgres_bin_with_pg_config(tmp_path: Path):
    command_bin = tmp_path / "command-bin"
    command_bin.mkdir()
    pg_bin = tmp_path / "pg-bin"
    pg_bin.mkdir()
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()

    _write_executable(
        command_bin / "pg_config",
        f"#!/bin/sh\n[ \"$1\" = --bindir ]\nprintf '%s\\n' '{pg_bin}'\n",
    )
    _write_executable(command_bin / "wal-g", "#!/bin/sh\nexit 0\n")
    _write_executable(command_bin / "pg_isready", "#!/bin/sh\nexit 1\n")
    _write_executable(
        pg_bin / "pg_controldata",
        """#!/bin/sh
cat <<'EOF'
max_connections setting: 400
max_worker_processes setting: 8
max_wal_senders setting: 10
max_prepared_xacts setting: 0
max_locks_per_xact setting: 64
EOF
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{command_bin}:/usr/bin:/bin",
            "PGDATA": str(pgdata),
            "WALG_LIBSODIUM_KEY": "0" * 64,
            "WALG_S3_PREFIX": "s3://restore-test",
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(PREPARE_SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (pgdata / "recovery.signal").exists()
    assert "max_connections = 400" in (pgdata / "postgresql.conf").read_text()
