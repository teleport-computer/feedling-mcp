# P0-B Task 5 atomic day-builder independent review

Reviewed commits:

- `8060d96a` — `feat(accounting): rebuild daily attempt rollups`
- `6ffc0e68` — `fix(accounting): share attempt rollup pipeline`

## Verdict

- **Spec compliance: PASS**
- **Code quality: PASS**

No Critical, Important, or Minor findings remain.  The atomic day builder is
ready to enter the bounded reconciler slice.  I found no content-bearing fields,
provider-hot-path dependency, or new infrastructure/deployment unit.

## Stage 1: spec compliance

### Important — closed: one shared day pipeline feeds both inserts

`_recompute_on_cursor` now executes one `_day_rebuild_statement`
(`backend/model_api_runtime/v2/provider_attempt_rollup.py:291-304`).  That
statement expands the cohort, attempt, correction, effective-rate, and pricing
pipeline exactly once, with `priced AS MATERIALIZED` shared by the dimension and
membership consumers (`:193-276`).  The two data-modifying CTEs insert into the
two independent derived tables and each returns one row per inserted aggregate;
the final scalar subqueries force both DML CTE dependencies and return exact
zero/nonzero counts (`:223-276`).  PostgreSQL data-modifying CTE snapshot/order
rules are safe here because neither insert reads or writes the other's target.

The regression observer proves one business statement and one occurrence of
every expensive materialized stage.  Its PostgreSQL JSON plan proves exactly two
`ModifyTable` nodes, one for each intended rollup table
(`tests/test_provider_attempt_rollup.py:379-426`).  The solution creates no temp
table, persistent staging table, function, index, or other catalog object.

All other reviewed implementation semantics match the design statically:

- Shanghai half-open day ownership and user/lane attribution come from
  `v2_turn_metrics`, not attempt timestamps/identity (`:70-78`).
- Main facts plus append-only corrections are folded once per produced fact;
  signed token/cost deltas, missing/conflicting currency, and cost-kind
  exclusivity are preserved (`:79-122`, `:201-228`).
- The set-based effective ranges preserve the same effective-time selection as
  the raw lookup, and cache allocation knownness uses differential-rate rules
  rather than treating a zero cache rate as automatically known (`:123-167`).
- Memberships preserve requested/resolved and known/unknown variants, while gap
  computation rereads every canonical attempt for selected calls and uses
  one-based ordinal arithmetic (`:240-277`).
- TTFT arrays are exact and sorted (`:220-221`).  Both derived tables and the
  dirty claim are replaced/cleared in one repeatable-read transaction; any SQL,
  pool, timeout, lock, or serialization exception rolls back and is reduced to a
  safe exception class (`:283-333`).  User-bearing FK cascades make a concurrent
  account deletion either block/rollback the rebuild or delete committed derived
  rows; there is no anonymous retained row.

## Stage 2: code quality and test adequacy

The SQL is content-free, parameterized, and the maintenance boundary is concise.
Pool acquisition is bounded to 0.5 seconds, statement/lock timeouts are local,
and error logging includes only the safe exception class.  There is no import
from dispatch/reply/retry/failover/heartbeat/job-state paths and no SQLite,
Redis, Kafka, new RDS, queue, service, deployment unit, or CVM.

The focused fixture covers retries/failover, mixed known/unknown membership,
signed corrections, currency conflict, a rate boundary, global gaps, sorted
TTFT, idempotency, and transaction rollback.  It does not structurally guard
against a duplicate correction/rate/pricing pipeline.  The new observer and
EXPLAIN assertion now provide that structural guard.  The existing injected
failure still proves the preceding day deletes, both derived-table inserts, and
dirty-claim clear are one outer transaction: any failure rolls the whole rebuild
back and retains the dirty claim.

## Fresh verification

```text
FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres \
  .venv-test/bin/python -m pytest \
  tests/test_provider_attempt_rollup.py tests/test_admin_usage.py -q

126 passed, 2 existing Alembic warnings in 2.78s
```

An initial sandboxed invocation could not reach local PostgreSQL and therefore
produced environment/setup failures; the fresh result above is the authoritative
run with approved localhost PostgreSQL access.
