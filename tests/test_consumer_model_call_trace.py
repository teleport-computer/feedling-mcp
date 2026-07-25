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
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["mycli", "ask", message], None))

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
    assert done["detail"]["thinking_present"] is False
    assert done["detail"]["thinking_source"] == ""
    assert done["detail"]["thinking_len"] == 0
    assert done["content_excerpt"]["reply_head"] == result.stdout
    assert done["content_excerpt"]["stderr_head"] == ""


def test_call_agent_cli_done_trace_carries_thinking_observation(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["mycli", "ask", message], None))

    stdout = "\n".join([
        '{"type":"assistant","message":{"role":"assistant","model":"claude-sonnet-4-5",'
        '"content":[{"type":"thinking","thinking":"I inspected the latest prompt."}]}}',
        '{"type":"assistant","message":{"role":"assistant","model":"claude-sonnet-4-5",'
        '"content":[{"type":"text","text":"hi"}]}}',
    ])
    result = subprocess.CompletedProcess(
        args=["mycli", "ask", "hi"], returncode=0, stdout=stdout, stderr="",
    )
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    crc.call_agent_cli("hi", trace_id="trace-thinking")

    done = calls[1]
    assert done["type"] == "agent.model.call.done"
    assert done["detail"]["thinking_present"] is True
    assert done["detail"]["thinking_source"] == "anthropic_thinking"
    assert done["detail"]["thinking_len"] == len("I inspected the latest prompt.")


def test_call_agent_cli_warns_when_claude_stdout_has_unparsed_thinking_marker(monkeypatch, caplog):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["mycli", "ask", message], None))

    stdout = "\n".join([
        '{"type":"stream_event","event":{"type":"content_block_start","index":0,'
        '"content_block":{"type":"thinking","thinking":"","signature":""}}}',
        '{"type":"assistant","message":{"role":"assistant","model":"claude-sonnet-4-5",'
        '"content":[{"type":"text","text":"hi"}]}}',
    ])
    result = subprocess.CompletedProcess(
        args=["mycli", "ask", "hi"], returncode=0, stdout=stdout, stderr="",
    )
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    with caplog.at_level("WARNING", logger=crc.log.name):
        crc.call_agent_cli("hi", trace_id="trace-marker")

    assert "claude stdout had thinking markers but parser yielded none" in caplog.text
    assert calls[1]["detail"]["thinking_present"] is False


def test_call_agent_cli_sets_trace_id_env_for_io_cli(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["mycli", "ask", message], None))

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
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["mycli", "ask", message], None))

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
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["mycli", "ask", message], None))

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

    def _fake_cli(
        message,
        image_paths=None,
        raw_text=False,
        trace_id="",
        lane="background",
        attempt_trigger="first",
    ):
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


def test_error_event_carries_error_detail_beyond_the_reply_head_cap(monkeypatch):
    """`reply_head` is capped at 1000 bytes and codex spends the first ~500 of
    them on two harmless notices (deprecated `[features].collab`, missing model
    metadata for the `gw-<uid>` alias) emitted before it ever contacts the model.
    What remains of the cap goes to retry chatter, so the FINAL error event — the
    one naming the cause — is cut off. Read from the top, every failure looks like
    the same two notices no matter what killed the turn; a `web_search` 400 and an
    upstream 403 have both been misdiagnosed as a "collab crash" this way.

    `_cli_error_detail` already extracts that final event for the RuntimeError
    below (the notices are nested under `item.completed`, so they never match a
    top-level `type == "error"`). Surface the same string on the trace event."""
    reconnect = ('{"type":"error","message":"Reconnecting... %d/5 (unexpected status 403 '
                 'Forbidden: litellm.APIError: APIError: OpenAIException - upstream rejected '
                 'the request for model gw-usr_deadbeefcafe0123)"}')
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"019f4716-e30b-7371-874b-b490e8ea29d3"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"error","message":'
        '"`[features].collab` is deprecated. Use `[features].multi_agent` instead. '
        '(Enable it with `--enable multi_agent` or `[features].multi_agent` in '
        'config.toml. See https://developers.openai.com/codex/config-basic for details.)"}}',
        '{"type":"item.completed","item":{"id":"item_1","type":"error","message":'
        '"Model metadata for `gw-usr_deadbeefcafe0123` not found. Defaulting to fallback '
        'metadata; this can degrade performance and cause issues."}}',
        '{"type":"turn.started"}',
        *[reconnect % i for i in range(1, 6)],
        '{"type":"error","message":"BALANCE_EXHAUSTED 预扣费额度失败, 用户剩余额度: ¥0.129786"}',
        '{"type":"turn.failed","error":{"message":"upstream 403"}}',
    ])
    assert len(stdout) > 1000, "fixture must overflow the reply_head cap"

    result = subprocess.CompletedProcess(
        args=["codex"], returncode=1, stdout=stdout, stderr="Reading additional input from stdin...\n",
    )
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'codex exec "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["codex", "exec", message], None))
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    with pytest.raises(RuntimeError):
        crc.call_agent_cli("hi", trace_id="trace-403")

    excerpt = calls[1]["content_excerpt"]

    # the excerpt opens on the notices, and the final error is past the cap
    assert "collab" in excerpt["reply_head"][:500]
    assert "BALANCE_EXHAUSTED" not in excerpt["reply_head"]

    # ...but the event now names the cause, in full, uncontaminated by the notices
    assert "BALANCE_EXHAUSTED" in excerpt["error_detail"]
    assert "¥0.129786" in excerpt["error_detail"]
    assert "collab" not in excerpt["error_detail"]


def test_error_event_extracts_nested_codex_turn_failed_detail(monkeypatch):
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"t"}',
        '{"type":"item.completed","item":{"type":"error","message":"`[features].collab` is deprecated."}}',
        '{"type":"turn.failed","error":{"message":"Invalid tool use format: web_search_options must be omitted"}}',
    ])
    result = subprocess.CompletedProcess(
        args=["codex"], returncode=1, stdout=stdout, stderr="",
    )
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'codex exec "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["codex", "exec", message], None))
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)

    with pytest.raises(RuntimeError):
        crc.call_agent_cli("hi", trace_id="trace-nested")

    excerpt = calls[1]["content_excerpt"]
    assert excerpt["error_detail"] == "Invalid tool use format: web_search_options must be omitted"
    assert "collab" not in excerpt["error_detail"]


def test_error_detail_absent_on_successful_turns(monkeypatch):
    """rc=0 has no error to surface; `_cli_error_detail` would fall back to a raw
    stdout snippet, which is noise on the `.done` event."""
    result = subprocess.CompletedProcess(
        args=["codex"], returncode=0, stdout='{"type":"agent_message","message":"hi"}', stderr="",
    )
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'codex exec "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None, lane="background": (["codex", "exec", message], None))
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: result)

    calls, fake_emit = _recorder()
    monkeypatch.setattr(crc, "_emit_debug_trace", fake_emit)
    crc.call_agent_cli("hi", trace_id="trace-ok")

    done = calls[1]
    assert done["type"] == "agent.model.call.done"
    assert "error_detail" not in (done["content_excerpt"] or {})
