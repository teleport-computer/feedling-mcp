# Exact Attempt Ranked Flags and Resumable Scale Cleanup Design

## Status and constraints

This design responds to the first formal 3M-row scale artifact. It introduces
no service, database, queue, extension, or external analytics dependency. All
state remains in the current PostgreSQL RDS schema and the not-yet-deployed
revision `0077_llm_usage_attempt_rollups`.

The production attempt statement keeps its 3,000ms limit and the report keeps
its existing total deadline. Cleanup receives a separately named diagnostic
timeout that is not a business-query budget. The retained local fixture remains
intact while this design is reviewed.

## Failure evidence and rejected SQL-only rewrite

The formal artifact
`docs/superpowers/evidence/2026-08-03-admin-usage-attempt-rollup-scale.json`
failed two independent gates:

- provider/model filtered p50/p95: 4,243.152/4,456.927ms;
- unfiltered p50/p95: 4,840.773/5,649.255ms;
- attempt-ledger EXPLAIN: 34,617.528ms filtered and 52,134.309ms
  unfiltered;
- raw-edge guards all passed with 8,208 raw turns/calls/attempts and 8,208
  bounded runtime-job and call probes;
- the rollup branch read 178,000 attempt-dimension rows and 731,528 wide
  call-membership rows, expanded memberships to about 2.51M filtered or 3.70M
  unfiltered scope rows, and spilled large sorts to temp;
- one prefix-wide user delete inherited the 3s timeout and timed out while
  cascading into 3M memberships. Its transaction rolled back, leaving the
  full fixture intact and the business producer unstarted.

An approved read-only alternative replaced the five-scope lateral expansion
with five exact `count(DISTINCT call_id)` aggregates. Its unfiltered ordinary
fetch still took 53,078.502ms for 2,017 rows. It did not remove the distinct-call
work and is rejected. Raising 3s, relaxing p95, approximating distinct calls,
or adding external infrastructure are also rejected.

## Existing exact report contract

Attempt-ledger filters are:

- half-open UTC time range, split into complete Shanghai rollup days and raw
  partial edge days;
- exact `user_id` and cohort lane;
- independently optional **resolved** provider and resolved model;
- completeness `all`, `metered`, or `unknown`.

The report returns logical calls for overview, user, lane, requested identity,
and resolved identity, plus overview missing outer and inner ordinals. Attempt,
token, cost, and TTFT facts remain additive at the existing
`llm_usage_daily_attempt_dimensions` grain. Filter options continue to come
from those dimensions.

For selector `S`, let `M(S)` be the call-attempt rows matching every supplied
user, lane, resolved provider/model, and completeness filter. Current exact
semantics are:

- cohort count: distinct `call_id` in `M(S)`; the same count is grouped by
  user or lane for those breakdowns;
- requested breakdown: distinct
  `(requested_provider, requested_model, call_id)` in `M(S)`;
- resolved breakdown: distinct
  `(resolved_provider, resolved_model, call_id)` in `M(S)`;
- gaps: compute each call's outer/inner gap over the full logical call, then
  include those two values once when the call has any row in `M(S)`.

A failover call may therefore appear once in an unfiltered overview and once
under each of its requested/resolved identity breakdowns. A resolved identity
filter counts it once if any attempt matches. Filtering never recomputes a gap
from only the visible attempts.

## Decision: rank counts into existing dimension rows

Revision 0077 will not create
`llm_usage_daily_call_memberships` or any of its indexes, retention deletes,
TEE registry entry, or report path. Instead, the existing day builder ranks
each call's priced attempt rows and places fixed additive call/gap values on
one deterministic, matching attempt-dimension row.

The existing `llm_usage_daily_attempt_dimensions` receives exactly 32
nonnegative `BIGINT NOT NULL DEFAULT 0` columns. No new rows, tables, services,
or covering indexes are added.

### Selector modes

There are four resolved-filter modes:

| Mode | Rank partition after `call_id` | Query shape |
| --- | --- | --- |
| `all` | no resolved identity | no provider or model filter |
| `provider` | `resolved_provider` | provider only |
| `model` | `resolved_model` | model only |
| `provider_model` | both resolved fields | provider and model |

There are two stored completeness modes:

| Mode | Rank partition | Query use |
| --- | --- | --- |
| `all` | no effective-usage flag | completeness `all` |
| `effective` | add `effective_usage_known` | `metered`/`unknown`, whose existing WHERE clause selects true/false |

The cross product is eight selector/completeness modes. Provider-only,
model-only, and pair modes cannot be merged: a call can fail over between two
providers that expose the same model or between two models at one provider.
Likewise `all` cannot reuse `effective`, because one call can contain both
known- and unknown-usage attempts.

### Fixed 32-column matrix

For every selector mode in
`{all, provider, model, provider_model}` and completeness mode in
`{all, effective}`, dimensions contain:

1. `logical_calls_cohort_<selector>_<completeness>`;
2. `logical_calls_requested_<selector>_<completeness>`;
3. `missing_outer_ordinals_<selector>_<completeness>`;
4. `missing_inner_ordinals_<selector>_<completeness>`.

That is `4 selectors * 2 completeness modes * 4 metrics = 32 BIGINTs`.
Each logical-call column is independently constrained to be no greater than
the row's `attempts`; all gap columns are nonnegative.

Resolved breakdown needs no ninth family. It always sums the matching
`logical_calls_cohort_provider_model_<completeness>` column grouped by the
existing resolved provider/model. Pair ranking already assigns a call once per
resolved identity, regardless of whether the outer query supplied no resolved
filter, provider only, model only, or both.

## Stable representative rules

The day builder keeps the existing corrected, rate-resolved, and priced CTEs.
After the full-call gap CTE, a `ranked` CTE computes 16 row numbers:

- eight cohort ranks, partitioned by `call_id` plus the selected resolved and
  completeness keys;
- eight requested ranks, using the same partition plus requested provider and
  requested model.

Every rank orders by immutable unique `attempt_id`. A rank-one attempt maps to
exactly one existing dimension grain, including its cost kind and currency.
The grouped dimension insert sums a one for each rank-one logical-call flag and
sums the full-call outer/inner gap only at the corresponding cohort rank-one
row. Non-representative attempts contribute zero.

This rule handles the important counterexamples:

- failover across resolved identities: `all` cohort has one representative,
  while provider/model/pair modes have one per matching selector value;
- mixed metering: `all` has one representative across both values, while
  `effective` has one in each boolean partition;
- requested identity changes inside a call: cohort remains one, requested
  rank is one per requested identity;
- cost/rate-card split: ranking occurs before dimension aggregation, so the
  representative's flag follows its actual cost/currency row without
  duplicating the call;
- late corrections/finalization: a rebuild may move the representative to a
  different effective-usage or cost dimension, but the unique rank and the
  atomic day replacement keep totals exact;
- gaps with hidden attempts: gaps are computed from the full call before any
  selector rank and are copied once into every selector set that contains the
  call.

## Query mapping and indexes

The existing selected-dimensions WHERE clause remains authoritative. The query
maps its resolved filter shape to one selector suffix and maps completeness
`all` to the `all` columns or metered/unknown to the `effective` columns.

- overview/user/lane sum the selected cohort logical-call column and selected
  gap columns;
- requested breakdown sums the selected requested column grouped by existing
  requested identity;
- resolved breakdown sums the pair cohort column for the selected completeness
  grouped by existing resolved identity;
- attempt/token/cost/TTFT sums are unchanged.

No query contains `call_id`, `DISTINCT`, membership materialization, or a
membership scope expansion. Existing additive scope grouping may use explicit
grouping sets or bounded per-scope aggregates if the temp-table probe shows
that the present five-way dimension expansion spills.

No flag is included in an index. Index mapping remains:

- exact grain and conflict replacement: existing unique dimension grain;
- user plus day: `ix_llm_usage_daily_attempt_dimensions_user`;
- day and resolved provider/model filtering and filter options:
  `ix_llm_usage_daily_attempt_dimensions_resolved`;
- unfiltered/lane queries: the same day-leading resolved index or a bounded
  heap scan, chosen by PostgreSQL.

This avoids copying 32 counters into indexes. Performance acceptance is based
on measured rows/blocks, not an assumed plan.

## Published call-cohort invariant

Daily sums are exact across days only when one logical call belongs to one
cohort day, user, and lane. Runtime call IDs are created for one logical call
inside one turn, but revision 0077 must prove rather than assume that invariant.

Before publishing a rebuilt day, maintenance checks every selected call across
all of its runtime attempts and joined turn metrics. A call mapping to multiple
users, cohort lanes, or Shanghai local days rolls back the complete day,
keeps/creates its dirty row, records an error, and does not advance the
watermark. The bounded raw-edge branch applies the same check. Consequently a
call cannot appear in both a complete rollup day and a raw edge day, and final
rollup/raw sums remain exact.

## Raw edge, replay, correction, and retention

Raw partial days keep the current bounded runtime-job/call probes. Their
corrected/priced rows run the identical 16 ranks and emit the same 32 additive
columns in `raw_dimensions`. Rollup and raw dimensions are `UNION ALL` inputs
to the same selected-dimension aggregate. Existing guards continue to forbid a
near-full attempt scan, full-window call probes, and per-attempt rate-card
probe loops.

Attempt corrections, late finalization, rate-card replay, turn-day moves, and
dirty-generation races retain the existing dirty-day/CAS machinery. One day
transaction deletes and reinserts its dimensions, validates the generation,
and advances the watermark only after all ranked flags and additive facts are
complete. A correction that changes only tokens/cost still rebuilds the same
atomic generation, so rank flags and attempt facts are never published from
different snapshots.

Retention deletes only the existing dimension rows for each retired day. There
is no membership or second call rollup to delete. The same transaction updates
`retained_from` and `retention_pending_from`; report truncation semantics are
unchanged.

## Schema and storage effect

The formal fixture has 731,199 dimension rows. Thirty-two fixed BIGINT values
add 256 logical bytes per row, about 187.2MB of raw counter values. Allowing for
page/tuple alignment, the expected heap increase is roughly 200–250MB; existing
indexes do not grow from included flag values. In exchange, revision 0077
removes the measured 2,820,399,104-byte membership heap/index relation. The
fixture remains 731,199 dimension rows rather than the rejected relational
cuboid's estimated 17,548,776 rows and 72M absolute upper bound.

The JSON alternative would keep fewer physical columns but require dynamic
key extraction/casts across 178,000 rows, weaken nonnegative/count constraints,
introduce TOAST variability, and make plan/storage bounds data dependent. It is
rejected. The normalized cuboid is rejected because its row and rebuild
amplification conflict with the minimal-infrastructure requirement.

Because revision 0077 is not deployed, upgrade creates only the final ranked
dimension schema; no membership-to-flag data migration exists. Downgrade drops
the 32 columns and related constraints along with the rest of revision 0077.
Migration and TEE registry tests must prove that membership table/index names
are absent and that the exact 32-column schema, constraints, existing indexes,
foreign keys, upgrade idempotence, wrong-schema rejection, and downgrade state
are correct.

## Non-persistent 3M performance experiment

Before production implementation or another formal run, a read-only source
experiment uses one PostgreSQL session against the retained local fixture:

1. create session-local TEMP dimensions with the proposed 32 columns;
2. build flags with the exact production CTE/rank algorithm without changing
   retained source, rollup, watermark, or schema objects;
3. verify every old raw/membership result against ranked results for both
   formal cohorts and adversarial multi-identity/mixed-completeness cases;
4. `ANALYZE` the TEMP relation and run both cohort statements cold and warm;
5. record execution time, touched rows/blocks, temp blocks, output equality,
   TEMP rows/bytes, and build time;
6. close the session so PostgreSQL drops every TEMP object automatically.

Expected selected dimension rows are 178,000 for each formal 90-day cohort.
Acceptance requires each attempt statement and both cold/warm measurements to
be strictly below 3,000ms, no temp spill, no membership/call-ID relation in the
candidate report plan, exact output equality, and unchanged raw-edge guards.
Failure rejects this design before any retained-fixture schema mutation.

## Resumable bounded cleanup

Cleanup never inherits the 3s query setting. Each batch transaction uses an
explicit `cleanup_statement_timeout_ms`, initially 120,000ms and recorded in
evidence. It is excluded from report p95 and never increases automatically.

The loop selects at most ten prefix users in stable order and explicitly
deletes indexed child rows before users:

1. provider-attempt corrections;
2. ranked attempt dimensions;
3. Hosted V2 daily dimensions and daily users;
4. provider attempts;
5. turn metrics;
6. users.

There is no membership or cuboid cleanup. The final user delete retains FK
cascade as a safety net but no longer cascades known multi-million-row children
in one statement. Each batch commits independently. Timeout/error rolls back
only that batch; the runner may halve the user batch down to one for a bounded
retry count without raising the timeout.

After each commit an atomic evidence checkpoint records cumulative
batches/users/per-table rows, effective batch size, last/max batch elapsed,
timeout, remaining prefix users, and `fixture_cleanup` phase. It contains no
user IDs. A crash loses only the active transaction; remaining work is found by
prefix.

Normal `--resume` still requires the exact complete 3M snapshot and rejects a
partial fixture. Explicit `--recover-cleanup` first validates the dedicated
local database identity, prefix syntax, and exclusive ownership: global counts
must equal prefix counts for every user-owned source/rollup table, and only the
two named fixture watermarks/dirty rows may exist. It then resumes batches,
deletes those watermarks/dirty rows after the last user, ANALYZEs affected large
tables, and requires all twelve global counts to be zero. Mixed ownership fails
closed without deletion.

Formal timing failure still attempts cleanup and prohibits the business
producer. Cleanup failure writes parseable failure/progress evidence and also
prohibits the producer. Only exact global zero permits business proof.

## Test and acceptance contract

Strict RED/GREEN coverage must include:

- every optional user/lane/provider/model/completeness combination against raw
  canonical truth for overview, user, lane, requested identity, resolved
  identity, and gaps;
- failover across requested/resolved identities, same-model/different-provider,
  same-provider/different-model, mixed completeness, cost split, and hidden
  attempts contributing full-call gaps;
- deterministic representative movement after correction/finalization/rate
  replay without duplicate or lost logical calls;
- cross-user/lane/day call invariant violations roll back and remain dirty;
- raw-edge/rollup equality and disjoint boundary behavior;
- dirty-generation/CAS races, turn-day moves, and retention;
- exact revision 0077 schema/index/constraint/registry/downgrade tests proving
  memberships are absent;
- plan guards rejecting memberships, `call_id`, distinct aggregates, temp
  spill, full-history attempt scans, or timeout increases;
- batch timeout rolling back only one batch, bounded size reduction, atomic
  checkpoints, crash recovery, partial-resume refusal, mixed-owner refusal,
  and final exact zero;
- unchanged business-failure isolation and atomic final artifacts.

Before another formal run: pass the non-persistent 3M TEMP experiment, focused
PostgreSQL tests, full migration tests, Ruff, compileall, harness self-test, and
a small temporary-database end-to-end workflow. The retained fixture is not
cleaned until exactness, storage, maintenance, and strict sub-3s gates all pass.
