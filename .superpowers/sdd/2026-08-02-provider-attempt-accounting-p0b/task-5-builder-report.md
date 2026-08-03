# P0-B Task 5 atomic day-builder report

## Scope

Implemented the maintenance-only `provider_attempt_rollup.recompute_local_day`
slice. Given one Shanghai local day, it rebuilds
`llm_usage_daily_attempt_dimensions` and
`llm_usage_daily_call_memberships` inside one bounded `REPEATABLE READ`
transaction, then removes that day's sparse dirty claim only after both inserts
succeed. This slice does not connect a worker loop, discover cursors, switch the
report read path, or perform retention.

The builder uses the current business RDS only. It adds no database instance,
SQLite, Redis, Kafka, service, deployment unit, queue, or provider-hot-path work.

## TDD evidence

Initial RED failed during collection because
`model_api_runtime.v2.provider_attempt_rollup` did not exist.

The focused real-PostgreSQL fixtures now prove:

- the Shanghai half-open whole-turn day cohort owns day/user/lane attribution;
- canonical `runtime_recorder` main facts plus append-only corrections are
  folded once, including signed token/cost deltas and conflicting currencies;
- immutable rate cards are resolved by set-based `lead(effective_at)` ranges,
  including the exact version boundary and a zero cache-read rate whose token
  allocation must still be known;
- retries, failovers, failures, possibly-billed rows, and requested/resolved and
  known/unknown memberships remain distinct without additive double counting;
- call gaps are computed from every canonical attempt for selected calls, even
  when a gap-filling attempt belongs to a job outside the rebuilt day, and use
  one-based ordinal semantics;
- exact TTFT arrays are sorted, reruns are idempotent, and raw overview,
  requested/resolved identity, cost, logical-call, and gap results match the
  current raw report;
- an injected real PostgreSQL trigger failure after both day deletes rolls back
  both derived tables, preserves their previous rows, leaves the dirty claim,
  and returns a safe error result without raising.

## Verification

```text
tests/test_provider_attempt_rollup.py + tests/test_admin_usage.py
125 passed, 2 existing Alembic warnings in 3.05s
```

Ruff passed for both changed Python files. `py_compile` and `git diff --check`
also passed.

## Builder review fix round

The initial builder executed the complete cohort/correction/rate pipeline once
for dimensions and again for memberships. Although both queries were set-based,
that duplicated the expensive day source work.

A new observer/captured-SQL RED now requires exactly one business rebuild
statement and exactly one occurrence of each materialized cohort, attempt,
correction, rate-range, and priced stage. The builder now uses one
data-modifying `WITH`: a shared pipeline feeds one dimensions `INSERT ...
RETURNING` and one memberships `INSERT ... RETURNING`, and the final `SELECT`
returns both inserted counts. Day deletes and dirty-claim removal remain in the
same outer transaction; no temporary table or catalog object is created.

PostgreSQL `EXPLAIN (FORMAT JSON)` additionally proves that the captured shared
statement has exactly the two intended `ModifyTable` nodes. The fix-round
builder plus full Admin Usage run is **126 passed**; all prior raw parity,
idempotency, and real-failure rollback assertions remain green.
