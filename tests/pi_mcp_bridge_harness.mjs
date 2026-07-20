// Harness for tests/test_pi_mcp_bridge.py — runs a piece of the bridge under
// node and prints a JSON result on stdout.
//
// Usage:
//   node pi_mcp_bridge_harness.mjs client <url>
//     → {"tools": [...]} | {"error": "..."}
//   node pi_mcp_bridge_harness.mjs mapping <json-of-servers>
//     → {"mapped": [...], "dropped": [...]}
//   node pi_mcp_bridge_harness.mjs extension
//     → {"tools": [{name, description, parameters}], "threw": false}
//
// The bridge is plain .js precisely so this harness can import() it with no
// build step and no type-stripping — see the design doc §4.1.

import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BRIDGE = path.resolve(HERE, "..", "tools", "pi_mcp_bridge");

const [, , mode, arg] = process.argv;

async function main() {
  if (mode === "client") {
    const { McpClient } = await import(path.join(BRIDGE, "mcp_client.js"));
    const client = new McpClient(arg, {}, { timeoutMs: 5000 });
    await client.initialize();
    const tools = await client.listTools();
    const called = await client.callTool(tools[0].name, { q: "x" });
    return { tools, called };
  }
  if (mode === "sse-client" || mode === "sse-client-noclose") {
    // Drive the legacy HTTP+SSE client against a real loopback server.
    // "sse-client-noclose" deliberately does NOT call close(): the still-open
    // GET stream is left as the only outstanding handle, so the process can
    // only exit if that stream's socket was unref'd. That is the real
    // production shape (a connected bridge client is never explicitly closed),
    // and the only version that actually proves unref — calling close() would
    // destroy the socket regardless of unref and mask a regression.
    const { SseMcpClient } = await import(path.join(BRIDGE, "mcp_client.js"));
    const client = new SseMcpClient(arg, {}, { timeoutMs: 5000 });
    await client.initialize();
    const tools = await client.listTools();
    const called = await client.callTool(tools[0].name, { q: "x" });
    if (mode === "sse-client") client.close();
    return { tools, called };
  }
  if (mode === "transport") {
    // effectiveTransport routing — pure, no network.
    const { effectiveTransport } = await import(path.join(BRIDGE, "mcp_client.js"));
    return JSON.parse(arg).map((cfg) => effectiveTransport(cfg));
  }
  if (mode === "mapping") {
    const { buildToolTable } = await import(path.join(BRIDGE, "tool_mapping.js"));
    return buildToolTable(JSON.parse(arg));
  }
  if (mode === "extension") {
    const mod = await import(path.join(BRIDGE, "index.js"));
    const tools = [];
    const pi = {
      registerTool: (t) => tools.push(t),
      on: () => {},
      registerCommand: () => {},
    };
    let threw = false;
    try {
      await mod.default(pi);
    } catch (err) {
      threw = true;
      return { threw, error: String(err && err.message) };
    }
    // Exercise every registered tool once so execute() paths are covered too.
    const executed = [];
    for (const t of tools) {
      const r = await t.execute("call-1", { q: "x" }, undefined, undefined, {});
      executed.push({ name: t.name, content: r.content });
    }
    return {
      threw,
      tools: tools.map((t) => ({
        name: t.name, description: t.description, parameters: t.parameters,
      })),
      executed,
    };
  }
  throw new Error(`unknown mode: ${mode}`);
}

main().then(
  (out) => { process.stdout.write(JSON.stringify(out)); },
  (err) => { process.stdout.write(JSON.stringify({ error: String(err && err.message) })); },
);
