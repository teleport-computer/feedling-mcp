# P0-B Task 4 — Formal review fix round 1

## Accepted findings and fixes

1. **Coverage cohort alignment.** Usage now builds a shared whole-turn cohort
   from `v2_turn_metrics` by `job_id`, constrained only by report time, user,
   and lane. Canonical attempts join those selected jobs; attempt timestamps no
   longer independently clip the numerator. The denominator is the cohort's
   whole-turn `model_calls`, so failover attempts outside the metric window are
   retained while attempts belonging only to out-of-window jobs are excluded.
   Historical whole-turn rows with NULL `job_id` remain in the denominator but
   cannot be guessed into the numerator, correctly surfacing a coverage gap.
2. **Unattributable filters.** Resolved provider/model and attempt
   completeness filters still filter attempt statistics, but logical-call
   coverage, its denominator, and its ratio become NULL with the explicit
   reason
   `provider_model_or_completeness_filters_cannot_attribute_whole_turn_model_calls`.
   Time/user/lane-only cohorts retain the ordinary distinct-call / whole-turn
   denominator.
3. **Sequence-gap scope.** The filtered attempt facts select call IDs; gap
   detection then reads every canonical `runtime_recorder` row for those call
   IDs without attempt-time or identity clipping. Outer and inner sequences
   remain one-based.
4. **Runtime alignment.** Runtime lane accounting now joins attempts to the
   same recent whole-turn `job_id` cohort and groups by the whole-turn lane.
   Attempts on old jobs cannot create an N/0 lane, while retries whose attempt
   timestamps fall outside the Runtime window remain attached to their recent
   job.
5. **Conservative estimated cost.** A rate estimate is emitted only when every
   token component with a nonzero applicable rate is known. A partial late
   correction cannot turn unknown output/reasoning/cache components into zero.
   The existing mutually exclusive regular/read/write/non-write-miss input
   partition remains intact.
6. **Authoritative currency provenance.** Currency classification now
   examines only cost-bearing main and correction components. Any non-NULL
   main cost or correction `cost_delta` with NULL currency, multiple correction
   currencies, or a main/correction conflict produces the explicit
   unknown-currency bucket. Token-only correction currencies are ignored.
7. **Filter options and copy.** Provider/model choices union whole-turn
   identities with resolved canonical-attempt identities from the shared job
   cohort. This optional query is savepoint-isolated and fail-open in both raw
   and hybrid report paths. The page explains filtered coverage unavailability
   and correctly says canonical attempt accounting is *below* the averages.

## Strict TDD evidence

- Cohort cycle: **6 expected RED failures** covered attempt-boundary/failover
  inclusion, out-of-cohort and N/0 exclusion, filtered coverage
  unavailability, cross-window gap recovery, resolved-only filter choices, and
  Runtime job-cohort alignment. After the shared cohort/gap/filter changes:
  **6 passed**, followed by **8 adjacent accounting/snapshot tests passed**.
- Cost cycle: **2 expected RED failures** showed a partial correction being
  estimated as `0.000020...` instead of unknown and a NULL-currency main cost
  being mislabeled USD. After completeness/provenance guards: **5 passed**
  including the two regressions and three adjacent cost cases.
- UI cycle: **2 expected RED failures** showed the wrong `above` direction and
  missing filtered-coverage explanation. After the copy/rendering change:
  **2 passed**.
- The first full-suite run exposed one test-fixture setup error: the same
  immutable rate-card version was inserted by a function-scoped fixture twice.
  The setup was made idempotent with `ON CONFLICT DO NOTHING`; the related
  failures were contamination from the aborted fixture, not report behavior.

## Verification

- PostgreSQL-backed `tests/test_admin_usage.py tests/test_v2_runtime_health.py`:
  **161 passed**, with only the two existing Alembic `path_separator`
  deprecation warnings.
- Ruff on all touched Python files: passed.
- `py_compile` on all touched Python files: passed.
- `git diff --check`: passed.

## Boundaries preserved

- Attempt, filter-option, and Runtime reads remain inside their caller's
  repeatable-read transaction. Optional Usage sections remain savepoint-
  isolated; filter-option or ledger failure does not hide whole-turn sections.
- Raw and hybrid paths use the same attempt cohort and resolved-option helper;
  the exported-snapshot, three-connection ceiling, admission/deadline, importer
  settlement, and raw fallback behavior are unchanged.
- Main rows remain current recorder observations; correction rows remain
  append-only deltas with an independent audit revision sequence.
- No Task 5 reconciler, retention/pruning, load harness, public API/docs-site
  contract, database, queue, or deployment unit was added.
