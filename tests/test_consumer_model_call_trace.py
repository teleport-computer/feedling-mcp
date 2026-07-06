"""
Tests for Task 6 DebugConsole M1 consumer additions in
tools/chat_resident_consumer.py:
- `agent.model.call.start` / `.done` / `.error` events around the CLI subprocess
  boundary in `call_agent_cli` (LLM in/out + stall visibility).

Run with: pytest tests/test_consumer_model_call_trace.py -v
"""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — same pattern as tests/test_chat_resident_consumer.py.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


def _recorder():
    calls = []

    def _fake_emit(subsystem, type, *, status="ok", summary="", explain="",
                   detail=None, content_excerpt=None, trace_id="", dur_ms=None):
        calls.append({
            "subsystem": subsystem, "type": type, "status": status,
            "summary": summary, "explain": explain, "detail": detail,
            "content_excerpt": content_excerpt, "trace_id": trace_id,
            "dur_ms": dur_ms,
        })

    return calls, _fake_emit


def test_call_agent_cli_emits_start_then_done(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None: ["mycli", "ask", message])

    result = subprocess.CompletedProcess(
        args=["mycli", "ask", "hi"], returncode=0,
        stdout='{"type":"result","duration_ms":10}', stderr="",
    )
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    crc.call_agent_cli("hi", trace_id="trace-123")

    types_seen = [c["type"] for c in calls]
    assert types_seen == ["agent.model.call.start", "agent.model.call.done"]

    start, done = calls
    assert start["trace_id"] == "trace-123"
    assert start["content_excerpt"] == {"prompt_head": "hi"}

    assert done["trace_id"] == "trace-123"
    assert done["status"] == "ok"
    assert done["dur_ms"] is not None
    assert done["detail"]["driver"] == "claude"
    assert done["detail"]["rc"] == 0
    assert done["content_excerpt"]["reply_head"] == result.stdout
    assert done["content_excerpt"]["stderr_head"] == ""


def test_call_agent_cli_sets_trace_id_env_for_io_cli(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None: ["mycli", "ask", message])

    result = subprocess.CompletedProcess(
        args=["mycli", "ask", "hi"], returncode=0,
        stdout='{"type":"result","duration_ms":10}', stderr="",
    )
    seen = {}

    def _fake_run(*a, **kw):
        seen["env"] = kw.get("env")
        return result

    monkeypatch.setattr(crc.subprocess, "run", _fake_run)
    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    crc.call_agent_cli("hi", trace_id="trace-env")

    assert seen["env"]["FEEDLING_TRACE_ID"] == "trace-env"
    assert seen["env"]["FEEDLING_DEBUG_TRACE_ID"] == "trace-env"
    assert calls[0]["trace_id"] == "trace-env"


def test_call_agent_cli_emits_error_on_nonzero_rc(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None: ["mycli", "ask", message])

    result = subprocess.CompletedProcess(
        args=["mycli", "ask", "hi"], returncode=1, stdout="", stderr="boom",
    )
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    # call_agent_cli raises RuntimeError on rc != 0 (existing behavior,
    # untouched) — the error event must still have been emitted first.
    with pytest.raises(RuntimeError):
        crc.call_agent_cli("hi", trace_id="trace-err")

    types_seen = [c["type"] for c in calls]
    assert types_seen == ["agent.model.call.start", "agent.model.call.error"]
    assert calls[1]["status"] == "error"
    assert calls[1]["trace_id"] == "trace-err"


def test_call_agent_cli_emits_error_on_timeout_and_reraises(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None: ["mycli", "ask", message])

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["mycli", "ask", "hi"], timeout=120)

    monkeypatch.setattr(crc.subprocess, "run", _raise_timeout)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    with pytest.raises(subprocess.TimeoutExpired):
        crc.call_agent_cli("hi", trace_id="trace-timeout")

    types_seen = [c["type"] for c in calls]
    assert types_seen == ["agent.model.call.start", "agent.model.call.error"]
    error_event = calls[1]
    assert error_event["status"] == "error"
    assert error_event["trace_id"] == "trace-timeout"
    assert "超时" in error_event["explain"]


def test_call_agent_threads_trace_id_to_cli(monkeypatch):
    """`call_agent` in cli mode must pass trace_id through to call_agent_cli."""
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    seen = {}

    def _fake_cli(message, image_paths=None, raw_text=False, trace_id=""):
        seen["trace_id"] = trace_id
        return "ok reply"

    monkeypatch.setattr(crc, "call_agent_cli", _fake_cli)

    crc.call_agent("hello", trace_id="trace-thread")

    assert seen["trace_id"] == "trace-thread"


def test_call_agent_http_accepts_trace_id_without_error(monkeypatch):
    """http path must not choke on the new trace_id kwarg (accept+ignore)."""
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    monkeypatch.setattr(crc, "call_agent_http", lambda message, images=None, raw_text=False: "ok")

    result = crc.call_agent("hello", trace_id="trace-http")
    assert result is not None
