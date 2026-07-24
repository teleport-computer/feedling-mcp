# Genesis deploy-orphan fast reclaim — design

## Problem

An onboarding distillation (a `genesis_import_jobs` row) that is `processing` when
its worker dies is stuck for **30 minutes** before any recovery. Observed on pre
2026-07-17: a runner-CVM deploy SIGKILLed the serve-worker mid-distill; the job
sat in `processing` until the time-based stale reaper (`FEEDLING_GENESIS_STALE_SEC
= 1800`) failed it at the 30-minute mark, after which the client retried and
succeeded.

## Why the existing PR D does not cover this

PR D (pool/history safety, largely merged) makes the **turn** pool a killable
crash domain and is explicit that **"Genesis is NEVER halted/killed; it stays in
the parent, untouched."** That guarantee is about *intra-container* isolation: when
the watchdog SIGKILLs the misbehaving **turn child**, Genesis (running on a parent
thread) survives.

The kill here is different: a **deploy replaces the whole container**, so docker
`stop`→SIGKILL takes down the parent too — Genesis included — mid-distill. Nothing
in PR D addresses "the whole container (parent + Genesis) is killed and the
in-flight genesis job must recover fast." This is a new, focused gap.

## Root mechanics (verified)

- `db.genesis_claim_uploaded_jobs` (db.py:2830) flips `uploaded`→`processing` with
  **no worker attribution** — nothing records which worker is processing a job.
- The genesis worker only re-claims `uploaded` jobs
  (`genesis.worker.tick`), never `processing` — so a wedged `processing` job is
  never resumed by the new worker.
- The only recovery is the time-based reaper `db.genesis_reap_stale_processing_jobs`
  (db.py:2943), fired each loop by `genesis.worker.reap_stale_processing_jobs`,
  with a deliberately generous 30-min cutoff (so a slow-but-live distill is not
  false-reaped).

## Approach (decisions locked)

**Heartbeat death-detection + resume-or-fail.**

1. **Attribute the claim.** `genesis_claim_uploaded_jobs` records the claiming
   genesis worker's id (the same id it heartbeats under, `<worker_id>:genesis`).
2. **Truthful liveness.** The genesis heartbeat must beat **independently of the
   tick**. Today it beats only before/after each tick (`daemon.run_loop`), so a
   multi-second distill freezes it — making a *live* worker look dead. Add a
   lightweight background beat (every ~15s) so a worker mid-distill still
   heartbeats. This lets the dead-threshold be tight without false positives.
3. **Fast reclaim.** A reclaim step (run each loop, before the time-based reaper)
   fails/requeues `processing` jobs whose `worker_claimed_by` is **not** among the
   live genesis workers in `v2_worker_heartbeats` (`kind='genesis'`, fresh within
   `FEEDLING_GENESIS_WORKER_DEAD_SEC`, default 120s):
   - **Resumable** (`received_chunks > 0` — encrypted chunks are stored) → reset
     `processing`→`uploaded` so a live worker re-runs it. Auto-recovery, no user
     action.
   - **Not resumable** (`received_chunks = 0` — plaintext onboarding, which is not
     persisted) → mark `failed` fast so iOS surfaces it and the user retries in
     seconds instead of 30 minutes.

The 30-min time-based reaper stays as the ultimate backstop for the rare
alive-but-wedged case (worker heartbeats fine but a job genuinely hangs).

### Why this is the right shape

It mirrors exactly what PR D already does for **turn** jobs: a killed child stops
renewing its lease and the reaper marks its jobs failed/requeuable via owner
fencing. Genesis simply lacked the equivalent (a claiming-worker identity + a
death-detected reclaim). We add that, reusing the existing `v2_worker_heartbeats`
liveness signal.

## Components

1. **Migration `0040_genesis_worker_claim`** — add `worker_claimed_by TEXT` (+
   `worker_claimed_at TIMESTAMPTZ NULL`) to `genesis_import_jobs`. A dedicated
   column, not the `output` jsonb: the plaintext reducer overwrites `output`
   (`plaintext.py:676`), which would erase an attribution stored there. Distinct
   from the resident path's `resident_consumer_id` (that path is legacy
   agent-runner; this is the serve-worker path).
2. **`db.genesis_claim_uploaded_jobs(*, worker_id, limit)`** — record
   `worker_claimed_by = worker_id`, `worker_claimed_at = now()` on claim (both
   primary and TEE-mirror writes).
3. **`db.genesis_reclaim_orphaned_processing_jobs(live_worker_ids, *, dead_sec, limit)`**
   — atomic (`FOR UPDATE SKIP LOCKED`) reclaim of `processing` rows whose
   `worker_claimed_by` is non-null AND not in `live_worker_ids` AND
   `worker_claimed_at < now() - dead_sec`: reset to `uploaded` when
   `received_chunks > 0`, else `failed` with `error='genesis_worker_lost'`. Returns
   the rows it changed (so the service layer can sync `genesis_state` blobs, same
   as the time reaper).
4. **`jobs_store.live_genesis_worker_ids(*, within_sec)`** — `SELECT worker_id FROM
   v2_worker_heartbeats WHERE kind='genesis' AND beat_at > now() - within_sec`.
   (Sibling to `workers_alive`/`live_worker_count`, which read `kind='turn'` only.)
5. **`genesis.worker.reclaim_orphaned_processing_jobs(worker_id)`** — reads live
   ids, calls the DB fn, syncs `genesis_state` for reset/failed rows (mirrors
   `reap_stale_processing_jobs`). Thread `worker_id` through
   `daemon.run_loop`→`tick`/reclaim.
6. **Independent genesis heartbeat** — in `serve_worker._start_genesis_thread`,
   beat on a ~15s timer decoupled from the tick (so a mid-distill worker stays
   live). Thread the genesis `worker_id` into `daemon.run_loop` so claim + reclaim
   use the same id the heartbeat writes.

## Acceptance (P0)

- **Deploy-kill recovery**: a `processing` genesis job whose `worker_claimed_by` is
  a dead worker (no fresh `kind='genesis'` heartbeat) is reset→`uploaded`
  (chunks stored) or `failed` (plaintext) within `dead_sec`, NOT 30 min.
- **No false reclaim of a live worker**: a job whose `worker_claimed_by` is a
  currently-heartbeating worker is never touched, even when its `updated_at` is
  stale (mid long distill) — because the worker heartbeats independently of the
  tick.
- **Backstop intact**: the 30-min time reaper still fails a job whose worker
  heartbeats fine but genuinely hangs.
- **Single-worker prod**: after a deploy, the new worker reclaims the old worker's
  orphan on an early tick (old id absent from live set once past `dead_sec`).

## Non-goals

- True mid-distill resume (checkpoint/replay of a partial LLM distill). Resumable
  jobs re-run the whole distill from stored chunks; plaintext jobs fail+retry.
- Changing docker/dstack stop-grace behavior (unreliable per the "dstack won't
  restart the container" constraint). Recovery is death-detected, not drain-based.
- Turn-pool changes (PR D already covers those).
