"""
Task 8 — VPS 目录实时注入
=========================

Pure-function + monkeypatched coverage for
``_prepend_io_cli_capability_catalog`` (tools/chat_resident_consumer.py):

  - hosted gate: byte-identical passthrough, build_catalog never called
  - http-backend gate: same passthrough (AGENT_MODE != "cli")
  - cli mode, resume-capable driver (claude/pi/hermes): injects on the first
    turn of a session, skips on a later turn of the SAME session
  - codex (no --resume): injects every turn regardless of session id
  - build_catalog returning None: no injection, no cache, retried next turn
  - injection-point invariant: the recent-chat transcript header from
    ``_foreground_agent_message`` stays topmost even when the catalog was
    injected earlier in the same compose chain

No real subprocesses run in these tests — ``io_cli_catalog.build_catalog`` is
monkeypatched everywhere.

Run with: pytest tests/test_consumer_capability_inject.py -v
"""

import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars before the module is imported.
# Mirrors tests/test_chat_resident_consumer.py EXACTLY (double-import trap:
# some other test files do `import chat_resident_consumer` with `tools/` on
# sys.path directly, which creates a SECOND module object with its own copy
# of every module-level global — `import tools.chat_resident_consumer as crc`
# is the form this file must use to share state with that suite).
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_capability_inject_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
# io_cli_catalog is imported lazily inside _prepend_io_cli_capability_catalog
# as a bare sibling import (consumer's real entrypoint runs as
# `python tools/chat_resident_consumer.py` with tools/ on sys.path[0]) — put
# tools/ on sys.path here too so that lazy import resolves under pytest, and
# so this test file can import the SAME module object to monkeypatch it.
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)
import io_cli_catalog  # noqa: E402  (after sys.path setup; same object crc imports lazily)


@pytest.fixture(autouse=True)
def _reset_capability_catalog_state(monkeypatch):
    """Module-level cache + session-dedup tracker are process-global; reset
    before every test so one test's injection decision never leaks into the
    next. Also defaults the gate open (self-hosted CLI) — individual tests
    override what they need."""
    monkeypatch.setattr(crc, "_HOSTED", False)
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "claude -p {message}")
    crc._io_cli_catalog_cache = None
    crc._io_cli_catalog_injected_session_id = None
    yield
    crc._io_cli_catalog_cache = None
    crc._io_cli_catalog_injected_session_id = None


def _mock_build_catalog(monkeypatch, *, return_value=None, side_effect=None):
    calls = []

    def _fake(io_cli_path, python=sys.executable):
        calls.append((io_cli_path, python))
        if side_effect is not None:
            return side_effect(len(calls))
        return return_value

    monkeypatch.setattr(io_cli_catalog, "build_catalog", _fake)
    return calls


# ---------------------------------------------------------------------------
# Gate: hosted / http-backend — byte-identical passthrough
# ---------------------------------------------------------------------------

def test_hosted_gate_returns_content_unchanged_and_never_builds(monkeypatch):
    monkeypatch.setattr(crc, "_HOSTED", True)
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG")

    result = crc._prepend_io_cli_capability_catalog("hello there")

    assert result == "hello there"
    assert calls == []


def test_http_backend_gate_returns_content_unchanged_and_never_builds(monkeypatch):
    monkeypatch.setattr(crc, "_HOSTED", False)
    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG")

    result = crc._prepend_io_cli_capability_catalog("hello there")

    assert result == "hello there"
    assert calls == []


# ---------------------------------------------------------------------------
# CLI mode, resume-capable driver (claude/pi/hermes) — once per session
# ---------------------------------------------------------------------------

def test_cli_mode_first_turn_injects(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    result = crc._prepend_io_cli_capability_catalog("user turn 1")

    assert result == "CATALOG_TEXT\n\nuser turn 1"
    assert len(calls) == 1
    assert crc._io_cli_catalog_cache == "CATALOG_TEXT"
    assert crc._io_cli_catalog_injected_session_id == "sess-1"


def test_cli_mode_same_session_second_turn_does_not_reinject(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    first = crc._prepend_io_cli_capability_catalog("user turn 1")
    second = crc._prepend_io_cli_capability_catalog("user turn 2")

    assert first == "CATALOG_TEXT\n\nuser turn 1"
    assert second == "user turn 2"  # unchanged — already injected this session
    assert len(calls) == 1  # build_catalog was not called a second time


def test_cli_mode_session_change_reinjects(monkeypatch):
    """A rotated / brand-new session id is a distinct key — inject again."""
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    first = crc._prepend_io_cli_capability_catalog("turn 1")

    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-2")
    second = crc._prepend_io_cli_capability_catalog("turn 2")

    assert first == "CATALOG_TEXT\n\nturn 1"
    assert second == "CATALOG_TEXT\n\nturn 2"
    # Cache is reused across the session change (no need to rebuild) — only
    # the dedup key changed, not the io_cli surface itself.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# codex (no --resume) — every turn, session id irrelevant
# ---------------------------------------------------------------------------

def test_codex_injects_every_turn(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "codex exec {message}")

    def _boom():
        raise AssertionError("_load_agent_session_id must not be consulted for codex")

    monkeypatch.setattr(crc, "_load_agent_session_id", _boom)
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    first = crc._prepend_io_cli_capability_catalog("turn 1")
    second = crc._prepend_io_cli_capability_catalog("turn 2")

    assert first == "CATALOG_TEXT\n\nturn 1"
    assert second == "CATALOG_TEXT\n\nturn 2"
    # Catalog build is still cached (no repeated subprocess work) even though
    # every turn re-injects it into the prompt.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# build_catalog -> None: skip this turn, no cache, retry next turn
# ---------------------------------------------------------------------------

def test_build_failure_skips_injection_without_caching_and_retries_next_turn(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    results = iter([None, "CATALOG_TEXT"])
    calls = _mock_build_catalog(monkeypatch, side_effect=lambda n: next(results))

    first = crc._prepend_io_cli_capability_catalog("turn 1")
    assert first == "turn 1"  # unchanged — build failed
    assert crc._io_cli_catalog_cache is None  # failure never cached
    assert crc._io_cli_catalog_injected_session_id is None  # not marked injected

    second = crc._prepend_io_cli_capability_catalog("turn 1 retry")
    assert second == "CATALOG_TEXT\n\nturn 1 retry"  # retried and succeeded
    assert crc._io_cli_catalog_cache == "CATALOG_TEXT"
    assert crc._io_cli_catalog_injected_session_id == "sess-1"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Injection-order invariant: transcript header (from _foreground_agent_message)
# stays topmost even when the catalog is injected earlier in the chain.
# ---------------------------------------------------------------------------

def test_transcript_header_stays_topmost_when_catalog_also_injected(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")
    monkeypatch.setattr(crc, "_foreground_history_injection_enabled", lambda *a, **kw: True)
    monkeypatch.setattr(
        crc, "_recent_chat_context_for_foreground", lambda before_ts, limit=None: "u: hi\na: hello"
    )

    content = "current user turn"
    # Mirrors the real call-site ordering in _process_messages:
    #   _prepend_time_anchor_foreground -> _prepend_io_cli_capability_catalog
    #   -> _foreground_agent_message
    # Time anchor is orthogonal to this invariant, so it is omitted here and
    # the two functions under test are chained directly.
    content = crc._prepend_io_cli_capability_catalog(content)
    assert content.startswith("CATALOG_TEXT")

    content = crc._foreground_agent_message(content, current_ts=1000.0)

    assert content.startswith(crc.FOREGROUND_CHAT_CONTEXT_HEADER)
    assert crc._message_has_injected_history(content)
    header_idx = content.index(crc.FOREGROUND_CHAT_CONTEXT_HEADER)
    catalog_idx = content.index("CATALOG_TEXT")
    assert header_idx == 0
    assert catalog_idx > header_idx  # catalog block sits below the transcript header
    assert content.endswith("current user turn")
