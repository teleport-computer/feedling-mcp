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
    assert env["AGENT_SESSION_MAX_TURNS"] == "24"   # host rotates sooner than VPS default (40)
    assert env["CLAUDE_CONFIG_DIR"] == "/agent-data/users/u_1/claude-home"
    assert env["CONSUMER_ID"] == "agent-runner:u_1"
    assert env["PATH"] == "/bin" and env["FEEDLING_API_URL"] == "http://b:5001"  # base preserved


def test_consumer_env_threads_self_authored_thinking_fallback_per_user():
    disabled = spawners.consumer_env(
        {"FEEDLING_SELF_AUTHORED_THINKING_FALLBACK": "1"},
        {"api_key": "fk", "provider_key": "sk-ant"},
        user_id="u_off",
        home="/agent-data/users/u_off",
    )
    enabled = spawners.consumer_env(
        {},
        {"api_key": "fk", "provider_key": "sk-ant", "thinking_fallback": True},
        user_id="u_on",
        home="/agent-data/users/u_on",
    )

    assert disabled["FEEDLING_SELF_AUTHORED_THINKING_FALLBACK"] == "0"
    assert enabled["FEEDLING_SELF_AUTHORED_THINKING_FALLBACK"] == "1"


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
    # / reasoning events into the thinking disclosure.
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex"},
        user_id="u_1", home="/h",
    )
    cmd = env["AGENT_CLI_CMD"]
    assert "-c model_reasoning_effort=medium" in cmd
    assert "-c model_reasoning_summary=auto" in cmd


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
    # per-user ambient timezone passed by env reference, so the container clock
    # isn't UTC (consumer_env sets TZ; the container whitelist must forward it)
    assert "TZ" in argv
    assert argv[argv.index("TZ") - 1] == "-e"


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
    assert any("io_cli.py identity-write" in rule for rule in allow)  # 7.D post-respawn tool
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
    # native (default) codex talks straight to OpenAI — no gateway config.toml
    assert "/h/codex-home/config.toml" not in files


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


# ---- codex → LiteLLM gateway (non-openai providers) ----


def test_codex_native_for_openai_uses_provider_key_directly():
    # openai is codex's native wire: the OpenAI key goes straight to CODEX_API_KEY
    # and there is no gateway override.
    env = spawners.consumer_env(
        {"FEEDLING_LITELLM_API_KEY": "gw-key"},
        {"api_key": "fk", "provider_key": "sk-oai", "driver": "codex", "provider": "openai"},
        user_id="u", home="/h",
    )
    assert env["CODEX_API_KEY"] == "sk-oai"


def test_codex_gateway_for_non_openai_presents_gateway_key_not_upstream():
    # gemini/openrouter/openai_compatible reach codex only through the in-CVM
    # LiteLLM gateway: codex presents the GATEWAY key, never the upstream provider
    # key (which stays inside the gateway's own config — minimize key exposure).
    env = spawners.consumer_env(
        {"FEEDLING_LITELLM_API_KEY": "gw-key", "FEEDLING_LITELLM_BASE_URL": "http://127.0.0.1:4000/v1"},
        {"api_key": "fk", "provider_key": "sk-upstream", "driver": "codex", "provider": "gemini"},
        user_id="u", home="/h",
    )
    assert env["CODEX_API_KEY"] == "gw-key"
    assert "sk-upstream" not in env.values()


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


def test_agent_home_files_codex_gateway_writes_responses_config():
    # 明确指定为非官方 provider，会注入身份块。测试检查 gateway config 仍被创建
    files = spawners.agent_home_files(
        "/h", driver="codex", provider="gemini", codex_transport="gateway",
        gateway_base_url="http://127.0.0.1:4000/v1", model="gw-gemini",
    )
    cfg = files["/h/codex-home/config.toml"]
    # codex speaks OpenAI Responses to the gateway, which fans out to the provider
    assert 'wire_api = "responses"' in cfg
    assert "http://127.0.0.1:4000/v1" in cfg
    assert "gw-gemini" in cfg
    # AGENTS.md still seeded
    assert "/h/codex-home/AGENTS.md" in files


def test_agent_home_files_codex_gateway_disables_multi_agent_tools():
    # codex 0.142 declares its multi-agent tools as a `{"type": "namespace"}` tool
    # group on EVERY Responses request — an OpenAI-only wire extension. Non-OpenAI
    # upstreams behind the gateway reject the whole request on it (xAI: 422
    # "unknown variant 'namespace', expected one of 'function', 'web_search'"),
    # so every turn dies before the model runs. Gateway config must turn the
    # multi-agent feature off; native OpenAI keeps it (no config.toml written there).
    files = spawners.agent_home_files(
        "/h", driver="codex", provider="openrouter", codex_transport="gateway",
        gateway_base_url="http://127.0.0.1:4000/v1", model="gw-x",
    )
    cfg = files["/h/codex-home/config.toml"]
    assert "[features]" in cfg
    assert "multi_agent = false" in cfg
    assert "collab = false" not in cfg


def test_agent_home_files_codex_native_omits_gateway_config():
    # 明确指定为官方 openai provider，native codex 不会创建 gateway config
    files = spawners.agent_home_files("/h", driver="codex", provider="openai", codex_transport="native")
    assert "/h/codex-home/config.toml" not in files


def test_stale_home_files_native_codex_prunes_gateway_config():
    # A user who switched from a gateway provider (gemini/openrouter/...) to native
    # openai leaves a codex-home/config.toml pointing at the in-CVM gateway on the
    # PERSISTENT home. agent_home_files writes nothing for native, so the stale file
    # would survive and keep routing codex at the (now-dead) :4000 — list it to prune.
    stale = spawners.stale_home_files("/h", driver="codex", codex_transport="native")
    assert "/h/codex-home/config.toml" in stale


def test_stale_home_files_gateway_codex_keeps_config():
    # Gateway transport WRITES config.toml this spawn — it must never be pruned.
    stale = spawners.stale_home_files("/h", driver="codex", codex_transport="gateway")
    assert "/h/codex-home/config.toml" not in stale


def test_materialize_home_prunes_stale_gateway_config_on_native(tmp_path):
    home = str(tmp_path / "u")
    cfg = tmp_path / "u" / "codex-home" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('model_provider = "feedling_gateway"\nbase_url = "http://127.0.0.1:4000/v1"\n')
    # 明确指定为官方 provider，所以不会注入身份块
    spawners.materialize_home(home, driver="codex", provider="openai", codex_transport="native")
    # the stale gateway config is gone → codex falls back to native (api.openai.com)
    assert not cfg.exists()
    # AGENTS.md still seeded
    assert (tmp_path / "u" / "codex-home" / "AGENTS.md").exists()


def test_materialize_home_writes_and_keeps_gateway_config(tmp_path):
    home = str(tmp_path / "u")
    # 明确指定为非官方 provider，会注入身份块。测试检查 gateway config 仍被创建
    spawners.materialize_home(
        home, driver="codex", provider="gemini", codex_transport="gateway",
        gateway_base_url="http://127.0.0.1:4000/v1", model="gw-gemini",
    )
    cfg = tmp_path / "u" / "codex-home" / "config.toml"
    assert cfg.exists()
    assert "http://127.0.0.1:4000/v1" in cfg.read_text()


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


def test_agent_home_files_identity_block_uses_identity_model_over_gateway_alias():
    # gateway codex：model 已改写成内部 gw-<uid> 别名，身份自称须用真实上游模型（Codex P2）
    files = spawners.agent_home_files(
        "/h", driver="codex", provider="gemini", codex_transport="gateway",
        gateway_base_url="http://127.0.0.1:4000/v1",
        model="gw-u123", identity_model="gemini-2.0-flash")
    agents_md = files["/h/codex-home/AGENTS.md"]
    assert "gemini-2.0-flash" in agents_md   # 身份块用真实模型
    assert "gw-u123" not in agents_md          # 不暴露内部别名
    # gateway config.toml 仍用 gw-<uid> 别名做 LiteLLM 路由
    assert 'model = "gw-u123"' in files["/h/codex-home/config.toml"]


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
# ---- pi driver: cli template, models.json seed, env isolation, stale prune ----


def test_consumer_env_uses_pi_cli_and_home_for_pi_driver():
    env = spawners.consumer_env(
        {}, {"api_key": "fk", "provider_key": "sk-relay", "driver": "pi",
             "provider": "openai_compatible", "model": "qwen-max",
             "base_url": "https://my.host/v1"},
        user_id="u_1", home="/h",
    )
    cmd = env["AGENT_CLI_CMD"]
    assert cmd.startswith("pi --mode json -t bash ")
    assert "--append-system-prompt /h/agent-tools-prompt.md" in cmd
    assert "--model feedling/qwen-max" in cmd
    assert "--session-id {session_id}" in cmd
    # message rides STDIN, NOT argv (pi arg-parses positionals — a @/-/-- message
    # would be mis-parsed), so the template ends at --session-id with no {message}.
    assert "{message}" not in cmd
    assert cmd.rstrip().endswith("--session-id {session_id}")
    assert env["PI_CODING_AGENT_DIR"] == "/h/pi-home/agent"
    assert env["PI_PROVIDER_API_KEY"] == "sk-relay"
    assert env["PI_OFFLINE"] == "1"
    # 不能沾别的 driver 的 env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "CLAUDE_CONFIG_DIR" not in env


def test_agent_home_files_pi_seeds_models_json_with_relay_provider():
    files = spawners.agent_home_files(
        "/h", driver="pi", model="qwen-max", base_url="https://my.host/v1/")
    doc = json.loads(files["/h/pi-home/agent/models.json"])
    prov = doc["providers"]["feedling"]
    assert prov["baseUrl"] == "https://my.host/v1"          # 尾斜杠被剥掉
    assert prov["api"] == "openai-completions"              # 中转站只收 chat/completions
    assert prov["apiKey"] == "$PI_PROVIDER_API_KEY"         # $ENV 插值，key 不落盘
    assert prov["compat"] == {"supportsDeveloperRole": False,
                              "supportsReasoningEffort": False}
    assert prov["models"] == [{"id": "qwen-max"}]
    # 工具 how-to 照常 seed；不写 claude/codex 的 home 文件
    assert "/h/agent-tools-prompt.md" in files
    assert "/h/claude-home/settings.json" not in files
    assert "/h/codex-home/AGENTS.md" not in files


def test_stale_home_files_prunes_pi_models_json_when_not_pi():
    stale = spawners.stale_home_files("/h", driver="codex", codex_transport="native")
    assert "/h/pi-home/agent/models.json" in stale
    stale_pi = spawners.stale_home_files("/h", driver="pi")
    assert "/h/pi-home/agent/models.json" not in stale_pi
