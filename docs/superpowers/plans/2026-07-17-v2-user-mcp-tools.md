# V2 Hosted Runtime — user MCP tool support

**Goal:** Let the V2 native tool loop expose a user's configured MCP servers as
callable tools and proxy `tools/call` to their endpoint, so hosted (db_action_v2)
accounts get the MCP capability that today only the resident/CLI path has.

**Architecture:** Reuse the existing `user_mcp` storage, routes, SSRF guard, CA
pinning, and `mcp__<server>__<tool>` naming. Add an async MCP JSON-RPC client
(`list_tools` with schemas + `tools/call`). At each chat-lane turn, load + decrypt
the user's enabled servers (enclave, runtime-token), fetch their tools, inject
namespaced ToolSpecs into the loop's catalog, and dispatch `mcp__*` calls by
proxying to the user's endpoint through the SSRF-guarded client.

## Global constraints (verbatim)

- BYOK-only; no platform-LLM-key fallback. No change to that invariant.
- The backend/serve-worker outbound MCP call MUST pass `blocked_url_kind` SSRF
  guard (non-global host → refuse to connect). Redirects stay disabled. CA
  pinning (`ca_pem`) honored.
- Secrets (url+headers+ca_pem) only ever live in `config_envelope`; decrypt via
  `core_enclave._decrypt_envelope_via_enclave(env, api_key, purpose="mcp_server_config", runtime_token=...)`.
- MCP exposed on the **chat lane only** (foreground), never wake/proactive —
  mirrors resident (`_user_mcp_cli_value`: claude gets `--mcp-config` on chat lane
  only).
- **Decision 1 (freshness): fetch per-turn.** initialize+tools/list per enabled
  server at turn start. Skip entirely when the `user_mcp` fingerprint shows zero
  enabled servers (common case = zero cost). Short timeout + graceful skip of a
  slow/down server.
- **Decision 2 (security): MCP results are TRUSTED.** MCP tool calls are NOT added
  to `provenance.EXTERNAL_READS` and do NOT set `external_content_seen`; the model
  may still do writes after an MCP call in the same turn. (User's product call:
  the MCP server is the user's own.)
- Scope: `tools` only (tools/list + tools/call). No MCP resources/prompts.
- Do not regress the 4429-passing baseline or the existing user_mcp tests.

## Tasks

### Task 1 — `backend/hosted/mcp_client.py`: async MCP JSON-RPC client
Extract the reusable JSON-RPC substrate and add schema-carrying `list_tools` +
`call_tool`. Reuse `mcp_probe.blocked_url_kind`, `_parse_rpc_response`,
`_classify_http`, `ProbeError`, `_PROTOCOL_VERSION`, timeouts. Factor the
`initialize`→session-id→`notifications/initialized` handshake into a shared async
helper; `mcp_probe.probe` may keep its own or delegate (do not break its 8 tests).
- `list_tools(url, headers, *, ca_pem=None, transport=None) -> list[dict]` —
  returns full tool objects `{name, description, inputSchema}`.
- `call_tool(url, headers, tool_name, arguments, *, ca_pem=None, transport=None) -> dict` —
  `tools/call`; returns `{content: [...], isError: bool}` normalized to text.
- Async-native (V2 dispatch is async); provide the SSRF check before connect.
- Tests (`tests/test_mcp_client.py`): in-process ASGI fake MCP server (mirror
  test_user_mcp_probe.py's `transport=`): list_tools returns schemas; call_tool
  happy path; call_tool error (isError) surfaced; SSRF non-global refused;
  headers forwarded; CA pem honored; server 5xx → ProbeError.

### Task 2 — `backend/hosted/mcp_tools.py`: per-turn MCP tool provider
**Layer correction (final):** lives in `hosted/`, NOT `v2/`. The
`test_v2_dependency_direction` guard forbids EVERY `v2/*.py` core module (derived
from the directory) from importing `hosted`, so the MCP loader — which needs
`mcp_core`/`mcp_client`/`core.enclave` — must live in `hosted` and be injected
into the worker via `TurnDeps.load_mcp_turn` (wired in
`serve_worker.build_production_deps`, the one assembly module exempt from the
guard). The worker duck-types the returned `McpTurn` (`.tool_specs`/`.handles`/
`.dispatch`); no hosted type crosses the import boundary. `worker._EMPTY_MCP_TURN`
is the fallback when the dep is unwired (wake lane / legacy / tests). Given `store`, `api_key`,
`runtime_token`, `enclave_sem`:
- `load_turn_mcp(store, *, api_key, runtime_token) -> McpTurn` where `McpTurn`
  holds `tool_specs: list[ToolSpec]` (namespaced `mcp__<server>__<tool>`, schema
  from inputSchema) and `dispatch(tc) -> ToolResult` (routes a namespaced call to
  `mcp_client.call_tool` against the decrypted server).
- Zero enabled servers (via `mcp_core` fingerprint/list) → empty McpTurn (no
  network).
- Per-server list_tools failure → skip that server's tools (log, never raise).
- Name collision guard: `mcp__` prefix cannot collide with platform tools; if two
  servers expose the same tool, server name namespaces them.
- Tests (`tests/test_v2_mcp_tools.py`): builds namespaced specs from a fake
  server; dispatch proxies to call_tool; down server skipped; no servers → empty;
  decrypt failure → skip + no raise.

### Task 3 — `tool_loop.py`: accept per-turn extra tool specs
- `run_tool_loop(..., extra_tool_specs: list[ToolSpec] | None = None)`.
- Catalog for the turn = base `_catalog()` + `extra_tool_specs`.
- `offered_names` already covers them. `validate_tool_args`: MCP tool names
  (those in extra_tool_specs) bypass the PARAMS check — pass-through (the MCP
  server validates its own args; user chose trusted). Do this WITHOUT weakening
  validation for platform tools.
- Per Decision 2, MCP names are NOT external reads: no change to
  `external_content_seen`/`WRITE_ACTIONS` gating for them.
- Tests (`tests/test_v2_tool_loop_mcp.py`): a turn offering an injected MCP spec
  gets it in `tools=`; model calling it dispatches (not rejected as malformed);
  a write after the MCP call is still allowed (Decision 2); unknown non-MCP name
  still rejected.

### Task 4 — `executor.py` + `worker.py` chat-lane wiring
- `worker.py` chat lane: build `McpTurn` once per turn (Task 2), pass
  `extra_tool_specs=mcp_turn.tool_specs` into `run_tool_loop`, and have the
  chat-lane `_dispatch_tools` route `mcp__*` calls to `mcp_turn.dispatch` while
  passing the rest to `v2_executor.dispatch_tool_calls`.
- Wake lane: unchanged (no MCP).
- `executor.py`: only touch if the dispatch split can't live entirely in
  worker's `_dispatch_tools`. Prefer keeping executor MCP-agnostic.
- Tests (`tests/test_v2_worker_mcp.py`): chat-lane turn with a configured server
  injects specs + dispatches to the server; wake lane injects nothing.

## Verify
Postgres 127.0.0.1:55432. Full suite:
`python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`
Baseline before this work: 4429 passed. Each task: new tests green + no regression.
End-to-end on pre after deploy: configure an MCP server on the V2 test account,
send a message that needs the tool, confirm the turn calls it.
