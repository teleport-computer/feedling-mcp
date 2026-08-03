# P0-B Task 5 schema slice report

## Scope

Added follow-up migration `0077_llm_usage_attempt_rollups` without modifying
the already-existing 0076 migration.  This slice is schema-only: it adds the
attempt daily dimension rollup, exact call-membership relation, sparse dirty-day
queue, durable source cursors, and their account-reset / RDS-only inventories.
It does not implement the day builder, reconciler, hybrid report, or retention
worker.

No SQLite, Redis, Kafka, RDS instance, service, deployment unit, or other
infrastructure was added.  All three tables are in the current business RDS and
are explicitly registered `SKIP` for TEE shadow replication.

## TDD evidence

Initial RED:

- `test_0077_installs_exact_attempt_rollup_grains_and_durable_cursors` failed
  because Alembic could not locate `0077_llm_usage_attempt_rollups`.
- `test_attempt_rollup_tables_stay_in_current_rds_only` failed because the three
  new tables were absent from the RDS/TEE registry.

Semantic follow-up RED:

- the watermark lacked an independent `attempt_updated_id` cursor;
- authoritative signed cost with `currency IS NULL` was rejected;
- signed token rollup sums were rejected.

GREEN preserves the raw report semantics: authoritative cost and token sums may
contain signed correction results, authoritative currency may be unknown,
estimated cost remains nonnegative with a three-letter currency, and unknown
cost is zero/NULL-currency.  The 0076 `(attempt_finished_at, attempt_id)` cursor
is retained while 0077 adds an independent
`(attempt_updated_at, attempt_updated_id)` cursor.

## Schema invariants

- `llm_usage_daily_attempt_dimensions` has one deletion-safe row at the exact
  requested/resolved/completeness/cost grain, token known counts, classified
  cost counts, and exact sorted finite TTFT arrays.  Its natural grain is the
  unique key; there is no surrogate id, primary key, or sequence.
- `llm_usage_daily_call_memberships` has one row at the exact identity and
  completeness membership grain.  Gap fields count missing **one-based**
  ordinal positions; zero means no positions are missing.  Its natural grain is
  also the unique key, with no surrogate id, primary key, or sequence.
- `llm_usage_rollup_dirty_days` deduplicates sparse dirty work by
  `(rollup_name, local_day)` and rejects negative generations.
- `llm_usage_rollup_watermarks` now has independent attempt-upsert, correction,
  turn-metric, and deterministic rate-card cursors plus replay/bootstrap/version
  state.  Downgrade removes only 0077-owned columns and preserves all 0076
  cursors.
- User-bearing rollup rows are `NOT NULL` foreign keys with
  `ON DELETE CASCADE`, have leading user indexes for deletion, and are also
  explicitly deleted by the account-reset DB belt.
- Concurrent source/report indexes validate relation, key/include columns,
  predicate, uniqueness, access method, and validity against the explicit
  `public` index/table namespaces.  Same-relation wrong definitions are rebuilt;
  an unrelated same-name index in another relation or schema is refused rather
  than dropped.  Downgrade preflights every index owner before its first
  concurrent drop, preventing partial or unrelated deletion.

## Verification

- Focused PostgreSQL suite:
  `tests/test_v2_jobs_migration.py tests/test_provider_attempt_rollup_migration.py tests/test_account_reset_purges_all_tables.py tests/test_tee_table_registry.py`
  — **63 passed**.
- New 0077 migration tests — **10 passed**.
- Ruff — passed for every changed Python file.
- `py_compile` — passed for every changed Python file.
- `git diff --check` — passed.
