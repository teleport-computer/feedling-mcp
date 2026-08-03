# P0-B Task 5 retention independent re-review

Initial implementation: `0b52a55586b3327ebd842e2512b0567687ce0587`

Initial review: `70b8ae6b` (Spec FAIL / Quality FAIL / Not Ready)

Fix reviewed: `a99d18073f98c704ed391f626408272cca9daff1`

## Verdict

- **Spec: PASS for the retention slice**
- **Quality: PASS for the retention slice**
- **Ready: YES for the formal 3M + load-proof stage**
- **P0-B release readiness: NO until I2 is closed by that formal stage**

The fix closes the original C1, C2, C3, and I1 findings with bounded,
transactionally visible behavior and focused PostgreSQL coverage. No new
retention correctness blocker remains. The broad retention-index write cost and
the authenticity of external business-path evidence remain intentionally
assigned to the next formal proof; they are not results this commit claims to
have produced.

## Original findings

### C1 — CLOSED: late-old rows are no longer hidden below a published boundary

Relevant implementation:

- `backend/model_api_runtime/v2/provider_attempt_rollup.py:917-990`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:1023-1069`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:476-505`

The unsafe same-cutoff no-op and `>= published_at` lower bounds are gone.
Matched deletion/proof always checks authoritative
`v2_turn_metrics.created_at < cutoff`; orphan deletion/proof always checks
`started_at < cutoff`. Both destructive branches remain bounded with
`FOR UPDATE ... SKIP LOCKED LIMIT quota`, while the `EXISTS` completion proof
cannot publish past a surviving late-old row.

Source-change dirty claims are allowed below `retained_from`; ordinary replay
and bootstrap claims remain fenced. Maintenance waits for source cursors to
have no backlog, then the retention target rotation consumes the old claim.
This handles both delayed/replayed orphan attempts and supported turn-date
corrections without rebuilding a retained-out day.

Regression coverage verified:

- `test_next_cutoff_prunes_late_orphan_older_than_published_boundary`
- `test_next_cutoff_prunes_turn_moved_behind_published_boundary_and_dirty_claim`
- `test_retention_same_published_cutoff_checks_late_data_without_mutation`

### C2 — CLOSED: pending boundary and destructive rows are atomically visible

Relevant implementation:

- `backend/alembic/versions/0077_llm_usage_attempt_rollups.py:177-196`
- `backend/model_api_runtime/v2/provider_attempt_rollup.py:913-1097`
- `backend/model_api_runtime/v2/jobs_store.py:6010-6069`
- `backend/model_api_runtime/v2/jobs_store.py:6424-6588`

`retention_pending_from` is durable in the existing watermark row. An
incomplete destructive page writes the pending boundary in the same PostgreSQL
transaction as all parent/derived deletes. A repeatable-read reader sees the
old boundary and old rows; a later reader sees the pending boundary and the
committed deletes. Error, cancellation, timeout, or CAS loss rolls the entire
page back, so no reader can observe deletion without either the old complete
snapshot or the new partial fence.

While pending, affected rollup days fall back to surviving raw rows and both
Admin and Runtime expose `provider_attempt_retention_pending`; the known
whole-turn denominator remains, while logical coverage is `None`. Final
completion atomically advances `retained_from` and clears the pending boundary.

Regression coverage verified:

- `test_retention_pending_fence_precedes_multi_page_destructive_state`
- `test_retention_pending_and_destructive_rows_are_atomically_visible`
- `test_usage_attempt_partition_pending_fence_falls_back_to_surviving_raw`
- `test_usage_attempt_pending_fence_keeps_surviving_raw_and_marks_partial`
- `test_token_usage_by_lane_marks_pending_retention_partial`
- `test_usage_page_explains_pending_retention_without_claiming_zero`

Migration review confirms the new column is installed, constrained relative to
`retained_from`, included in downgrade cleanup, and covered by the existing
schema/downgrade tests. Exact concurrent-index recovery and unrelated-owner
preflight remain intact.

### C3 — CLOSED: arbitrary Admin timezones honor the Shanghai retention boundary

Relevant implementation:

- `backend/model_api_runtime/v2/jobs_store.py:6109-6168`
- `backend/model_api_runtime/v2/jobs_store.py:6424-6459`
- `backend/model_api_runtime/v2/jobs_store.py:6528-6557`

The raw fallback now converts the Shanghai local retention day to its exact UTC
midnight and crops the already-published interval independently of the report's
display timezone. Coverage detection uses that same UTC boundary for UTC and
arbitrary IANA zones. A pending fence marks totals partial but deliberately
keeps surviving raw rows visible; a published boundary excludes the known
pruned interval. Both paths preserve the whole-turn denominator and set the
ratio to `None` rather than reporting a false zero.

Regression coverage verified for `UTC` and `America/Los_Angeles` by
`test_usage_attempt_non_shanghai_range_honors_retained_boundary`, in addition
to Shanghai Admin and Runtime cases.

### I1 — CLOSED: one strict global explicit-row budget with rotating fairness

Relevant implementation:

- `backend/model_api_runtime/v2/provider_attempt_rollup.py:926-1021`

Attempts, dimensions, memberships, and dirty days now share one decrementing
`max_rows` budget. Attempt quota is shared between matched and orphan parents.
The starting target rotates by watermark version; an empty target consumes no
budget and the remainder is reclaimed by later targets. With `max_rows=1`, each
tick explicitly mutates at most one row and repeated ticks reach all four
targets.

Correction cascades are counted separately as implicit FK work rather than
misrepresented as explicit budget consumption. Their amplification is now
visible for the formal load proof.

Regression coverage verified:

- `test_max_retention_rows_is_one_global_fair_budget_and_cascades_are_separate`
- existing locked-row, rollback/cancel, cascade, and plan-shape tests

## I2 — OPEN for the next formal proof

The retention slice is ready to run the proof, but I2 is not yet closed.

### What is now fail-closed

`scripts/perf/admin_usage_scale.py` now requires:

- exactly two report cohorts with p95 strictly below 3,000 ms;
- one attempt statement, exact runtime-job index use, rollup relation use, and
  complete-plan scan/probe guards;
- exact valid retention-index definition plus nonnegative size and maintenance
  evidence from the connected 3M database;
- complete FK/watermark/dirty cleanup with zero residual rows; and
- an explicit business-path evidence file. Missing, unreadable, malformed, or
  incomplete JSON exits/fails, as do pool timeouts, business errors, baseline
  mismatches, missing provider lanes, or absent retention-index metrics.

The formal output's `passed` bit is the conjunction of all these gates, and the
process exits nonzero when any gate fails. The earlier defect where index use
was merely recorded rather than enforced is closed.

### What the formal load producer must still add

The current business-path JSON gate validates self-reported values but does not
establish provenance. A hand-authored JSON object with healthy values passes
`_business_path_evidence_passed`; the repository currently has no producer that
binds those fields to raw samples, the tested commit, fixture/run identity, or
the formal invocation. Therefore the next load task must not treat an arbitrary
JSON file as proof.

Before I2 can close, the producer and consumer should provide an auditable
binding, for example:

- producer-owned schema/version and unique run ID;
- tested commit and configuration/fixture identity;
- raw baseline and saturated/failure-injection samples from which p95,
  results-equivalence, errors, queue bound, retry counts, and pool occupancy are
  recomputed rather than trusted as booleans;
- provider-lane evidence for OpenRouter, Anthropic, and Google;
- a digest (or same-process generation) binding the consumed evidence to the
  formal output; and
- tests proving a fabricated value, altered sample, mismatched run/commit,
  stale file, missing field, or missing file makes the formal result fail.

The formal 3M run must also record the broad retention index's size,
bytes/attempt, table share, normal-planner use, maintenance latency, pool
occupancy, and provider-path latency/results. This is the intended point at
which the remaining write-amplification risk is accepted or rejected.

## Verification

Independent focused PostgreSQL run after the fix:

- retention late-old, pending fence, strict global budget, index plan,
  non-Shanghai coverage, pending Admin/Runtime, and evidence-gate cases:
  **12 passed**, 2 existing Alembic deprecation warnings;
- migration install and downgrade cases for the new watermark field:
  **2 passed**, 5 existing Alembic deprecation warnings;
- changed Python files: `py_compile` passed;
- commit diff: `git diff --check` passed.

Implementation report additionally records the broader final retention suite as
**243 passed**, 37 existing Alembic deprecation warnings, with Ruff and
`py_compile` passing.

## Ready verdict

**Ready for formal proof.** C1, C2, C3, and I1 are closed. The retention fix
does not need another implementation pass before the 3M + load stage.

**Not ready to declare P0-B complete or release-ready.** I2 remains open until a
trusted load-evidence producer and the actual formal 3M + 3M run close the
index-write and no-business-impact gates.
