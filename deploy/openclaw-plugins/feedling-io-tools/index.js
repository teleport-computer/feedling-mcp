import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";

const execFileAsync = promisify(execFile);
const SIGNALS = ["now","location","weather","motion","calendar","steps","sleep","workout","vitals","focus","audio_route","app","reminders","activity","body","metabolic","cycle","mood"];
const FAST_SIGNALS = new Set(["now","location","weather","motion","calendar","focus","audio_route"]);

// Declared in code so the gateway recognizes and forwards the configured config
// to register(). (Declaring it only in openclaw.plugin.json was not enough — the
// runtime definePluginEntry defaulted to an empty schema and config arrived empty.)
const CONFIG_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    consumerRoot: { type: "string" },
    serviceEnvPath: { type: "string" },
    pythonPath: { type: "string" },
    timeoutMs: { type: "integer", minimum: 1000 },
  },
};

function parseEnvFile(filePath) {
  const raw = readFileSync(filePath, "utf8");
  const env = {};
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    env[key] = value;
  }
  return env;
}

// No hardcoded deployment paths. config (openclaw.json) first, then env
// (per-host deployment config), then fail loudly — never guess a path.
function resolveConfig(config) {
  const cfg = config || {};
  const consumerRoot = cfg.consumerRoot || process.env.FEEDLING_CONSUMER_ROOT;
  const serviceEnvPath = cfg.serviceEnvPath || process.env.FEEDLING_SERVICE_ENV;
  if (!consumerRoot || !serviceEnvPath) {
    throw new Error(
      "feedling-io-tools not configured: set plugins.entries['feedling-io-tools'].config.{consumerRoot,serviceEnvPath} in openclaw.json, or FEEDLING_CONSUMER_ROOT / FEEDLING_SERVICE_ENV in the environment.",
    );
  }
  return {
    consumerRoot,
    serviceEnvPath,
    pythonPath: cfg.pythonPath || process.env.FEEDLING_PYTHON || "python3",
    timeoutMs: cfg.timeoutMs || 20000,
  };
}

// Generic io_cli.py invoker. `args` is the full argv after the script path
// (e.g. ["perception", "now"] or ["memory-index", "--limit", "5"]).
async function runCli(config, args) {
  const c = resolveConfig(config);
  const consumerRoot = path.resolve(c.consumerRoot);
  const serviceEnvPath = path.resolve(c.serviceEnvPath);
  const cliPath = path.join(consumerRoot, "tools", "io_cli.py");

  if (!existsSync(serviceEnvPath)) throw new Error(`service env file not found: ${serviceEnvPath}`);
  if (!existsSync(cliPath)) throw new Error(`io_cli.py not found: ${cliPath}`);

  const fileEnv = parseEnvFile(serviceEnvPath);
  const env = {
    ...process.env,
    FEEDLING_API_URL: fileEnv.FEEDLING_API_URL,
    FEEDLING_API_KEY: fileEnv.FEEDLING_API_KEY,
    FEEDLING_ENCLAVE_URL: fileEnv.FEEDLING_ENCLAVE_URL,
  };

  const { stdout, stderr } = await execFileAsync(
    c.pythonPath,
    [cliPath, ...args],
    { cwd: consumerRoot, env, timeout: c.timeoutMs, maxBuffer: 1024 * 1024 },
  );
  const text = (stdout || stderr || "").trim();
  if (!text) throw new Error(`empty response from io_cli.py for ${args.join(" ")}`);
  let parsed;
  try { parsed = JSON.parse(text); }
  catch (error) { throw new Error(`non-JSON response for ${args.join(" ")}: ${text.slice(0, 500)}`); }
  return parsed;
}

async function runPerception(config, signal) {
  return runCli(config, ["perception", signal]);
}

// Build io_cli argv from a tool's structured params. Order: positional first,
// then flags. Booleans become store_true flags only when true.
function flagsFromParams(params, spec) {
  const out = [];
  for (const [key, flag, kind] of spec) {
    const v = params ? params[key] : undefined;
    if (v === undefined || v === null || v === "") continue;
    if (kind === "bool") { if (v) out.push(flag); }
    else { out.push(flag, String(v)); }
  }
  return out;
}

function toolResult(payload) {
  return { content: [{ type: "text", text: JSON.stringify(payload) }] };
}

export default definePluginEntry({
  id: "feedling-io-tools",
  name: "Feedling IO Tools",
  description: "Expose Feedling IO perception/memory/screen CLI as native OpenClaw tools.",
  configSchema: CONFIG_SCHEMA,
  register(api, config = {}) {
    for (const signal of SIGNALS) {
      const costClass = FAST_SIGNALS.has(signal) ? "fast" : "slow";
      api.registerTool({
        name: `perception_${signal}`,
        description: `[${costClass}] Read Feedling perception signal: ${signal} (provider-safe tool name for perception.${signal})`,
        parameters: { type: "object", properties: {}, additionalProperties: false },
        async execute() {
          try {
            const payload = await runPerception(config, signal);
            return toolResult(payload);
          } catch (err) {
            return toolResult({ ok: false, error: (err && err.message) ? err.message : String(err), signal });
          }
        },
      });
    }

    // Helper: register a tool that maps structured params → io_cli argv.
    const registerCli = ({ name, description, parameters, build }) => {
      api.registerTool({
        name,
        description,
        parameters,
        async execute(params = {}) {
          try {
            return toolResult(await runCli(config, build(params || {})));
          } catch (err) {
            return toolResult({ ok: false, error: (err && err.message) ? err.message : String(err), tool: name });
          }
        },
      });
    };

    // memory.index — compact readside index (plaintext-safe).
    registerCli({
      name: "memory_index",
      description: "[fast] Read a compact index of the user's memory cards (provider-safe name for memory.index). Returns id/summary/bucket/threads/score — use memory_fetch for verbatim content.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 200, description: "max cards (default 50)" },
          bucket: { type: "string", description: "filter by bucket name" },
          thread: { type: "string", description: "filter by thread/dimension tag" },
          query: { type: "string", description: "free-text relevance query" },
          ambient: { type: "boolean", description: "ambient/background selection mode" },
        },
      },
      build: (p) => ["memory-index", ...flagsFromParams(p, [
        ["limit", "--limit", "value"],
        ["bucket", "--bucket", "value"],
        ["thread", "--thread", "value"],
        ["query", "--query", "value"],
        ["ambient", "--ambient", "bool"],
      ])],
    });

    // memory.fetch — verbatim decrypted cards by id (plaintext-safe).
    registerCli({
      name: "memory_fetch",
      description: "[slow] Fetch verbatim decrypted memory cards by id (provider-safe name for memory.fetch). Pass ids from memory_index.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["ids"],
        properties: {
          ids: { type: "array", items: { type: "string" }, minItems: 1, description: "memory card ids" },
          limit: { type: "integer", minimum: 1, maximum: 100, description: "max cards (default 20)" },
          include_archived: { type: "boolean" },
          include_superseded: { type: "boolean" },
        },
      },
      build: (p) => ["memory-fetch", ...(Array.isArray(p.ids) ? p.ids.map(String) : []), ...flagsFromParams(p, [
        ["limit", "--limit", "value"],
        ["include_archived", "--include-archived", "bool"],
        ["include_superseded", "--include-superseded", "bool"],
      ])],
    });

    // screen.recent — recent frame metadata (no pixels).
    registerCli({
      name: "screen_recent",
      description: "[slow] List recent screen frame metadata (provider-safe name for screen.recent). No pixels; use screen_read for a caption.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { limit: { type: "integer", minimum: 1, maximum: 100, description: "max frames (default 10)" } },
      },
      build: (p) => ["screen-recent", ...flagsFromParams(p, [["limit", "--limit", "value"]])],
    });

    // screen.read — decrypted caption/ocr for a frame (latest by default).
    registerCli({
      name: "screen_read",
      description: "[fast caption, slow image] Read the decrypted caption/ocr of a screen frame (provider-safe name for screen.read). Defaults to the latest frame; pixels off unless include_image.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          frame_id: { type: "string", description: "frame id; default = latest" },
          include_image: { type: "boolean", description: "include base64 JPEG (large)" },
        },
      },
      build: (p) => ["screen-read", ...flagsFromParams(p, [
        ["frame_id", "--frame-id", "value"],
        ["include_image", "--include-image", "bool"],
      ])],
    });

    // photo.recent — recent photo metadata (scene/time; no raw pixels).
    registerCli({
      name: "photo_recent",
      description: "[slow] List recent photo metadata (scene/time; provider-safe name for photo.recent). No raw pixels — the agent uses scene/metadata.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { limit: { type: "integer", minimum: 1, maximum: 100, description: "max photos (default 10)" } },
      },
      build: (p) => ["photo-recent", ...flagsFromParams(p, [["limit", "--limit", "value"]])],
    });

    // photo.read — one specific photo's details by id (metadata + optional image).
    registerCli({
      name: "photo_read",
      description: "[slow] Read one specific photo's details by id (provider-safe name for photo.read). Pass an id from photo_recent. Returns metadata; include_image decrypts the actual JPEG (large) for a closer look.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["id"],
        properties: {
          id: { type: "string", description: "photo id (from photo_recent)" },
          include_image: { type: "boolean", description: "include decrypted base64 JPEG (large)" },
        },
      },
      build: (p) => ["photo-read", ...flagsFromParams(p, [
        ["id", "--id", "value"],
        ["include_image", "--include-image", "bool"],
      ])],
    });

    // perception.trend — rolling baseline + delta for one numeric field (sense change vs norm).
    registerCli({
      name: "perception_trend",
      description: "[slow] Rolling baseline + delta for one numeric perception field over N days (provider-safe name for perception.trend). Read the daily digest to sense how a signal changed vs the user's own norm. e.g. signal=vitals field=resting_heart_rate.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["signal"],
        properties: {
          signal: { type: "string", description: "e.g. vitals/steps/sleep/weather/activity/metabolic/body" },
          field: { type: "string", description: "numeric field, e.g. resting_heart_rate / step_count / asleep_minutes" },
          days: { type: "integer", minimum: 1, maximum: 365, description: "lookback days (default 30)" },
        },
      },
      build: (p) => ["perception-trend", String(p.signal || ""), ...flagsFromParams(p, [
        ["field", "--field", "value"],
        ["days", "--days", "value"],
      ])],
    });

    // perception.history — raw per-day rollup docs for a signal (the accumulated daily digest).
    registerCli({
      name: "perception_history",
      description: "[slow] Per-day rollup docs for a perception signal over N days (provider-safe name for perception.history). This is the accumulated daily digest — use it to look back over days, not just 'now'.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["signal"],
        properties: {
          signal: { type: "string", description: "e.g. vitals/sleep/motion/location/calendar/reminders/mood/now_playing" },
          days: { type: "integer", minimum: 1, maximum: 365, description: "lookback days (default 14)" },
        },
      },
      build: (p) => ["perception-history", String(p.signal || ""), ...flagsFromParams(p, [
        ["days", "--days", "value"],
      ])],
    });

    // schedule_wake — ask to be woken at a later time (native self-wake; replaces the old JSON action).
    registerCli({
      name: "schedule_wake",
      description: "Ask to be woken at a later time so you can come back to something (provider-safe name for schedule.wake). e.g. at='2026-06-29T18:00' reason='check in after their meeting'.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["at"],
        properties: {
          at: { type: "string", description: "when to wake: ISO time (2026-06-29T18:00) or a relative spec" },
          tz: { type: "string", description: "IANA timezone (optional; defaults to the user's)" },
          reason: { type: "string", description: "why you're scheduling it (optional)" },
        },
      },
      build: (p) => ["schedule-wake", ...flagsFromParams(p, [
        ["at", "--at", "value"],
        ["tz", "--tz", "value"],
        ["reason", "--reason", "value"],
      ])],
    });

    // cancel_wake — cancel a previously scheduled self-wake.
    registerCli({
      name: "cancel_wake",
      description: "Cancel a previously scheduled self-wake (provider-safe name for cancel.wake). Pass the wake/timer id.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["wake_id"],
        properties: {
          wake_id: { type: "string", description: "the scheduled wake/timer id to cancel" },
          reason: { type: "string", description: "why (optional)" },
        },
      },
      build: (p) => ["cancel-wake", ...flagsFromParams(p, [
        ["wake_id", "--wake-id", "value"],
        ["reason", "--reason", "value"],
      ])],
    });
  },
});
