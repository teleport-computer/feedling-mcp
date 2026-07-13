# Hosted Runtime V2 — PR D: Pool / History Safety — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the V2 turn pool a killable/restartable child-process crash domain with a hard-timeout watchdog + health-reflecting capacity + a live kill switch (turns halt, Genesis unaffected), and close the history-integrity gaps (seq cursor, prompt-coverage invariant via synchronous catch-up compaction, coverage-gated GC, compaction CAS-loss requeue, reconcile sweeper).

**Architecture:** Split `serve_worker._serve` into a parent supervisor (heartbeat/reaper/scheduler/Genesis/watchdog/kill-switch-poll/reconcile-sweeper) + a killable turn-child subprocess running `run_worker_loop`'s N slots. History safety wires the (already-built) PR A seq cursor + effect outbox into production and gates deletion/coverage on the summary watermark.

**Tech Stack:** Python asyncio + `multiprocessing`/`subprocess`, psycopg/Postgres, alembic; reuses PR A `effect_outbox`/`effect_id`/`cursor`/`reconcile_unenqueued_v2_messages`, PR C `run_tool_loop`, `reaper.py`, `compaction.py`.

## Global Constraints

- **Crash domain = child PROCESS:** turn slots run in a killable/restartable turn-child subprocess; the parent keeps heartbeat/reaper/scheduler/Genesis-thread/watchdog. Watchdog SIGKILLs a wedged child → capacity=0 → respawn.
- **Prompt invariant = synchronous catch-up compaction:** before assembling a turn's prompt, if `watermark_seq < tail_start_seq - 1` (a gap), run an inline catch-up compaction to cover it, then re-read. Post-assembly hard-assert every message with `seq > watermark_seq` is in the tail.
- **Genesis is NEVER halted/killed:** it runs on a parent thread with a separate `genesis_import_jobs` table + `kind='genesis'` heartbeat + separate token. The kill switch and the child-kill must not touch it.
- **Kill switch is fail-CLOSED at admission** (503 `turns_halted`), stops claim, fences active writes; live-flippable without redeploy.
- **Retention/GC only deletes covered content:** the chat trim delete boundary clamps to `min(count-cutoff, watermark_seq)` — never delete a row with `seq > watermark_seq`.
- **Compaction CAS-loss requeues** (never silent-abandon); reconcile sweeper runs in the parent.
- **Exactly-once on kill** leans on PR A's effect_id-idempotent + generation-fenced outbox — do NOT add new outbox mechanics; place the kill/restart boundary so recovery re-drives through `apply_pending_effects`.
- **NO-COMMIT:** leave every change in the working tree; never `git commit`/`git add`/`git stash`/`git checkout --`/`git reset`/`git clean`. (Template `git add`/`commit` steps → "leave in working tree.")
- **Postgres** `127.0.0.1:55432`. Full suite `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`; baseline = 8 pre-existing failures.

## Reused interfaces (grounding, verified)

- `serve_worker._serve` (serve_worker.py:1321-1376): `asyncio.gather` of `run_worker_loop` + `_reaper_loop` + `_heartbeat_loop` + `_scheduler_loop`; Genesis on a thread (`_start_genesis_thread`, :1268). `main()` (:1379) does `db.init_schema` + `wire_assembly` + `asyncio.run(_serve(...))`.
- `_heartbeat_loop` (:1186) UPSERTs `jobs_store.record_worker_heartbeat(worker_id, capacity=v2_worker.MAX_WORKERS, kind='turn')` — the CONSTANT capacity to fix.
- `v2_worker.run_worker_loop(worker_id, *, max_workers, poll_interval, stop_event, deps, wake_event)` (:1521) → N `_slot_loop`. `MAX_WORKERS`=4.
- `db._chat_insert_on_cursor(cur, user_id, msg_id, ts, doc, max_messages, *, trimmed_docs_out=None) -> seq` (db.py:2078) — the trim `DELETE ... WHERE seq < (newest max_messages)`.
- `worker._run_compaction` (worker.py:616-694): CAS via `jobs_store.upsert_summary_row_cas`; on loss (:680) `mark_failed("summary_cas_lost")` + abandon; success path (:670) re-enqueues `compaction_catchup`.
- `db.reconcile_unenqueued_v2_messages() -> int` (db.py:2261, unwired). `cursor.load_seq(store)` / `cursor.advance_effect(*, job_id, ordinal, generation, new_seq)` (cursor.py, unused). `db.chat_max_seq`/`chat_messages_after_seq`.
- `chat_send_core.py:96-135` admission gate. `jobs_store.claim_next_job` (jobs_store.py:189). PR A `effect_outbox.enqueue_effect`/`apply_pending_effects`.

---

## Half A — Pool safety

### Task 1: D4 — Live kill switch (`kill_switch.py` + migration + read points)

**Files:**
- Create: `backend/model_api_runtime/v2/kill_switch.py`, `backend/alembic/versions/0030_v2_runtime_control.py`
- Modify: `backend/hosted/chat_send_core.py` (admission read, fail-closed), `backend/model_api_runtime/v2/worker.py` (`_slot_loop` claim gate + the write-fence closures)
- Test: `tests/test_v2_kill_switch.py`

**Interfaces:**
- Produces: `kill_switch.turns_halted() -> bool` (cached ~2s), `kill_switch.set_turns_halted(halted: bool) -> None`. Backed by `v2_runtime_control(id INT PK DEFAULT 1, turns_halted BOOL NOT NULL DEFAULT false, updated_at)`.

- [ ] **Step 1: Migration `0030_v2_runtime_control.py`** (down_revision `0029_v2_turn_metrics_whole_turn`):
```python
from alembic import op
revision = "0030_v2_runtime_control"; down_revision = "0029_v2_turn_metrics_whole_turn"
branch_labels = None; depends_on = None
def upgrade():
    op.execute("CREATE TABLE IF NOT EXISTS v2_runtime_control ("
               "id INT PRIMARY KEY DEFAULT 1, turns_halted BOOLEAN NOT NULL DEFAULT false, "
               "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), CHECK (id=1))")
    op.execute("INSERT INTO v2_runtime_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
def downgrade():
    op.execute("DROP TABLE IF EXISTS v2_runtime_control")
```
(Also mirror the CREATE + seed row into `db.init_schema` since the test DB is built by init_schema, not alembic — verify where init_schema builds tables and add it.)

- [ ] **Step 2: Failing test** `tests/test_v2_kill_switch.py` — `turns_halted()` default False; `set_turns_halted(True)` → `turns_halted()` True (bypass the cache in the test via a `_cache_ttl=0`/`_invalidate` hook or sleep past TTL); back to False resumes.

- [ ] **Step 3: Implement `kill_switch.py`** — `turns_halted()` reads `SELECT turns_halted FROM v2_runtime_control WHERE id=1` with a small module-level TTL cache (`(value, fetched_at)`; refetch when older than `_CACHE_TTL_SEC=2`); `set_turns_halted` UPDATEs + invalidates the cache. Fail-safe: on DB error, `turns_halted()` returns False (don't halt the pool because the control read failed) — EXCEPT the admission gate reads it fail-CLOSED via a separate `turns_halted(default_on_error=True)` param so a control-plane outage doesn't admit into a halted pool. Keep it dependency-clean (imports `db` only).

- [ ] **Step 4: Wire read points:**
  - `chat_send_core.py` (next to the `workers_alive` gate ~:105): `if kill_switch.turns_halted(default_on_error=True): return {"error":"turns_halted"}, 503` (fail-closed, BEFORE the enclave decrypt).
  - `worker._slot_loop` (before `claim_next_job`): `if kill_switch.turns_halted(): <idle this iteration>` (claim nothing while halted).
  - The write-fence closures (`_before_write`/`_fence_wake_effect`): add `if kill_switch.turns_halted(): raise <a fence error>` so active writes are fenced (turn fails cleanly / effect stays pending).
  - Do NOT touch any Genesis path.

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_kill_switch.py tests/test_v2_dependency_direction.py -q` → PASS; single head `0030`. Leave in working tree.

---

### Task 2: D1 — Turn-child subprocess + parent supervisor (structural split)

**Files:**
- Create: `backend/model_api_runtime/v2/turn_child.py`, `backend/model_api_runtime/v2/child_supervisor.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (`_serve` no longer `create_task`s `run_worker_loop`; instead the supervisor spawns the child)
- Test: `tests/test_v2_child_supervisor.py` (unit, with a FAKE child target — no real turn work)

**Interfaces:**
- Produces:
  - `turn_child.main()` — the child entrypoint: re-init DB pool + `wire_assembly()` in the fresh process, then run `run_worker_loop`, emitting a **progress heartbeat** — each slot writes `progress_pipe.send(("progress", slot_id, monotonic_ish))` on every claim/round boundary (a `multiprocessing.Pipe` end passed in), OR (simpler, DB-mediated) bumps a `last_progress_at` on its heartbeat row. Choose the **pipe** for immediate wedge detection; pass the child a `multiprocessing.Connection` write-end.
  - `child_supervisor.ChildSupervisor(spawn_target, *, liveness_timeout_sec)` with `start()`, `poll_liveness() -> {"alive": bool, "last_progress_age_sec": float}`, `kill_and_respawn()` (SIGKILL via `proc.kill()`, `proc.join(timeout)`, then `start()` a fresh proc), `stop()`. `spawn_target` defaults to `turn_child.main` but is injectable so the test passes a fake target.

- [ ] **Step 1: Failing test** `tests/test_v2_child_supervisor.py` (NO real turns — inject fake targets):
  - `start()` spawns a child running a fake target that sends periodic progress → `poll_liveness()["alive"]` True, `last_progress_age_sec` small.
  - a fake target that sends ONE progress then sleeps forever (wedge) → after `liveness_timeout_sec`, `poll_liveness()["alive"]` still True (process alive) but `last_progress_age_sec > liveness_timeout_sec` (wedged).
  - `kill_and_respawn()` on a wedged child → the old PID is gone (`proc.is_alive()` False for the old handle) and a fresh child is running with a new PID. Use a fake target that writes its PID to a shared value so the test can assert the PID changed.
  - `stop()` cleanly terminates the child.
  Use `multiprocessing.get_context("spawn")` so the test target is a module-level function (picklable). Keep the fake target in the test module (module-level def).

- [ ] **Step 2-3:** implement `child_supervisor.py` (spawn via `mp.get_context("spawn").Process(target=..., args=(conn,))`; a background reader drains the progress pipe into `last_progress_at`; `kill_and_respawn` = `proc.kill()` + `join` + fresh spawn) and `turn_child.py` (re-init + run_worker_loop + progress emission). Keep `serve_worker._serve` responsible for constructing the supervisor and NOT `create_task`ing `run_worker_loop` anymore — the child runs it.

- [ ] **Step 4:** `serve_worker._serve` change: replace the `run_worker_loop` task in the `gather` with `supervisor = child_supervisor.ChildSupervisor(turn_child.main, ...)` + `supervisor.start()`; keep `_reaper_loop`/`_heartbeat_loop`/`_scheduler_loop`/Genesis in the PARENT. On `stop_event`, `supervisor.stop()`. (The watchdog that reads `poll_liveness` + calls `kill_and_respawn` is Task 3.)

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_child_supervisor.py -q` → PASS. Then a process-level smoke (marked, not in the fast unit path if slow). Leave in working tree.

---

### Task 3: D2 — Watchdog + hard-timeout (`watchdog.py` + parent loop)

**Files:**
- Create: `backend/model_api_runtime/v2/watchdog.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (add `_watchdog_loop` to the parent `gather`)
- Test: `tests/test_v2_watchdog.py`

**Interfaces:**
- Consumes: `child_supervisor.ChildSupervisor.poll_liveness`, `jobs_store.record_worker_heartbeat` (capacity=0 on kill).
- Produces: `watchdog.should_kill(liveness: dict, *, turn_hard_timeout_sec, child_liveness_timeout_sec, jobs_claimable: bool) -> bool` (pure decision) + `_watchdog_loop(supervisor, worker_id, stop_event, *, interval)` (parent loop: poll → if `should_kill` → write capacity=0, `supervisor.kill_and_respawn()`).

- [ ] **Step 1: Failing test** `tests/test_v2_watchdog.py` (pure `should_kill`): healthy (fresh progress) → False; wedged (`last_progress_age_sec > child_liveness_timeout_sec` AND jobs_claimable) → True; child dead → True; a single turn over `turn_hard_timeout_sec` → True; idle (no progress but no claimable jobs) → False (don't kill an idle-but-healthy child). Plus a loop test with a fake supervisor asserting capacity=0 is written and `kill_and_respawn` is called on the kill decision.

- [ ] **Step 2-4:** implement `should_kill` (pure) + `_watchdog_loop` (interruptible `wait_for(stop_event.wait(), timeout=interval)` like the other loops; on kill: `await asyncio.to_thread(jobs_store.record_worker_heartbeat, worker_id, capacity=0, kind='turn')` THEN `supervisor.kill_and_respawn()`). Add it to `_serve`'s task list. `TURN_HARD_TIMEOUT_SEC` env-configurable (default 180), `CHILD_LIVENESS_TIMEOUT_SEC` default 45.

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_watchdog.py -q` → PASS. Leave in working tree.

---

### Task 4: D3 — Capacity reflects health (`_heartbeat_loop`)

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py` `_heartbeat_loop` (capacity from child health, not the constant).
- Test: `tests/test_v2_capacity_health.py`

**Interfaces:**
- Consumes: `child_supervisor.ChildSupervisor.poll_liveness`.
- Produces: `_heartbeat_loop(worker_id, stop_event, *, supervisor, interval)` — writes `capacity = 0 if not liveness["alive"] or liveness["last_progress_age_sec"] > _CAPACITY_STALE_SEC else v2_worker.MAX_WORKERS`.

- [ ] **Step 1: Failing test** — a fake supervisor whose `poll_liveness` returns alive → heartbeat records capacity=MAX_WORKERS; dead/wedged → capacity=0. (Monkeypatch `jobs_store.record_worker_heartbeat` to capture the capacity arg.)
- [ ] **Step 2-4:** thread `supervisor` into `_heartbeat_loop`, derive capacity. Ensure the watchdog's capacity=0 write and the heartbeat's don't fight (watchdog writes 0 immediately on kill; heartbeat also derives 0 while the child is down — consistent).
- [ ] **Step 5: Run** `python -m pytest tests/test_v2_capacity_health.py -q` → PASS. Leave in working tree.

---

### Task 5: Half-A P0/acceptance tests

**Files:** Create `tests/test_v2_p0_pool_safety.py`.

- [ ] **P0 — all slots stuck → capacity zeroes + child restarts + Genesis unaffected:** with a fake wedged child (progress frozen) + jobs claimable, drive one `_watchdog_loop` iteration → assert capacity=0 recorded AND `kill_and_respawn` issued; assert a `kind='genesis'` heartbeat written independently stays fresh (Genesis untouched).
- [ ] **kill switch:** `set_turns_halted(True)` → `chat_send_core` admission returns 503 `turns_halted`; `_slot_loop` claims nothing (monkeypatch claim_next_job, assert not called while halted); an in-flight write is fenced; a Genesis import path is unaffected (its claim fn still callable). Flip back → resumes.
- [ ] **watchdog hard-timeout:** `should_kill` returns True for a turn exceeding `TURN_HARD_TIMEOUT_SEC`.
- [ ] **Run** `python -m pytest tests/test_v2_p0_pool_safety.py -q` → PASS. Leave in working tree.

---

## Half B — History safety

### Task 6: D8 — Compaction CAS-loss requeue (`_run_compaction`)

**Files:** Modify `backend/model_api_runtime/v2/worker.py` `_run_compaction` (~:680 CAS-loss branch). Test `tests/test_v2_compaction_cas_requeue.py`.

- [ ] **Step 1: Failing test** — drive `_run_compaction` with `upsert_summary_row_cas` monkeypatched to return False (CAS loss) → assert a fresh `compaction_catchup` (or `maintenance`) job is enqueued (single-flight) instead of a silent abandon; the job is still `mark_failed` for THIS attempt but a retry is queued. A NON-CAS failure (provider error) does NOT requeue (only CAS loss retries).
- [ ] **Step 2-4:** in the CAS-loss branch, after `mark_failed(job_id, "summary_cas_lost")`, add `jobs_store.enqueue_job(user_id, "compaction_catchup", reason="cas_lost_retry")` (mirror the success-path catch-up requeue; single-flight prevents storms). Keep `tm.flush(failed=True, ...)`.
- [ ] **Step 5: Run** the test + `tests/test_v2_compaction_integration.py` → PASS. Leave in working tree.

---

### Task 7: D9 — Reconcile sweeper (parent loop)

**Files:** Modify `serve_worker.py` (add `_reconcile_loop` to `_serve`'s parent gather). Test `tests/test_v2_reconcile_sweeper.py`.

- [ ] **Step 1: Failing test** — seed an orphan (v2 user with a committed chat message but no active chat job, like `tests/test_v2_reconcile.py`); run one `_reconcile_loop` iteration → `db.reconcile_unenqueued_v2_messages` was called and a catch-up chat job now exists.
- [ ] **Step 2-4:** add `_reconcile_loop(stop_event, *, interval=_RECONCILE_INTERVAL_SEC)` (interruptible, mirrors `_reaper_loop`; calls `await asyncio.to_thread(db.reconcile_unenqueued_v2_messages)` each tick, swallows per-iteration errors) to the PARENT task list in `_serve`.
- [ ] **Step 5: Run** `python -m pytest tests/test_v2_reconcile_sweeper.py -q` → PASS. Leave in working tree.

---

### Task 8: D7 — GC/retention gate on summary coverage (`_chat_insert_on_cursor` trim)

**Files:** Modify `backend/db.py` `_chat_insert_on_cursor` (the trim DELETE, :2106-2119). Test `tests/test_v2_gc_coverage_gate.py`.

**Interfaces:** the trim delete boundary clamps to the user's summary watermark seq — never delete `seq > watermark_seq`.

- [ ] **Step 1: Failing test** — seed a user, set a summary watermark at a low seq (via the summary row), append > `max_messages` messages so the naive trim would delete rows above the watermark; assert AFTER the insert that NO row with `seq > watermark_seq` was deleted (the row count may exceed max_messages — that's intended until compaction advances the watermark), and that rows at/below the watermark ARE trimmed normally.
- [ ] **Step 2-4:** change the trim DELETE to also bound by the watermark: read the user's `watermark_seq` (from the summary row; translate the summary's `watermark_ts` to a seq via `chat_messages` if the summary still stores ts — OR after Task 9 the summary stores watermark_seq directly) and change the DELETE to `... WHERE user_id=%s AND seq < LEAST((SELECT MIN(seq) FROM (newest max_messages)), COALESCE(<watermark_seq>+1, ...))` — i.e. never delete a row whose seq is beyond the summarized boundary. If there is no summary row yet (watermark unknown), do NOT trim (fail-safe: never delete uncovered history). Keep `trimmed_docs_out` collection for R2 cleanup of the rows actually trimmed.
- [ ] **Step 5: Run** `python -m pytest tests/test_v2_gc_coverage_gate.py tests/test_v2_p0_seq_integrity.py -q` → PASS. Leave in working tree.

---

### Task 9: D5 — seq-cursor migration (reply/cutover/last_replied → seq)

**Files:** Modify `worker.py` (turn read boundary + fold cursor + `last_replied` → seq via `cursor.advance_effect`/`load_seq`), `serve_worker.py` (`_read_summary`/`_read_tail` seq boundary), `compaction` watermark → seq. Test `tests/test_v2_seq_cursor_wired.py`.

**Interfaces:** Consumes `cursor.load_seq(store)`/`cursor.advance_effect(*, job_id, ordinal, generation, new_seq)`, `db.chat_max_seq`/`chat_messages_after_seq`. Produces: production now advances `v2_reply_cursor_seq` via a `cursor` effect at turn end, and the read boundary (`since`) is seq-based.

- [ ] **Step 1: Failing test** — a turn that produces a reply enqueues a `cursor` effect advancing `v2_reply_cursor_seq` to the replied-through seq; the next turn's read boundary uses that seq (not ts); `cursor.load_seq` returns it. Assert two messages with identical ts are both correctly bounded by seq (承 PR A seq-integrity — no boundary skip/dup under identical ts).
- [ ] **Step 2-4:** at turn finalize (chat + wake), enqueue a `cursor` effect (`effect_outbox.enqueue_effect(effect_type="cursor", payload=cursor.advance_effect(...)-derived)`) so `_sink_cursor` advances the seq. Replace `since = last_replied_ts` reads with `cursor.load_seq(store)` (seq) + `db.chat_messages_after_seq`. Migrate compaction's `new_watermark = old[-1]["ts"]` to `old[-1]["seq"]` and store `watermark_seq` on the summary row (add a column via a migration IF the summary row lacks a seq column — check `v2_conversation_summary` schema; add `watermark_seq BIGINT` if needed, else reuse). Keep back-compat: existing users with only a ts watermark get a one-time ts→seq translation.
- [ ] **Step 5: Run** `python -m pytest tests/test_v2_seq_cursor_wired.py tests/test_v2_worker.py tests/test_v2_compaction_integration.py -q` → PASS. Leave in working tree.

---

### Task 10: D6 — Prompt invariant + synchronous catch-up compaction

**Files:** Modify `worker.py` prompt assembly (`process_job`/`_run_wake` after read summary+tail). Test `tests/test_v2_prompt_invariant.py`.

**Interfaces:** Consumes Task 9's `watermark_seq` + `compaction.compact`. Produces: before the loop, a `_ensure_prompt_coverage(...)` that runs a synchronous catch-up compaction when `watermark_seq < tail_start_seq - 1`, then re-reads; a post-assembly assertion that every message with `seq > watermark_seq` is in the tail.

- [ ] **Step 1: Failing test** — seed a user with a summary watermark far behind + more than `_TAIL_HARD_CAP` messages after it (a gap). Run the coverage check → a synchronous catch-up compaction advances the watermark to close the gap; assert post-assembly that every message with `seq > watermark_seq` is in the assembled tail (no message in the gap between watermark and tail). Assert the compaction ran INLINE (not just enqueued).
- [ ] **Step 2-4:** implement `_ensure_prompt_coverage(store, user_id, *, watermark_seq, tail_start_seq, ...)` — if `watermark_seq < tail_start_seq - 1`, read the gap batch (oldest-first from watermark, like `_read_compaction_tail`), `compaction.compact` it, CAS-write the advanced summary (retry-on-CAS-loss inline a bounded number of times), re-read summary+tail. Add the coverage assertion. Wire it into `process_job`/`_run_wake` right after reading summary+tail, before `run_tool_loop`.
- [ ] **Step 5: Run** `python -m pytest tests/test_v2_prompt_invariant.py tests/test_v2_worker.py -q` → PASS. Leave in working tree.

---

### Task 11: Half-B P0/acceptance tests + kill-at-boundary exactly-once

**Files:** Create `tests/test_v2_p0_history_safety.py`.

- [ ] **P0 — 5000+ identical-ts + compaction CAS race → no loss, no wrong deletion:** append 5000+ messages with identical ts while keeping compaction behind / CAS-racing; assert (a) the GC trim never deleted a row with `seq > watermark_seq` (Task 8), (b) after catch-up every message is summarized or in the tail (Task 10), (c) a CAS-lost compaction requeued (Task 6). Extends `test_v2_p0_seq_integrity`.
- [ ] **P0 — kill at every durable-effect boundary → exactly one reply/effect:** parametrize a simulated kill (interrupt) before/after enqueue and before/after the status flip; assert after a re-drive (re-claim the same job) there is exactly one reply bubble + one of each effect (leans on PR A's effect_id fence + `apply_pending_effects` idempotency; PR D provides the re-claim path). This is the crash-domain recovery invariant.
- [ ] **Full suite** `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py` → 8-baseline, zero new, single alembic head (0030, or 0031 if Task 9 added a watermark_seq migration). Leave in working tree; do NOT commit.

---

## Self-Review

- **Spec coverage:** D1→T2; D2→T3; D3→T4; D4→T1; D5→T9; D6→T10; D7→T8; D8→T6; D9→T7. Pool P0s→T5; history/kill-boundary P0s→T11. All nine components + the P0 subset covered.
- **Ordering/deps:** Half A: 1(kill switch, independent)→2(child split)→3(watchdog, needs supervisor)→4(capacity, needs supervisor)→5(pool P0). Half B: 6(CAS requeue, independent)→7(reconcile, independent)→8(GC gate)→9(seq cursor, provides watermark_seq)→10(prompt invariant, needs 9)→11(history P0). Half A and Half B are largely independent; Half B tasks 6/7/8 don't depend on the child split.
- **Type consistency:** `kill_switch.turns_halted(default_on_error=False)->bool` T1/T5; `ChildSupervisor.poll_liveness()->{"alive","last_progress_age_sec"}` T2/T3/T4; `watchdog.should_kill(liveness,*,...)->bool` T3/T5; `watermark_seq` T8/T9/T10; `cursor.advance_effect/load_seq` T9/T10.
- **Genesis-safety:** every pool task (T1 kill switch, T2 child split, T3 watchdog) explicitly leaves Genesis in the parent / untouched; T5 asserts `genesis_alive` across a kill.
- **NO-COMMIT:** every task leaves changes in the working tree.
- **Testability of the child split:** T2/T3/T5 use FAKE injected child targets + pure decision functions so the crash-domain logic is unit-tested without real turn work; a process-level smoke is marked separately.
