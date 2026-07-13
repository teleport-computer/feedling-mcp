# Hosted Runtime V2 — PR D: Pool / History Safety — Design

**Status:** Approved (design), awaiting spec review → plan
**Depends on:** PR A (effect foundation `d7e19a9`) + PR B (transport) + PR C (unified tool loop) — all merged.
**Directive:** @sxysun 4-PR next round — PR D is the final "pool/history safety" PR. **7 P0 fault injections must be green before any deploy / any internal-user flip.** PR D lands the pool + history P0s, completing the set.

## Goal

Make the V2 turn pool a **killable/restartable crash domain** with a hard-timeout watchdog and health-reflecting capacity, add a **live kill switch** that halts admission/claim + fences active writes but never touches Genesis/import, and close the **history-integrity** gaps: migrate reply/cutover/summary to the stable seq cursor, enforce a prompt invariant (every message is covered by a committed summary OR appears verbatim in the prompt), gate retention/R2 GC on summary coverage, and make compaction CAS-loss retry/requeue instead of silently abandoning.

**Two user-decided mechanisms (locked):**
- **Crash domain = a child PROCESS.** Turn slots move to a killable/restartable turn-child subprocess; the parent supervisor keeps heartbeat/reaper/scheduler/Genesis-thread. The watchdog SIGKILLs a wedged child, writes capacity=0, and respawns it.
- **Prompt invariant = synchronous catch-up compaction.** When messages have accumulated past the summary watermark beyond the tail window, run a synchronous catch-up compaction to cover the gap BEFORE assembling the turn's prompt (bounded prompt, guaranteed coverage).

## Current state (grounding — verified, file:line)

- **Pool is one process, one loop:** `serve_worker._serve`/`main` (serve_worker.py:1321-1387) runs `asyncio.run` of `run_worker_loop` (N=`MAX_WORKERS`=4 `_slot_loop` tasks, worker.py:1521-1558/1452-1518) + `_reaper_loop` + `_heartbeat_loop` + `_scheduler_loop` as sibling asyncio tasks, plus Genesis on a **separate OS thread** (`_start_genesis_thread`, serve_worker.py:1268-1318). Docker `restart: unless-stopped` (deploy/docker-compose.phala.prod.runner.yaml) restarts only on process DEATH, not on wedge.
- **No hard-timeout on a turn:** `_slot_loop`'s `await _run_turn(job, deps)` is never wall-clock-bounded (grep-confirmed). Per-HTTP timeouts exist deep in provider_client/enclave; `_TURN_MAX_LLM_CALLS=6` bounds round COUNT not time. A truly-wedged coroutine occupies its slot forever. `reaper.py` + job-lease TTL (`RUNNING_TTL_SEC`=300) marks the DB job row failed (data-plane) but never touches the physically wedged coroutine (process-plane).
- **Capacity is a constant:** `_heartbeat_loop` (serve_worker.py:1186-1226) writes `capacity=MAX_WORKERS` unconditionally every ~10s; only graceful shutdown writes capacity=0. It never zeroes on wedge.
- **Genesis is already fully separate:** separate `genesis_import_jobs` table + `db.genesis_claim_uploaded_jobs` (not `agent_jobs`/`claim_next_job`), `kind='genesis'` heartbeat (invisible to `workers_alive`/`live_worker_count`), separate `_mint_genesis_token`/`_GENESIS_TOKEN_SCOPE`, its own thread. A kill switch / crash-domain move must simply keep Genesis in the parent and not couple to it.
- **No kill switch** exists. Admission choke point = `chat_send_core.py:96-135` (`workers_alive` gate + admission ceiling, fail-open). Claim choke point = `jobs_store.claim_next_job` (jobs_store.py:189-271). Write fences = the `_before_write`/`_fence_wake_effect` closures in `process_job`/`_run_wake` (worker.py:1194-1246, 792-824) which already check `runtime_mode_enabled` + lease before every write.
- **compaction CAS-loss silently abandons:** `_run_compaction` (worker.py:680-685) on CAS loss calls `jobs_store.mark_failed(job_id, "summary_cas_lost")` and returns "failed" — comment "视为丢弃，不重试、不报错". The success path (:670-676) DOES re-enqueue `compaction_catchup` when the tail is still over threshold; the failure path has NO equivalent requeue.
- **seq cursor built but UNUSED:** `cursor.advance_effect`/`load_seq` (cursor.py) have ZERO production callers; the `_sink_cursor` sink is wired (serve_worker.py:884-898) but nothing produces a cursor effect. Prompt assembly is entirely ts-based: `process_job` reads `since=last_replied_ts` → `read_summary`→`(summary, watermark_ts, version)` → `read_tail(user_id, watermark, _TAIL_HARD_CAP=60)`.
- **Prompt invariant VIOLATED today:** `_read_tail_window` (serve_worker.py:359-417) chat path takes `candidates[-60:]` (newest 60 after watermark). If >60 messages accumulate past the summary watermark (compaction behind/failed/CAS-lost), everything between watermark and `candidates[-61]` is silently dropped — not summarized, not in tail. No invariant enforces coverage today.
- **Retention/GC is coverage-blind (highest severity):** every chat insert `db._chat_insert_on_cursor` (db.py:2078-2119) runs a trim `DELETE FROM chat_messages WHERE seq < (newest max_messages by seq)` — hard-deletes rows beyond the newest `MAX_CHAT_MESSAGES=5000`, ungated on summary coverage. If compaction is >5000 behind, un-summarized history is permanently deleted. `max_messages=5000` is exactly the acceptance-test number.
- **`reconcile_unenqueued_v2_messages` built, unwired:** db.py:2261-2306, docstring: "Periodic wiring... deferred to PR D's sweeper; not invoked anywhere in this PR."
- **PR A/B/C reuse:** PR A outbox (effect_id-idempotent + generation-fenced `apply_pending_effects`) is the exactly-once machinery the kill-recovery leans on. PR C `run_tool_loop` is the single foreground turn entry (both chat `process_job` + wake `_run_wake`) — the coroutine the child isolates.

---

## Components

### Half A — Pool safety

#### D1 — Turn-child subprocess + parent supervisor (`serve_worker.py` split; new `backend/model_api_runtime/v2/turn_child.py`)

Split `serve_worker._serve` into:
- **Parent supervisor**: runs `_reaper_loop`, `_heartbeat_loop` (now health-derived, D3), `_scheduler_loop`, the Genesis thread, the NEW watchdog loop (D2), the kill-switch poll (D4), and the reconcile sweeper (D9). The parent OWNS the turn-child lifecycle: `spawn_turn_child()` → `subprocess`/`multiprocessing.Process` running `turn_child.main()` (which runs `run_worker_loop`'s N slots). The parent can `SIGKILL` + respawn the child.
- **Turn child** (`turn_child.py`): a minimal entrypoint that re-initializes the DB pool + config in the fresh process and runs `run_worker_loop`. It emits a **liveness/progress signal** the parent reads — a per-slot "last progress ts" over a pipe (preferred, immediate) OR a child-level heartbeat row; on each claim/round-boundary a slot updates its progress marker. Shared-nothing: the child re-opens its own pool; parent and child coordinate only via the pipe + DB.

#### D2 — Watchdog + hard-timeout (`backend/model_api_runtime/v2/watchdog.py`, new; parent loop)

A parent watchdog tracks the child's per-slot progress signal. Triggers a kill when EITHER: (a) a single turn exceeds `TURN_HARD_TIMEOUT_SEC` (a wall-clock ceiling, e.g. 180s, > the 6-call budget's realistic max but bounded), OR (b) the child stops emitting ANY progress for `CHILD_LIVENESS_TIMEOUT_SEC` while jobs are claimable (all slots wedged). On trigger: write `capacity=0` (D3), `SIGKILL` the child, wait for exit, respawn a fresh child. In-flight jobs the killed child held are recovered by the reaper's lease TTL (marks them failed/requeuable) — PR A's idempotent outbox makes a re-drive exactly-once. The watchdog NEVER touches Genesis (separate thread in the parent).

#### D3 — Capacity reflects health (`serve_worker._heartbeat_loop`)

Replace the constant `capacity=MAX_WORKERS` with a value derived from actual child state: `capacity = 0` when the child is dead/killed/restarting; otherwise the live free-slot count (or `MAX_WORKERS` while the child is confirmed alive via a fresh progress signal). The heartbeat writes capacity=0 the instant the watchdog decides to kill, so admission's `workers_alive`/`live_worker_capacity` reflect the outage within one heartbeat/watchdog interval (acceptance: "capacity zeroes within a watchdog interval").

#### D4 — Live kill switch (`backend/model_api_runtime/v2/kill_switch.py` + migration + admin surface)

A durable, pollable flag: a `v2_runtime_control` single-row table (or a well-known global blob) with a `turns_halted BOOLEAN` column, flippable live (admin endpoint / SQL) without redeploy. Read points:
- **Admission** (`chat_send_core.py`, next to `workers_alive`): if halted → 503 `turns_halted` (**fail-CLOSED**, unlike the SLA fail-open).
- **Claim** (`_slot_loop` before `claim_next_job`, and/or `claim_next_job` itself): halted → claim nothing (slots idle).
- **Write fence** (the `_before_write`/`_fence_wake_effect` closures): halted → fence the write (do not apply; the turn fails cleanly / the effect stays pending for post-halt drain).
Genesis is untouched: it claims via `genesis_claim_uploaded_jobs` on a separate table and heartbeats `kind='genesis'` — the kill switch predicate is only wired into the turn paths. The flag is cached with a short TTL so a live flip takes effect within seconds.

### Half B — History safety

#### D5 — seq-cursor migration (`cursor.py` wiring, `worker.py`, `serve_worker.py`)

Wire the (currently unused) seq cursor into production: reply/cutover advance the cursor via a `cursor` outbox effect (`cursor.advance_effect` → the existing `_sink_cursor`), and the turn read boundary (`since`), the fold cursor, and compaction's watermark migrate from `ts` to `seq` (`db.chat_max_seq`/`chat_messages_after_seq`, seq-ordered/keyed, built to avoid the identical-ts hazard). `last_replied_ts` is superseded by a `last_replied_seq` (kept in runtime_state / the cursor blob). This closes the identical-timestamp boundary hazard `cursor.py`'s docstring warns about.

#### D6 — Prompt invariant + synchronous catch-up compaction (`worker.py` prompt assembly + `compaction`)

Before assembling a turn's prompt (`process_job`/`_run_wake`, after reading summary+tail): compute `watermark_seq` (summary coverage boundary) and `tail_start_seq` (oldest message the tail window will include). If `watermark_seq < tail_start_seq - 1` (a gap: messages neither summarized nor in the tail), run a **synchronous catch-up compaction** that folds the gap `[watermark_seq+1 .. tail_start_seq-1]` into the summary and advances the watermark, THEN re-read summary+tail. A hard guard (assertion / structural guarantee) asserts post-assembly that every message with `seq > watermark_seq` is present verbatim in the tail — the prompt invariant. The catch-up reuses `_run_compaction`'s oldest-first contiguous-from-watermark read (which is already correct) but runs inline in the turn rather than as a separate job.

#### D7 — GC/retention gate on summary coverage (`db._chat_insert_on_cursor` / the trim)

Gate the 5000-row trim on the summary watermark: the delete boundary clamps to `min(seq_rank_cutoff, watermark_seq)` — i.e. NEVER delete a row whose `seq > watermark_seq` (un-summarized). If compaction is behind, `chat_messages` may temporarily exceed `MAX_CHAT_MESSAGES` (grows until catch-up compaction advances the watermark, then the next trim reclaims). R2 body deletion follows the same gate (only delete bodies for rows that are safe to trim). This makes "retention/R2 GC only deletes already-covered content" a hard invariant.

#### D8 — Compaction CAS-loss requeue (`worker._run_compaction`)

On CAS loss (`upsert_summary_row_cas` returns False), instead of `mark_failed("summary_cas_lost")` + abandon, requeue a fresh `compaction_catchup` job (mirroring the success-path catch-up requeue at worker.py:670-676) so the still-over-budget tail is retried, never permanently abandoned. Bounded by the single-flight enqueue (no requeue storm). The CAS-lost attempt's computed batch is discarded (correct — a fresh attempt recomputes from the un-advanced watermark), but a retry is guaranteed.

#### D9 — Reconcile sweeper (`serve_worker` parent loop)

Add a periodic parent loop (sibling to `_reaper_loop`) that calls `db.reconcile_unenqueued_v2_messages()` (the built-but-unwired orphan-message sweep — a message persisted but with no active chat job → single-flight enqueue a catch-up). Runs in the PARENT (survives turn-child kills), on the reaper cadence or its own interval.

## Data flow / error handling

- **kill at a durable-effect boundary → exactly one reply/effect:** the turn-child is killed at an arbitrary point; the reaper's lease TTL marks its in-flight job failed/requeuable; a fresh child re-claims and re-drives. Because every effect is `effect_id`-idempotent + generation-fenced (PR A), re-enqueuing the same ids and re-draining `apply_pending_effects` yields exactly one reply/effect — regardless of whether the kill landed before enqueue (safe re-do) or after (idempotent replay).
- **all slots wedged → capacity=0 + restart + Genesis unaffected:** watchdog detects no progress → capacity=0 (D3) → SIGKILL+respawn child (D1/D2); Genesis thread lives in the parent, untouched.
- **5000+ identical-ts + attachments + compaction CAS race → no lost history / no wrong deletion:** seq total order (D5) + GC gated on watermark (D7) + CAS-loss requeue (D8) + prompt-invariant catch-up (D6) together guarantee no message is deleted before it is summarized and no message falls into an un-covered prompt gap.

## Testing — PR D's P0 subset + acceptance

- **P0 — kill at every durable-effect boundary → exactly one reply/effect:** parametrize the kill point around enqueue/dispatch/status-flip; assert after recovery exactly one reply bubble + one of each effect (leans on PR A's exactly-once; PR D adds the crash/restart boundary placement + the re-claim path).
- **P0 — all slots stuck → capacity zeroes + child restarts + Genesis unaffected:** a watchdog unit test with a fake stuck-child (progress signal frozen) asserts capacity→0 within the interval + a respawn is issued; a process-level smoke test spawns a real child, wedges it, and asserts SIGKILL+respawn while a Genesis heartbeat keeps ticking. Assert `genesis_alive` stays true across the kill.
- **P0 — 5000+ messages, identical ts, attachments, compaction crash/CAS race → no lost history, no wrong deletion:** extends PR A's seq-integrity P0 — append 5000+ identical-ts messages while compaction is deliberately kept behind / CAS-raced; assert (a) the GC trim never deletes a row with seq > watermark_seq, (b) after catch-up every message is either summarized or in the tail (prompt invariant), (c) a CAS-lost compaction requeues and eventually covers.
- **kill switch:** flip `turns_halted` → admission returns 503 `turns_halted`, `_slot_loop` claims nothing, an in-flight write is fenced, AND a Genesis import job still processes (separate path). Flip back → turns resume.
- **watchdog hard-timeout:** a turn exceeding `TURN_HARD_TIMEOUT_SEC` triggers the kill path (unit test with a fake clock / injected slow turn).

## Out of scope

- The child-process model targets the V2 turn pool only; `extraction`/`compaction` background lanes run inside the same turn-child slots (they're `agent_jobs` lanes) — they are killed/restarted with the child (acceptable; they're idempotent/requeuable). Genesis is explicitly NOT in the child.
- No new provider/model behavior; no changes to PR C's loop semantics beyond the seq-cursor migration + the inline catch-up compaction hook.

## The 7 P0 fault injections — completion check

With PR D, all seven P0 fault injections the @sxysun directive gates deploy on are implemented across the four PRs: (PR A) exactly-once across a durable-effect boundary, ABA/no-cross-generation, history integrity under identical timestamps; (PR C) unified-loop behavior (weak-model 1-call-1-bubble, reply+web intermediate exactly-once, mid-turn fold, malicious-page refusal); (PR D) all-slots-stuck watchdog + kill-recovery + the retention/coverage no-loss guarantee. PR D's completion is the gate for running the full 7-P0 suite green before any deploy / internal-user flip.
