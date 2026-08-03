# Narrow Daily Call Dimensions and TEMP Feasibility Design

## Status and scope

This design replaces the rejected wide ranked-flags candidate with one narrow
daily call-fact relation. It remains inside the current PostgreSQL RDS and adds
no service, queue, extension, package, schema, or analytics dependency.

The next authorized action is a non-persistent TEMP experiment against the
retained local 3M fixture. This document does not authorize migration,
production builder, report, retention, cleanup, or deployment changes. A TEMP
failure stops this design before those changes.

The existing business contract remains unchanged:

- one attempt-ledger SQL statement per report;
- the complete statement, not each internal branch, must execute strictly
  below 3,000ms;
- exact optional user, lane, resolved-provider, resolved-model, and
  completeness filters;
- exact overview, user, lane, requested-model, resolved-model, cost, gap, and
  filter-option output;
- exact full-call outer and inner gaps;
- bounded raw partial days;
- telemetry and maintenance remain fail-open to business traffic.

## Measured baseline and rejected predecessor

The retained fixture contains 3,000,000 turns, 3,000,000 provider attempts,
731,199 existing attempt-dimension rows, and 3,000,000 membership rows.

The current membership relation measures:

| Component | Bytes |
| --- | ---: |
| Heap | 574,840,832 |
| Indexes | 2,245,369,856 |
| Total | 2,820,399,104 |

The rejected ranked-flags TEMP relation copied every attempt fact, token, cost,
TTFT, currency, and identity column before appending 32 flags. It measured
731,199 rows, a 913,424,384-byte heap, 82,026,496 bytes of diagnostic indexes,
and 995,704,832 bytes total. Its unfiltered candidate was cancelled at the
unchanged 3,000ms limit. The fixture remained unchanged and every TEMP object
was dropped when the session closed.

The failure does not reject stable ranked flags. It rejects coupling those
flags to the wide attempt-fact row and scanning that wide projection for every
report scope.

## Alternatives

### A. One narrow call-dimension table — selected

Create one `llm_usage_daily_call_dimensions` relation whose only payload is
the exact call-filter identity and 32 additive ranked flags. Attempt facts,
tokens, costs, currency, and TTFT remain in
`llm_usage_daily_attempt_dimensions`.

The report reads the two relations independently, aggregates each to report
scope, and combines only the small aggregate outputs. It never joins the two
relations at their row grain.

This minimizes stored width without multiplying tables or rows. It is the only
approach advanced to the next TEMP checkpoint.

### B. Flags on the existing wide relation plus a covering projection — rejected

Keeping flags on the existing attempt dimensions preserves the rejected wide
heap. A covering index containing 32 flags would duplicate a large payload,
increase rebuild churn, and depend on visibility-map state for index-only
behavior after daily replacement. Estimated table-plus-index storage is
roughly 1.0–1.3GB, and the failed wide TEMP scan already demonstrates the main
latency risk.

### C. Four selector-specific vertical tables — fallback only

Four tables could each store one selector's eight flags. A selected query would
scan narrower rows, but the identity and three-index set would repeat four
times, producing about 2.9M rows and more than 1GB estimated total storage.
Daily rebuild, replay, retention, cleanup, and atomic publication would span
five derived tables. This option returns to brainstorming only if all approved
query shapes for A fail the new TEMP checkpoint.

## Narrow relation contract

The persistent design, if a later plan is authorized, uses this exact grain:

1. `local_day`;
2. `user_id`;
3. `cohort_lane`;
4. `requested_provider`;
5. `requested_model`;
6. `resolved_provider`;
7. `resolved_model`;
8. `effective_usage_known`.

It stores no `attempts`, tokens, costs, TTFT samples, `cost_kind`, `currency`,
`call_id`, or per-row refresh timestamp.

The relation appends exactly 32 `BIGINT NOT NULL DEFAULT 0` columns. For each
selector in `all`, `provider`, `model`, and `provider_model`, and each
completeness mode in `all` and `effective`, it stores:

- `logical_calls_cohort_<selector>_<completeness>`;
- `logical_calls_requested_<selector>_<completeness>`;
- `missing_outer_ordinals_<selector>_<completeness>`;
- `missing_inner_ordinals_<selector>_<completeness>`.

Every flag has a nonnegative check. The narrow row deliberately has no attempt
counter against which to enforce a same-row upper bound; exactness instead
comes from deterministic ranks, atomic day replacement, adversarial tests, and
the formal source/reference comparison.

The proposed indexes are:

1. a unique constraint on the complete eight-column grain;
2. `(user_id, local_day)`;
3. `(local_day, resolved_provider, resolved_model, user_id, cohort_lane)`
   including `requested_provider`, `requested_model`, and
   `effective_usage_known`.

No index includes any of the 32 flags.

## Stable ranking and exact semantics

The corrected and priced attempt pipeline remains authoritative. Full-call
outer and inner gaps are computed before selector filtering. Sixteen stable
`row_number` values order by immutable unique `attempt_id`:

- eight cohort ranks partition by `call_id`, the selector's resolved identity,
  and optional effective-completeness boolean;
- eight requested ranks use the same partition plus requested provider and
  requested model.

Rank-one attempts contribute a one or the full-call gap to the corresponding
flag. The final group discards attempt cost/currency and merges flags at the
narrow eight-column grain. On the retained fixture, a read-only group over
that grain is exactly 731,199 rows, equal to the existing fact grain. In
general the narrow relation must never contain more rows than the corresponding
attempt-dimension relation for the same retained days.

Resolved-model output sums the provider/model cohort flag. Requested-model
output sums the requested flag. Overview, user, lane, and gaps sum the selected
cohort/gap flags. The selector suffix follows the supplied resolved filters;
completeness `all` uses `all`, while `metered` and `unknown` select the matching
boolean and use `effective`.

## Single-statement report architecture

The attempt-ledger remains one SQL statement with two independent sources:

- `selected_attempt_dimensions` reads the existing fact relation and produces
  attempts, retries, failures, tokens, TTFT, cost, and filter options;
- `selected_call_dimensions` reads the narrow call relation and produces
  logical calls and gaps.

Both apply identical day, user, lane, resolved identity, and completeness
predicates. They aggregate independently to overview, user, lane, requested,
and resolved scope. Only those small scope outputs are joined by null-safe
scope keys. A row-grain facts-to-calls join is prohibited because removal of
cost and currency makes the grains different and such a join would multiply
facts or flags.

Two query shapes belong to approach A and enter the same experiment:

- **A1 bounded unions:** five explicit per-scope aggregates for each source;
- **A2 grouping sets:** one source scan with the five scope grouping sets.

Neither shape may expand rows through a lateral five-scope values list. The
complete-day plan may contain no membership relation, `call_id`,
`count(DISTINCT ...)`, or row-grain facts-to-calls join. The raw branch may use
`call_id` only behind the existing fixed partial-day bounds and 8,208-row
guards.

## Atomic rebuild and lifecycle

A future production day rebuild would compute corrected/priced attempts, gaps,
attempt facts, and ranked call flags from one repeatable snapshot. One database
transaction would:

1. validate that every call belongs to one user, lane, and Shanghai day;
2. delete the selected day's rows from both derived relations;
3. insert attempt dimensions;
4. insert narrow call dimensions;
5. perform the existing dirty-generation CAS;
6. advance the watermark only after both inserts and the CAS succeed.

Any failure rolls back both derived relations, retains dirty work, and leaves
the watermark unchanged. Corrections, late finalization, rate-card replay, and
turn-day movement rebuild both relations even when one relation's values do
not change. This avoids publishing fact and call generations from different
snapshots.

Retention deletes both relations by bounded local day in the same transaction
before updating retained/pending boundaries. Fixture cleanup explicitly
deletes narrow call dimensions before users and never inherits the 3,000ms
business statement timeout. No membership retention or cascade remains.

Raw partial days generate ephemeral fact and narrow-call dimensions from the
same corrected/priced/ranked pipeline, then feed the same two-source aggregate
statement. No persistent raw relation is added.

## Storage estimate and hard limit

On the retained fixture, the eight-column identity composite measures an
average 123.62 bytes and a maximum 129 bytes. Thirty-two scalar BIGINT values
add 256 logical bytes, for an average logical tuple near 379.62 bytes before
page-level fill effects.

The estimated storage is:

| Component | Estimate |
| --- | ---: |
| Heap | 300–340MB |
| Unique grain index | 100–110MB |
| User/day index | about 69MB |
| Resolved/day identity index | 110–140MB |
| Total | 580–660MB |

The retained-fixture TEMP gate requires total relation plus index storage at or
below 700,000,000 bytes and strictly below 705,099,776 bytes, which is 25% of
the measured 2,820,399,104-byte membership total. Both conditions apply;
passing one does not compensate for failing the other.

## Non-persistent TEMP experiment

The experiment runs in one connection to the retained dedicated local
database. It does not edit migration or production code.

### Setup and build

1. Capture all twelve persistent table counts, source checksums for users,
   turns, attempts, corrections, and rate cards, and both watermark JSON
   witnesses.
2. Set `temp_buffers` to 8MB.
3. Create `pg_temp.admin_usage_daily_call_dimensions` with exactly the narrow
   schema and proposed indexes.
4. Build one Shanghai day at a time directly from corrected/priced source
   attempts and stable ranks. Record day count, inserted rows, total, p50, p95,
   max, and per-day timeout failures. Every day remains below the existing
   120,000ms maintenance limit.
5. `ANALYZE` the narrow relation and measure heap, each index, and total bytes.
6. Build a separate bounded TEMP raw-call relation for the two partial days.
   Require exactly 8,208 attempts and 8,208 logical calls.
7. Execute the existing mixed-provider, mixed-model, mixed-completeness,
   requested-identity, and gap adversarial matrix against TEMP facts. Every
   supported filter and output scope must equal canonical raw-call truth.

### Timing order and cache-order control

The experiment does not label any measurement as physically cold. Building
and analyzing the relation already affects PostgreSQL and operating-system
caches. The first measured sample is named
`first_execution_after_build_analyze`.

For each formal cohort, each A shape runs:

1. `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON, TIMING OFF)` as its first execution
   after build/analyze or after the other shape's first sample;
2. an immediate warm EXPLAIN with the same options;
3. an ordinary result fetch.

Shape order is interleaved to avoid giving A2 a permanent cache advantage:

- unfiltered cohort: A1 first, then A2;
- provider/model-filtered cohort: A2 first, then A1.

The old reference statement runs only after candidate timing so it cannot warm
candidate fact pages first. It receives the diagnostic 180,000ms timeout and
is used solely for exact row-set comparison.

Every A1 and A2 first, warm, and ordinary execution is individually subject to
the unchanged 3,000ms statement timeout. A shape is eligible only if all six
measurements across both cohorts are strictly below 3,000ms, all plans report
zero Temp Read Blocks and Temp Written Blocks, and both result sets are exact.

If both shapes are eligible, select the shape with the lower maximum across
all of its first, warm, and ordinary measurements. If only one is eligible,
select it. If neither is eligible, approach A fails and production work stops;
option C may then return to brainstorming. A timeout or spill is recorded as a
failed sample, not retried with a larger budget.

### Complete hard gate

Approach A passes only when all of the following are true:

- retained TEMP rows equal 731,199 and never exceed fact-dimension rows;
- heap plus indexes are at most 700,000,000 bytes and below 705,099,776 bytes;
- every day build is below 120,000ms;
- raw attempts and logical calls are both exactly 8,208;
- the adversarial matrix is exact;
- at least one query shape is eligible under the interleaved selection rule;
- both formal cohorts exactly match the old reference for every output row;
- every eligible first, warm, and ordinary sample is below 3,000ms;
- every eligible plan has zero temporary read/write blocks;
- complete-day plans contain neither memberships, call IDs, distinct-call
  aggregation, lateral scope expansion, nor row-grain facts-to-calls joins;
- recorded plan rows and blocks show bounded fact and call scans, expected to
  be about 178,000 rows each for the complete-day cohort;
- persistent counts, source checksums, and watermarks are unchanged.

The harness collects post-state in `finally`, including after syntax, timeout,
exactness, storage, or plan failure. It then closes the TEMP session and opens
a fresh connection to require zero non-temporary probe objects. Evidence is
written atomically for both pass and failure.

Only a complete pass permits a later production implementation plan. This
experiment does not itself authorize that plan or any fixture cleanup.
