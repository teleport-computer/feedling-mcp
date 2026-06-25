"""Pure-unit tests for backend/agent_runtime/spawners.py.

Covers the process spawner's env shaping and the (opt-in) container spawner's
docker argv. The live process/docker spawn is integration. Pure-unit (no PG).
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent_runtime import spawners


def test_consumer_env_drives_resident_in_cli_mode_for_claude():
    env = spawners.consumer_env(
        {"PATH": "/bin", "FEEDLING_API_URL": "http://b:5001"},
        {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_1", home="/agent-data/users/u_1",
    )
    assert env["FEEDLING_API_KEY"] == "fk"
    assert env["AGENT_MODE"] == "cli"
    assert "claude" in env["AGENT_CLI_CMD"]          # default claude cli template
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"
    # per-user isolation paths under the user's home
    assert env["CHECKPOINT_FILE"] == "/agent-data/users/u_1/checkpoint.json"
    assert env["AGENT_SESSION_FILE"] == "/agent-data/users/u_1/agent-session.txt"
    assert env["CLAUDE_CONFIG_DIR"] == "/agent-data/users/u_1/claude-home"
    assert env["CONSUMER_ID"] == "agent-runner:u_1"
    assert env["PATH"] == "/bin" and env["FEEDLING_API_URL"] == "http://b:5001"  # base preserved


def test_consumer_env_uses_codex_cli_and_home_for_codex_driver():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex"},
        user_id="u_1", home="/h",
    )
    assert "codex" in env["AGENT_CLI_CMD"]
    assert env["CODEX_API_KEY"] == "sk-oai"
    assert env["CODEX_HOME"] == "/h/codex-home"
    assert "ANTHROPIC_API_KEY" not in env


def test_consumer_env_honors_custom_cli_cmd():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "cli_cmd": "claude --resume -p {message}"},
        user_id="u", home="/h",
    )
    assert env["AGENT_CLI_CMD"] == "claude --resume -p {message}"


def test_build_container_argv_isolates_per_user():
    argv = spawners.build_container_argv(
        {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_1", home="/agent-data/users/u_1",
        image="ghcr.io/x/feedling-agent-runner:dev",
    )
    assert argv[:3] == ["docker", "run", "-d"]
    # one container + one volume per user (no shared home)
    assert "--name" in argv and "feedling-agent-u_1" in argv
    assert any(a.startswith("feedling-agent-vol-u_1:") for a in argv)
    # secrets passed by env reference, not baked as plaintext args
    assert "ANTHROPIC_API_KEY" in argv
    assert "sk-ant" not in argv
    # image present, with the command following it (docker run [opts] IMAGE [cmd])
    img = "ghcr.io/x/feedling-agent-runner:dev"
    assert img in argv
    assert argv.index(img) < argv.index("python")


def test_process_spawner_reaps_exited_child_not_zombie():
    # A child that exits must report not-alive (and be reaped, not a zombie).
    # os.kill(pid, 0) would wrongly say a zombie is alive; poll() reaps it.
    sp = spawners.ProcessSpawner()
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    pid = sp.register(proc)
    proc.wait()                       # ensure it has exited
    assert sp.is_alive(pid) is False
    assert proc.returncode is not None  # reaped — returncode is set


def test_process_spawner_reports_running_child_then_kills_it():
    sp = spawners.ProcessSpawner()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pid = sp.register(proc)
    assert sp.is_alive(pid) is True
    sp.kill(pid)
    assert sp.is_alive(pid) is False


def test_get_spawner_returns_spawn_alive_kill_triple_sharing_state():
    spawn, alive, kill = spawners.get_spawner("process")
    # all three bound to the same registry instance
    assert callable(spawn) and callable(alive) and callable(kill)
    assert alive.__self__ is kill.__self__


def test_build_container_argv_runs_resident_consumer_not_supervisor():
    argv = spawners.build_container_argv(
        {"api_key": "fk"}, user_id="u_2", home="/h", image="img",
    )
    # the per-user container runs the single-user resident consumer, not a supervisor
    joined = " ".join(argv)
    assert "chat_resident_consumer.py" in joined


# ---- A1-lite: hosted agent gets Feedling perception tools (skill + Bash) ----


def test_default_claude_cmd_grants_perception_tool_and_loads_prompt():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u", home="/agent-data/users/u",
    )
    cmd = env["AGENT_CLI_CMD"]
    # the io_cli perception verbs are pre-granted so `claude -p` can run them
    # unattended (no interactive permission prompt), scoped to that one CLI.
    assert "--allowed-tools" in cmd
    assert "io_cli.py perception" in cmd
    assert "io_cli.py perception-trend" in cmd
    # the perception how-to is appended as a system prompt from the per-user home
    assert "--append-system-prompt-file /agent-data/users/u/agent-tools-prompt.md" in cmd
    # the resident still substitutes the message
    assert cmd.endswith("-p {message}")


def test_custom_cli_cmd_opts_out_of_default_grant():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "cli_cmd": "claude -p {message}"},
        user_id="u", home="/h",
    )
    # operator-supplied cli_cmd is taken verbatim — they own the tool grant.
    assert env["AGENT_CLI_CMD"] == "claude -p {message}"


def test_agent_home_files_seeds_prompt_and_claude_permission_allow():
    files = spawners.agent_home_files("/agent-data/users/u", driver="claude")
    # the perception how-to lands in the per-user home (matches --append-system-prompt-file)
    prompt_path = "/agent-data/users/u/agent-tools-prompt.md"
    assert prompt_path in files
    assert "perception" in files[prompt_path]
    # claude settings.json (under CLAUDE_CONFIG_DIR) pre-allows the io_cli command
    settings_path = "/agent-data/users/u/claude-home/settings.json"
    assert settings_path in files
    settings = json.loads(files[settings_path])
    allow = settings["permissions"]["allow"]
    assert any("io_cli.py perception" in rule for rule in allow)


def test_agent_home_files_codex_seeds_agents_md():
    files = spawners.agent_home_files("/h", driver="codex")
    # codex reads AGENTS.md; the same how-to is seeded into its home
    assert "/h/codex-home/AGENTS.md" in files
    assert "perception" in files["/h/codex-home/AGENTS.md"]
    # no claude settings.json for a codex user
    assert not any(p.endswith("claude-home/settings.json") for p in files)


# ---- Stage D slice 3a: runtime-token file delivery ----


def test_consumer_env_points_at_runtime_token_file():
    env = spawners.consumer_env({}, {"api_key": "fk"}, user_id="u", home="/agent-data/users/u")
    # the consumer reads its short-lived token from this file (refreshed by the
    # supervisor); empty/absent file → it falls back to the api key.
    assert env["FEEDLING_RUNTIME_TOKEN_FILE"] == "/agent-data/users/u/runtime-token"


def test_write_runtime_token_writes_file(tmp_path):
    home = str(tmp_path / "home")
    Path(home).mkdir()
    spawners.write_runtime_token(home, "tok.sig")
    assert (tmp_path / "home" / "runtime-token").read_text() == "tok.sig"


def test_write_runtime_token_creates_home_if_missing(tmp_path):
    home = str(tmp_path / "nope")
    spawners.write_runtime_token(home, "tok2")
    assert (tmp_path / "nope" / "runtime-token").read_text() == "tok2"
