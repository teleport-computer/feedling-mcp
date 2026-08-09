/**
 * Zero-dependency MCP client — JSON-RPC over streamable HTTP (McpClient) or
 * the legacy HTTP+SSE transport (SseMcpClient).
 *
 * Deliberately NOT the `mcp` SDK, for the same reason backend/hosted/mcp_probe.py
 * hand-rolled its own: four methods don't justify a node_modules tree (and its
 * supply-chain surface) inside the TEE image.
 *
 * PROTOCOL_VERSION MUST stay in sync with mcp_probe.py's _PROTOCOL_VERSION —
 * the two are the same protocol against the same user servers.
 */

import { request as httpsRequest } from "node:https";
import { request as httpRequest } from "node:http";

export const PROTOCOL_VERSION = "2025-03-26";

// Byte ceiling on any one SSE stream's unframed buffer / single event, matching
// backend/hosted/mcp_probe.py's _MAX_SSE_BYTES. A hostile server that streams
// bytes with no newline (or millions of tiny data: lines) must trip this, not
// grow Node's heap unbounded. The GET stream can outlive the 45s handshake for
// a legitimately-connected server, so this cap — not just a timeout — is the
// memory guard.
export const MAX_SSE_BYTES = 262144;


/** 有界读取响应体。超过上限即中止,不把剩下的字节读进内存。 */
async function _readBounded(resp, limit) {
  if (!resp.body || typeof resp.body.getReader !== "function") {
    // 测试里的 fetch 替身通常只实现 text();退回去但仍然截断。
    const text = await resp.text();
    return text.length > limit ? text.slice(0, limit) : text;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let out = "";
  let seen = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    seen += value.byteLength;
    out += decoder.decode(value, { stream: true });
    if (seen > limit) {
      try {
        await reader.cancel();
      } catch (_e) {
        // 取消失败无所谓,我们已经不再读了。
      }
      break;
    }
  }
  return out;
}

/**
 * Pick the MCP transport for one materialized server entry. Mirrors
 * user_mcp_materialize.effective_transport: explicit type wins, otherwise
 * the /sse URL-path heuristic (hand-written VPS user-mcp.json may omit type).
 */
export function effectiveTransport(cfg) {
  const t = String((cfg && cfg.type) || "").trim().toLowerCase();
  if (t === "sse" || t === "http") return t;
  const path = String((cfg && cfg.url) || "").split("?", 1)[0].replace(/\/+$/, "");
  return path.toLowerCase().endsWith("/sse") ? "sse" : "http";
}

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
      // Always drain the body, even for notifications, so the socket is freed —
      // but **bounded**. SSE 那侧一直有 MAX_SSE_BYTES 上限,这条普通 HTTP 路径
      // 却是无界 `resp.text()`:一个恶意或有 bug 的服务器用一个巨大的
      // tools/list 响应就能把 agent 进程撑爆,而且发生在任何 schema 体积统计
      // 之前(codex 审出的不对称)。同一个客户端里两条路必须同一个标准。
      const body = await _readBounded(resp, MAX_SSE_BYTES);
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

/**
 * Legacy HTTP+SSE transport (2024-11-05): a long-lived GET stream delivers an
 * `endpoint` event naming a same-origin POST target; every JSON-RPC request
 * POSTs there and its reply arrives back on the stream, correlated by id.
 * Chinese map providers (Tencent/AMap) still advertise this as their `/sse`
 * URL — the reason this class exists (2026-07-19 SSE-transport batch).
 *
 * The GET stream uses node:http(s).request instead of fetch for ONE reason:
 * the socket handle. An in-flight fetch body read is an active libuv handle
 * that would keep the node process alive after pi's turn ends — this bridge
 * must never be the thing that hangs a chat turn's teardown. `socket.unref()`
 * removes the stream from the keep-alive set while events still arrive for
 * as long as the process runs. POSTs are short-lived and stay on fetch
 * (fetchImpl injectable for tests, same as McpClient).
 *
 * Same interface as McpClient: initialize() / listTools() / callTool(), plus
 * close() to abort the stream eagerly.
 */
export class SseMcpClient {
  constructor(url, headers, { timeoutMs = 10000, fetchImpl = fetch } = {}) {
    this.url = url;
    this.headers = headers || {};
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
    this.nextId = 1;
    this.pending = new Map(); // id -> {resolve, reject}
    this.postUrl = null;
    this._req = null;
    this._streamErr = null;
  }

  close() {
    if (this._req) {
      try { this._req.destroy(); } catch { /* already gone */ }
      this._req = null;
    }
  }

  _fail(err) {
    if (!this._streamErr) this._streamErr = err || new Error("SSE stream failed");
    for (const [id, waiter] of this.pending) {
      this.pending.delete(id);
      waiter.reject(this._streamErr);
    }
  }

  /** Race `promise` against the per-op timeout. The loser's eventual
   * rejection is swallowed so it can't surface as an unhandledRejection. */
  _withTimeout(promise, label) {
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`${label} exceeded ${this.timeoutMs}ms`)),
        this.timeoutMs);
    });
    promise.catch(() => {}); // detach: timeout may win the race
    return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
  }

  _connect() {
    return new Promise((resolve, reject) => {
      let settled = false;
      const once = (fn, arg) => {
        if (!settled) { settled = true; fn(arg); }
      };
      let base;
      try {
        base = new URL(this.url);
      } catch (err) {
        reject(err);
        return;
      }
      const mod = base.protocol === "https:" ? httpsRequest : httpRequest;
      const req = mod(base, {
        method: "GET",
        headers: { accept: "text/event-stream", ...this.headers },
      }, (res) => {
        if ((res.statusCode || 0) >= 400) {
          res.resume();
          once(reject, new Error(`http ${res.statusCode}`));
          return;
        }
        const ctype = String(res.headers["content-type"] || "");
        if (!ctype.includes("text/event-stream")) {
          res.resume();
          once(reject, new Error(`not an event stream (${ctype})`));
          return;
        }
        if (res.socket && res.socket.unref) res.socket.unref();
        let buf = "";
        let eventName = "";
        let dataLines = [];
        let eventBytes = 0;
        const overBudget = () => {
          // Bound both the unframed buffer (no-newline flood) and one event's
          // total bytes across all its lines (tiny-line flood). Kill on breach.
          this._fail(new Error("SSE stream exceeded size budget"));
          try { req.destroy(); } catch { /* already gone */ }
          once(reject, this._streamErr);
        };
        const dispatch = (name, data) => {
          if (name === "endpoint") {
            if (this.postUrl) return; // one endpoint per session
            let target;
            try {
              target = new URL(data.trim(), this.url);
            } catch {
              once(reject, new Error("bad endpoint event"));
              return;
            }
            // Server-controlled data: refuse a cross-origin POST target so a
            // hostile stream can't aim the bridge's requests elsewhere.
            if (target.protocol !== base.protocol || target.host !== base.host) {
              once(reject, new Error("endpoint origin mismatch"));
              return;
            }
            this.postUrl = target.toString();
            once(resolve);
            return;
          }
          let doc;
          try { doc = JSON.parse(data); } catch { return; } // ping/progress
          const waiter = doc && this.pending.get(doc.id);
          if (waiter) {
            this.pending.delete(doc.id);
            waiter.resolve(doc);
          }
        };
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          buf += chunk;
          let idx;
          while ((idx = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, idx).replace(/\r$/, "");
            buf = buf.slice(idx + 1);
            if (line === "") {
              if (dataLines.length) dispatch(eventName || "message", dataLines.join("\n"));
              eventName = "";
              dataLines = [];
              eventBytes = 0;
              continue;
            }
            // Cap the WHOLE event, not just data: lines. A single oversized
            // event:/comment/unknown field must trip this too — otherwise a
            // hostile server could hold megabytes in eventName between blank
            // lines and never touch the data: branch (codex3 R2).
            eventBytes += Buffer.byteLength(line, "utf8");
            if (eventBytes > MAX_SSE_BYTES) return overBudget();
            if (line.startsWith(":")) {
              // keep-alive comment
            } else if (line.startsWith("event:")) {
              eventName = line.slice("event:".length).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice("data:".length).trim());
            }
          }
          // No newline yet: an unframed buffer past the cap is a no-newline flood.
          if (Buffer.byteLength(buf, "utf8") > MAX_SSE_BYTES) return overBudget();
        });
        res.on("end", () => { this._fail(new Error("SSE stream ended")); once(reject, this._streamErr); });
        res.on("error", (err) => { this._fail(err); once(reject, err); });
      });
      req.on("error", (err) => { this._fail(err); once(reject, err); });
      req.on("socket", (s) => { if (s.unref) s.unref(); });
      req.end();
      this._req = req;
    });
  }

  async _post(payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const resp = await this.fetchImpl(this.postUrl, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "application/json, text/event-stream",
          ...this.headers,
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
        redirect: "error", // same posture as McpClient / mcp_probe.py
      });
      // Drain so the socket is freed; the real reply rides the GET stream.
      await resp.text();
      // Notifications tolerate any status (parity with McpClient._post):
      // strictness here would kill the whole handshake for a server whose
      // only sin is not ack'ing a notification cleanly.
      if (payload.id === undefined) return;
      if (!resp.ok) throw new Error(`http ${resp.status}`);
    } finally {
      clearTimeout(timer);
    }
  }

  _rpc(method, params) {
    if (this._streamErr) return Promise.reject(this._streamErr);
    if (!this.postUrl) return Promise.reject(new Error("not connected"));
    const id = this.nextId++;
    const payload = { jsonrpc: "2.0", id, method };
    if (params !== undefined) payload.params = params;
    const reply = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    return this._withTimeout((async () => {
      await this._post(payload);
      const doc = await reply;
      if (doc && doc.error) {
        const msg = doc.error.message || JSON.stringify(doc.error);
        throw new Error(`rpc error: ${msg}`);
      }
      return doc ? doc.result : null;
    })(), `rpc ${method}`).finally(() => this.pending.delete(id));
  }

  async initialize() {
    await this._withTimeout(this._connect(), "SSE endpoint handshake");
    await this._rpc("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "feedling-pi-bridge", version: "1" },
    });
    await this._post({ jsonrpc: "2.0", method: "notifications/initialized" });
  }

  async listTools() {
    const result = await this._rpc("tools/list");
    return (result && result.tools) || [];
  }

  async callTool(name, args) {
    return await this._rpc("tools/call", { name, arguments: args || {} });
  }
}
