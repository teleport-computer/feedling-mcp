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

// 软上限。原值 50,2026-08-09 提到 100:一个用户装了 6 个服务器(仅 gardenforum
// 一个就 25 个工具),超出部分**按服务器名字母序**被丢掉,而 tavily 恰好排最后
// —— 于是「测试连接通过、AI 却说搜不到」。上限本身是防御性的(挡住工具面爆炸),
// 不是产品意图,所以放宽而不是取消;真正的修复是让它**可见**(见 index.js 里
// 无条件输出的 surface 行),别再静默丢弃。
// 与 hosted/mcp_tools.py 的 MAX_MCP_TOOLS_PER_TURN **保持同一个数**。
// 两条路不同 = 同一个用户换个 driver 行为就变,这一整周修的就是这类不一致。
// 128 是实测出来的(见 tools/e2e/tool_count_ceiling_probe.py 与那边的注释):
// 500 个工具都没撞硬墙、弱模型 300 个仍全对,真正的代价是每轮的 token;
// 128 ≈ 23k token,而且能把本月工具最多的真实用户(6 台共 107 个)全装下。
export const MAX_TOOLS = 128;
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
  const byServer = new Map();
  for (const s of servers || []) {
    const list = [];
    for (const t of s.tools || []) {
      if (!t || !t.name) continue;
      list.push(t);
    }
    // 每台服务器内部按工具名排序,保证同一台的取舍也是确定的。
    list.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    byServer.set(s.name, list);
  }
  // 服务器之间按名字排序:分配顺序不能依赖握手完成的先后。
  const names = [...byServer.keys()].sort();

  // 轮转分配,而不是「排好序取前 N 个」。
  //
  // 旧做法是把所有工具排成一列后截断,于是**字母序靠后的服务器整台饿死**:
  // usr_1baf 装了 6 台(gardenforum 一台就 25 个工具),排最后的 tavily 一个
  // 工具都没注册上 —— 用户看到的是「连接测试通过,AI 却说搜不到」。
  // 轮转让每台先各拿一个,再拿第二个……即使总数超限,每台也都有代表工具;
  // 只有工具特别多的那台会被削顶。仍然完全确定(排序 + 固定轮次)。
  const mapped = [];
  const dropped = [];
  const taken = new Set();
  const cursor = new Map(names.map((n) => [n, 0]));
  let progressed = true;
  while (progressed && mapped.length < MAX_TOOLS) {
    progressed = false;
    for (const server of names) {
      if (mapped.length >= MAX_TOOLS) break;
      const list = byServer.get(server) || [];
      const i = cursor.get(server);
      if (i >= list.length) continue;
      cursor.set(server, i + 1);
      progressed = true;
      const t = list[i];
      const piName = piToolName(server, t.name, taken);
      taken.add(piName);
      mapped.push({
        piName,
        server,
        mcpName: t.name,
        description: t.description
          || `MCP tool "${t.name}" from server "${server}"`,
        // Passed through verbatim: pi accepts a bare JSON Schema (it branches on
        // !hasTypeBoxMetadata && isJsonSchemaObject — pi-ai validation.js:257).
        parameters: t.inputSchema || { type: "object", properties: {} },
      });
    }
  }
  for (const server of names) {
    const list = byServer.get(server) || [];
    for (let i = cursor.get(server); i < list.length; i++) {
      dropped.push(`${server}/${list[i].name}`);
    }
  }
  return { mapped, dropped };
}

/**
 * 每台服务器「发现了几个 / 真正注册了几个」。
 *
 * ⚠️ 报告必须用**注册后**的数字。第一版只报发现数(connected[].tools.length),
 * 于是一台服务器的工具全被丢掉时,日志里照样写着 `tavily:4` ——
 * 恰好把这条埋点要回答的那个问题答错了(codex 审出)。
 */
export function surfaceCounts(servers, mapped) {
  const registered = new Map();
  for (const m of mapped || []) {
    registered.set(m.server, (registered.get(m.server) || 0) + 1);
  }
  return (servers || [])
    .map((s) => {
      const found = (s.tools || []).length;
      const kept = registered.get(s.name) || 0;
      return `${s.name}:${kept}/${found}`;
    })
    .join(",");
}

/**
 * 工具面的总 UTF-8 字节数 —— 数量之外的另一半成本(codex 提)。
 *
 * ⚠️ 必须用 `Buffer.byteLength(..., "utf8")`,不能用 `String.length`:
 * 后者数的是 UTF-16 码元,中文描述会**少算一半以上**、emoji 更离谱,
 * 而请求体是按 UTF-8 字节走的。这个指标本来就是拿来判断"工具面是不是太大"的,
 * 量错了就没意义。
 */
export function schemaBytes(mapped) {
  let total = 0;
  for (const m of mapped || []) {
    try {
      total += Buffer.byteLength(JSON.stringify(m.parameters || {}), "utf8");
      total += Buffer.byteLength(String(m.description || ""), "utf8");
    } catch (_e) {
      // 循环引用之类:算不出来就跳过,不能因为统计把整轮弄挂。
    }
  }
  return total;
}
