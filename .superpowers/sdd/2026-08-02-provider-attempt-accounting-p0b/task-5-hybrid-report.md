# P0-B Task 5 hybrid report query switch

## Scope

Switched Admin Usage and Runtime attempt reads from the full canonical ledger to
the 0077 daily-rollup/raw-edge hybrid source.  This slice changes only report
queries and the opt-in performance harness preparation.  It does not implement
retention, run the formal 3M + 3M proof, or open/push a PR.

No SQLite, new PostgreSQL/RDS, Redis, Kafka, service, deployment unit, queue, or
CVM was added.  All persistence remains in the current business RDS.

## Exact source selection

- The exporter reads the attempt watermark and sparse dirty-day rows inside the
  same `REPEATABLE READ` transaction before exporting its snapshot.
- A Shanghai day uses attempt rollups only when it is already a full, safe P0-A
  rollup day and is also within the attempt watermark's retained/completed
  interval and absent from the attempt dirty queue.
- Every first/last partial day, P0-A unsafe day, attempt dirty/unready day, and
  day outside the attempt completed interval uses the exact half-open raw edge.
- Multiple raw edges are combined with `OR`, then user/lane predicates are
  applied with `AND`; the rolling 89+2 SQL and all four boundary parameters are
  directly tested.
- Non-Shanghai and unavailable attempt-rollup state retain exact raw fail-open
  behavior.

## One shared attempt statement

One statement materializes:

- rolled additive dimension rows plus raw-edge corrected/set-based-priced
  dimensions; and
- rolled exact call memberships plus raw-edge exact global call memberships.

Shared scope expansion aggregates overview, user, lane, requested identity,
resolved identity, cost, filter options, calls, and one-copy ordinal gaps without
repeating the correction/rate pipeline.  Nullable signed tokens remain nullable
through known counts.  Authoritative/estimated/unknown cost and missing/conflict
currency semantics remain unchanged.  Exact TTFT p50/p95 expands the stored
sorted samples and computes percentiles over the real combined sample set; it
never averages daily percentiles.  Effective rate selection uses the builder's
set-based `lead(effective_at)` ranges and has no per-attempt lateral rate-card
lookup.

Whole-turn `model_calls` is passed from the already-rendered whole-turn payload
and applied with a pure helper.  Provider/model/completeness filters keep the
existing explicit logical-coverage-unavailable reason and do not trigger another
turn-cohort scan.

## Parallel snapshot and fail-open behavior

Attempt accounting now runs first in the existing task-B importer, concurrently
with exporter core and task A.  The topology remains exporter + two importers,
with a tested maximum of three connections and no fourth checkout.  A barrier
test proves the attempt statement overlaps exporter core instead of running
after all three bins.

Every attempt statement is capped by a dedicated deadline cursor at at most
3,000 ms.  The following task-B latency statements resume through the ordinary
importer deadline cursor; a real `SHOW statement_timeout` regression proves the
remaining report deadline is restored above three seconds.  The attempt section
uses its own savepoint, so an injected attempt failure returns `attempts=None`
while sibling latency/model/lane sections remain available.

Runtime health builds the same exact Shanghai partition for its rolling hours,
uses the same hybrid statement and 3-second fail-open boundary, and preserves
whole-turn lane rows with zero recorded attempts.

## TDD evidence

Initial RED failures proved the missing partition/query helpers and that the
old report still ran the attempt statement sequentially on the exporter.  A
follow-up RED exposed a correctness bug in the first draft: two raw boundary
days were joined with `AND`, producing an empty cohort.  The fixed query uses one
parenthesized `OR` range clause.

Real PostgreSQL tests cover rollup-only, raw-only, hybrid, partial boundaries,
dirty fallback, late correction, requested/resolved filters, nullable tokens,
cost/currency conflicts, logical coverage/gaps, exact TTFT, exported-snapshot
writer races, one attempt statement, the three-connection cap, Runtime rollup,
parallel overlap, timeout restoration, and independent failure degradation.

## Performance harness preparation

The destructive dedicated-local harness now bootstraps both P0-A and attempt
rollups, validates and cleans both derived relations/watermarks/dirty work, and
records both attempt rollup relations in EXPLAIN evidence.  Its formal gate now
also requires:

- exactly one attempt statement;
- the bounded raw-edge runtime-job index;
- both 0077 rollup relations in the plan;
- no full-history attempt scan; and
- no per-attempt rate-card probe loop or full-window call-id probe loop.

The formal 3M + 3M run is deliberately deferred to the later proof slice.

## Fresh verification

- Admin Usage, Runtime health, attempt builder/reconciler/migration: **216
  passed**, 29 existing Alembic warnings.
- Ruff: passed for all five changed Python files.
- `py_compile`: passed for all five changed Python files.
- `git diff --check`: passed.

## Harness measurement-scale review fix

The formal EXPLAIN gate now classifies the complete JSON plan before truncating
the 128-node evidence display.  Its attempt-row bound is derived from the exact
raw-edge cohort selected by `query.raw_days`: half-open turn ranges are counted
first, then runtime-recorder attempts are counted through the job join, with
logical calls recorded separately for the call-probe bound.  A sequential
attempt scan or examined attempt rows above three times that exact edge cohort
(with a 100-row floor) fails; call probes are bounded relative to the exact
raw-edge logical calls.  Cleanup is also part of the formal success gate and
requires cascade verification, watermark removal, and all residual counts at
zero.

Synthetic regressions prove that forbidden rate/call probes after display node
128 and a 2,999,999-row index scan in a 3,000,000-row ledger fail, while the
rollup-plus-bounded-edge shape passes.  Empty probe lists pass only when the
complete plan contains no matching nodes.

Review-fix verification (without the formal 3M run):

- Admin Usage, Runtime health, attempt builder/reconciler/migration: **220
  passed**, 29 existing Alembic warnings.
- Ruff: passed for the harness and its tests.
- `py_compile`: passed for the harness and its tests.
- `git diff --check`: passed.
