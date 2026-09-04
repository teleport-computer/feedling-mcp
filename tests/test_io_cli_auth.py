"""The independent resident io_cli uses only its account API key."""

import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

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


def _foreground_context_limit_from_fresh_import(configured: str | None) -> int:
    env = os.environ.copy()
    if configured is None:
        env.pop("FEEDLING_FOREGROUND_CHAT_CONTEXT_LIMIT", None)
    else:
        env["FEEDLING_FOREGROUND_CHAT_CONTEXT_LIMIT"] = configured
    env["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [str(ROOT / "tools"), str(ROOT / "backend"), env.get("PYTHONPATH")],
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import chat_resident_consumer as resident; "
            "print(resident.FOREGROUND_CHAT_CONTEXT_LIMIT)",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip().splitlines()[-1])


def test_foreground_chat_context_limit_defaults_to_50_messages():
    assert _foreground_context_limit_from_fresh_import(None) == 50


def test_foreground_chat_context_limit_honors_environment_override():
    assert _foreground_context_limit_from_fresh_import("12") == 12


@pytest.mark.parametrize(
    ("requested_limit", "expected_count", "expected_fetch_limit", "first_message"),
    [
        (60, 50, 54, "message-10"),
        (0, 1, 20, "message-59"),
    ],
)
def test_foreground_chat_context_limit_clamps_both_bounds(
    monkeypatch,
    requested_limit,
    expected_count,
    expected_fetch_limit,
    first_message,
):
    history = [
        {"role": "user", "content": f"message-{index}", "ts": index + 1}
        for index in range(60)
    ]
    fetch_limits = []

    def _fake_history(*, since, limit, include_image_body):
        assert since == 0
        assert include_image_body is False
        fetch_limits.append(limit)
        return history

    monkeypatch.setattr(resident, "get_decrypted_history", _fake_history)
    monkeypatch.setattr(
        resident,
        "_clean_messages_for_proactive_context",
        lambda messages: messages,
    )
    monkeypatch.setattr(
        resident,
        "_chat_context_line",
        lambda message, **_kwargs: message["content"],
    )

    context = resident._recent_chat_context_for_foreground(
        before_ts=0,
        limit=requested_limit,
    )

    assert fetch_limits == [expected_fetch_limit]
    assert context.splitlines() == [
        f"message-{index}"
        for index in range(60 - expected_count, 60)
    ]
    assert context.splitlines()[0] == first_message


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

_RESIDENT_LOCALIZATION_CASES = (
    (
        "resident_consumer_stale",
        "你的 VPS resident consumer 版本可能太旧或没有正常接走任务，请更新并重启。",
        "Your VPS resident consumer may be out of date, or it is not picking "
        "up tasks properly. Please update it and restart.",
        116,
        "ac8ed38dce0480c19fd7e8fc02e7980777d188327d45eb5aa1a69c8d3ead2a01",
    ),
    (
        "resident_decrypt_source_unavailable",
        "你的 VPS resident 解密源不可用，真实加密消息暂时无法回复。",
        "The decryption source on your VPS resident is unavailable, so "
        "encrypted messages cannot be answered for now.",
        108,
        "e4da5940813becd4d113a0bab1f2272639d0182b2bd70546002de4b25b89ddae",
    ),
    (
        "resident_decrypt_health_unreported",
        "你的 VPS resident 端没有上报可验证的解密健康状态,通常是 consumer 版本太旧,请更新并重启。",
        "Your VPS resident has not reported a verifiable decryption health "
        "status. This usually means the consumer is out of date. Please update "
        "it and restart.",
        151,
        "34c109beabb508e011b075bcb162f07c5f637f3718e99b32c1bd2fa0715644f7",
    ),
    (
        "resident_never_claimed",
        "你的 VPS resident consumer 长时间没有接走入住/记忆蒸馏任务，请更新并重启。",
        "Your VPS resident consumer has not picked up onboarding or memory "
        "distillation tasks for a long time. Please update it and restart.",
        131,
        "ef856ba6a5747ddd824d4512f87dc445616bdb0aca5a65edde5131ea0effd034",
    ),
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


@pytest.mark.parametrize(
    ("code", "zh", "en", "en_length", "en_sha256"),
    _RESIDENT_LOCALIZATION_CASES,
)
def test_resident_localization_preserves_zh_and_supplies_exact_en(
    code, zh, en, en_length, en_sha256
):
    spec = error_contract.require_spec(code)

    assert spec.text("en") == en
    assert spec.text("en") != zh
    assert len(spec.text("en")) == en_length
    assert spec.text("en").isascii()
    assert hashlib.sha256(spec.text("en").encode()).hexdigest() == en_sha256
    assert spec.text("zh") == zh
    assert spec.text("") == zh

    if code == "resident_decrypt_health_unreported":
        assert spec.text("zh")[31] == "\u002c"
        assert spec.text("zh")[49] == "\u002c"


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
