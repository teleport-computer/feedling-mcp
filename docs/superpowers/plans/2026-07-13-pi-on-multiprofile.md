# Pi driver on multi-profile schema — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Steps use `- [ ]` checkboxes. Spec: `docs/superpowers/specs/2026-07-13-pi-on-multiprofile-schema-design.md`.

**Goal:** Re-implement the pi driver consolidation (retire the in-CVM LiteLLM gateway; route gemini/openrouter/openai_compatible/deepseek through a native `pi` driver, preserving reasoning forwarding) on top of `test`'s `model_api_routes`/`model_api_credentials` schema.

**Architecture:** `test` already returns the pi-consumable discovery dict shape from the new route-JOIN SQL, so the schema split is contained in ONE function; everything else transplants from the `pi-driver` branch (the complete reference implementation) with three deltas: (a) new-schema discovery, (b) native reasoning forwarding instead of the gateway's, (c) NO `thinking_fallback` (test reverted it).

**Tech Stack:** Python 3.11, FastAPI, psycopg3, Alembic, pytest. External `pi` CLI + `claude`/`codex` CLIs in the runner image.

## Global Constraints
- **Reference implementation:** `pi-driver` branch. Read verbatim with `git show pi-driver:<path>`. Adapt per each task; do NOT reintroduce `thinking_fallback` (test reverted it) or the JSONB `user_blobs(kind='model_api')` reads (test uses routes/credentials).
- **Final driver map:** anthropic→claude; openai→codex(native); gemini/deepseek/openrouter/openai_compatible→pi. deepseek via pi `anthropic-messages` @ `<base>/anthropic` (text-only).
- **NO gateway anywhere** after this. Reasoning is forwarded NATIVELY by pi (§4.3/§7.1 of the spec), never via a proxy.
- **Two load-bearing sync points that must agree:** the SQL `CASE` in `db.list_agent_runtime_enabled_users` and `agent_runtime_cutover` provider→driver sets. Their cross-reference comments must stay accurate.
- **DO NOT `git commit`/`git add`/`git stash`.** Leave changes unstaged; the human commits. Skip any "commit" step.
- **Workspace:** worktree `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/pi-multiprofile` (branch `feat/pi-on-multiprofile`, off `origin/test` b6073c6).
- **PG for tests:** `export DATABASE_URL="postgresql://postgres:test@localhost:55432/postgres"` + `docker start feedling-test-pg`. New-schema tests seed via `db.model_api_credential_create` + `db.model_api_route_upsert`/`_activate`/`_mark_test` (see existing `tests/test_agent_runtime_discovery.py::_seed_model_api`).

---

### Task 1: Driver derivation + hosting-ready + send-gate (cutover)

**Files:**
- Modify: `backend/hosted/agent_runtime_cutover.py`
- Modify: `backend/hosted/chat_send_core.py`
- Test: `tests/test_hosted_agent_runtime_cutover.py`

**Interfaces — Produces:** `driver_for_provider(p)` → `"pi"` for {openai_compatible,gemini,openrouter,deepseek}, `"claude"` for anthropic, `"codex"` for openai. `check_supervisor_live(*, require_pi=False, require_gateway removed)`. Heartbeat evaluation reads a `pi` capability bit.

- [ ] **Step 1: Failing tests.** In `test_hosted_agent_runtime_cutover.py`:
  ```python
  def test_driver_map_pi():
      import backend.hosted.agent_runtime_cutover as c
      assert c.driver_for_provider("openai_compatible") == "pi"
      assert c.driver_for_provider("gemini") == "pi"
      assert c.driver_for_provider("openrouter") == "pi"
      assert c.driver_for_provider("deepseek") == "pi"
      assert c.driver_for_provider("anthropic") == "claude"
      assert c.driver_for_provider("openai") == "codex"
  def test_hosting_ready_no_litellm(monkeypatch):
      import backend.hosted.agent_runtime_cutover as c
      monkeypatch.delenv("FEEDLING_LITELLM_ENABLE", raising=False)
      monkeypatch.setenv("FEEDLING_HOST_ALL", "1")
      monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "s")
      c.assert_hosting_ready()   # must not raise
  ```
  Re-point / delete any existing assertion that maps gemini/openrouter/openai_compatible→codex or requires FEEDLING_LITELLM_ENABLE, and any `require_gateway` heartbeat test → `require_pi`.
- [ ] **Step 2: Run → FAIL.** `python -m pytest tests/test_hosted_agent_runtime_cutover.py -q`
- [ ] **Step 3: Implement.** Transplant from `git show pi-driver:backend/hosted/agent_runtime_cutover.py` (lines ~40-66 for the provider sets + `driver_for_provider`; ~90 `assert_hosting_ready`; ~126-235 heartbeat `require_pi`/`check_supervisor_live`). Set:
  - `_CLAUDE_PROVIDERS = {"anthropic"}`, `_CODEX_PROVIDERS = {"openai"}`, `_PI_PROVIDERS = {"openai_compatible","gemini","openrouter","deepseek"}`.
  - `driver_for_provider`: claude/pi/codex branches, else `legacy`.
  - `codex_transport`: only `"native"`/`""`.
  - `assert_hosting_ready`: require only `FEEDLING_HOST_ALL` + `FEEDLING_RUNTIME_TOKEN_SECRET` (drop litellm).
  - `evaluate_supervisor_heartbeat`/`evaluate_supervisor_instances`/`check_supervisor_live`: replace `require_gateway` with `require_pi`, reading `hb["pi"]`.
  - Keep the "sync with db.list_agent_runtime_enabled_users CASE" comment.
  In `chat_send_core.py` (~line 116, transplant from pi-driver): `_require_pi = agent_runtime_cutover.driver_for_provider(_provider) == "pi"; live, reason = agent_runtime_cutover.check_supervisor_live(require_pi=_require_pi)`.
- [ ] **Step 4: Run → PASS.** `python -m pytest tests/test_hosted_agent_runtime_cutover.py -q`
- [ ] **Step 5: COMMIT — SKIP.**

---

### Task 2: Discovery on the route/credential schema (db.py)

**Files:**
- Modify: `backend/db.py::list_agent_runtime_enabled_users`
- Test: `tests/test_agent_runtime_discovery.py`

**Interfaces — Produces:** `list_agent_runtime_enabled_users()` (NO params) → rows `{user_id, driver, provider, model, base_url, supports_responses, reasoning_effort}`, driver derived: anthropic/claude→claude, openai→codex, ELSE→pi. Discovery unconditional (all fit providers with an active test_ok route).

- [ ] **Step 1: Failing tests.** Extend `test_agent_runtime_discovery.py`. Its `_seed_model_api` already seeds the NEW schema (credential + active route). Add:
  ```python
  def test_discovery_pi_providers(_clean_blobs):
      for p in ("gemini", "openrouter", "openai_compatible", "deepseek"):
          _seed_model_api(f"{p}_u", provider=p, test_status="ok",
                          base_url="https://relay/v1" if p == "openai_compatible" else "")
      rows = {r["user_id"]: r for r in db.list_agent_runtime_enabled_users()}
      for p in ("gemini", "openrouter", "openai_compatible", "deepseek"):
          assert rows[f"{p}_u"]["driver"] == "pi"
  def test_discovery_unconditional_no_include_gateway(_clean_blobs):
      # signature no longer takes include_gateway; all fit providers discovered
      import inspect
      assert "include_gateway" not in inspect.signature(db.list_agent_runtime_enabled_users).parameters
  ```
  Re-point existing assertions expecting gemini/openrouter/openai_compatible→codex or gated by `include_gateway`.
- [ ] **Step 2: Run → FAIL** (needs PG). `python -m pytest tests/test_agent_runtime_discovery.py -q`
- [ ] **Step 3: Implement.** In `list_agent_runtime_enabled_users`: drop the `include_gateway` param; set `providers = ["anthropic","claude","deepseek","openai","gemini","openrouter","openai_compatible"]` (full fit set, no gating). Keep test's `model_api_routes r JOIN model_api_credentials c` body, `WHERE r.is_active AND r.test_status='ok' AND LOWER(c.provider)=ANY(%s)`, and the returned keys. Change the CASE to:
  ```sql
  CASE LOWER(c.provider)
    WHEN 'anthropic' THEN 'claude'
    WHEN 'claude'    THEN 'claude'
    WHEN 'openai'    THEN 'codex'
    ELSE 'pi'
  END AS driver
  ```
  Update the docstring's driver-map + Returns line. Grep callers of `list_agent_runtime_enabled_users(include_gateway=...)` and drop the arg (supervisor `_discover_enabled` is Task 5; any other caller fix here).
- [ ] **Step 4: Run → PASS.** `python -m pytest tests/test_agent_runtime_discovery.py -q`
- [ ] **Step 5: COMMIT — SKIP.**

---

### Task 3: pi models.json + native reasoning forwarding (spawners, pure)

**Files:**
- Modify: `backend/agent_runtime/spawners.py` (`_pi_models_json`, `_PI_PROVIDER_ID`, `_PI_OPENROUTER_BASE`, `_claude_anthropic_base_url`)
- Test: `tests/test_agent_runtime_spawners.py`

**Interfaces — Produces:** `_pi_models_json(*, base_url, model, provider, reasoning_effort)` → provider dict per §4.3. `_claude_anthropic_base_url(entry)` → `""` always.

- [ ] **Step 1: Failing tests.** Reuse the file's `_prov` helper pattern. Add (adapt from `git show pi-driver:tests/test_agent_runtime_spawners.py` deepseek/gemini/openrouter tests, PLUS the new reasoning assertions):
  ```python
  def test_pi_models_gemini():
      p = _prov("gemini", model="gemini-2.0-flash", base_url="")
      assert p["api"] == "google-generative-ai" and "compat" not in p
      assert p["models"][0]["input"] == ["text", "image"]
  def test_pi_models_openrouter_headers_and_base():
      p = _prov("openrouter", model="x", base_url="")
      assert p["api"] == "openai-completions"
      assert p["baseUrl"] == "https://openrouter.ai/api/v1"
      assert p["headers"]["HTTP-Referer"] and p["headers"]["X-Title"]
  def test_pi_models_openai_compatible_uses_user_base():
      p = _prov("openai_compatible", model="qwen", base_url="https://my/v1/")
      assert p["api"] == "openai-completions" and p["baseUrl"] == "https://my/v1"
  def test_pi_models_deepseek_anthropic_messages_text_only():
      p = _prov("deepseek", model="deepseek-reasoner", base_url="")
      assert p["api"] == "anthropic-messages"
      assert p["baseUrl"] == "https://api.deepseek.com/anthropic"
      assert "compat" not in p and p["models"][0]["input"] == ["text"]
  # NATIVE REASONING (no gateway):
  def test_pi_models_openrouter_forwards_reasoning_effort():
      p = _prov("openrouter", model="x", base_url="", reasoning_effort="high")
      assert p["compat"]["supportsReasoningEffort"] is True
      # effort injected into pi's per-model reasoning config (field per pre-spike)
      assert _model_reasoning_effort(p) == "high"   # helper reads p["models"][0][<pi reasoning field>]
  def test_pi_models_openrouter_off_omits_reasoning():
      p = _prov("openrouter", model="x", base_url="", reasoning_effort="off")
      assert p["compat"]["supportsReasoningEffort"] is False
      assert _model_reasoning_effort(p) == ""
  ```
  Add a `_model_reasoning_effort(p)` test helper that reads whatever field the implementation uses (keep test + impl consistent).
  Also: `test_claude_anthropic_base_url_empty_for_all` → `_claude_anthropic_base_url({"provider":"deepseek","base_url":"https://api.deepseek.com"}) == ""`.
- [ ] **Step 2: Run → FAIL.** `python -m pytest tests/test_agent_runtime_spawners.py -q -k "pi_models or anthropic_base_url"`
- [ ] **Step 3: Implement.** Transplant `_pi_models_json` from `git show pi-driver:backend/agent_runtime/spawners.py:181-270` (gemini/openrouter/deepseek/openai_compatible branches). ADD a `reasoning_effort` kwarg and native reasoning wiring per spec §4.3:
  - Normalize effort: `e = (reasoning_effort or "").strip().lower()`; `on = e and e not in {"off","none",""}`.
  - For openrouter (and openai_compatible only when `on`): `compat["supportsReasoningEffort"] = on`; when `on`, add the effort to the model entry's pi reasoning config field (the exact field name is fixed by the §7.1 pre-spike — use a single module constant `_PI_MODEL_REASONING_KEY` so test + impl share it; default to `"reasoningEffort"` pending the spike, and normalize non-enum efforts to `"medium"` like the gateway did).
  - gemini branch: forward the effort into gemini's native thinking config when `on` (verify field in spike; guard behind the same constant/helper).
  - deepseek: text-only, no reasoning param (leave conservative).
  Simplify `_claude_anthropic_base_url` to always `return ""` (deepseek moved to pi); delete `_CLAUDE_COMPAT_BASE_URLS` if present and grep its callers.
- [ ] **Step 4: Run → PASS.** `python -m pytest tests/test_agent_runtime_spawners.py -q -k "pi_models or anthropic_base_url"`
- [ ] **Step 5: COMMIT — SKIP.**

---

### Task 4: pi spawn wiring — cli-cmd, home, consumer_env; delete codex gateway config (spawners)

**Files:**
- Modify: `backend/agent_runtime/spawners.py` (`_default_cli_cmd`, `agent_home_files`, `materialize_home`, `stale_home_files`, `consumer_env`, delete `_codex_gateway_config`/`_GATEWAY_PROVIDER_ID`/gateway transport & env passthrough)
- Test: `tests/test_agent_runtime_spawners.py`

**Interfaces — Consumes:** Task 3 `_pi_models_json(..., reasoning_effort=...)`. **Produces:** pi driver spawns a `pi --mode json -t bash …` consumer whose home carries `pi-home/agent/models.json`; `consumer_env` sets `PI_PROVIDER_API_KEY`; codex never writes `config.toml`.

- [ ] **Step 1: Failing tests.** Add (adapt from pi-driver's spawner tests):
  ```python
  def test_pi_default_cli_cmd():
      cmd = spawners._default_cli_cmd("pi", "/h", model="x")
      assert "pi --mode json" in cmd and "-t bash" in cmd
      assert "--model feedling/x" in cmd and "--session-id {session_id}" in cmd
      assert "{message}" not in cmd   # pi reads message from STDIN
  def test_pi_home_writes_models_json():
      entry = {"driver": "pi", "provider": "openrouter", "model": "x", "reasoning_effort": "high"}
      files = spawners.agent_home_files("/h", driver="pi", provider="openrouter",
                                        model="x", reasoning_effort="high")
      assert "/h/pi-home/agent/models.json" in files
  def test_pi_consumer_env_sets_provider_key():
      env = spawners.consumer_env({}, {"provider_key": "sk-or", "driver": "pi",
                                       "provider": "openrouter", "model": "x"},
                                  user_id="u", home="/h")
      assert env["PI_PROVIDER_API_KEY"] == "sk-or"
      assert "FEEDLING_LITELLM_BASE_URL" not in env
  def test_codex_never_writes_config_toml():
      files = spawners.agent_home_files("/h", driver="codex", provider="openai")
      assert "/h/codex-home/config.toml" not in files
  ```
  Delete existing gateway-config tests (`*codex_gateway*`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Transplant the pi branches from `git show pi-driver:backend/agent_runtime/spawners.py` — `_default_cli_cmd` pi branch (~L328), `agent_home_files`/`materialize_home`/`stale_home_files` pi branches writing `pi-home/agent/models.json` via `_pi_models_json(..., reasoning_effort=entry.reasoning_effort)`, `consumer_env` pi branch (~L627) setting `PI_PROVIDER_API_KEY`. DELETE `_codex_gateway_config`, `_GATEWAY_PROVIDER_ID`, the codex gateway transport branch, and remove `FEEDLING_LITELLM_*` from `_CONSUMER_ENV_KEYS`. Thread `reasoning_effort` from the discovered entry into the pi models.json call.
- [ ] **Step 4: Run → PASS.** `python -m pytest tests/test_agent_runtime_spawners.py -q`
- [ ] **Step 5: COMMIT — SKIP.**

---

### Task 5: Supervisor — drop gateway, wire pi discovery + heartbeat

**Files:**
- Modify: `backend/agent_runtime/supervisor.py`
- Test: `tests/test_agent_runtime_supervisor.py`

**Interfaces — Consumes:** Task 2 `list_agent_runtime_enabled_users()`. **Produces:** `_discover_enabled()` (no params) projecting `{driver,provider,model,base_url,supports_responses,reasoning_effort}`; `_effective_roster(base, *, autodiscover, host_all_discovered=None)` → `list` (no gateways tuple); heartbeat payload carries `pi:True` not `gateway`.

- [ ] **Step 1: Failing tests.** Adapt from `git show pi-driver:tests/test_agent_runtime_supervisor.py`. Assert: `_discover_enabled()` takes no `include_gateway`; a pi-driver roster row survives `_apply_discovery`; heartbeat payload has `pi` capability; delete all `_gateway_entries`/`_drop_gateway_users`/`_effective_roster(...gateway_enabled=...)` tests.
- [ ] **Step 2: Run → FAIL** (needs PG).
- [ ] **Step 3: Implement.** Transplant supervisor pi changes from `pi-driver` (do NOT carry `thinking_fallback`): `_discover_enabled()` no-params projecting the 6 keys (no thinking_fallback); delete `_gateway_entries`, `_owned_gateway_entries`, `_drop_gateway_users`, `_wire_gateway_models`, `_reconcile_gateway`, the `litellm_gateway` import, `gateway_enabled`, and the gateway child spawn in `main()`; `_effective_roster` returns just `roster`; `pi_enabled=True`; `_supervisor_heartbeat_payload`/`_supervisor_instance_payload` carry `pi` not `gateway`; `_spawn_identity` tuple keeps provider/model/base_url and drops the gateway-key special case (keep `provider_key`).
- [ ] **Step 4: Run → PASS.** `python -m pytest tests/test_agent_runtime_supervisor.py tests/test_agent_runtime_discovery.py -q`
- [ ] **Step 5: COMMIT — SKIP.**

---

### Task 6: Resident consumer — pi stream parsing

**Files:**
- Modify: `tools/chat_resident_consumer.py`
- Test: `tests/test_chat_resident_consumer.py`

**Interfaces — Produces:** `_pi_turn_from_stream(raw)→(reply,thinking)`, `_pi_turn_metrics(raw)→dict`, `_is_pi_cmd(cmd)`, pi branch in `_cli_error_detail` (message_end stopReason=error), `_attach_provider_reasoning(..., source="pi_thinking")`; `_JSON_NON_FINAL_EVENTS` includes pi intermediate events; `call_agent_cli` pi branch surfaces pi errors (never echoes the user prompt).

- [ ] **Step 1: Failing tests.** Transplant the pi consumer tests from `git show pi-driver:tests/test_chat_resident_consumer.py` (the `_pi_stream_lines`/`_PI_HEADER` helpers + `test_call_agent_cli_pi_*`, `test_cli_failure_surfaces_pi_error_message`, `test_pi_intermediate_events_are_non_final`). IMPORTANT: the current test `_prepare_cli_command` signature takes `lane` — any pi test that monkeypatches it must accept `lane` (use `lambda message, image_paths=None, lane="background": [...]`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Transplant from `git show pi-driver:tools/chat_resident_consumer.py`: `_pi_turn_from_stream` (~L3114), `_pi_turn_metrics` (~L3151), `_is_pi_cmd` (~L3744), the pi branch of `_cli_error_detail` (~L2931 `stopReason=error`+`errorMessage`, folded into the existing codex-priority logic — keep test's `_codex_error_message` priority code), pi entries in `_JSON_NON_FINAL_EVENTS`, and the `call_agent_cli` pi branch that uses `_pi_turn_from_stream` and folds thinking via `_attach_provider_reasoning(pi_reply, pi_thinking, source="pi_thinking", kind="provider_reasoning_summary", native=True)`, raising on empty reply rather than echoing the prompt.
- [ ] **Step 4: Run → PASS.** `python -m pytest tests/test_chat_resident_consumer.py -q`
- [ ] **Step 5: COMMIT — SKIP.**

---

### Task 7: Retire LiteLLM — delete module + scrub env + guard

**Files:**
- Delete: `backend/agent_runtime/litellm_gateway.py`, `tests/test_litellm_gateway.py`
- Modify: `deploy/Dockerfile.agent-runner`, `deploy/docker-compose.*.yaml`, `.github/workflows/ci.yml`, `tools/gen_url_map.py`, `tests/conftest.py`
- Create: `tests/test_no_litellm_anywhere.py`

- [ ] **Step 1: Failing guard.** Transplant `git show pi-driver:tests/test_no_litellm_anywhere.py` (asserts no `import litellm`/`litellm_gateway` identifier in `backend/`/`tools/`, the module file is gone, and no `FEEDLING_LITELLM` string outside the guard). Run → FAIL.
- [ ] **Step 2: Implement.** `git rm` the two files (leave unstaged deletion). Remove every `FEEDLING_LITELLM_*` reference (inventory from spec §4.7 / Explore map §5): `deploy/Dockerfile.agent-runner:49-58` (the litellm venv install + `FEEDLING_LITELLM_PYTHON`), all `deploy/docker-compose.*.yaml`, `.github/workflows/ci.yml` job env, `tools/gen_url_map.py:50`, `tests/conftest.py:29`. Remove the `litellm_gateway` import + any `build_model_entry`/`gateway_model_id` calls left in code or tests (grep first).
- [ ] **Step 3: Run → PASS.** `python -m pytest tests/test_no_litellm_anywhere.py -q`
- [ ] **Step 4: COMMIT — SKIP.**

---

### Task 8: Docs

**Files:** Modify `backend/agent_runtime/README.md`, `deploy/DEPLOYMENTS.md`, `docs/CHANGELOG.md`.

- [ ] **Step 1:** README driver table → anthropic=claude; openai=codex(native); gemini/openrouter/openai_compatible/deepseek=pi (deepseek anthropic-messages @ `/anthropic`, text-only; reasoning forwarded natively).
- [ ] **Step 2:** DEPLOYMENTS.md — replace the gateway three-var prerequisite with the two-var host-all set (`FEEDLING_HOST_ALL`+`FEEDLING_RUNTIME_TOKEN_SECRET`); note the runner image no longer installs the litellm venv.
- [ ] **Step 3:** Prepend a dated `docs/CHANGELOG.md` entry (match the top entry format): pi consolidation re-homed onto the multi-profile schema; gateway retired; native reasoning forwarding; final driver map. Note the §7.1 pi-reasoning pre-spike is the pre-prod gate.
- [ ] **Step 4: COMMIT — SKIP.**

---

### Task 9: Full-suite gate

- [ ] **Step 1:** `docker start feedling-test-pg`; `export DATABASE_URL=…`. Run `python -m pytest -q -p no:randomly tests/ --ignore=tests/test_api.py`.
- [ ] **Step 2:** Expected: green except the KNOWN pre-existing `test` baseline failures (reproduce them on clean `origin/test` first to confirm none are new — notably the two `test_model_api_path.py::test_chat_response_*verify_ping*` mock-signature failures, plus any `test_data_track`/DB-pollution set). Report the EXACT failing-id list so the controller confirms zero new regressions.
- [ ] **Step 3: COMMIT — SKIP.**

---

## Post-plan (human + pre, NOT a subagent task)
- **§7.1 pi reasoning pre-spike on `pre`:** deploy the branch, run a real openrouter turn with `reasoning_effort=high`, confirm a real reasoning chain returns with NO litellm/gateway process. This fixes `_PI_MODEL_REASONING_KEY` if the placeholder field name is wrong. Also validate deepseek `anthropic-messages` auth + gemini/openrouter/openai_compatible turns. Then prod.
