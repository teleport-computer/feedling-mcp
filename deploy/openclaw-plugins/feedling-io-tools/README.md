# feedling-io-tools (OpenClaw plugin)

Canonical source for the OpenClaw plugin that exposes the Feedling tool CLI
(`tools/io_cli.py`) as native OpenClaw tools, so a resident agent (OpenClaw /
Hermes / Claude Code) can pull perception, read/recall memory, and read screen
with real agentic tool calls instead of a "make the model emit JSON" prompt.

This lives in-repo so it is version-controlled and redeployable — it used to
exist only on the VPS (`~/.openclaw/workspace/plugins/feedling-io-tools/`), which
meant the edits were lost on any VPS rebuild.

## What it does
- **Perception** — one tool per signal in `SIGNALS` (`perception_now`,
  `perception_mood`, …). Each shells out to `io_cli.py perception <signal>`;
  tool descriptions label fast vs slow signals.
- **Daily digest (perception history)** — `perception_trend` (rolling baseline +
  delta for a numeric field → `io_cli.py perception-trend`) and
  `perception_history` (per-day rollup docs → `io_cli.py perception-history`).
  These let the agent read the **accumulated daily digest** (look back over
  days vs the user's own norm), not just the `now` snapshot.
- **Memory** (A-full Phase-0, read side) — `memory_index` (compact readside
  index → `io_cli.py memory-index`) and `memory_fetch` (verbatim cards by id →
  `io_cli.py memory-fetch <ids>`). Both plaintext-safe (no client crypto);
  `memory_index` is fast, `memory_fetch` is slow.
  Memory *writes* stay on the consumer/client-encrypted action path, not this
  read plugin.
- **Screen** — `screen_recent` (frame metadata → `io_cli.py screen-recent`) and
  `screen_read` (decrypted caption/ocr of the latest frame → `io_cli.py
  screen-read`; pixels off unless `include_image`). Caption/OCR is fast;
  recent/image-heavy reads are slow.
- **Photo** — `photo_recent` (recent photo metadata: scene/time, no raw pixels →
  `io_cli.py photo-recent`). The agent uses scene/metadata, not bytes.
- **Actions (send/sleep/schedule_wake/…)** are NOT plugin tools — the agent emits
  them as output actions which the resident consumer parses. `io_cli.py
  send/wait-for-wake/schedule-wake` are graceful no-op stubs.
- No hardcoded paths/keys: config (openclaw.json) → env → throw. The service env
  file (`FEEDLING_API_URL`/`FEEDLING_API_KEY`/`FEEDLING_ENCLAVE_URL`) is read at
  call time.

## Keep SIGNALS in sync
`SIGNALS` in `index.js` MUST mirror the agent-pullable signals — i.e.
`AGENT_PERCEPTION_SIGNALS` in `backend/agent/perception_core.py` and the groups in
`tools/io_cli.py`. When a new signal is exposed to the agent, add it here too,
copy this file to the VPS, and restart the gateway (below).

## Deploy to the VPS
```bash
# from a checkout of feedling-mcp:
scp -i <key> deploy/openclaw-plugins/feedling-io-tools/{index.js,openclaw.plugin.json,package.json} \
  openclaw@<host>:~/.openclaw/workspace/plugins/feedling-io-tools/

# configure once (either in openclaw.json plugins.entries['feedling-io-tools'].config
# or as env on the gateway service):
#   FEEDLING_CONSUMER_ROOT=/home/openclaw/feedling-mcp      (has tools/io_cli.py)
#   FEEDLING_SERVICE_ENV=/home/openclaw/feedling-chat-resident.env
#   FEEDLING_PYTHON=python3   (optional)

# the gateway caches plugins — restart to reload:
systemctl --user restart openclaw-gateway.service
```
