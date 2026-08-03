# P0-B Task 4 — Usage accounting switches to provider attempts

## Delivered

- Preserved `v2_turn_metrics` as whole-turn truth for turns, terminal outcomes,
  reference cohorts, and the `model_calls` reconciliation denominator.
- Added a canonical `attempts` Usage payload read from rev-latest
  `llm_provider_attempts` rows with `source='runtime_recorder'`. Calls,
  physical attempts, retry/failover/failure attempts, reasoning/cache/input/
  output tokens, TTFT, possibly-billed state, and attempt identities now come
  from this ledger section.
- Applied append-only correction deltas over main-row values. A field is known
  when either the main field or at least one correction delta is non-NULL; its
  value is `coalesce(main, 0) + sum(delta)`. Main revisions do not materialize
  correction deltas, so the report must keep this addition and must not later
  add a second materialization path.
- Classified cost without collapsing uncertainty: main cost or cost correction
  is authoritative adjusted cost; otherwise known usage uses the immutable
  resolved-provider/model rate card effective at `started_at`; otherwise cost
  remains unknown/NULL. Estimated input cost partitions normalized effective
  input into mutually exclusive regular, cache-read, cache-write, and
  non-write cache-miss categories before applying rates, so cached input is not
  billed twice. A later rate-card version does not rewrite an earlier attempt
  estimate.
- Added requested and resolved provider/model breakdowns plus per-user and
  per-lane attempt breakdowns. Provider/model report filters intentionally
  target resolved attempt identity; requested identity remains separately
  visible.
- Added `logical_call_coverage = distinct recorded call_id / whole-turn
  model_calls`. Physical attempt count is never used as this numerator.
  Missing outer and inner ordinal sequences are exposed separately as
  `attempt_sequence_gaps`.
- Gap detection follows the production one-based outer/inner ordinal contract.
  Provider/model/completeness filters select logical calls, then gap detection
  examines the full time/user/lane attempt cohort for those calls so a filtered
  retry or failover cannot create a false gap.
- Kept cost rows in explicit currency buckets. Currencyless unknown attempts
  are not folded into a known currency, and conflicting main/correction
  currencies keep authoritative cost in an unknown-currency bucket rather
  than inheriting the matched rate-card currency.
- Kept the existing exported repeatable-read snapshot, three-connection cap,
  process/RDS admission, statement deadline, raw fallback, and importer
  settlement behavior. The attempt query runs on the exporter inside an
  isolated savepoint; a ledger failure yields `attempts=None` without hiding
  whole-turn, daily, user, model, lane, filter, or reference-cohort sections.
- Extended Runtime lane data with canonical `attempt_lanes` from the same
  repeatable-read transaction as its whole-turn denominator. The Runtime page
  uses attempt input/output and logical-call coverage when available, and
  labels the legacy whole-turn fallback when the attempt section is unavailable.
- Updated operator copy and tables to distinguish Whole-turn truth from the
  Provider-attempt ledger, requested from resolved identity, logical coverage
  from ordinal gaps, possibly-billed attempts, and authoritative/estimated/
  unknown cost. Dynamic identities and currency labels remain HTML escaped.

## TDD evidence

1. RED: three PostgreSQL Usage tests failed with `KeyError: 'attempts'` before
   the ledger payload existed. They cover correction sums, retry/failover and
   failure attempts, reasoning/cache/TTFT/possibly-billed values, effective
   rate cards, requested/resolved identity, logical coverage, ordinal gaps,
   and all-unknown NULL behavior.
2. GREEN: the ledger tests passed after the exporter-snapshot aggregate and
   payload were added.
3. RED: the Usage renderer lacked Whole-turn/attempt source labels, cost
   classification, reconciliation panels, and escaped requested/resolved
   tables. The focused HTML test failed on the missing `Whole-turn truth`
   contract.
4. GREEN: the renderer test passed after the accounting panels and source
   language were added.
5. RED: Runtime lane accounting failed with `KeyError: 'attempt_lanes'` before
   the canonical lane projection existed.
6. GREEN: Runtime lane tests passed after correction-aware attempt aggregation
   and logical-call reconciliation were added.
7. Review-fix RED: five PostgreSQL assertions failed on the initial
   implementation: one-based ordinals were treated as zero-based, identity
   filtering created false sequence gaps, normalized input/cache categories
   overlapped in estimates, authoritative currency conflicts inherited the
   rate-card currency, and currencyless unknown cost was folded into USD.
8. Review-fix GREEN: the attempt cohort/facts split, one-based gap formula,
   disjoint billing categories, and explicit currency grouping made all five
   regression tests pass. A separate Runtime RED/GREEN also proved a
   whole-turn lane with no recorded attempts remains present as `0 / N`
   logical-call coverage with token fields unknown.

## Verification

- `tests/test_admin_usage.py tests/test_v2_runtime_health.py` against the local
  disposable PostgreSQL fixture: **153 passed**. Alembic emitted its two
  existing `path_separator` deprecation warnings.
- `ruff check` on all five touched Python files: passed.
- `python3 -m py_compile` on all touched production and test modules: passed.
- `git diff --check`: passed.
- The exported-snapshot writer race test now inserts both a whole-turn metric
  and an attempt after export; neither appears in the report snapshot.
- The existing three-connection, admission, importer timeout/settlement,
  section-degradation, raw fallback, rollup parity, payload, and HTML safety
  tests remain green.

## Self-review / boundaries

- Confirmed main-row revision and correction revision are independent: main
  stores the latest recorder observation; correction rows are late-accounting
  deltas and remain additive audit facts. No correction was materialized back
  into the main table.
- Confirmed unknown token fields use SQL `sum(nullable_field)` and remain NULL
  when every source is unknown. No `coalesce(sum(...), 0)` was introduced for
  usage or monetary values.
- Confirmed normalized prompt/input includes cache usage and is never added to
  cache buckets wholesale. The estimate partitions cache categories first;
  the explicit regression fixture would be `0.00041000` with the old
  overlapping formula and is `0.00025000` with the billing partition.
- Confirmed production ordinals start at one and gaps are computed before
  resolved-identity/completeness filtering for every selected logical call.
- Confirmed authoritative, estimated, and unknown currency classification is
  chosen from the cost source itself; a rate-card currency never labels an
  authoritative cost whose main/correction currencies conflict.
- Confirmed rate-card selection is resolved identity plus `effective_at <=
  started_at`, ordered latest-first. Future versions are excluded by test.
- Confirmed legacy `source='legacy_best_effort'` attempts are excluded from
  canonical totals.
- No reconciler, rollup retention/pruning, load harness, new database/service,
  public API contract, or documentation-site change was added; those remain
  outside Task 4.

## 2026-08-03 Phase 1 performance diagnosis (no implementation)

Evidence source:
`docs/superpowers/evidence/2026-08-03-admin-usage-attempt-scale.json`.
The fixture was 3,000,000 `v2_turn_metrics` rows plus 3,000,000 canonical
attempt rows over 365 days. The rolling 90-day turn/job cohort contained
739,736 rows (24.658%). The dedicated PostgreSQL instance used `work_mem=4MB`,
`shared_buffers=128MB`, `effective_io_concurrency=1`, and
`random_page_cost=4`. The attempt table occupied 1.183 GB, its indexes 1.887
GB, and the combined relation 3.070 GB.

### Reproduction and immediate failure mechanism

- Five unfiltered report samples were stable at p50 17.840 s / p95 17.936 s;
  five provider/model-filtered samples were stable at p50 17.219 s / p95
  17.289 s. This is reproducible, not an isolated cold-start outlier.
- The production report sets a 15 s PostgreSQL statement timeout. The hybrid
  path finishes its other bins before running `attempt_ledger` on the exporter.
  The attempt statement consumes the full 15 s, is cancelled, and its savepoint
  catches the exception. The report therefore returns after roughly
  `other report work + 15 s`, with the attempt section fail-open/unavailable.
- The harness explains the captured SQL separately under a 120 s diagnostic
  timeout. That complete execution takes **73.211 s unfiltered** and **36.347 s
  provider/model-filtered**. Thus the 17–18 s request timing is not completed
  attempt accounting; it is the 15 s cancellation signature. Subtracting the
  timeout leaves about 2.936 s unfiltered and 2.289 s filtered at p95 for the
  rest of the report.

### Plan-to-SQL root-cause trace

| SQL stage | Recorded plan evidence | Amplification and interpretation |
| --- | --- | --- |
| `turn_cohort` (`jobs_store.py` 6080–6084) | Sequentially scans 3M turn rows to retain 739,736; 8,208 hit + 76,338 read blocks; then an 8.7 MB external merge sort | A 24.7% range is broad enough that the sequential scan is plausible. It still reads about 0.58 GiB and proves the hybrid attempt path returns to the raw 3M turn table. This is a secondary but unavoidable floor in the current query. |
| `attempt_base` job join (6085–6088) | `ix_llm_provider_attempts_runtime_job` reports 2,999,833 actual rows, no `Index Cond`, and 152,566 read blocks | The index is used only as the ordered side of a merge join. The 739,736 job IDs are uniformly interleaved across nearly the whole 1..3M job-id domain, so the merge must walk virtually the entire partial index/table. It reads 4.055 source attempt rows per matched cohort row and about 1.164 GiB. “Index used” is therefore not evidence of a selective lookup. |
| rate-card lateral join (6131–6144) | `ux_llm_rate_cards_effective_at` executes 739,736 loops, returns zero rows in this fixture, and records 1,479,472 shared hits | Pricing performs one empty lookup per cohort attempt—exactly two buffer hits per row. This happens before `facts` applies provider/model/completeness filters, so the filtered query still pays all 739,736 probes even though only 502,885 facts survive. |
| `selected_calls` / `gap_facts` (6183–6196) | Unfiltered: 739,736 call-index loops, 3,144,236 hits and 554,444 reads. Filtered: 502,885 loops, 2,198,469 hits and 315,956 reads | The set of selected calls is fed into an index nested loop: one `ix_llm_provider_attempts_call` lookup per call. It touches exactly five logical buffers per call in this fixture. Unfiltered this is 28.22 GiB of logical buffer traffic and 4.23 GiB of reads against a 128 MB buffer cache, even though every synthetic call has one attempt and zero gaps. Two downstream outer/inner gap groupings then sort/scan the materialized rows again. This is the largest directly observed amplification. |
| materialized `facts` and root `Append` (6181–6240) | 739,736 unfiltered or 502,885 filtered wide facts. `facts` is referenced seven times: selected calls, overview, user, lane, requested model, resolved model, and cost. The plan contains repeated 90–105 MB external merge sorts and large temp-block counts. | Materialization prevents re-running correction/pricing, but it turns the wide result into a temp-backed relation that every `UNION ALL` arm scans again. Five metric arms each include `count(DISTINCT call_id)` and two ordered-set TTFT percentiles, so they repeatedly sort at `work_mem=4MB`. The compact evidence contains cumulative parent buffer counts, so those temp numbers must not be summed as exclusive I/O; the repeated spills themselves are unambiguous. |

Corrections are empty in this fixture and their scan returns zero, so they are
not the cause. The provider/model filter reduces final facts to 67.982%, but it
does not reduce the raw turn scan, the near-full job-index walk, or the 739,736
rate-card probes. This explains why the live filtered report still waits for the
same 15 s cancellation.

### Root cause statement

The timeout is caused by a high-cardinality raw analytical query being executed
on every request. The query combines four multiplicative costs: a broad raw
turn cohort, a merge join that walks nearly all 3M attempts despite naming the
job index, per-attempt rate-card probes, and per-call gap probes followed by
seven scans/aggregations of a wide materialized fact set under 4 MB `work_mem`.
The 15 s timeout and savepoint only hide the unfinished section; they are not
the performance cause.

### Minimal A/B hypotheses for the next debugging phase

Each experiment changes one variable on a rebuilt dedicated 3M+3M fixture and
must compare result checksums/row counts as well as `EXPLAIN (ANALYZE, BUFFERS,
FORMAT JSON)`.

1. **Job-join access path.** A=current merge/index plan. B=`SET LOCAL
   enable_mergejoin=off` for the same SQL, permitting a hash join/attempt
   sequential scan. Expected marker: the 2,999,833-row index scan and cohort
   external sort disappear. If elapsed/read blocks fall, the current partial
   index is useful for selective jobs but harmful as a forced full ordered
   scan at 24.7% selectivity. Attempt-base count must remain 739,736.
2. **Rate-card N lookups.** Because this fixture has no matching rate cards,
   replace only the lateral result with typed NULL rate fields. Costs remain
   unknown, so the payload is equivalent for this fixture. Expected marker:
   739,736 loops and 1,479,472 hits disappear. The timing delta isolates the
   lateral contribution without proposing a production pricing shortcut.
3. **Gap N lookups.** Because the fixture guarantees one `(1,1)` attempt per
   call, replace only the gap result with known zero constants. Expected marker:
   739,736 call-index loops, 554,444 reads, and the outer/inner gap sorts
   disappear while every non-gap output and zero-gap result remains identical.
4. **Spill sensitivity.** Run the unchanged query once at 4 MB and once with a
   diagnostic transaction-local `work_mem=256MB`. Expected marker: external
   merge sorts/temp blocks shrink or disappear. This measures spill cost only;
   it is not a production fix because report concurrency makes a blanket
   `work_mem` increase unsafe.
5. **Repeated-facts branch peeling.** Starting with the same materialized facts,
   retain overview/filter options and add user, lane, requested, resolved, cost,
   and gap arms one at a time. Record the marginal full-facts scan, sort, temp
   I/O, and elapsed time per arm. This distinguishes unavoidable base-fact cost
   from the root `Append` multiplication. Each retained arm's result must match
   the corresponding current payload section.
6. **Combined set-based floor, only after 1–5.** Apply the individually confirmed
   diagnostic variants together and measure the complete report. Do not infer
   this floor by adding isolated timings because cache and spill effects are
   nonlinear.

### Can set-based rewrites alone satisfy the existing 3 s whole-report gate?

The evidence says **not with defensible margin**. The non-attempt report already
consumes about 2.29–2.94 s at p95, leaving roughly 0.71 s for the filtered
attempt section and only 0.06 s unfiltered. Even after eliminating the lateral
and call-level N lookups, the request would still need to read a 739,736-row
cohort from a 3M-row raw turn table, join/aggregate 739,736 attempts, compute
exact distinct calls and percentiles, and emit all breakdowns. A set-based
rewrite is worth measuring to close the query-shape defect, but claiming that it
can reliably fit the remaining whole-report budget would contradict the
recorded I/O floor and current P0-A timing.

Performance proof should therefore move to **Task 5: daily attempt rollup**, in
the existing business PostgreSQL/RDS and existing maintenance loop—no new
service, database, queue, or infrastructure.

### Task 5 rollup design constraints from this diagnosis

1. **Authoritative day.** Assign every attempt/call to the day of its joined
   whole-turn `v2_turn_metrics.created_at`, not attempt `started_at`, preserving
   Task 4's job-cohort semantics.
2. **Two compact fact layers.** A daily attempt-dimension fact table should hold
   additive attempt counts, nullable token sum/known-count pairs, cost source/
   currency totals, and exact TTFT sample arrays (matching the existing P0-A
   exact-latency pattern) at a user/lane/requested/resolved/completeness grain.
   A separate daily logical-call fact should precompute distinct-call coverage
   and full-call outer/inner gaps. It needs grouping cells for unfiltered,
   provider-only, model-only, provider+model, and all/metered/unknown filters so
   failover calls are counted once for each supported selection but are never
   incorrectly summed across identity cells.
3. **Hybrid exactness for custom boundaries.** Reuse the current partition
   rule: read full interior Shanghai local days from daily attempt facts, and
   execute the current raw/correction-aware SQL only for the first/last partial
   day and any dirty days. Sources must be half-open and disjoint before their
   aggregates are merged. Thus an arbitrary custom timestamp range stays exact;
   non-Shanghai timezones retain the current exact raw fallback unless a
   timezone-specific rollup is deliberately added.
4. **Correction-safe rebuild, never incremental guessing.** Use the existing
   `llm_usage_rollup_watermarks` cursors: finalized attempt cursor plus monotonic
   `late_correction_id`. Map every new correction through attempt `job_id` to its
   authoritative turn day, mark that day dirty, and atomically delete/recompute
   the entire day's attempt and call facts from latest main rows plus all
   append-only deltas. While dirty, query that day raw. `replay_generation`
   forces bounded historical rebuild after accounting-code changes. Before
   implementation, verify whether completed attempt main rows are immutable; if
   not, add an `(updated_at, attempt_id)` source cursor because
   `(finished_at, attempt_id)` alone can miss in-place revisions.
5. **Rate-card replay.** Rate cards are immutable by version but can be appended.
   A newly inserted back-effective card can change estimates, so its affected
   provider/model days must be dirtied or a replay generation advanced.
6. **Request-path target.** A 90-day query should read compact daily rows for 89
   full days and at most two raw boundary-day slices (roughly 16k attempts in
   this uniform fixture instead of 739,736), plus explicitly dirty days. The
   scale gate must prove the real hybrid path, correction replay, exact boundary
   parity, one coherent snapshot, and whole-report p95 under 3 s.

This section is diagnosis/design evidence only. No query, schema, timeout,
rollup worker, or production code was changed in this phase.
