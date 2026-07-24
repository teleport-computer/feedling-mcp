# Hosted Runtime V2 — PR A: Control Plane / Effect Foundation (Design)

> Sub-project A of @sxysun's next-round Hosted Runtime V2 directive. Formalized
> 2026-07-12 on `feat/hosted-runtime-v2` @ 172b4ab (post PR #70 explicit-enrollment
> baseline). B/C/D depend on this. **Not to be deployed / no user flipped until the
> 7 P0 fault injections are green.**

## Goal

Build the correctness foundation the rest of Hosted Runtime V2 stands on: a
**generation-fenced, idempotent effect outbox**, a **resident → draining → V2
cutover state machine** with a **monotonic per-user runtime generation**, and a
**stable per-user sequence cursor**. Every user-visible or durable side effect
(reply, status, cursor advance, follow-up job, memory write, identity patch,
schedule change) is written through the outbox and applies **exactly once** and
**never across a runtime-generation boundary** (no ABA contamination).

## Architecture

One unifying abstraction: the **generation-fenced effect outbox**.

A user oscillates between the resident CLI runtime and the V2 worker pool. While a
V2 worker is mid-turn for a user, control can move away and back (resident → V2 →
resident → V2). If that worker's late write lands, it contaminates a runtime era it
no longer owns — the **ABA problem**. The fix, and everything PR A builds, is:

1. A monotonic per-user **`runtime_generation`** that increments on every cutover.
   It is the fence value. Never reused.
2. A **`hosted_runtime_state`** machine `resident → draining → v2` (and back), where
   `draining` is the barrier that lets the outgoing runtime quiesce before the new
   generation is authoritative.
3. Every `agent_job` **pins** the `runtime_generation` observed when it is created.
4. Every side effect is written to a **`v2_effect_outbox`** row carrying that pinned
   generation and a **deterministic `effect_id`**. Applying an effect is a single
   transaction: *apply iff `pinned_generation == user's current generation`*, else
   **deterministically discard**. A `UNIQUE(effect_id)` constraint makes retries
   no-ops (exactly-once).
5. Reads (cursor, summary coverage, cutover decisions) anchor on the **stable
   per-user `seq`** already present on `chat_messages` — never on wall-clock `ts`,
   which is not unique under load (5000+ messages can share a timestamp).

B, C, and D never write side effects directly; they enqueue outbox rows and inherit
exactly-once + no-cross-generation-contamination for free.

## Components

Each is a focused unit with a narrow interface; a caller can use it without reading
its internals.

### A1 — Stable sequence cursor (`v2/cursor.py`, `db.py`)

`chat_messages` already carries a monotonic per-user `seq` (`chat_user_seq_idx`,
`ORDER BY seq ASC`). PR A promotes `seq` to the single canonical cursor and retires
`cursor_ts`.

- **Consumes:** `chat_messages(user_id, seq, msg_id, ts, doc)`.
- **Produces:**
  - `db.chat_max_seq(user_id) -> int` — highest committed seq (0 if none).
  - `db.chat_messages_after_seq(user_id, after_seq, *, limit) -> list[row]` —
    ordered by `seq ASC`, the only ordering used anywhere downstream.
  - `cursor.CursorState` = `{last_seq: int}` persisted per user in the
    `model_api_runtime` profile (`v2_reply_cursor_seq`), replacing any `*_ts`
    cursor. Advancing the cursor is an outbox effect (`effect_type='cursor'`), so it
    is generation-fenced and idempotent like every other effect.
- **Migration:** verify `seq` is dense/monotonic per user; if any legacy rows lack a
  `seq`, backfill deterministically by `(ts ASC, msg_id ASC)`. Any code path reading
  `cursor_ts` is repointed to `seq`; grep-guarded by a test that fails if `cursor_ts`
  reappears in a read path.

### A2 — Runtime generation + cutover state machine (`v2/cutover.py`, `db.py`)

Extends the existing `hosted_runtime_mode` (`resident_cli | db_action_v2`) with a
`draining` transitional state and a monotonic generation counter, stored on the
`model_api_runtime` profile so it lives beside `hosted_runtime_mode` and is read the
same way (`effective_hosted_runtime_mode`, PR #70).

- **State:** `hosted_runtime_state ∈ {resident, draining, v2}`. `runtime_generation:
  BIGINT` (starts at 1). The user-facing `hosted_runtime_mode` (`resident_cli` vs
  `db_action_v2`) remains the *intent*; `hosted_runtime_state` is the *live* position
  in a cutover.
- **Transitions (each bumps `runtime_generation` by exactly 1, atomically via a
  single `UPDATE ... SET runtime_generation = runtime_generation + 1` guarded on the
  from-state):**
  - `resident → draining`: intent flipped to `db_action_v2`; resident consumer told
    to finish its in-flight turn and stop claiming.
  - `draining → v2`: resident consumer confirmed quiesced (lease released / no
    in-flight turn); V2 becomes authoritative.
  - `v2 → draining → resident`: the reverse, for rollback / opt-out.
- **Produces:**
  - `db.get_runtime_generation(user_id) -> int`
  - `db.advance_runtime_state(user_id, *, from_state, to_state) -> int | None` —
    CAS on `hosted_runtime_state`; returns the new generation, or `None` if the
    from-state no longer holds (lost race). Callers must treat `None` as "someone
    else moved the machine; re-read and reconcile," never as success.
- **ABA guarantee:** because generation only ever increases and is bumped on every
  transition, a worker that pinned generation *g* can never see its effects applied
  after the user has moved to *g+1* — even if the user returns to the "same" v2 state
  (that return is a *different, higher* generation).

### A3 — Job pins expected generation (`v2/jobs_store.py`, `db.py`)

`agent_jobs` gains `expected_runtime_generation BIGINT`. It is stamped at **enqueue**
time (the generation authoritative when the send/wake decided to route to V2) and is
immutable for the job's life. It is distinct from the existing `input_generation`
(which counts coalesced new-input arrivals within a turn — unchanged).

- **Produces:** `jobs_store.enqueue_job(..., expected_generation=...)` stamps it;
  `claim_next_job` returns it; workers thread it into every outbox write.
- **Claim-time guard:** `claim_next_job` additionally refuses (or immediately
  completes as `superseded`) a job whose `expected_runtime_generation` is already
  behind `get_runtime_generation(user_id)` — a cheap early-out so a stale job never
  even starts a turn. (Defense in depth; the outbox fence is the authoritative one.)

### A4 — Generation-fenced effect outbox (`v2/effect_outbox.py`, migration)

The core. One table, one apply primitive, seven effect types.

```
CREATE TABLE v2_effect_outbox (
  effect_id        TEXT PRIMARY KEY,          -- deterministic (A5)
  user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  job_id           BIGINT,                    -- source job (NULL for control-plane effects)
  effect_type      TEXT NOT NULL,             -- reply|status|cursor|job|memory|identity|schedule
  expected_generation BIGINT NOT NULL,        -- pinned from the job (A3)
  payload          JSONB NOT NULL,            -- effect-type-specific
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|discarded
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at       TIMESTAMPTZ,
  attempt_count    INT NOT NULL DEFAULT 0,
  last_error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX v2_effect_outbox_pending ON v2_effect_outbox (user_id, created_at)
  WHERE status = 'pending';
```

- **`enqueue_effect(effect_id, user_id, job_id, effect_type, expected_generation,
  payload)`** — INSERT ... ON CONFLICT (effect_id) DO NOTHING. The conflict path is
  the idempotency guarantee: re-enqueuing the same logical effect is a no-op.
- **`apply_pending_effects(user_id)`** — for each pending row, in one transaction:
  ```
  SELECT runtime_generation  -- current, FOR UPDATE on the profile row
  IF row.expected_generation == current:
      dispatch(effect_type, payload)   -- the real durable write (A6)
      UPDATE ... SET status='applied', applied_at=now()
  ELSE:
      UPDATE ... SET status='discarded'   -- deterministic, silent, terminal
  ```
  Applier is idempotent and re-runnable: a crash between `dispatch` and the status
  update is recovered because `dispatch` itself is keyed by `effect_id` at the sink
  (the reply/status/etc. write carries `effect_id` and is unique-constrained too), so
  a replay re-dispatches harmlessly and then marks applied.
- **Who runs the applier:** the worker applies its own turn's effects at end-of-turn;
  a background sweeper (D's crash-domain concern, wired in D) re-applies any pending
  rows an interrupted worker left behind. PR A ships the applier + a minimal
  end-of-turn call; the resilient sweeper loop is D.

### A5 — Deterministic effect_id (`v2/effect_id.py`)

`effect_id` must be identical across retries of the *same logical effect* and
distinct across different effects, WITHOUT randomness (retries would otherwise
double-write).

- `effect_id = f"{job_id}:{effect_type}:{ordinal}"` where `ordinal` is a
  deterministic per-(job, effect_type) counter assigned in the turn's execution order
  (the loop in C emits effects in a fixed order; the ordinal is that order). For
  control-plane effects with no job (e.g. a cutover-driven cursor advance),
  `effect_id = f"gen{generation}:{effect_type}:{seq_or_key}"`.
- Pure function, no I/O, exhaustively unit-tested. This is the linchpin of
  exactly-once; it gets its own property test (same inputs → same id; different
  effect → different id).

### A6 — Effect dispatch sinks (`v2/effect_sinks.py`, assembly in `serve_worker.py`)

`dispatch(effect_type, payload)` routes an *applied* effect to its real durable
write. Each sink is itself keyed by `effect_id` (unique) so the apply/dispatch pair
is crash-safe:

| effect_type | sink |
|---|---|
| `reply`     | write model-authored chat bubble (the only bubble writer) |
| `status`    | agent_status_events append |
| `cursor`    | advance `v2_reply_cursor_seq` |
| `job`       | enqueue a follow-up `agent_job` (fenced: only if generation still current) |
| `memory`    | capabilities memory_write |
| `identity`  | capabilities identity_patch |
| `schedule`  | wake schedule upsert |

Sinks live in the assembly layer (may touch `hosted`/`capabilities`); the outbox
core (`effect_outbox.py`, `effect_id.py`) stays pure and dependency-direction-clean.

**Amendment (post-review hardening):** the mechanism is **claim-release-on-failure**,
not bare claim-then-write. `db.effect_sink_claim` commits on its own connection
*before* the sink's durable write runs; a whole-branch review found that if the
write itself then raised, the applier's transaction rolled back (leaving the
outbox row `pending`, correctly triggering a replay) but the claim had already
committed — so the replay's `effect_sink_claim` returned `False` and the sink
no-op'd, permanently losing the effect while the outbox eventually looked
`applied`-adjacent (stuck `pending` forever, silently never delivered).
Fixed by wrapping every sink's write in `claim -> try: write / except: release
+ re-raise` (`db.effect_sink_release`, `backend/db.py`), so a write failure
un-claims and a replay redoes the write instead of skipping it. `_sink_job`'s
generation-fence early-`return` is exempt — that skip is intentional (the
effect legitimately consumed its claim) and must NOT be re-driven. Also fixed
in the same pass: `_sink_cursor` now uses `db.patch_blob_strict` (raising)
instead of `db.patch_blob` (best-effort, swallows exceptions and returns
`None`), so a cursor write failure actually reaches the claim-release wrapper
instead of failing silently. Residual gap: a hard process crash in the
microsecond window between the claim commit and the write starting still
orphans the claim (no exception fires to trigger a release) — narrow enough
that it's deferred to a PR D effect-sweeper rather than closed here; see
`db.effect_sink_claim`'s docstring. Covered by
`tests/test_v2_p0_exactly_once.py::test_write_error_after_claim_releases_and_replay_completes_real_reply_sink`,
which drives the real `_sink_reply`/`build_production_effect_dispatch` path
(not the pure test double) with the underlying writer raising once then
succeeding.

### A7 — Transactional send + enqueue (`hosted/chat_send_core.py`, `db.py`)

Today chat/send writes the user message and enqueues the job as two steps; a crash
between them orphans one. PR A makes them atomic.

- **Design:** write the encrypted user message row (`chat_messages`) and INSERT the
  `agent_job` (with `expected_runtime_generation` stamped from A2) in **one DB
  transaction**. Both are Postgres, so a single transaction is the simplest correct
  option — no distributed outbox needed here.
- **Reconciliation backstop:** a periodic check finds committed user messages whose
  `seq > last enqueued seq` for a v2 user and enqueues a catch-up chat job
  (idempotent via single-flight). This covers the theoretical gap where the message
  commits but the process dies before the transaction's job insert is visible to the
  worker bus — belt to the transaction's braces.

## Data flow (one v2 chat turn)

1. **send** (A7): user message + `agent_job{expected_generation=g}` committed atomically.
2. **claim** (A3): worker claims the job, sees `expected_generation=g`; early-out if
   `g < current`.
3. **turn** (C, later): loop produces N effects; each is `enqueue_effect(effect_id,
   …, expected_generation=g, …)` — buffered in the outbox, nothing durable yet.
4. **apply** (A4): end of turn, `apply_pending_effects(user)`: for each, `g ==
   current ? dispatch+applied : discarded`.
5. If the user was cut over mid-turn (`current` is now `g+1`), **all** of this turn's
   effects discard deterministically — no half-written reply, no stale memory.

## Error handling

- **Lost cutover CAS** (`advance_runtime_state → None`): re-read state, do not assume
  success; the transition is retried or abandoned by the caller (never silently
  treated as done).
- **Apply crash mid-dispatch:** recovered by re-running the applier; sinks are
  `effect_id`-unique so re-dispatch is a no-op, then status flips to applied.
- **Effect whose generation moved:** `discarded`, terminal, silent (no user-visible
  error chip — matches the no-filler invariant).
- **Send transaction failure:** neither message nor job commits; client retries the
  send (the send itself is not yet idempotent — noted as a B/C follow-up, out of PR A
  scope, but the reconciliation backstop prevents a committed-message/lost-job orphan).

## Testing — the PR A subset of the 7 P0 fault injections

The lead's acceptance list; PR A owns these three, written as deterministic
fault-injection tests (crash/interleave points forced, not timing-dependent):

1. **Exactly-once across a durable-effect boundary:** kill the process at each
   apply/dispatch/status boundary; after recovery there is exactly one reply / one of
   each effect. (effect_id uniqueness + applier idempotency.)
2. **No cross-generation contamination (ABA):** worker pins `g`; force a
   `draining→v2` cutover to `g+1` mid-turn; assert every one of the turn's effects is
   `discarded` and nothing durable was written for `g`.
3. **History integrity under identical timestamps:** 5000+ messages with an identical
   `ts`; assert cursor advance, ordering, and summary-coverage all use `seq` and lose
   nothing / mis-order nothing. (The other four P0s — pool watchdog, compaction
   CAS/retention — are D's; the tool-loop ones are C's.)

Plus unit tests: `effect_id` purity/property test; generation monotonicity CAS test;
outbox apply-vs-discard truth table; send+enqueue atomicity (crash between = neither).

## Out of scope (later PRs)

- Normalized provider API, tool encode/decode, usage normalization, whole-turn
  metric (**PR B**).
- The unified provider-native tool loop, reply-as-tool, message folding, removing
  `is_official`/`rule_plan` dispatch (**PR C**). PR A ships the outbox the loop will
  write to, not the loop.
- The kill/restart turn-pool crash domain, hard-timeout watchdog, live kill switch,
  compaction CAS-retry, retention/R2 GC coverage (**PR D**). PR A ships the applier;
  D ships the resilient sweeper + pool safety around it.
- Making `chat/send` itself idempotent (client-side dedup key) — B/C follow-up.

## Interfaces B/C/D consume from PR A

- `cutover.advance_runtime_state`, `db.get_runtime_generation` — B/C/D read generation.
- `jobs_store.enqueue_job(..., expected_generation)` — B/C enqueue fenced jobs.
- `effect_outbox.enqueue_effect(...)` + `effect_id.derive(...)` — **C writes every
  side effect here** instead of touching sinks directly.
- `effect_outbox.apply_pending_effects` — D's sweeper wraps this in the resilient loop.
- `db.chat_messages_after_seq`, `db.chat_max_seq`, `v2_reply_cursor_seq` — C/D read
  history and advance the cursor by seq.
