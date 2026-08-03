# P0-B Task 5 bounded reconciler report

## Scope

Implemented the reconciler-only slice for the existing current-RDS attempt
rollups. It is default-on with an explicit false-like opt-out and runs beside the
P0-A usage rollup in the existing `serve_worker._usage_rollup_loop`; no new task,
process, service, deployment unit, database, SQLite, Redis, Kafka, or queue was
added. This slice does not switch the Admin report read path and does not add the
final retention or load/performance proof.

## TDD and correctness

The focused RED/GREEN suite proves:

- sparse bootstrap and replay enqueue bounded existing Shanghai days;
- independent attempt `(updated_at,id)`, late-correction `id`, turn-metric
  `(updated_at,id)`, and rate-card creation/identity/version cursors advance in
  the same short transaction as their durable dirty-day rows;
- an injected crash after dirty insertion rolls back both dirty rows and cursors;
- a bounded stale-start pass runs first, marks billing uncertainty without
  consuming provider-event revision 1, and a late complete rev1 can still win;
- an attempt update and late correction dirty the owning turn day through
  `job_id`, while a rate-card append dirties only its effective provider/model
  interval;
- a rate-card interval larger than the global dirty budget advances through a
  new bounded replay generation instead of permanently retrying or losing days;
- the global distinct-day budget is shared by every source, while many same-day
  rows still advance up to the independent source-row budget;
- a tiny 0077 trigger records both OLD and NEW Shanghai days for a turn
  `created_at` day move, the information unavailable from the post-update cursor;
  a timestamp change within one Shanghai day returns without duplicate-key work;
- dirty generation is a CAS fence for day replacement; builder failure retains
  the claim; advisory contention, cancellation boundaries, optional-lane
  exceptions, and unlock uncertainty remain fail-open; an uncertain unlock
  closes the physical session so no pooled borrower inherits the lock; and
- the existing worker loop catches each reporting lane independently, so an
  attempt-rollup failure cannot cancel usage maintenance or any provider/reply/
  retry/heartbeat/job coroutine.

All SQL is content-free and all error reporting is reduced to safe exception
class names. Test and prod RDS were not accessed; verification used only the
throwaway local PostgreSQL test database on `127.0.0.1:55432`.

## Verification

- Focused PostgreSQL + worker suites:
  `test_provider_attempt_rollup.py`,
  `test_provider_attempt_rollup_reconciler.py`,
  `test_provider_attempt_rollup_migration.py`,
  `test_provider_attempt_recorder.py`,
  `test_v2_usage_rollup.py`, and `test_v2_serve_worker.py` — **154 passed**.
- Reconciler file — **19 passed**; 0077 migration file — **12 passed**.
- Ruff passed for the reconciler, migration, and tests; the changed worker file
  passed with its pre-existing module-path `E402` and unrelated `F401` baseline
  exclusions.
- `py_compile` and `git diff --check` passed.

## Independent-review fix round

The first independent review found that the source-row limit was applied once
per stream, stale-start lookup lacked a selective index, and the fixed
five-minute cadence had no bounded catch-up signal. The fix round adds:

- one hard global source-row budget (default 6,000) shared by attempt,
  correction, turn, and rate-card streams; the three main streams receive a
  reserved fair first quota, and unused quota is reclaimed only in a bounded
  second pass;
- a test observer proving total fetched and advanced source rows remain within
  the configured budget, correction/turn cursors advance under continuous
  attempt backlog, and same-day rows can consume the independent row budget
  without consuming extra dirty-day capacity;
- safe per-stream backlog booleans, maximum source lag seconds, and a dirty-work
  signal in tick results; successful pending work uses a bounded five-second
  catch-up delay, while errors keep the normal cadence and cannot tight-loop;
- a real-PostgreSQL static 2,101-row attempt batch, larger than the old 2,000-row
  page, that the new default page consumes in one tick; and
- exact recoverable concurrent partial index
  `ix_llm_provider_attempts_stale_started` on `(started_at,attempt_id)` for the
  canonical started-only predicate. Migration tests cover exact validity,
  same-relation wrong-definition repair, unrelated-owner downgrade refusal, and
  an `EXPLAIN (ANALYZE, BUFFERS)` plan using the partial index.

The second pooled connection used by the day builder remains intentionally
unchanged for the final load proof, per review scope.

The final loop fix tracks a separate monotonic next-eligible time per reporting
lane. A successful pending lane may run again after five seconds, while an error
lane keeps the normal cadence even if its sibling is catching up. Mixed-lane
tests prove a pending usage lane advances repeatedly without re-invoking a
failed attempt lane; disabled, stop, cancellation, and all-error paths retain
their bounded behavior.
