# P0-B Task 5 bounded retention report

## Scope

Added retention to the existing `provider_attempt_rollup` maintenance lane only.
It remains default-on, reuses the existing advisory leader and worker loop, and
stores/deletes data only in the current business RDS. No thread, process,
service, deployment unit, database instance, SQLite, Redis, Kafka, or queue was
added. This slice intentionally does not run the formal 3M + 3M performance
proof or open/update a PR.

## Policy and ordering

- `FEEDLING_PROVIDER_ATTEMPT_RETENTION_DAYS` defaults to 400 days, clamps shorter
  values to 400, accepts a longer policy, and safely falls back on malformed
  values.
- The published cutoff is a Shanghai local day. Job-backed canonical attempts
  are owned by authoritative `v2_turn_metrics.created_at`; attempts with no
  matching whole-turn row use `started_at` conservatively.
- Retention runs only after the current tick has rebuilt every eligible dirty
  day and every source cursor reports no backlog. Replay/bootstrap avoids
  rebuilding retained-out days, while late source-change claims below a
  published boundary remain durable until retention consumes them.
- Parent attempts are selected with bounded `FOR UPDATE OF a SKIP LOCKED`
  batches. One rotating global budget is shared fairly by parents, dimensions,
  memberships, and dirty claims. Corrections disappear only through the parent
  FK cascade and are reported separately from explicit budget consumption.
- The first incomplete destructive page publishes `retention_pending_from` in
  the same transaction as its deletes. `retained_from` advances only after the
  transaction proves there are no
  eligible parent attempts or derived rows left. A locked row, SQL error,
  timeout, cancellation, or CAS loss prevents publication and rolls back the
  page. A previously published boundary never regresses.
- Rate cards, whole-turn metrics, and users are never retention targets. Account
  reset remains an immediate independent FK/explicit-belt deletion path.

## Bounded plan and steady state

The job-backed delete takes a bounded newest-first page through
`ix_v2_turn_metrics_created_at` and `ix_llm_provider_attempts_runtime_job`.
Matched and orphan proofs continue below an already-published boundary, so late
attempts and corrected turn dates cannot become permanently invisible. The
orphan branch uses a
new exact partial current-RDS index,
`ix_llm_provider_attempts_retention_started`, on
`(started_at, attempt_id) INCLUDE (job_id)` for canonical runtime rows. An
EXPLAIN regression with a 3,001-row skewed fixture requires all three indexes.
Repeated same-cutoff ticks retain bounded late-data probes rather than using an
unsafe watermark fast path.

## Coverage payload

Admin hybrid/raw attempt payloads now expose `retained_from`,
`retention_pending_from`,
`retention_truncated`, and `retention_partial_reason`. A query crossing the
boundary keeps the known whole-turn denominator but marks logical-call coverage
unavailable and attempt totals partial; missing retained-out rows are never
rendered as complete zero. The Admin HTML explains the boundary. Runtime health
exports the same retention coverage and sets per-lane logical coverage to
`None` with an explicit reason instead of `0`. UTC and arbitrary IANA display
timezones compare against Shanghai retention midnight, crop only the published
interval, and preserve surviving raw rows while a pending fence is active.

## Independent review hardening

The follow-up review findings C1-C3 and I1 are covered by RED-to-GREEN tests:

- a late orphan and a matched turn moved behind `retained_from` are deleted
  before the next cutoff publishes;
- multi-page deletion publishes an atomic pending fence, including a
  repeatable-read test proving no intermediate reader state;
- UTC and America/Los_Angeles queries crossing the Shanghai boundary expose the
  same partial reason, denominator, and unavailable ratio;
- `max_retention_rows=1` never explicitly mutates more than one target row per
  tick and rotating progress reaches every target; correction cascades are
  measured separately.

I2 remains a formal-run gate rather than an invented local result. The opt-in
3M harness verifies the exact retention-index definition under the normal
planner and records index bytes, bytes per attempt, share of attempt-table
bytes, and maintenance counters. It fails closed unless JSON evidence also
supplies pool peak/capacity/timeouts and recorder plus OpenRouter/Anthropic/
Google request counts, p95 latency, business errors, and baseline-result
equivalence. The formal 3M run was intentionally not run in this task.

## TDD evidence

Initial REDs covered the missing environment policy and retention API. Follow-up
REDs caught two real defects before commit:

- an inner candidate `LIMIT` could stop `SKIP LOCKED` from reaching an unlocked
  second parent;
- a bounded newest-turn candidate page could starve an older turn that still
  owned attempts after newer expired turns had already been cleared; and
- a cross-retention payload dropped the still-known whole-turn denominator.

The GREEN suite covers batch bounds, correction cascade, job-backed and orphan
selection, locked rows, derived pruning, preserved rate cards/turn metrics/users,
late watermark publication, rollback/cancellation, dirty-before-retention
ordering, replay cutoff fences, steady-state no-op, index-driven EXPLAIN,
Admin/Runtime partial coverage, HTML copy, and the existing account-reset path.

## Verification

- Final retention migration/reconciler/Admin/Runtime suite: **243 passed**, 37
  existing Alembic deprecation warnings.
- Ruff: passed for all changed Python files.
- `py_compile`: passed for all changed Python files.
- `git diff --check`: passed.
