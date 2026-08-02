# Admin Usage report scale verification — 2026-08-02

## Outcome

The current raw-table report does not meet the 2-second p95 budget. It already
reaches 2.411 seconds p95 with 300,000 source rows, and the 3,000,000-row warm
path exceeded 90 seconds before five report samples could be collected.

A deletion-safe, user-grain daily rollup can meet the target only when the
independent report branches execute concurrently on one exported PostgreSQL
snapshot. The final three-connection prototype completed the full default
90-day report at **1.473 seconds p95**. The equivalent serial two-table query
was **2.981 seconds p95**. This is evidence for a design option, not production
DDL or a production implementation.

Machine-readable measurements are in
[`2026-08-02-admin-usage-scale.json`](./2026-08-02-admin-usage-scale.json).

## Reproduction harness

The opt-in harness is `scripts/perf/admin_usage_scale.py`. It defaults to:

- 3,000,000 deterministic, content-free `v2_turn_metrics` rows;
- 2,000 users over 365 days and a 90-day measured window;
- five measured runs after one explicit warm-up;
- unfiltered and `openrouter/openai/gpt-4o-mini` filtered cohorts;
- `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for every SQL statement captured
  from the real `usage_report_snapshot()` entry point;
- a hard failure when either cohort's warmed p95 is at least 2,000 ms;
- fixture cleanup scoped by a random user-id prefix unless `--keep-data` is
  explicitly supplied.

The harness validates a migrated, explicitly supplied PostgreSQL database; it
does not run migrations or point itself at a default database. Example:

```shell
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/usage_scale' \
  .venv-test/bin/python scripts/perf/admin_usage_scale.py
```

The fast self-check mutation-tests both SQL safety gates and validates the
nearest-rank p50/p95 calculation:

```shell
.venv-test/bin/python scripts/perf/admin_usage_scale.py --self-test
```

## Raw-path evidence

At 300,000 rows (74,019 inside the 90-day range), all metric statements used
the existing `v2_turn_metrics.created_at` index and retained the half-open
`created_at >= start AND created_at < end` bounds. The unfiltered warmed samples
were 2410.962, 2385.316, 2363.958, 2384.815, and 2369.048 ms (p95 2410.962
ms). Provider/model-filtered p95 was 1497.881 ms. The slowest unfiltered
statements were overview (750.960 ms) and per-user aggregation (662.383 ms).

At 3,000,000 rows, about 740,000 rows fell in the 90-day range. Capturing and
warming the full raw report exceeded 90 seconds, and execution was stopped
instead of reporting an invented five-run percentile. This does not weaken the
budget conclusion: the five-run 300,000-row result already fails the threshold.

## Rollup experiments

The experiments preserved exact token NULL semantics with sum plus known-count
pairs and preserved exact latency samples, including duplicates. Cross-user
fleet aggregates were rejected because a user deletion could not cascade
accurately. The acceptable data shape therefore remained user-grain.

Key serial results:

| Shape | Scale | p95 |
|---|---:|---:|
| One user/day/dimension table | 300k source | 1202.420 ms |
| One user/day/dimension table | 3M source | 3222.325 ms |
| Same table, 64 MB `work_mem` | 3M source | 2943.076 ms |
| Narrow CTE and reused aggregates | 3M source | 2946.680 ms |
| `GROUPING SETS` | 3M source | 8195.579 ms |
| Two user-grain tables, default report | 3M source | 2980.701 ms |
| Two user-grain tables, provider/model filter | 3M source | 1929.877 ms |

The final two-table prototype used one row per user/day for fleet totals and
one row per user/day/lane/provider/model for breakdowns. Completeness was not a
grouping dimension: each row carried overlapping all/metered/unknown counters,
token sums, and known-counts. It produced 731,984 rows in each table. With the
full realistic counter width, the tables occupied 359 MB and 430 MB versus
1,356 MB for the 3,000,000-row source.

## Same-snapshot parallel result

The successful prototype used three total PostgreSQL connections, not three
workers plus a coordinator:

1. The coordinator began `REPEATABLE READ, READ ONLY`, exported a snapshot,
   and executed overview/daily/users/distribution.
2. A second connection imported that snapshot and executed models, lanes,
   filter options, and primary provider/model.
3. A third connection imported the same snapshot and calculated exact
   provider/model latency percentiles.

Timing included transaction setup, snapshot export/import, concurrent query
execution, result reads, and commits. No report-result cache was used. Five
end-to-end wall samples were 1472.828, 1453.521, 1458.801, 1458.750, and
1447.730 ms: p50 1458.750 ms and p95 1472.828 ms. The breakdown branch was the
bottleneck at 1436.793–1468.135 ms; the core branch took 759.779–772.828 ms,
and exact provider/model latency took 341.170–348.230 ms.

Before production use, pool capacity must be evaluated because one admin
report temporarily consumes three connections. Failure to acquire the extra
connections, refresh a rollup, or read analytics must degrade only the Admin
Usage section; it must never block or fail runtime traffic. The proposed
persistence remains on the existing business RDS, refreshed asynchronously by
an idempotent worker. It requires no SQLite, trigger, synchronous hot-path
write, or new infrastructure.

## Safety and scope

The harness asserts that every captured metric query excludes content-bearing
columns such as message bodies, tool input/output, prompts, and replies. It
also asserts both time bounds on every `v2_turn_metrics` statement. No public
API, OpenAPI contract, deployment topology, or infrastructure is changed by
this verification-only commit.
