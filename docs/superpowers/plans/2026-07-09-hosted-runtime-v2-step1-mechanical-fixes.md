# Hosted Runtime V2 — Step 1: Mechanical Merge-Condition Fixes

> **For agentic workers:** execute via superpowers:subagent-driven-development, **NO-COMMIT mode** (user commits themselves — never `git add`/`git commit`). Review each task via filesystem-snapshot diff.
> **Source:** `2026-07-09-hosted-runtime-v2-merge-conditions-backlog.md` — this plan implements the "机械项 (A-C 补丁)" rows only. Conditions 2, 3, §6-admission-ceiling are deferred to subproject D (step 2).

**Goal:** Close the mechanical §9 merge-conditions on `feat/hosted-runtime-v2`: 1a worker liveness guard, 1c error surface, 1e async provider, 4a vocab reconcile, 4b web capability, 4c memory_search, 5 deployment docs.

**Tech Stack:** Python, FastAPI, Postgres (psycopg), Alembic, pytest. Docker PG on :55432 for DB-backed tests.

## Global Constraints (copy into every reviewer prompt)

- **NO commits, NO `git add`.** User commits at the very end.
- **Dependency direction (hard, AST-guarded by `tests/test_v2_dependency_direction.py`):** modules under `backend/model_api_runtime/v2/` and `backend/capabilities/` MUST NOT import `hosted` or `agent_runtime`. Only `serve_worker.py` (injection layer) wires hosted deps in via `TurnDeps`. `provider_client` (at `backend/provider_client.py`) IS allowed.
- **BYOK-only invariant:** every LLM call uses the user's own JIT-decrypted key. No platform LLM key anywhere. Don't regress.
- **No-filler:** only model-authored `final_response` writes a chat bubble. Status/error events are runtime-authored, never assistant text.
- **Baseline:** 7 pre-existing failures on origin/test (user_blobs FK + verify-ping) are NOT regressions. Full backend suite baseline = 2440 passed / 7 failed with Docker PG on :55432.
- All new work under existing packages; follow `CONTRIBUTING.md` (asgi_app.py assembly-only, etc.).

---

## Task 1 (cond 1e): async reliable provider wrapper

**Files:**
- Modify: `backend/provider_client.py` (add `reliable_chat_completion_async`, near the sync `reliable_chat_completion` @ line 69)
- Modify: `backend/model_api_runtime/v2/responder.py` (respond → await async wrapper)
- Modify: `backend/model_api_runtime/v2/planner.py` (official_plan → await async wrapper)
- Modify: `backend/model_api_runtime/v2/worker.py` (drop the `asyncio.to_thread(reliable_chat_completion, …)` bridges @ ~204, ~243 — call awaitable directly)
- Test: `tests/test_provider_client_async_reliable.py` (new), update any v2 responder/planner tests that patched the sync path.

**Interfaces:**
- Produces: `async def reliable_chat_completion_async(config, messages, *, max_attempts=3, base_delay_sec=1.0, max_delay_sec=30.0, **kwargs) -> dict` — same retry/backoff/classification as sync `reliable_chat_completion` (lines 84-103) but `await asyncio.sleep` instead of `time.sleep`, and `await chat_completion_async(...)` instead of `chat_completion(...)`. Same `feedling_error_class` tagging on terminal failure. Reuse existing `classify_provider_error`, `_retry_after_seconds`.

**Steps:**
- [ ] Write failing test: async wrapper retries transient then succeeds; sets `feedling_error_class="transient_exhausted"` after max_attempts; never retries `provider_config` class. Use a fake async chat fn raising a classified transient twice then returning.
- [ ] Implement `reliable_chat_completion_async` mirroring the sync retry loop; `await asyncio.sleep(delay)`.
- [ ] Point v2 responder/planner at it; remove the `to_thread` bridges in worker.py so provider calls are truly async (unblocks >32 concurrency).
- [ ] Run: `pytest tests/test_provider_client_async_reliable.py tests/test_v2_responder.py tests/test_v2_planner.py -v` (adjust names to actual). Expected: green.

**Note:** verify no remaining `to_thread(reliable_chat_completion` in v2/. The reads/enclave calls stay wrapped by ENCLAVE_SEMAPHORE — only the *provider* call changes to native async.

---

## Task 2 (cond 1a): v2 worker liveness heartbeat + send-path guard

**Files:**
- Create: `backend/alembic/versions/0015_v2_worker_heartbeats.py` (new table, down_revision = 0014)
- Modify: `backend/model_api_runtime/v2/jobs_store.py` (add `record_worker_heartbeat(worker_id)` + `workers_alive(*, within_sec=30) -> bool`)
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (heartbeat loop: every ~10s UPSERT this process's row; gather it alongside worker+reaper loops with `return_exceptions=True`)
- Modify: `backend/hosted/chat_send_core.py` (db_action_v2 branch: BEFORE persisting the user message, `if not jobs_store.workers_alive(): return <distinct 503 workers_unavailable>`)
- Test: `tests/test_v2_worker_heartbeat.py` (new), extend `tests/test_hosted_runtime_mode.py` or the chat_send test for the guard.

**Interfaces:**
- Table `v2_worker_heartbeats(worker_id text primary key, beat_at timestamptz not null default now())`.
- Produces: `record_worker_heartbeat(worker_id: str) -> None` (UPSERT beat_at=now()); `workers_alive(*, within_sec: int = 30) -> bool` (`SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats WHERE beat_at > now() - make_interval(secs=>within_sec))`).

**Steps:**
- [ ] Write migration 0015 (create table; drop in downgrade). Run `alembic upgrade head` against Docker PG :55432; confirm table exists.
- [ ] Write failing test: `workers_alive()` false on empty table; true after `record_worker_heartbeat("w1")`; false again after simulating stale (insert beat_at = now()-60s).
- [ ] Implement the two jobs_store fns.
- [ ] serve_worker: add `_heartbeat_loop(worker_id, stop_event)` doing `record_worker_heartbeat` every 10s; gather with the existing worker+reaper loops (`return_exceptions=True`). worker_id from an existing per-process id (reuse whatever serve_worker already uses; else `f"{socket.gethostname()}:{os.getpid()}"` — no Date/random needed).
- [ ] chat_send_core: in db_action_v2 branch, before any v2 persist, call `workers_alive()`; if false return a distinct response — reuse the existing 503 helper shape but a NEW error code `workers_unavailable` (MUST be distinct from the supervisor-dead 503 so its meaning is preserved). Nothing persisted on refusal.
- [ ] Write failing test: with no heartbeats, POST send in db_action_v2 mode → distinct 503/`workers_unavailable`, and assert NO agent_jobs row and NO chat message persisted.
- [ ] Run: `pytest tests/test_v2_worker_heartbeat.py tests/test_hosted_runtime_mode.py -v` + the chat_send guard test. Expected: green.

**Placement care:** the guard must sit BEFORE the message-append at `chat_send_core.py:~135-141`, mirroring the wedge-guard "nothing persists unless we've committed to answering" lesson. Confirm the `_v2_mode` detection is available at that point; move detection earlier if needed.

---

## Task 3 (cond 1c): terminal-failure error surface

**Files:**
- Modify: `backend/model_api_runtime/v2/status_stream.py` (add `error` kind to `_KIND_LABEL` @ 44-57, label e.g. `"出问题了"` — keep copy neutral, runtime-authored, NOT in companion voice)
- Modify: `backend/model_api_runtime/v2/worker.py` (TurnDeps: add `record_terminal_error: Callable[[str, str], None] | None`; both failure paths — the `except` @ ~261-264 and the provider-resolve failure @ ~276-280 — emit `_emit_status(user_id, job_id, "error")` and call `deps.record_terminal_error(user_id, msg)` if injected)
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (wire `record_terminal_error` into TurnDeps → calls config_store)
- Modify: `backend/hosted/config_store.py` (add PUBLIC `set_last_runtime_error(store, message: str) -> None` wrapping `_patch_model_api_runtime_profile(store, {"last_runtime_error": str(message)[:300]})`)
- Test: `tests/test_v2_error_surface.py` (new); extend `tests/test_v2_worker.py`.

**Interfaces:**
- Consumes: `config_store.set_last_runtime_error(store, message)` (new public wrapper).
- Produces: `TurnDeps.record_terminal_error` callback; on terminal failure an `error`-kind status event exists in the stream AND `last_runtime_error` is patched so iOS (`hosted/setup_core.py:265`) shows a chip.

**Steps:**
- [ ] config_store: add `set_last_runtime_error`; unit test it patches the profile field (truncated to 300).
- [ ] status_stream: add `error` kind + label; test `redact_status("error")` returns the label (not the `处理中` fallback).
- [ ] worker: add `record_terminal_error` to TurnDeps (default None — keep dependency boundary; do NOT import hosted in worker). In both failure paths emit error status event + call the injected callback (guard `store`/re-fetch via `core_store.get_store(user_id)` since the early-failure `store` binding may be unbound).
- [ ] serve_worker: wire the callback to `lambda uid, msg: config_store.set_last_runtime_error(core_store.get_store(uid), msg)`.
- [ ] Write failing test: a job whose turn raises → status stream contains an `error` event AND `set_last_runtime_error` was invoked (spy/mock the callback). Transient-then-success does NOT emit error (retries already exhausted inside reliable wrapper before reaching this except, so terminal only).
- [ ] Run: `pytest tests/test_v2_error_surface.py tests/test_v2_worker.py tests/test_v2_dependency_direction.py -v`. The dependency-direction test MUST stay green (proves worker still doesn't import hosted).

---

## Task 4 (cond 4a): reconcile planner vocabulary with registry

**Files:**
- Modify: `backend/model_api_runtime/v2/planner.py` (`_WRITE_ACTIONS` @ 22-25 + system-prompt list @ 112-115: REMOVE `capture_memory, schedule_followup, schedule_wake, cancel_wake, sleep`)
- Modify: `backend/model_api_runtime/v2/planner.py` `rule_plan` — verify/adjust: if `rule_plan` emits `{"type":"sleep"}` for any FOREGROUND (chat/manual_wake) case, replace with an empty/read-only plan so the responder answers from prefetch (foreground always replies; sleep=no-reply is a wake/D concern).
- Modify: `docs/superpowers/specs/runtime-v2-parity-matrix.md` (mark schedule/capture/sleep as **deferred to subproject D**, not V2 foreground actions)
- Test: update `tests/test_v2_planner.py` (assert pruned vocab; assert planner never emits a non-registry action for a chat lane).

**Interfaces:**
- After this task, every action-type the planner can emit for chat/manual_wake lanes ∈ `registry.CAPABILITIES.keys() ∪ {final_response}`. (web_search/memory_search get ADDED in Tasks 5/6.)

**Steps:**
- [ ] Read `rule_plan` fully; determine every path that can emit `sleep`/scheduling. Confirm foreground vs wake usage.
- [ ] Write failing test: for a chat-lane input, `plan(...)` (both rule_plan and a mocked official_plan returning junk) yields only actions in `CAPABILITIES ∪ {final_response}`; no `sleep`/`schedule_*`/`capture_memory`.
- [ ] Remove the 5 strings from `_WRITE_ACTIONS` and the system prompt. Fix `rule_plan` foreground sleep→read-only/empty.
- [ ] Update parity matrix rows (schedule/capture/sleep → D).
- [ ] Run: `pytest tests/test_v2_planner.py tests/test_v2_executor.py -v`. Expected: green (executor no longer receives dead strings; skip bucket for those disappears).

**Note:** `capture_memory` is a pure removal — `memory_write` (already in vocab, maps to `registry.memory_write`) is the real memory-write path. Do NOT add a `capture_memory` capability.

---

## Task 5 (cond 4b): web capability facade

**Files:**
- Create: `backend/capabilities/web.py` (`web_search`, `web_fetch` → `CapabilityResult`)
- Modify: `backend/capabilities/registry.py` (register `web_search`, `web_fetch`; add to `READ_ACTIONS`)
- Modify: `backend/model_api_runtime/v2/planner.py` (add `web_search`, `web_fetch` to `_READ_ACTIONS` + system prompt so the planner may emit them)
- Modify: `docs/superpowers/specs/runtime-v2-parity-matrix.md` (add web_search/web_fetch rows — parity with legacy runtime web access)
- Test: `tests/test_capability_web.py` (new)

**Interfaces:**
- `web_search(store, *, api_key, runtime_token, params) -> CapabilityResult` — facade over `model_api_runtime/tools.run_web_searches` (keyless DuckDuckGo scrape). `params={"query": str, "limit": int?}`. `ok=True, data=[{title,url,snippet}]`. Reuse `tools.sanitize_web_query`/`query_has_sensitive_data` for redaction.
- `web_fetch(store, *, api_key, runtime_token, params) -> CapabilityResult` — `params={"url": str}`; `httpx.get` + `tools._strip_html_text`; size-cap + timeout; `ok=True, data={"url","text"}`.

**Steps:**
- [ ] Write failing test: `web_search` returns ok result list (monkeypatch `tools.run_web_searches` to a fixture — no live network in tests); sensitive query is redacted/blocked. `web_fetch` returns stripped text (monkeypatch httpx). Enforce timeout + size cap.
- [ ] Implement `capabilities/web.py` as thin facade over `tools.py`; adapt to `CapabilityResult` via existing `cap_data`/`ok`/`err` helpers (`capabilities/types.py`, `errors.py`).
- [ ] Register in registry; add to planner READ vocab + prompt.
- [ ] Update parity matrix.
- [ ] Run: `pytest tests/test_capability_web.py tests/test_v2_planner.py -v`. Expected: green. **No live network in tests** — always monkeypatch.

---

## Task 6 (cond 4c): memory_search capability

**Files:**
- Modify: `backend/capabilities/memory.py` (add `search` → alias of `index` with `query` always forwarded)
- Modify: `backend/capabilities/registry.py` (register `memory_search` → `memory.search`; READ_ACTIONS)
- Modify: `backend/model_api_runtime/v2/planner.py` (add `memory_search` to `_READ_ACTIONS` + prompt: "keyword/grep over memory cards")
- Modify: `docs/superpowers/specs/runtime-v2-parity-matrix.md` (add memory_search row — better-than-parity: legacy runtime can't search memory)
- Test: `tests/test_capability_memory.py` (extend)

**Interfaces:**
- `memory.search(store, *, api_key, runtime_token, params) -> CapabilityResult` — calls the same path as `memory.index` (`memory/memory_core.py:index` → `memory_readside_core.memory_index_core`) but requires `params["query"]` (non-empty; err if missing). Returns the matched cards.

**Steps:**
- [ ] Write failing test: `memory_search` with a `query` forwards it to `memory_core.index` (monkeypatch/assert query reaches the core payload); missing query → `err` (bad_request).
- [ ] Implement `memory.search` (thin: set query, delegate to existing index path).
- [ ] Register `memory_search`; add to planner READ vocab + prompt.
- [ ] Update parity matrix.
- [ ] Run: `pytest tests/test_capability_memory.py tests/test_v2_planner.py -v`. Expected: green.

**Note:** ranking/keyword match is enclave-side; the Python facade only guarantees `query` is forwarded. Don't reimplement matching.

---

## Task 7 (cond 5): pin deployment target in docs

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (docstring @ ~245-247: remove the hedge "may run in a separate process/CVM/pod"; state definitively: runs as a sibling entrypoint inside the **runner CVM supervisor**, same image, alongside resident consumers + genesis worker)
- Modify: `deploy/DEPLOYMENTS.md` (runner-CVM section: add a `serve_worker` (v2 worker pool) entry next to the genesis worker; note it's gated by rollout flag / not yet started in prod)
- Modify: `docs/superpowers/plans/2026-07-09-hosted-runtime-v2-merge-conditions-backlog.md` (flip condition 5 row → docs done; actual process start remains subproject D rollout)

**Steps:**
- [ ] Tighten serve_worker docstring to the single decided target (runner CVM). No code change.
- [ ] Add the DEPLOYMENTS.md entry (documentation of intent; actual manifest/compose start = D).
- [ ] Update the backlog row.
- [ ] No test (docs only). Sanity: `pytest tests/test_v2_dependency_direction.py -q` still green (unchanged code).

---

## Final whole-step review

After Tasks 1-7: dispatch one whole-diff reviewer (most capable model) over the step-1 diff. Verify: dependency-direction test green, BYOK invariant intact, no-filler intact (error events are runtime-authored, not bubbles), full backend suite = baseline (2440/7) with no NEW failures. Record results in `.superpowers/sdd/progress.md`. Leave everything uncommitted for the user.
