# Hosted Runtime V2 — PR C: Unified Provider-Native Tool Loop — Design

**Status:** LANDED / CURRENT ARCHITECTURE RECORD
**Depends on:** PR A (effect foundation, merged `d7e19a9`) + PR B (provider transport/telemetry, merged)
**Directive:** @sxysun 4-PR next round — PR C is "unified loop". Product hard decision: NO official/rule behavior tiering by provider/model. All models go through ONE provider-native tool loop; behavior degrades naturally from what the model actually does. **7 P0 fault injections must be green before any deploy / any internal-user flip.**

## Goal

Replace the staged `is_official → rule_plan/official_plan → executor → forced separate responder` V2-foreground pipeline with ONE provider-native tool loop. Every model (official or BYOK-weak) receives the same tool catalog (the 18 capabilities + a `reply` special tool) as `ToolSpec`s and runs the same loop. The model's `tool_calls` drive actions; `reply{text}` writes an immediately-visible intermediate bubble and continues; a response with no `tool_calls` means its plain text IS the final reply (no forced second responder call). Effects flow through PR A's generation-fenced outbox (this PR is the first to WIRE the effect producers). Provider calls use PR B's `tools=`/`tool_calls` transport and fold into the whole-turn metric.

**Scope (user-decided):** the **chat AND wake** foreground lanes migrate to the unified loop. `extraction`/`compaction` lanes keep their current direct single-call paths (they are not conversational tool loops).

## Key product rules (from the directive, locked)

- No `is_official`→rule/official behavior dispatch anywhere in V2 production. `_is_official_identity` survives ONLY as an identity-copy tag, never deciding the loop.
- Same tool catalog + schema + loop for all models.
- No tool_call → the model's plain text is the implicit final reply. Normal chat never forces an extra responder round-trip.
- `reply{text}` is a worker special tool: it writes an intermediate bubble immediately, then the loop continues.
- Before every provider call, fold in any newly-visible user messages — no debounce, no loop restart; already-completed tool results are preserved.
- Same-round reads run in parallel; writes/replies execute in a deterministic order; `call_id`s are never dropped.
- A response carrying BOTH plain text and tool_calls: the text is **preamble/thinking, NOT a bubble** (this closes the known "claude『我去查查』preamble leaked as the reply" bug). Only `reply{text}` or a no-tool-call terminal response produces a user bubble.
- broken tool args, or relay tools returning 400/422: exactly one same-turn fallback, no persisted tier.
- web/tool observations carry provenance and can NEVER self-authorize a durable write.

## Current state (grounding — verified, file:line)

- **Turn loop:** `worker.process_job` chat branch (`worker.py:933-1074`) is a two-layer loop: outer `while True` with `replan_count`/`llm_calls` (cap `_TURN_MAX_LLM_CALLS=6`, `:126`) wrapping `v2_agent_loop.run_turn(decide=_decide, run_tools=_run_tools, …)` (`:997`). `_decide` (`:948`) calls `v2_planner.plan` (`:959`); `_run_tools` (`:975`) calls `v2_executor.execute_plan` (`:992`). After the loop, `v2_responder.respond` (`:1059`) is FORCE-called for chat (`wants_reply` hard-wired True, `:1024`). End-of-turn `deps.apply_pending_effects(user_id)` drain already wired (`:1106-1110`) — but no producer feeds it.
- **`v2_agent_loop`** (`agent_loop.py`): a bounded state-machine skeleton (`Decision{actions,wants_reply,final_text}`, `LoopResult`, stop reasons), NOT a real tool loop. `final_text` is a dead seam reserved for a native tool-calling backend.
- **`planner.py`:** `plan()` branches on `is_official` (`:134`): `rule_plan` (zero-LLM, `:85`) vs `official_plan` (BYOK JSON-plan call, `:207`). "Plan" = `list[{"type","payload"}]`, ≤5, closed vocab `_READ_ACTIONS ∪ _WRITE_ACTIONS ∪ {final_response}` (`:20-28`). The JSON-plan-in-a-string protocol + `_PLANNER_SYSTEM` prose (`:144-165`) is what `tools=[ToolSpec]` replaces.
- **`executor.py`:** `execute_plan` (`:110-174`) — `_split_plan` (`:36`) buckets by `cap_registry.READ_ACTIONS`/`WRITE_ACTIONS`; reads parallel under `read_sem` (`asyncio.gather`, `:150`), writes strictly serial with a `before_write` fence re-checking runtime-mode/lease (`:162-171`, wired to `_ensure_runtime_mode`+`_renew_lease`). `_run_one` (`:72`) = `mark_action_running → cap_registry.run_capability → mark_action_done/failed`. **This read-parallel / write-serial-fenced machinery is reused, not rebuilt.**
- **`responder.py`:** `respond(*, provider_config, summary, tail, action_results, usage_out, …) -> str` (`:109`) — the forced second LLM call; folds `action_results` into grounding context (`_fold_action_results`, `:63`, with char caps + blob-strip = BUG-1 defense), builds messages via `context.build_turn_messages`, makes its own `reliable_chat_completion_async` (`:154`, no tools). **Removed from the hot path**; its context-assembly helpers are salvaged.
- **Tool catalog EXISTS:** `capabilities/registry.py` — `CAPABILITIES` (18 names → fns, `:11-30`), `WRITE_ACTIONS={memory_write,identity_patch,schedule_wake,cancel_wake}` (`:32`), `READ_ACTIONS` (the other 14, `:33`), `run_capability(action_type, store, *, api_key, runtime_token, params)` (`:36`). PR C derives `ToolSpec`s from this instead of hand-writing prose. `web_search`/`web_fetch` (`capabilities/web.py`) return untrusted external content (the provenance concern).
- **PR A outbox (producer side unwired):** `db.effect_enqueue(effect_id,user_id,job_id,effect_type,expected_generation,payload)` (`db.py:3426`), `effect_id.derive(*,job_id,effect_type,ordinal)` (`effect_id.py:11`), `derive_control` (`:16`). No `effect_outbox.enqueue_effect` wrapper yet. Consumer `apply_pending_effects` + `serve_worker.build_production_effect_dispatch` + 7 sinks already built/wired. Effect types: `{reply,status,cursor,job,memory,identity,schedule}`. A producer needs `expected_generation` = the turn's pinned runtime generation (ABA-safety).
- **PR B transport:** `provider_client.chat_completion_async(config, messages, *, tools: list[ToolSpec]|None=None) -> dict` with `result["tool_calls"]` = `[{id,name,args,args_raw,args_ok}]`; `provider_types.ProviderResponse.from_result`; `TurnMetrics.add_call(usage)`.
- **Message fold:** `_coalesce_inputs` (`worker.py:377`) → `v2_coalesce.coalesce_pending` (ts-cursor). Today a new-message fold path RESTARTS the outer loop via `v2_inval.evaluate`→REPLAN→`continue`. PR C's per-round fold replaces this for chat/wake. PR A `cursor.py` provides the seq-based cursor (`load_seq`, `advance_effect`) preferred over the ts-cursor (ts collisions under concurrent workers).

---

## Components

### C1 — Tool-schema derivation (`backend/capabilities/tool_schema.py`, new)

`build_tool_specs() -> list[ToolSpec]`: one `ToolSpec(name, description, parameters)` per `registry.CAPABILITIES` name, plus the synthetic `reply` tool (`parameters={"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}`). `parameters` is an explicit JSON-Schema object per capability, derived from each capability's known param shape (today only prose in `_PLANNER_SYSTEM`/each capability's `params` handling — this PR makes it a real schema). `chat_image_read` is excluded from the model-facing catalog (BUG-1 mitigation, mirroring `planner.py:17-19`). The catalog is identical for every provider/model.

### C2 — The unified tool loop (`backend/model_api_runtime/v2/tool_loop.py`, new; rewrites `agent_loop.py`'s role)

`async run_tool_loop(*, provider_config, build_messages, dispatch_tools, on_reply, fold_new_messages, tm, max_calls, provenance_seed) -> LoopOutcome`. Pure of hosted/db (dependencies injected), dependency-direction-clean. Per round:
1. `messages = build_messages(folded_inputs, prior_tool_results)`.
2. `result = await provider_client.chat_completion_async(provider_config, messages, tools=CATALOG)`; `tm.add_call(result["usage"])`.
3. `pr = ProviderResponse.from_result(result)`.
4. If `pr.tool_calls` is empty → **terminal**: `pr.text` is the final reply → `on_reply(pr.text, final=True)`; return.
5. Else: the accompanying `pr.text` (if any) is preamble/thinking — NOT a bubble (dropped from user-visible output; may be folded into thinking telemetry). Classify each tool_call: `reply` special tool → `on_reply(args["text"], final=False)` (intermediate bubble, loop continues); other names → `dispatch_tools(tool_calls)` → `list[ToolResult]` (with provenance).
6. Before the next provider call: `folded_inputs += fold_new_messages()` (newly-visible user messages via the seq cursor) — appended, loop NOT restarted, prior tool_results preserved.
7. Bounded by `max_calls` (reuse `_TURN_MAX_LLM_CALLS=6`): on the last allowed call, request with `tools` omitted (or force a terminal) so the turn always ends with model-authored text, never a filler.

`LoopOutcome{final_text, rounds, stop_reason, replied_intermediate: bool}`. Stop reasons reworked around "empty tool_calls = done" / "budget exhausted" (drop the `final_response` sentinel scan).

### C3 — Tool-call dispatcher (refactor `executor.py` to accept `ToolCall`s)

`dispatch_tool_calls(tool_calls: list[ToolCall], *, provenance, store, api_key, runtime_token, enqueue_effect, before_write) -> list[ToolResult]`. Reuses `_split_plan`'s read/write bucketing (by `cap_registry.READ_ACTIONS`/`WRITE_ACTIONS`) and the existing read-parallel (`read_sem`+`gather`) / write-serial+`before_write`-fence loop. Changes:
- Input is `ToolCall{id,name,args}` not `{"type","payload"}`; output is `ToolResult{call_id, content}` preserving each `call_id`.
- **Reads** run inline (their content feeds back to the model), via `run_capability(name, store, api_key=…, runtime_token=…, params=args)`; the `ToolResult` is tagged provenance `external` for `web_search`/`web_fetch`, else `internal`.
- **Writes** (`WRITE_ACTIONS`) are NOT run inline — they are enqueued as effects (C5) AFTER passing the provenance write-gate (C4); the `ToolResult` reports "queued" (so the model sees the write was accepted, and the durable apply happens through the fenced outbox).
- Unknown tool name / broken args (`args_ok=False`) → a `ToolResult` describing the error (no crash); the model may correct on the next round (one same-turn fallback, C-error-handling).

### C4 — Provenance tracking + deterministic write gate

Each `ToolResult` carries `provenance ∈ {user, wake_trigger, external, internal}`. The turn seeds provenance: a chat turn with a real user message → `user` authorization present; a wake turn → `wake_trigger` authorization present. `web_search`/`web_fetch` results are `external`.

**Write gate (deterministic, in the dispatcher):** a write tool_call (`memory_write`/`identity_patch`/`schedule_wake`/`cancel_wake`) is authorized only if the turn holds a `user` or `wake_trigger` authorization. A write whose turn holds NO such authorization — i.e. the turn's only provenance is `external` tool content (a purely web-driven round with no user/wake origin) — is **deterministically refused**: the dispatcher returns a refusal `ToolResult` ("write refused: no user/wake authorization in this turn"), enqueues NO effect, performs NO durable write. This is a fixed rule in code, independent of what the tool content says — so a malicious page instructing `memory_write` cannot self-authorize. (Chat/wake turns legitimately keep their write ability; the gate only bites a turn with no user/wake origin, the malicious-page P0 case.) The existing `before_write` runtime-mode/lease fence still applies on top.

### C5 — Effect producers wired to the PR A outbox (`effect_outbox.enqueue_effect` + call sites)

Add the missing thin wrapper `effect_outbox.enqueue_effect(*, job_id, user_id, effect_type, ordinal, expected_generation, payload) -> str` = `eid = effect_id.derive(job_id=…, effect_type=…, ordinal=…); db.effect_enqueue(eid, user_id, job_id, effect_type, expected_generation, payload); return eid`. Every durable side effect the loop produces enqueues here with the turn's pinned `expected_generation` and a monotonically increasing per-turn `ordinal`:
- `reply{text}` (intermediate + terminal) → `reply` effect, payload `{"text": str}`.
- `memory_write` → `memory` effect `{"actions": [...]}`; `identity_patch` → `identity` `{"patch": {...}}`; `schedule_wake`/`cancel_wake` → `schedule`.
- status transitions → `status`; cursor advance → `cursor` (via `cursor.advance_effect`); follow-up job → `job`.

Reads are NOT effects (they run inline). The already-wired `apply_pending_effects` drains the outbox; because every effect is `effect_id`-keyed + generation-fenced, a turn retry/redelivery re-enqueues the SAME ids (no duplicate bubbles/writes) — this is how "retry 不重复" holds.

### C6 — Immediate reply visibility

When the loop emits a `reply` effect (intermediate `reply{text}` tool OR terminal plain text), it calls `apply_pending_effects(user_id)` right after enqueuing so the reply sink writes the bubble immediately (mid-loop). `apply_pending_effects` is idempotent + generation-fenced, so calling it repeatedly mid-loop is safe; a final drain at turn end flushes any remaining non-reply effects. This satisfies "reply{text} 可立即写中间 bubble 后继续".

### C7 — Per-round message folding (no debounce, no restart)

`fold_new_messages()` re-reads user messages after the turn's seq cursor (PR A `cursor.load_seq` / `db.chat_messages_after_seq`) and returns any newly-visible ones. Called before each provider call inside the loop. The loop appends them to `folded_inputs`; it does NOT reset round state, does NOT re-enter an outer loop, and preserves already-completed `ToolResult`s. Replaces the chat lane's `v2_inval.evaluate`→REPLAN→`continue` restart machinery. No time-based debounce.

### C8 — Wake lane on the unified loop

`_run_wake` (`worker.py:497`) calls `run_tool_loop` with a wake-flavored `build_messages` (proactive "open the conversation" system prompt) and `provenance_seed=wake_trigger` (no user message required; the old `ResponderError`-on-no-user-message guard does not apply to wake). Same catalog, same loop, same effect outbox. Wake writes are authorized by `wake_trigger`.

### C9 — worker.py surgery + deletions

- `process_job` chat branch (`:933-1074`): replace the two-layer loop + forced `responder.respond` with a single `run_tool_loop` call; remove `is_official` from control flow.
- `_run_wake`: route to `run_tool_loop` (C8).
- `TurnDeps.is_official`: demoted to a telemetry-only tag (or removed if no telemetry consumer); the loop never branches on it.
- Delete `planner.plan/rule_plan/official_plan/validate_plan/_PLANNER_SYSTEM/_parse_plan_json`. Keep `_READ_ACTIONS/_WRITE_ACTIONS` only if reused by C1 (prefer `cap_registry`'s split directly).
- `responder.respond` removed from control flow; salvage `context.build_turn_messages` + `_fold_action_results`/`_action_context_str` as helpers for the loop's `build_messages` (tool-result grounding, keeping the BUG-1 char-cap/blob-strip defenses).
- `agent_loop.py` state machine superseded by `tool_loop.py` (delete or thin to shared stop-reason constants).

## Data flow (one chat turn)

1. `_run_turn`: resolve provider_config (single decrypt), mint runtime_token, build `TurnMetrics`, pin `expected_generation = db.get_runtime_generation(user_id)`.
2. `run_tool_loop`: build messages (summary + tail + folded user inputs + prior tool_results) → `chat_completion_async(tools=CATALOG)` → `tm.add_call(usage)`.
3. Classify tool_calls: reads parallel-inline (results fed back, provenance-tagged); writes → provenance-gate → `enqueue_effect` (queued result); `reply{text}` → enqueue reply effect + immediate drain (bubble appears).
4. Fold newly-visible user messages (seq cursor) before the next call; continue. Bounded at 6 calls.
5. Empty tool_calls → plain text = final reply → enqueue reply effect + drain.
6. Terminal: `finish_chat_job`, advance `last_replied`/cursor (as effects), final `apply_pending_effects` drain, `tm.flush(failed=False, status="ok")`.

## Error handling

- **broken tool args** (`args_ok=False`) → error `ToolResult`; the model may correct once this turn. If the model repeats broken args or a relay tool returns 400/422, do exactly ONE same-turn fallback: retry the provider call with `tools` omitted (forcing plain text) → that text becomes the reply. No tier is persisted (nothing written to the user's profile/mode).
- **empty reply** on a terminal round → no-filler: treat as a failed turn (`tm.flush(failed=True, …)` + terminal error status), never write a placeholder bubble.
- **generation advanced mid-turn** (cutover): effects pinned to the old generation are discarded by the outbox fence (PR A); the turn's writes/replies for the stale generation never apply — no cross-generation contamination.
- **mid-turn crash**: re-drive re-enqueues the same `effect_id`s; `apply_pending_effects` + `effect_sink_claim` make it exactly-once (PR A) — no duplicate bubble/write.

## Testing — PR C's P0 subset + acceptance

- **No dispatch tiering:** a guard test asserts V2 production has no `is_official → rule_plan/official_plan` behavior branch (grep-style AST/import guard, like `test_v2_dependency_direction.py`; `planner.plan` gone).
- **4 providers, live loop:** each returns 2 tool_calls in one round and receives 2 results by `call_id` (loop layer over PR B's codec).
- **P0 — weak model plain text:** a model returning plain text with no tool_calls → exactly 1 provider call, exactly 1 bubble, no responder call.
- **P0 — `reply(我看看哈)` + web_search:** the intermediate reply bubble is written+visible BEFORE the web_search round completes; the next round continues; a turn retry re-enqueues the same effect_ids → no duplicate bubble (exactly-once via PR A).
- **P0 — mid-turn fold:** user B's message arrives while round-1 tool is running → round-2's prompt contains A + B + round-1 tool results; no ~100ms debounce; no loop restart (round state/tool_results preserved).
- **P0 — malicious page write refusal:** a turn whose only provenance is an `external` web_fetch result emits a `memory_write`/`identity_patch`/`schedule_wake` tool_call → the dispatcher deterministically refuses (refusal ToolResult, no effect enqueued, no durable write).
- **Reads-parallel / writes-ordered / call_id integrity:** two read tool_calls dispatch concurrently; two write tool_calls serialize in order; every `call_id` round-trips.

## Out of scope (PR D)

- Turn pool crash domain / kill switch / hard-timeout capacity=0 + restart.
- Summary coverage + retention/R2 GC coverage invariant + compaction CAS-loss retry/requeue.
- `extraction`/`compaction` lanes stay on their current direct single-call paths (not migrated to the loop).

## Interfaces PR D consumes from PR C

- `run_tool_loop(...)` as the single foreground turn entry (D wraps it in the killable crash-domain child).
- The effect-producer call sites (D's kill switch fences active writes at the outbox boundary).
- The seq-cursor per-round fold (D's summary-coverage invariant reuses the same cursor).
