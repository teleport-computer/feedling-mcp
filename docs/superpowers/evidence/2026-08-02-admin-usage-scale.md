# Admin Usage report production scale verification — 2026-08-02

## Outcome

The production Admin Hosted V2 Usage report passes the 3,000 ms p95 gate at
the required 3,000,000-row source scale. The fresh, unfiltered 90-day report
measured **1,856.380 ms p95**; the provider/model-filtered report measured
**1,311.303 ms p95**. Both results came from the real
`usage_report_snapshot()` entry point after bootstrapping the production rollup
tables from an empty, migrated database.

The real rolling 90-day, 3M-row gate was amended from p95 `<2s` to p95 `<3s`
with explicit user approval on 2026-08-03. The report remains default-on and
fail-open on the existing business RDS, with no new infrastructure or hot-path
write introduced by this threshold adjustment.

Machine-readable samples, coverage metadata, relation sizes, captured SQL
plans, and cleanup counts are in
[`2026-08-02-admin-usage-scale.json`](./2026-08-02-admin-usage-scale.json).

## Fresh standard gate

The final run used one explicit warm-up followed by five measured executions
per cohort. p95 is the nearest-rank value, so with five samples it is the
maximum measured value.

| Cohort | Measured samples (ms) | p50 | p95 | Budget |
|---|---|---:|---:|---:|
| Unfiltered | 1856.258, 1846.012, 1847.883, 1838.799, 1856.380 | 1847.883 | **1856.380** | < 3000 |
| `openrouter/openai/gpt-4o-mini` | 1285.730, 1295.001, 1288.769, 1311.303, 1303.678 | 1295.001 | **1311.303** | < 3000 |

The deterministic fixture contained 3,000,000 content-free
`v2_turn_metrics` rows across 2,000 users and 365 days. The measured 90-day
half-open interval contained 739,736 source rows. Production rollup bootstrap
completed with no dirty range or last error and produced 730,000 rows in each
user-grain rollup table:

| Relation | Rows | Relation bytes | Index bytes | Total bytes |
|---|---:|---:|---:|---:|
| `v2_usage_daily_users` | 730,000 | 398,688,256 | 80,699,392 | 479,518,720 |
| `v2_usage_daily_dimensions` | 730,000 | 498,352,128 | 93,773,824 | 592,281,600 |

## Production query shape

The report uses one `REPEATABLE READ, READ ONLY` exported snapshot and exactly
three concurrent PostgreSQL connections. Work was measured independently and
then assigned to three approximately equal bins:

1. Exporter: core totals/users plus lane latency.
2. Importer A: fleet user-day distribution, model rows, filter options, and
   primary provider/model.
3. Importer B: daily rows, lane rows, and model latency.

Before the fresh gate, retained-fixture warmed medians were 1,547.024 ms,
1,490.456 ms, and 1,547.066 ms for those three bins. Two retained five-run
checks also passed before the database was emptied: unfiltered p95 values were
1,617.516 ms and 1,567.045 ms; filtered p95 values were 1,299.266 ms and
1,166.240 ms.

Every optional task executes in its own savepoint. A task error rolls back and
degrades only that report section; an exhausted shared deadline may degrade
all optional bins while preserving bounded cancellation and connection
release. Report admission is fail-fast, and analytics failures do not affect
runtime traffic.

## SQL and correctness gates

Both final cohorts reported `hybrid-parallel` coverage with all 90 days served
from rollups, no raw days, and no `v2_turn_metrics` branch in captured report
SQL. The harness additionally verified:

- half-open time bounds on every captured raw metric branch;
- no content-bearing prompt, reply, message, or tool columns in reporting SQL;
- exact latency percentile SQL was captured and explained;
- rollup/raw and filtered mixed-partition parity in the database-backed suite;
- `completeness=unknown` retains exact daily and user-day distributions;
- at most three database connections are active per report.

The final unfiltered exact-latency plan executed in 475.407 ms; the filtered
plan executed in 294.977 ms. Both plans used the production
`v2_usage_daily_dimensions` grain index over the requested day range.

## Environment and infrastructure scope

The gate ran only against the dedicated local database
`feedling_usage_scale_task4d` on `127.0.0.1:55432`. It was empty before the
fixture, used PostgreSQL's default **4 MB `work_mem`**, and never touched a
remote or production RDS. The final implementation persists analytics in the
existing business PostgreSQL/RDS schema and adds no SQLite, trigger,
synchronous hot-path write, cache service, or other infrastructure.

The production implementation and performance harness were verified with:

- 92 passing Admin Usage tests;
- 303 passing Admin/Data Track/Runtime/Migration tests;
- Ruff, Python compilation, and `git diff --check`;
- an independent review with no Critical, Important, or Minor findings.

## Reproduction and cleanup

The opt-in harness refuses remote/shared databases and accepts only a dedicated
local database named `feedling_usage_scale_<name>` on port 55432:

```shell
.venv-test/bin/python scripts/perf/admin_usage_scale.py \
  --database-url \
  'postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d' \
  --output docs/superpowers/evidence/2026-08-02-admin-usage-scale.json \
  --precondition-note \
  'Dedicated empty local PostgreSQL database on 127.0.0.1:55432; default 4MB work_mem; no remote RDS touched'
```

After measurement and EXPLAIN capture, deleting the 2,000 fixture users
cascaded through the 3,000,000 source rows and both rollup tables. The harness
then removed its watermark and verified residual counts of zero for users,
source metrics, both rollup tables, and watermark state.
