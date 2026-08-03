# P0-B Task 5: attempt rollup structural design review

## Recommendation

Do not try to tune the current monolithic raw `attempt_ledger` statement into the
3-second budget.  The formal 3M + 3M run shows structural work amplification:

- the 90-day whole-turn cohort is found by reading all 3M `v2_turn_metrics` rows;
- the `job_id` merge reads 2,999,833 of 3M attempts to return 739,736;
- effective rate-card lookup runs 739,736 index probes;
- sequence-gap lookup performs 739,736 (unfiltered) random `call_id` probes; and
- the materialized fact set is scanned repeatedly for each report scope.

The measured attempt statement took 73.2 seconds unfiltered and 36.3 seconds
filtered.  Raising `statement_timeout`, changing `work_mem`, or adding another raw
index does not remove those operations.  The minimum structural solution is an
attempt-specific daily rollup in the **current business RDS**, maintained by the
existing Runtime V2 maintenance process, with the same full-day-rollup/raw-edge
query rule already used by P0-A.

This adds PostgreSQL tables and code in the existing deployment only.  It adds no
database instance, service, queue, deployment unit, SQLite, Redis, or Kafka.

## Required grains

### 1. `llm_usage_daily_attempt_dimensions`

One additive row per:

`(local_day, user_id, cohort_lane, requested_provider, requested_model,
resolved_provider, resolved_model, effective_usage_known, cost_kind, currency)`

`local_day`, `user_id`, and `cohort_lane` come from the joined whole-turn metric,
not from `attempt.started_at`.  This preserves today's contract: the report first
selects the whole-turn `created_at >= start AND created_at < end` cohort and then
includes all canonical attempts for those jobs.

Each row stores:

- attempts, retry/failover/failed/possibly-billed counts;
- token sums **and known counts** for input/output/reasoning/cache read/write/miss,
  so unknown remains unknown rather than becoming zero;
- authoritative/estimated/unknown-cost attempt counts and the amount for its
  currency (`cost_kind` is `authoritative`, `estimated`, or `unknown`); and
- a sorted `double precision[]` of non-null TTFT samples.

An attempt belongs to exactly one row, so additive metrics and cost do not double
count.  Corrections are folded into the effective values during day rebuild; they
are not copied into the main attempt and then added a second time.  Estimated
cost is resolved once during maintenance by a set-based effective-date join
(`lead(effective_at)` ranges), removing per-attempt LATERAL lookups from reports.

### 2. `llm_usage_daily_call_memberships`

One row per unique:

`(local_day, user_id, cohort_lane, call_id, requested_provider,
requested_model, resolved_provider, resolved_model, effective_usage_known)`

This is the exact, compact relation for non-additive semantics.  During a day
rebuild, first compute each call's global one-based ordinal gaps over **all** its
canonical attempts, then attach the same `missing_outer_ordinals` and
`missing_inner_ordinals` to each unique identity/completeness membership.

At report time, apply resolved provider/model and completeness filters, collapse
to one row per `call_id`, and then count calls/sum one copy of the gaps.  Requested
and resolved breakdowns use `count(DISTINCT call_id)` over the membership relation.
This preserves calls that have retries/failovers across multiple identities and
calls that contain both known- and unknown-usage attempts.  It also removes the
current random re-read of raw attempts by `call_id`.

The leading access index should be `(local_day, resolved_provider,
resolved_model, user_id, cohort_lane)` with the call/requested/completeness/gap
columns covered.  A second `(local_day, user_id, cohort_lane)` covering index
serves the unfiltered/user/lane path.  The 90-day formal fixture then reads only
the compact 90-day membership slice (about 740k rows), not all 3M raw attempts or
hundreds of thousands of heap lookups.

These two tables are sufficient; do not add a per-job permanent counter or a new
event stream.  If the formal gate shows the unfiltered membership scan alone is
still material, a daily call-total row is a measured follow-up, not part of the
initial schema.

### 3. `llm_usage_rollup_dirty_days`

Use a small deduplicating work table keyed by `(rollup_name, local_day)`, with
reason/generation/timestamps.  Sparse late corrections must enqueue only their
affected days; a single contiguous dirty range can accidentally force months of
raw fallback when two old days change.

All derived user-bearing rows have `user_id REFERENCES users(user_id) ON DELETE
CASCADE`, plus the existing account-reset explicit cleanup inventory.  No derived
row is anonymous or allowed to survive account deletion.

## Watermarks, locking, and fail-open behavior

Extend the existing `llm_usage_rollup_watermarks` attempt row (or add equivalent
columns in the follow-up migration) with four durable cursors:

1. `(attempt_updated_at, attempt_id)` for full-row attempt upserts;
2. `late_correction_id` for append-only correction deltas;
3. `(turn_metric_updated_at, turn_metric_id)` because cohort day/user/lane comes
   from `v2_turn_metrics`; and
4. a deterministic rate-card creation cursor, plus `replay_generation`.

Cursor discovery and insertion of affected local days into the dirty table must
commit in the same short transaction.  Never advance a cursor before its dirty
days are durable.  A rate-card insert dirties only days whose resolved
provider/model and `started_at` fall in that card's effective range; a future
version therefore cannot rewrite earlier historical pricing.

Reuse the P0-A maintenance pattern:

- a distinct `pg_try_advisory_lock` session key admits one reconciler;
- each discovery pass and each day rebuild is a short `REPEATABLE READ`
  transaction with bounded rows, bounded days, pool timeout, statement timeout,
  and cancellation checks;
- rebuilding one day atomically deletes/reinserts both rollup relations, removes
  that dirty-day claim, and advances the generation/version by CAS;
- always unlock before returning the pooled connection; close the connection if
  unlock is uncertain; and
- catch every pool/SQL/timeout/serialization error at the maintenance boundary,
  record only a safe error class, and return.  The provider recorder, reply,
  retry/failover, heartbeat, and job state paths never wait for this work.

The reconciler is default-on with an explicit false-like opt-out, runs from the
existing `serve_worker` maintenance loop, and remains independently disableable.

## Exact hybrid query

Compute one Shanghai-day partition and use a day as rolled only when **both** the
P0-A whole-turn rollup and the attempt rollup mark it complete and clean.

- A fully covered, clean local day reads both daily rollups.
- The first/last partial day and every dirty day read raw
  `v2_turn_metrics + attempts + corrections` with the exact half-open predicate
  on whole-turn `created_at`.
- The two sources are disjoint and combined before rendering.

This makes an arbitrary custom `[start, end)` exact: only complete local calendar
days are substitutable; no timestamp is rounded.  The usual rolling 90-day query
is 89 rolled days plus two raw boundary days.  Raw pricing must also use the
set-based rate-card range join, not the current per-attempt LATERAL lookup.

Whole-turn `model_calls` remains the denominator from P0-A.  Attempt rows supply
attempts/retries/failover/tokens/cache/reasoning/TTFT/possibly-billed/cost.
Provider/model/completeness filters continue to return logical coverage as
unavailable with the current explicit reason when the whole-turn denominator is
not attributable.

## Exact TTFT percentiles

Daily p50/p95 values are not composable and must not be averaged.  Store each
cell's sorted, exact non-null TTFT samples.  Fetch the full-day arrays once and
combine them with raw-edge samples using a k-way merge/order-statistic selection
for p50 and p95 for overview, user, lane, requested-model, and resolved-model
groups.  This is exact and avoids repeatedly sorting the same 90-day attempt set
inside five SQL UNION branches.  Null TTFT stays excluded exactly as today; an
empty sample set returns `NULL`.

The scale test must measure array transfer and percentile assembly as part of the
end-to-end report time.  Approximate sketches or sampled TTFT are not acceptable
for this P0 because they would degrade current semantics.

## Stale starts, corrections, and retention

- Reconcile stale `started` rows first in bounded batches; its higher revision
  and `updated_at` then naturally dirties the cohort day.
- A late correction dirties the parent attempt's cohort day through
  `attempt.job_id -> v2_turn_metrics.job_id`; rebuilding from the raw main row plus
  all append-only deltas is idempotent and auditable.
- Retention is a separate bounded `FOR UPDATE SKIP LOCKED` batch, after rollup
  refresh.  Delete parent attempts only through the parent so corrections cascade;
  prune corresponding derived days under the same published retention policy.
- Use a configurable default of at least 400 days (365-day admin preset plus
  safety overlap).  Expose `retained_from`/truncation in report coverage and never
  silently present a range outside retention as complete.  Account deletion is
  immediate and independent of age.

If product policy requires longer custom history, raise this same-RDS retention
value; do not introduce archive infrastructure in P0-B.

## Migration and implementation split

1. **Schema/red tests**: add the two user-scoped rollups, dirty-day queue,
   watermark cursors, exact constraints/index recovery, cascade/account-reset
   tests, and no-TEE/no-new-infrastructure assertions.  Prefer a new migration if
   `0076` may already have been applied anywhere; otherwise folding into the
   unshipped `0076` is mechanically smaller but operationally less safe.
2. **Pure day builder/TDD**: fixtures covering retries, failover identity changes,
   known+unknown attempts in one call, signed corrections, multi-currency cost,
   rate-card boundaries, one-based ordinal gaps, and sorted TTFT arrays.  Assert
   day rebuild equals the current raw report for every scope.
3. **Bounded reconciler/TDD**: attempt/turn/correction/rate cursors, sparse dirty
   days, bootstrap/replay, advisory contention, CAS loss, timeout, cancellation,
   stale-start ordering, retention, and injected RDS failures.  Assert no error
   escapes and provider-visible results/retry counts are unchanged.
4. **Hybrid report/TDD**: rollup-only, raw-only, 89+2 hybrid, arbitrary timezone
   boundary, dirty-day fallback, requested/resolved filters, user/lane,
   completeness, unknown token/cost, coverage-unavailable reason, gap equality,
   and exact TTFT equality against raw.
5. **Proof**: run migration/account-reset tests, failure/load injection, full
   non-API suite, and the formal 3M whole-turn + 3M attempt harness.  Passing means
   every measured cohort uses exactly one bounded attempt rollup/raw statement,
   no full-history raw attempt scan or per-attempt rate-card/call probe appears in
   EXPLAIN, cleanup is zero, and end-to-end rolling-90d p95 is strictly below
   3000 ms.

## Decision gate

Proceed with this rollup design.  Do not accept a timeout increase as the Task 4
performance fix.  If the first implementation misses the gate, use its EXPLAIN
to decide whether the compact call-membership scan needs a measured daily
call-total accelerator; do not pre-emptively add infrastructure or weaken report
semantics.
