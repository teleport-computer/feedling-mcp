/**
 * Pure MCP-tool → pi-tool mapping. No I/O, no network — unit-testable alone.
 *
 * pi carries gemini, whose tool names must match ^[a-zA-Z0-9_-]{1,64}$. MCP tool
 * names come from the user's server and are unconstrained, so every name gets
 * sanitized, length-capped, and de-duplicated.
 *
 * DETERMINISM IS A HARD REQUIREMENT: the same (server, tool) set must always
 * produce the same pi names. Servers finish their handshakes in whatever order
 * the network gives us, so the table is sorted before names are assigned — a
 * name that shifts between turns makes the model see a different toolset each
 * turn, and any tool the model remembers from earlier stops resolving.
 */

export const MAX_TOOLS = 50;
const MAX_NAME_LEN = 64;

function sanitizeSegment(s) {
  return String(s == null ? "" : s).replace(/[^a-zA-Z0-9_-]/g, "_");
}

/** FNV-1a → base36. Deterministic across processes (unlike hashing objects). */
function shortHash(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(36).slice(0, 6);
}

/**
 * @param {string} server  already constrained upstream to [a-z0-9_-]{1,32}
 * @param {string} tool    arbitrary text from the user's MCP server
 * @param {Set<string>} taken  names already assigned in this pass
 */
export function piToolName(server, tool, taken) {
  const base = `mcp_${sanitizeSegment(server)}_${sanitizeSegment(tool)}`;
  const capped = base.slice(0, MAX_NAME_LEN);
  if (!taken || !taken.has(capped)) return capped;
  // Collision: derive the suffix from the FULL original pair, not from a
  // counter — a counter would depend on iteration order and drift between turns.
  // "|" is a safe separator (keeps the encoding injective): server names are
  // constrained upstream to [a-z0-9_-]{1,32}, so one can never contain it.
  const suffix = `_${shortHash(`${server}|${tool}`)}`;
  return base.slice(0, MAX_NAME_LEN - suffix.length) + suffix;
}

/**
 * @param {Array<{name: string, tools: Array<{name, description, inputSchema}>}>} servers
 * @returns {{mapped: Array<{piName, server, mcpName, description, parameters}>,
 *            dropped: string[]}}
 */
export function buildToolTable(servers) {
  const pairs = [];
  for (const s of servers || []) {
    for (const t of s.tools || []) {
      if (!t || !t.name) continue;
      pairs.push({ server: s.name, tool: t });
    }
  }
  // Sort so name assignment never depends on handshake completion order.
  pairs.sort((a, b) => {
    if (a.server !== b.server) return a.server < b.server ? -1 : 1;
    if (a.tool.name !== b.tool.name) return a.tool.name < b.tool.name ? -1 : 1;
    return 0;
  });

  const taken = new Set();
  const mapped = [];
  const dropped = [];
  for (const p of pairs) {
    if (mapped.length >= MAX_TOOLS) {
      dropped.push(`${p.server}/${p.tool.name}`);
      continue;
    }
    const piName = piToolName(p.server, p.tool.name, taken);
    taken.add(piName);
    mapped.push({
      piName,
      server: p.server,
      mcpName: p.tool.name,
      description: p.tool.description
        || `MCP tool "${p.tool.name}" from server "${p.server}"`,
      // Passed through verbatim: pi accepts a bare JSON Schema (it branches on
      // !hasTypeBoxMetadata && isJsonSchemaObject — pi-ai validation.js:257).
      parameters: p.tool.inputSchema || { type: "object", properties: {} },
    });
  }
  return { mapped, dropped };
}
