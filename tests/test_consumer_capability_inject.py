"""
Task 8 — VPS 目录实时注入
=========================

Pure-function + monkeypatched coverage for
``_prepend_io_cli_capability_catalog`` (tools/chat_resident_consumer.py):

  - hosted gate: byte-identical passthrough, build_catalog never called
  - http-backend gate: same passthrough (AGENT_MODE != "cli")
  - cli mode, resume-capable driver (claude/pi/hermes): injects on the first
    turn of a session, skips on a later turn of the SAME session — ONLY once
    the turn is confirmed committed (pending -> commit, Codex review I10)
  - codex (no --resume): injects every turn regardless of session id
  - build_catalog returning None: full catalog not injected, no cache, retried
    next turn — but the D3 sourcing guardrail (io_cli_catalog.D3_SOURCING_RULE)
    is still prepended alone (I2: it must not vanish just because the --help
    sweep failed — it's the only defense left against instructions smuggled
    through files/web pages/memory cards now that D2 confirmation is gone)
  - injection-point invariant: the recent-chat transcript header from
    ``_foreground_agent_message`` stays topmost even when the catalog was
    injected earlier in the same compose chain
  - pending -> commit / discard through the REAL foreground call site
    (``_process_messages``): a turn whose agent call fails before the model
    ever saw the prompt does not permanently skip the catalog for the rest
    of that session — the very next turn of the same session retries it

No real subprocesses run in these tests — ``io_cli_catalog.build_catalog`` is
monkeypatched everywhere.

Run with: pytest tests/test_consumer_capability_inject.py -v
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

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
    """Module-level cache + session-dedup trackers (committed + pending) are
    process-global; reset before every test so one test's injection decision
    never leaks into the next. Also defaults the gate open (self-hosted CLI)
    — individual tests override what they need."""
    monkeypatch.setattr(crc, "_HOSTED", False)
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", "claude -p {message}")
    crc._io_cli_catalog_cache = None
    crc._io_cli_catalog_injected_session_id = None
    crc._io_cli_catalog_pending_session_id = None
    yield
    crc._io_cli_catalog_cache = None
    crc._io_cli_catalog_injected_session_id = None
    crc._io_cli_catalog_pending_session_id = None
    # The one test in this file that exercises the real _process_messages
    # failure path (test_agent_call_failure_retries_injection_next_turn_*)
    # also trips _notify_agent_turn_failure's system-notice rate limiter — a
    # module-global dict keyed by error class. Clear it here too so this
    # file never leaks a stale rate-limit window into an unrelated test file
    # collected later in the same pytest process.
    crc._reset_system_notice_state()


def _mock_build_catalog(monkeypatch, *, return_value=None, side_effect=None):
    calls = []

    def _fake(io_cli_path, python=sys.executable):
        calls.append((io_cli_path, python))
        if side_effect is not None:
            return side_effect(len(calls))
        return return_value

    monkeypatch.setattr(io_cli_catalog, "build_catalog", _fake)
    return calls


def _make_msg(role="user", content="hello", ts=None):
    msg = {"role": role, "content": content}
    if ts is not None:
        msg["ts"] = ts
    return msg


def _with_download_delivery_prompt(prefix: str, content: str) -> str:
    return f"{prefix}\n{crc._outbound_file_prompt_block()}\n\n{content}"


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
# CLI mode, resume-capable driver (claude/pi/hermes) — once per session,
# gated on COMMIT (Codex review I10) not on injection itself.
# ---------------------------------------------------------------------------

def test_cli_mode_first_turn_injects_and_marks_pending_not_committed(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    result = crc._prepend_io_cli_capability_catalog("user turn 1")

    assert result == _with_download_delivery_prompt("CATALOG_TEXT", "user turn 1")
    assert "send-file" in result
    assert str(crc.OUTBOUND_FILE_DIR) in result
    assert len(calls) == 1
    assert crc._io_cli_catalog_cache == "CATALOG_TEXT"
    # Injected into the prompt, but NOT yet confirmed — the caller has not
    # told us the agent call for this turn succeeded.
    assert crc._io_cli_catalog_pending_session_id == "sess-1"
    assert crc._io_cli_catalog_injected_session_id is None


def test_commit_after_success_confirms_session_and_clears_pending(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    crc._prepend_io_cli_capability_catalog("turn 1")
    crc._commit_io_cli_catalog_injection()

    assert crc._io_cli_catalog_injected_session_id == "sess-1"
    assert crc._io_cli_catalog_pending_session_id is None


def test_discard_after_failure_clears_pending_without_committing(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    crc._prepend_io_cli_capability_catalog("turn 1")
    crc._discard_io_cli_catalog_pending_injection()

    assert crc._io_cli_catalog_injected_session_id is None  # never confirmed
    assert crc._io_cli_catalog_pending_session_id is None  # dropped, not carried over


def test_cli_mode_same_session_second_turn_does_not_reinject_after_commit(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    first = crc._prepend_io_cli_capability_catalog("user turn 1")
    crc._commit_io_cli_catalog_injection()  # turn 1's agent call succeeded
    second = crc._prepend_io_cli_capability_catalog("user turn 2")

    assert first == _with_download_delivery_prompt("CATALOG_TEXT", "user turn 1")
    assert second == "user turn 2"  # unchanged — already confirmed this session
    assert len(calls) == 1  # build_catalog was not called a second time


def test_cli_mode_same_session_second_turn_reinjects_without_commit(monkeypatch):
    """The I10 case at the unit level: turn 1 injects but is never committed
    (its agent call is presumed to have failed) — turn 2 of the SAME session
    must retry, not silently skip for the rest of the session."""
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    first = crc._prepend_io_cli_capability_catalog("user turn 1")
    # No commit call here — simulates the turn 1 agent call failing.
    second = crc._prepend_io_cli_capability_catalog("user turn 2")

    assert first == _with_download_delivery_prompt("CATALOG_TEXT", "user turn 1")
    assert second == _with_download_delivery_prompt(
        "CATALOG_TEXT", "user turn 2"
    )  # retried, not skipped
    assert len(calls) == 1  # cache still reused — no need to rebuild


def test_cli_mode_session_change_reinjects(monkeypatch):
    """A rotated / brand-new session id is a distinct key — inject again."""
    calls = _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")

    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    first = crc._prepend_io_cli_capability_catalog("turn 1")
    crc._commit_io_cli_catalog_injection()

    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-2")
    second = crc._prepend_io_cli_capability_catalog("turn 2")

    assert first == _with_download_delivery_prompt("CATALOG_TEXT", "turn 1")
    assert second == _with_download_delivery_prompt("CATALOG_TEXT", "turn 2")
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

    assert first == _with_download_delivery_prompt("CATALOG_TEXT", "turn 1")
    assert second == _with_download_delivery_prompt("CATALOG_TEXT", "turn 2")
    # Catalog build is still cached (no repeated subprocess work) even though
    # every turn re-injects it into the prompt.
    assert len(calls) == 1
    # codex never touches the pending/committed session trackers at all.
    assert crc._io_cli_catalog_pending_session_id is None
    assert crc._io_cli_catalog_injected_session_id is None


# ---------------------------------------------------------------------------
# build_catalog -> None: skip this turn, no cache, retry next turn
# ---------------------------------------------------------------------------

def test_build_failure_skips_injection_without_caching_and_retries_next_turn(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    results = iter([None, "CATALOG_TEXT"])
    calls = _mock_build_catalog(monkeypatch, side_effect=lambda n: next(results))

    first = crc._prepend_io_cli_capability_catalog("turn 1")
    # Full catalog not injected (build failed), but I2: the D3 sourcing
    # guardrail alone must still ship — it's the only defense against
    # instructions smuggled through files/web pages/memory cards now that D2
    # (confirmation) is gone, and it must not depend on the --help sweep
    # succeeding.
    assert first == _with_download_delivery_prompt(
        io_cli_catalog.D3_SOURCING_RULE, "turn 1"
    )
    assert crc._io_cli_catalog_cache is None  # failure never cached
    assert crc._io_cli_catalog_pending_session_id is None  # never even marked pending
    assert crc._io_cli_catalog_injected_session_id is None  # not confirmed

    second = crc._prepend_io_cli_capability_catalog("turn 1 retry")
    assert second == _with_download_delivery_prompt(
        "CATALOG_TEXT", "turn 1 retry"
    )  # retried and succeeded
    assert crc._io_cli_catalog_cache == "CATALOG_TEXT"
    assert crc._io_cli_catalog_pending_session_id == "sess-1"  # pending until commit
    assert crc._io_cli_catalog_injected_session_id is None  # still not confirmed
    assert len(calls) == 2


def test_build_failure_d3_fallback_present_even_when_catalog_never_built_before(monkeypatch):
    """I2 regression pin: a session whose VERY FIRST catalog build fails must
    still get the D3 sourcing guardrail on that very first turn — not just on
    a retry after a prior success. Simulates the VPS first-turn generation
    failure scenario called out in the finding."""
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-first")
    _mock_build_catalog(monkeypatch, return_value=None)

    out = crc._prepend_io_cli_capability_catalog("hello")
    assert io_cli_catalog.D3_SOURCING_RULE in out
    assert out.startswith(io_cli_catalog.D3_SOURCING_RULE)


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


# ---------------------------------------------------------------------------
# Codex review I10 — end-to-end through the REAL foreground call site
# (_process_messages): a turn whose agent call fails before the model ever
# saw the prompt must not permanently skip the catalog for the rest of that
# resume-capable session.
# ---------------------------------------------------------------------------

def test_agent_call_failure_retries_injection_next_turn_then_stops_after_commit(monkeypatch):
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess-1")
    _mock_build_catalog(monkeypatch, return_value="CATALOG_TEXT")
    # Ensure the fallback path runs (posts FALLBACK_REPLY) rather than the
    # early `continue` — irrelevant to injection, but keeps _process_messages
    # on its normal completed path for turn 1.
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    # This turn's real failure path also runs _notify_agent_turn_failure,
    # which rate-limits a "system" chat notice via a module-global dict keyed
    # by error class. Reset before AND after so this test neither inherits a
    # stale rate-limit window from an earlier test nor leaks one forward into
    # a later, unrelated test in the same pytest process (established pattern
    # — see tests/test_consumer_error_classify.py).
    crc._reset_system_notice_state()

    seen_messages = []
    call_count = {"n": 0}

    def _fake_call_agent(message, *args, **kwargs):
        call_count["n"] += 1
        seen_messages.append(message)
        if call_count["n"] == 1:
            # Simulates a subprocess/HTTP failure BEFORE the model ever saw
            # the prompt — the exact I10 scenario.
            raise RuntimeError("subprocess died before the model saw the prompt")
        return "ok"

    monkeypatch.setattr(crc, "call_agent", _fake_call_agent)

    with patch.object(crc, "post_reply"):
        crc._process_messages([_make_msg(content="turn 1", ts=1.0)])
        crc._process_messages([_make_msg(content="turn 2", ts=2.0)])
        crc._process_messages([_make_msg(content="turn 3", ts=3.0)])

    assert call_count["n"] == 3
    # Turn 1: injected, then the call FAILED — must not be marked committed.
    assert "CATALOG_TEXT" in seen_messages[0]
    # Turn 2 (same session): turn 1 was never committed -> retried, not skipped.
    assert "CATALOG_TEXT" in seen_messages[1]
    # Turn 2's call succeeded -> committed. Turn 3 (same session) must NOT
    # re-inject.
    assert "CATALOG_TEXT" not in seen_messages[2]
    assert crc._io_cli_catalog_injected_session_id == "sess-1"
    assert crc._io_cli_catalog_pending_session_id is None
