"""Per-turn user-MCP tool provider (backend/model_api_runtime/v2/mcp_tools.py):
loads enabled servers, fetches tools fresh, builds namespaced ToolSpecs, and
dispatches mcp__ calls to the server. All network/enclave seams monkeypatched."""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from hosted import mcp_tools  # noqa: E402
from hosted import mcp_client  # noqa: E402
from provider_types import ToolCall  # noqa: E402

STORE = types.SimpleNamespace(user_id="usr_test")


def _servers(*names):
    return {"servers": [{"name": n, "enabled": True,
                         "config_envelope": {"id": f"env_{n}"}} for n in names]}


def _patch(monkeypatch, *, servers, decrypt=None, list_tools=None, call_tool=None):
    monkeypatch.setattr(mcp_tools.mcp_core, "envelopes_payload",
                        lambda store: (servers, 200))
    if decrypt is not None:
        monkeypatch.setattr(mcp_tools, "_decrypt", decrypt)
    if list_tools is not None:
        monkeypatch.setattr(mcp_client, "list_tools", list_tools)
    if call_tool is not None:
        monkeypatch.setattr(mcp_client, "call_tool", call_tool)


def test_builds_namespaced_specs_with_schemas(monkeypatch):
    async def fake_list(url, headers, *, ca_pem=None, transport=None):
        return [{"name": "search", "description": "find things",
                 "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]
    _patch(monkeypatch, servers=_servers("weather"),
           decrypt=lambda env, api_key, runtime_token: {"url": "https://w.example.com", "headers": {}},
           list_tools=fake_list)
    turn = asyncio.run(mcp_tools.load_turn_mcp(STORE, api_key="k", runtime_token="rt"))
    assert not turn.is_empty
    assert [s.name for s in turn.tool_specs] == ["mcp__weather__search"]
    spec = turn.tool_specs[0]
    assert spec.parameters["properties"]["q"]["type"] == "string"
    assert turn.handles("mcp__weather__search")


def test_dispatch_proxies_to_call_tool(monkeypatch):
    seen = {}

    async def fake_list(url, headers, *, ca_pem=None, transport=None):
        return [{"name": "search", "description": "d", "inputSchema": {"type": "object"}}]

    async def fake_call(url, headers, name, arguments, *, ca_pem=None, transport=None):
        seen.update(url=url, name=name, arguments=arguments)
        return {"is_error": False, "text": "sunny 25C"}

    _patch(monkeypatch, servers=_servers("weather"),
           decrypt=lambda env, api_key, runtime_token: {"url": "https://w.example.com",
                                                         "headers": {"Authorization": "Bearer x"}},
           list_tools=fake_list, call_tool=fake_call)
    turn = asyncio.run(mcp_tools.load_turn_mcp(STORE, api_key="k", runtime_token="rt"))
    call = ToolCall(id="c1", name="mcp__weather__search", args={"q": "SF"})
    result = asyncio.run(turn.dispatch(call))
    assert result.call_id == "c1"
    assert "sunny 25C" in result.content
    # routed to the raw tool name on the right server url
    assert seen == {"url": "https://w.example.com", "name": "search", "arguments": {"q": "SF"}}


def test_tool_error_prefixed_but_not_fatal(monkeypatch):
    async def fake_list(url, headers, *, ca_pem=None, transport=None):
        return [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]

    async def fake_call(url, headers, name, arguments, *, ca_pem=None, transport=None):
        return {"is_error": True, "text": "rate limited"}

    _patch(monkeypatch, servers=_servers("s"),
           decrypt=lambda env, api_key, runtime_token: {"url": "https://s.example.com", "headers": {}},
           list_tools=fake_list, call_tool=fake_call)
    turn = asyncio.run(mcp_tools.load_turn_mcp(STORE, api_key="k", runtime_token="rt"))
    result = asyncio.run(turn.dispatch(ToolCall(id="c1", name="mcp__s__t", args={})))
    assert "error" in result.content and "rate limited" in result.content


def test_no_enabled_servers_is_empty(monkeypatch):
    _patch(monkeypatch, servers={"servers": [{"name": "off", "enabled": False,
                                              "config_envelope": {}}]})
    turn = asyncio.run(mcp_tools.load_turn_mcp(STORE, api_key="k", runtime_token="rt"))
    assert turn.is_empty and turn.tool_specs == []


def test_down_server_is_skipped_not_fatal(monkeypatch):
    async def boom_list(url, headers, *, ca_pem=None, transport=None):
        raise mcp_client.ProbeError("timeout", "read timeout")

    async def ok_list(url, headers, *, ca_pem=None, transport=None):
        return [{"name": "ok", "description": "d", "inputSchema": {"type": "object"}}]

    calls = {"n": 0}

    async def mixed_list(url, headers, *, ca_pem=None, transport=None):
        calls["n"] += 1
        return await (boom_list if url.endswith("down") else ok_list)(url, headers)

    def decrypt(env, api_key, runtime_token):
        return {"url": "https://up" if env["id"] == "env_up" else "https://x/down", "headers": {}}

    _patch(monkeypatch, servers=_servers("up", "down"), decrypt=decrypt, list_tools=mixed_list)
    turn = asyncio.run(mcp_tools.load_turn_mcp(STORE, api_key="k", runtime_token="rt"))
    # the healthy server's tool survives; the down one is silently dropped
    assert [s.name for s in turn.tool_specs] == ["mcp__up__ok"]


def test_decrypt_failure_is_skipped_not_fatal(monkeypatch):
    def boom(env, api_key, runtime_token):
        raise RuntimeError("enclave 503")
    _patch(monkeypatch, servers=_servers("s"), decrypt=boom)
    turn = asyncio.run(mcp_tools.load_turn_mcp(STORE, api_key="k", runtime_token="rt"))
    assert turn.is_empty


def test_is_mcp_tool_helper():
    assert mcp_tools.is_mcp_tool("mcp__x__y")
    assert not mcp_tools.is_mcp_tool("memory_write")
    assert not mcp_tools.is_mcp_tool("")
