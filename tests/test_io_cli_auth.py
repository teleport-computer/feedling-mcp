"""The independent resident io_cli uses only its account API key."""

import hashlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402

from conftest import capture_sleeps
from notices import catalog, error_contract

for key, value in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_io_cli_auth_checkpoint.json",
}.items():
    os.environ.setdefault(key, value)

import chat_resident_consumer as resident  # noqa: E402


_RESIDENT_AGENT_CLI_LOGGED_OUT_ZH = (
    "你的 VPS 上的 AI 助手登录已失效，请到 VPS 上重新登录后再试。"
)
_RESIDENT_AGENT_CLI_LOGGED_OUT_EN = (
    "Your AI assistant on the VPS is no longer signed in. Please sign in again "
    "on the VPS and try once more."
)
_RESIDENT_AGENT_CLI_AUTH_FAILURES = (
    "agent exited: Failed to authenticate: OAuth session expired and could "
    "not be refreshed",
    "agent exited: Not logged in · Please run /login",
)


def test_resident_agent_cli_logged_out_copy_is_exact_and_bilingual():
    spec = error_contract.require_spec("resident_agent_cli_logged_out")

    assert (spec.domain, spec.family, spec.blame) == (
        "resident",
        "resident",
        "user_environment",
    )
    assert spec.safe_text_zh == _RESIDENT_AGENT_CLI_LOGGED_OUT_ZH
    assert len(spec.safe_text_zh) == 37
    assert hashlib.sha256(spec.safe_text_zh.encode()).hexdigest() == (
        "8c7549f684ccf950d51d2485f1974e82d4bf24759737b70aeab287e4bc314292"
    )
    assert spec.safe_text_zh[20] == "\uff0c"
    assert spec.safe_text_zh[36] == "\u3002"
    assert "," not in spec.safe_text_zh
    assert "." not in spec.safe_text_zh

    assert spec.safe_text_en == _RESIDENT_AGENT_CLI_LOGGED_OUT_EN
    assert len(spec.safe_text_en) == 103
    assert spec.safe_text_en.isascii()
    assert hashlib.sha256(spec.safe_text_en.encode()).hexdigest() == (
        "9ae4b613fa2ba91bd37546b5485a16542315602b34075d5913332f9e8f5eedac"
    )


@pytest.mark.parametrize("detail", _RESIDENT_AGENT_CLI_AUTH_FAILURES)
def test_resident_agent_cli_auth_failures_have_specific_class(detail):
    expected_code = "resident_agent_cli_logged_out"

    assert catalog.classify_upstream(detail) == expected_code
    assert (
        resident.classify_agent_error(RuntimeError(detail)).error_class
        == expected_code
    )


@pytest.mark.parametrize("detail", ("Invalid API key", "provider_http_401"))
def test_resident_agent_cli_matcher_does_not_steal_provider_auth(detail):
    matcher_codes = [spec.code for spec in error_contract.matcher_specs()]
    assert matcher_codes.index("auth_invalid") < matcher_codes.index(
        "resident_agent_cli_logged_out"
    )
    assert catalog.classify_upstream(detail) == "auth_invalid"
    assert (
        resident.classify_agent_error(RuntimeError(detail)).error_class
        == "auth_invalid"
    )


def test_auth_headers_prefers_api_key(monkeypatch):
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    assert io_cli._auth_headers() == {"X-API-Key": "k"}


def test_auth_headers_empty_without_api_key(monkeypatch):
    monkeypatch.delenv("FEEDLING_API_KEY", raising=False)
    assert io_cli._auth_headers() == {}


def test_memory_fetch_rejects_literal_placeholder_before_request(monkeypatch, capsys):
    monkeypatch.setattr(io_cli, "_require_backend", lambda: ("http://backend.test", {}))

    def _unexpected_http(*_args, **_kwargs):
        raise AssertionError("placeholder ids must not reach the backend")

    monkeypatch.setattr(io_cli, "_http_json", _unexpected_http)
    args = types.SimpleNamespace(
        ids=["ids"],
        limit=20,
        include_archived=False,
        include_superseded=False,
    )

    with pytest.raises(SystemExit) as exc:
        io_cli.cmd_memory_fetch(args)

    assert exc.value.code == 2
    body = json.loads(capsys.readouterr().out.strip())
    assert body["ok"] is False
    assert "run memory-index first" in body["error"]


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
        func=lambda _args: None,
    )

    io_cli._emit_tool_trace(args, 0, 12.34)

    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://backend.test/v1/debug/trace/event"
    assert calls[0]["auth"] == {"X-API-Key": "k"}
    assert calls[0]["timeout"] == 5.0
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


def _capture_attachment_tool_trace(monkeypatch, *, verb, exit_code, output):
    events = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "trace-attachment")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.setattr(io_cli, "_LAST_TOOL_OUTPUT", output)
    monkeypatch.setattr(
        io_cli,
        "_http_json",
        lambda *_args, **kwargs: events.append(kwargs["payload"]["event"])
        or (200, {"status": "ok"}),
    )

    io_cli._emit_tool_trace(
        types.SimpleNamespace(
            verb=verb,
            path="/safe/test-input",
            name="result.txt" if verb == "send-file" else "result.png",
            func=lambda _args: None,
        ),
        exit_code,
        4.2,
    )

    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize(
    ("verb", "error_code"),
    [
        ("send-file", "wrong_file_suffix"),
        ("send-image", "too_many_staged_images"),
    ],
)
def test_attachment_failure_trace_keeps_fixed_rejection_code(
    monkeypatch, verb, error_code
):
    event = _capture_attachment_tool_trace(
        monkeypatch,
        verb=verb,
        exit_code=1,
        output={"ok": False, "error": error_code},
    )

    assert event["status"] == "error"
    assert event["detail"]["error_code"] == error_code


def test_attachment_failure_trace_redacts_dynamic_error_path(monkeypatch):
    sensitive_path = "/private/customer/alice/quarterly-plan.md"
    event = _capture_attachment_tool_trace(
        monkeypatch,
        verb="send-file",
        exit_code=1,
        output={
            "ok": False,
            "error": f"[Errno 13] Permission denied: '{sensitive_path}'",
        },
    )

    assert event["detail"]["error_code"] == "unclassified"
    assert sensitive_path not in json.dumps(event["detail"], ensure_ascii=False)


def test_non_attachment_failure_trace_has_no_attachment_error_code(monkeypatch):
    event = _capture_attachment_tool_trace(
        monkeypatch,
        verb="memory-index",
        exit_code=1,
        output={"ok": False, "error": "backend_unavailable"},
    )

    assert event["status"] == "error"
    assert "error_code" not in event["detail"]


def test_successful_attachment_trace_has_no_failure_noise(monkeypatch):
    event = _capture_attachment_tool_trace(
        monkeypatch,
        verb="send-file",
        exit_code=0,
        output={"ok": True, "staged": True, "name": "result.txt"},
    )

    assert event["status"] == "ok"
    assert event["detail"] == {
        "tool": "send-file",
        "args": {"path": "/safe/test-input", "name": "result.txt"},
        "result_status": "ok",
        "dur_ms": 4.2,
    }


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
    assert [call["method"] for call in calls] == ["POST", "GET", "POST", "POST"]
    assert calls[0]["payload"]["state"] == "running"
    assert calls[2]["payload"]["state"] == "success"
    event = calls[3]["payload"]["event"]
    assert event["type"] == "agent.tool.call"
    assert event["detail"]["tool"] == "perception"
    assert event["detail"]["args"] == {"signals": "1 item(s): now"}
    assert event["detail"]["result_status"] == "ok"


def test_memory_activity_metadata_uses_actual_items_and_complete_categories():
    assert io_cli._memory_activity_metadata(
        "memory_index",
        {
            "ok": True,
            "items": [
                {"id": "m1", "bucket": "我们的关系", "summary": "private"},
                {"id": "m2", "bucket": "Our relationship"},
                {"id": "m3", "bucket": "我们的关系"},
                {"id": "m4", "bucket": "家庭"},
            ],
        },
    ) == {
        "memory_count": 4,
        "memory_categories": [
            {"key": "relationship", "count": 3},
            {"key": "family", "count": 1},
        ],
    }


def test_memory_index_keeps_its_own_activity_identity():
    assert io_cli._activity_tool_name(
        types.SimpleNamespace(verb="memory-index")
    ) == "memory_index"


def test_activity_tool_name_is_generic_for_future_io_tools():
    assert io_cli._activity_tool_name(
        types.SimpleNamespace(verb="workspace-export")
    ) == "workspace_export"


def test_terminal_activity_retries_with_vps_safe_timeout(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "turn-cancel")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")

    def _fake_http(method, url, auth, *, payload=None, insecure=False, timeout=30):
        calls.append({
            "method": method,
            "url": url,
            "auth": auth,
            "payload": payload,
            "timeout": timeout,
        })
        return (-1, {"error": "timed_out"}) if len(calls) == 1 else (200, {"status": "ok"})

    monkeypatch.setattr(io_cli, "_http_json", _fake_http)
    capture_sleeps(monkeypatch, io_cli, sleeps)

    io_cli._emit_turn_activity(
        types.SimpleNamespace(verb="cancel-wake"),
        "v1:cancel-1",
        "success",
        dur_ms=42,
        exit_code=0,
    )

    assert len(calls) == 2
    assert [call["timeout"] for call in calls] == [5.0, 5.0]
    assert calls[0]["payload"]["tool_name"] == "cancel_wake"
    assert calls[0]["payload"]["state"] == "success"
    assert calls[0]["payload"]["result_code"] == "ok"
    assert sleeps == [0.15]


def test_running_activity_does_not_retry_or_delay_tool(monkeypatch):
    calls = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "turn-running")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.setattr(
        io_cli,
        "_http_json",
        lambda *args, **kwargs: calls.append((args, kwargs)) or (-1, {"error": "timed_out"}),
    )
    # 负向替身:这里被断言的性质是"根本不该睡",所以替身要抛。用 on_sleep 保住这个性质,
    # 同时不碰进程全局的 stdlib time.sleep —— 否则后台线程随便睡一下就会在这里炸,
    # 而失败信息会指着一个与被测代码无关的地方。
    capture_sleeps(
        monkeypatch,
        io_cli,
        on_sleep=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("running must not retry")
        ),
    )

    io_cli._emit_turn_activity(
        types.SimpleNamespace(verb="memory-index"),
        "v1:memory-1",
        "running",
    )

    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 2.0


def test_generate_image_activity_keeps_actionable_failure_code(monkeypatch):
    calls = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "turn-image-required")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.setattr(
        io_cli,
        "_LAST_TOOL_OUTPUT",
        {
            "ok": False,
            "http_status": 409,
            "error": {"error": "image_generation_model_required"},
        },
    )
    monkeypatch.setattr(
        io_cli,
        "_http_json",
        lambda *args, **kwargs: calls.append(kwargs["payload"])
        or (200, {"status": "ok"}),
    )

    io_cli._emit_turn_activity(
        types.SimpleNamespace(verb="generate-image"),
        "v1:image-1",
        "failure",
        exit_code=1,
    )

    assert calls[-1]["result_code"] == "image_generation_model_required"


def test_non_image_tool_failure_stays_generic(monkeypatch):
    calls = []
    monkeypatch.setenv("FEEDLING_TRACE_ID", "turn-generic-error")
    monkeypatch.setenv("FEEDLING_API_URL", "http://backend.test")
    monkeypatch.setenv("FEEDLING_API_KEY", "k")
    monkeypatch.setattr(
        io_cli,
        "_LAST_TOOL_OUTPUT",
        {"error": "image_generation_model_required"},
    )
    monkeypatch.setattr(
        io_cli,
        "_http_json",
        lambda *args, **kwargs: calls.append(kwargs["payload"])
        or (200, {"status": "ok"}),
    )

    io_cli._emit_turn_activity(
        types.SimpleNamespace(verb="workspace-export"),
        "v1:generic-1",
        "failure",
        exit_code=1,
    )

    assert calls[-1]["result_code"] == "tool_error"


def test_memory_activity_metadata_custom_bucket_falls_back_to_total():
    assert io_cli._memory_activity_metadata(
        "memory_fetch",
        {
            "ok": True,
            "items": [
                {"id": f"m{index}", "bucket": "妈妈" if index == 0 else "家庭"}
                for index in range(11)
            ],
        },
    ) == {"memory_count": 11}
