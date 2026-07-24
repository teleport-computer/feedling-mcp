"""The independent resident io_cli uses only its account API key."""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_auth_headers_prefers_api_key(monkeypatch):
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    assert io_cli._auth_headers() == {"X-API-Key": "k"}


def test_auth_headers_empty_without_api_key(monkeypatch):
    monkeypatch.delenv("FEEDLING_API_KEY", raising=False)
    assert io_cli._auth_headers() == {}


def test_emit_tool_trace_posts_agent_tool_call_with_redacted_args(monkeypatch):
    calls = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "trace-1")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")

    def _fake_http(method, url, auth, *, payload=None, insecure=False, timeout=30):
        calls.append({
            "method": method, "url": url, "auth": auth, "payload": payload,
            "insecure": insecure, "timeout": timeout,
        })
        return 200, {"status": "ok"}

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)
    args = types.SimpleNamespace(
        verb="memory-index",
        limit=5,
        query="where was i yesterday",
        bucket="places",
        thread="",
        ambient=False,
        include_sensitive=False,
        func=lambda _args: None,
    )

    io_cli._emit_tool_trace(args, 0, 12.34)

    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://backend.test/v1/debug/trace/event"
    assert calls[0]["auth"] == {"X-API-Key": "k"}
    assert calls[0]["timeout"] == 1.0
    event = calls[0]["payload"]["event"]
    assert event["subsystem"] == "agent"
    assert event["type"] == "agent.tool.call"
    assert event["trace_id"] == "trace-1"
    assert event["turn_id"] == "trace-1"
    assert event["dur_ms"] == 12.3
    assert event["detail"] == {
        "tool": "memory-index",
        "args": {"limit": 5, "query": "<redacted chars=21>", "bucket": "places"},
        "result_status": "ok",
        "dur_ms": 12.3,
    }
    assert "where was i yesterday" not in json.dumps(event, ensure_ascii=False)


def test_emit_tool_trace_noops_without_trace_id(monkeypatch):
    calls = []
    monkeypatch.delenv("FEEDLING_TRACE_ID", raising=False)
    monkeypatch.delenv("FEEDLING_DEBUG_TRACE_ID", raising=False)
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.setattr(io_cli, "_http_json", lambda *a, **kw: calls.append((a, kw)))

    io_cli._emit_tool_trace(types.SimpleNamespace(verb="perception"), 0, 1)

    assert calls == []


def test_main_emits_tool_trace_after_command_exit(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "turn-main")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.setattr(sys, "argv", ["io_cli", "perception", "now"])

    def _fake_http(method, url, auth, *, payload=None, insecure=False, timeout=30):
        calls.append({"method": method, "url": url, "payload": payload})
        if method == "GET":
            return 200, {"snapshot": {"now": {"ok": True}}}
        return 200, {"status": "ok"}

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)

    with pytest.raises(SystemExit) as exc:
        io_cli.main()

    assert exc.value.code == 0
    stdout = json.loads(capsys.readouterr().out.strip())
    assert stdout["ok"] is True
    assert [call["method"] for call in calls] == ["GET", "POST"]
    event = calls[1]["payload"]["event"]
    assert event["type"] == "agent.tool.call"
    assert event["detail"]["tool"] == "perception"
    assert event["detail"]["args"] == {"signals": "1 item(s): now"}
    assert event["detail"]["result_status"] == "ok"
