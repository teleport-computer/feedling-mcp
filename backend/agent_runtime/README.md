# agent_runtime — hosted multi-tenant supervisor for the resident consumer

Implements the agent-runner from `docs/AGENT_RUNTIME_CC_CODEX_PLAN.zh.md`.
Retirement of the legacy hosted model_api line is tracked in
`docs/HOSTED_MODEL_API_RETIREMENT_ROADMAP.zh.md`.

## What this is

The **canonical consumer is the VPS resident consumer**
(`tools/chat_resident_consumer.py`). It already does poll / enclave-decrypt /
reply / output cleaning / verify-ping / proactive / images. This package is the
**multi-tenant hosting layer** the resident consumer lacks: it runs one resident
consumer per API-key user inside the CVM, driven in `cli` mode against
`claude` / `codex exec`.

So API-key users become "hosted resident users": their own agent (own session /
home / provider key), same backend tools/encryption/protocol as VPS users — the
only difference is the consumer runs in our CVM instead of on the user's machine.

```
agent-runner (CVM)
  supervisor (this package)
    per active user → tools/chat_resident_consumer.py
      AGENT_MODE=cli, AGENT_CLI_CMD="claude -p {message}" / "codex exec --skip-git-repo-check --json {message}"
      per-user home / checkpoint / session / provider key (enclave-decrypted)
```

## Layout

```
agent_runtime/
  supervisor.py    multi-tenant supervisor: lease + spawn/heartbeat/reap; resolves
                   roster (whoami + JIT provider-key decrypt); spawns resident consumer
  spawners.py      isolation seam: process (default) | container (opt-in);
                   builds the resident-consumer env; ProcessSpawner reaps via poll()
  leases.py        DB-backed lease (acquire/renew/release/takeover) over
                   agent_runtime_instances (migration 0005)
  tokens.py        short-lived user-scoped runtime tokens (mint/verify) — kept for
                   the future backend runtime-token auth path (not used yet)
```

The P3 hosted cutover lives in `backend/hosted/agent_runtime_cutover.py`
(routes a flagged model_api user to the runtime instead of the inline LLM call).

## Run the supervisor

```bash
export DATABASE_URL=postgresql://.../feedling?sslmode=require
export FEEDLING_API_URL=http://localhost:5001
export FEEDLING_ENCLAVE_URL=https://localhost:5003
# roster: each entry an api_key + a provider key (plaintext, or a
# provider_key_envelope decrypted JIT via the enclave) + optional driver/cli_cmd
export AGENT_RUNTIME_USERS='[{"api_key":"<u1 key>","provider_key":"<anthropic>"},
                             {"api_key":"<u2 key>","driver":"codex","provider_key":"<openai>"}]'
python backend/agent_runtime/supervisor.py
```

**Acceptance:** two roster users each get their own resident-consumer process,
runtime home (`/agent-data/users/<uid>`), and lease row; both chat concurrently
without sharing session/home/logs. A second supervisor can't steal a live lease;
a crashed/exited consumer's lease expires and is taken over (and `poll()` reaps
the child so it isn't mistaken for alive).

## Provider / driver routing

| Provider | Driver | Agent | Transport |
|---|---|---|---|
| `anthropic` | `claude` | claude CLI native | Anthropic Messages wire (V1) |
| `deepseek` | `claude` | claude CLI | Anthropic Messages wire @ `ANTHROPIC_BASE_URL={base_url}/anthropic` |
| `openai` | `codex` | codex exec native | OpenAI Responses wire |
| `gemini` | `pi` | pi native relay | `google-generative-ai` @ OpenAI-compatible shim |
| `openrouter` | `pi` | pi native relay | `openai-completions` @ `https://openrouter.ai/api/v1` |
| `openai_compatible` | `pi` | pi native relay | `openai-completions` @ user's `base_url` |

**Reasoning effort:** forwarded natively by the pi relay (no gateway intermediary). Provider support:
- `anthropic` (claude): native
- `openai` (codex): native Responses wire
- `gemini`, `openrouter`, `openai_compatible` (pi): the model entry sets `reasoning: true` (pi's thinking switch — verified against pi-ai 0.80.3 `pi --list-models`; the earlier `reasoningEffort` field was ignored) and the resident passes the level via `--thinking <level>`. openrouter pins `compat.thinkingFormat="openrouter"`. Gated on the route's `reasoning_effort`; null stays off (`_PI_REASONING_DEFAULT`).
- `deepseek` (claude): thinking streamed via the thinking-claude command (`_claude_cli_should_stream_thinking`).

## Tests

```bash
python -m pytest tests/test_agent_runtime_*.py tests/test_hosted_agent_runtime_cutover.py -q
```
- Pure-unit (no Postgres): `tokens`, `spawners`, `hosted_agent_runtime_cutover`.
- DB-backed (need the throwaway test Postgres): `leases`, `supervisor` tick.

## Not yet (see the retirement roadmap)

- **Stage A0** (blocks onboarding): the resident consumer already handles
  verify-ping + output cleaning — so hosting it closes those parity gaps "for
  free"; the work is wiring claude/codex as its cli agent + Feedling tools.
  *Contract verified (2026-06-25):* the env `consumer_env` sets matches what the
  resident reads, and the default claude command renders to
  `claude … --output-format json … -p <msg>` with `--resume <sid>` on later
  turns (`tests/test_agent_runtime_resident_contract.py`). Gap: codex has no cli
  session-resume branch yet; remaining A0 needs a live backend+enclave+provider
  run for the onboarding green-light.
- **Stage A1 — Feedling tools (skill + Bash, skeleton done 2026-06-25; expanded
  for A-full P2-1):** the hosted agent pulls perception, memory readside, and
  screen context via `tools/io_cli.py`, NOT the OpenClaw-only `feedling-io-tools`
  plugin (see docs/AGENT_CLI_INTEGRATION_SURVEY.md). The default claude command
  pre-grants the io_cli context verbs
  (`--allowed-tools`) and appends the how-to
  (`agent_tools_prompt.md` → `--append-system-prompt-file`); `spawners`
  `agent_home_files()` seeds that prompt + a claude `settings.json` allow-list
  per user. Pending the A0 live run: whether `claude -p` actually invokes it
  unattended + the Bash allow-rule match; codex AGENTS.md/sandbox path unverified.
- **Stage B (done 2026-06-25):** `POST /v1/model_api/driver` (flip
  legacy|claude|codex) + `GET /v1/model_api/key_envelope` (own provider-key
  ciphertext); supervisor `_resolve_roster` self-fetches the envelope so a roster
  need only carry api_keys.
- **Stage C (done 2026-06-25):** `db.list_agent_runtime_enabled_users()` +
  supervisor `_discover_enabled`/`_apply_discovery` (behind
  `AGENT_RUNTIME_AUTODISCOVER`) filter the roster to backend-enabled users, driver
  from the flag. Constraint: api_keys are stored hashed, so the supervisor can't
  recover credentials from the DB — full no-roster discovery needs Stage D.
- **Stage D — runtime-token auth (slices 1–3 done 2026-06-25):** end-to-end when
  `FEEDLING_RUNTIME_TOKEN_SECRET` is set (OFF → zero change). Primitive in
  `core/runtime_token.py` (`agent_runtime/tokens.py` is a shim). (1) `accounts/`
  `require_user()` accepts `X-Feedling-Runtime-Token` (present-but-invalid fails
  closed). (2) `enclave_app.py` forwards the token to whoami for `/v1/envelope/
  decrypt` + the cached decrypt-and-serve routes. (3) the supervisor mints a
  per-user token (`token_writer`) and refreshes `{home}/runtime-token` each tick;
  the resident consumer reads it and switches `_HEADERS` to the token. (4)
  `runtime_auth.authorize_scope` enforces the token's scope on
  `/v1/memory/actions` + `/v1/identity/actions` (api-key auth = full access).
  Bug fixes (2026-06-25): enclave `_whoami_cache` prunes expired entries (no leak
  under token rotation); the consumer decodes the token `exp` and falls back to
  the api key when stale (no wedge if the supervisor stops refreshing). Remaining:
  forward the token (not the api key) on `core.enclave` decrypt so token-auth
  memory writes work; widen scope enforcement + narrow minted scopes; drop
  `FEEDLING_API_KEY` from the consumer env entirely.
- **Ops:** on-demand spawn / idle exit; finishing the container isolation
  strategy (`docs/AGENT_RUNTIME_ISOLATION.md`).
- Stage F: delete the legacy hosted model_api inline path.
