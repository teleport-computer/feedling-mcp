/**
 * Zero-dependency MCP client — single-shot JSON-RPC over streamable HTTP.
 *
 * Deliberately NOT the `mcp` SDK, for the same reason backend/hosted/mcp_probe.py
 * hand-rolled its own: four methods don't justify a node_modules tree (and its
 * supply-chain surface) inside the TEE image.
 *
 * PROTOCOL_VERSION MUST stay in sync with mcp_probe.py's _PROTOCOL_VERSION —
 * the two are the same protocol against the same user servers.
 */

export const PROTOCOL_VERSION = "2025-03-26";

/**
 * Parse a JSON-RPC reply that may arrive as plain JSON or as an SSE stream.
 * Streamable-HTTP servers may answer either way for the same request, so both
 * shapes have to work (mcp_probe.py:82 does the same on the Python side).
 */
export function parseRpcResponse(contentType, body) {
  if (String(contentType || "").includes("text/event-stream")) {
    // Take the LAST data: line that parses — earlier ones may be pings or
    // progress notifications, the final one carries the result.
    const lines = String(body).split(/\r?\n/).reverse();
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const payload = trimmed.slice(5).trim();
      if (!payload) continue;
      try {
        return JSON.parse(payload);
      } catch {
        // not the JSON-RPC frame — keep scanning backwards
      }
    }
    throw new Error("no JSON-RPC payload found in SSE stream");
  }
  return JSON.parse(body);
}

export class McpClient {
  constructor(url, headers, { timeoutMs = 10000, fetchImpl = fetch } = {}) {
    this.url = url;
    this.headers = headers || {};
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
    this.sessionHeaders = {};
    this.nextId = 1;
  }

  async _post(payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const resp = await this.fetchImpl(this.url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "application/json, text/event-stream",
          ...this.headers,
          ...this.sessionHeaders,
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
        redirect: "error", // same posture as mcp_probe.py: no redirect chasing
      });
      // Always drain the body, even for notifications, so the socket is freed.
      const body = await resp.text();
      const sid = resp.headers.get("mcp-session-id");
      if (sid) this.sessionHeaders["Mcp-Session-Id"] = sid;
      // Notifications are fire-and-forget. The MCP spec requires sending
      // notifications/initialized before further requests, but servers answer it
      // however they like. mcp_probe.py:148 ignores its status outright
      // ("tolerate servers that 4xx it") and this client must match: a strict
      // check here kills the whole handshake — and therefore every tool — for a
      // server whose only sin is not ack'ing a notification cleanly.
      if (payload.id === undefined) return null;
      if (!resp.ok) throw new Error(`http ${resp.status}`);
      const parsed = parseRpcResponse(resp.headers.get("content-type"), body);
      if (parsed && parsed.error) {
        const msg = parsed.error.message || JSON.stringify(parsed.error);
        throw new Error(`rpc error: ${msg}`);
      }
      return parsed ? parsed.result : null;
    } finally {
      clearTimeout(timer);
    }
  }

  async initialize() {
    await this._post({
      jsonrpc: "2.0",
      id: this.nextId++,
      method: "initialize",
      params: {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "feedling-pi-bridge", version: "1" },
      },
    });
    await this._post({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async listTools() {
    const result = await this._post({
      jsonrpc: "2.0", id: this.nextId++, method: "tools/list",
    });
    return (result && result.tools) || [];
  }

  async callTool(name, args) {
    return await this._post({
      jsonrpc: "2.0",
      id: this.nextId++,
      method: "tools/call",
      params: { name, arguments: args || {} },
    });
  }
}
