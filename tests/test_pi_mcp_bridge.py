"""pi user-MCP bridge tests (tools/pi_mcp_bridge/).

The bridge is JS, so behavior is exercised through a node harness
(tests/pi_mcp_bridge_harness.mjs) driven from pytest. Grep-the-source
assertions (the older feedling-io-tools style) cannot reach any of the
branches that matter here: name sanitizing, collision de-dup, the tool cap,
and — most importantly — that a dead MCP server is skipped silently instead
of taking the whole pi startup down with it.

Unlike tests/test_user_mcp_probe.py's ASGI + httpx.MockTransport fake (which
is in-process and never binds), node's fetch needs a real port, so this uses
a stdlib ThreadingHTTPServer.

Requires: node on PATH (CI: actions/setup-node).

Run with:
    cd backend && PYTHONPATH=. python3 -m pytest ../tests/test_pi_mcp_bridge.py -v
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "pi_mcp_bridge_harness.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node required for the pi bridge harness (CI installs it via setup-node)",
)


def _make_handler(tools):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request stderr noise
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                result = {"content": [{"type": "text",
                                       "text": f"called {req['params']['name']}"}]}
            else:
                self.send_response(400)
                self.end_headers()
                return
            body = json.dumps(
                {"jsonrpc": "2.0", "id": req.get("id"), "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("mcp-session-id", "sess-1")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def fake_mcp():
    """Spin a real-port fake MCP server; yields a factory -> url."""
    servers = []

    def start(tools):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(tools))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}/mcp"

    yield start
    for srv in servers:
        srv.shutdown()


def _harness(mode, arg=None, env=None, want_stderr=False):
    cmd = ["node", str(_HARNESS), mode]
    if arg is not None:
        cmd.append(arg)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    assert proc.stdout, f"harness printed nothing; stderr={proc.stderr!r}"
    out = json.loads(proc.stdout)
    return (out, proc.stderr) if want_stderr else out


def test_client_handshake_list_and_call(fake_mcp):
    url = fake_mcp([{"name": "search", "description": "find things",
                     "inputSchema": {"type": "object",
                                     "properties": {"q": {"type": "string"}}}}])
    out = _harness("client", url)
    assert "error" not in out, out
    assert [t["name"] for t in out["tools"]] == ["search"]
    assert out["called"]["content"][0]["text"] == "called search"


def test_client_parses_sse_framed_reply():
    """Streamable-HTTP servers may answer the same request as SSE.

    Uses its own handler rather than the fake_mcp fixture, which only speaks the
    plain-JSON framing.
    """

    class SseHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            if req.get("method") == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if req.get("method") == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "sse", "version": "0"}}
            elif req.get("method") == "tools/list":
                result = {"tools": [{"name": "s", "description": "d",
                                     "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "called s"}]}
            frame = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                "result": result})
            body = f": ping\n\ndata: {frame}\n\n".encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SseHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = _harness("client", f"http://127.0.0.1:{srv.server_port}/mcp")
        assert "error" not in out, out
        assert [t["name"] for t in out["tools"]] == ["s"]
    finally:
        srv.shutdown()


def test_client_tolerates_server_that_4xxs_the_initialized_notification():
    """A server may answer notifications/initialized with anything.

    mcp_probe.py:148 ignores that call's status outright ("tolerate servers that
    4xx it"). This client must match: a strict check would kill the handshake —
    and with it every tool — for a server whose only sin is not ack'ing a
    fire-and-forget notification cleanly.
    """

    class RudeAckHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(404)          # rude, but must not be fatal
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "rude", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "still_here", "description": "d",
                                     "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "called still_here"}]}
            body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                               "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), RudeAckHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = _harness("client", f"http://127.0.0.1:{srv.server_port}/mcp")
        assert "error" not in out, out
        assert [t["name"] for t in out["tools"]] == ["still_here"]
    finally:
        srv.shutdown()


def test_bridge_protocol_version_matches_the_probe():
    """The bridge and mcp_probe.py talk the same protocol to the same servers —
    a silent skew between them is a latent interop bug."""
    js = (Path(__file__).parent.parent / "tools" / "pi_mcp_bridge"
          / "mcp_client.js").read_text()
    py = (Path(__file__).parent.parent / "backend" / "hosted"
          / "mcp_probe.py").read_text()
    assert 'export const PROTOCOL_VERSION = "2025-03-26";' in js
    assert '_PROTOCOL_VERSION = "2025-03-26"' in py


# ---------------------------------------------------------------------------
# Legacy HTTP+SSE transport (SseMcpClient, SSE-transport batch 2026-07-19)
# ---------------------------------------------------------------------------


def _legacy_sse_handler(*, endpoint_path="/messages?session_id=s1"):
    """Loopback legacy transport: GET /sse streams an `endpoint` event then the
    JSON-RPC replies queued by POSTs to /messages. Mirrors mcp.map.qq.com/sse."""
    import queue

    replies = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            self._chunk(f"event:endpoint\ndata:{endpoint_path}\n\n")
            try:
                while True:
                    doc = replies.get(timeout=5)
                    self._chunk("event:message\ndata:" + json.dumps(doc) + "\n\n")
            except queue.Empty:
                self._chunk("")  # terminate stream
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _chunk(self, s):
            data = s.encode()
            try:
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "initialize":
                replies.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "serverInfo": {"name": "legacy", "version": "0"}}})
            elif method == "tools/list":
                replies.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                    "tools": [{"name": "geocode", "description": "find place",
                               "inputSchema": {"type": "object"}}]}})
            elif method == "tools/call":
                replies.put({"jsonrpc": "2.0", "id": req["id"], "result": {
                    "content": [{"type": "text", "text": "called geocode"}]}})
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()

    return Handler


def test_sse_client_handshake_list_and_call():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _legacy_sse_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = _harness("sse-client", f"http://127.0.0.1:{srv.server_port}/sse")
        assert "error" not in out, out
        assert [t["name"] for t in out["tools"]] == ["geocode"]
        assert out["called"]["content"][0]["text"] == "called geocode"
    finally:
        srv.shutdown()


def test_sse_client_get_stream_socket_is_unref_d():
    """codex3 P2: the real unref proof. The bridge client is NEVER explicitly
    closed in production — a connected client just goes out of scope at turn
    end. So drive it WITHOUT close() and require node to still exit on its own:
    that can only happen if the long-lived GET stream's socket was unref'd. (The
    prior test called close() before timing, which destroys the socket
    regardless of unref and so proved nothing.)"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _legacy_sse_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t0 = time.monotonic()
        out = _harness("sse-client-noclose", f"http://127.0.0.1:{srv.server_port}/sse")
        elapsed = time.monotonic() - t0
        assert "error" not in out, out
        assert [t["name"] for t in out["tools"]] == ["geocode"]
        # No close() was called; node must still exit promptly — proving the
        # open GET stream is not keeping the event loop alive.
        assert elapsed < 4.0, f"harness took {elapsed:.1f}s — GET socket not unref'd"
    finally:
        srv.shutdown()


def test_sse_client_refuses_cross_origin_endpoint():
    """The endpoint event is server-controlled; a cross-origin target must be
    refused so a hostile stream can't aim the bridge's POSTs elsewhere."""
    handler = _legacy_sse_handler(
        endpoint_path="http://evil.example.invalid:1/messages")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = _harness("sse-client", f"http://127.0.0.1:{srv.server_port}/sse")
        assert "error" in out
        assert "origin mismatch" in out["error"]
    finally:
        srv.shutdown()


def test_sse_client_no_newline_flood_hits_byte_budget():
    """A GET stream flooding bytes with no newline must trip MAX_SSE_BYTES and
    tear the connection down — not grow Node's heap unbounded. The harness exits
    promptly (well under the server's read timeout), proving the guard fired and
    closed the socket rather than the process lingering on an open stream."""

    class Flood(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    data = b"x" * 8192   # no newline, ever
                    self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Flood)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t0 = time.monotonic()
        out = _harness("sse-client", f"http://127.0.0.1:{srv.server_port}/sse")
        elapsed = time.monotonic() - t0
        assert "error" in out
        assert "size budget" in out["error"]
        assert elapsed < 5.0, f"took {elapsed:.1f}s — budget/destroy didn't fire"
    finally:
        srv.shutdown()


def test_sse_client_oversized_event_line_hits_byte_budget():
    """A single huge `event:` line (properly newline-terminated, so the
    no-newline buffer guard never fires) must still trip the per-event budget —
    the cap covers every field, not just data: (codex3 R2)."""

    class BigEvent(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            # "event:" + 512KB of name + newline — one framed line, no data:.
            payload = b"event:" + b"A" * (512 * 1024) + b"\n"
            try:
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
                while True:  # keep the stream open so only the budget can end it
                    self.wfile.write(b"1\r\n:\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), BigEvent)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = _harness("sse-client", f"http://127.0.0.1:{srv.server_port}/sse")
        assert "error" in out
        assert "size budget" in out["error"]
    finally:
        srv.shutdown()


def test_effective_transport_routing():
    specs = [
        {"type": "sse", "url": "https://x/mcp"},      # explicit wins
        {"type": "http", "url": "https://x/sse"},     # explicit wins
        {"url": "https://mcp.map.qq.com/sse?key=K"},  # heuristic → sse
        {"url": "https://x/mcp"},                     # heuristic → http
    ]
    out = _harness("transport", json.dumps(specs))
    assert out == ["sse", "http", "sse", "http"]


def _servers(*specs):
    """specs: (server_name, [tool_name, ...])"""
    return json.dumps([
        {"name": s, "tools": [{"name": t, "description": f"desc {t}",
                               "inputSchema": {"type": "object"}} for t in tools]}
        for s, tools in specs
    ])


def test_mapping_prefixes_and_sanitizes_for_gemini():
    """pi carries gemini, whose tool names must match ^[a-zA-Z0-9_-]{1,64}$.
    MCP tool names come from the user's server and are unconstrained."""
    out = _harness("mapping", _servers(("jira", ["search.issues", "create ticket"])))
    names = [m["piName"] for m in out["mapped"]]
    assert names == ["mcp_jira_create_ticket", "mcp_jira_search_issues"]
    for n in names:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n), n


def test_mapping_is_deterministic_regardless_of_server_order():
    """The model must see the same toolset every turn. Servers finish their
    handshakes in nondeterministic order, so mapping must sort, not zip."""
    a = _harness("mapping", _servers(("alpha", ["x"]), ("beta", ["y"])))
    b = _harness("mapping", _servers(("beta", ["y"]), ("alpha", ["x"])))
    assert [m["piName"] for m in a["mapped"]] == [m["piName"] for m in b["mapped"]]


def test_mapping_dedupes_collisions_deterministically():
    """Two different MCP tool names can sanitize to the same pi name."""
    out = _harness("mapping", _servers(("s", ["a.b", "a-b", "a b"])))
    names = [m["piName"] for m in out["mapped"]]
    assert len(names) == len(set(names)), names
    again = _harness("mapping", _servers(("s", ["a.b", "a-b", "a b"])))
    assert names == [m["piName"] for m in again["mapped"]]


def test_mapping_collision_suffix_survives_a_sibling_collider_being_added():
    """The one property hash-of-pair buys over a counter.

    A counter renumbers on insertion: add a colliding tool that sorts BEFORE an
    existing one and the existing one's suffix shifts. A hash of the original
    (server, tool) pair does not move. This matters because the table is rebuilt
    every chat turn — a name that shifts when the user adds an unrelated tool
    silently breaks every tool the model remembered.

    "a b", "a!b", "a.b" and "a/b" all sanitize to "a_b" and therefore collide;
    "a-b" keeps its dash and does not. Sort order is by raw name, so "a!b"
    (0x21) lands between "a b" (0x20) and "a-b" (0x2D) — i.e. BEFORE "a.b".
    """
    before = _harness("mapping", _servers(("s", ["a b", "a-b", "a.b"])))
    after = _harness("mapping", _servers(("s", ["a b", "a!b", "a-b", "a.b"])))

    name_of = {m["mcpName"]: m["piName"] for m in before["mapped"]}
    name_of_after = {m["mcpName"]: m["piName"] for m in after["mapped"]}

    # Every tool present before must keep the exact same pi name afterwards.
    for mcp_name in ("a b", "a-b", "a.b"):
        assert name_of[mcp_name] == name_of_after[mcp_name], (
            f"{mcp_name} renamed by an unrelated sibling: "
            f"{name_of[mcp_name]} -> {name_of_after[mcp_name]}")


def test_mapping_caps_tools_and_reports_what_it_dropped():
    """Silent truncation would present as 'tools come and go' — the hardest
    class of bug to triage, and indistinguishable from the symptom this whole
    feature exists to fix ('the AI can't see my tool')."""
    probe = _harness("mapping", _servers(("s", ["t000"])))
    cap = probe["cap"]  # 从桥拿上限,别在测试里抄魔数(改常量时会静默失配)
    total = cap + 10
    out = _harness(
        "mapping", _servers(("s", [f"t{i:03d}" for i in range(total)]))
    )
    assert len(out["mapped"]) == cap
    assert len(out["dropped"]) == 10
    assert all(d.startswith("s/") for d in out["dropped"])


def test_mapping_passes_mcp_schema_through_untouched():
    """pi accepts a bare JSON Schema (no TypeBox metadata) — validation.js:257
    branches on exactly that — so the MCP inputSchema needs no conversion."""
    schema = {"type": "object", "properties": {"q": {"type": "string"}},
              "required": ["q"]}
    servers = json.dumps([{"name": "s", "tools": [
        {"name": "find", "description": "d", "inputSchema": schema}]}])
    out = _harness("mapping", servers)
    assert out["mapped"][0]["parameters"] == schema


def _bridge_env(tmp_path, servers_doc):
    """Build the child env the resident would hand pi: the bridge is one shared
    static file, so the per-user config path rides FEEDLING_USER_MCP_FILE."""
    cfg = tmp_path / "user-mcp.json"
    cfg.write_text(json.dumps(servers_doc))
    env = dict(os.environ)
    env["FEEDLING_USER_MCP_FILE"] = str(cfg)
    return env


def test_extension_registers_tools_from_a_live_server(fake_mcp, tmp_path):
    url = fake_mcp([{"name": "search", "description": "find things",
                     "inputSchema": {"type": "object",
                                     "properties": {"q": {"type": "string"}}}}])
    env = _bridge_env(tmp_path,
                      {"mcpServers": {"jira": {"type": "http", "url": url,
                                               "headers": {}}}})
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert [t["name"] for t in out["tools"]] == ["mcp_jira_search"]
    assert out["tools"][0]["parameters"] == {
        "type": "object", "properties": {"q": {"type": "string"}}}
    assert out["executed"][0]["content"][0]["text"] == "called search"


def test_extension_survives_a_dead_server(tmp_path):
    """THE critical path. pi awaits this factory and blocks startup on it, so an
    uncaught throw here doesn't degrade MCP — it takes the user's whole chat
    turn down. A broken third-party server must never cost someone their agent.
    """
    # Port 1 is reserved/unbound — connection refused, fast.
    env = _bridge_env(tmp_path,
                      {"mcpServers": {"dead": {"type": "http",
                                               "url": "http://127.0.0.1:1/mcp",
                                               "headers": {}}}})
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []


def test_extension_keeps_live_server_when_a_sibling_is_dead(fake_mcp, tmp_path):
    """One bad server must not deprive the user of the good ones."""
    url = fake_mcp([{"name": "ok", "description": "d",
                     "inputSchema": {"type": "object"}}])
    env = _bridge_env(tmp_path, {"mcpServers": {
        "good": {"type": "http", "url": url, "headers": {}},
        "dead": {"type": "http", "url": "http://127.0.0.1:1/mcp", "headers": {}},
    }})
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert [t["name"] for t in out["tools"]] == ["mcp_good_ok"]


def test_extension_noop_without_env_or_config(tmp_path):
    env = dict(os.environ)
    env.pop("FEEDLING_USER_MCP_FILE", None)
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []

    env["FEEDLING_USER_MCP_FILE"] = str(tmp_path / "missing.json")
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []


def test_extension_survives_malformed_config(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text("{not json")
    env = dict(os.environ)
    env["FEEDLING_USER_MCP_FILE"] = str(cfg)
    out = _harness("extension", None, env)
    assert out["threw"] is False, out
    assert out["tools"] == []


def test_extension_tool_call_error_returns_content_not_throw(tmp_path):
    """A failing tools/call must come back to the model as text it can react to;
    throwing would surface as a broken turn instead of a tool error."""

    class DyingHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "d", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "boom", "description": "d",
                                     "inputSchema": {"type": "object"}}]}
            else:  # tools/call → JSON-RPC error
                body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                   "error": {"code": -32000,
                                             "message": "upstream exploded"}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                               "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), DyingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = _bridge_env(tmp_path, {"mcpServers": {"s": {
            "type": "http", "url": f"http://127.0.0.1:{srv.server_port}/mcp",
            "headers": {}}}})
        out = _harness("extension", None, env)
        assert out["threw"] is False, out
        payload = json.loads(out["executed"][0]["content"][0]["text"])
        assert payload["ok"] is False
        assert "upstream exploded" in payload["error"]
    finally:
        srv.shutdown()


def test_extension_logs_failures_to_stderr_and_keeps_stdout_clean(tmp_path):
    """The log half of "log and continue" — and the stream it goes to.

    pi --mode json emits its JSONL event stream on STDOUT and the resident parses
    it, so the bridge must log only to stderr. Without this test, changing a
    console.error to console.log (or dropping the logs entirely) leaves every
    other test green while corrupting the event stream the whole hosted chat
    path depends on.
    """
    env = _bridge_env(tmp_path, {"mcpServers": {"dead": {
        "type": "http", "url": "http://127.0.0.1:1/mcp", "headers": {}}}})
    out, err = _harness("extension", None, env, want_stderr=True)

    assert out["threw"] is False, out
    assert out["tools"] == []
    # The dead server must be named, and named on stderr.
    assert "[user_mcp]" in err
    assert "dead" in err
    # stdout must carry the harness's JSON result and nothing else — any bridge
    # log leaking onto stdout would corrupt pi's event stream in production.
    assert json.loads(json.dumps(out)) == out  # out came from stdout; it parsed cleanly


def test_extension_bounds_a_slow_but_alive_server_to_the_total_budget(tmp_path):
    """A server that never fails but answers just slowly must not be able to
    occupy the turn for 3x CONNECT_TIMEOUT_MS worth of POSTs (index.js's
    SERVER_TOTAL_TIMEOUT_MS). Uses FEEDLING_MCP_SERVER_TIMEOUT_MS to shrink the
    real 30s budget to something a test can afford to wait out, and asserts on
    wall-clock time to prove the deadline — not mere speed — is what bounded it.
    """

    class SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            # Slow but alive: every real POST answers, just late enough that
            # the handshake as a whole blows the (shrunk) total budget.
            time.sleep(0.2)
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {},
                          "serverInfo": {"name": "slow", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "late", "description": "d",
                                     "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "called late"}]}
            body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                               "result": result}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = _bridge_env(tmp_path, {"mcpServers": {"slow": {
            "type": "http", "url": f"http://127.0.0.1:{srv.server_port}/mcp",
            "headers": {}}}})
        # 3 POSTs x 0.2s sleep = 0.6s to complete the handshake; a 300ms
        # per-server budget must cut it off first.
        env["FEEDLING_MCP_SERVER_TIMEOUT_MS"] = "300"
        start = time.monotonic()
        out = _harness("extension", None, env)
        elapsed = time.monotonic() - start

        assert out["threw"] is False, out
        # The slow server contributed no tools — it was cut off, not waited for.
        assert out["tools"] == []
        # Bounded by the shrunk budget, not by the real 30s default: proves the
        # deadline actually fired rather than the server merely finishing fast.
        assert elapsed < 5, f"took {elapsed}s — budget was not enforced"
    finally:
        srv.shutdown()


# ── 轮转公平分配(2026-08-09 换掉字母序截断)─────────────────────────────


def _big(name, count):
    return {"name": name, "tools": [
        {"name": f"t{i:03d}", "description": "d" * 40,
         "inputSchema": {"type": "object", "properties": {}}}
        for i in range(count)
    ]}


def test_a_huge_server_cannot_starve_a_small_one():
    """一台 200 个工具的服务器不能把 4 个工具的那台挤成 0。

    这是 usr_1baf(2026-08-09)的真实症状:旧实现把所有工具排成一列后按
    **服务器名**截断,`tavily` 字母序排最后 → 一个工具都没注册上。
    用户看到的是「连接测试通过,AI 却说搜不到」—— 探针直连服务器确实成功,
    而伴侣真的看不到它的工具,两件事发生在两个进程里。
    """
    out = _harness("mapping", json.dumps([_big("gardenforum", 200),
                                          _big("tavily", 4)]))
    assert len(out["mapped"]) == out["cap"]
    kept = {m["server"] for m in out["mapped"]}
    assert "tavily" in kept, "小服务器被整台饿死了"
    assert out["per_server"].count("tavily:4/4") == 1, out["per_server"]


def test_the_reported_users_exact_configuration_keeps_every_server():
    """她的真实配置(6 台 107 个工具):现在**一台都不用裁**。

    2026-08-10 上限统一到 128(实测得出)之后 107 < 128 —— 真实用户整套完整
    到达模型。这条原本断言 `mapped == cap`(那时 cap=100,会裁掉 7 个);
    保留这个用例是为了盯住"真实用户不该撞墙"这件事,所以断言改成全留。
    """
    servers = [_big("game", 8), _big("gaodemap", 12), _big("gardenforum", 25),
               _big("luckin-coffee", 30), _big("mcdonalds", 28), _big("tavily", 4)]
    out = _harness("mapping", json.dumps(servers))
    assert len(out["mapped"]) == 107, "真实用户的工具被裁了"
    assert out["dropped"] == []
    for name in ("game", "gaodemap", "gardenforum", "luckin-coffee",
                 "mcdonalds", "tavily"):
        assert f"{name}:0/" not in out["per_server"], (
            f"{name} 被饿死:{out['per_server']}"
        )
    # 工具少的那几台应当**全部**拿到
    assert "tavily:4/4" in out["per_server"]
    assert "game:8/8" in out["per_server"]


def test_allocation_is_deterministic_regardless_of_handshake_order():
    """握手完成的先后不能影响分配结果 —— 否则模型每轮看到的工具面都在变,
    它记住的工具下一轮就调不出来了。"""
    servers = [_big("aaa", 60), _big("bbb", 60), _big("ccc", 60)]
    first = _harness("mapping", json.dumps(servers))
    shuffled = _harness("mapping", json.dumps(list(reversed(servers))))
    assert [m["piName"] for m in first["mapped"]] == \
           [m["piName"] for m in shuffled["mapped"]]


def test_nothing_is_lost_when_the_total_fits_under_the_cap():
    servers = [_big("a", 3), _big("b", 5)]
    out = _harness("mapping", json.dumps(servers))
    assert len(out["mapped"]) == 8
    assert out["dropped"] == []
    assert "a:3/3" in out["per_server"] and "b:5/5" in out["per_server"]


def test_schema_bytes_counts_utf8_not_utf16_code_units():
    """中文/emoji 的描述必须按 UTF-8 字节算。

    `String.length` 数的是 UTF-16 码元,中文会少算一半以上 —— 而这个指标
    存在的意义就是判断「工具面是不是太大」,量错了就没意义(codex 审出)。
    """
    servers = json.dumps([{"name": "s", "tools": [
        {"name": "weather", "description": "查询天气🌤️",
         "inputSchema": {"type": "object", "properties": {}}}]}])
    out = _harness("mapping", servers)
    # "查询天气🌤️" = UTF-16 长度 7,UTF-8 21 字节;加上 schema 的 JSON
    assert out["schema_bytes"] > 21, out["schema_bytes"]
