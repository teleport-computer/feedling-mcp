# Spec: Retire LiteLLM — route gemini/openrouter through pi natively

> **RETIRED / DO NOT DEPLOY.** Historical design; resident/supervisor topology
> references are not current operations.

**Date:** 2026-07-07
**Status:** Draft (awaiting review)
**Author:** brainstormed with Claude
**Scope owner:** hosted agent-runtime (`backend/agent_runtime/`, `backend/hosted/`)

> Team convention: files under `docs/superpowers/` are kept LOCAL and are NOT
> committed to git. This spec lives here for the brainstorm→plan→implement cycle
> only.

---

## 1. Goal

Remove the in-CVM **LiteLLM gateway** entirely. Route the two providers that
currently depend on it — **gemini** and **openrouter** — through the **pi**
driver's native provider adapters instead of `codex + LiteLLM`.

After this change the runner runs **no LiteLLM proxy child**, ships **no LiteLLM
venv**, and the deploy surface carries **no `FEEDLING_LITELLM_*`** knobs.

## 2. Why this is possible

LiteLLM's ONLY job in this codebase is the in-CVM gateway that translates
**codex's OpenAI-Responses wire** into the gemini / openrouter wire. Verified:

- `backend/provider_client.py` and `backend/hosted/setup_core.py` reference
  LiteLLM only in **comments** — no runtime calls.
- The legacy inline chat path (`backend/hosted/turn.py`, `backend/chat/`) has
  **zero** LiteLLM references.
- So LiteLLM has exactly one consumer: `backend/agent_runtime/litellm_gateway.py`
  spawned by the supervisor for codex-gateway users.

pi speaks these providers natively (confirmed by inspecting the deployed
`@earendil-works/pi-ai@…` adapters on the pre runner):

| provider   | pi wire (`models.json` `api`) | auth | notes |
|------------|-------------------------------|------|-------|
| openrouter | `openai-completions`          | `apiKey: $ENV` (Bearer) | OpenRouter is OpenAI-compatible; base `https://openrouter.ai/api/v1`. Optional `HTTP-Referer` / `X-Title` via provider `headers`. |
| gemini     | `google-generative-ai`        | `apiKey: $ENV` (pi's adapter handles `x-goog-api-key` internally) | pi ships a native Google Generative AI adapter (`api/google-generative-ai.js`). |

Once gemini/openrouter move to pi, the gateway has **zero users** → LiteLLM is
dead code and is removed in the same change.

## 3. Scope decisions (resolved in brainstorming)

1. **Codex driver stays — minimal scope.** Only gemini/openrouter move to pi.
   `openai` keeps using **codex-native** (direct OpenAI Responses; never touched
   LiteLLM; empirically stable). Final drivers: `claude` (anthropic/deepseek),
   `codex` (openai only), `pi` (openai_compatible/gemini/openrouter).
2. **Retire the `FEEDLING_PI_DRIVER_ENABLE` flag — pi is unconditional.** With
   LiteLLM gone there is no `codex+gateway` fallback for these providers, so a
   flag "off" state is meaningless. `openai_compatible`/`gemini`/`openrouter`
   derive to pi unconditionally. Rollout safety comes from **validating the whole
   thing in `pre` first**, then a big-bang `prod` deploy (prod user count is
   intentionally tiny).

## 4. Current architecture (what we're changing)

```
provider ──derive──> driver ──transport──> reaches model via
anthropic/deepseek     claude              Anthropic wire (direct)
openai                 codex               OpenAI Responses (direct)         <-- unchanged
gemini      ┐          codex     ┐         codex Responses ─> LiteLLM gw ─> gemini wire
openrouter  ┘(gateway) codex     ┘(gateway) codex Responses ─> LiteLLM gw ─> openrouter wire   <-- REMOVING
openai_compatible      pi (flagged)         pi ─> relay (direct)             <-- generalizing
```

Key current code:

- `backend/hosted/agent_runtime_cutover.py`
  - `_CLAUDE_PROVIDERS = {"anthropic", "deepseek"}`
  - `_CODEX_PROVIDERS = {"openai", "gemini", "openrouter", "openai_compatible"}`
  - `_PI_PROVIDERS = {"openai_compatible"}` (+ `pi_driver_enabled()` gate)
  - `driver_for_provider`, `codex_transport` (returns `"gateway"` for non-openai
    codex providers), `assert_hosting_ready` (requires `FEEDLING_LITELLM_ENABLE`
    unless pi-only), heartbeat `require_gateway` / `evaluate_supervisor_heartbeat`.
- `backend/db.py::list_agent_runtime_enabled_users(include_gateway, include_pi)`
  — CASE maps provider→driver; gateway providers discovered only when
  `include_gateway`.
- `backend/agent_runtime/spawners.py` — `_pi_models_json`, `agent_home_files`
  (pi branch), `consumer_env` (injects `FEEDLING_LITELLM_BASE_URL/API_KEY` for
  codex-gateway users), `_CONSUMER_ENV_KEYS`.
- `backend/agent_runtime/supervisor.py` — spawns the LiteLLM proxy child
  (~L1118–1169) when `FEEDLING_LITELLM_ENABLE`; `gateway_model_id()` rewrites the
  model id for gateway users (~L934); `_discover_enabled(include_gateway,
  include_pi)`; `_effective_roster(pi_enabled, …)`; heartbeat `gateway`/`pi`.
- `backend/agent_runtime/litellm_gateway.py` — the proxy config + child launcher.
- Deploy: every `deploy/docker-compose.phala.*.yaml` +
  `docker-compose.agent-runner.yaml` carry `FEEDLING_LITELLM_*`;
  `deploy/Dockerfile.agent-runner` installs a LiteLLM venv; `ci.yml` injects
  `FEEDLING_LITELLM_ENABLE` / `FEEDLING_LITELLM_API_KEY` into every deploy job.

## 5. Target architecture

```
provider ──derive──> driver ──> reaches model via
anthropic/deepseek     claude    Anthropic wire (direct)
openai                 codex     OpenAI Responses (direct)
gemini                 pi        pi google-generative-ai adapter (direct)
openrouter             pi        pi openai-completions + openrouter base (direct)
openai_compatible      pi        pi openai-completions + relay base (direct)
```

No gateway. No LiteLLM. `codex_transport` only ever returns `"native"` (openai)
or `""`.

## 6. Detailed changes by component

### 6.1 Driver derivation — `backend/hosted/agent_runtime_cutover.py`
- `_CODEX_PROVIDERS = {"openai"}` (drop gemini/openrouter/openai_compatible).
- `_PI_PROVIDERS = {"openai_compatible", "gemini", "openrouter"}`.
- Delete `pi_driver_enabled()` and its `FEEDLING_PI_DRIVER_ENABLE` reads; make
  the pi branch in `driver_for_provider` unconditional.
- `codex_transport`: openai → `"native"`; everything else → `""`. Remove the
  `"gateway"` return path.
- `assert_hosting_ready`: remove the `FEEDLING_LITELLM_ENABLE` requirement
  entirely (no provider needs the gateway now).
- Heartbeat gate: remove `require_gateway` plumbing and the `gateway` capability
  check from `evaluate_supervisor_heartbeat` / `evaluate_supervisor_instances` /
  `check_supervisor_live`. Keep `require_pi` (still meaningful: a runner must be
  running the pi driver for these users). Callers in
  `backend/hosted/chat_send_core.py` drop `_require_gateway`.

### 6.2 Discovery — `backend/db.py`
- `list_agent_runtime_enabled_users`: fold gemini/openrouter into the
  always-discovered set mapping to the `pi` driver; drop `include_gateway`.
  Collapse `include_pi` (now that pi is unconditional the parameter is dead —
  all of anthropic/deepseek/openai/gemini/openrouter/openai_compatible are
  discovered, deriving to claude/codex/pi). Keep the `oc_driver` CASE simple:
  gemini/openrouter/openai_compatible → `pi`.

### 6.3 pi provider config — `backend/agent_runtime/spawners.py`
- Generalize `_pi_models_json(*, base_url, model, provider)` to emit the right
  `api` + base per provider:
  - `openai_compatible` → `api: "openai-completions"`, `baseUrl` = user relay
    (current behavior), `input: ["text","image"]`, conservative `compat`.
  - `openrouter` → `api: "openai-completions"`, `baseUrl:
    "https://openrouter.ai/api/v1"`, optional `headers` (`HTTP-Referer` /
    `X-Title`), `input: ["text","image"]`.
  - `gemini` → `api: "google-generative-ai"`, Google endpoint base, `apiKey:
    "$PI_PROVIDER_API_KEY"`, `input: ["text","image"]`. `compat` is
    openai-completions-specific, so it is omitted for gemini (pi's adapter owns
    the wire).
- `consumer_env`: delete the codex-gateway branch that injects
  `FEEDLING_LITELLM_BASE_URL` / `FEEDLING_LITELLM_API_KEY`; drop those keys from
  `_CONSUMER_ENV_KEYS`. Every pi user (all three providers) gets
  `PI_PROVIDER_API_KEY = entry["provider_key"]` (already the pi mechanism).
- `stale_home_files`: unchanged in shape (still prunes the pi `models.json` when
  a home flips off pi), but pi now covers more providers.

### 6.4 Supervisor — `backend/agent_runtime/supervisor.py`
- Delete the LiteLLM proxy child spawn block (~L1118–1169) and the
  `from agent_runtime import … litellm_gateway` import.
- Delete `gateway_model_id()` usage (~L934): gemini/openrouter model ids now pass
  through unchanged to pi's `models.json` (no gateway-rewrite).
- `_discover_enabled` / `_effective_roster`: drop `include_gateway` / `pi_enabled`
  parameters and the `FEEDLING_PI_DRIVER_ENABLE` / `FEEDLING_LITELLM_ENABLE` env
  reads. Discovery is unconditional.
- Heartbeat payloads (`_supervisor_heartbeat_payload`,
  `_supervisor_instance_payload`, `_heartbeat_loop`): drop the `gateway` field
  (or hard-code `False`/omit). Keep `pi` (already surfaced correctly after the
  2026-07-07 `list_supervisor_instance_heartbeats` fix).

### 6.5 Delete `backend/agent_runtime/litellm_gateway.py`
Remove the module and its tests.

### 6.6 Deploy
- Remove `FEEDLING_LITELLM_*` env from every compose:
  `docker-compose.phala.{prod,test,pre,''}.{yaml,runner.yaml}`,
  `docker-compose.agent-runner.yaml`, `docker-compose.ci.yml`,
  `docker-compose.memory-sandbox.yaml`.
- Remove `FEEDLING_PI_DRIVER_ENABLE` wiring added on the pre branch
  (compose + `deploy-pre-cvm` / `deploy-pre-runner-cvm` CI) — it's retired.
- `deploy/Dockerfile.agent-runner`: drop the LiteLLM venv install (frees the
  large `litellm` install; per memory it was the runner's only heavyweight
  process at ~250 MB×N).
- `ci.yml`: remove `FEEDLING_LITELLM_ENABLE` / `FEEDLING_LITELLM_API_KEY` env +
  `-e` passthroughs from ALL deploy jobs (test/pre/prod, backend + runner).
- Repo variables/secrets `*_FEEDLING_LITELLM_*` and
  `TEST_FEEDLING_PI_DRIVER_ENABLE` become unused (leave or clean up separately).

### 6.7 Docs
Update `backend/agent_runtime/README.md` driver table and `docs/CHANGELOG.md`.

## 7. Migration / rollout

1. Land all code + deploy changes on a feature branch; full local test suite
   green.
2. Deploy to **pre**. Existing gemini/openrouter users respawn as pi (driver
   change → one respawn, session reset — acceptable). Validate end-to-end per
   provider (see §8).
3. Confirm `pre` runner: no LiteLLM child process, no `FEEDLING_LITELLM_*` in
   container env, heartbeat has no `gateway` requirement, gemini/openrouter turns
   succeed with vision + tools.
4. **Big-bang prod** deploy once pre is proven. Tiny prod user base makes the
   cutover low-blast-radius. No dual-run / no flag.

Live-user impact: a gemini/openrouter user mid-session gets one consumer respawn
(driver change). No data migration — model_api config is unchanged; only the
runner-side driver/`models.json` differs.

## 8. Testing strategy

Unit (must pass before any deploy):
- `driver_for_provider` / `codex_transport` for all 6 providers (gemini/openrouter
  → pi, openai → codex-native, no `"gateway"` anywhere).
- `list_agent_runtime_enabled_users` discovers gemini/openrouter as pi.
- `_pi_models_json` emits correct `api`/`baseUrl`/`input`/`headers` per provider
  (openrouter openai-completions, gemini google-generative-ai).
- `consumer_env` no longer injects `FEEDLING_LITELLM_*`; `PI_PROVIDER_API_KEY`
  set for all pi providers.
- Heartbeat gate: `require_gateway` removed; `require_pi` still gates.
- Guard test: no `litellm` import anywhere (mirror the existing
  `test_no_flask_anywhere` pattern).

End-to-end on **pre** (real relays/keys), per provider:
- **openrouter**: text turn, image/vision turn, tool-call turn, reasoning/thinking
  folded to disclosure, usage/cost surfaced (or gracefully 0).
- **gemini**: same matrix over the `google-generative-ai` wire — this is the
  highest-risk path; verify auth, vision, tools, thinking.
- Regression: openai (codex-native), anthropic/deepseek (claude),
  openai_compatible relay (pi) all still work.

## 9. Risks & mitigations

- **Gemini wire is the critical path.** pi abstracts auth, but Google's
  generative-ai API differs in model-id conventions, safety settings, tool schema,
  and thinking. *Mitigation:* dedicate the first pre-validation pass to gemini;
  keep a git revert ready; do not prod-cut until gemini's full matrix passes.
- **OpenRouter headers.** Some models/accounts want `HTTP-Referer`/`X-Title`.
  *Mitigation:* set them in the provider `headers`; verify a couple of OpenRouter
  models.
- **Model capability declaration.** pi omits images unless `input` includes
  `"image"` (root cause of the 2026-07-07 vision bug). *Mitigation:* every
  provider's `models.json` model entry declares `input: ["text","image"]`.
- **Usage/cost variance.** Some providers/relays don't return usage → cost 0.
  Non-blocking; already handled.
- **Big-bang prod.** *Mitigation:* pre is a full mirror; validate exhaustively;
  tiny prod blast radius; revert = redeploy previous image.

## 10. Out of scope

- Moving `openai` off codex-native to pi (codex stays for openai).
- Retiring the codex driver.
- Any change to claude-driver providers (anthropic/deepseek).
- pi image-generation (`openrouter-images`) or other pi adapters
  (bedrock/mistral/vertex/azure) — not currently hosted.

## 11. Open questions

- Gemini base URL / default model-id convention pi expects for
  `google-generative-ai` — confirm during the gemini pre spike.
- Whether any current prod user is on gemini vs openrouter (informs which path to
  validate hardest) — check prod `user_blobs` before cutover.
