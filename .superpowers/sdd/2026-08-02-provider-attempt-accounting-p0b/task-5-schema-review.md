# P0-B Task 5 schema independent re-review

Reviewed commits:

- `bc1c5dd2` — `feat(accounting): add attempt usage rollup schema`
- `27e6c245` — `fix(accounting): harden attempt rollup migration`

## Final verdict

- **Spec compliance: PASS**
- **Code quality: PASS**

No Critical, Important, or Minor findings remain in this schema slice.

## Stage 1: spec compliance

The combined commits remain compliant with the accepted P0-B design:

- They add only tables, columns, constraints, functions, and indexes in the
  existing current-business-RDS Alembic chain. No SQLite, Redis, Kafka, new RDS
  instance, service, queue, deployment unit, or CVM is introduced. The three
  new tables remain explicitly `SKIP` in the TEE registry
  (`backend/tee_shadow/table_registry.py:201-212`).
- `0077_llm_usage_attempt_rollups` follows the existing 0076 migration rather
  than rewriting it.
- `llm_usage_daily_attempt_dimensions` retains the required exact additive
  grain and attempt/retry/failover/failure/possibly-billed, signed token,
  known-count, cost-kind/currency, and exact sorted TTFT fields.
- `llm_usage_daily_call_memberships` retains the exact requested/resolved/
  completeness membership grain needed for distinct-call, filtered coverage,
  and one-copy global ordinal-gap semantics.
- Authoritative signed corrections and unknown currency remain representable;
  estimated cost remains nonnegative/currency-qualified; unknown cost remains
  zero with NULL currency.
- Sparse dirty days are deduplicated by `(rollup_name, local_day)`, and the
  independent attempt-upsert, correction, turn-metric, and deterministic
  rate-card cursors remain durable alongside the 0076 cursors.
- Both user-bearing tables remain `NOT NULL` user FKs with `ON DELETE CASCADE`,
  user-leading deletion indexes, and explicit account-reset cleanup.
- Test and prod RDS both run PostgreSQL 17.9, so the PostgreSQL features used by
  the migration are supported. A fresh read-only check showed both live RDS
  migration chains are still at `0073_merge_tail_anchor_deepseek`; therefore no
  old 0077 schema phase has been applied live and there is no live residual-ID
  compatibility concern from the pre-fix commit.

As before, day building, reconciliation, hybrid reads, retention, and the 3M +
3M performance proof are intentionally outside this schema-only slice and
remain required in subsequent Task 5 work.

## Stage 2: previous findings closure

### Important — closed

The unused surrogate `BIGSERIAL` primary keys were removed from both rollup
tables (`backend/alembic/versions/0077_llm_usage_attempt_rollups.py:55-55,118-118`).
The exact natural-grain unique constraints remain, while the redundant heap
column, sequence, and primary-key btree no longer exist. Tests explicitly assert
that neither table has an `id`, primary key, or owned sequence and that replaying
the schema phase does not create one
(`tests/test_provider_attempt_rollup_migration.py:114-154,339-356`). This closes
the high-cardinality membership storage/write-amplification finding.

### Minor — closed

Index ownership validation now enumerates every same-name index across schemas,
requires both the index and table to be in `public`, requires exactly one result,
and then verifies the exact intended relation and definition
(`backend/alembic/versions/0077_llm_usage_attempt_rollups.py:282-310`). Concurrent
index DDL also qualifies target tables and destructive drops with `public`.
The cross-schema regression proves upgrade refuses an ambiguous foreign-schema
index without deleting it
(`tests/test_provider_attempt_rollup_migration.py:400-429`).

Downgrade now preflights ownership of every index before the first autocommit
drop, so an error cannot leave an earlier subset already deleted. It drops only
present, validated public-owned names
(`backend/alembic/versions/0077_llm_usage_attempt_rollups.py:325-333`). The new
regression replaces one owned index with a same-name index on `public.users`,
asserts downgrade refuses, and proves the unrelated index survives
(`tests/test_provider_attempt_rollup_migration.py:432-459`). This closes the
destructive downgrade finding.

## Regression assessment

- Correct same-relation invalid/wrong definitions are still recoverable.
- Same-name indexes on another public relation or another schema are refused.
- Schema-phase replay remains idempotent and does not recreate surrogate
  storage.
- Downgrade preserves all 0076 watermark columns and removes only 0077-owned
  columns/tables/indexes/functions.
- Public qualification does not change the intended current RDS target and
  removes dependence on `search_path` for concurrent index operations.
- The natural-grain unique indexes, user deletion indexes, resolved-filter
  index, and cohort covering index remain intact; no replacement write
  amplification was introduced by the fix.

## Fresh verification

```text
FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres \
  .venv-test/bin/python -m pytest \
  tests/test_provider_attempt_rollup_migration.py \
  tests/test_account_reset_purges_all_tables.py \
  tests/test_tee_table_registry.py \
  tests/test_v2_jobs_migration.py -q

63 passed, 56 warnings in 3.74s
```

The warnings are the existing Alembic `path_separator` deprecation warnings.
The run provisioned real throwaway PostgreSQL 16 RDS/TEE test databases.
