"""Per-turn user-MCP tool provider (chat lane only).

Loads the caller's enabled MCP servers, fetches each server's tools FRESH at turn
start (per-turn freshness decision), and exposes them to ``run_tool_loop`` as
namespaced ``ToolSpec``s (``mcp__<server>__<tool>``). Dispatch proxies the model's
call to the server through the SSRF-guarded ``mcp_client``.

Failure is never fatal to the turn: a server that can't be decrypted or listed is
simply not offered (its tools dropped, logged). Zero enabled servers → an empty
turn with no network at all.

Layer: this lives in ``hosted`` because it needs ``mcp_core``/``mcp_client`` +
``core.enclave``. The V2 core (worker/tool_loop) must not import ``hosted``
(dependency-direction guard), so ``serve_worker.build_production_deps`` injects
``load_turn_mcp`` as ``TurnDeps.load_mcp_turn`` and the worker calls it through
that seam. The returned ``McpTurn`` is duck-typed by the worker (``tool_specs`` /
``handles`` / ``dispatch``) — no hosted type crosses the import boundary.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from provider_types import ToolResult, ToolSpec
from hosted import mcp_core, mcp_client

log = logging.getLogger("feedling.hosted.mcp_tools")

MCP_TOOL_PREFIX = "mcp__"


def qualified_name(server: str, tool: str) -> str:
    return f"{MCP_TOOL_PREFIX}{server}__{tool}"


def is_mcp_tool(name: str) -> bool:
    return str(name or "").startswith(MCP_TOOL_PREFIX)


@dataclass(frozen=True)
class _Route:
    url: str
    headers: dict
    ca_pem: str | None
    tool: str  # the raw tool name on the server (un-namespaced)


@dataclass
class McpTurn:
    tool_specs: list  # list[ToolSpec], namespaced
    routes: dict      # qualified_name -> _Route

    @property
    def is_empty(self) -> bool:
        return not self.tool_specs

    def handles(self, name: str) -> bool:
        return name in self.routes

    async def dispatch(self, call) -> ToolResult:
        """Proxy one model tool call to its server. Never raises: a transport
        failure or tool error comes back as an error string the model can read,
        because aborting the turn on a flaky user server is worse than telling
        the model the call failed."""
        route = self.routes.get(call.name)
        if route is None:
            return ToolResult(call_id=call.id, content="error: unknown mcp tool")
        try:
            out = await mcp_client.call_tool(
                route.url, route.headers, route.tool, call.args or {}, ca_pem=route.ca_pem)
        except Exception as e:  # noqa: BLE001 — flaky user server must not kill the turn
            return ToolResult(call_id=call.id,
                              content=f"error: mcp call failed: {str(e)[:200]}")
        text = str(out.get("text") or "").strip()
        if out.get("is_error"):
            return ToolResult(call_id=call.id, content=f"error: {text or 'tool reported failure'}")
        return ToolResult(call_id=call.id, content=text or "ok")


def _normalize_schema(input_schema) -> dict:
    """Coerce an MCP inputSchema into an object JSON Schema the providers accept.
    MCP servers should send ``{"type":"object",...}``; be defensive about missing
    type / non-dict so one odd server can't break tool serialization."""
    if isinstance(input_schema, dict):
        schema = dict(input_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema
    return {"type": "object", "properties": {}}


def _decrypt(envelope, api_key, runtime_token) -> dict:
    from core import enclave as core_enclave
    kwargs = {"runtime_token": runtime_token} if runtime_token else {}
    raw = core_enclave._decrypt_envelope_via_enclave(
        envelope, api_key, purpose="mcp_server_config", **kwargs)
    secret = json.loads(raw.decode("utf-8"))
    if not isinstance(secret, dict) or not secret.get("url"):
        raise ValueError("mcp secret missing url")
    return secret


async def load_turn_mcp(store, *, api_key=None, runtime_token="") -> McpTurn:
    """Build the turn's MCP tool surface. Chat lane only — callers on other lanes
    simply don't call this (mirrors the resident, which gives MCP only on chat)."""
    try:
        payload = mcp_core.envelopes_payload(store)
    except Exception as e:  # noqa: BLE001
        log.warning("mcp envelopes load failed for %s: %s",
                    getattr(store, "user_id", "?"), str(e)[:160])
        return McpTurn([], {})
    if isinstance(payload, tuple):
        payload = payload[0]
    servers = [s for s in (payload.get("servers") or []) if s.get("enabled")]
    if not servers:
        return McpTurn([], {})

    async def _one(srv):
        name = str(srv.get("name") or "")
        try:
            secret = await asyncio.to_thread(
                _decrypt, srv["config_envelope"], api_key, runtime_token)
            tools = await mcp_client.list_tools(
                secret["url"], secret.get("headers") or {}, ca_pem=secret.get("ca_pem"))
        except Exception as e:  # noqa: BLE001 — one bad server never sinks the turn
            log.warning("mcp server %r skipped: %s", name, str(e)[:160])
            return None
        return (name, secret, tools)

    results = await asyncio.gather(*[_one(s) for s in servers])
    specs: list[ToolSpec] = []
    routes: dict[str, _Route] = {}
    for r in results:
        if r is None:
            continue
        name, secret, tools = r
        for t in tools:
            if not isinstance(t, dict):
                continue
            raw = str(t.get("name") or "")
            if not raw:
                continue
            q = qualified_name(name, raw)
            if q in routes:  # first server/tool wins on a collision
                continue
            specs.append(ToolSpec(
                name=q,
                description=str(t.get("description") or f"{name} · {raw}"),
                parameters=_normalize_schema(t.get("inputSchema")),
            ))
            routes[q] = _Route(url=secret["url"], headers=secret.get("headers") or {},
                               ca_pem=secret.get("ca_pem"), tool=raw)
    return McpTurn(specs, routes)
