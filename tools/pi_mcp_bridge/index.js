/**
 * feedling user-MCP bridge — a pi extension.
 *
 * pi has no built-in MCP (README:491: "No MCP. ... or build an extension that
 * adds MCP support"). This reads the ALREADY-MATERIALIZED user-mcp.json that the
 * resident consumer writes, connects to each server, and registers every MCP
 * tool as a native pi tool. Nothing in the storage/delivery/materialization
 * chain changes.
 *
 * Loaded per-turn via `-e` on the chat lane only (chat_resident_consumer.py's
 * _user_mcp_cli_value). A background turn simply loads no extension, so the MCP
 * tools do not exist that turn — that's what keeps proactive turns from silently
 * spending the user's third-party quota (v2 spec §1).
 *
 * ERROR POLICY — the single most important thing in this file: pi awaits this
 * factory and BLOCKS STARTUP on it (docs/extensions.md). An uncaught throw here
 * does not degrade MCP; it takes the user's entire chat turn down. Every failure
 * path below therefore logs and continues. A broken third-party server must
 * never cost someone their agent.
 *
 * All logging goes to stderr: pi --mode json emits its JSONL event stream on
 * stdout, and the resident parses it.
 */

import { readFileSync } from "node:fs";
import { McpClient, SseMcpClient, effectiveTransport } from "./mcp_client.js";
import { buildToolTable, MAX_TOOLS, schemaBytes, surfaceCounts } from "./tool_mapping.js";

// Matches mcp_probe.py's _CONNECT_TIMEOUT — same servers, same patience.
// This is a PER-POST budget (mcp_client.js's _post() starts a fresh
// AbortController on every call), not a per-server one: one server connection
// issues three POSTs (initialize, notifications/initialized, tools/list), so
// left alone this can cost up to ~3x this value per server.
const CONNECT_TIMEOUT_MS = 10000;

// mcp_probe.py has two tiers: _CONNECT_TIMEOUT (10s, connect-phase only) and a
// separate _TOTAL_TIMEOUT (30s, whole request) via httpx.Timeout(total,
// connect=connect). fetch() cannot express a connect-only timeout — only a
// whole-request abort — so the faithful JS rendering keeps CONNECT_TIMEOUT_MS
// as the per-POST cap above and adds this as a hard ceiling across one
// server's whole connect sequence (initialize() + listTools()). That gives
// every server a deterministic upper bound instead of "3 x 10s, and it grows
// if the POST count ever changes."
//
// FEEDLING_MCP_SERVER_TIMEOUT_MS overrides this — TEST-ONLY, so a regression
// test can prove the budget is enforced without waiting out the real 30s.
const SERVER_TOTAL_TIMEOUT_MS =
  Number(process.env.FEEDLING_MCP_SERVER_TIMEOUT_MS) || 30000;

// Races `promise` against a `ms`-timer; rejects with a labeled error if the
// timer wins. Always clears the timer — a dangling setTimeout would keep the
// Node process alive and hang pi's startup, which is exactly the class of
// failure this budget exists to prevent.
async function withDeadline(promise, ms, label) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} exceeded ${ms}ms`)), ms);
  });
  try {
    return await Promise.race([promise, deadline]);
  } finally {
    clearTimeout(timer);
  }
}

function loadServers(path) {
  const doc = JSON.parse(readFileSync(path, "utf8"));
  const out = [];
  for (const [name, cfg] of Object.entries((doc && doc.mcpServers) || {})) {
    if (!cfg || !cfg.url) continue;
    out.push({ name, url: cfg.url, headers: cfg.headers || {},
               transport: effectiveTransport(cfg) });
  }
  return out;
}

function makeClient(s) {
  // "sse" → legacy HTTP+SSE (long-lived GET stream + POST endpoint);
  // anything else → streamable HTTP. Materialized configs carry an explicit
  // type; hand-written VPS ones fall back to the /sse URL heuristic inside
  // effectiveTransport (already resolved into s.transport by loadServers).
  const Ctor = s.transport === "sse" ? SseMcpClient : McpClient;
  return new Ctor(s.url, s.headers, { timeoutMs: CONNECT_TIMEOUT_MS });
}

export default async function feedlingUserMcpBridge(pi) {
  try {
    const file = process.env.FEEDLING_USER_MCP_FILE;
    if (!file) return; // not a pi user-MCP turn

    let servers;
    try {
      servers = loadServers(file);
    } catch (err) {
      console.error(`[user_mcp] cannot read ${file}: ${err && err.message}`);
      return;
    }
    if (!servers.length) return;

    const clients = new Map();
    // allSettled, not all: the per-server try/catch below already turns every
    // known failure into a benign { name, tools: [] }, but isolation should
    // not depend on that being the *only* way a map callback can reject. If a
    // future change to mcp_client.js ever made `new McpClient(...)` throw
    // synchronously, Promise.all would reject the whole batch on one bad
    // server, hit the outer catch-all below, and silently drop every server's
    // tools for the turn — exactly the failure this bridge exists to prevent.
    // allSettled keeps one server's rejection from taking the others down.
    const settled = await Promise.allSettled(servers.map(async (s) => {
      const client = makeClient(s);
      try {
        const tools = await withDeadline((async () => {
          await client.initialize();
          return client.listTools();
        })(), SERVER_TOTAL_TIMEOUT_MS, `server "${s.name}"`);
        clients.set(s.name, client);
        return { name: s.name, tools };
      } catch (err) {
        // Skip this server, keep the others, let pi start. An SSE client may
        // have a half-open stream at this point — abort it eagerly (its
        // socket is unref'd either way, so it could not hang teardown, but
        // there is no reason to keep the connection).
        if (typeof client.close === "function") client.close();
        console.error(
          `[user_mcp] server "${s.name}" unreachable, skipped: ${err && err.message}`);
        return { name: s.name, tools: [] };
      }
    }));
    const connected = settled.map((r, i) => {
      if (r.status === "fulfilled") return r.value;
      console.error(
        `[user_mcp] server "${servers[i].name}" rejected unexpectedly, skipped: `
        + `${r.reason && r.reason.message}`);
      return { name: servers[i].name, tools: [] };
    });

    const { mapped, dropped } = buildToolTable(connected);
    // 无条件输出一行结构化的工具面摘要。以前只有「丢弃时」才打日志,于是
    // 「这一轮模型到底看得到哪些 MCP 工具」在生产上**完全不可观测** ——
    // 用户报「AI 说搜不到」时,我们连"工具有没有被注册进去"都答不上来。
    // consumer 会解析这一行并写进 debug trace(admin 可见)。
    // detail 是 `服务器:注册数/发现数` —— 必须报**注册后**的数字。
    // 只报发现数的话,一台服务器的工具全被丢掉时日志照样写着 tavily:4,
    // 恰好把这条埋点要回答的问题答错(codex 审出)。
    const perServer = surfaceCounts(connected, mapped);
    console.error(
      `[user_mcp] surface servers=${connected.length} `
      + `registered=${mapped.length} dropped=${dropped.length} `
      + `cap=${MAX_TOOLS} bytes=${schemaBytes(mapped)} `
      + `detail=${perServer || "(none)"}`);
    if (dropped.length) {
      console.error(
        `[user_mcp] tool cap ${MAX_TOOLS} reached — dropped ${dropped.length}: `
        + dropped.join(", "));
    }

    for (const t of mapped) {
      pi.registerTool({
        name: t.piName,
        label: `${t.server}: ${t.mcpName}`,
        description: t.description,
        parameters: t.parameters,
        async execute(_toolCallId, params) {
          const client = clients.get(t.server);
          if (!client) {
            return toolError(t.piName, `server "${t.server}" is not connected`);
          }
          try {
            const result = await client.callTool(t.mcpName, params || {});
            const content = (result && result.content) || [];
            return {
              content: content.length ? content : [{ type: "text", text: "" }],
              details: { server: t.server, tool: t.mcpName },
            };
          } catch (err) {
            // Hand the model a readable failure instead of throwing — a throw
            // surfaces as a broken turn rather than a tool it can retry/route
            // around.
            return toolError(t.piName, String(err && err.message));
          }
        },
      });
    }

    console.error(
      `[user_mcp] registered ${mapped.length} tool(s) from `
      + `${clients.size}/${servers.length} server(s)`);
  } catch (err) {
    // Last line of defense: pi must still start.
    console.error(`[user_mcp] bridge disabled (unexpected): ${err && err.message}`);
  }
}

function toolError(tool, message) {
  return {
    content: [{ type: "text",
                text: JSON.stringify({ ok: false, error: message, tool }) }],
    details: { ok: false },
  };
}
