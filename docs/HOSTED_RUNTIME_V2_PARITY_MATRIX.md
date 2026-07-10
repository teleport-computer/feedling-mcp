# Hosted Runtime V2 — Parity Matrix

> **This is walkthrough §8 gate 0**, built late (2026-07-10). The gate said: *"Table of every resident capability → V2 action. Gate: every later acceptance test traces to a row."* We skipped it and started at gate 1, so capability gaps have been discovered by accident (three real bugs below were found while building this table, not by tests). Every future V2 acceptance test should cite a row here.
>
> **Scope:** what the legacy per-user resident consumer (`tools/chat_resident_consumer.py` + `backend/agent_runtime/supervisor.py`) can do, versus what the V2 worker pool (`backend/model_api_runtime/v2/*` + `backend/capabilities/*`) can do.

## How to read this

| Mark | Meaning |
|---|---|
| ✅ | Verified in code, `file:line` cited |
| ⚠️ | Exists but broken / half-wired — see notes |
| ❌ | Absent |
| 🔎 | **Inferred, not verified** — needs confirmation before anyone relies on it |

---

## A. Agent tool surface

The resident agent's tools are the `io_cli` subcommands it can shell out to (`tools/io_cli.py`, `add_parser` calls) **plus whatever the codex/claude CLI provides natively** (web, file read, bash). V2's tools are `backend/capabilities/registry.py:12-27`, selectable by the planner's vocabulary (`backend/model_api_runtime/v2/planner.py:17-23`).

| Capability | Resident | V2 | Verdict |
|---|---|---|---|
| `memory_index` | ✅ `io_cli memory-index` | ✅ `registry.py:14` | aligned |
| `memory_fetch` | ✅ `io_cli memory-fetch` | ✅ `registry.py:15` | aligned |
| `perception_snapshot` | ✅ `io_cli perception` | ✅ `registry.py:18` | aligned |
| `perception_trend` | ✅ `io_cli perception-trend` | ✅ `registry.py:19` | aligned |
| `perception_history` | ✅ `io_cli perception-history` | ✅ `registry.py:20` | aligned |
| `screen_recent` | ✅ `io_cli screen-recent` | ✅ `registry.py:21` | aligned |
| `screen_read` | ✅ `io_cli screen-read` | ✅ `registry.py:22` | aligned |
| `photo_recent` | ✅ `io_cli photo-recent` | ✅ `registry.py:23` | aligned |
| `photo_read` | ✅ `io_cli photo-read` | ✅ `registry.py:24` | aligned |
| `memory_search` | ❌ no such tool | ✅ `registry.py:17` | **V2 stronger** (walkthrough called this out) |
| `memory_write` | ❌ agent can't; only the capture lane writes memory | ✅ `registry.py:16` | **V2 stronger** |
| `identity_get` / `identity_patch` | ❌ `io_cli identity-write` is `(phase 2 — not implemented yet)` | ✅ `registry.py:12-13` | **V2 stronger** |
| web search / fetch | 🔎 via codex/claude CLI built-in — **no** `web` subcommand exists in `io_cli` or the consumer | ✅ `web_search` / `web_fetch` `registry.py:26-27` (DuckDuckGo facade) | roughly aligned, different impl |
| **chat image** | ✅ `io_cli chat-image`; **codex attaches images natively** (`chat_resident_consumer.py:2768`); non-native drivers get a local file path (`:371-379`) | ⚠️ `chat_image_read` `registry.py:25` returns raw `image_b64` (`capabilities/chat.py:50-52`) into a **text-only** responder | **🔴 BUG-1, see below** |
| **`schedule_wake` / `cancel_wake`** | ✅ `io_cli schedule-wake` / `cancel-wake` | ❌ not in planner vocabulary (`planner.py:17-23`); executor lists them as control actions and **SKIPs** them (`executor.py:28-31`) — unemittable and uninterpreted | **🔴 GAP** |
| local file read / bash | ✅ codex runs sandbox-bypassed (`--dangerously-bypass-…`), so it reads local files | ❌ | **decide: drop or port** — this may be an accident of the sandbox flag, not a product capability |

---

## B. Background lanes

`LANES = {"chat","manual_wake","heartbeat","scheduled","capture","maintenance"}` (`jobs_store.py:16`). A lane is only a capability if it has **both a producer and a handler**.

| Lane | Resident | V2 producer | V2 handler | Verdict |
|---|---|---|---|---|
| chat | ✅ chat poll loop | ✅ `chat_send_core` → `enqueue_job` | ✅ chat path (`worker.py:395+`) | aligned |
| heartbeat | ✅ `PROACTIVE_TICK_ENABLED` (`:228`), POST `/v1/proactive/tick` | ✅ D3 scheduler (`scheduler.py` + `serve_worker._scheduler_loop`) | ✅ `_run_wake` (`worker.py:392`) | aligned |
| manual_wake | ✅ manual/force payload on `/v1/proactive/tick` | ✅ D3 bridge (`proactive_core.proactive_tick`) | ✅ `_run_wake` | aligned |
| `scheduled` | ✅ `fire_scheduled_wakes` → `/v1/proactive/scheduled/fire`, timers in `proactive_scheduled_wakes_v2` | ❌ **none** | ✅ in `_WAKE_LANES` (`worker.py:100`) | **🔴 dead lane** — handler with no producer; agent-scheduled timers never fire under V2. Compounded by the missing `schedule_wake` capability (§A). |
| `capture` | ✅ `fire_capture_tick()` (`:3844`) → `/v1/capture/tick` | ❌ none | ❌ **none** | **🔴 GAP + BUG-2** |
| dream / memory consolidation | ✅ proactive job kind (`_is_memory_dream_job` `:4782`; `build_dream_prompt` `:125`) | ❌ | ❌ | **🔴 GAP** |
| screen-watch | ✅ `SCREEN_WATCH_ENABLED` / `SCREEN_WATCH_INTERVAL_SEC=120` (`:246-250`) | ❌ | ❌ | **🔴 GAP** |
| maintenance (compaction) | ❌ n/a (resident has no summary compaction) | ✅ `worker.py:363` | ✅ `_run_compaction` (`worker.py:387`) | V2-only |

---

## C. Turn shape

| | Resident | V2 |
|---|---|---|
| Who picks tools | The model itself, natively (`tools=` wire protocol) | A separate **planner** LLM emits a JSON plan of ≤5 actions, **before seeing any tool results** (`planner.py`) |
| Iteration | Multi-round: the codex/claude CLI runs its own agent loop inside `subprocess.run(..., timeout=120)` (`chat_resident_consumer.py:3172+`) | **One shot.** `planner → executor → responder` |
| Replan | n/a | ⚠️ exists but is **not model-driven** — `v2_inval.evaluate(safe_point="before_final_response")` (`worker.py:347-358`) only replans when **a new user message arrived**, never because the model wants more context |
| Who writes the reply | Same model that called the tools; it saw raw tool results in its own conversation | A **different** call (`responder.respond`), which sees tool results only as a truncated JSON blob (`_action_context_str` → `json.dumps(folded)[:8000]`, `responder.py:33`) |

**This is the deepest gap, and it is architectural, not a missing function.** Addressed by `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-agent-loop-design.md`.

---

## D. Not capabilities — infrastructure that lives in the runner container

These block "delete agent-runner" but are a rehome, not a rewrite.

| Thing | Where | Note |
|---|---|---|
| genesis import worker | `FEEDLING_GENESIS_WORKER_ENABLED: "1"` in `deploy/docker-compose.phala.prod.runner.yaml:87`; `supervisor._genesis_worker_should_start` (`:979`) | prod compose comment: the runner CVMs are the **only** place genesis import jobs get drained |
| in-CVM LiteLLM gateway child | `supervisor.py:60` imports `litellm_gateway`; spawned for gateway providers (gemini / openrouter / openai_compatible) | 🔎 **Unverified**: V2's `responder` calls `provider_client` over plain HTTP and never spawns codex, so it *probably* needs no gateway. **Must be confirmed before the gateway child is removed.** |
| per-user leases + heartbeats | `agent_runtime/leases.py`, `agent_runtime_instances` | V2 uses `agent_jobs` single-flight + `v2_worker_heartbeats` instead |

---

## E. Bugs found while building this matrix

Four real defects, none caught by the 2637-test suite — because no test traced to a parity row.

**🔴 BUG-1 — `chat_image_read` poisons the turn's grounding context (live, reachable today).**
`chat_image_read` is in the planner's vocabulary (`planner.py:21`), so the planner *can* pick it when a user sends an image. It returns `{"message_id","image_mime","image_b64"}` (`capabilities/chat.py:50-52`). `_fold_action_results` copies `data` **verbatim, no key whitelist** (`responder.py`), then `_action_context_str` does `json.dumps(folded)[:8000]`. A base64 JPEG blows past 8000 chars, so the model receives ~8000 characters of truncated base64 **instead of** the memory cards / perception it also fetched. Not "V2 is image-blind" — V2 actively corrupts the turn when it tries to look at an image.

**🔴 BUG-2 — a `capture`-lane job falls through to the chat path.**
`enqueue_job(uid, "capture")` is accepted (`capture` ∈ `LANES`, `jobs_store.py:16`), but `process_job` dispatches only `maintenance` (`worker.py:387`) and `_WAKE_LANES` (`:392`); everything else takes the chat path. The chat path's early-return guard is `if not coalesced and lane == "chat"` (`worker.py:404`), so a capture job does **not** bail — it runs planner → executor → responder and **writes a chat bubble**, and on failure emits a user-visible error chip. Latent only because nothing enqueues `capture` today.

**🔴 BUG-3 — `scheduled` lane is a handler with no producer.**
See §B. Agent-scheduled wakes silently never fire for `db_action_v2` users.

**🔴 BUG-4 — a chat turn whose plan omits `final_response` silently produces no reply (live, hits *trusted* models only).**
`validate_plan` appends `final_response` only when the model asked for it (`planner.py:54-58`); it never forces one. `worker.py:458` computes `wants_reply = any(s["type"] == "final_response" for s in steps)`, and when it's False the responder is skipped entirely — the job goes `mark_completed` + `_emit_status "done"` with **no chat bubble**. The user's message is swallowed, and the client's long-poll sees a completed turn with nothing in it.

Reproduced:
```
>>> planner.validate_plan({"plan":[{"type":"memory_search","payload":{"query":"x"}}]})
[{'type': 'memory_search', 'payload': {'query': 'x'}}]     # no final_response
>>> wants_reply
False                                                       # → responder never runs
```
`rule_plan` always appends `final_response` (`planner.py:75`), so weak/relay models are immune. Only `official_plan` — the *trusted* models we route our best users to — can drop it. `_PLANNER_SYSTEM` says "include final_response LAST", which is a prompt-level hope, not an invariant.

This is the deepest reason the one-shot shape is wrong: **"the planner didn't ask to reply" and "the planner wants more tools first" are the same wire signal today**, and the worker guesses the first. Under the agent loop the same signal means "loop again", with a forced `final_response` at the round cap — so the loop **fixes BUG-4 by construction** rather than needing a separate patch.

---

## F. Triage

**1. Must build (V2 cannot replace resident without these)**
- Agent loop (see §C and the loop spec) — the one-shot planner cannot chain or recover from a tool miss. **Also fixes BUG-4.**
- Multimodal: give `provider_client` a real image content block; stop feeding b64 through the text context (fully fixes BUG-1; the loop round only stops the bleeding).
- `schedule_wake` / `cancel_wake` capability + planner vocabulary, and a producer for the `scheduled` lane (fixes BUG-3).
- `capture` lane: producer + handler (fixes BUG-2).
- `dream` lane.
- `screen_watch` lane.

**2. Decide: drop or port**
- Local file read / bash. Resident has it only because codex runs with the sandbox bypassed. Probably not an intended product capability.

**3. V2 is already ahead — no work**
- `memory_search`, `memory_write`, `identity_get`/`identity_patch`, summary compaction.

**4. Infrastructure rehome (not capability work)**
- Move the genesis import worker out of the agent-runner container.
- Confirm gateway providers need no in-CVM LiteLLM under V2, then drop the child.

---

## G. Open questions

1. **Local file read / bash** — port, or declare it a sandbox accident and drop it?
2. **Gateway + LiteLLM** — does V2 truly bypass it? (Blocks removing the supervisor's LiteLLM child.)
3. **Resident web access** — confirm it comes from the CLI's built-in web tool and not something we haven't found. If a user's provider is a relay whose CLI has no web tool, does the resident agent have web at all today? (Affects whether V2's DuckDuckGo facade is parity, a regression, or an upgrade.)
4. **dream / screen_watch** — still wanted as products, or candidates for §2?

---

## Traceability rule (the gate)

From here on: **every V2 acceptance test names the row it covers.** A capability with no row is not shipped; a row with no test is not done.
