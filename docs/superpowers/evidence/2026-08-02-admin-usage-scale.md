# Admin Usage report production scale verification — 2026-08-02

## Outcome

The production Admin Hosted V2 Usage report passes the 3,000 ms p95 gate at
the required 3,000,000-row source scale. The fresh, unfiltered 90-day report
measured **2,534.168 ms p95**; the provider/model-filtered report measured
**2,104.938 ms p95**. Both results came from the real rolling
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
| Unfiltered | 2441.852, 2404.135, 2434.798, 2511.754, 2534.168 | 2441.852 | **2534.168** | < 3000 |
| `openrouter/openai/gpt-4o-mini` | 2079.195, 2079.272, 2085.048, 2072.711, 2104.938 | 2079.272 | **2104.938** | < 3000 |

The deterministic fixture contained 3,000,000 content-free
`v2_turn_metrics` rows across 2,000 users and 365 days. The measured 90-day
half-open interval contained 739,736 source rows. Production rollup bootstrap
completed with no dirty range or last error and produced 731,199 rows in each
user-grain rollup table:

| Relation | Rows | Relation bytes | Index bytes | Total bytes |
|---|---:|---:|---:|---:|
| `v2_usage_daily_users` | 731,199 | 399,343,616 | 104,292,352 | 503,767,040 |
| `v2_usage_daily_dimensions` | 731,199 | 499,113,984 | 195,420,160 | 694,689,792 |

## Production query shape

The report uses one `REPEATABLE READ, READ ONLY` exported snapshot and exactly
three concurrent PostgreSQL connections. Work was measured independently and
then assigned to three approximately equal bins:

1. Exporter: core totals/users plus lane latency.
2. Importer A: fleet user-day distribution, model rows, filter options, and
   primary provider/model.
3. Importer B: daily rows, lane rows, and model latency.

The fixed query window was the half-open interval
`[2026-05-04T12:30:00Z, 2026-08-02T12:30:00Z)`. It was intentionally not
aligned to Asia/Shanghai midnight: 89 complete local days
(`2026-05-05` through `2026-08-01`) came from rollups, while partial local days
`2026-05-04` and `2026-08-02` came from authoritative raw metrics. This is the
real rolling preset shape; the earlier all-full-day measurement is not the
acceptance gate.

Every optional task executes in its own savepoint. A task error rolls back and
degrades only that report section; an exhausted shared deadline may degrade
all optional bins while preserving bounded cancellation and connection
release. Report admission is fail-fast, and analytics failures do not affect
runtime traffic.

## SQL and correctness gates

Both final cohorts reported `hybrid-parallel` coverage with exactly the expected
89 rollup days and two raw partial days. Captured SQL included the
`v2_turn_metrics` branch, and the harness verified that its presence matched the
partition. It additionally verified:

- half-open time bounds on every captured raw metric branch;
- no content-bearing prompt, reply, message, or tool columns in reporting SQL;
- direct scalar half-open raw ranges used `ix_v2_turn_metrics_created_at`
  Bitmap Index Scans for `[2026-05-04T12:30Z, 2026-05-04T16:00Z)` and
  `[2026-08-01T16:00Z, 2026-08-02T12:30Z)`;
- exact latency percentile SQL was captured and explained;
- rollup/raw and filtered mixed-partition parity in the database-backed suite;
- `completeness=unknown` retains exact daily and user-day distributions;
- at most three database connections are active per report.

The final unfiltered overview plan executed in 1,723.485 ms and read 178,000
rollup user-day rows plus 8,208 bounded raw-edge rows. The filtered overview
plan executed in 1,220.051 ms and read 121,040 rollup dimension rows plus 5,596
matching raw-edge rows. Exact-latency plans executed in 511.423 ms unfiltered
and 592.995 ms filtered; both combined the production dimension-grain index
with the same bounded raw-edge index scans.

## Environment and infrastructure scope

The gate ran only against the dedicated local database
`feedling_usage_scale_task4d` on `127.0.0.1:55432`. It was empty before the
fixture, used PostgreSQL's default **4 MB `work_mem`**, and never touched a
remote or production RDS. The final implementation persists analytics in the
existing business PostgreSQL/RDS schema and adds no SQLite, trigger,
synchronous hot-path write, cache service, or other infrastructure.

The production implementation and performance harness were verified with:

- 95 passing Admin Usage tests;
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
