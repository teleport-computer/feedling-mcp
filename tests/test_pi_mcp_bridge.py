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
    out = _harness("mapping", _servers(("s", [f"t{i:03d}" for i in range(60)])))
    assert len(out["mapped"]) == 50
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
