---
document_lifecycle: decision
canonical_owner: self
---
# Hosted Runtime V2 — PR D: Pool / History Safety — Decision and historical design record

## Current reconciliation (2026-08-24)

This landed decision remains the canonical owner for the retained PR-D safety
invariants: progress/stall and absolute clocks, owner-fenced lease and write
recovery, effect-outbox idempotency, the live turn kill switch, seq-based
cursor/effect boundaries, prompt-coverage catch-up, durable source retention,
CAS-loss requeue, and the parent reconcile sweeper. Current implementation is
in `watchdog.py`, `child_supervisor.py`, `kill_switch.py`, `cursor.py`,
`worker.py`, `serve_worker.py`, and `db.py`; the focused watchdog,
kill-switch, history/cursor, compaction, and reconcile tests exercise those
invariants.

The D1--D3 **shared multi-slot child and whole-pool restart topology** is
superseded by the retained [three-pool slot-isolation decision](2026-08-14-runtime-v2-three-pool-slot-isolation-design.md).
Runtime V2 now has the three logical `foreground`, `wake`, and `heavy` pools,
and each configured slot is one child process. `pool_config.py`,
`pool_supervisor.py`, `slot_protocol.py`, `turn_child.py`, and `serve_worker.py`
are the current topology owners. The original current-state snapshot and
rollout gate below are historical evidence as of 2026-07-13, not operating
instructions or a current deployment claim.

### Current D1--D9 reconciliation

| Decision | Current implementation and owner |
|---|---|
| D1--D3 | The historical shared child is replaced by one `turn_child` process per `SlotSpec`; `pool_config.py` defines `foreground`/`wake`/`heavy`, `pool_supervisor.py` owns the fleet, and `serve_worker.py` publishes pool-aware fleet capacity. The [three-pool decision](2026-08-14-runtime-v2-three-pool-slot-isolation-design.md) owns this topology. |
| D2 recovery | Each watchdog targets one supervisor, advertises that pool/slot unavailable, confirms the physical kill, recovers only its exact `job_id + claimed_by` identity, and starts the replacement. Genesis remains a separate parent-thread path. |
| D4 | `kill_switch.py`, `jobs_store.py`, and `chat_send_core.py` implement live admission/claim/write fencing while leaving Genesis outside the predicate. |
| D5--D6 | `serve_worker.py`, `effect_outbox.py`, and `db.py` persist and dispatch the seq-aware `v2_reply_cursor_seq` boundary; `worker.py` uses seq watermarks, coverage catch-up, and a post-assembly bounded-tail assertion. |
| D7--D8 | Raw source retention is independent of prompt coverage, and compaction CAS loss requeues a fresh catch-up attempt. |
| D9 | `serve_worker._reconcile_loop` is wired in the parent fleet and calls `db.reconcile_unenqueued_v2_messages()`. |

Deployment truth is owned by [`CURRENT_STATE.md`](../../CURRENT_STATE.md), the
exact deployed commit, and that commit's compose; this decision does not state
live slot counts.

> **Historical retention note (2026-07-18):** D7's coverage-gated deletion has
> been superseded. Summary coverage now controls prompt compaction only; raw
> encrypted `chat_messages` rows and attached bodies are never automatically
> deleted by the 5,000-row hot-window limit or a summary watermark. Current
> deployment truth is [`CURRENT_STATE.md`](../../CURRENT_STATE.md), plus the
> current code/configuration named above.

**Historical status (2026-07-13):** Approved design, then awaiting spec review → plan.
**Depends on:** PR A (effect foundation `d7e19a9`) + PR B (transport) + PR C (unified tool loop) — all merged.
**Directive:** @sxysun 4-PR next round — PR D is the final "pool/history safety" PR. **7 P0 fault injections must be green before any deploy / any internal-user flip.** PR D lands the pool + history P0s, completing the set.

## Historical goal and design snapshot (2026-07-13)

The proposal would make the V2 turn pool a **killable/restartable crash domain** with a split stall/absolute-budget watchdog and health-reflecting capacity, add a **live kill switch** that halts admission/claim + fences active writes but never touches Genesis/import, and close the **history-integrity** gaps: migrate reply/cutover/summary to the stable seq cursor, enforce a prompt invariant (every message is covered by a committed summary OR appears verbatim in the prompt), retain raw chat/R2 source history independently of prompt compaction, and make compaction CAS-loss retry/requeue instead of silently abandoning.

**Two user-decided mechanisms (locked):**
- **Crash domain = a child PROCESS.** The original proposal moved turn slots to a killable/restartable turn-child subprocess; its shared-child shape is superseded by the current per-slot owner.
- **Prompt invariant = synchronous catch-up compaction.** The original proposal required catch-up before assembling a prompt; that retained invariant is now implemented as described above.

## Historical current-state snapshot (2026-07-13, superseded where noted)

- **Pool is one process, one loop:** `serve_worker._serve`/`main` (serve_worker.py:1321-1387) runs `asyncio.run` of `run_worker_loop` (N=`MAX_WORKERS`=4 `_slot_loop` tasks, worker.py:1521-1558/1452-1518) + `_reaper_loop` + `_heartbeat_loop` + `_scheduler_loop` as sibling asyncio tasks, plus Genesis on a **separate OS thread** (`_start_genesis_thread`, serve_worker.py:1268-1318). Docker `restart: unless-stopped` (deploy/docker-compose.phala.prod.runner.yaml) restarts only on process DEATH, not on wedge.
- **No hard-timeout on a turn:** `_slot_loop`'s `await _run_turn(job, deps)` is never wall-clock-bounded (grep-confirmed). Per-HTTP timeouts exist deep in provider_client/enclave; `_TURN_MAX_LLM_CALLS=6` bounds round COUNT not time. A truly-wedged coroutine occupies its slot forever. `reaper.py` + job-lease TTL (`RUNNING_TTL_SEC`=300) marks the DB job row failed (data-plane) but never touches the physically wedged coroutine (process-plane).
- **Capacity is a constant:** `_heartbeat_loop` (serve_worker.py:1186-1226) writes `capacity=MAX_WORKERS` unconditionally every ~10s; only graceful shutdown writes capacity=0. It never zeroes on wedge.
- **Genesis is already fully separate:** separate `genesis_import_jobs` table + `db.genesis_claim_uploaded_jobs` (not `agent_jobs`/`claim_next_job`), `kind='genesis'` heartbeat (invisible to `workers_alive`/`live_worker_count`), separate `_mint_genesis_token`/`_GENESIS_TOKEN_SCOPE`, its own thread. A kill switch / crash-domain move must simply keep Genesis in the parent and not couple to it.
- **No kill switch** exists. Admission choke point = `chat_send_core.py:96-135` (`workers_alive` gate + admission ceiling, fail-open). Claim choke point = `jobs_store.claim_next_job` (jobs_store.py:189-271). Write fences = the `_before_write`/`_fence_wake_effect` closures in `process_job`/`_run_wake` (worker.py:1194-1246, 792-824) which already check `runtime_mode_enabled` + lease before every write.
- **compaction CAS-loss silently abandons:** `_run_compaction` (worker.py:680-685) on CAS loss calls `jobs_store.mark_failed(job_id, "summary_cas_lost")` and returns "failed" — comment "视为丢弃，不重试、不报错". The success path (:670-676) DOES re-enqueue `compaction_catchup` when the tail is still over threshold; the failure path has NO equivalent requeue.
- **seq cursor built but UNUSED:** `cursor.advance_effect`/`load_seq` (cursor.py) have ZERO production callers; the `_sink_cursor` sink is wired (serve_worker.py:884-898) but nothing produces a cursor effect. Prompt assembly is entirely ts-based: `process_job` reads `since=last_replied_ts` → `read_summary`→`(summary, watermark_ts, version)` → `read_tail(user_id, watermark, _TAIL_HARD_CAP=60)`.
- **Prompt invariant VIOLATED today:** `_read_tail_window` (serve_worker.py:359-417) chat path takes `candidates[-60:]` (newest 60 after watermark). If >60 messages accumulate past the summary watermark (compaction behind/failed/CAS-lost), everything between watermark and `candidates[-61]` is silently dropped — not summarized, not in tail. No invariant enforces coverage today.
- **Retention resolution (implemented 2026-07-18):** the old append-time `DELETE` was removed entirely. `MAX_CHAT_MESSAGES=5000` is now only the bounded process-cache window; PostgreSQL rows and R2 bodies remain durable until an explicit user/account deletion or a same-id body replacement retires an old object.
- **`reconcile_unenqueued_v2_messages` built, unwired:** db.py:2261-2306, docstring: "Periodic wiring... deferred to PR D's sweeper; not invoked anywhere in this PR."
- **PR A/B/C reuse:** PR A outbox (effect_id-idempotent + generation-fenced `apply_pending_effects`) is the exactly-once machinery the kill-recovery leans on. PR C `run_tool_loop` is the single foreground turn entry (both chat `process_job` + wake `_run_wake`) — the coroutine the child isolates.

---

## Historical 2026-07 proposal components (not current topology or runbook)

### Half A — Pool safety

#### D1 — Historical shared turn-child subprocess + parent supervisor (superseded topology)

The proposal would split `serve_worker._serve` into:
- **Parent supervisor**: runs `_reaper_loop`, `_heartbeat_loop` (now health-derived, D3), `_scheduler_loop`, the Genesis thread, the NEW watchdog loop (D2), the kill-switch poll (D4), and the reconcile sweeper (D9). The parent OWNS the turn-child lifecycle: `spawn_turn_child()` → `subprocess`/`multiprocessing.Process` running `turn_child.main()` (which runs `run_worker_loop`'s N slots). The parent can `SIGKILL` + respawn the child.
- **Turn child** (`turn_child.py`): a minimal entrypoint that re-initializes the DB pool + config in the fresh process and runs `run_worker_loop`. It emits a **liveness/progress signal** the parent reads — a per-slot "last progress ts" over a pipe (preferred, immediate) OR a child-level heartbeat row; on each claim/round-boundary a slot updates its progress marker. Shared-nothing: the child re-opens its own pool; parent and child coordinate only via the pipe + DB.

#### D2 — Historical watchdog proposal (retained invariant; topology superseded)

The proposal described four distinct watchdog signals instead of treating turn age as proof of a wedge: (a) process death or a stale event-loop heartbeat; (b) stale slot progress with no active turn while work is claimable; (c) an active turn with stalled provider/tool/compaction progress; and (d) a turn above an absolute age. Its 240s/1500s example budgets and legacy-alias cleanup were design-time values, not current configuration.

The proposal bounded queue and capacity writes, then described capacity-zero, kill and respawn. Current recovery instead follows the per-slot exact-claim sequence in the reconciliation table; Genesis remains outside the turn watchdog and kill switch.

#### D3 — Historical health-capacity proposal (retained invariant; per-slot topology supersedes shared-child wording)

The proposal would replace constant capacity with health-derived capacity. Current pool-aware fleet heartbeats publish the corresponding capacity per logical pool.

#### D4 — Historical live-kill-switch proposal (the invariant is landed)

A durable, pollable flag: a `v2_runtime_control` single-row table (or a well-known global blob) with a `turns_halted BOOLEAN` column, flippable live (admin endpoint / SQL) without redeploy. Read points:
- **Admission** (`chat_send_core.py`, next to `workers_alive`): if halted → 503 `turns_halted` (**fail-CLOSED**, unlike the SLA fail-open).
- **Claim** (`_slot_loop` before `claim_next_job`, and/or `claim_next_job` itself): halted → claim nothing (slots idle).
- **Write fence** (the `_before_write`/`_fence_wake_effect` closures): halted → fence the write (do not apply; the turn fails cleanly / the effect stays pending for post-halt drain).
Genesis is untouched: it claims via `genesis_claim_uploaded_jobs` on a separate table and heartbeats `kind='genesis'` — the kill switch predicate is only wired into the turn paths. The flag is cached with a short TTL so a live flip takes effect within seconds.

### Half B — History safety

#### D5 — Historical seq-cursor migration proposal (the invariant is landed)

The proposal would wire the then-unused seq cursor/effect boundary and migrate the reply, fold and compaction boundaries from timestamp to seq, closing identical-timestamp ambiguity.

#### D6 — Historical prompt-coverage proposal (the invariant is landed)

The proposal would detect a summary/tail coverage gap, synchronously catch it up before prompt assembly, then assert the bounded tail covers each unfurled message.

#### D7 — Historical durable-retention proposal (the invariant is landed)

The proposal established that prompt coverage is not permission to delete source history; its retained source-retention invariant is now implemented independently of prompt compaction.

#### D8 — Historical CAS-loss-requeue proposal (the invariant is landed)

The proposal would requeue a fresh single-flight `compaction_catchup` on CAS loss rather than abandon the still-over-budget tail; that retained retry invariant is now implemented.

#### D9 — Historical reconcile-sweeper proposal (the invariant is landed)

The proposal would add a parent loop for the then-unwired `db.reconcile_unenqueued_v2_messages()` sweep; the current parent fleet wires it through `_reconcile_loop`.

## Historical 2026-07 data-flow / error-handling proposal

- **kill at a durable-effect boundary → exactly one reply/effect:** the turn-child is killed at an arbitrary point; the reaper's lease TTL marks its in-flight job failed/requeuable; a fresh child re-claims and re-drives. Because every effect is `effect_id`-idempotent + generation-fenced (PR A), re-enqueuing the same ids and re-draining `apply_pending_effects` yields exactly one reply/effect — regardless of whether the kill landed before enqueue (safe re-do) or after (idempotent replay).
- **all slots wedged → capacity=0 + restart + Genesis unaffected:** watchdog detects no progress → capacity=0 (D3) → SIGKILL+respawn child (D1/D2); Genesis thread lives in the parent, untouched.
- **5000+ identical-ts + attachments + compaction CAS race → no lost history / no wrong deletion:** seq total order (D5) + source retention independent of compaction (D7) + CAS-loss requeue (D8) + prompt-invariant catch-up (D6) together guarantee every source row remains durable and no message falls into an un-covered prompt gap.

## Historical 2026-07 acceptance proposal

- **P0 — kill at every durable-effect boundary → exactly one reply/effect:** parametrize the kill point around enqueue/dispatch/status-flip; assert after recovery exactly one reply bubble + one of each effect (leans on PR A's exactly-once; PR D adds the crash/restart boundary placement + the re-claim path).
- **P0 — all slots stuck → capacity zeroes + child restarts + Genesis unaffected:** a watchdog unit test with a fake stuck-child (progress signal frozen) asserts capacity→0 within the interval + a respawn is issued; a process-level smoke test spawns a real child, wedges it, and asserts SIGKILL+respawn while a Genesis heartbeat keeps ticking. Assert `genesis_alive` stays true across the kill.
- **P0 — 5000+ messages, identical ts, attachments, compaction crash/CAS race → no lost history, no wrong deletion:** extends PR A's seq-integrity P0 — append 5000+ identical-ts messages while compaction is deliberately kept behind / CAS-raced; assert (a) every source row and attachment remains durable regardless of watermark, (b) exact seq pages visit every row, (c) after catch-up every message is either summarized or in the tail, and (d) a CAS-lost compaction requeues and eventually covers.
- **kill switch:** flip `turns_halted` → admission returns 503 `turns_halted`, `_slot_loop` claims nothing, an in-flight write is fenced, AND a Genesis import job still processes (separate path). Flip back → turns resume.
- **watchdog split budgets:** fake-clock tests prove that a turn older than the former 180s ceiling survives while it keeps making real progress, a stalled turn crosses the stall budget and triggers recovery, and a continually-progressing turn still triggers recovery at the larger absolute ceiling. Separate tests cover unconditional stale event-loop recovery, queue-gated idle/pre-claim recovery, and bounded DB calls.

## Historical 2026-07 out of scope

- The child-process model targets the V2 turn pool only; `extraction`/`compaction` background lanes run inside the same turn-child slots (they're `agent_jobs` lanes) — they are killed/restarted with the child (acceptable; they're idempotent/requeuable). Genesis is explicitly NOT in the child.
- No new provider/model behavior; no changes to PR C's loop semantics beyond the seq-cursor migration + the inline catch-up compaction hook.

## Historical 2026-07 P0 completion check

With PR D, all seven P0 fault injections the @sxysun directive gates deploy on are implemented across the four PRs: (PR A) exactly-once across a durable-effect boundary, ABA/no-cross-generation, history integrity under identical timestamps; (PR C) unified-loop behavior (weak-model 1-call-1-bubble, reply+web intermediate exactly-once, mid-turn fold, malicious-page refusal); (PR D) all-slots-stuck watchdog + kill-recovery + the durable-source/prompt-coverage no-loss guarantee. PR D's completion is the gate for running the full 7-P0 suite green before any deploy / internal-user flip.
