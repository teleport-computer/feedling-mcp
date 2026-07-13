# Spec: Re-implement the pi driver consolidation on test's multi-profile schema

**Date:** 2026-07-13
**Status:** Draft (awaiting review)
**Base branch:** `test` (b6073c6) — NOT the stale `pi-driver`.

> `docs/superpowers/` is LOCAL. This spec re-homes the pi consolidation that
> currently lives only on `pi-driver` onto the `model_api_routes/credentials`
> schema that `test` adopted via 0014 (PR #67/#68).

---

## 1. Why this exists

`pi-driver` carries a finished pi consolidation (retire the in-CVM LiteLLM
gateway; route gemini/openrouter/openai_compatible/deepseek through a native
`pi` driver). It was built against the OLD `user_blobs(kind='model_api')` JSONB
model. `test` has since replaced that model with two normalized tables
(`model_api_credentials` + `model_api_routes`, migration `0014`). A mechanical
rebase produces broken code (it drops test's route-JOIN discovery). So the pi
work must be **re-implemented on the new schema**. On `test`, pi is 100%
greenfield — there is no pi code anywhere; `pi-driver` is the reference design.

## 2. What does NOT change (the lucky part)

`db.list_agent_runtime_enabled_users` on the new schema still returns the SAME
dict shape the pi downstream consumes:
`{user_id, driver, provider, model, base_url, supports_responses, reasoning_effort}`.
So the schema split is contained almost entirely in that one function's SQL.
Everything downstream (spawners, supervisor, driver derivation, models.json)
transplants from `pi-driver` with only cosmetic edits. The pi work never reads
credentials/routes directly — it consumes discovery output.

Also: `test` REVERTED the self-authored "thinking_fallback" feature
(`306b061`, `cffe486`), so we carry NO `thinking_fallback` field — simpler than
the last rebase.

## 3. Target driver map (unchanged from the whole project)

- `anthropic` → **claude** (native Anthropic wire)
- `openai` → **codex** (native OpenAI Responses)
- `gemini` / `deepseek` / `openrouter` / `openai_compatible` → **pi**
  - gemini → pi `google-generative-ai`
  - openrouter → pi `openai-completions` @ fixed `https://openrouter.ai/api/v1` + Referer/Title headers
  - openai_compatible → pi `openai-completions` @ user `base_url`
  - deepseek → pi `anthropic-messages` @ `<base or api.deepseek.com>/anthropic`, **text-only**

After this, NO provider needs the gateway; the in-CVM LiteLLM gateway is retired.

## 4. Changes by component

### 4.1 Driver derivation — `backend/hosted/agent_runtime_cutover.py`
- `_CLAUDE_PROVIDERS = {"anthropic"}` (drop deepseek → pi).
- `_CODEX_PROVIDERS = {"openai"}` (native only; drop gemini/openrouter/openai_compatible).
- `_PI_PROVIDERS = {"openai_compatible", "gemini", "openrouter", "deepseek"}`.
- `driver_for_provider`: add pi branch → returns `"pi"`; codex only for openai.
- `codex_transport`: only `"native"`/`""` (gateway branch deleted).
- `assert_hosting_ready`: drop the `FEEDLING_LITELLM_ENABLE` requirement (keep
  `FEEDLING_HOST_ALL` + `FEEDLING_RUNTIME_TOKEN_SECRET`).
- Heartbeat gate: drop `require_gateway`; add `require_pi` (a `pi` capability
  bit on the heartbeat), mirroring how `gateway` was gated. `check_supervisor_live`
  gains `require_pi` and the send-gate passes it when the user's driver is pi.
- Keep the "sync with the SQL CASE" invariant comment pointing at db.py.

### 4.2 Discovery — `backend/db.py::list_agent_runtime_enabled_users`
Rewrite on top of test's route-JOIN body (do NOT reintroduce JSONB):
- CASE: `anthropic/claude → claude`; `openai → codex`; **ELSE → pi**
  (so deepseek/gemini/openrouter/openai_compatible all derive pi).
- Discovery becomes **unconditional**: drop the `include_gateway` param and the
  provider allowlist gating — every fit provider with an active, test_ok route is
  discovered every tick (no gateway proxy to avoid anymore). The `providers`
  allowlist becomes the full fit set `{anthropic, claude, deepseek, openai,
  gemini, openrouter, openai_compatible}`.
- Keep the `model_api_routes r JOIN model_api_credentials c` body, the
  `WHERE r.is_active AND r.test_status='ok'` predicate, and the returned keys
  exactly (incl. `reasoning_effort` from `r`, `supports_responses`/`base_url`
  from `c`).

### 4.3 pi models.json + spawn — `backend/agent_runtime/spawners.py`
Transplant from `pi-driver`, with ONE upgrade over the pi-driver version —
**native reasoning forwarding, no gateway** (see §7.1):
- Add `_pi_models_json(*, base_url, model, provider, reasoning_effort)` with the
  four per-provider branches (gemini / openrouter / openai_compatible /
  deepseek) as on `pi-driver`. `apiKey: "$PI_PROVIDER_API_KEY"`; vision
  `input:["text","image"]` except deepseek text-only.
- **Reasoning (DECISION: preserve, gateway-free).** When the route carries a
  `reasoning_effort` (low/medium/high; `off`/`""` = disabled), pi must forward it
  natively on its own wire — NOT via any proxy. Concretely: set
  `compat.supportsReasoningEffort: True` for the provider AND inject the effort
  into pi's per-model reasoning config in `models.json`, so pi emits the
  provider-appropriate reasoning param (openrouter chat wire: top-level
  `reasoning: {effort}`; gemini: its native thinking config). The gateway's
  old `reasoning: {effort, summary:"auto"}` (Responses wire) is the behavior to
  match, re-expressed on pi's chat/google wire. Effort `off`/empty → omit
  reasoning entirely (`supportsReasoningEffort: False`, conservative wire).
  The EXACT pi models.json field that carries the effort value is verified in the
  §7.1 pre-spike; openrouter is the must-pass case. openai_compatible stays
  conservative (reasoning only when the route explicitly sets an effort, since
  relay compat is the weak point).
- Add a `pi` branch to `_default_cli_cmd`:
  `pi --mode json -t bash --append-system-prompt <prompt> --model feedling/<model> --session-id {session_id}`
  (STDIN-fed message; images as native `@<path>` refs).
- `agent_home_files` / `materialize_home` / `stale_home_files`: add a pi branch
  writing `pi-home/agent/models.json`; pi needs no codex-style gateway config.
- `consumer_env`: pi branch sets `PI_PROVIDER_API_KEY` = the decrypted provider
  key; drop all `FEEDLING_LITELLM_*` passthrough keys.
- DELETE `_codex_gateway_config`, `_GATEWAY_PROVIDER_ID`, the codex gateway
  transport branch, and gateway env passthrough.

### 4.4 Supervisor — `backend/agent_runtime/supervisor.py`
- `_discover_enabled()` → no `include_gateway`; project the same keys (no
  thinking_fallback).
- Delete `_gateway_entries`, `_owned_gateway_entries`, `_drop_gateway_users`,
  `_wire_gateway_models`, `_reconcile_gateway`, `gateway_enabled`, the
  `litellm_gateway` import and the gateway child spawn.
- `_effective_roster` returns just `roster` (no gateways tuple).
- `pi_enabled = True`; heartbeat payloads carry `pi` (not `gateway`).
- `_spawn_identity` respawn tuple: keep provider/model/base_url so a pi route
  change respawns; drop the gateway-key special case.

### 4.5 Resident consumer — `tools/chat_resident_consumer.py`
Transplant pi stream handling from `pi-driver`: `_pi_turn_from_stream`,
`_pi_turn_metrics`, pi branch in `_cli_error_detail` (message_end
stopReason=error), and fold pi thinking via `_attach_provider_reasoning(...,
source="pi_thinking")`. `_is_pi_cmd` guard on the call path.

### 4.6 Send gate — `backend/hosted/chat_send_core.py`
`require_pi = driver_for_provider(provider) == "pi"`;
`check_supervisor_live(require_pi=require_pi)`.

### 4.7 Retire LiteLLM — delete + scrub
- Delete `backend/agent_runtime/litellm_gateway.py` + `tests/test_litellm_gateway.py`.
- Remove `FEEDLING_LITELLM_*` from `deploy/Dockerfile.agent-runner`, all
  `deploy/docker-compose.*.yaml`, `.github/workflows/ci.yml` jobs,
  `tools/gen_url_map.py`, and `tests/conftest.py`.
- Add `tests/test_no_litellm_anywhere.py` guard.

### 4.8 Docs
`backend/agent_runtime/README.md` driver table; `deploy/DEPLOYMENTS.md`
(gateway env → two-var host-all set); `docs/CHANGELOG.md`.

## 5. Testing

All discovery/setup tests already seed the NEW tables (`model_api_credential_create`
+ `model_api_route_upsert/_activate/_mark_test`). So:
- Discovery: extend `test_agent_runtime_discovery.py` — seed gemini/openrouter/
  openai_compatible/deepseek routes, assert driver `pi`; assert discovery is
  unconditional (no include_gateway).
- Spawners: pi models.json per-provider tests (gemini api, openrouter baseUrl+
  headers, openai_compatible base_url, deepseek anthropic-messages text-only);
  pi cli-cmd; codex never writes config.toml.
- Cutover: `driver_for_provider` pi cases; `assert_hosting_ready` no longer needs
  litellm; heartbeat `require_pi`.
- Consumer: pi stream reply/thinking/error tests.
- Guard: `test_no_litellm_anywhere`.
- Full suite must stay green except the pre-existing baseline failures on `test`.

## 6. Rollout
Land on a fresh branch off `test`; validate on pre (real gemini/openrouter/
deepseek/openai_compatible keys) — deepseek `anthropic-messages` auth + openrouter
reasoning are the things to watch — then prod. Deploy backend + runner together
(discovery/heartbeat contract changes).

## 7. Open questions / risks
### 7.1 openrouter reasoning — DECIDED: preserve, gateway-free (pre-spike required)
`test` added openrouter reasoning summaries/effort *through the gateway*
(`7201ebd`, `4588254`): Responses-wire `reasoning:{effort, summary:"auto"}`,
effort sourced from the route's `reasoning_effort`. **Decision: keep reasoning
forwarding, but with ZERO gateway** — pi emits reasoning natively on its own
wire (§4.3). The one unknown is pi's exact models.json field for the effort
value; resolve it with a **pre-spike on `pre` before the plan's reasoning task
is marked done**:
- Spike: configure a pi openrouter provider with `supportsReasoningEffort:True`
  + effort `high`; run one turn; confirm a REAL reasoning chain comes back
  (reasoning tokens > 0 / visible summary), matching the gateway's prior result.
- Acceptance: openrouter reasoning works via pi with no `litellm`/gateway
  process anywhere; effort `off` produces no reasoning param.
- If pi cannot forward reasoning on the chat wire at all (unexpected), STOP and
  escalate — do not silently fall back to a gateway.

### 7.2 Other risks
- **pi anthropic-messages auth for deepseek** — unverified wire (x-api-key vs
  Bearer) against `api.deepseek.com/anthropic`; pre-spike first.
- **supports_responses** is now on the credential and irrelevant to pi (pi
  speaks chat/completions natively); still returned by discovery and simply
  ignored on the pi path — no probing needed for pi providers.

## 8. Out of scope
- Moving `anthropic` itself to pi / retiring the claude driver.
- Any change to the credentials/routes setup path (pi only consumes discovery).
- Deleting the retained `user_blobs(kind='model_api')` rollback snapshot.
