"""Spawn strategies for per-user consumers — the isolation seam (plan §P5).

The canonical consumer is the existing VPS resident consumer
(``tools/chat_resident_consumer.py``): the agent-runner hosts it in the CVM,
one process per user, driven in ``cli`` mode against ``claude`` / ``codex exec``.
The resident consumer already does poll / enclave-decrypt / reply / output
cleaning / verify-ping / proactive — so the agent-runner only adds multi-tenant
supervision (lease + spawn + per-user isolation), which the single-user resident
consumer lacks.

Default ``process`` strategy = one child process per user in the shared
agent-runner container. ``container`` is the opt-in strong-isolation strategy
(per-user container/volume) — see docs/AGENT_RUNTIME_ISOLATION.md; live spawn
falls back to process until its lifecycle is finished.

``consumer_env`` / ``build_container_argv`` are pure (testable); process/docker
spawn is thin glue.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("feedling.agent_runtime.spawners")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The canonical consumer (repo_root/tools/chat_resident_consumer.py).
_RESIDENT_CONSUMER = str(_REPO_ROOT / "tools" / "chat_resident_consumer.py")
# The Feedling perception CLI the hosted agent pulls context through (A1-lite:
# skill + Bash, see docs/AGENT_CLI_INTEGRATION_SURVEY.md). Absolute so the path
# resolves the same from the agent's cwd in dev (repo root) and image (/app).
_IO_CLI = str(_REPO_ROOT / "tools" / "io_cli.py")
# perception verbs io_cli.py exposes; each becomes a scoped Bash allow-rule + is
# documented in the agent prompt so an unattended `claude -p` can pull context.
_PERCEPTION_VERBS = ("perception", "perception-trend", "perception-history")
# The how-to prompt shipped beside this module (into the image via COPY backend/).
_AGENT_PROMPT_TEXT = (Path(__file__).resolve().parent / "agent_tools_prompt.md").read_text()
_AGENT_PROMPT_BASENAME = "agent-tools-prompt.md"


def runtime_token_path(home: str) -> str:
    """Path of the per-user runtime-token file the supervisor writes and the
    consumer reads (Stage D)."""
    return f"{home}/runtime-token"


def write_runtime_token(home: str, token: str) -> None:
    """Write/refresh the per-user runtime token (0600). The supervisor calls this
    at spawn and on each heartbeat so the long-running consumer always has a
    fresh, short-lived token."""
    p = Path(runtime_token_path(home))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _perception_allow_rules(io_cli: str = _IO_CLI) -> list[str]:
    """Claude Bash permission allow-rules scoping the agent to just io_cli."""
    return [f"Bash(python {io_cli} {verb}:*)" for verb in _PERCEPTION_VERBS]


def _default_cli_cmd(driver: str, home: str, io_cli: str = _IO_CLI) -> str:
    """Default cli command per driver (resident substitutes ``{message}``).

    For claude we pre-grant the io_cli perception verbs (so an unattended
    ``claude -p`` runs them without an interactive permission prompt) and append
    the how-to as a system prompt from the per-user home. Operators can override
    the whole thing per roster entry via ``cli_cmd``.
    """
    if driver == "codex":
        return "codex exec --json {message}"
    grant = ",".join(_perception_allow_rules(io_cli))
    prompt_file = f"{home}/{_AGENT_PROMPT_BASENAME}"
    return (
        f"claude --allowed-tools '{grant}' "
        f"--append-system-prompt-file {prompt_file} -p {{message}}"
    )


def agent_home_files(home: str, *, driver: str, io_cli: str = _IO_CLI) -> dict[str, str]:
    """Per-user files seeded into the agent home before spawn (pure: path→content).

    Always seeds the perception how-to (referenced by ``--append-system-prompt-file``
    for claude, and read as ``AGENTS.md`` by codex). For claude it also writes a
    ``settings.json`` under ``CLAUDE_CONFIG_DIR`` whose ``permissions.allow``
    pre-authorizes the io_cli command (defense-in-depth alongside the CLI flag).
    """
    files = {f"{home}/{_AGENT_PROMPT_BASENAME}": _AGENT_PROMPT_TEXT}
    if driver == "codex":
        files[f"{home}/codex-home/AGENTS.md"] = _AGENT_PROMPT_TEXT
    else:
        settings = {"permissions": {"allow": _perception_allow_rules(io_cli)}}
        files[f"{home}/claude-home/settings.json"] = json.dumps(settings, indent=2)
    return files


def consumer_env(base_env: dict, entry: dict, *, user_id: str, home: str) -> dict:
    """Build the env for a per-user resident-consumer child.

    Sets the resident consumer's contract (AGENT_MODE/AGENT_CLI_CMD + per-user
    checkpoint/session/image paths) and the provider key (plaintext — the
    supervisor decrypts the envelope via the enclave before spawn). FEEDLING_API_URL
    / FEEDLING_ENCLAVE_URL flow through from ``base_env``. ``base_env`` is not
    mutated.
    """
    driver = (entry.get("driver") or "claude").strip().lower()
    env = dict(base_env)
    env["FEEDLING_API_KEY"] = entry["api_key"]
    env["AGENT_MODE"] = entry.get("agent_mode", "cli")
    env["AGENT_CLI_CMD"] = entry.get("cli_cmd") or _default_cli_cmd(driver, home)
    # Per-user isolation: separate checkpoint, agent session, image temp dir, and
    # a per-user agent home (Claude/Codex) so nothing is shared across users.
    env["CHECKPOINT_FILE"] = f"{home}/checkpoint.json"
    env["AGENT_SESSION_FILE"] = f"{home}/agent-session.txt"
    env["IMAGE_TEMP_DIR"] = f"{home}/images"
    env["CONSUMER_ID"] = f"agent-runner:{user_id}"
    # Stage D: the consumer reads its short-lived runtime token from this file
    # (refreshed by the supervisor). Absent/empty → it falls back to the api key.
    env["FEEDLING_RUNTIME_TOKEN_FILE"] = runtime_token_path(home)
    if driver == "codex":
        env["CODEX_HOME"] = f"{home}/codex-home"
        if entry.get("provider_key"):
            env["CODEX_API_KEY"] = entry["provider_key"]
    else:
        env["CLAUDE_CONFIG_DIR"] = f"{home}/claude-home"
        if entry.get("provider_key"):
            env["ANTHROPIC_API_KEY"] = entry["provider_key"]
    return env


# ---- process strategy (default) ----


def _signal_alive(pid: int) -> bool:
    """Best-effort liveness for a pid we don't own a handle for. NOTE: on POSIX
    this returns True for an unreaped zombie — only a fallback for pids not in a
    ProcessSpawner's registry (e.g. after a supervisor restart)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal_kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class ProcessSpawner:
    """Default isolation: resident consumer as a child process per user, reaped
    via the Popen handle.

    Keeping the ``Popen`` and using ``poll()`` avoids the zombie trap:
    ``os.kill(pid, 0)`` succeeds for a zombie, so a crashed/idle-exited consumer
    would otherwise look alive forever and never be respawned. ``poll()`` reaps
    the child and reports the real exit.
    """

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}

    def register(self, proc: subprocess.Popen) -> int:
        self._procs[proc.pid] = proc
        return proc.pid

    def spawn(self, entry: dict, user_id: str, home: str) -> int:
        driver = (entry.get("driver") or "claude").strip().lower()
        for path, content in agent_home_files(home, driver=driver).items():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        env = consumer_env(os.environ, entry, user_id=user_id, home=home)
        return self.register(subprocess.Popen([sys.executable, _RESIDENT_CONSUMER], env=env))

    def is_alive(self, pid: int) -> bool:
        proc = self._procs.get(pid)
        if proc is None:
            return _signal_alive(pid)
        return proc.poll() is None  # poll() reaps; None means still running

    def kill(self, pid: int) -> None:
        proc = self._procs.get(pid)
        if proc is None:
            _signal_kill(pid)
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._procs.pop(pid, None)


# ---- container strategy (opt-in strong isolation) ----

_CONSUMER_ENV_KEYS = (
    "FEEDLING_API_KEY", "FEEDLING_API_URL", "FEEDLING_ENCLAVE_URL",
    "AGENT_MODE", "AGENT_CLI_CMD", "CHECKPOINT_FILE", "AGENT_SESSION_FILE",
    "IMAGE_TEMP_DIR", "CONSUMER_ID", "FEEDLING_RUNTIME_TOKEN_FILE",
    "ANTHROPIC_API_KEY", "CODEX_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
)


def build_container_argv(entry: dict, *, user_id: str, home: str, image: str) -> list[str]:
    """`docker run` argv for a per-user, strongly-isolated resident consumer.

    Secrets pass by env-var *reference* (``-e KEY``, value inherited from the
    supervisor's environment via ``consumer_env``) so they never appear as
    plaintext argv. One named container + one named volume per user — no shared
    home.
    """
    env = consumer_env({}, entry, user_id=user_id, home="/agent-data")
    argv = [
        "docker", "run", "-d",
        "--name", f"feedling-agent-{user_id}",
        "--restart", "unless-stopped",
        "-v", f"feedling-agent-vol-{user_id}:/agent-data",
    ]
    for key in _CONSUMER_ENV_KEYS:
        if key in env:
            argv += ["-e", key]
    argv += [image, "python", "-u", "tools/chat_resident_consumer.py"]
    return argv


def get_spawner(kind: str):
    """Return (spawn_fn, alive_fn, kill_fn) bound to one shared ProcessSpawner.

    'process' (default) is the v1 path. 'container' falls back to process until
    the container lifecycle is finished — see docs/AGENT_RUNTIME_ISOLATION.md.
    """
    if kind == "container":
        log.warning("isolation=container not yet wired for live spawn; using process strategy")
    sp = ProcessSpawner()
    return sp.spawn, sp.is_alive, sp.kill
