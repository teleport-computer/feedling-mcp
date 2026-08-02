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
