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
  day. Replay, source discovery, and dirty selection exclude days before an
  already-published `retained_from`.
- Parent attempts are selected with bounded `FOR UPDATE OF a SKIP LOCKED`
  batches. Corrections disappear only through the parent FK cascade. Derived
  dimensions, memberships, and expired dirty claims are pruned in bounded
  batches in the same transaction.
- `retained_from` advances only after the transaction proves there are no
  eligible parent attempts or derived rows left. A locked row, SQL error,
  timeout, cancellation, or CAS loss prevents publication and rolls back the
  page. A previously published boundary never regresses.
- Rate cards, whole-turn metrics, and users are never retention targets. Account
  reset remains an immediate independent FK/explicit-belt deletion path.

## Bounded plan and steady state

The job-backed delete first takes a bounded newest-first page of expired
whole-turn jobs from `ix_v2_turn_metrics_created_at`, then performs parameterized
lookups through `ix_llm_provider_attempts_runtime_job`. The orphan branch uses a
new exact partial current-RDS index,
`ix_llm_provider_attempts_retention_started`, on
`(started_at, attempt_id) INCLUDE (job_id)` for canonical runtime rows. An
EXPLAIN regression with a 3,001-row skewed fixture requires all three indexes.
Once a cutoff is published, repeated ticks on the same Shanghai day are a
watermark-only no-op; the next day scans only the newly expired interval.

## Coverage payload

Admin hybrid/raw attempt payloads now expose `retained_from`,
`retention_truncated`, and `retention_partial_reason`. A query crossing the
boundary keeps the known whole-turn denominator but marks logical-call coverage
unavailable and attempt totals partial; missing retained-out rows are never
rendered as complete zero. The Admin HTML explains the boundary. Runtime health
exports the same retention coverage and sets per-lane logical coverage to
`None` with an explicit reason instead of `0`.

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

- Focused PostgreSQL/Admin/Runtime/account/worker suite: **331 passed**, 30
  existing Alembic deprecation warnings.
- Final Admin + Runtime rerun after denominator hardening: **188 passed**, 2
  existing Alembic deprecation warnings.
- Ruff: passed for all changed Python files.
- `py_compile`: passed for all changed Python files.
- `git diff --check`: passed.
