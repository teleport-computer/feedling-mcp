"""Regression guards for resident CLI parse-failure evidence (T539)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
os.environ.setdefault("AGENT_MODE", "http")

from tools import chat_resident_consumer as crc


def _empty_stream(driver: str, byte_length: int = 372) -> str:
    """A non-empty, exit-zero transport stream with no deliverable reply."""
    if driver == "codex":
        first = json.dumps({"type": "thread.started", "thread_id": "t539"})
        shape = {"type": "turn.failed", "diagnostic_padding": ""}
    else:
        first = json.dumps({"type": "system", "subtype": "init", "session_id": "t539"})
        shape = {"type": "result", "subtype": "error", "diagnostic_padding": ""}
    empty = first + "\n" + json.dumps(shape, separators=(",", ":"))
    shape["diagnostic_padding"] = "x" * (byte_length - len(empty.encode("utf-8")))
    raw = first + "\n" + json.dumps(shape, separators=(",", ":"))
    assert len(raw.encode("utf-8")) == byte_length
    return raw


def _install_cli_result(monkeypatch: pytest.MonkeyPatch, driver: str, raw: str) -> None:
    cmd = (
        ["codex", "exec", "--json"]
        if driver == "codex"
        else ["claude", "-p", "--output-format", "stream-json"]
    )
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", " ".join(cmd))
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    monkeypatch.setattr(crc, "_agent_cli_cwd", lambda: None)
    monkeypatch.setattr(crc, "_agent_cli_cwd_error", "")
    monkeypatch.setattr(
        crc,
        "_prepare_cli_command",
        lambda _message, **_kwargs: (list(cmd), None),
    )
    monkeypatch.setattr(
        crc,
        "_run_cli_subprocess",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout=raw, stderr=""
        ),
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_save_agent_session_id", lambda _sid: None)
    monkeypatch.setattr(crc, "_record_agent_session_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_bind_pending_catalog_to_session", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_turn_ledger_open", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_queue_provider_attempt_ledger", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_emit_recall_completed", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_trace_user_mcp_wiring", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_trace_user_mcp_registered", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_trace_user_mcp_surface", lambda *_a, **_k: None)
    monkeypatch.setattr(crc, "_validate_claude_actual_model", lambda *_a, **_k: None)
    monkeypatch.setattr(
        crc, "_call_with_resident_busy_poll", lambda invoke, lane: invoke()
    )


@pytest.mark.parametrize("driver", ["codex", "claude"])
def test_exit_zero_empty_stream_preserves_raw_and_traces_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver: str
) -> None:
    raw = _empty_stream(driver)
    _install_cli_result(monkeypatch, driver, raw)
    monkeypatch.setattr(crc, "FEEDLING_HOME", tmp_path)
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        crc,
        "_emit_debug_trace",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = crc.call_agent("hello", lane="chat", trace_id="msg_t539")

    assert result == [crc.FALLBACK_REPLY]
    assert crc._consume_reply_parse_failed() == "reply_parse_failed"
    files = list((tmp_path / "reply-parse-failures").glob("*.raw"))
    assert len(files) == 1
    assert files[0].read_bytes() == raw.encode("utf-8")
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() in files[0].name
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700

    parse_events = [item for item in events if item[0][1] == "agent.reply.parse_failed"]
    assert len(parse_events) == 1
    _args, kwargs = parse_events[0]
    assert "content_excerpt" not in kwargs
    assert set(kwargs["detail"]) == {
        "raw_bytes",
        "raw_sha256",
        "exit_code",
        "driver",
        "parse_empty_stage",
        "captured_at",
        "raw_preview",
        "local_path",
    }
    assert kwargs["detail"]["raw_bytes"] == 372
    assert kwargs["detail"]["raw_sha256"] == hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()
    assert kwargs["detail"]["exit_code"] == 0
    assert kwargs["detail"]["driver"] == driver
    assert kwargs["detail"]["parse_empty_stage"] == f"{driver}_stream_sanitization"
    assert kwargs["detail"]["raw_preview"] == raw[
        : crc.REPLY_PARSE_FAILURE_PREVIEW_CHARS
    ]
    assert len(kwargs["detail"]["raw_preview"]) <= 80
    assert kwargs["detail"]["local_path"] == str(files[0])
    assert raw not in json.dumps(kwargs, ensure_ascii=False)

    from admin import data_track

    public_detail = data_track._debug_event_public_json({
        "type": "agent.reply.parse_failed",
        "detail": kwargs["detail"],
    })["detail"]
    assert public_detail["raw_preview"] == raw[:80]
    assert public_detail["local_path"] == str(files[0])

    widened = dict(kwargs["detail"], raw_preview=raw[:81])
    assert data_track._debug_event_public_json({
        "type": "agent.reply.parse_failed",
        "detail": widened,
    })["detail"] == {}


def test_parse_failure_artifacts_apply_per_file_count_and_total_caps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(crc, "FEEDLING_HOME", tmp_path)
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_MAX_BYTES", 5)
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_MAX_FILES", 2)
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_TOTAL_BYTES", 100)
    monkeypatch.setattr(crc, "_emit_debug_trace", lambda *_a, **_k: None)

    for index in range(3):
        path = crc._preserve_reply_parse_failure(
            f"raw-{index}-long",
            cmd=["codex", "exec", "--json"],
            exit_code=0,
            parse_empty_stage="codex_stream",
            trace_id=f"msg_{index}",
        )
        assert path is not None
        os.utime(path, ns=(index + 1, index + 1))

    files = list((tmp_path / "reply-parse-failures").glob("*.raw"))
    assert len(files) == 2
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_TOTAL_BYTES", 8)
    crc._rotate_reply_parse_failures(tmp_path / "reply-parse-failures")
    files = list((tmp_path / "reply-parse-failures").glob("*.raw"))
    assert len(files) == 1
    assert sum(path.stat().st_size for path in files) <= 8
    assert all(path.stat().st_size <= 5 for path in files)
    assert any(path.read_bytes() == b"raw-2" for path in files)


def test_concurrent_parse_failures_keep_rotation_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(crc, "FEEDLING_HOME", tmp_path)
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_MAX_BYTES", 100)
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_MAX_FILES", 4)
    monkeypatch.setattr(crc, "REPLY_PARSE_FAILURE_TOTAL_BYTES", 400)
    monkeypatch.setattr(crc, "_emit_debug_trace", lambda *_a, **_k: None)
    gate = threading.Barrier(8)

    def _write(index: int) -> None:
        gate.wait()
        crc._preserve_reply_parse_failure(
            f"concurrent-raw-{index}",
            cmd=["claude", "-p"],
            exit_code=0,
            parse_empty_stage="claude_stream",
            trace_id=f"concurrent_{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(8)))

    files = list((tmp_path / "reply-parse-failures").glob("*.raw"))
    assert len(files) == 4
    assert sum(path.stat().st_size for path in files) <= 400
    assert all(path.read_text().startswith("concurrent-raw-") for path in files)


@pytest.mark.parametrize(
    ("driver", "raw"),
    [
        (
            "codex",
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        ),
        (
            "claude",
            '{"type":"result","subtype":"success","result":"ok"}',
        ),
    ],
)
def test_successful_stream_does_not_create_parse_failure_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver: str, raw: str
) -> None:
    _install_cli_result(monkeypatch, driver, raw)
    monkeypatch.setattr(crc, "FEEDLING_HOME", tmp_path)
    monkeypatch.setattr(crc, "_emit_debug_trace", lambda *_a, **_k: None)

    result = crc.call_agent("hello", lane="chat", trace_id="msg_success")

    assert result["messages"] == ["ok"]
    assert not (tmp_path / "reply-parse-failures").exists()
