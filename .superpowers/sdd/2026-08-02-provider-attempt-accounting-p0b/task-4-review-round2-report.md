# Task 4 formal review round 2 report

Date: 2026-08-03

## Outcome

The round-1 review findings are addressed in code and regression tests:

- Estimated-cost knownness now depends on each cache category's rate
  differential from regular input. A missing cache read/write/miss allocation is
  allowed only when that category rate equals the regular input rate; a zero
  cache rate remains a real differential rate. The miss rate is no longer
  treated as an implicit fallback sentinel.
- The cost partition keeps normalized `input_tokens` as the total prompt token
  count containing cache tokens, then charges disjoint regular/read/write/
  non-write-miss categories. Output and reasoning remain separate normalized
  counters; no overlap assumption was added for them.
- Usage now gets resolved provider/model filter choices from the same
  attempt-ledger SQL statement as attempt accounting. Both raw and hybrid paths
  execute one savepoint-isolated attempt statement inside their existing
  repeatable-read snapshot.
- Migration 0076 adds a recoverable partial btree on `job_id` for canonical
  runtime rows:

  ```sql
  CREATE INDEX CONCURRENTLY ix_llm_provider_attempts_runtime_job
  ON llm_provider_attempts (job_id)
  WHERE source='runtime_recorder' AND job_id IS NOT NULL
  ```

  Exact definition, same-table wrong-definition rebuild, and other-relation
  same-name refusal are covered.
- Runtime attempt aggregation sets a transaction-local 3000 ms statement
  timeout immediately before the attempt query. Timeout or other attempt
  failures remain fail-open: legacy lane totals survive while `attempt_lanes`
  becomes unavailable.
- Representative Usage and Runtime joins are EXPLAIN-tested against 12,000
  attempt rows and must select `ix_llm_provider_attempts_runtime_job` without a
  sequential scan of `llm_provider_attempts`.
- The scale harness now supports an explicit attempt fixture. Its legacy default
  remains metric-only (`--attempt-rows` defaults to 0); the P0-B formal gate must
  opt into 3,000,000 deterministic canonical attempts. Attempts use canonical
  UUID-shaped IDs, safe call IDs, one-based ordinals, revision 2, completed/
  complete state, zero corrections, bounded 100k-row inserts, FK-cascade
  cleanup, and attempt/index/scan evidence fields.

No reconciler or retention work was added.

## Verification

- `tests/test_admin_usage.py`: **120 passed**
- `tests/test_v2_jobs_migration.py`: **42 passed**
- `tests/test_v2_runtime_health.py` excluding the separately owned EXPLAIN
  case: **50 passed, 1 deselected**
- Controller rerun of
  `test_usage_and_runtime_attempt_joins_use_runtime_job_index`: **1 passed**
- Focused Runtime timeout/fail-open test: **1 passed**
- Focused migration exact/recovery tests: **3 passed**
- Focused raw+hybrid Usage attempt statement-count tests: **2 passed**
- Scale harness row-contract, tiny canonical fixture cleanup, and self-test:
  **3 passed**
- Ruff, `compileall`, and `git diff --check`: passed

Existing Alembic `path_separator` deprecation warnings remain unchanged.

## Controller-owned formal scale command

This round prepared the attempt-inclusive fixture but did not claim performance
results. Run the approved rolling 90-day gate in the dedicated local database:

```bash
.venv-test/bin/python scripts/perf/admin_usage_scale.py \
  --database-url 'postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d' \
  --output docs/superpowers/evidence/2026-08-03-admin-usage-attempt-scale.json \
  --rows 3000000 \
  --attempt-rows 3000000 \
  --users 2000 \
  --history-days 365 \
  --runs 5
```

The harness records the exact attempt row count, rows matched by the 90-day job
cohort, valid partial-index definition, one attempt-ledger statement per cohort,
attempt relation scan nodes, whether the runtime-job index was selected, and
post-cascade residual counts.
