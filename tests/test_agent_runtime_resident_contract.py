"""Contract test: spawners.consumer_env ↔ tools/chat_resident_consumer.py.

The agent-runner hosts the VPS resident consumer by handing it an env built by
``spawners.consumer_env``. That env is only correct if the *names* it sets are
the ones the resident consumer actually reads, and if the resident's cli-mode
command builder turns the default ``AGENT_CLI_CMD`` into a valid ``claude``
invocation (with json output + session resume). Those two modules live apart
(backend/ vs tools/), so nothing else pins this seam — this test does.

Run as a subprocess: the resident module reads its config at import scope and
logs a startup line, so importing it in-process would pollute the suite. Pure
unit (no Postgres) — the child only renders argv, it never hits network/DB.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parent.parent

# Child script: build the hosted env via consumer_env, apply it, fake a `claude`
# on PATH, import the resident consumer, and emit the rendered cli argv + the
# config it derived from the env.
_CHILD = textwrap.dedent(
    """
    import json, os, sys, importlib.util
    from pathlib import Path

    repo = Path(sys.argv[1])
    home = Path(sys.argv[2]); home.mkdir(parents=True, exist_ok=True)
    fakebin = Path(sys.argv[3]); fakebin.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(repo / "backend" / "agent_runtime"))
    import spawners
    env = spawners.consumer_env(
        {"PATH": os.environ["PATH"], "FEEDLING_API_URL": "http://localhost:5001"},
        {"api_key": "k-abc", "driver": "claude", "provider_key": "sk-ant-xxx"},
        user_id="u1", home=str(home),
    )
    for k, v in env.items():
        os.environ[k] = v

    fc = fakebin / "claude"; fc.write_text("#!/bin/sh\\necho '{}'\\n"); fc.chmod(0o755)
    os.environ["PATH"] = f"{fakebin}:{os.environ['PATH']}"

    spec = importlib.util.spec_from_file_location(
        "resident", str(repo / "tools" / "chat_resident_consumer.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    turn1 = m._prepare_cli_command("hello world")[1:]   # drop resolved abspath
    m._save_agent_session_id("sess-123")
    turn2 = m._prepare_cli_command("second message")[1:]

    print(json.dumps({
        "agent_mode": m.AGENT_MODE,
        "agent_cli_cmd": m.AGENT_CLI_CMD,
        "checkpoint": str(m.CHECKPOINT_FILE),
        "session_template": m.AGENT_SESSION_FILE_TEMPLATE,
        "image_dir": str(m.IMAGE_TEMP_DIR),
        "consumer_id": m.CONSUMER_ID,
        "verify_ping_reply": m.VERIFY_PING_REPLY,
        "turn1": turn1,
        "turn2": turn2,
    }))
    """
)


def _run_child(tmp_path) -> dict:
    home = tmp_path / "home"
    fakebin = tmp_path / "bin"
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(REPO), str(home), str(fakebin)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"child failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_hosted_env_names_match_resident_and_are_per_user(tmp_path):
    out = _run_child(tmp_path)
    home = str(tmp_path / "home")
    # The resident consumer read exactly the names spawners.consumer_env sets.
    assert out["agent_mode"] == "cli"
    assert out["checkpoint"] == f"{home}/checkpoint.json"
    assert out["session_template"] == f"{home}/agent-session.txt"
    assert out["image_dir"] == f"{home}/images"
    assert out["consumer_id"] == "agent-runner:u1"
    # verify-ping handling is inherited from resident "for free".
    assert out["verify_ping_reply"] == "__verify_ack__"
    # A1-lite: the default claude command pre-grants the io_cli perception tool
    # and appends the how-to from the per-user home; resident still owns {message}.
    cmd = out["agent_cli_cmd"]
    assert cmd.startswith("claude ")
    assert "--allowed-tools" in cmd and "io_cli.py perception" in cmd
    assert f"--append-system-prompt-file {home}/agent-tools-prompt.md" in cmd
    assert cmd.endswith("-p {message}")


def _index(seq, val):
    return seq.index(val)


# Stage D slice 3b: the consumer authenticates with the runtime-token file when
# present (dropping the api key), else stays on X-API-Key.
_AUTH_CHILD = textwrap.dedent(
    """
    import json, os, sys, importlib.util
    from pathlib import Path

    repo = Path(sys.argv[1]); home = Path(sys.argv[2]); home.mkdir(parents=True, exist_ok=True)
    write_token = sys.argv[3]   # "" → no token file

    sys.path.insert(0, str(repo / "backend" / "agent_runtime"))
    import spawners
    env = spawners.consumer_env(
        {"PATH": os.environ["PATH"], "FEEDLING_API_URL": "http://localhost:5001"},
        {"api_key": "k-longterm", "driver": "claude", "provider_key": "sk"},
        user_id="u1", home=str(home),
    )
    for k, v in env.items():
        os.environ[k] = v
    if write_token:
        Path(env["FEEDLING_RUNTIME_TOKEN_FILE"]).write_text(write_token)

    spec = importlib.util.spec_from_file_location("resident", str(repo / "tools" / "chat_resident_consumer.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    print(json.dumps(dict(m._HEADERS)))
    """
)


def _run_auth_child(tmp_path, token: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _AUTH_CHILD, str(REPO), str(tmp_path / "home"), token],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"child failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _mint(ttl: float, *, now: float | None = None) -> str:
    sys.path.insert(0, str(REPO / "backend"))
    from core import runtime_token
    return runtime_token.mint(b"secret", user_id="u1", runtime_instance_id="ri",
                              scope=["chat"], ttl=ttl, now=now)


def test_consumer_uses_fresh_runtime_token_when_file_present(tmp_path):
    headers = _run_auth_child(tmp_path, _mint(ttl=3600))  # exp well in the future
    assert headers.get("X-Feedling-Runtime-Token")
    assert "X-API-Key" not in headers  # long-term key not sent while a fresh token exists


def test_consumer_falls_back_to_api_key_when_token_expired(tmp_path):
    # Stage D bug #3: a stale token file (supervisor stopped refreshing) must NOT
    # wedge the consumer on an expired token — it falls back to the api key.
    import time as _t
    expired = _mint(ttl=10, now=_t.time() - 600)  # exp ~590s in the past
    headers = _run_auth_child(tmp_path, expired)
    assert headers.get("X-API-Key") == "k-longterm"
    assert "X-Feedling-Runtime-Token" not in headers


def test_consumer_keeps_api_key_when_no_token_file(tmp_path):
    headers = _run_auth_child(tmp_path, "")
    assert headers.get("X-API-Key") == "k-longterm"
    assert "X-Feedling-Runtime-Token" not in headers


def test_default_claude_cli_renders_json_and_resumes_session(tmp_path):
    out = _run_child(tmp_path)
    home = str(tmp_path / "home")
    # First turn: no stored session → json output, the message, the tool grant +
    # appended prompt all survive resident's command massaging, and no --resume.
    t1 = out["turn1"]
    assert "--resume" not in t1
    assert t1[_index(t1, "--output-format") + 1] == "json"
    assert "--allowed-tools" in t1
    assert t1[_index(t1, "--append-system-prompt-file") + 1] == f"{home}/agent-tools-prompt.md"
    assert t1[-2:] == ["-p", "hello world"]
    # Later turn: stored session id is injected as --resume (before the message).
    t2 = out["turn2"]
    assert t2[_index(t2, "--resume") + 1] == "sess-123"
    assert _index(t2, "--resume") < _index(t2, "-p")
    assert t2[-2:] == ["-p", "second message"]
