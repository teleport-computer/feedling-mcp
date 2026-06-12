"""
Regression tests for MCP server's session_id → api_key resolution.

Written after the 2026-05-11 P0 incident: `_resolve_for_session` had a
peer-IP-based fallback that, behind a reverse proxy where every client
shares the same upstream IP, returned other users' keys. Specifically,
when user A's MCP runtime didn't echo `?key=` on POST /messages/, the
server "found" user A's session by looking up "any pending key from the
same peer" — and that pending key belonged to whichever user had most
recently opened a SSE connection through the same proxy.

These tests do NOT spin up a real MCP server. They import the auth-state
module-level functions directly and stress them concurrently.

What they cover:

1. _remember + _resolve_for_session basic isolation: different
   session_ids resolve to their OWN keys, even under heavy concurrency.

2. The deleted peer-fallback DOES NOT come back: a request with no
   session_id-match must resolve to None regardless of peer IP collisions
   with other entries. This is the test that would have failed on the
   buggy version and now passes — and must keep passing.

3. The SSE-response binding helper (`_sniff_session_id_and_bind`)
   correctly extracts a server-assigned session_id from a synthetic
   SSE event-stream and binds the captured key to it. This is the
   replacement mechanism for clients that only forward `?key=` on the
   initial GET; correctness of this binding is what restored
   compatibility with init-only-key clients without re-introducing the
   peer-IP attack surface.

Run:
    pytest tests/test_mcp_session_isolation.py -v
"""

from __future__ import annotations

import os
import random
import string
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — make backend importable, then import the module under
# test. mcp_server imports FastMCP / starlette which are real dependencies;
# we DON'T need to mock them — we only call pure helpers.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# Importing mcp_server starts a FastMCP instance at module scope; that's
# fine — we don't run it, we just call its helpers.
import mcp_server  # noqa: E402
from mcpsrv import client as mcp_client  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: clean cache before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_session_cache():
    with mcp_server._session_keys_lock:
        mcp_server._session_keys.clear()
    yield
    with mcp_server._session_keys_lock:
        mcp_server._session_keys.clear()


def _rand_id(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ---------------------------------------------------------------------------
# Core resolution behavior
# ---------------------------------------------------------------------------

def test_remember_then_resolve_returns_same_key():
    sid = "session-aaa"
    key = "key-aaa-1234"
    mcp_server._remember(sid, key)
    assert mcp_server._resolve_for_session(sid) == key


def test_chat_history_merges_verify_ping_plaintext(monkeypatch):
    """MCP history usually comes from the enclave decrypt path, but synthetic
    verify pings are local_only and intentionally cannot be enclave-decrypted.
    Preserve only that server-authored sentinel plaintext so resident
    consumers can answer live-connection checks."""

    decrypted = {
        "messages": [
            {
                "id": "ping-1",
                "role": "user",
                "source": "verify_ping",
                "content": "",
                "content_type": "text",
                "ts": 1.0,
            },
            {
                "id": "normal-1",
                "role": "user",
                "source": "chat",
                "content": "",
                "content_type": "text",
                "ts": 2.0,
            },
        ]
    }
    plain = {
        "messages": [
            {
                "id": "ping-1",
                "source": "verify_ping",
                "content": "__VERIFY_PING__:abc123",
            },
            {
                "id": "normal-1",
                "source": "chat",
                "content": "must-not-copy",
            },
        ]
    }

    monkeypatch.setattr(mcp_client, "_get_decrypted", lambda *args, **kwargs: decrypted)
    monkeypatch.setattr(mcp_client, "_get", lambda *args, **kwargs: plain)

    out = mcp_server.chat_get_history(limit=20)

    msgs = out["messages"]
    assert msgs[0]["content"] == "__VERIFY_PING__:abc123"
    assert msgs[1]["content"] == ""


def test_split_tagged_reasoning_removes_tags_from_visible_chat():
    visible, reasoning = mcp_server._split_tagged_reasoning(
        "<think>\n比较了用户最新问题和已有上下文。\n</think>\n\n这是最终回复。"
    )

    assert visible == "这是最终回复。"
    assert reasoning == "比较了用户最新问题和已有上下文。"


def test_split_tagged_reasoning_handles_reasoning_and_thought_blocks():
    visible, reasoning = mcp_server._split_tagged_reasoning(
        "开头\n<reasoning>第一段推理</reasoning>\n中间\n<thought>第二段思考</thought>\n结尾"
    )

    assert visible == "开头\n\n中间\n\n结尾"
    assert reasoning == "第一段推理\n\n第二段思考"


def test_missing_session_id_resolves_to_none():
    """The defining post-P0 invariant: no session_id, no fallback, no key."""
    mcp_server._remember("session-X", "key-X")
    assert mcp_server._resolve_for_session(None) is None
    assert mcp_server._resolve_for_session("") is None


def test_unbound_session_id_resolves_to_none():
    mcp_server._remember("session-X", "key-X")
    assert mcp_server._resolve_for_session("session-Y") is None


def test_peer_arg_is_ignored_even_with_matching_entry():
    """Old buggy fallback: same peer IP would let an unknown session_id
    grab whichever key was most recently stashed by anyone from that
    peer. After the fix, peer is a no-op."""
    mcp_server._remember("session-A", "key-A")
    # Try to "fish" the key out using a different session_id but the same
    # peer that hypothetically registered it. peer must be a no-op.
    assert mcp_server._resolve_for_session("session-OTHER", peer="10.0.0.1") is None
    assert mcp_server._resolve_for_session(None, peer="10.0.0.1") is None


def test_remember_silently_drops_missing_inputs():
    """_remember must require BOTH session_id and key. If either is
    missing it's a no-op, never silently bound to a sentinel."""
    mcp_server._remember(None, "key-X")
    mcp_server._remember("session-X", "")
    mcp_server._remember("", "")
    mcp_server._remember(None, "")
    with mcp_server._session_keys_lock:
        assert len(mcp_server._session_keys) == 0


# ---------------------------------------------------------------------------
# Concurrency: no cross-bleed under load
# ---------------------------------------------------------------------------

def test_concurrent_remember_then_resolve_each_gets_own_key():
    """Spin up 200 (session_id, key) pairs across 32 threads, alternating
    _remember and _resolve_for_session calls. Every resolved key must be
    the same one that was registered for that session_id. No bleed
    allowed under any interleaving."""
    pairs = [(f"sid-{i}-{_rand_id()}", f"key-{i}-{_rand_id()}") for i in range(200)]

    # Phase 1 — concurrent remember
    with ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(lambda p: mcp_server._remember(p[0], p[1]), pairs))

    # Phase 2 — concurrent resolve, every lookup must return its own key
    errors = []
    def check(p):
        got = mcp_server._resolve_for_session(p[0])
        if got != p[1]:
            errors.append((p[0], p[1], got))
    with ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(check, pairs))
    assert not errors, f"cross-bleed under concurrency: {errors[:5]} (total {len(errors)})"


def test_overwrite_session_id_is_idempotent():
    """A session_id that gets remembered twice (e.g. client reconnects)
    takes the LATEST key. Never returns a stale earlier value."""
    mcp_server._remember("sid-A", "key-A-v1")
    mcp_server._remember("sid-A", "key-A-v2")
    assert mcp_server._resolve_for_session("sid-A") == "key-A-v2"


# ---------------------------------------------------------------------------
# SSE-response binding helper (the post-P0 safe replacement for peer fallback)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sse_sniff_binds_session_id_to_key():
    """Synthetic SSE response carries the standard MCP `event: endpoint`
    line whose data field includes `session_id=<uuid>`. The middleware
    helper must extract that session_id and bind the captured key to it,
    so subsequent POSTs to /messages/?session_id=<uuid> auth correctly."""
    cls = mcp_server.KeyCaptureMiddleware
    captured_key = "k-init-abc123"

    sse_uuid = "abcdef01-2345-6789-abcd-ef0123456789"
    chunks = [
        b": ping\n\n",  # initial comment some servers send
        f"event: endpoint\ndata: /messages/?session_id={sse_uuid}\n\n".encode(),
        b"event: message\ndata: {\"hello\":\"world\"}\n\n",  # later traffic
    ]

    async def fake_body_iter():
        for c in chunks:
            yield c

    # Run the sniffer; it should forward every chunk unchanged AND bind.
    forwarded = []
    async for chunk in cls._sniff_session_id_and_bind(fake_body_iter(), captured_key):
        forwarded.append(chunk)

    assert forwarded == chunks, "sniffer must forward every chunk unchanged"
    assert mcp_server._resolve_for_session(sse_uuid) == captured_key, (
        "sniffer must bind key to the SSE-assigned session_id"
    )


@pytest.mark.asyncio
async def test_sse_sniff_gives_up_after_cap_without_binding():
    """If the endpoint event never arrives within the 8 KB sniff cap, the
    helper must give up gracefully — no binding, but every chunk still
    forwarded."""
    cls = mcp_server.KeyCaptureMiddleware
    captured_key = "k-no-endpoint"

    # 12 KB of garbage with NO endpoint event
    chunks = [b"x" * 4096 for _ in range(3)]

    async def fake_body_iter():
        for c in chunks:
            yield c

    forwarded = []
    async for chunk in cls._sniff_session_id_and_bind(fake_body_iter(), captured_key):
        forwarded.append(chunk)

    assert forwarded == chunks
    # Nothing was bound — no session id was visible in the stream.
    with mcp_server._session_keys_lock:
        assert mcp_server._session_keys == {}


@pytest.mark.asyncio
async def test_sse_sniff_ignores_session_id_after_first_bind():
    """If the stream happens to contain something that looks like another
    endpoint event later, the helper must NOT re-bind. Stop sniffing
    after the first successful bind to limit attack surface."""
    cls = mcp_server.KeyCaptureMiddleware

    sse_first = "11111111-1111-1111-1111-111111111111"
    sse_decoy = "22222222-2222-2222-2222-222222222222"
    chunks = [
        f"event: endpoint\ndata: /messages/?session_id={sse_first}\n\n".encode(),
        # A later message that looks like another endpoint but mustn't
        # overwrite the first binding. (A pathological / spoofed peer
        # could try to ride in via something that pattern-matches.)
        f"event: endpoint\ndata: /messages/?session_id={sse_decoy}\n\n".encode(),
    ]

    async def fake_body_iter():
        for c in chunks:
            yield c

    async for _ in cls._sniff_session_id_and_bind(fake_body_iter(), "key-real"):
        pass

    assert mcp_server._resolve_for_session(sse_first) == "key-real"
    assert mcp_server._resolve_for_session(sse_decoy) is None
