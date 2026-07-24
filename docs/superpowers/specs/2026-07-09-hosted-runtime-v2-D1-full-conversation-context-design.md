# Hosted Runtime V2 — Full-Conversation Context (condition 2) Design

> **Status:** design, uncommitted, on `feat/hosted-runtime-v2` worktree.
> **Scope:** this is **Step 2 = walkthrough §9 condition 2 ONLY** (full conversation + compaction). Conditions 3 (native agent loop) and §6 (admission ceiling) are **explicitly out** — deferred to later subproject-D work or dropped. See `2026-07-09-hosted-runtime-v2-merge-conditions-backlog.md`.
> **Source of the requirement:** `~/downloads/feedling-runtime-v2-walkthrough.html` §5 ("context & token economics") + §9 condition 2.

## 1. Problem

The V2 turn currently shows the model **only the pending (not-yet-replied) user messages**: `serve_worker._read_messages` reads chat rows *after the last assistant reply*; `coalesce_pending` keeps only `role==user, ts>since`; `responder._build_messages` builds user turns exclusively from those. There is no assistant history, no earlier conversation, no summary. Result: **小克 forgets what you were just talking about** — a hard, user-visible regression versus the resident CLI (which replays the whole transcript via `--resume`). Until this is fixed, V2 chat is worse than what it aims to replace, so it cannot replace it.

**Non-goal (deliberately):** this design does **not** make the turn an agent loop and does **not** add prompt caching. The turn stays `plan → parallel reads → reply`. The only change is **what conversation context reaches the model**, plus a background job that keeps that context bounded. Token-cost optimization (`cache_control`) is a separate later item; correctness of memory is the goal here.

## 2. The context model

Every chat turn assembles the prompt as four layers (Claude-Code-style "summary + verbatim tail"):

```
[persona / identity digest]   — already available (identity capability / runtime_state digest)
[compacted itemized summary]  — NEW: encrypted at rest, decrypted into the prompt
[verbatim recent tail]        — NEW: the last N messages, BOTH roles, chronological, verbatim
   └── the just-sent new messages are simply the newest entries of the tail
```

The model **always sees the entire conversation**: the far past as itemized summary, the near past + present verbatim. This is the whole fix.

**Tail vs. coalesce — one source of prompt context, do not double-render.** The verbatim tail (from `read_tail`) is the *whole* recent window and already **includes the just-sent new messages** as its newest entries — so it is the single conversation source that reaches the prompt. The existing `coalesce_pending` / "messages since last reply" logic is **retained but repurposed**: it is only the turn's *bookkeeping* — which messages triggered this job (for the single-flight cursor, replan/merge decisions, and the "has the user said something new mid-turn" check). Coalesced pending messages are **not** a separate prompt layer and must **not** be appended to the prompt again on top of the tail. `build_turn_messages` therefore takes the tail (not pending); pending stays inside the coalesce/replan path unchanged.

### 2.1 The watermark

A single per-user marker `summary_watermark` (a message timestamp or id) divides the two:

- **summary covers** all history with `ts ≤ watermark` (folded into itemized form).
- **verbatim tail** = messages with `ts > watermark`.

The watermark is **not sensitive** (a timestamp/id). **Resolved at plan time (see §3.2): it is co-located in the same row as the encrypted summary** (a new `v2_conversation_summary` table), not in `runtime_state` — so one read returns both and one write updates both atomically. Only the summary *content* is encrypted; the watermark column is plaintext.

### 2.2 Storage

- **Summary content:** an **envelope-encrypted per-user blob** (reuse `core.envelope`, the same at-rest encryption as chat messages). **Never stored in plaintext `runtime_state`** (walkthrough §5 red line; keeps runtime_state ciphertext-agnostic per the condition-7 seam). Read requires an enclave decrypt (like a chat-message decrypt); write requires an enclave re-encrypt.
- **Watermark + budget bookkeeping:** plaintext `runtime_state` JSONB.
- **Memory cards** (the long-term curated store, `capabilities/memory.*`) are **unchanged and separate** — do not conflate the conversation summary (recency compression) with memory cards (long-term facts).

## 3. Compaction (maintenance-lane job)

When a turn assembles context and finds the tail over budget, it **enqueues a compaction job** (maintenance lane, the user's own BYOK key, off the hot path) and **continues replying without waiting**.

The compaction worker:

```
read:  current encrypted summary (decrypt) + the oldest K messages in (watermark, new_watermark]
call:  user's BYOK LLM (reliable_chat_completion_async from Step 1) → NEW itemized entries for those old messages
merge: APPEND-AND-MERGE the new entries into the existing itemized summary — NEVER a full rewrite
write: re-encrypt the summary + ADVANCE the watermark, atomically
```

After a fold the verbatim tail naturally shortens (the folded messages are now represented by the summary).

**Why append-and-merge, never full rewrite (§5):** (1) repeatedly re-summarizing degrades quality over time ("context collapse"); (2) rewriting the whole summary changes bytes and would break the future prompt-cache prefix. Appending new items keeps both intact. (Caching itself is out of scope, but the summary is authored cache-friendly from day one so the later caching item is a pure add.)

### 3.1 Budgets (env-configurable)

- `FEEDLING_V2_TAIL_BUDGET_MSGS` default **20** — tail over this triggers compaction.
- `FEEDLING_V2_TAIL_KEEP_MSGS` default **10** — compaction folds down to roughly this many verbatim, the rest into the summary.
- (Token-based budgets can replace message-count later; message-count is the shippable first cut.)

### 3.2 Idempotency & atomicity

A reaper re-claim must not double-fold. Compaction is **read-interval → produce-entries → append + advance-watermark, done atomically**; on re-run the watermark has already advanced, so it re-reads from the new position and folds nothing twice.

**Resolved (plan decision):** the encrypted summary, the watermark, and a `version` integer all live in **one row** of a new `v2_conversation_summary(user_id PK, summary_envelope JSONB, watermark_ts, version, updated_at)` table. Envelope **encryption is pure local crypto** (`content_encryption.build_envelope`, X25519+AEAD, in-process — verified; only *decryption* is an enclave HTTP round-trip), so the fully-materialized envelope dict + the new watermark + `version+1` are written as a **single-row compare-and-swap** (`UPDATE … SET … WHERE user_id=%s AND version=%s`; 0 rows affected ⇒ lost the race ⇒ abort this fold, the other writer won). This gives atomicity with no cross-store problem and idempotency via the version+watermark. This supersedes the earlier "watermark in runtime_state" sketch.

## 4. Files & interfaces

All new logic lives in the `v2/` layer and **must not import `hosted`/`agent_runtime`** (AST-guarded by `tests/test_v2_dependency_direction.py`). Hosted/enclave access is **injected via `TurnDeps`** by the assembly layer `serve_worker.py` — the same pattern Step 1/A-B-C already use for `read_messages`/`resolve_provider`/`record_terminal_error`.

**New — `backend/model_api_runtime/v2/context.py` (pure):**
- `build_turn_messages(*, persona: dict, summary: str, tail: list[dict]) -> list[dict]` — assembles the three-layer message list (persona + summary + verbatim tail) for the responder. The tail already includes the just-sent messages; pending is **not** a parameter (see §2, "Tail vs. coalesce"). Pure; no I/O.
- `needs_compaction(tail: list[dict], *, budget: int) -> bool` — pure.

**New — `backend/model_api_runtime/v2/compaction.py`:**
- `async def compact(*, provider_config, current_summary: str, old_messages: list[dict], llm) -> str` — folds `old_messages` into `current_summary` via `llm` (= `provider_client.reliable_chat_completion_async`, user's BYOK key), append-and-merge, returns the new summary text. No hosted/enclave imports; the encrypt/store and watermark advance are done by the caller (worker) via injected deps.

**`TurnDeps` (in `worker.py`) gains (all injected by `serve_worker`, default-None-safe where sensible):**
- `read_summary: Callable[[str], tuple[str, Any]]` — `user_id -> (decrypted_summary, watermark)`. enclave-bound (ENCLAVE_SEMAPHORE).
- `write_summary: Callable[[str, str, Any], None]` — `(user_id, summary, watermark) -> None`; re-encrypt + advance watermark atomically (§3.2). enclave-bound.
- `read_tail: Callable[[str, int], list[dict]]` — `(user_id, window) -> [{id,ts,role,content}]`, most recent `window` messages BOTH roles, chronological, each enclave-decrypted. This is a **new** read used for prompt context; the existing `read_messages` (post-last-assistant user rows) + `coalesce_pending` are **retained** for the coalesce/single-flight bookkeeping (§2 "Tail vs. coalesce") — read_tail does not replace them. enclave-bound.

**`serve_worker.py` (assembly layer — the only place that imports hosted/enclave):** implement + wire the three deps above onto `core.envelope`/`core.enclave`/`core.store` + `runtime_state`.

**`worker.process_job` — dispatch by job kind:**
- `kind == "chat"` (existing): coalesce → prefetch → `read_summary` + `read_tail` → `context.build_turn_messages` → responder (unchanged turn shape). If `needs_compaction(tail)` → enqueue a `compaction` job on the maintenance lane (best-effort, non-blocking).
- `kind == "compaction"` (new): `read_summary` → `read_tail`(the fold window) → `compaction.compact` → `write_summary`. Writes **no chat bubble** (no-filler).

Job kind is a column/field on `agent_jobs` (chat vs compaction); compaction jobs claim on the **maintenance lane** so single-flight `(user_id, lane)` keeps chat and compaction independent and de-dupes concurrent compactions for the same user.

## 5. Invariants (must not regress)

- **BYOK-only:** compaction's LLM call uses the user's injected `provider_config` (Step 1 `reliable_chat_completion_async`); no platform/company LLM key anywhere.
- **no-filler:** the compaction job writes only the summary blob; it never writes an assistant chat bubble. Only the turn's model-authored `final_response` writes a bubble.
- **Dependency direction:** `context.py`/`compaction.py` (v2) import no `hosted`/`agent_runtime`; all hosted/enclave access is injected via `TurnDeps`. `tests/test_v2_dependency_direction.py` stays green.
- **Single-decrypt (provider key):** the turn's provider-key JIT decrypt remains once per job. The summary blob is an **envelope decrypt** (same class as the existing per-message chat decrypt), not a second provider-key decrypt — consistent with the current model. All enclave-bound calls (read_summary/write_summary/read_tail) are wrapped by `ENCLAVE_SEMAPHORE` (spec R3: single-threaded enclave; don't amplify 502s).
- **Ciphertext-agnostic seam (condition 7):** the summary is stored encrypted behind the injected `read_summary`/`write_summary` abstraction; nothing in v2 core assumes the envelope layer exists forever (post-TEE-Postgres it becomes a plaintext store swap behind the same interface).

## 6. Testing

- `context.build_turn_messages` — pure: correct four-layer ordering; empty summary / empty tail / pending-only edge cases; assistant + user roles preserved in the tail.
- `context.needs_compaction` — boundary at budget.
- `compaction.compact` — append-and-merge (existing items preserved, new items appended, NOT a rewrite — assert old items still present); monkeypatched `llm` (no live provider); BYOK config passed through.
- Watermark advance + **idempotency**: a compaction re-run (simulating reaper re-claim) folds nothing twice; watermark monotonic.
- `worker.process_job` dispatch-by-kind: chat job assembles full context (summary+tail+pending reach the responder); compaction job runs compact + write_summary and writes **no** chat bubble.
- Integration (DB, PG :55432): an over-budget chat turn enqueues a compaction job on the maintenance lane; after the compaction job runs, the tail read shrinks and the summary grows; the next chat turn's assembled context contains the summary.
- Dependency-direction guard stays green.

## 7. Out of scope (this Step 2 spec)

- `cache_control` prompt caching (later D item — the summary is authored cache-friendly so it's a pure add).
- Native tool-calling agent loop / walkthrough condition 3 (dropped for the companion product — the deterministic `plan → parallel reads → reply` turn is sufficient; a full agent loop is over-engineering here).
- §6 admission ceiling / backpressure at send.
- Proactive/wake lanes (D3), load test + tokens/turn metric + gated rollout + killing resident processes (D4) — the concurrency payoff lands there, not here.

## 8. Rollout / safety

- All work stays behind the existing `hosted_runtime_mode = db_action_v2` per-user gate — resident CLI users are untouched.
- **No commits / no `git add`** during implementation (user's iron rule; user commits at the end).
- Baseline: full backend suite = 2477 passed / 7 pre-existing failed (debug-trace/memory-capture/verify-ping) after Step 1; this spec's work must add zero new regressions.
