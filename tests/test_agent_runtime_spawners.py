"""Pure-unit tests for backend/agent_runtime/spawners.py.

Covers the process spawner's env shaping and the (opt-in) container spawner's
docker argv. The live process/docker spawn is integration. Pure-unit (no PG).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert env["AGENT_SESSION_MAX_TURNS"] == "24"   # host rotates sooner than VPS default (40)
    assert env["CLAUDE_CONFIG_DIR"] == "/agent-data/users/u_1/claude-home"
    assert env["FEEDLING_HOME"] == "/agent-data/users/u_1"
    assert env["CONSUMER_ID"] == "agent-runner:u_1"
    assert env["PATH"] == "/bin" and env["FEEDLING_API_URL"] == "http://b:5001"  # base preserved


def test_consumer_env_carries_no_resident_genesis_distill_opt_out():
    # The resident genesis-distill lane has NO opt-out any more: a consumer that
    # skips it makes the user's import spin forever (backend leaves the job
    # `awaiting_resident` with no timeout, no error, no log). Hosted spawns are
    # kept off the lane by the BACKEND's routing instead — `awaiting_resident`
    # jobs only exist for sealed uploads, and cloud uploads are plaintext — so
    # the spawner must not reintroduce an env kill-switch.
    env = spawners.consumer_env(
        {"PATH": "/bin"},
        {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_1", home="/agent-data/users/u_1",
    )
    assert "FEEDLING_GENESIS_RESIDENT_ENABLED" not in env
    # ...and the container strategy must not smuggle one in via the allowlist.
    assert "FEEDLING_GENESIS_RESIDENT_ENABLED" not in spawners._CONSUMER_ENV_KEYS


def test_consumer_env_sets_tz_china_default_when_user_timezone_unknown():
    # Hosted agent process tree must not inherit the CVM's UTC clock: an unknown
    # user tz falls back to the China default so CN users don't perceive time 8h
    # off. (u_no_tz is unregistered -> _get_user_timezone None -> default.)
    env = spawners.consumer_env(
        {"PATH": "/bin", "FEEDLING_API_URL": "http://b:5001"},
        {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_no_tz", home="/agent-data/users/u_no_tz",
    )
    assert env["TZ"] == "Asia/Shanghai"


def test_consumer_env_sets_tz_from_user_first_class_timezone(monkeypatch):
    from accounts import registry
    monkeypatch.setattr(registry, "_get_user_timezone", lambda uid: "America/New_York")
    env = spawners.consumer_env(
        {"PATH": "/bin", "FEEDLING_API_URL": "http://b:5001"},
        {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_ny", home="/agent-data/users/u_ny",
    )
    assert env["TZ"] == "America/New_York"


def test_consumer_env_uses_stream_json_for_native_anthropic_sonnet_thinking():
    env = spawners.consumer_env(
        {"PATH": "/bin"},
        {
            "api_key": "fk",
            "provider": "anthropic",
            "provider_key": "sk-ant",
            "driver": "claude",
            "model": "claude-sonnet-4-5",
        },
        user_id="u_1",
        home="/agent-data/users/u_1",
    )

    cmd = env["AGENT_CLI_CMD"]
    assert "--output-format stream-json" in cmd
    assert "--include-partial-messages" in cmd
    assert "--effort high" in cmd
    assert "--permission-mode acceptEdits" in cmd  # non-interactive image Read
    # thinking-claude must grant Read on the image dir too (sonnet-4-5 is a thinking
    # model → this branch → otherwise chat images are invisible: Read denied).
    # Double-slash = filesystem-absolute; --add-dir trusts the out-of-cwd dir.
    assert "Read(//agent-data/users/u_1/images/**)" in cmd
    assert "--add-dir /agent-data/users/u_1/images" in cmd
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5"


def test_consumer_env_uses_stream_json_for_deepseek_claude_thinking():
    env = spawners.consumer_env(
        {"PATH": "/bin"},
        {
            "api_key": "fk",
            "provider": "deepseek",
            "provider_key": "sk-ds",
            "driver": "claude",
            "model": "deepseek-v4-pro",
        },
        user_id="u_1",
        home="/agent-data/users/u_1",
    )

    cmd = env["AGENT_CLI_CMD"]
    assert "--output-format stream-json" in cmd
    assert "--include-partial-messages" in cmd
    assert "--effort high" in cmd
    assert "--permission-mode acceptEdits" in cmd  # non-interactive image Read
    # the thinking-claude command must ALSO grant Read on the image temp dir, or a
    # thinking model (deepseek/sonnet-4) can't open chat images (Read denied under -p).
    # DOUBLE leading slash: a single slash anchors at the settings source (cwd /app),
    # so Read(/agent-data/...) resolves to /app/agent-data/... and never matches.
    assert "Read(//agent-data/users/u_1/images/**)" in cmd
    # --add-dir puts the out-of-cwd image dir inside claude's trusted workspace, so
    # the Read is permitted even under the headless workspace-trust boundary.
    assert "--add-dir /agent-data/users/u_1/images" in cmd
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"


def test_consumer_env_uses_codex_cli_and_home_for_codex_driver():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex"},
        user_id="u_1", home="/h",
    )
    assert "codex" in env["AGENT_CLI_CMD"]
    assert env["CODEX_API_KEY"] == "sk-oai"
    assert env["CODEX_HOME"] == "/h/codex-home"
    assert "ANTHROPIC_API_KEY" not in env


def test_consumer_env_host_session_cap_default_and_override():
    # Host (agent-runner) sessions rotate at 24 turns (vs the shared consumer
    # default 40) to tighten the in-session voice-drift window. Host-only:
    # VPS consumers don't go through consumer_env. Operator env (base_env) wins.
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_1", home="/h",
    )
    assert env["AGENT_SESSION_MAX_TURNS"] == "24"
    env_override = spawners.consumer_env(
        {"AGENT_SESSION_MAX_TURNS": "12"}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_1", home="/h",
    )
    assert env_override["AGENT_SESSION_MAX_TURNS"] == "12"


def test_default_codex_cmd_skips_git_repo_check():
    # The hosted consumer runs codex with cwd = the user's home (NOT a git repo).
    # Without --skip-git-repo-check, `codex exec` refuses to run ("Not inside a
    # trusted directory…") and exits 1 BEFORE any model call — so the default
    # template MUST pass it or every hosted codex turn dead-ends.
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex"},
        user_id="u_1", home="/h",
    )
    assert "--skip-git-repo-check" in env["AGENT_CLI_CMD"]


def test_default_codex_cmd_bypasses_bwrap_sandbox():
    # codex's Linux sandbox (read-only / workspace-write) wraps commands in
    # bubblewrap, which needs unprivileged user namespaces — DISABLED in the
    # dstack/TDX CVM kernel, so bwrap fails and every io_cli read the agent makes
    # fails to launch. The CVM is already the isolation boundary, so the template
    # MUST bypass codex's own sandbox or every hosted codex memory read breaks.
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex"},
        user_id="u_1", home="/h",
    )
    cmd = env["AGENT_CLI_CMD"]
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    # the bwrap-requiring workspace-write sandbox must NOT be used in-CVM
    assert "--sandbox workspace-write" not in cmd


def test_default_codex_cmd_requests_reasoning_summary_events():
    # Codex only surfaces reasoning to the resident consumer if the CLI is asked
    # to run with reasoning enabled. The consumer already parses agent_reasoning
    # / reasoning events into the thinking disclosure. OpenAI native summaries
    # are best-effort; detailed improves the hit rate but does not guarantee one.
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex"},
        user_id="u_1", home="/h",
    )
    cmd = env["AGENT_CLI_CMD"]
    assert "-c model_reasoning_effort=medium" in cmd
    assert "-c model_reasoning_summary=detailed" in cmd


# ---- V1 web capability: block the drivers' NATIVE web tools so the model is
# forced through our io_cli web-search/web-fetch verbs (server-side authorized +
# kill-switchable), never the driver's own ungated tool. ----


def test_claude_default_cmd_disallows_native_web_tools():
    # claude ships built-in WebSearch/WebFetch. They are NOT allow-listed, so the
    # model's preferred WebSearch is denied ("requires approval") and it never
    # falls back to our io_cli web-search verb → user sees "联网被权限拦截".
    # --disallowed-tools hard-denies both (deny > allow); WebFetch too, or a model
    # blocked from search could still fetch arbitrary URLs, bypassing our gate.
    cmd = spawners._default_cli_cmd("claude", "/h")
    assert "--disallowed-tools WebSearch,WebFetch" in cmd


def test_claude_thinking_cmd_disallows_native_web_tools():
    # Same block on the thinking (stream-json) claude command — the deepseek /
    # sonnet-4 / opus-4 path routes here and must not leak the native web tools.
    cmd = spawners._default_thinking_claude_cmd("/h")
    assert "--disallowed-tools WebSearch,WebFetch" in cmd
    # exercised end-to-end through consumer_env for a thinking model too
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider": "anthropic", "driver": "claude",
             "model": "claude-sonnet-4-5", "provider_key": "sk-ant"},
        user_id="u", home="/h")
    assert "--disallowed-tools WebSearch,WebFetch" in env["AGENT_CLI_CMD"]


def test_claude_settings_deny_web_tools_and_survives_user_mcp_merge():
    # Two-layer defense: the settings.json permissions.deny blocks the native web
    # tools even if an operator cli_cmd override drops the --disallowed-tools flag
    # (claude still reads settings.json; deny outranks allow).
    files = spawners.agent_home_files("/h", driver="claude", provider="anthropic")
    settings = json.loads(files["/h/claude-home/settings.json"])
    assert settings["permissions"]["deny"] == ["WebSearch", "WebFetch"]

    # The consumer merges user-MCP allow rules via merge_settings_allow, which only
    # rewrites permissions.allow — the deny must survive the merge untouched.
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    import user_mcp_materialize

    merged_text = user_mcp_materialize.merge_settings_allow(
        files["/h/claude-home/settings.json"],
        ["mcp__svc__*"], managed_names={"svc"})
    merged = json.loads(merged_text)
    assert merged["permissions"]["deny"] == ["WebSearch", "WebFetch"]  # deny preserved
    assert "mcp__svc__*" in merged["permissions"]["allow"]             # allow merged
    # the io_cli allow rules also survive the merge
    assert any("io_cli.py perception" in r for r in merged["permissions"]["allow"])


def test_codex_default_cmd_disables_native_web_search():
    # codex ships a native Responses web-search tool (web.run). Disable it at the
    # CLI (top-level `-c web_search=disabled` — the only form codex-cli 0.142.4
    # honors) so web only ever flows through io_cli, positioned before {mcp}/{message}.
    cmd = spawners._default_cli_cmd("codex", "/h")
    assert "-c web_search=disabled" in cmd
    assert cmd.index("-c web_search=disabled") < cmd.index("{mcp}")
    assert cmd.index("-c web_search=disabled") < cmd.index("{message}")
    # the legacy/ignored forms are NOT used (they no-op on 0.142.4)
    assert "tools.web_search" not in cmd
    assert "features.web_search" not in cmd


def test_pi_default_cmd_has_no_web_disable_knob():
    # pi (0.80.3) has no native web tool (only read/bash/edit/write/grep/find/ls),
    # so there is nothing to disable — the pi command must NOT grow a fabricated
    # web-disable flag.
    cmd = spawners._default_cli_cmd("pi", "/h", model="m")
    assert "web_search" not in cmd
    assert "--disallowed-tools" not in cmd
    assert "WebSearch" not in cmd and "WebFetch" not in cmd


def test_default_cli_cmds_carry_mcp_placeholder():
    # The resident consumer's `_render_cli_template` (Task 6) replaces `{mcp}`
    # per turn: claude chat turns → `--mcp-config <file>`, codex non-chat turns
    # → `-c mcp_servers={}` (clearing config.toml's [mcp_servers]), and the
    # opposite turn kind → empty string. That only works if the default
    # templates carry the `{mcp}` token in a position a CLI flag can occupy.
    codex = spawners._default_cli_cmd("codex", "/h")
    claude = spawners._default_cli_cmd("claude", "/h")
    thinking = spawners._default_thinking_claude_cmd("/h")
    assert "{mcp}" in codex and codex.index("{mcp}") < codex.index("{message}")
    assert "{mcp}" in claude and claude.index("{mcp}") < claude.index("-p {message}")
    assert "{mcp}" in thinking


def test_consumer_env_tolerates_missing_api_key_for_zero_roster():
    # Stage D host-all: a discovered entry has NO api_key (the consumer auths with
    # the runtime-token file). consumer_env must not KeyError on it.
    env = spawners.consumer_env(
        {}, {"provider_key": "sk-ant", "driver": "claude"},
        user_id="u", home="/agent-data/users/u",
    )
    assert env["FEEDLING_API_KEY"] == ""
    assert env["FEEDLING_RUNTIME_TOKEN_FILE"] == "/agent-data/users/u/runtime-token"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"


def test_consumer_env_isolates_user_mcp_paths_per_user():
    # P0 cross-user leak guard. The materialized user-MCP config (server URL +
    # auth headers) and its CA bundles live at consumer-side paths keyed by
    # sha1(FEEDLING_API_KEY). For Stage-D host-all users FEEDLING_API_KEY is ""
    # so that fingerprint COLLIDES to sha1("") for every user on the host — the
    # /tmp defaults would then be one shared file. consumer_env must pin all
    # three MCP paths under the per-user home (exactly as it already does for
    # CHECKPOINT_FILE) so two host-all agents never read each other's MCP creds.
    env = spawners.consumer_env(
        {}, {"provider_key": "sk-ant", "driver": "claude"},
        user_id="u_a", home="/agent-data/users/u_a",
    )
    assert env["USER_MCP_FILE"] == "/agent-data/users/u_a/user-mcp.json"
    assert env["USER_MCP_CA_FILE"] == "/agent-data/users/u_a/user-mcp-ca.pem"
    assert env["USER_MCP_CASTORE_FILE"] == "/agent-data/users/u_a/user-mcp-castore.pem"

    # Two host-all users (both empty api_key -> colliding fingerprint) must get
    # DISTINCT paths — this is the actual leak the fix closes.
    env_b = spawners.consumer_env(
        {}, {"provider_key": "sk-ant", "driver": "claude"},
        user_id="u_b", home="/agent-data/users/u_b",
    )
    assert env["FEEDLING_API_KEY"] == "" and env_b["FEEDLING_API_KEY"] == ""
    assert env["USER_MCP_FILE"] != env_b["USER_MCP_FILE"]
    assert env["USER_MCP_CA_FILE"] != env_b["USER_MCP_CA_FILE"]
    assert env["USER_MCP_CASTORE_FILE"] != env_b["USER_MCP_CASTORE_FILE"]

    # ...and the container strategy must forward them, or the strong-isolation
    # path would silently drop back to the shared /tmp default.
    for key in ("USER_MCP_FILE", "USER_MCP_CA_FILE", "USER_MCP_CASTORE_FILE"):
        assert key in spawners._CONSUMER_ENV_KEYS


def test_consumer_env_isolates_hermes_openclaw_config_dirs():
    # Same collision shape as the USER_MCP_* /tmp defaults, one layer over: the
    # consumer's hermes/openclaw materializers fall back to Path.home()/.hermes
    # and ~/.openclaw when their env vars are unset — a single shared HOME on the
    # host-all runner. They only write when the directory exists (dormant on
    # hosted today), but the moment anything creates ~/.openclaw every
    # co-resident consumer starts cross-writing plaintext MCP creds there. Pin
    # both dirs under the per-user home like claude-home/codex-home.
    env_a = spawners.consumer_env(
        {}, {"provider_key": "sk-ant", "driver": "claude"},
        user_id="u_a", home="/agent-data/users/u_a",
    )
    env_b = spawners.consumer_env(
        {}, {"provider_key": "sk-ant", "driver": "claude"},
        user_id="u_b", home="/agent-data/users/u_b",
    )
    assert env_a["HERMES_CONFIG_DIR"] == "/agent-data/users/u_a/hermes-home"
    assert env_a["OPENCLAW_CONFIG_DIR"] == "/agent-data/users/u_a/openclaw-home"
    assert env_a["HERMES_CONFIG_DIR"] != env_b["HERMES_CONFIG_DIR"]
    assert env_a["OPENCLAW_CONFIG_DIR"] != env_b["OPENCLAW_CONFIG_DIR"]
    for key in ("HERMES_CONFIG_DIR", "OPENCLAW_CONFIG_DIR"):
        assert key in spawners._CONSUMER_ENV_KEYS


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
    # env passes via a per-user --env-file — secrets NEVER in argv (a bare
    # `-e KEY` would inherit the shared supervisor env → per-user smear; a
    # `-e KEY=value` would expose secrets to `ps`). The env-file gets both right.
    assert "--env-file" in argv
    assert spawners.container_env_file_path("u_1") in argv
    assert "sk-ant" not in argv
    assert not any("ANTHROPIC_API_KEY" in a for a in argv)
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


def test_process_spawner_escalates_to_sigkill_when_sigterm_ignored():
    # A consumer that traps & ignores SIGTERM must still be force-killed, else a
    # stuck child lingers and can double-run alongside its replacement. kill()
    # escalates to SIGKILL after a grace window.
    import time
    sp = spawners.ProcessSpawner()
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code])
    pid = sp.register(proc)
    time.sleep(0.4)                     # let the child install the SIGTERM handler
    assert sp.is_alive(pid) is True
    sp.kill(pid)
    assert sp.is_alive(pid) is False    # SIGKILL took it down despite ignored SIGTERM


def test_signal_kill_escalates_to_sigkill_for_sigterm_ignoring_pid():
    # The no-Popen-handle fallback (used after a supervisor restart / container
    # path) must also escalate SIGTERM → SIGKILL, not give up after one SIGTERM.
    import time
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code])
    time.sleep(0.4)
    spawners._signal_kill(proc.pid)
    proc.wait(timeout=10)               # reaps; returns once SIGKILL lands
    assert proc.returncode is not None


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


# ---- A-full: hosted agent gets Feedling native context tools (skill + Bash) ----


def test_default_claude_cmd_grants_io_cli_tools_and_loads_prompt():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u", home="/agent-data/users/u",
    )
    cmd = env["AGENT_CLI_CMD"]
    # acceptEdits: without a non-interactive permission mode, claude -p denies its
    # own allow-listed Read of the chat image (hallucinates "no permission").
    assert "--permission-mode acceptEdits" in cmd
    # the io_cli verbs are pre-granted so `claude -p` can run them
    # unattended (no interactive permission prompt), scoped to that one CLI.
    assert "--allowed-tools" in cmd
    assert "io_cli.py perception" in cmd
    assert "io_cli.py perception-trend" in cmd
    assert "io_cli.py memory-index" in cmd
    assert "io_cli.py memory-fetch" in cmd
    assert "io_cli.py screen-recent" in cmd
    assert "io_cli.py screen-read" in cmd
    # the context-tool how-to is appended as a system prompt from the per-user home
    assert "--append-system-prompt-file /agent-data/users/u/agent-tools-prompt.md" in cmd
    # the resident still substitutes the message
    assert cmd.endswith("-p {message}")


def test_default_claude_cmd_grants_chat_image():
    # chat-image is documented in agent_tools_prompt.md AND advertised by the
    # consumer's history placeholder (`io_cli chat-image --id <id>`). Without it in
    # the allowlist, claude's --allowed-tools blocks the call in the non-interactive
    # acceptEdits mode ("This command requires approval") — the agent then loops and
    # tells the user "waiting for permission approval" instead of showing the image.
    # (Live regression on usr_6491814…: proactive turn ran chat-image, got denied.)
    for entry in ({"api_key": "fk", "provider_key": "sk-ant"},
                  {"api_key": "fk", "provider_key": "sk-ant", "model": "deepseek-reasoner"}):
        env = spawners.consumer_env({}, entry, user_id="u", home="/agent-data/users/u")
        assert "io_cli.py chat-image" in env["AGENT_CLI_CMD"], entry


def test_default_claude_cmd_grants_image_read():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u", home="/agent-data/users/u",
    )
    cmd = env["AGENT_CLI_CMD"]
    # claude -p must be allowed to Read the decrypted image temp files (IMAGE_TEMP_DIR
    # = {home}/images), or it cannot open the screenshot/photo whose path the resident
    # injects into the prompt — the image would stay invisible to the model.
    # Double leading slash → filesystem-absolute (single slash anchors at cwd /app).
    assert "Read(//agent-data/users/u/images/" in cmd
    # …and the dir is added to claude's trusted workspace (out-of-cwd read boundary).
    assert "--add-dir /agent-data/users/u/images" in cmd


def test_default_claude_cmd_grants_file_read():
    # Chat file uploads (pdf/docx/xlsx/text) are decrypted/extracted to
    # {home}/files and their path is injected into the prompt. Without Read on that
    # dir + --add-dir, claude -p denies the read and the agent reports the file as
    # "0 KB / permission not granted" (same failure class as the image path).
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u", home="/agent-data/users/u",
    )
    cmd = env["AGENT_CLI_CMD"]
    assert "Read(//agent-data/users/u/files/" in cmd
    assert "--add-dir /agent-data/users/u/files" in cmd
    # …and the consumer is told to land files there (matches the grant).
    assert env["FILE_TEMP_DIR"] == "/agent-data/users/u/files"


def test_default_claude_cmd_substitutes_io_cli_path_in_prompt():
    # The system prompt template ships literal `<io_cli>` placeholders. They MUST be
    # substituted with the real io_cli path, or the model can't know where io_cli is
    # and guesses a nonexistent path (observed: /feedling-io-cli/io_cli.py) → every
    # perception/memory/photo Bash call is denied ("requires approval").
    files = spawners.agent_home_files(
        "/agent-data/users/u", driver="claude", provider="anthropic",
        io_cli="/app/tools/io_cli.py",
    )
    prompt = files["/agent-data/users/u/agent-tools-prompt.md"]
    assert "<io_cli>" not in prompt
    assert "python /app/tools/io_cli.py perception" in prompt


def test_custom_cli_cmd_opts_out_of_default_grant():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "cli_cmd": "claude -p {message}"},
        user_id="u", home="/h",
    )
    # operator-supplied cli_cmd is taken verbatim — they own the tool grant.
    assert env["AGENT_CLI_CMD"] == "claude -p {message}"


def test_agent_home_files_seeds_prompt_and_claude_permission_allow():
    # 明确指定为官方 provider，所以不会注入身份块
    files = spawners.agent_home_files("/agent-data/users/u", driver="claude", provider="anthropic")
    # the context-tool how-to lands in the per-user home (matches --append-system-prompt-file)
    prompt_path = "/agent-data/users/u/agent-tools-prompt.md"
    assert prompt_path in files
    assert "perception" in files[prompt_path]
    assert "memory-index" in files[prompt_path]
    assert "memory-fetch" in files[prompt_path]
    assert "screen-recent" in files[prompt_path]
    assert "screen-read" in files[prompt_path]
    assert "Fast:" in files[prompt_path]
    assert "Slow:" in files[prompt_path]
    # claude settings.json (under CLAUDE_CONFIG_DIR) pre-allows the io_cli command
    settings_path = "/agent-data/users/u/claude-home/settings.json"
    assert settings_path in files
    settings = json.loads(files[settings_path])
    # defaultMode is REQUIRED: without it, claude -p in stream-json mode denies the
    # allow-listed Read of the chat image ("I need permission to see the image").
    # acceptEdits makes the pre-granted allowlist honored non-interactively.
    assert settings["permissions"]["defaultMode"] == "acceptEdits"
    allow = settings["permissions"]["allow"]
    assert any("io_cli.py perception" in rule for rule in allow)
    assert any("io_cli.py memory-index" in rule for rule in allow)
    assert any("io_cli.py identity-write" in rule for rule in allow)  # 7.D post-respawn tool + rename
    assert any("io_cli.py send-file" in rule for rule in allow)
    assert any("Write(//agent-data/users/u/outbound-files/**)" == rule for rule in allow)
    # identity-read: the agent could write its own card but not read it, so a rename
    # was a blind write and "你叫什么" had to be guessed. Granting the read closes both.
    assert any("io_cli.py identity-read" in rule for rule in allow)
    assert any("io_cli.py screen-read" in rule for rule in allow)
    # and Read on the decrypted image temp dir, so the CLI can open attached images
    # (double leading slash = filesystem-absolute; single slash anchors at cwd /app)
    assert any(rule.startswith("Read(//agent-data/users/u/images/") for rule in allow)


def test_agent_home_files_codex_seeds_agents_md():
    # 明确指定为官方 provider，所以不会注入身份块
    files = spawners.agent_home_files("/h", driver="codex", provider="openai")
    # codex reads AGENTS.md; the same how-to is seeded into its home
    assert "/h/codex-home/AGENTS.md" in files
    assert "perception" in files["/h/codex-home/AGENTS.md"]
    assert "memory-index" in files["/h/codex-home/AGENTS.md"]
    assert "screen-read" in files["/h/codex-home/AGENTS.md"]
    # no claude settings.json for a codex user
    assert not any(p.endswith("claude-home/settings.json") for p in files)
    # codex now gets a config.toml, but ONLY to disable its native web-search tool
    # (native OpenAI otherwise; no gateway config). web must flow through io_cli.
    assert files["/h/codex-home/config.toml"] == 'web_search = "disabled"\n'


def test_openclaw_feedling_plugin_declares_native_memory_screen_tools_with_costs():
    plugin = Path(__file__).parent.parent / "deploy" / "openclaw-plugins" / "feedling-io-tools" / "index.js"
    text = plugin.read_text()

    assert "name: `perception_${signal}`" in text
    assert "[${costClass}] Read Feedling perception signal" in text
    assert 'name: "memory_index"' in text
    assert "[fast] Read a compact index" in text
    assert 'name: "memory_fetch"' in text
    assert "[slow] Fetch verbatim decrypted memory cards" in text
    assert 'name: "screen_recent"' in text
    assert "[slow] List recent screen frame metadata" in text
    assert 'name: "screen_read"' in text
    assert "[fast caption, slow image] Read the decrypted caption/ocr" in text


def test_consumer_env_claude_deepseek_points_at_anthropic_compat_endpoint():
    # deepseek runs on the claude (Anthropic-wire) driver but is NOT anthropic:
    # the CLI must be pointed at deepseek's /anthropic-compatible endpoint + its
    # own model, else it hits api.anthropic.com with a foreign key → exit 1.
    env = spawners.consumer_env(
        {}, {"driver": "claude", "provider": "deepseek", "model": "deepseek-v4-flash",
             "base_url": "https://api.deepseek.com", "provider_key": "sk-ds"},
        user_id="u_1", home="/h",
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_API_KEY"] == "sk-ds"
    assert env["ANTHROPIC_MODEL"] == "deepseek-v4-flash"
    # claude Code's background "small/fast" calls must use the deepseek model too,
    # not a claude-* default the endpoint doesn't serve.
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "deepseek-v4-flash"


def test_consumer_env_claude_native_anthropic_keeps_default_endpoint():
    # native anthropic must NOT get a base-url override — the CLI default
    # (api.anthropic.com) is correct; only foreign claude-wire providers override.
    env = spawners.consumer_env(
        {}, {"driver": "claude", "provider": "anthropic", "model": "claude-haiku-4-5",
             "provider_key": "sk-ant"},
        user_id="u_1", home="/h",
    )
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"


# ---- codex: native-only (LiteLLM gateway retired) ----


def test_codex_native_for_openai_uses_provider_key_directly():
    # openai is codex's only driven provider now (native OpenAI Responses): the
    # OpenAI key goes straight to CODEX_API_KEY — no gateway indirection left.
    env = spawners.consumer_env(
        {},
        {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex", "provider": "openai"},
        user_id="u", home="/h",
    )
    assert env["CODEX_API_KEY"] == "sk-oai"


def test_agent_home_files_prepends_genesis_persona_when_present():
    # Host genesis persona is prepended to the appended system prompt so the agent
    # boots as itself; the tools how-to stays present (single appended file).
    # 明确指定为官方 provider，所以不会注入身份块
    files = spawners.agent_home_files(
        "/h", driver="claude", provider="anthropic", persona_content="You are Kai. Terse; you ask back.")
    append = files["/h/agent-tools-prompt.md"]
    assert append.startswith("You are Kai. Terse; you ask back.")  # persona first
    assert "memory-index" in append and "perception" in append     # tools still there
    # codex gets the same composed append in AGENTS.md
    cfiles = spawners.agent_home_files("/h", driver="codex", provider="openai", persona_content="You are Kai.")
    assert cfiles["/h/codex-home/AGENTS.md"].startswith("You are Kai.")
    assert "memory-index" in cfiles["/h/codex-home/AGENTS.md"]


def test_persona_from_blob_decrypts_envelope():
    # Persona is stored encrypted; the reader decrypts content_envelope at spawn.
    blob = {"encrypted": True, "content_envelope": {"body_ct": "ct"}}
    assert spawners._persona_from_blob(blob, lambda env: "You are Kai.") == "You are Kai."


def test_persona_from_blob_empty_on_absent_or_undecryptable():
    assert spawners._persona_from_blob(None, lambda env: "x") == ""
    assert spawners._persona_from_blob({}, lambda env: "x") == ""                        # no envelope
    assert spawners._persona_from_blob({"content_envelope": {}}, lambda env: "x") == ""  # no body_ct
    # decrypt failure (enclave down / token-only auth) degrades to tools-only

    def _boom(env):
        raise RuntimeError("enclave down")
    assert spawners._persona_from_blob({"content_envelope": {"body_ct": "ct"}}, _boom) == ""


def test_agent_home_files_no_persona_is_tools_only():
    # Fresh start / no genesis / VPS → today's behaviour: tools-only, no persona prefix.
    # 明确指定为官方 provider，所以不会注入身份块
    append = spawners.agent_home_files("/h", driver="claude", provider="anthropic")["/h/agent-tools-prompt.md"]
    assert not append.startswith("You are")
    assert "perception" in append


def test_agent_home_files_blank_persona_is_tools_only():
    # Whitespace-only persona must not inject an empty prefix.
    # 明确指定为官方 provider，所以不会注入身份块
    append = spawners.agent_home_files(
        "/h", driver="claude", provider="anthropic", persona_content="   \n  ")["/h/agent-tools-prompt.md"]
    assert append.startswith("# Feedling context tools")  # tools how-to header, no prefix


def test_agent_home_files_codex_seeds_web_search_disabled_config_toml():
    # LiteLLM gateway retired: codex talks straight to OpenAI. The ONLY thing its
    # config.toml carries is the top-level `web_search = "disabled"` key, so codex's
    # native Responses web-search tool (web.run) stays off and web only ever flows
    # through our io_cli web-search verb (server-side authorized + kill-switchable).
    files = spawners.agent_home_files("/h", driver="codex", provider="openai")
    assert files["/h/codex-home/config.toml"] == 'web_search = "disabled"\n'
    # NOT the old dead LiteLLM gateway config — only the web toggle.
    assert "feedling_gateway" not in files["/h/codex-home/config.toml"]
    assert "base_url" not in files["/h/codex-home/config.toml"]


def test_codex_web_search_disabled_survives_user_mcp_merge():
    # The consumer merges user MCP servers into config.toml via
    # user_mcp_materialize.codex_config_merged at runtime. That merge prepends the
    # managed [mcp_servers.*] block AFTER the existing top-level content, so the
    # seeded `web_search = "disabled"` key must survive it (TOML: bare top-level
    # keys precede any table header, which is exactly the resulting order).
    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    import user_mcp_materialize

    seeded = spawners.agent_home_files(
        "/h", driver="codex", provider="openai")["/h/codex-home/config.toml"]
    merged = user_mcp_materialize.codex_config_merged(
        seeded, [{"name": "svc", "url": "https://mcp.example/mcp", "enabled": True}])
    assert 'web_search = "disabled"' in merged
    assert "[mcp_servers.svc]" in merged
    # the top-level key precedes the first table header (valid TOML ordering)
    assert merged.index('web_search = "disabled"') < merged.index("[mcp_servers.svc]")
    # merging zero servers leaves the seed untouched too
    assert user_mcp_materialize.codex_config_merged(seeded, []).strip() == seeded.strip()


def test_stale_home_files_codex_always_prunes_config_toml():
    # Historical case: a user who used to be bridged through the in-CVM LiteLLM
    # gateway (now retired) may still carry a codex-home/config.toml pointing at
    # the (now-dead) gateway on the PERSISTENT home. agent_home_files never
    # writes that file any more, so it must always be pruned here.
    stale = spawners.stale_home_files("/h", driver="codex")
    assert "/h/codex-home/config.toml" in stale


def test_materialize_home_overwrites_stale_gateway_config_on_codex(tmp_path):
    # A codex user with a stale in-CVM LiteLLM gateway config.toml on a PERSISTENT
    # home. codex now WRITES a fresh config.toml (web_search disabled), which is in
    # `files` so the prune guard skips it and the fresh write OVERWRITES the stale
    # gateway content — same net effect (no dead gateway config survives) as the
    # old unlink path, and it also lands the web-search toggle.
    home = str(tmp_path / "u")
    cfg = tmp_path / "u" / "codex-home" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('model_provider = "feedling_gateway"\nbase_url = "http://127.0.0.1:4000/v1"\n')
    # 明确指定为官方 provider，所以不会注入身份块
    spawners.materialize_home(home, driver="codex", provider="openai")
    # the stale gateway config is gone (overwritten) → codex is native + web off
    assert cfg.read_text() == 'web_search = "disabled"\n'
    assert "feedling_gateway" not in cfg.read_text()
    # AGENTS.md still seeded
    assert (tmp_path / "u" / "codex-home" / "AGENTS.md").exists()


def test_materialize_home_prunes_codex_config_when_not_codex(tmp_path):
    # A user who SWITCHED AWAY from codex (now claude) must have any leftover
    # codex-home/config.toml pruned — it is not in `files` for a non-codex driver,
    # so the prune path (unlink) still applies. Guards that the overwrite-for-codex
    # change did not disable pruning for the drivers that still need it.
    home = str(tmp_path / "u")
    cfg = tmp_path / "u" / "codex-home" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('web_search = "disabled"\n')
    spawners.materialize_home(home, driver="claude", provider="anthropic")
    assert not cfg.exists()


def test_materialize_home_creates_image_dir_for_claude(tmp_path):
    # The claude command adds `--add-dir {home}/images`; claude refuses/warns on a
    # missing --add-dir target, and the dir is created lazily only when the FIRST
    # image is decrypted. Create it at spawn so the very first turn's --add-dir is
    # valid even before any image has arrived.
    home = str(tmp_path / "u")
    spawners.materialize_home(home, driver="claude", provider="anthropic")
    assert (tmp_path / "u" / "images").is_dir()
    # Same for the chat-file dir the claude command --add-dir's every turn.
    assert (tmp_path / "u" / "files").is_dir()
    assert (tmp_path / "u" / "outbound-files").is_dir()


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


def test_is_official_identity_native_anthropic_and_openai_only():
    assert spawners._is_official_identity("anthropic", "") is True
    assert spawners._is_official_identity("openai", "") is True
    assert spawners._is_official_identity("OpenAI", "  ") is True  # 大小写/空白容忍
    assert spawners._is_official_identity("deepseek", "https://api.deepseek.com") is False
    assert spawners._is_official_identity("gemini", "") is False
    assert spawners._is_official_identity("openai_compatible", "") is False
    # 冒充防御：anthropic 但配了中转 base_url → 判非官方
    assert spawners._is_official_identity("anthropic", "https://relay.example/anthropic") is False
    # provider 缺省 → 按官方处理（legacy/native/default 路径不误伤，即便带 base_url）
    assert spawners._is_official_identity("", "") is True
    assert spawners._is_official_identity("  ", "https://x") is True
    # 官方 provider 存了「默认」base_url 仍算官方（validate_config 会持久化默认值）——
    # 单纯非空不等于非官方；只有自定义/非默认 endpoint 才翻非官方（Codex P1）
    assert spawners._is_official_identity("anthropic", "https://api.anthropic.com/v1") is True
    assert spawners._is_official_identity("openai", "https://api.openai.com/v1") is True
    assert spawners._is_official_identity("openai", "https://api.openai.com/v1/") is True  # 尾斜杠容忍
    assert spawners._is_official_identity("openai", "https://relay.example/v1") is False    # 自定义


def test_identity_override_block_empty_for_official():
    assert spawners._identity_override_block("anthropic", "claude-3.5-sonnet", "") == ""
    assert spawners._identity_override_block("openai", "gpt-4o", "") == ""


def test_identity_override_block_uses_model_id_for_third_party():
    block = spawners._identity_override_block("deepseek", "deepseek-chat", "https://api.deepseek.com")
    assert "deepseek-chat" in block
    assert "Claude Code" in block  # 明确点名不许冒充的壳子身份
    assert "Codex" in block


def test_identity_override_block_falls_back_to_provider_name_when_model_empty():
    assert "gemini" in spawners._identity_override_block("gemini", "", "")


def test_identity_override_block_empty_when_provider_absent():
    # provider 缺省按官方处理，不注块（回归防护：Codex P2 —— legacy/native/default 路径）
    assert spawners._identity_override_block("", "", "") == ""
    assert spawners._identity_override_block("", "gpt-4o", "") == ""


def test_agent_home_files_injects_identity_block_for_third_party_claude():
    files = spawners.agent_home_files(
        "/h", driver="claude", provider="deepseek",
        base_url="https://api.deepseek.com", model="deepseek-chat")
    append = files["/h/agent-tools-prompt.md"]
    assert "deepseek-chat" in append
    assert append.startswith("## 你的真实身份")  # 身份块置顶


def test_agent_home_files_no_identity_block_for_native_anthropic():
    files = spawners.agent_home_files(
        "/h", driver="claude", provider="anthropic", model="claude-3.5-sonnet")
    append = files["/h/agent-tools-prompt.md"]
    assert "你的真实身份" not in append


def test_agent_home_files_no_identity_block_for_native_openai_codex():
    files = spawners.agent_home_files(
        "/h", driver="codex", provider="openai", model="gpt-4o")
    agents_md = files["/h/codex-home/AGENTS.md"]
    assert "你的真实身份" not in agents_md


def test_agent_home_files_no_identity_block_when_provider_absent():
    # 回归防护(Codex P2)：provider 缺省的 legacy/native/default 条目不得被注入第三方块。
    # _codex_transport 把缺省 provider 当原生 OpenAI，claude 缺省即原生 anthropic。
    claude_append = spawners.agent_home_files("/h", driver="claude")["/h/agent-tools-prompt.md"]
    assert "你的真实身份" not in claude_append
    codex_md = spawners.agent_home_files("/h", driver="codex")["/h/codex-home/AGENTS.md"]
    assert "你的真实身份" not in codex_md


def test_agent_home_files_official_provider_with_default_base_url_no_block():
    # 官方 provider 带默认 base_url（validate_config 持久化）仍不注块（Codex P1）
    files = spawners.agent_home_files(
        "/h", driver="claude", provider="anthropic",
        base_url="https://api.anthropic.com/v1", model="claude-3.5-sonnet")
    assert "你的真实身份" not in files["/h/agent-tools-prompt.md"]


def test_agent_home_files_identity_block_uses_identity_model_over_model():
    # identity_model 目前恒为空（LiteLLM 网关已退役，不再有 gw-<uid> 别名改写 model），
    # 字段/参数保留只是防未来复用——但当调用方显式传入时，身份块仍须优先用它。
    files = spawners.agent_home_files(
        "/h", driver="codex", provider="gemini",
        model="internal-alias", identity_model="gemini-2.0-flash")
    agents_md = files["/h/codex-home/AGENTS.md"]
    assert "gemini-2.0-flash" in agents_md   # 身份块用 identity_model
    assert "internal-alias" not in agents_md


def test_agent_home_files_identity_block_reaches_codex_and_pi():
    cfiles = spawners.agent_home_files(
        "/h", driver="codex", provider="gemini", model="gemini-2.0-flash")
    assert "gemini-2.0-flash" in cfiles["/h/codex-home/AGENTS.md"]
    pfiles = spawners.agent_home_files(
        "/h", driver="pi", provider="openai_compatible",
        base_url="https://relay.example/v1", model="some-relay-model")
    assert "some-relay-model" in pfiles["/h/agent-tools-prompt.md"]


def test_agent_home_files_identity_block_sits_above_persona():
    files = spawners.agent_home_files(
        "/h", driver="claude", provider="deepseek",
        base_url="https://api.deepseek.com", model="deepseek-chat",
        persona_content="You are Kai.")
    append = files["/h/agent-tools-prompt.md"]
    assert append.index("你的真实身份") < append.index("You are Kai.")


# ---- pi driver wiring (Task 4): cli template, models.json seed, env, stale prune ----


def test_pi_default_cli_cmd():
    cmd = spawners._default_cli_cmd("pi", "/h", model="m")
    assert "pi --mode json" in cmd
    # -t bash would ALSO filter out extension-registered tools (pi 0.80.3
    # agent-session.js:1867 filters allCustomTools through isAllowedTool), which
    # would silently kill the user-MCP bridge. -xt read,edit,write leaves the
    # same active set (["bash"]) while letting extension tools through.
    assert "-xt read,edit,write" in cmd
    assert "-t bash" not in cmd
    # -ne: with no -t, allowedToolNames is undefined and auto-discovered
    # extensions in ~/.pi/agent/extensions/ would get their tools activated by
    # includeAllExtensionTools. The agent can write files there via bash. -ne
    # closes discovery; explicit -e still works.
    assert "-ne" in cmd
    assert "{mcp}" in cmd


def test_pi_default_cli_cmd_tool_posture_equals_old_t_bash():
    """Regression: a pi user with no MCP config must keep the old active set.

    pi's defaultActiveToolNames is the hardcoded ["read","bash","edit","write"]
    (sdk.js:131). Excluding read/edit/write leaves exactly ["bash"] — which is
    what `-t bash` used to produce. Asserting this against the real command
    (rather than against a literal) is the point: it is what would catch the
    template drifting away from the posture it is supposed to preserve.
    """
    cmd = spawners._default_cli_cmd("pi", "/h", model="m")
    excluded = re.search(r"-xt (\S+)", cmd).group(1).split(",")
    # Order within -xt is not semantically meaningful; the SET is.
    assert set(excluded) == {"read", "edit", "write"}
    assert {"read", "bash", "edit", "write"} - set(excluded) == {"bash"}


def test_pi_default_cli_cmd_omits_model_when_unset():
    cmd = spawners._default_cli_cmd("pi", "/h", model="")
    assert "--model" not in cmd
    assert cmd.rstrip().endswith("--session-id {session_id}")


def test_pi_default_cli_cmd_quotes_model_with_spaces():
    """A model id with spaces must survive the consumer's ``shlex.split`` as ONE
    argv token.

    Relay aliases routinely carry spaces — prod carries a dozen of the form
    ``[Kiro] claude-opus-4-6-thinking [不补]``. The template is a SHELL STRING that
    the resident re-tokenizes with ``shlex.split`` (chat_resident_consumer
    ``_cli_cmd_tokens``), so a bare f-string interpolation splits the alias apart
    and its tail lands as POSITIONAL argv. pi treats positionals as extra user
    messages: it answered the real turn, then answered the stray ``[不补]`` with
    "好的。" — and ``_pi_turn_from_stream`` keeps the LAST text-bearing assistant
    message, so every real reply was discarded and users got "好的。" instead
    (usr_50c1177a, prod 2026-07-21; memory_capture died the same way with
    ``no_json_object`` after ``{"cards": []}`` was overwritten).
    """
    model = "[kiro零缓] claude-opus-4-6-thinking [不补]"
    cmd = spawners._default_cli_cmd("pi", "/h", model=model)
    argv = shlex.split(cmd)
    assert f"{spawners._PI_PROVIDER_ID}/{model}" in argv


def test_pi_default_cli_cmd_model_matches_models_json_entry_id():
    """``--model`` must name EXACTLY the id ``_pi_models_json`` registers.

    ``_pi_models_json`` writes ``model.strip()`` as the entry id. Before quoting,
    surrounding whitespace was harmless — ``shlex.split`` discarded it. Quoting makes
    it load-bearing, so an unstripped route row would ship ``--model 'feedling/gpt-5 '``
    against an entry id of ``gpt-5`` and pi would exit rc=1 (`Model not found`) before
    any request. Both sides must normalize identically."""
    for model in (" gpt-5 ", "[Kiro] claude-opus-4-6 [不补]", "m"):
        cmd = spawners._default_cli_cmd("pi", "/h", model=model)
        argv = shlex.split(cmd)
        sent = argv[argv.index("--model") + 1]
        entry_id = json.loads(
            spawners._pi_models_json(base_url="https://r/v1", model=model,
                                     provider="openai_compatible")
        )["providers"][spawners._PI_PROVIDER_ID]["models"][0]["id"]
        assert sent == f"{spawners._PI_PROVIDER_ID}/{entry_id}", model


def test_claude_default_cli_cmd_selects_exact_route_model():
    argv = shlex.split(
        spawners._default_cli_cmd(
            "claude", "/h", model="claude-fable-5",
        )
    )

    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert "--fallback-model" not in argv


def test_claude_default_cli_cmd_quotes_model_as_one_token():
    model = "custom model alias"
    argv = shlex.split(spawners._default_cli_cmd("claude", "/h", model=model))

    assert argv[argv.index("--model") + 1] == model


def test_claude_thinking_cli_cmd_selects_exact_route_model():
    argv = shlex.split(
        spawners._default_thinking_claude_cmd(
            "/h", model="claude-opus-4-8",
        )
    )

    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert "--fallback-model" not in argv


def test_consumer_env_pins_claude_route_model_in_argv_and_metadata():
    env = spawners.consumer_env(
        {"PATH": "/bin"},
        {
            "api_key": "fk",
            "provider": "anthropic",
            "provider_key": "sk-ant",
            "driver": "claude",
            "model": "claude-fable-5",
        },
        user_id="u_1",
        home="/agent-data/users/u_1",
    )
    argv = shlex.split(env["AGENT_CLI_CMD"])

    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert env["ANTHROPIC_MODEL"] == "claude-fable-5"
    assert env["FEEDLING_AGENT_PROVIDER"] == "anthropic"
    assert env["FEEDLING_AGENT_MODEL_ID"] == "claude-fable-5"


def test_pi_home_writes_models_json():
    files = spawners.agent_home_files("/h", driver="pi", provider="openrouter",
                                      model="x", reasoning_effort="high")
    assert "/h/pi-home/agent/models.json" in files
    doc = json.loads(files["/h/pi-home/agent/models.json"])
    prov = doc["providers"]["feedling"]
    assert prov["api"] == "openai-completions"
    assert prov["compat"]["supportsReasoningEffort"] is True   # reasoning threaded through
    assert prov["compat"]["thinkingFormat"] == "openrouter"    # openrouter reasoning wire
    # pi's real thinking switch is the model entry's `reasoning` boolean, NOT the
    # (ignored) `reasoningEffort` field that shipped in b2022da.
    assert prov["models"][0]["reasoning"] is True
    assert "reasoningEffort" not in prov["models"][0]
    # pi gets no claude/codex home files
    assert "/h/claude-home/settings.json" not in files
    assert "/h/codex-home/AGENTS.md" not in files


def test_pi_models_json_reasoning_default_on_null_off_when_explicit():
    # Unset (null) reasoning_effort defaults thinking ON (_PI_REASONING_DEFAULT=medium);
    # an EXPLICIT off disables it (the default fills null only). No dead reasoningEffort.
    on = json.loads(spawners._pi_models_json(
        base_url="https://relay.x/v1", model="x", provider="openrouter", reasoning_effort=""))
    m_on = on["providers"]["feedling"]["models"][0]
    assert m_on["reasoning"] is True
    assert "reasoningEffort" not in m_on
    assert on["providers"]["feedling"]["compat"]["supportsReasoningEffort"] is True

    off = json.loads(spawners._pi_models_json(
        base_url="https://relay.x/v1", model="x", provider="openrouter", reasoning_effort="off"))
    m_off = off["providers"]["feedling"]["models"][0]
    assert m_off["reasoning"] is False
    assert off["providers"]["feedling"]["compat"]["supportsReasoningEffort"] is False


def test_pi_gemini_models_json_has_baseurl():
    # pi REJECTS a custom model with no baseUrl ("baseUrl is required") and fails to
    # load the WHOLE models.json — so the gemini branch must always emit one.
    doc = json.loads(spawners._pi_models_json(
        base_url="https://relay.x/v1", model="g", provider="gemini", reasoning_effort="medium"))
    prov = doc["providers"]["feedling"]
    assert prov["api"] == "google-generative-ai"
    assert prov["baseUrl"] == "https://relay.x/v1"          # relay base_url used
    assert prov["models"][0]["reasoning"] is True
    # empty base_url falls back to google's default rather than emitting no baseUrl
    doc2 = json.loads(spawners._pi_models_json(
        base_url="", model="g", provider="gemini", reasoning_effort=""))
    assert doc2["providers"]["feedling"]["baseUrl"].startswith("https://generativelanguage.googleapis.com")


def test_pi_default_cli_cmd_threads_thinking_level():
    # The route's reasoning_effort reaches pi as --thinking <level> so the exact
    # level (not just "on") is honored. Unset defaults to medium; explicit off omits.
    cmd_hi = spawners._default_cli_cmd("pi", "/h", model="m", reasoning_effort="high")
    assert "--thinking high" in cmd_hi
    cmd_lo = spawners._default_cli_cmd("pi", "/h", model="m", reasoning_effort="low")
    assert "--thinking low" in cmd_lo
    cmd_null = spawners._default_cli_cmd("pi", "/h", model="m", reasoning_effort="")
    assert "--thinking medium" in cmd_null       # unset → default-on medium
    cmd_off = spawners._default_cli_cmd("pi", "/h", model="m", reasoning_effort="off")
    assert "--thinking" not in cmd_off           # explicit off → no flag


@pytest.mark.skipif(shutil.which("pi") is None,
                    reason="pi CLI not installed (real-pi integration test)")
def test_pi_models_json_loads_and_enables_reasoning_in_real_pi(tmp_path):
    """Feed spawner-generated models.json to the REAL pi CLI and assert pi
    recognizes the model's reasoning capability.

    This is the regression line the reasoningEffort bug slipped through: a
    hand-built JSONL fixture only exercises the resident's parser, never whether
    pi actually treats the model as thinking-capable. ``pi --list-models`` prints
    a ``thinking`` column = ``model.reasoning ? "yes" : "no"``, so it verifies the
    whole models.json → pi model-registry path (including that pi LOADS the file
    at all — the gemini missing-baseUrl bug failed the whole file silently)."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir),
           "PI_PROVIDER_API_KEY": "sk-test", "PI_OFFLINE": "1"}

    def thinking_col(provider: str, effort: str) -> str:
        (agent_dir / "models.json").write_text(spawners._pi_models_json(
            base_url="https://relay.example/v1", model="m", provider=provider,
            reasoning_effort=effort))
        out = subprocess.run(["pi", "--list-models", "feedling"],
                             capture_output=True, text=True, env=env, timeout=60).stdout
        rows = [ln.split() for ln in out.splitlines() if ln.startswith("feedling")]
        assert rows, f"pi failed to load models.json for {provider}/{effort!r}: {out!r}"
        # columns: provider model context max-out thinking images
        return rows[0][-2]

    assert thinking_col("openrouter", "high") == "yes"        # explicit level enabled
    assert thinking_col("openrouter", "") == "yes"            # null → default-on (medium)
    assert thinking_col("openrouter", "off") == "no"          # explicit off disables
    assert thinking_col("openai_compatible", "medium") == "yes"
    assert thinking_col("gemini", "medium") == "yes"          # LOADS (baseUrl present)


def test_pi_consumer_env_sets_provider_key():
    env = spawners.consumer_env({}, {"provider_key": "sk-or", "driver": "pi",
                                     "provider": "openrouter", "model": "x"},
                                user_id="u", home="/h")
    assert env["PI_PROVIDER_API_KEY"] == "sk-or"
    assert env["PI_CODING_AGENT_DIR"] == "/h/pi-home/agent"
    assert env["PI_OFFLINE"] == "1"
    # pi must not inherit any other driver's env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "CLAUDE_CONFIG_DIR" not in env


def test_consumer_env_uses_pi_cli_and_home_for_pi_driver():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-relay", "driver": "pi",
             "provider": "openai_compatible", "model": "qwen-max",
             "base_url": "https://my.host/v1"},
        user_id="u_1", home="/h",
    )
    cmd = env["AGENT_CLI_CMD"]
    assert cmd.startswith("pi --mode json -ne -xt read,edit,write {mcp} ")
    assert "--append-system-prompt /h/agent-tools-prompt.md" in cmd
    assert "--model feedling/qwen-max" in cmd
    assert "--session-id {session_id}" in cmd
    assert "{message}" not in cmd
    assert cmd.rstrip().endswith("--session-id {session_id}")


def test_consumer_env_keys_have_no_litellm():
    assert not any("LITELLM" in k for k in spawners._CONSUMER_ENV_KEYS)


def test_stale_home_files_prunes_pi_models_json_when_not_pi():
    stale = spawners.stale_home_files("/h", driver="codex")
    assert "/h/pi-home/agent/models.json" in stale
    stale_pi = spawners.stale_home_files("/h", driver="pi")
    assert "/h/pi-home/agent/models.json" not in stale_pi


# ---- pi models.json generator (Task 3, pure) ----


def _prov(provider, *, model, base_url, reasoning_effort=""):
    """Build the pi provider dict directly from ``_pi_models_json`` (pure — the
    generator is exercised on its own here; the ``agent_home_files`` pi wiring is
    covered in the pi-driver wiring section above)."""
    doc = json.loads(spawners._pi_models_json(
        base_url=base_url, model=model, provider=provider,
        reasoning_effort=reasoning_effort,
    ))
    return doc["providers"][spawners._PI_PROVIDER_ID]


def _model_reasoning(p):
    # pi's real thinking switch is the model entry's `reasoning` boolean (the exact
    # level rides the CLI --thinking flag, not models.json).
    return p["models"][0].get("reasoning", False)


def test_pi_models_gemini():
    p = _prov("gemini", model="gemini-2.0-flash", base_url="")
    assert p["api"] == "google-generative-ai" and "compat" not in p
    assert p["models"][0]["input"] == ["text", "image"]


def test_pi_models_gemini_always_has_base_url():
    """pi REJECTS a custom provider without ``baseUrl`` — regardless of api type —
    and a rejected provider voids the WHOLE models.json ("No models available"),
    so the turn dies with `Model "feedling/<id>" not found` (rc=1) before any
    request is made. Verified in-CVM against pi 0.80.3 (`--list-models`) and
    stated outright in pi's own docs/models.md: "The baseUrl is required when
    adding custom models to the google-generative-ai API type." A credential
    always carries the persisted default, but fall back anyway so an empty
    base_url can never resurrect the void-config failure."""
    p = _prov("gemini", model="gemini-2.0-flash", base_url="")
    assert p["baseUrl"] == "https://generativelanguage.googleapis.com/v1beta"


def test_pi_models_gemini_custom_base():
    p = _prov("gemini", model="gemini-2.0-flash",
              base_url="https://gw.example.com/v1beta/")
    assert p["baseUrl"] == "https://gw.example.com/v1beta"


def test_pi_models_openrouter_headers_and_base():
    p = _prov("openrouter", model="x", base_url="")
    assert p["api"] == "openai-completions"
    assert p["baseUrl"] == "https://openrouter.ai/api/v1"
    assert p["headers"]["HTTP-Referer"] and p["headers"]["X-Title"]


def test_pi_models_openai_compatible_uses_user_base():
    p = _prov("openai_compatible", model="qwen", base_url="https://my/v1/")
    assert p["api"] == "openai-completions" and p["baseUrl"] == "https://my/v1"


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai_compatible", "https://relay.example/v1"),
        ("openrouter", ""),
        ("gemini", ""),
    ],
)
def test_pi_models_json_pins_max_tokens(provider, base_url):
    """EVERY branch must pin ``maxTokens``, or pi fills its own 16384 default
    (pi-coding-agent model-registry: ``maxTokens: modelDef.maxTokens ?? 16384``).

    That default is what breaks low-budget users: relays pre-authorize against the
    requested max_tokens, and an OpenRouter key with a total-limit simply 402s —
    observed live as ``pi agent produced no reply: 402: You requested up to 16384
    tokens, but can only afford 1698``. pi exposes no --max-tokens flag, so the
    model entry is the ONLY lever."""
    p = _prov(provider, model="m", base_url=base_url)
    assert p["models"][0]["maxTokens"] == spawners._pi_max_tokens()


def test_pi_max_tokens_env_override(monkeypatch):
    monkeypatch.setenv("FEEDLING_PI_MAX_TOKENS", "2048")
    assert spawners._pi_max_tokens() == 2048
    p = _prov("openai_compatible", model="m", base_url="https://r/v1")
    assert p["models"][0]["maxTokens"] == 2048


def test_pi_max_tokens_stays_above_the_thinking_budget():
    """Guard the footgun: pi reserves a thinking budget by level (medium = 8192) and
    clamps with ``if maxTokens <= thinkingBudget: thinkingBudget = maxTokens - 1024``.
    So a maxTokens at/below the budget silently collapses thinking to 1024 tokens.
    Our default ships with thinking ON at medium (``_PI_REASONING_DEFAULT``), so the
    default maxTokens must leave room for both. Lowering it is a deliberate act —
    do it via FEEDLING_PI_MAX_TOKENS, and lower --thinking with it."""
    assert spawners._pi_max_tokens() > 8192


def test_pi_max_tokens_rejects_garbage(monkeypatch):
    monkeypatch.setenv("FEEDLING_PI_MAX_TOKENS", "not-a-number")
    assert spawners._pi_max_tokens() == spawners._PI_MAX_TOKENS_DEFAULT
    monkeypatch.setenv("FEEDLING_PI_MAX_TOKENS", "0")
    assert spawners._pi_max_tokens() == spawners._PI_MAX_TOKENS_DEFAULT


# NATIVE REASONING (no gateway):


def test_pi_openrouter_forwards_reasoning_effort():
    p = _prov("openrouter", model="x", base_url="", reasoning_effort="high")
    assert p["compat"]["supportsReasoningEffort"] is True
    assert p["compat"]["thinkingFormat"] == "openrouter"
    assert _model_reasoning(p) is True


def test_pi_openrouter_off_omits_reasoning():
    p = _prov("openrouter", model="x", base_url="", reasoning_effort="off")
    assert p["compat"]["supportsReasoningEffort"] is False
    assert _model_reasoning(p) is False


def test_pi_openrouter_bad_effort_normalized_to_medium():
    # Garbage effort normalizes to medium: enables reasoning on the model AND pins
    # the CLI to --thinking medium (the level itself now rides the flag, not json).
    p = _prov("openrouter", model="x", base_url="", reasoning_effort="ultra")
    assert _model_reasoning(p) is True
    assert "--thinking medium" in spawners._default_cli_cmd(
        "pi", "/h", model="x", reasoning_effort="ultra")


def test_pi_openai_compatible_reasoning_default_on_off_when_explicit():
    # null (unset) defaults ON; explicit off disables; an explicit level passes through.
    assert _model_reasoning(_prov("openai_compatible", model="q", base_url="https://m/v1")) is True
    assert _model_reasoning(_prov("openai_compatible", model="q", base_url="https://m/v1", reasoning_effort="off")) is False
    p = _prov("openai_compatible", model="q", base_url="https://m/v1", reasoning_effort="low")
    assert p["compat"]["supportsReasoningEffort"] is True and _model_reasoning(p) is True


def test_claude_anthropic_base_url_deepseek_compat_endpoint():
    # Native anthropic keeps the CLI default (no override); deepseek gets its
    # /anthropic-compatible endpoint (custom base_url honored, default otherwise).
    assert spawners._claude_anthropic_base_url({"provider": "anthropic"}) == ""
    assert spawners._claude_anthropic_base_url(
        {"provider": "deepseek", "base_url": "https://api.deepseek.com"}
    ) == "https://api.deepseek.com/anthropic"
    assert spawners._claude_anthropic_base_url(
        {"provider": "deepseek", "base_url": ""}
    ) == "https://api.deepseek.com/anthropic"
    assert spawners._claude_anthropic_base_url(
        {"provider": "deepseek", "base_url": "https://ds.example.com/"}
    ) == "https://ds.example.com/anthropic"


# Reseller/relay marketing tags in the model id pollute the agent's self-reference.
# Sanitize them out of the IDENTITY string only (routing keeps the raw name).
def test_sanitize_model_name_strips_reseller_tags_for_identity():
    s = spawners._sanitize_model_name_for_identity
    assert s("[Kiro] claude-opus-4-6 [不补]") == "claude-opus-4-6"
    assert s("[特价纯血]claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert s("[特特价次kiro]claude-opus-4-6-thinking") == "claude-opus-4-6-thinking"
    assert s("【促销】gpt-4o-mini") == "gpt-4o-mini"
    # clean ids untouched (incl. openrouter provider/model slash form + dots)
    assert s("deepseek-v4-flash") == "deepseek-v4-flash"
    assert s("anthropic/claude-opus-4.6") == "anthropic/claude-opus-4.6"
    # all-tags -> empty so the identity block falls back to provider, not a tag
    assert s("[全是标签]") == ""
    assert s("") == ""


def test_identity_block_uses_sanitized_model_name():
    block = spawners._identity_override_block(
        "openai_compatible", "[Kiro] claude-opus-4-6 [不补]", "https://relay.example/v1")
    assert "claude-opus-4-6" in block
    assert "[不补]" not in block          # the marketing tag never reaches self-reference
    assert "[Kiro]" not in block


def test_identity_block_all_tags_falls_back_to_provider():
    block = spawners._identity_override_block(
        "openai_compatible", "[全是标签]", "https://relay.example/v1")
    assert "`openai_compatible`" in block
    assert "[全是标签]" not in block


def test_identity_sanitization_does_not_change_pi_routing_model():
    raw_model = "[Kiro] claude-opus-4-6 [不补]"
    files = spawners.agent_home_files(
        "/h", driver="pi", provider="openai_compatible",
        base_url="https://relay.example/v1", model=raw_model)
    prompt = files["/h/agent-tools-prompt.md"]
    models = json.loads(files["/h/pi-home/agent/models.json"])

    assert "`claude-opus-4-6`" in prompt
    assert "[Kiro]" not in prompt
    assert "[不补]" not in prompt
    assert models["providers"]["feedling"]["models"][0]["id"] == raw_model


def test_every_verb_documented_in_the_rendered_prompt_is_also_allowlisted():
    """Prompt/allowlist consistency, enforced generically (T13 successor).

    agent_tools_prompt.md's command block is now the ``<io_cli_catalog>``
    placeholder (T13) — verbs are taught dynamically from the live io_cli --help
    sweep (tools/io_cli_catalog.py), not hand-listed in the .md source anymore.
    That live sweep can surface verbs the hosted claude driver was never granted
    (e.g. ``identity-redistill`` — VPS-local-IPC-only, no [setup]/[ops] tag to
    filter it out), which would reproduce the exact "requires approval" loop this
    test used to catch for the old hand list. So the check now runs against the
    RENDERED prompt (what the model actually sees), not the static .md source.
    """
    prompt = spawners.agent_home_files("/h", driver="claude", provider="anthropic")[
        "/h/agent-tools-prompt.md"
    ]
    documented = set(re.findall(rf"python {re.escape(spawners._IO_CLI)} ([a-z][a-z0-9-]*)", prompt))
    assert documented, "rendered prompt must document at least one io_cli verb"

    missing = sorted(documented - set(spawners._IO_CLI_VERBS))
    assert not missing, (
        f"verbs documented in the rendered prompt but not granted in "
        f"_IO_CLI_VERBS (the agent will be blocked calling them): {missing}"
    )


def test_recent_apps_verb_is_granted():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u", home="/agent-data/users/u",
    )
    assert "io_cli.py perception-recent-apps" in env["AGENT_CLI_CMD"]


def test_consumer_env_keys_include_anthropic_wire_overrides():
    # consumer_env sets ANTHROPIC_BASE_URL/MODEL/SMALL_FAST_MODEL for claude-wire
    # third parties (deepseek), but the container strategy only forwards keys in
    # _CONSUMER_ENV_KEYS. Missing them → a containerized deepseek agent hits
    # api.anthropic.com with a foreign key and every turn fails.
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
        assert key in spawners._CONSUMER_ENV_KEYS


def test_container_env_file_carries_per_user_values(monkeypatch):
    # The env-file body must carry each user's OWN computed values (a bare
    # `-e KEY` argv would have inherited the shared supervisor env → smear).
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.internal:5001")
    entry = {"provider_key": "sk-deepseek", "driver": "claude",
             "provider": "deepseek", "model": "deepseek-chat"}
    body = spawners.container_env_file_content(entry, user_id="u_a")
    lines = dict(ln.split("=", 1) for ln in body.splitlines() if ln)

    # per-user secret + claude-wire redirect present with real values
    assert lines["ANTHROPIC_API_KEY"] == "sk-deepseek"
    assert lines["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert lines["ANTHROPIC_MODEL"] == "deepseek-chat"
    # per-user MCP path is the home-pinned one (container home = the volume mount
    # /agent-data), never the shared /tmp default
    assert lines["USER_MCP_FILE"] == "/agent-data/user-mcp.json"
    # GLOBAL values (consumer_env flows them through from base_env) must survive
    # the env-file build too, or the containerized consumer can't reach backend.
    assert lines["FEEDLING_API_URL"] == "http://backend.internal:5001"


def test_container_env_file_does_not_smear_supervisor_platform_key(monkeypatch):
    # A keyless host-all claude entry (no provider_key) must NOT inherit the
    # supervisor's OWN platform ANTHROPIC_API_KEY. The env-file base must be a
    # narrow global allowlist, NOT dict(os.environ) — otherwise the supervisor's
    # credentials smear into the "strongly isolated" per-user container and the
    # keyless agent silently bills the platform key (violates the no-platform-
    # key host-all invariant).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-PLATFORM-supervisor")
    monkeypatch.setenv("CODEX_API_KEY", "cdx-PLATFORM-supervisor")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.internal:5001")
    body = spawners.container_env_file_content(
        {"driver": "claude"}, user_id="u_a")  # no provider_key → keyless
    lines = dict(ln.split("=", 1) for ln in body.splitlines() if ln)
    assert "ANTHROPIC_API_KEY" not in lines
    assert "CODEX_API_KEY" not in lines
    # the intended global value still flows through
    assert lines["FEEDLING_API_URL"] == "http://backend.internal:5001"


def test_container_env_file_drops_newline_injecting_value(monkeypatch):
    # A decrypted BYOK provider_key with a newline must NOT become an injectable
    # second env-file line: docker --env-file would parse it as a separate env
    # var (e.g. HTTP_PROXY) and redirect the agent's outbound calls.
    body = spawners.container_env_file_content(
        {"driver": "claude", "provider_key": "sk-abc\nHTTP_PROXY=http://attacker"},
        user_id="u_a")
    assert "HTTP_PROXY" not in body
    assert "attacker" not in body
    # the poisoned key is dropped, not emitted broken
    lines = dict(ln.split("=", 1) for ln in body.splitlines() if ln)
    assert "ANTHROPIC_API_KEY" not in lines
