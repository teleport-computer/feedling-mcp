# P0-B Task 5 hybrid query independent review

Reviewed commits: `0e51de20`, review fixes `2c5529c8` and `548d70d2`

Review mode: read-only, two stages (specification first, code quality second).

## Stage 1 — Specification review: PASS

The hybrid read matches the P0-B/0077 contracts reviewed:

- Attempt rollups are selected only from the safe intersection of the P0-A
  full-day partition, the attempt completed/retained interval, and clean days.
- Every unsafe, dirty, unready, retained-out, or partial day falls back to a
  disjoint half-open raw range. Multiple raw ranges are parenthesized with
  `OR`; user/lane filters are applied outside that range union.
- Rollup and raw facts have matching additive/cost/token/TTFT/member payloads.
  Corrections remain signed deltas; nullable token knownness, cache pricing,
  missing/conflicting currency, and authoritative/estimated/unknown cost retain
  the canonical raw semantics.
- Exact logical-call membership and ordinal gaps are preserved across filters.
  TTFT percentiles expand the exact stored samples rather than averaging daily
  percentiles.
- Rollup-only SQL does not touch raw attempt relations; dirty/raw edges reuse
  the shared set-based correction and rate-card pipeline.
- Usage keeps one attempt statement in the exported repeatable-read snapshot,
  patches the whole-turn denominator after the parallel result, merges resolved
  identity filter options, and keeps provider/model/completeness coverage
  explicitly unavailable when the denominator cannot be attributed.
- The attempt importer genuinely overlaps exporter core in the existing
  exporter + two-importer topology. The attempt savepoint and 3-second cap are
  isolated, the following task-B deadline is restored, and the writer-race test
  covers both a new turn and its attempt after snapshot export.
- Runtime uses the same partition/query and preserves whole-turn lanes with zero
  recorded attempts while degrading attempt telemetry independently.

## Stage 2 — Code quality review: PASS

The sole Important performance-gate finding is closed.

Files/lines:

- `scripts/perf/admin_usage_scale.py:481-608`
- `tests/test_admin_usage.py:232-394`

The final implementation now:

- classifies the complete JSON plan before truncating the 128-node display;
- derives the baseline from the exact half-open raw-edge turn cohort and its
  runtime-recorder attempts/logical calls;
- keeps aggregate `3 * edge attempts` / `2 * edge calls` bounds with fixed
  floors for harmless multi-node plan noise;
- independently caps the largest single attempt scan and call probe without
  those floors;
- rejects a single scan reaching the recorded near-full threshold whenever the
  expected edge is smaller than the ledger;
- requires both rollup relations, the runtime-job index, bounded rate/call
  probes, the latency budget, one attempt statement, and successful zero-residue
  cleanup for both formal cohorts.

Direct synthetic evidence from `548d70d2`:

```text
small full: total=50 edge=1 single=50 -> FAIL
small near-full: total=50 edge=1 single=49 -> FAIL
small legitimate: total=50 edge=1 scans=1+3, edge calls=2/probe loops=3 -> PASS
formal full: total=3,000,000 edge=16,000 single=2,999,999 -> FAIL
formal bounded: total=3,000,000 edge=16,000 examined=32,500 -> PASS
```

The single-node limits close the floor-dominant false PASS without making a
legitimate small multi-node edge plan fail. For the formal 3M fixture, the
aggregate limit remains three times the measured raw edge, while any individual
node is held to the same unfloored edge-relative limit; this is conservative but
allows the expected job lookup plus exact call-gap lookup shape.

## Verification

Fresh command:

```text
FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres \
  .venv-test/bin/python -m pytest \
  tests/test_admin_usage.py tests/test_v2_runtime_health.py \
  tests/test_provider_attempt_rollup.py \
  tests/test_provider_attempt_rollup_reconciler.py \
  tests/test_provider_attempt_rollup_migration.py -q
```

Final result after `548d70d2`: **221 passed**, 29 Alembic deprecation warnings,
exit 0.

Additional direct threshold probe: all five small/formal reject/accept cases
listed above matched the expected verdict.

No code was modified by this review beyond this force-tracked report.
