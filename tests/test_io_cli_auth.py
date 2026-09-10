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


# ---------------------------------------------------------------------------
# T511 — V1 recall observability: per-turn ledger + memory.recall.completed.
# Lives here (not tests/test_chat_resident_consumer.py) because this file is in
# the CI executed set and already imports both io_cli and the consumer.
# ---------------------------------------------------------------------------


def _ledger_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_turn_ledger_records_only_memory_read_verbs(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("FEEDLING_TURN_LEDGER", str(path))
    monkeypatch.setattr(io_cli, "_LAST_TOOL_OUTPUT", {"ok": True, "items": [1, 2, 3]})
    io_cli._append_turn_ledger(types.SimpleNamespace(verb="memory-index", query="杯盖"), 0)
    io_cli._append_turn_ledger(types.SimpleNamespace(verb="memory-index", query=None), 0)
    io_cli._append_turn_ledger(types.SimpleNamespace(verb="memory-fetch"), 0)
    io_cli._append_turn_ledger(types.SimpleNamespace(verb="web-search"), 0)
    rows = _ledger_lines(path)
    assert [r["tool"] for r in rows] == ["memory-index", "memory-index", "memory-fetch"]
    assert [r["query"] for r in rows] == [True, False, False]
    assert all(r["items"] == 3 and r["ok"] is True and r["exit"] == 0 for r in rows)
    # Content never leaks into the ledger: only tool name and counts.
    assert not any("杯盖" in line for line in path.read_text(encoding="utf-8").splitlines())


def test_turn_ledger_is_silent_without_env_and_never_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("FEEDLING_TURN_LEDGER", raising=False)
    monkeypatch.setattr(io_cli, "_LAST_TOOL_OUTPUT", {"ok": True, "items": []})
    io_cli._append_turn_ledger(types.SimpleNamespace(verb="memory-index", query=None), 0)
    assert not list(tmp_path.iterdir())
    # Unwritable path must not break the tool call either.
    monkeypatch.setenv("FEEDLING_TURN_LEDGER", str(tmp_path / "missing-dir" / "ledger.jsonl"))
    io_cli._append_turn_ledger(types.SimpleNamespace(verb="memory-fetch"), 0)


def test_recall_counts_unknown_without_ledger_is_null_not_zero():
    counts, unknown = resident._recall_counts_from_ledger(None)
    assert set(counts) == set(resident._RECALL_LEDGER_KEYS)
    assert all(v is None for v in counts.values())
    assert unknown == list(resident._RECALL_LEDGER_KEYS)


def test_recall_counts_fold_ledger_rows():
    ok = {"ok": True, "exit": 0}
    rows = [
        {"tool": "memory-index", "query": False, "items": 94, **ok},
        {"tool": "memory-index", "query": True, "items": 0, **ok},
        {"tool": "memory-index", "query": True, "items": 2, **ok},
        {"tool": "memory-fetch", "items": 3, **ok},
        {"tool": "memory-fetch", "items": 1, **ok},
        {"tool": "web-search", "items": 5, **ok},
    ]
    counts, unknown = resident._recall_counts_from_ledger(rows)
    assert unknown == []
    assert counts == {
        "index_calls": sum(1 for r in rows if r["tool"] == "memory-index" and not r["query"]),
        "search_calls": sum(1 for r in rows if r["tool"] == "memory-index" and r["query"]),
        "empty_searches": sum(
            1 for r in rows
            if r["tool"] == "memory-index" and r["query"] and r["ok"] and r["items"] == 0
        ),
        "fetch_cards": sum(r["items"] for r in rows if r["tool"] == "memory-fetch" and r["ok"]),
    }


def _capture_debug_traces(monkeypatch):
    calls = []

    def fake_emit(subsystem, type, **kw):
        calls.append({"subsystem": subsystem, "type": type, **kw})

    monkeypatch.setattr(resident, "_emit_debug_trace", fake_emit)
    return calls


def test_select_trace_says_selected_not_injected(monkeypatch):
    calls = _capture_debug_traces(monkeypatch)
    log = {"mode": "bucketed:unified", "counts": {"injected": 4, "candidate_pool": 41}, "dur_ms": 3}
    resident._emit_injection_trace(log)
    assert [c["type"] for c in calls] == ["memory.select.traced"]
    # The old name claimed an injection that never reached the prompt (T510).
    assert not any(c["type"] == "memory.inject" for c in calls)
    ev = calls[0]
    assert ev["subsystem"] == "memory"
    assert "已选 4 张" in ev["summary"] and "注入 4 张" not in ev["summary"]
    assert ev["detail"]["arrival_evidence"] == "memory.context.applied"
    assert "injected_to_prompt" not in ev["detail"]
    assert ev["detail"]["counts"] == log["counts"]


def test_recall_completed_reports_ledger_counts_and_resets(monkeypatch, tmp_path):
    calls = _capture_debug_traces(monkeypatch)
    child_env = {}
    resident._turn_ledger_open(child_env)
    path = child_env["FEEDLING_TURN_LEDGER"]
    assert os.path.exists(path) and resident._turn_ledger_path == path
    rows = [
        {"tool": "memory-index", "query": False, "items": 20, "ok": True, "exit": 0},
        {"tool": "memory-index", "query": True, "items": 0, "ok": True, "exit": 0},
        {"tool": "memory-fetch", "items": 2, "ok": True, "exit": 0},
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    resident._RECALL_TURN_STATE.update({"selected": 5, "candidate_pool": 41, "quoted": 1})

    resident._emit_recall_completed(trace_id="tr_1", driver="pi", lane="chat")

    assert [c["type"] for c in calls] == ["memory.recall.completed"]
    ev = calls[0]
    assert ev["subsystem"] == "memory" and ev["trace_id"] == "tr_1"
    d = ev["detail"]
    assert d["runtime"] == "v1" and d["driver"] == "pi" and d["lane"] == "chat"
    assert d["counts"] == {
        "injected": 0,
        "selected": 5,
        "index_calls": 1,
        "search_calls": 1,
        "empty_searches": 1,
        "fetch_cards": 2,
    }
    assert d["quoted_memories"] == 1 and d["unknown"] == ["job_id"] and d["source"] == "turn_ledger"
    assert d["turn_id"] == "tr_1" and d["job_id"] is None and d["lane_raw"] == "chat"
    assert "注入0" in ev["summary"] and "已选5" in ev["summary"] and "?" not in ev["summary"]
    # Turn state and ledger are consumed exactly once.
    assert not os.path.exists(path) and resident._turn_ledger_path is None
    assert {k: resident._RECALL_TURN_STATE[k] for k in ("selected", "candidate_pool", "quoted")} == {"selected": None, "candidate_pool": None, "quoted": 0}


def test_recall_completed_marks_missing_ledger_as_unknown(monkeypatch):
    calls = _capture_debug_traces(monkeypatch)
    resident._turn_ledger_close()
    resident._RECALL_TURN_STATE.update({"selected": None, "candidate_pool": None, "quoted": 0})

    resident._emit_recall_completed(trace_id="tr_2", driver="codex", lane="chat")

    assert [c["type"] for c in calls] == ["memory.recall.completed"]
    d = calls[0]["detail"]
    assert d["counts"]["injected"] == 0
    assert all(d["counts"][k] is None for k in resident._RECALL_LEDGER_KEYS)
    assert d["counts"]["selected"] is None
    assert d["unknown"] == ["selected", *resident._RECALL_LEDGER_KEYS, "job_id"]
    assert "?" in calls[0]["summary"]


@pytest.mark.parametrize(
    "rows, expect_counts, expect_unknown",
    [
        # fetch succeeded but reported no integer count → fetch_cards unknown, not 0
        (
            [{"tool": "memory-fetch", "ok": True, "exit": 0, "items": None}],
            {"index_calls": 0, "search_calls": 0, "empty_searches": 0, "fetch_cards": None},
            ["fetch_cards"],
        ),
        # failed search: it is a call, but never an "empty search"
        (
            [{"tool": "memory-index", "query": True, "ok": False, "exit": 1, "items": 0}],
            {"index_calls": 0, "search_calls": 1, "empty_searches": 0, "fetch_cards": 0},
            [],
        ),
        # successful search without a valid count → empty_searches unknown
        (
            [{"tool": "memory-index", "query": True, "ok": True, "exit": 0, "items": "3"}],
            {"index_calls": 0, "search_calls": 1, "empty_searches": None, "fetch_cards": 0},
            ["empty_searches"],
        ),
        # failed fetch contributes nothing and does not poison the count
        (
            [
                {"tool": "memory-fetch", "ok": False, "exit": 1, "items": None},
                {"tool": "memory-fetch", "ok": True, "exit": 0, "items": 2},
            ],
            {"index_calls": 0, "search_calls": 0, "empty_searches": 0, "fetch_cards": 2},
            [],
        ),
    ],
)
def test_recall_counts_trust_only_successful_integer_counts(rows, expect_counts, expect_unknown):
    counts, unknown = resident._recall_counts_from_ledger(rows)
    assert counts == expect_counts
    assert unknown == expect_unknown


def test_turn_ledger_with_a_bad_line_is_unknown_not_partial(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"tool": "memory-index", "query": False, "ok": True, "exit": 0, "items": 3})
        + "\n{not json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(resident, "_turn_ledger_path", str(path))
    assert resident._turn_ledger_read() is None
    path.write_text("[1, 2]\n", encoding="utf-8")
    assert resident._turn_ledger_read() is None
    path.write_bytes(b"\xff\xfe not utf8\n")
    assert resident._turn_ledger_read() is None
    path.write_text("", encoding="utf-8")
    assert resident._turn_ledger_read() == []
    resident._turn_ledger_path = None


def test_recall_completed_maps_lane_and_carries_job_id(monkeypatch):
    calls = _capture_debug_traces(monkeypatch)
    resident._turn_ledger_close()
    resident._emit_recall_completed(trace_id="tr_3", driver="claude", lane="heartbeat", job_id="job_9")
    d = calls[0]["detail"]
    assert d["lane"] == "wake" and d["lane_raw"] == "heartbeat"
    assert d["turn_id"] == "tr_3" and d["job_id"] == "job_9" and calls[0]["job_id"] == "job_9"
    assert "job_id" not in d["unknown"]
    assert resident._recall_lane("chat") == "chat" and resident._recall_lane("background") == "wake"


def test_terminal_without_started_turn_only_cleans_up(monkeypatch):
    calls = _capture_debug_traces(monkeypatch)
    child_env = {}
    resident._turn_ledger_open(child_env)
    path = child_env["FEEDLING_TURN_LEDGER"]
    resident._RECALL_TURN_STATE.update({"selected": 2, "candidate_pool": 9, "quoted": 1})
    resident._emit_cli_model_call_terminal({"started": False}, trace_id="tr_4", succeeded=True)
    assert calls == []
    assert not os.path.exists(path) and resident._turn_ledger_path is None
    assert {k: resident._RECALL_TURN_STATE[k] for k in ("selected", "candidate_pool", "quoted")} == {"selected": None, "candidate_pool": None, "quoted": 0}


def test_recall_completed_emits_even_when_terminal_trace_raises(monkeypatch):
    calls = []

    def flaky_emit(subsystem, type, **kw):
        if type.startswith("agent.model.call."):
            raise RuntimeError("boom")
        calls.append({"subsystem": subsystem, "type": type, **kw})

    monkeypatch.setattr(resident, "_emit_debug_trace", flaky_emit)
    child_env = {}
    resident._turn_ledger_open(child_env)
    path = child_env["FEEDLING_TURN_LEDGER"]
    context = {"started": True, "started_at": 0.0, "cmd": ["pi"], "result": None, "lane": "chat"}
    resident._emit_cli_model_call_terminal(context, trace_id="tr_5", succeeded=False,
                                           failure=RuntimeError("driver failed"))
    assert [c["type"] for c in calls] == ["memory.recall.completed"]
    assert not os.path.exists(path) and resident._turn_ledger_path is None


# ---------------------------------------------------------------------------
# T512 — V1 per-turn automatic memory injection (enclave picks → prompt block).
# ---------------------------------------------------------------------------


def _picked(*cards):
    return [dict(id=c[0], text=c[1], bucket=c[2], reason="", matched=list(c[3]), score=c[4]) for c in cards]


def test_stash_auto_memories_is_unknown_without_context_memories():
    assert resident._stash_auto_memories(None, {"counts": {"injected": 3}}) is None
    assert resident._stash_auto_memories("nope", None) is None
    assert resident._stash_auto_memories([], None) == []


def test_stash_auto_memories_joins_cards_with_selection_reasons():
    cards = [{"id": "m1", "summary": "露营灯保修码 NP-4286", "bucket": "生活"},
             {"id": "m2", "content": "只有正文的卡", "bucket": "b"}, {"id": "m3", "description": "旧形状描述", "content": "正文"},
             {"id": "", "summary": "no id"}, "junk"]
    trace = {"selected": [
        {"id": "m1", "bucket": "query", "reason": "phrase_match", "matched_phrases": ["露营灯", "保修码"], "score": 0.72},
    ], "rejected_sample": []}
    picked = resident._stash_auto_memories(cards, trace)
    # legacy log shape (selection_trace nested) still works
    assert resident._stash_auto_memories(cards, {"selection_trace": trace})[0]["score"] == 0.72
    # a card with only a body is NOT rendered (the block never carries bodies);
    # legacy description still is.
    assert [c["id"] for c in picked] == ["m1", "m3"]
    assert picked[0]["bucket"] == "query" and picked[0]["matched"] == ["露营灯", "保修码"] and picked[0]["score"] == 0.72
    assert picked[1]["text"] == "旧形状描述" and picked[1]["score"] == 0.0
    assert all("正文" not in c["text"] for c in picked)


def test_auto_memory_context_orders_by_score_skips_quoted_and_keeps_ids_only():
    picked = _picked(("low", "低分卡", "recent", [], 0.1), ("hi", "高分卡 保修码", "query", ["保修码"], 0.9),
                     ("q", "用户已引用的卡", "query", [], 0.8))
    text, ids = resident._auto_memory_context(picked, ["q"])
    assert ids == ["hi", "low"]
    assert text.startswith("相关记忆(") and "memory-fetch" in text
    assert text.index("(id=hi)") < text.index("(id=low)")
    assert "用户已引用的卡" not in text and "(id=q)" not in text
    assert "匹配「保修码」" in text and "最近记下" in text


def test_auto_memory_context_drops_whole_cards_over_budget_never_slices():
    big = "很长的摘要" * 80  # > AUTO_MEMORY_SUMMARY_MAX_CHARS → shown as id + note only, never cut
    picked = _picked(("a", big, "query", [], 0.9), ("b", "短卡二", "recent", [], 0.5), ("c", "短卡三", "recent", [], 0.4))
    text, ids = resident._auto_memory_context(picked, [], budget_chars=260)
    # the over-long card keeps its id with a "fetch" note; the rest are whole-card drops
    for mid in ids:
        assert f"(id={mid})" in text
    assert "…" not in text and "很长的摘要很长" not in text
    assert resident.AUTO_MEMORY_TOO_LONG_NOTE in text
    assert set(ids) <= {"a", "b", "c"} and ids == sorted(ids, key=lambda m: -{"a": 0.9, "b": 0.5, "c": 0.4}[m])
    assert len(text) <= 260
    assert resident._auto_memory_context(picked, [], budget_chars=10) == ("", [])
    assert resident._auto_memory_context(None, []) == ("", []) and resident._auto_memory_context([], []) == ("", [])


def test_recall_completed_reports_real_injection_from_turn_state(monkeypatch):
    calls = _capture_debug_traces(monkeypatch)
    resident._turn_ledger_close()
    resident._RECALL_TURN_STATE.update({"selected": 4, "candidate_pool": 40, "quoted": 0,
                                        "injected": 2, "injected_ids": ["m1", "m9", "m3"], "injected_chars": 321,
                                        "arrived_ids": ["m1", "m9"], "missing_ids": ["m3"], "arrival_channel": "stdin", "driver_request": "prepared"})
    resident._emit_recall_completed(trace_id="tr_6", driver="pi", lane="chat")
    d = calls[0]["detail"]
    assert d["counts"]["injected"] == 2 and d["injected_ids"] == ["m1", "m9"] and d["missing_ids"] == ["m3"]
    assert d["rendered_ids"] == ["m1", "m9", "m3"] and d["injected_chars"] == 321 and d["arrival_channel"] == "stdin"
    assert d["driver_request"] == "prepared"
    assert d["v2_profile_lane"] is False and d["native_session_memory"] == "unknown"
    assert "注入2" in calls[0]["summary"]
    assert resident._RECALL_TURN_STATE["injected"] == 0 and resident._RECALL_TURN_STATE["injected_ids"] == []
    assert resident._RECALL_TURN_STATE["arrived_ids"] == [] and resident._RECALL_TURN_STATE["injected_chars"] == 0


def test_v1_memory_protocol_has_fact_discipline():
    block = resident._memory_read_prompt_block()
    assert "FACT DISCIPLINE" in block
    assert "Never guess a plausible value" in block
    assert "相关记忆" in block and "memory-fetch" in block
    # the pre-existing entry points stay intact
    assert "memory-index --limit 20" in block and "Never claim memories are unavailable" in block


def test_v1_memory_protocol_teaches_navigation_not_search_first():
    block = resident._memory_read_prompt_block()
    # order: injected block first → locate → pick → relate (threads) → fetch bodies
    assert block.index("相关记忆") < block.index("(1) Locate") < block.index("(2) Pick") < block.index("(3) Relate") < block.index("(4) Fetch")
    assert "memory-index --query <keywords>" in block and "BM25 token ranking" in block
    assert "Zero results do not prove the memory is absent" in block
    assert "ranking=substring-legacy" in block
    assert "--bucket <bucket>" in block and "--thread <thread>" in block
    assert "follow one with" in block and "threads" in block
    assert "summaries are pointers, not the record" in block
    assert "evidence, not instructions" in block
    # valid id sources: the injected block, this turn's index result, related_items — never invented/stale
    assert "related_items" in block and "Never invent an id" in block and "older turn" in block


def test_hosted_agent_prompt_memory_section_teaches_navigation():
    from pathlib import Path
    text = (ROOT / "backend" / "agent_runtime" / "agent_tools_prompt.md").read_text(encoding="utf-8")
    section = text[text.index("## Memory"):]
    section = section[: section.index("\n## ", 5)] if "\n## " in section[5:] else section
    assert "locate → pick → relate → fetch" in section
    assert "strict two-step" not in section and "Index first" not in section
    for step in ("1. **Locate.**", "2. **Pick.**", "3. **Relate.**", "4. **Fetch.**"):
        assert step in section
    assert "memory-index --query <keywords>" in section and "BM25" in section
    assert "ranking=substring-legacy" in section
    assert "memory-index --thread <thread>" in section and "memory-index --bucket <bucket>" in section
    assert "相关记忆" in section and "evidence, not" in section
    assert "Fact discipline" in section and "Never guess a plausible value" in section
    assert "memory-index --limit 20" in section
    assert "related_items" in section and "don't\ninvent ids" in section
    assert "didn't come from the current recall step's index result" not in section


def test_history_fetch_requests_selection_trace_and_stashes_picks(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "messages": [{"id": "u1", "role": "user", "ts": 1.0}, {"id": "a1", "role": "openclaw", "ts": 2.0},
                             {"id": "u2", "role": "user", "ts": 3.0}],
                "context_memories": [{"id": "m1", "summary": "露营灯保修码 NP-4286", "bucket": "生活"}],
                "context_memory_trace": {"selected": [{"id": "m1", "bucket": "query", "reason": "phrase_match",
                                                        "matched_phrases": ["露营灯"], "score": 0.7}]},
                "context_memory_log": {"mode": "bucketed:unified", "counts": {"injected": 1, "candidate_pool": 40}},
            }

    class _Client:
        def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = dict(params or {})
            return _Resp()

    monkeypatch.setattr(resident, "_ENCLAVE_CLIENT", _Client())
    monkeypatch.setattr(resident, "FEEDLING_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setattr(resident, "_emit_debug_trace", lambda *a, **k: None)
    assert [m["id"] for m in resident._fetch_from_enclave(0.0, 20)] == ["u1", "a1", "u2"]
    assert captured["url"].endswith("/v1/chat/history")
    assert captured["params"].get("context_trace") == "1"
    # the main poll never binds picks to the turn; the per-turn fetch does
    assert resident._RECALL_TURN_STATE["injected_ids"] == []
    resident._recall_turn_reset()


def _page(*msgs):
    return [{"id": i, "role": r, "ts": float(n)} for n, (i, r) in enumerate(msgs, 1)]


def test_arrival_reports_zero_when_block_was_squeezed_out(monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(resident, "AGENT_CLI_CMD", "pi --mode json")
    calls = []
    monkeypatch.setattr(resident, "_emit_debug_trace", lambda subsystem, type, **kw: calls.append({"type": type, **kw}))
    monkeypatch.setattr(resident, "_prepare_cli_command", lambda msg, **kw: (["pi", "--mode", "json"], None))
    monkeypatch.setattr(resident.subprocess, "run", lambda *a, **kw: _sp.CompletedProcess(args=[], returncode=0, stdout='{"type":"result","duration_ms":5}', stderr=""))
    resident._recall_turn_reset()
    resident._RECALL_TURN_STATE.update({"injected_ids": ["m1"], "rendered_lines": {"m1": "- (id=m1) 露营灯保修码 · 与这句相关"}, "rendered_header": "相关记忆(测试):"})
    try:
        resident.call_agent_cli("只有用户的话，块被挤掉了 (id=m1)", trace_id="tr_sq", lane="chat")
    except Exception:  # noqa: BLE001 — see above
        pass
    arrived = [c for c in calls if c["type"] == "memory.context.applied"][0]
    assert arrived["detail"]["arrived"] == 0 and arrived["detail"]["missing_ids"] == ["m1"] and arrived["detail"]["chars"] == 0
    done = [c for c in calls if c["type"] == "memory.recall.completed"][0]
    assert done["detail"]["counts"]["injected"] == 0 and done["detail"]["rendered_ids"] == ["m1"] and done["detail"]["injected_chars"] == 0




class _TurnEnclave:
    """Fake enclave for the per-turn selection fetch: records params, answers per before_seq."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(dict(params or {}))
        body = self.pages.get(int(params["before_seq"]))

        class _R:
            status_code = 200

            def raise_for_status(self_inner):
                if body is None:
                    raise RuntimeError("boom")

            def json(self_inner):
                return body
        return _R()


def _turn_page(msg_id, seq, cards, mode="bucketed:unified"):
    return {"messages": [{"id": f"a{seq-1}", "role": "openclaw", "seq": seq - 1}, {"id": msg_id, "role": "user", "seq": seq}],
            "context_memories": cards,
            "context_memory_trace": {"selected": [{"id": c["id"], "bucket": "query", "score": 0.9, "matched_phrases": ["x"]} for c in cards]},
            "context_memory_log": {"mode": mode, "counts": {"injected": len(cards), "candidate_pool": 7}}}


def test_per_turn_fetch_binds_each_message_to_its_own_page(monkeypatch):
    fake = _TurnEnclave({
        11: _turn_page("u10", 10, [{"id": "m1", "summary": "卡一"}]),
        13: _turn_page("u12", 12, [{"id": "m2", "summary": "卡二"}]),
    })
    monkeypatch.setattr(resident, "_ENCLAVE_CLIENT", fake)
    monkeypatch.setattr(resident, "FEEDLING_ENCLAVE_URL", "http://enclave.test")
    calls = _capture_debug_traces(monkeypatch)
    resident._recall_turn_reset()
    t1, ids1 = resident._auto_memory_block_for({"id": "u10", "seq": 10}, "tr")
    assert ids1 == ["m1"] and "卡一" in t1 and resident._RECALL_TURN_STATE["selected"] == 1
    assert resident._RECALL_TURN_STATE["candidate_pool"] == 7 and resident._RECALL_TURN_STATE["rendered_lines"]["m1"].startswith("- (id=m1)")
    t2, ids2 = resident._auto_memory_block_for({"id": "u12", "seq": 12}, "tr")
    assert ids2 == ["m2"] and "卡二" in t2 and "卡一" not in t2
    # both messages of one poll got their own selection, with the exact page each
    assert [c["before_seq"] for c in fake.calls] == [11, 13]
    assert all(c["limit"] == resident.AUTO_MEMORY_TURN_PAGE and c["context_trace"] == "1" and c["include_image_body"] == "false" for c in fake.calls)
    assert [c["type"] for c in calls].count("context.auto_memory") == 2
    resident._recall_turn_reset()


@pytest.mark.parametrize("msg, page, expect_selected, expect_ids", [
    ({"id": "u1"}, None, None, []),                                                     # no seq → unknown, no fetch
    ({"id": "u1", "seq": 0}, None, None, []),                                           # non-positive seq → unknown
    ({"id": "u1", "seq": True}, None, None, []),                                        # bool is not a seq
    ({"id": "", "seq": 5}, _turn_page("u1", 5, [{"id": "m1", "summary": "x"}]), None, []),  # missing id → unknown
    ({"id": "u1", "seq": 5}, {"messages": [], "context_memories": [{"id": "m1", "summary": "x"}], "context_memory_log": {"mode": "ok"}}, None, []),  # empty page → unknown
    ({"id": "u1", "seq": 5}, {"messages": [{"id": "a4", "role": "openclaw", "seq": 4}], "context_memories": [{"id": "m1", "summary": "x"}], "context_memory_log": {"mode": "ok"}}, None, []),  # no user row → unknown
    ({"id": "u1", "seq": 5}, _turn_page("u1", 5, [], mode="failed"), None, []),          # failed mode → unknown
    ({"id": "u1", "seq": 5}, _turn_page("u1", 5, []), 0, []),                            # healthy, nothing picked → 0
    ({"id": "u1", "seq": 5}, _turn_page("u9", 5, [{"id": "m1", "summary": "x"}]), None, []),  # page ends at another user msg → unknown
    ({"id": "u1", "seq": 5}, {"messages": [], "context_memory_log": {"mode": "ok"}}, None, []),  # no context_memories field → unknown
])
def test_per_turn_fetch_unknown_vs_zero(monkeypatch, msg, page, expect_selected, expect_ids):
    fake = _TurnEnclave({6: page} if page is not None else {})
    monkeypatch.setattr(resident, "_ENCLAVE_CLIENT", fake)
    monkeypatch.setattr(resident, "FEEDLING_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setattr(resident, "_emit_debug_trace", lambda *a, **k: None)
    resident._recall_turn_reset()
    text, ids = resident._auto_memory_block_for(msg, "tr")
    assert (text, ids) == ("", expect_ids)
    assert resident._RECALL_TURN_STATE["selected"] == expect_selected
    if not isinstance(msg.get("seq"), int) or isinstance(msg.get("seq"), bool) or msg.get("seq", 0) <= 0 or not msg.get("id"):
        assert fake.calls == []
    resident._recall_turn_reset()


def test_per_turn_fetch_transport_failure_is_unknown_not_zero(monkeypatch):
    fake = _TurnEnclave({})  # every before_seq → raise
    monkeypatch.setattr(resident, "_ENCLAVE_CLIENT", fake)
    monkeypatch.setattr(resident, "FEEDLING_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setattr(resident, "_emit_debug_trace", lambda *a, **k: None)
    resident._recall_turn_reset()
    assert resident._auto_memory_block_for({"id": "u1", "seq": 3}, "tr") == ("", [])
    assert resident._RECALL_TURN_STATE["selected"] is None and fake.calls[0]["before_seq"] == 4
    resident._recall_turn_reset()


def test_selected_counts_enclave_picks_but_rendered_skips_body_only_cards(monkeypatch):
    page = _turn_page("u1", 5, [{"id": "m1", "content": "只有正文"}, {"id": "m2", "summary": "有摘要"}])
    fake = _TurnEnclave({6: page})
    monkeypatch.setattr(resident, "_ENCLAVE_CLIENT", fake)
    monkeypatch.setattr(resident, "FEEDLING_ENCLAVE_URL", "http://enclave.test")
    calls = _capture_debug_traces(monkeypatch)
    resident._recall_turn_reset()
    text, ids = resident._auto_memory_block_for({"id": "u1", "seq": 5}, "tr")
    assert ids == ["m2"] and "只有正文" not in text
    assert resident._RECALL_TURN_STATE["selected"] == 2
    d = calls[-1]["detail"]
    assert d["selected"] == 2 and d["renderable"] == 1 and d["rendered"] == 1
    resident._recall_turn_reset()


def test_arrival_header_only_is_not_injection(monkeypatch):
    calls = _capture_debug_traces(monkeypatch)
    resident._recall_turn_reset()
    resident._RECALL_TURN_STATE.update({"injected_ids": ["m1"], "rendered_lines": {"m1": "- (id=m1) 卡 · 可能相关"}, "rendered_header": "相关记忆(测试):"})
    resident._auto_memory_arrival("相关记忆(测试):\n\n用户的话", "stdin", driver="pi", trace_id="tr")
    d = calls[-1]["detail"]
    assert d["arrived"] == 0 and d["chars"] == 0 and resident._RECALL_TURN_STATE["injected"] == 0
    resident._recall_turn_reset()
    resident._RECALL_TURN_STATE.update({"injected_ids": ["m1"], "rendered_lines": {"m1": "- (id=m1) 卡 · 可能相关"}, "rendered_header": "相关记忆(测试):"})
    block = "相关记忆(测试):\n- (id=m1) 卡 · 可能相关"
    resident._auto_memory_arrival(block + "\n\n用户的话", "stdin", driver="pi", trace_id="tr")
    assert calls[-1]["detail"]["chars"] == len(block) and resident._RECALL_TURN_STATE["injected_chars"] == len(block)
    resident._recall_turn_reset()


# ---------------------------------------------------------------------------
# T513 #5 — optional retrieval_cues pass-through into the sealed card body.
# ---------------------------------------------------------------------------


def test_retrieval_cues_normalized_bounded_and_deduped():
    raw = ["  露营灯 ", "露营灯", "", None, "保修码 NP-4286", "x" * 300, "帐篷", "睡袋", "第七条不要"]
    out = resident._normalize_retrieval_cues(raw)
    assert out[:2] == ["露营灯", "保修码 NP-4286"]
    assert len(out) == resident.RETRIEVAL_CUES_MAX
    assert all(len(c) <= resident.RETRIEVAL_CUE_CHARS for c in out) and "x" * resident.RETRIEVAL_CUE_CHARS in out
    assert "第七条不要" not in out
    assert resident._normalize_retrieval_cues(None) == [] and resident._normalize_retrieval_cues("露营灯") == []
    assert resident._normalize_retrieval_cues({"a": 1}) == []
    # strictly list[str]: objects, numbers and bools are skipped, never stringified
    assert resident._normalize_retrieval_cues([{"k": "v"}, ["x"], 7, 3.5, True, False, "只留我"]) == ["只留我"]


def test_capture_inner_keeps_legacy_shape_without_cues_and_adds_them_when_present():
    base = {"summary": " 露营灯的小档案 ", "content": "保修码是 NP-4286。", "bucket": "生活", "threads": ["露营灯"], "importance": 0.7}
    inner = resident._capture_inner_from_card(base)
    assert inner == {"summary": "露营灯的小档案", "content": "保修码是 NP-4286。", "bucket": "生活", "threads": ["露营灯"]}
    with_cues = resident._capture_inner_from_card({**base, "retrieval_cues": ["保修码", "NP-4286", "保修码"]}, voice_call_id="vc1")
    assert with_cues["retrieval_cues"] == ["保修码", "NP-4286"] and with_cues["voice_call_id"] == "vc1"
    # a non-list / empty cues field must not create the key
    assert "retrieval_cues" not in resident._capture_inner_from_card({**base, "retrieval_cues": []})
    assert "retrieval_cues" not in resident._capture_inner_from_card({**base, "retrieval_cues": "保修码"})


def test_auto_memory_context_shows_whole_summaries_up_to_the_limit():
    """haoxuan (T529): a cut summary ("他一开始想辞职…") reads as a different fact.
    Up to the limit the summary is shown whole; over it, id + note only."""
    whole = "他一开始想辞职，后来跟他妈聊完就打消了。" * 8   # 160 chars: was cut at 120 before
    assert 120 < len(whole) <= resident.AUTO_MEMORY_SUMMARY_MAX_CHARS
    picked = _picked(("w", whole, "query", [], 0.9))
    text, ids = resident._auto_memory_context(picked, [])
    assert ids == ["w"] and whole in text and "…" not in text
