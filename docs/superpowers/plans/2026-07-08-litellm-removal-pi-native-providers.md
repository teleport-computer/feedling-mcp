# LiteLLM Removal — pi-native gemini/openrouter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the in-CVM LiteLLM gateway entirely by routing gemini and openrouter through the pi driver's native provider adapters, leaving `claude`/`codex`(openai-only)/`pi` as the drivers.

**Architecture:** pi already speaks openrouter (OpenAI-compatible) and gemini (google-generative-ai) natively. Move both providers from the `codex+gateway` path to `pi` in driver derivation and discovery; teach `_pi_models_json` to emit per-provider config; delete the LiteLLM proxy child, its module, all `FEEDLING_LITELLM_*` deploy wiring, and the now-pointless `FEEDLING_PI_DRIVER_ENABLE` flag (pi becomes unconditional). Validate on `pre`, then big-bang `prod`.

**Tech Stack:** Python 3.11, FastAPI/ASGI backend, pytest, psycopg (Postgres), pi CLI (`@earendil-works/pi-coding-agent`), Phala TDX CVM deploy via `phala` CLI + GitHub Actions.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-07-07-litellm-removal-pi-native-providers-design.md`. `docs/superpowers/` is LOCAL, never committed.
- Drivers after this change: `claude` = {anthropic, deepseek}; `codex` = {openai} (native only, no gateway); `pi` = {openai_compatible, gemini, openrouter}. `codex_transport` returns only `"native"` (openai) or `""`.
- pi omits images unless the model entry's `input` includes `"image"` — EVERY pi provider's `models.json` model entry MUST declare `"input": ["text", "image"]`.
- pi provider `api` per provider: openrouter → `openai-completions`; gemini → `google-generative-ai`; openai_compatible → `openai-completions`.
- Never run `git commit`/`git add` unless the human explicitly asked; the plan's commit steps are the explicit authorization for THIS work. End commit messages with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Every task ends with a green suite.
- Local test DB: `export DATABASE_URL="postgresql://postgres:test@localhost:55432/postgres"` (throwaway `feedling-test-pg` container, `docker start feedling-test-pg`).
- Do implementation in a dedicated git worktree (superpowers:using-git-worktrees). Do NOT work on `test`/`pre`/`main` directly.

---

## Task 1: Driver derivation + send-gate — move gemini/openrouter to pi, retire the flag, drop the gateway transport

**Files:**
- Modify: `backend/hosted/agent_runtime_cutover.py` (`_CLAUDE_PROVIDERS`/`_CODEX_PROVIDERS`/`_PI_PROVIDERS` ~L40-52, `pi_driver_enabled`, `driver_for_provider`, `codex_transport`, `assert_hosting_ready` ~L115, `evaluate_supervisor_heartbeat`/`evaluate_supervisor_instances`/`check_supervisor_live` require_gateway plumbing)
- Modify: `backend/hosted/chat_send_core.py:~99-102` (drop `_require_gateway`)
- Test: `tests/test_hosted_agent_runtime_cutover.py`

**Interfaces:**
- Produces: `driver_for_provider(provider: str) -> str` — "claude"|"codex"|"pi"|"legacy", pi unconditional for {openai_compatible,gemini,openrouter}. `codex_transport(provider: str) -> str` — "native" for openai, "" otherwise. `check_supervisor_live(*, require_pi: bool = False, now: float | None = None) -> tuple[bool,str]` (no `require_gateway`). `evaluate_supervisor_heartbeat(hb, *, now, max_age, require_pi=False)` (no `require_gateway`). `assert_hosting_ready()` no longer requires `FEEDLING_LITELLM_ENABLE`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_hosted_agent_runtime_cutover.py`:

```python
import backend.hosted.agent_runtime_cutover as cutover

def test_gemini_and_openrouter_derive_to_pi_unconditionally(monkeypatch):
    monkeypatch.delenv("FEEDLING_PI_DRIVER_ENABLE", raising=False)  # flag retired
    assert cutover.driver_for_provider("gemini") == "pi"
    assert cutover.driver_for_provider("openrouter") == "pi"
    assert cutover.driver_for_provider("openai_compatible") == "pi"
    assert cutover.driver_for_provider("openai") == "codex"
    assert cutover.driver_for_provider("anthropic") == "claude"
    assert cutover.driver_for_provider("deepseek") == "claude"

def test_codex_transport_only_native_or_empty():
    assert cutover.codex_transport("openai") == "native"
    for p in ("gemini", "openrouter", "openai_compatible", "anthropic"):
        assert cutover.codex_transport(p) == ""

def test_assert_hosting_ready_no_longer_requires_litellm(monkeypatch):
    monkeypatch.delenv("FEEDLING_LITELLM_ENABLE", raising=False)
    cutover.assert_hosting_ready()  # must not raise

def test_send_gate_has_no_require_gateway_param():
    import inspect
    sig = inspect.signature(cutover.check_supervisor_live)
    assert "require_gateway" not in sig.parameters
    assert "require_pi" in sig.parameters

def test_pi_heartbeat_gate_ignores_gateway_flag():
    # pi user is live on a runner reporting pi:true even with gateway:false
    hb = {"ts": 1_000_000.0, "host_all": True, "gateway": False, "pi": True}
    live, reason = cutover.evaluate_supervisor_heartbeat(
        hb, now=1_000_001.0, max_age=90, require_pi=True)
    assert live is True and reason == ""
```

Delete/replace any existing test asserting `driver_for_provider("gemini") == "codex"`, `codex_transport(...) == "gateway"`, `pi_driver_enabled`, or `require_gateway` behavior in this file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_hosted_agent_runtime_cutover.py -q`
Expected: FAIL (gemini→codex today; `require_gateway` still present).

- [ ] **Step 3: Implement the derivation + gate changes**

In `backend/hosted/agent_runtime_cutover.py`:

```python
_CLAUDE_PROVIDERS = {"anthropic", "deepseek"}
_CODEX_PROVIDERS = {"openai"}
_PI_PROVIDERS = {"openai_compatible", "gemini", "openrouter"}
```

Delete `pi_driver_enabled()` and every `FEEDLING_PI_DRIVER_ENABLE` read. Make the pi branch unconditional:

```python
def driver_for_provider(provider: str) -> str:
    p = provider_client.normalize_provider(provider)
    if p in _CLAUDE_PROVIDERS:
        return "claude"
    if p in _PI_PROVIDERS:
        return "pi"
    if p in _CODEX_PROVIDERS:
        return "codex"
    return "legacy"

def codex_transport(provider: str) -> str:
    """openai → native (direct OpenAI Responses); everything else → "" (not
    codex-driven, no in-CVM gateway remains)."""
    p = provider_client.normalize_provider(provider)
    if driver_for_provider(p) != "codex":
        return ""
    return "native"
```

In `assert_hosting_ready`, delete the block that appends `FEEDLING_LITELLM_ENABLE` to `missing` (no provider needs the gateway now).

Remove `require_gateway` from the three gate functions and their bodies:

```python
def evaluate_supervisor_heartbeat(hb, *, now, max_age, require_pi=False):
    if not isinstance(hb, dict):
        return (False, "no_supervisor_heartbeat")
    try:
        ts = float(hb.get("ts") or 0)
    except (TypeError, ValueError):
        return (False, "bad_supervisor_heartbeat")
    if ts <= 0:
        return (False, "bad_supervisor_heartbeat")
    if now - ts > max_age:
        return (False, f"stale_supervisor_heartbeat_{int(now - ts)}s")
    if not hb.get("host_all"):
        return (False, "supervisor_host_all_inactive")
    if require_pi and not hb.get("pi"):
        return (False, "supervisor_pi_disabled")
    return (True, "")
```

Apply the same `require_gateway` removal to `evaluate_supervisor_instances` and `check_supervisor_live` (drop the param + the `require_gateway` argument passed through).

In `backend/hosted/chat_send_core.py`, delete the `_require_gateway = …` line and pass only `require_pi`:

```python
    _require_pi = agent_runtime_cutover.driver_for_provider(_provider) == "pi"
    live, reason = agent_runtime_cutover.check_supervisor_live(require_pi=_require_pi)
```

Grep for any other `check_supervisor_live(` / `evaluate_supervisor_heartbeat(` / `codex_transport(` callers and update: `grep -rn "check_supervisor_live\|require_gateway\|codex_transport\|pi_driver_enabled" backend/ tools/`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_hosted_agent_runtime_cutover.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/hosted/agent_runtime_cutover.py backend/hosted/chat_send_core.py tests/test_hosted_agent_runtime_cutover.py
git commit -m "$(cat <<'EOF'
refactor(cutover): derive gemini/openrouter to pi, retire flag + gateway transport

gemini/openrouter now derive to the pi driver unconditionally (no more
FEEDLING_PI_DRIVER_ENABLE, no codex+gateway path). codex_transport only
returns native (openai) or ""; the send-gate drops require_gateway and keeps
require_pi. assert_hosting_ready no longer requires FEEDLING_LITELLM_ENABLE.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Discovery — db.py maps gemini/openrouter to pi unconditionally

**Files:**
- Modify: `backend/db.py::list_agent_runtime_enabled_users` (~L1260-1300)
- Modify: `backend/agent_runtime/supervisor.py` callers of discovery (`_discover_enabled`, `_effective_roster`) that pass `include_gateway`/`include_pi`
- Test: `tests/test_agent_runtime_discovery.py`

**Interfaces:**
- Produces: `list_agent_runtime_enabled_users() -> list[dict]` — no params. Discovers anthropic/deepseek/openai/gemini/openrouter/openai_compatible; each row `{"user_id","driver","provider","model","base_url","supports_responses"}`; gemini/openrouter/openai_compatible → driver `"pi"`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test**

In `tests/test_agent_runtime_discovery.py` (uses the local test DB; follow the existing fixture that inserts `user_blobs` rows with `kind='model_api'`):

```python
def test_gemini_openrouter_discovered_as_pi(seed_model_api_user):
    seed_model_api_user("usr_gem", provider="gemini", model="gemini-2.5-flash", test_status="ok")
    seed_model_api_user("usr_or", provider="openrouter", model="x-ai/grok-2", test_status="ok")
    rows = {r["user_id"]: r for r in db.list_agent_runtime_enabled_users()}
    assert rows["usr_gem"]["driver"] == "pi"
    assert rows["usr_or"]["driver"] == "pi"

def test_list_agent_runtime_enabled_users_takes_no_flag_params():
    import inspect
    assert inspect.signature(db.list_agent_runtime_enabled_users).parameters == {}
```

(If no `seed_model_api_user` fixture exists, insert rows with the same raw SQL the existing discovery tests use.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_agent_runtime_discovery.py -q`
Expected: FAIL (gemini/openrouter only returned when `include_gateway=True`; signature still has params).

- [ ] **Step 3: Implement**

In `backend/db.py`, drop the params and discover all providers unconditionally, mapping to pi:

```python
def list_agent_runtime_enabled_users() -> list[dict]:
    """Every user with a fit-able provider + test_status='ok' is hosted. Driver
    derived per provider (kept in sync with cutover.driver_for_provider):
    anthropic/deepseek → claude; openai → codex (native); gemini/openrouter/
    openai_compatible → pi (direct, no gateway)."""
    providers = ["anthropic", "claude", "deepseek", "openai",
                 "gemini", "openrouter", "openai_compatible"]
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id,
              CASE LOWER(COALESCE(doc->>'provider', ''))
                WHEN 'anthropic' THEN 'claude'
                WHEN 'claude'    THEN 'claude'
                WHEN 'deepseek'  THEN 'claude'
                WHEN 'openai'    THEN 'codex'
                ELSE 'pi'
              END AS driver,
              LOWER(COALESCE(doc->>'provider', '')) AS provider,
              COALESCE(doc->>'model', '') AS model,
              COALESCE(doc->>'base_url', '') AS base_url,
              COALESCE(doc->>'supports_responses', '') AS supports_responses
            FROM user_blobs
            WHERE kind = 'model_api'
              AND COALESCE(doc->>'test_status', '') = 'ok'
              AND LOWER(COALESCE(doc->>'provider','')) = ANY(%s)
            ORDER BY user_id
            """,
            (providers,),
        ).fetchall()
    return [
        {"user_id": r[0], "driver": r[1], "provider": r[2], "model": r[3],
         "base_url": r[4], "supports_responses": r[5]}
        for r in rows
    ]
```

In `backend/agent_runtime/supervisor.py`, update `_discover_enabled` to call `db.list_agent_runtime_enabled_users()` with no args and drop its `include_gateway`/`include_pi` params; update `_effective_roster` to drop `pi_enabled`/gateway plumbing and the `FEEDLING_PI_DRIVER_ENABLE`/`FEEDLING_LITELLM_ENABLE` env reads that fed discovery. (Keep the gateway-child spawn for now — removed in Task 5.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_agent_runtime_discovery.py tests/test_agent_runtime_supervisor.py -q`
Expected: PASS. Update any supervisor test asserting `include_gateway`/`pi_enabled` roster behavior.

- [ ] **Step 5: Commit**

```bash
git add backend/db.py backend/agent_runtime/supervisor.py tests/test_agent_runtime_discovery.py tests/test_agent_runtime_supervisor.py
git commit -m "$(cat <<'EOF'
refactor(discovery): discover gemini/openrouter as pi, drop gateway/pi flags

list_agent_runtime_enabled_users takes no flag params and always discovers all
fit-able providers; gemini/openrouter/openai_compatible map to pi. Supervisor
discovery/roster drop include_gateway/pi_enabled and their env reads.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: pi models.json — per-provider api/base/headers

**Files:**
- Modify: `backend/agent_runtime/spawners.py::_pi_models_json` (~L197), `agent_home_files` pi branch (~L469, thread `provider` through)
- Test: `tests/test_agent_runtime_spawners.py`

**Interfaces:**
- Produces: `_pi_models_json(*, base_url: str, model: str, provider: str) -> str`. openrouter → `api:"openai-completions"`, `baseUrl:"https://openrouter.ai/api/v1"`, `headers:{"HTTP-Referer":"https://feedling.app","X-Title":"Feedling"}`; gemini → `api:"google-generative-ai"`, no `compat`; openai_compatible → `api:"openai-completions"` + user `base_url` (unchanged). Every model entry has `"input": ["text","image"]`. `apiKey:"$PI_PROVIDER_API_KEY"` for all.
- Consumes: `provider` from `agent_home_files`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_agent_runtime_spawners.py`:

```python
import json
from backend.agent_runtime import spawners

def _prov(driver_provider, **kw):
    files = spawners.agent_home_files("/h", driver="pi", provider=driver_provider, **kw)
    return json.loads(files["/h/pi-home/agent/models.json"])["providers"]["feedling"]

def test_models_json_openrouter_uses_openai_completions_and_base():
    p = _prov("openrouter", model="x-ai/grok-2", base_url="")
    assert p["api"] == "openai-completions"
    assert p["baseUrl"] == "https://openrouter.ai/api/v1"
    assert p["headers"]["X-Title"] == "Feedling"
    assert p["models"] == [{"id": "x-ai/grok-2", "input": ["text", "image"]}]

def test_models_json_gemini_uses_google_generative_ai():
    p = _prov("gemini", model="gemini-2.5-flash", base_url="")
    assert p["api"] == "google-generative-ai"
    assert "compat" not in p  # compat is openai-completions-specific
    assert p["models"] == [{"id": "gemini-2.5-flash", "input": ["text", "image"]}]

def test_models_json_openai_compatible_unchanged():
    p = _prov("openai_compatible", model="gpt-5.4-mini", base_url="https://api.gemai.cc/v1/")
    assert p["api"] == "openai-completions"
    assert p["baseUrl"] == "https://api.gemai.cc/v1"
    assert p["models"][0]["input"] == ["text", "image"]
```

Update the existing `test_agent_home_files_pi_seeds_models_json_with_relay_provider` to pass `provider="openai_compatible"`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_agent_runtime_spawners.py -q -k "models_json or pi_seeds"`
Expected: FAIL (`_pi_models_json` has no `provider` param; no per-provider branching).

- [ ] **Step 3: Implement**

```python
_PI_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

def _pi_models_json(*, base_url: str, model: str, provider: str) -> str:
    """pi models.json registering the user's provider. api/base/headers vary by
    provider; every model declares input:["text","image"] so pi sends attached
    pictures as real vision content instead of omitting them."""
    p = (provider or "").strip().lower()
    model_entry = {"id": (model or "").strip() or "default", "input": ["text", "image"]}
    if p == "gemini":
        prov = {
            "name": "Feedling relay",
            "api": "google-generative-ai",
            "apiKey": "$PI_PROVIDER_API_KEY",
            "models": [model_entry],
        }
    elif p == "openrouter":
        prov = {
            "name": "Feedling relay",
            "baseUrl": _PI_OPENROUTER_BASE,
            "api": "openai-completions",
            "apiKey": "$PI_PROVIDER_API_KEY",
            "headers": {"HTTP-Referer": "https://feedling.app", "X-Title": "Feedling"},
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
            "models": [model_entry],
        }
    else:  # openai_compatible
        prov = {
            "name": "Feedling relay",
            "baseUrl": (base_url or "").strip().rstrip("/"),
            "api": "openai-completions",
            "apiKey": "$PI_PROVIDER_API_KEY",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
            "models": [model_entry],
        }
    return json.dumps({"providers": {_PI_PROVIDER_ID: prov}}, indent=2) + "\n"
```

In `agent_home_files`, the pi branch already receives `provider`; pass it through:

```python
        files[f"{home}/pi-home/agent/models.json"] = _pi_models_json(
            base_url=base_url, model=model, provider=provider)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_agent_runtime_spawners.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agent_runtime/spawners.py tests/test_agent_runtime_spawners.py
git commit -m "$(cat <<'EOF'
feat(spawners): per-provider pi models.json (openrouter/gemini/openai_compatible)

_pi_models_json emits the right api/base/headers per provider: openrouter →
openai-completions + openrouter base + referer headers; gemini →
google-generative-ai; openai_compatible → relay base. All declare
input:["text","image"] for vision.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: consumer_env — drop LiteLLM gateway env, pi key for every pi provider

**Files:**
- Modify: `backend/agent_runtime/spawners.py::consumer_env` (gateway branch ~L645/743) and `_CONSUMER_ENV_KEYS` (~L786)
- Test: `tests/test_agent_runtime_spawners.py`

**Interfaces:**
- Produces: `consumer_env(...)` sets `PI_PROVIDER_API_KEY=entry["provider_key"]` + `PI_CODING_AGENT_DIR` + `PI_OFFLINE=1` for ALL pi providers (openai_compatible/gemini/openrouter); never sets `FEEDLING_LITELLM_BASE_URL`/`FEEDLING_LITELLM_API_KEY`. `_CONSUMER_ENV_KEYS` has no `FEEDLING_LITELLM_*`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
def test_consumer_env_pi_gemini_sets_provider_key_no_litellm():
    env = spawners.consumer_env(driver="pi", entry={"provider": "gemini", "provider_key": "gk-xyz"}, home="/h")
    assert env["PI_PROVIDER_API_KEY"] == "gk-xyz"
    assert "FEEDLING_LITELLM_BASE_URL" not in env
    assert "FEEDLING_LITELLM_API_KEY" not in env

def test_consumer_env_keys_have_no_litellm():
    assert not any("LITELLM" in k for k in spawners._CONSUMER_ENV_KEYS)
```

(Match `consumer_env`'s real call signature — check the current one and mirror it.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_agent_runtime_spawners.py -q -k "consumer_env"`
Expected: FAIL (gateway branch still injects `FEEDLING_LITELLM_*`; key set only for openai_compatible).

- [ ] **Step 3: Implement**

In `consumer_env`, delete the codex-gateway branch that sets `FEEDLING_LITELLM_BASE_URL`/`FEEDLING_LITELLM_API_KEY`. Make the pi branch fire for every pi provider:

```python
    if driver == "pi":
        env["PI_CODING_AGENT_DIR"] = f"{home}/pi-home/agent"
        env["PI_OFFLINE"] = "1"
        env["PI_PROVIDER_API_KEY"] = entry.get("provider_key", "")
```

Remove `"FEEDLING_LITELLM_BASE_URL"` and `"FEEDLING_LITELLM_API_KEY"` from `_CONSUMER_ENV_KEYS`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/test_agent_runtime_spawners.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agent_runtime/spawners.py tests/test_agent_runtime_spawners.py
git commit -m "$(cat <<'EOF'
refactor(spawners): pi provider key for all pi providers, drop LiteLLM env

consumer_env sets PI_PROVIDER_API_KEY for gemini/openrouter/openai_compatible
and never injects FEEDLING_LITELLM_*; those keys leave _CONSUMER_ENV_KEYS.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Delete the LiteLLM gateway — supervisor child, module, model-id rewrite, heartbeat gateway field

**Files:**
- Modify: `backend/agent_runtime/supervisor.py` (delete gateway child spawn ~L1118-1169, `litellm_gateway` import ~L60, `gateway_model_id` usage ~L934, `gateway` field in `_supervisor_heartbeat_payload`/`_supervisor_instance_payload`/`_heartbeat_loop`)
- Delete: `backend/agent_runtime/litellm_gateway.py`
- Delete: `tests/test_litellm_gateway.py` (if present)
- Create: `tests/test_no_litellm_anywhere.py` (guard, mirrors `test_no_flask_anywhere`)
- Test: `tests/test_agent_runtime_supervisor.py`, `tests/test_no_litellm_anywhere.py`

**Interfaces:**
- Produces: heartbeat payloads with no `gateway` key (or `gateway=False` constant — pick one; tests assert it's not required). Supervisor no longer imports `litellm_gateway`.
- Consumes: `list_supervisor_instance_heartbeats` already surfaces `pi` (fixed 2026-07-07).

- [ ] **Step 1: Write the failing guard + supervisor tests**

`tests/test_no_litellm_anywhere.py`:

```python
import pathlib, re

def test_no_litellm_imports_in_backend():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for py in (root / "backend").rglob("*.py"):
        text = py.read_text()
        if re.search(r"^\s*import\s+litellm\b|^\s*from\s+litellm\b", text, re.M):
            offenders.append(str(py))
        if "litellm_gateway" in text:
            offenders.append(f"{py} (references litellm_gateway)")
    assert offenders == [], f"LiteLLM references remain: {offenders}"

def test_litellm_gateway_module_deleted():
    root = pathlib.Path(__file__).resolve().parents[1]
    assert not (root / "backend/agent_runtime/litellm_gateway.py").exists()
```

In `tests/test_agent_runtime_supervisor.py` add:

```python
def test_heartbeat_payload_has_no_gateway_requirement():
    from backend.agent_runtime import supervisor
    hb = supervisor._supervisor_heartbeat_payload("owner:1", host_all=True, pi=True, ts=1_000_000.0)
    assert hb["pi"] is True and hb["host_all"] is True
    # gateway is no longer a hosting capability the gate depends on
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_no_litellm_anywhere.py tests/test_agent_runtime_supervisor.py -q`
Expected: FAIL (module still exists; `_supervisor_heartbeat_payload` still requires `gateway=`).

- [ ] **Step 3: Implement**

- Delete `backend/agent_runtime/litellm_gateway.py` and `tests/test_litellm_gateway.py`.
- In `supervisor.py`: remove `litellm_gateway` from the `from agent_runtime import …` line; delete the `if gateway_enabled: … start litellm proxy child …` block (~L1118-1169) and the `FEEDLING_LITELLM_*` env reads around it; delete the `gateway_model_id(...)` model rewrite (~L934) so `e["model"]` passes through unchanged.
- Change the heartbeat payload builders to drop the `gateway` param:

```python
def _supervisor_heartbeat_payload(owner, *, host_all, pi, ts):
    return {"ts": ts, "owner": owner, "host_all": bool(host_all), "pi": bool(pi)}
```

Apply the same to `_supervisor_instance_payload` and `_heartbeat_loop` (drop `gateway`). Update their call sites.

- [ ] **Step 4: Run to verify they pass, then the whole agent-runtime + gate suite**

Run: `python -m pytest tests/test_no_litellm_anywhere.py tests/test_agent_runtime_supervisor.py tests/test_agent_runtime_discovery.py tests/test_hosted_agent_runtime_cutover.py tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A backend/agent_runtime/ tests/test_no_litellm_anywhere.py tests/test_agent_runtime_supervisor.py
git rm backend/agent_runtime/litellm_gateway.py tests/test_litellm_gateway.py 2>/dev/null || true
git commit -m "$(cat <<'EOF'
refactor(supervisor): delete LiteLLM gateway child, module, and gateway heartbeat

No provider uses the in-CVM gateway now. Remove the proxy child spawn,
litellm_gateway.py, the gateway model-id rewrite, and the gateway heartbeat
capability. Add a guard test that no LiteLLM references remain in backend/.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Deploy cleanup — remove FEEDLING_LITELLM_* / FEEDLING_PI_DRIVER_ENABLE + LiteLLM venv

**Files:**
- Modify: `deploy/docker-compose.phala.yaml`, `docker-compose.phala.test.yaml`, `docker-compose.phala.pre.yaml`, `docker-compose.phala.prod.yaml` (if present) + each `*.runner.yaml`, `deploy/docker-compose.agent-runner.yaml`, `deploy/docker-compose.ci.yml`, `deploy/docker-compose.memory-sandbox.yaml`
- Modify: `deploy/Dockerfile.agent-runner` (remove LiteLLM venv install)
- Modify: `.github/workflows/ci.yml` (remove `FEEDLING_LITELLM_*` env + `-e` passthroughs and `FEEDLING_PI_DRIVER_ENABLE` from all deploy jobs)
- Test: `tests/test_no_litellm_anywhere.py` (extend to deploy configs)

**Interfaces:**
- Produces: deploy surface with zero `FEEDLING_LITELLM_*` / `FEEDLING_PI_DRIVER_ENABLE`.
- Consumes: nothing.

- [ ] **Step 1: Extend the guard test to deploy configs**

Append to `tests/test_no_litellm_anywhere.py`:

```python
def test_no_litellm_or_pi_flag_in_deploy_configs():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for cfg in list((root / "deploy").rglob("*.yaml")) + \
               list((root / "deploy").rglob("*.yml")) + \
               [root / ".github/workflows/ci.yml", root / "deploy/Dockerfile.agent-runner"]:
        if not cfg.exists():
            continue
        text = cfg.read_text()
        if "FEEDLING_LITELLM" in text or "FEEDLING_PI_DRIVER_ENABLE" in text or "litellm" in text.lower():
            offenders.append(str(cfg))
    assert offenders == [], f"LiteLLM/pi-flag deploy refs remain: {offenders}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_no_litellm_anywhere.py::test_no_litellm_or_pi_flag_in_deploy_configs -q`
Expected: FAIL (many deploy files still reference the vars).

- [ ] **Step 3: Implement**

- Grep the full set: `grep -rniE "FEEDLING_LITELLM|FEEDLING_PI_DRIVER_ENABLE|litellm" deploy/ .github/workflows/ci.yml`.
- Delete every `FEEDLING_LITELLM_ENABLE/PORT/API_KEY/BASE_URL/PYTHON/CONFIG` line from each compose `environment:` / `x-*-env` block and the `FEEDLING_PI_DRIVER_ENABLE` lines added on the pre branch.
- In `ci.yml`, delete the `FEEDLING_LITELLM_*` and `FEEDLING_PI_DRIVER_ENABLE` `env:` mappings and their `-e "…"` passthroughs from every deploy job (test/pre/prod, backend + runner).
- In `deploy/Dockerfile.agent-runner`, remove the LiteLLM venv install step (the `/opt/litellm-venv` pip install) and any `FEEDLING_LITELLM_PYTHON` default.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_no_litellm_anywhere.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add deploy/ .github/workflows/ci.yml tests/test_no_litellm_anywhere.py
git commit -m "$(cat <<'EOF'
chore(deploy): remove LiteLLM venv + FEEDLING_LITELLM_*/FEEDLING_PI_DRIVER_ENABLE

Strip the LiteLLM venv install from the runner image and every
FEEDLING_LITELLM_* / FEEDLING_PI_DRIVER_ENABLE knob from all compose files and
CI deploy jobs. Guard test covers deploy configs.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Docs — driver table + CHANGELOG

**Files:**
- Modify: `backend/agent_runtime/README.md` (driver table), `docs/CHANGELOG.md`
- Test: none (docs)

- [ ] **Step 1: Update README driver table**

Set the driver table to: claude = anthropic/deepseek; codex = openai (native); pi = openai_compatible/gemini/openrouter (direct, no gateway). Remove all LiteLLM-gateway prose.

- [ ] **Step 2: Add CHANGELOG entry**

Add a dated entry summarizing: LiteLLM gateway retired; gemini/openrouter now pi-native; flag removed; per-provider models.json; pre-validated then prod.

- [ ] **Step 3: Run the full suite (final gate)**

Run: `export DATABASE_URL="postgresql://postgres:test@localhost:55432/postgres"; python -m pytest -q`
Expected: PASS (baseline pre-existing `tests/test_data_track.py::test_fast_validation_*` failures excepted — confirm no NEW failures).

- [ ] **Step 4: Commit**

```bash
git add backend/agent_runtime/README.md docs/CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: LiteLLM retired; gemini/openrouter are pi-native

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Post-implementation: pre validation (manual, per spec §7-§8)

Not a code task — the rollout gate. After merging to `pre` and redeploying:
- `phala ssh feedling-io-agents-pre -- 'docker exec agent-runner sh -c "ps aux | grep -i litellm"'` → no LiteLLM process.
- `docker exec agent-runner sh -c "env | grep -i litellm"` → empty.
- End-to-end per provider (real keys): **openrouter** (text/image/tool/reasoning) and **gemini** (same matrix over google-generative-ai — the critical path). Regress openai (codex), anthropic/deepseek (claude), openai_compatible (pi).
- Only after pre is proven: big-bang prod deploy.

---

## Self-Review

**Spec coverage:** §6.1 cutover → Task 1; §6.1 gate → Task 1; §6.2 discovery → Task 2; §6.3 models.json → Task 3; §6.3 consumer_env → Task 4; §6.4 supervisor + §6.5 delete module → Task 5; §6.6 deploy → Task 6; §6.7 docs → Task 7; §7-§8 rollout/testing → post-implementation section. No gaps.

**Placeholder scan:** No TBD/TODO; every code step has real code; commands have expected output. The two spec open questions (gemini base URL, prod user distribution) are resolved during the post-implementation gemini pre-spike, not left as plan placeholders.

**Type consistency:** `driver_for_provider`/`codex_transport` return types consistent across tasks; `_pi_models_json(*, base_url, model, provider)` signature matches its Task 3 definition and Task 3 caller; `list_agent_runtime_enabled_users()` (no params) consistent between Task 2 def and its supervisor caller; heartbeat payload drops `gateway` consistently in Task 5 across all three builders; `PI_PROVIDER_API_KEY` name consistent between Task 3 (`$PI_PROVIDER_API_KEY` in models.json) and Task 4 (env var set by consumer_env).
