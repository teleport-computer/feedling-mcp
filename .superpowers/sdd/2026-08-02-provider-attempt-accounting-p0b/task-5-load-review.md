# P0-B Task 5 load/formal proof independent review

Reviewed commit: `deff128524dea481e85c32e9a4943db1c1bb211d`

## Resume-path review of `a3911c76f663dee0490f121959985af20895f04a`

### Resume review verdict

- **Spec: FAIL**
- **Quality: FAIL**
- **Ready to run destructive formal resume: NO**

The control-flow safety properties are largely correct: the prefix is strict
and formal-only; connected-database/schema/resume validation precedes the
business producer; invalid validation and `--validate-resume-only` return
before cleanup is armed; a full resume skips only seed/bootstrap and reruns the
business producer, both timing cohorts, SQL capture/EXPLAIN, final gates, and
cleanup.  Exact-prefix deletion is used for user-scoped rows.

However, the preflight does not yet prove that a same-cardinality fixture has
the deterministic source distribution or semantically correct rollups, formal
configuration is not fully locked, and the producer's own rollup writes are not
revalidated before timing and destructive cleanup.

### C1 — Deterministic source and rollup integrity can be forged with equal counts

Relevant code:

- `scripts/perf/admin_usage_scale.py:297-391`
- `scripts/perf/admin_usage_scale.py:1046-1180`
- `tests/test_admin_usage.py:98-159`

Global and prefix cardinalities are checked exactly, as are user ID/document
shape, distinct source IDs/calls/jobs, source/user ownership, one membership
per call, orphan absence, and watermark/retention fields.  Those are useful but
do not establish the deterministic fixture used by the performance proof.

The clearest counterexample is already in the tests.  The accepted synthetic
snapshot uses `rows_in_90d=739726` and raw-edge counts of 16438, while the real
read-only artifact reports 739736 and 8208.  `_validate_resume_snapshot`
accepts both because it requires only a positive 90-day count and equality
between turn/attempt/call counters.  Moving source timestamps while preserving
all IDs and counts therefore passes preflight but changes the report cohort,
raw-edge volume, plans, and timing distribution.

Likewise, the three daily rollup tables are validated only by total row count.
Daily keys or aggregates can be swapped/corrupted while keeping 731199 rows.
Membership validation checks call/user existence but not its local day, lane,
requested/resolved provider/model identity, or equality with the canonical
attempt/turn cohort.  A same-count membership-day corruption passes and changes
which 90-day rows the hybrid report reads.  The final formal gate checks
performance and plan shape, not semantic equality of the returned totals, so it
does not repair this gap.

Minimum fix:

- Derive the exact expected 90-day and raw-edge source values from the fixture
  formula and fixed production window/partition, then require exact equality.
  Prefer a deterministic SQL/formula computation bound to the seed contract
  rather than unexplained copied magic numbers.
- For source shape, validate the deterministic timestamp/user/job/call and
  provider/model/lane distribution with exact checksums or bidirectional set
  equality/anti-joins.
- For each daily rollup, compare canonical keys and aggregate fields to a
  recomputed raw-source relation using `EXCEPT`/anti-joins or a collision-
  resistant ordered checksum plus exact counts/totals.
- For memberships, require bidirectional equality with the expected
  call/user/Shanghai-local-day and requested/resolved identity projection, not
  merely non-orphan cardinality.
- Add negative tests that alter a source timestamp, a daily aggregate/key, and
  a membership local day while preserving every current count; all must fail.

### C2 — Formal users/history configuration is not exact or provenance-safe

Relevant code:

- `scripts/perf/admin_usage_scale.py:1497-1519`
- `scripts/perf/admin_usage_scale.py:1630-1656`
- `scripts/perf/admin_usage_scale.py:1968-1978`

Formal mode requires exactly 3,000,000 turns and attempts, but still accepts any
positive `--users` and any `--history-days >= 90`.  A fresh formal run can seed
one user over 90 days and remain eligible for the existing cardinality gate.
On resume the actual preflight does require 2,000 users, but the final artifact
records the caller's unrelated `args.users` and `args.history_days`; the final
gate does not reconcile those fields with the validated fixture.

Minimum fix:

- Require formal CLI configuration to use exactly 2,000 users and 365 history
  days before database access, alongside the existing 3M/3M requirements.
- Require the final gate's fixture fields and pre/post resume snapshots to agree
  with all four formal constants.  Add fresh-formal and resumed-formal negative
  tests for wrong users/history values.

### C3 — Producer rollup writes invalidate the only checked snapshot

Relevant code:

- `scripts/perf/admin_usage_scale.py:1568-1619`
- `scripts/perf/admin_usage_scale.py:1671-1684`
- `scripts/perf/provider_attempt_business_path.py:1348-1460`

The preflight correctly runs before the producer.  In resume mode the producer
then deliberately inserts twenty dirty days and successfully recomputes those
existing rollup days.  It proves its maintenance outcomes and removes the
load-proof dirty claims, but it does not prove that the resumed daily rows,
membership state, counts, and watermarks still match the validated fixture.
The runner copies the stale pre-producer `resume_snapshot` into evidence, arms
cleanup, and proceeds directly to timing.

A recompute regression that deletes, duplicates, or changes aggregate rows can
therefore invalidate preflight while the stale snapshot remains healthy in the
artifact.  Timing/EXPLAIN may still pass, after which cleanup destroys the only
recoverable fixture.

Minimum fix:

- Immediately after the business producer settles, collect a second complete
  resume snapshot and run the same strengthened exact validator before timing
  or arming destructive cleanup.
- Store both pre- and post-producer snapshots in evidence and require exact
  invariant equality (allowing only explicitly documented volatile watermark
  timestamps/cursors).
- Add a test producer that preserves business evidence but corrupts one daily
  rollup row; assert timing is never called, cleanup is not armed, and the run
  fails closed.

### I1 — Control flow is statically safe but lacks mutation-spy regression tests

The current ordering does satisfy these properties by inspection:

- invalid prefix/snapshot fails before producer and before the cleanup
  `try/finally` exists;
- validate-only executes SELECT-based schema/snapshot collection, optionally
  writes its local JSON output, and returns before producer/timing/cleanup;
- a successful full resume sets `cleanup_armed` only after business evidence is
  produced;
- cleanup deletes user-scoped rows by exact `left(...)=prefix` matching, then
  removes the globally unique watermarks only after global ownership passed;
- an external hard interruption cannot emit final `passed=true`; an
  interruption during the producer can leave fail-closed load-proof dirty
  state that a future preflight rejects rather than silently trusting.

The added tests exercise helper validation but not `_run` ordering or mutation
absence.  Add spies/fakes proving invalid preflight never calls producer,
delete, seed, bootstrap, timing, or EXPLAIN; validate-only calls only expected
read operations; full resume skips seed/bootstrap but invokes every downstream
proof stage; and failure before post-producer validation never deletes the
fixture.  Also document that a second external interruption during the
producer may require an explicit repair/validation step because leftover
`load_proof` dirty rows intentionally block automatic reuse.

### Resume review verification

- Resume helper negatives plus provider business-path/recorder/provider tests:
  **87 passed, 1 skipped**.
- Dry read-only artifact inspected at
  `/private/tmp/admin-usage-resume-validation.json`; it records exact global and
  prefix 3M/3M/2,000 counts, 739736 90-day rows, 8208 raw-edge rows, clean
  watermarks, completed-through 2026-08-02, retained-from 2025-06-29, and zero
  dirty rows.
- No formal run, external provider network request, remote RDS write, or local
  fixture cleanup was performed.
- The existing untracked business-path evidence file was not modified.

### Resume ready verdict

**Not ready for destructive resume.**  Strengthen C1's exact deterministic
source/rollup equality, lock C2's 2,000-user/365-day configuration, perform and
record C3's post-producer revalidation, and add the I1 control-flow spy tests.
The read-only validation result is useful evidence, but it is not yet enough to
authorize timing followed by cleanup.

## Final re-review of `7864b97a39f680f5b5bab8380ade1792c4722356`

### Final verdict

- **Spec: PASS for the load/formal harness**
- **Quality: PASS for the load/formal harness**
- **Ready for the formal 3M + 3M run: YES**
- **P0-B release readiness: still depends on the actual formal artifact and
  remaining branch-wide verification**

The fix closes I3.  The evidence now binds successful dirty-day recomputation,
not merely nested connection acquisition, to the pool-contention workload.  No
open finding remains in this review slice.

### I3 — CLOSED: raw successful tick outcomes are fail-closed

The probe canonicalizes every raw maintenance result to explicit JSON types and
retains tick index, monotonic start/end, and the complete production outcome.
The validator requires every recorded tick to have `status='ok'`, at least one
refreshed day, matching `days_refreshed`, no error/cancel/lock-busy field, and a
valid canonical ISO local date for every refreshed day.

Across all tick outcomes it requires exactly twenty refreshed dates, all
unique, and exact equality with the separately claimed refreshed-day list.
`dirty_remaining_before_cleanup` is measured after all worker threads and the
recorder have settled but before the `finally` cleanup; it must be zero.

Every maintenance pool interval now carries its tick index.  For every
successful tick, the validator requires both `attempt_rollup_outer` and
`attempt_rollup_rebuild` acquisitions, with their complete intervals contained
inside that tick's measured interval.  Conversely, every maintenance
acquisition must reference one of the validated successful tick indices.  A
failed recompute can no longer contribute an unbound interval to an otherwise
healthy artifact.

The production loop seeds exactly twenty dirty claims and runs with
`max_days=1` until all twenty successful days are observed, breaking early on
an error/no-op outcome and bounding attempts at twice the seeded-day count.
Structured error, zero-day success, duplicate day, insufficient coverage,
non-string date, invalid date, and missing/incorrect interval evidence all fail
validation.

### Exception, serialization, and cleanup behavior

Maintenance outcomes containing dates/datetimes are normalized recursively;
non-string dictionary keys or unsupported non-JSON values raise before an
artifact can be produced.  Such producer exceptions are captured by the
maintenance thread, make the aggregate probe fail, and still execute the
existing `finally` cleanup.  The cleanup deletes seeded dirty claims and both
watermarks after restoring the production observer and pool accessor.

The explicit pre-cleanup zero check is separate from that cleanup, so deletion
cannot manufacture the successful-drain claim.  The formal scale harness's
later source/rollup/correction/watermark residual-count gate remains unchanged.

### Final verification

- Provider business-path, recorder, async provider, and formal/default gate
  tests: **73 passed, 1 skipped**.
- New maintenance negative coverage includes error status, no-op success,
  duplicate days, fewer than twenty days, non-string/invalid local dates, and
  canonical date serialization.
- Changed perf scripts: `compileall` passed.
- `git diff a641ab4d 7864b97a --check`: passed.
- Root has separately retained the real local artifact evidence; this final
  review did not repeat a PostgreSQL probe and did not run the prohibited 3M
  fixture.
- No external provider network request or remote RDS access was performed.

### Final ready verdict

**Ready for the formal 3M + 3M run.**  C1/C2/C3/C4 and I1/I2/I3 from the three
review rounds are closed.  Formal success must still come from a fresh exact
3M-turn/3M-attempt run at the reviewed commit, pass both strict sub-3,000 ms
cohorts and all plan/index/business-path gates, and finish with zero residual
state.

## Second re-review of `2e55b1a915d2b2094ac0430ba9be4ebbaca78a4c`

### Second re-review verdict

- **Spec: FAIL**
- **Quality: FAIL**
- **Ready for the formal 3M + 3M run: NO**

The fix closes the previous C3, C4, and I2 findings.  Candidate provider call
intervals are now bound to raw DB acquisition intervals; every candidate must
have a non-empty recomputed intersection and the full batch must cover all six
roles.  Timeout evidence now reads the production Usage total and attempt
budgets and separately records the actual maintenance argument.  The stale
default-attempt test is updated and green.

One remaining maintenance-outcome evidence gap can still allow a broken
rebuild workload to satisfy the contention proof.

### Previous C3 — CLOSED: provider samples are bound to raw contention intervals

Every provider path now stores monotonic `started_ns` and `finished_ns` arrays.
For each OpenRouter, Anthropic, and Google pool-contention candidate, the
producer derives `overlapping_roles` from half-open intersections with raw
connection acquisition/release intervals.  The validator independently
recomputes the intersection, requires every candidate's intersection to be
non-empty, and requires the complete candidate population to cover all six
exact roles.  Result digests, exceptions, HTTP attempts, retries, raw paired
deltas, and strict p95 remain independently recomputed.

The provider worker waits for real exporter/maintenance roles before sampling,
and report plus maintenance loops continue until the provider population is
finished.  Negative tests reject a candidate outside the DB intervals and a
batch missing the rebuild role.  This closes the timing-to-contention binding
defect from the first re-review.

### Previous C4 — CLOSED: all three timeout contracts reflect their actual source

The producer reads `jobs_store._USAGE_REPORT_STATEMENT_TIMEOUT_MS` (15,000 ms)
and `jobs_store._RUNTIME_ATTEMPT_USAGE_STATEMENT_TIMEOUT_MS` (3,000 ms), fails
immediately if either production contract changes, and records them under
distinct names.  The maintenance field is the 3,000 ms constant actually
passed as `statement_timeout_ms` to `run_maintenance_tick`.  The validator
checks all three independently, and parameterized negative tests reject every
cross-labelled/mismatched value.

### Previous I2 — CLOSED: default-attempt focused test is current

`test_admin_usage_scale_attempt_fixture_is_explicit_and_bounded` now expects
the formal default of 3,000,000 attempts while retaining explicit zero,
partial, negative, and over-limit cases.  The focused test passes.

### I3 — OPEN: rebuild acquisition is recorded, but maintenance success is not

Relevant code:

- `scripts/perf/provider_attempt_business_path.py:1155-1174`
- `scripts/perf/provider_attempt_business_path.py:1194-1214`
- `scripts/perf/provider_attempt_business_path.py:1242-1265`
- `scripts/perf/provider_attempt_business_path.py:268-310`

The probe seeds twenty real dirty-day claims and repeatedly invokes the real
`provider_attempt_rollup.run_maintenance_tick`; nested
`attempt_rollup_rebuild` intervals prove that `recompute_local_day` acquired
its second production connection rather than the maintenance loop remaining a
pure no-op.  However, `run_maintenance_tick` deliberately returns structured
`status='error'` results instead of raising.  The maintenance thread stores
those tick results only in `outcomes['maintenance']`, then the producer drops
them when constructing the artifact.  Neither the artifact nor validator
requires any tick to be `ok`, any dirty day to be successfully refreshed, or
the refreshed days to be unique.

A failing recompute can therefore repeatedly acquire the rebuild connection
for the same surviving dirty day, produce all required raw intervals and role
coverage, and still pass the evidence validator.  The load report's stronger
claim that twenty real dirty-day recompute inputs were exercised is not bound
to auditable outcomes.

Minimum fix:

- Add raw maintenance tick outcomes to the artifact and validate that every
  workload tick used for contention returned `status='ok'` (or the exact
  documented success status), with no error/cancel/lock-busy result.
- Record and validate a non-empty set of unique successfully refreshed dirty
  days; if the intended claim is twenty, require exactly twenty distinct seeded
  days to be consumed and confirm no load-proof dirty claims remain before the
  cleanup delete.
- Add a negative test whose maintenance function returns structured errors
  while still acquiring a nested connection, and prove it cannot satisfy the
  validator.

### Second re-review verification

- Producer/recorder/provider plus formal/default gate tests: **66 passed, 1
  skipped**.
- `git diff 215e0d44 2e55b1a9 --check`: passed.
- Static trace confirmed candidate interval intersection is recomputed from raw
  DB intervals and all-six-role batch coverage is mandatory.
- Static trace confirmed 15,000 ms total Usage deadline and 3,000 ms attempt
  cap come from the production module, while the 3,000 ms maintenance timeout
  is the actual argument passed by the probe.
- No 3M run, external provider network call, or remote RDS access was performed.

### Second re-review ready verdict

**Not ready.**  C3, C4, and I2 are closed.  Bind successful, distinct dirty-day
rebuild outcomes into the artifact and validator, add the structured-error
negative test, then obtain one final focused re-review before starting the
formal 3M + 3M run.

## Re-review of `e6b30a84d3a4e2163ed91a02ed8061cd7edd4735`

### Re-review verdict

- **Spec: FAIL**
- **Quality: FAIL**
- **Ready for the formal 3M + 3M run: NO**

The fix closes C1 and C2.  It also substantially strengthens I1: six exact
pool roles are captured as acquisition/release intervals, peak and overlap
claims are recomputed from those raw intervals, and all three provider adapters
now have raw adjacent baseline/candidate samples with a strict recomputed
paired-p95 gate.  Two remaining evidence defects and one focused-test
regression still block the expensive formal run.

### C1 — CLOSED: exact 3M + 3M is enforced twice

Formal CLI mode now rejects any turn or attempt cardinality other than exactly
3,000,000 before database URL validation or PostgreSQL access.  The final gate
independently requires `formal=True`, exact fixture cardinalities, and exact
observed `source.total_rows` / `source.attempt_rows`.  `--non-formal` evidence
is permanently ineligible to pass even if every timing and plan assertion is
otherwise healthy.

Verified by the two new formal-gate tests and a direct 100/100 formal invocation
that exited with the cardinality error before attempting a database
connection.

### C2 — CLOSED: each failure has a reason-level witness

Startup, pool construction, SQL execution, and serialization now begin with an
empty per-mode queue and use separate recorders.  Startup/pool/SQL witnesses
count the exact injected call and capture the exception type; serialization
injects one malformed object and waits for its consumption and exact one-drop
delta.  The large fixed capacity prevents the measured fanout from saturating
these queues, and the validator rejects queue-full attribution.

An independent producer probe with 20 samples/provider observed: startup one
thread-factory call and one drop; pool 252 factory calls and 252 drops; SQL 252
`executemany` calls and 252 drops; serialization one injected/consumed item and
one drop.  All queue preconditions were zero and all reported queue-full drops
were zero.

### C3 — OPEN: paired provider timings are not bound to the real pool-contention interval

Relevant code:

- `scripts/perf/provider_attempt_business_path.py:714-796`
- `scripts/perf/provider_attempt_business_path.py:989-1017`
- `scripts/perf/provider_attempt_business_path.py:1021-1049`
- `scripts/perf/provider_attempt_business_path.py:1084-1098`

The pool probe starts provider, maintenance, and Usage threads at one barrier,
but maintenance and the one Usage report are free to finish while the provider
thread continues 120 measured logical calls.  Provider samples contain elapsed
duration only; they do not contain monotonic start/end timestamps.  The pool
acquisition intervals therefore cannot establish which candidate samples, or
how many of the p95 population, actually overlapped the Usage exporter/
importers and maintenance connections.

The `pool_contention` candidate differs from baseline by enqueuing into the
recorder, so it does measure recorder hot-path overhead.  It does not prove the
reported paired p95 was measured during real report + maintenance shared-pool
contention.  A brief overlap somewhere in the run and a p95 computed mostly
after those background operations finish can both pass.

Minimum fix:

- Store monotonic start/end timestamps for every pool baseline/candidate
  provider sample and validate a predeclared minimum sample population per
  provider whose candidate intervals overlap the required DB-role contention
  window; or hold/repeat the real Usage/maintenance workload until all measured
  candidate samples finish.
- Compute the pool-contention paired p95 only over the auditable overlapping
  pairs (with the same minimum 20/provider), and add a negative test where DB
  role intervals end before the provider samples.

### C4 — OPEN: the artifact's 3,000 ms Usage report timeout is not production truth

Relevant code:

- `scripts/perf/provider_attempt_business_path.py:40`
- `scripts/perf/provider_attempt_business_path.py:281`
- `scripts/perf/provider_attempt_business_path.py:1025-1033`
- `backend/model_api_runtime/v2/jobs_store.py:43-44`
- `backend/model_api_runtime/v2/jobs_store.py:7843-7874`

`REPORT_STATEMENT_TIMEOUT_MS = 3_000` is producer-owned.  It is passed to the
attempt-maintenance tick, but it is not passed to
`jobs_store.usage_report_snapshot`.  The production Usage exporter transaction
and importer deadline use `_USAGE_REPORT_STATEMENT_TIMEOUT_MS = 15_000`; only
the attempt sub-section has a separate 3,000 ms cap.  Nevertheless the pool
artifact states `report_statement_timeout_ms: 3000`, and the validator merely
compares it with the producer's own constant.

This is a self-reported placeholder rather than the production report budget
claimed by the load report.  It must not be used as proof that the report's
connection occupancy is bounded by three seconds.

Minimum fix:

- Report both production budgets with precise names, reading or observing the
  actual Usage constants/configuration: the 15,000 ms total exporter/importer
  deadline and the 3,000 ms attempt-section cap.
- If the proof requires a 3,000 ms whole-report limit, add an explicit supported
  test-only argument at the production entry point and verify the SQL session
  setting/deadline; do not label the maintenance argument as the report budget.
- Add a negative test that changes/mismatches the production Usage budget and
  proves the artifact validator fails.

### I2 — OPEN: stale focused test after changing the default attempt cardinality

Relevant code:

- `tests/test_admin_usage.py:87-96`
- `scripts/perf/admin_usage_scale.py:31-34`

`DEFAULT_ATTEMPT_ROWS` correctly changed from zero to 3,000,000 for the formal
default, but `test_admin_usage_scale_attempt_fixture_is_explicit_and_bounded`
still asserts `_resolve_attempt_rows(3_000_000, None) == 0`.  The focused test
therefore fails.  Update the assertion and preserve explicit negative/bounded
coverage for non-formal probes before claiming a green load-proof suite.

### Six-role interval verification

The role capture itself is genuine.  The exporter is explicitly scoped;
importer connections are initially marked pending and relabeled from the
production snapshot-import observer while their acquired connection remains
active; recorder threads are identified by their production thread name; and
maintenance outer/rebuild roles are distinguished by nested active connection
state on the same explicitly scoped thread.  `_derive_pool_timeline` rebuilds
peak occupancy and active-role snapshots from acquisition/release timestamps,
requires all three Usage roles to overlap, requires maintenance outer/rebuild
to overlap, and requires recorder + maintenance outer + at least one Usage role
to overlap.  Summary tampering or disjoint intervals fail validation.

This closes the original coarse-label/summary-trust portion of I1, but it does
not close C3's missing link between those intervals and the timed provider
sample population.

### Re-review verification

- Producer/recorder/provider plus new formal-gate tests: **59 passed, 1
  skipped**.
- Existing focused default-attempt test: **1 failed** because it still expects
  zero instead of the new 3,000,000 default.
- Independent in-process producer probe (no PostgreSQL/network): completed and
  validated all four exact failure witnesses; queue/failure paired p95 values
  remained below 5 ms.
- `admin_usage_scale.py --self-test`: passed.
- Direct 100/100 formal CLI probe: rejected before database access.
- Full Admin attempt was not valid evidence in this shell because
  `DATABASE_URL` was intentionally unset; database-backed errors from that run
  are environmental and are not counted as implementation findings.
- No 3M run, external network request, or remote RDS access was performed.

### Re-review ready verdict

**Not ready.**  C1 and C2 are closed, and six-role pool accounting is now
auditable.  Close C3 and C4, repair I2, then obtain one more focused review
before running the formal 3M + 3M proof.

## Verdict

- **Spec: FAIL**
- **Quality: FAIL**
- **Ready for the formal 3M + 3M run: NO**

The producer does exercise the real synchronous OpenRouter, Anthropic, and
Gemini adapter branches through `reliable_chat_completion` and
`chat_completion`.  Its deterministic `httpx.MockTransport` returns one real
retryable 503 and one provider-shaped success, and the validator compares raw
result digests, exception fingerprints, HTTP-attempt counts, retry counts, and
paired timing arrays per provider and sample.  Queue saturation is genuinely
bounded and the alternating adjacent sampling order is validated.

The maintenance probe also genuinely observes a nested connection: the outer
connection is held by `provider_attempt_rollup.run_maintenance_tick`, while
`recompute_local_day` acquires a second connection through the same tracked
pool.  `_TrackedPool` sets `maintenance_second_connection_observed` from two
simultaneously active connections under the explicit maintenance operation;
that bit is not inferred from a thread name.

However, the following findings prevent this commit from being accepted as the
formal no-business-impact gate.

## Findings

### C1 — Formal cardinality is not fail-closed

Relevant code:

- `scripts/perf/admin_usage_scale.py:31-32`
- `scripts/perf/admin_usage_scale.py:82-101`
- `scripts/perf/admin_usage_scale.py:249-255`
- `scripts/perf/admin_usage_scale.py:1174-1177`
- `scripts/perf/admin_usage_scale.py:1557-1563`

The required proof is exactly 3,000,000 whole turns plus 3,000,000 provider
attempts.  The harness defaults to 3,000,000 turns but defaults attempts to
zero, accepts any positive `--rows`, and accepts any `--attempt-rows` between
zero and `rows`.  Neither `_formal_gate_passed` nor the final `passed`
conjunction requires the observed source cardinalities to equal the formal
3M/3M contract.

The documented 100/100 probe returned false only because PostgreSQL happened
to choose a sequential scan.  That is planner behavior, not a cardinality
gate.  A small fixture that chooses the expected indexes can therefore be
reported as a successful formal proof.

Minimum fix:

- Introduce explicit formal constants for both source cardinalities and require
  `args.rows == 3_000_000` and resolved attempts `== 3_000_000` before seeding,
  or separate a clearly named non-formal probe mode from the formal mode.
- Recheck observed `source.total_rows`, `source.attempt_rows`, and the intended
  90-day cohort invariants in the final gate rather than trusting CLI config.
- Add a unit test constructing otherwise passing cohorts/index/business
  evidence at 100/100 and prove the final formal gate remains false independent
  of plan choice.

### C2 — Failure modes are accepted from an undifferentiated drop counter

Relevant code:

- `scripts/perf/provider_attempt_business_path.py:628-675`
- `scripts/perf/provider_attempt_business_path.py:214-218`

The pool, SQL, and serialization recorders are fed a high-volume common fanout.
Each mode is declared observed solely when that recorder's aggregate
`dropped_count` is greater than zero.  The same counter is incremented for
queue-full drops and for the injected off-thread failure.  Consequently the
artifact does not prove that the pool factory was invoked and failed, that the
SQL execution was reached and failed, or that the malformed serialization
item was actually consumed; an unrelated queue-full drop satisfies every
mode's validator.

This undermines the central failure-injection claim even though the injections
are configured in the producer.

Minimum fix:

- Give each injected factory/cursor/malformed event an explicit thread-safe
  invocation/consumption witness and require it before producing the artifact.
- Record per-reason drop deltas, or isolate each failure mode with a bounded
  enqueue followed by a wait for its exact injected witness before provider
  sampling.
- Add tests proving queue-full-only drops cannot satisfy pool, SQL, or
  serialization evidence.

### I1 — Pool evidence does not prove the claimed role overlap or latency impact

Relevant code:

- `scripts/perf/provider_attempt_business_path.py:266-360`
- `scripts/perf/provider_attempt_business_path.py:700-859`
- `scripts/perf/provider_attempt_business_path.py:220-251`

The probe invokes the real recorder, Usage report, and attempt maintenance, and
the underlying report code can use one exporter plus two importer connections.
But the artifact collapses exporter/importer A/importer B into the single
`usage_report` label, collapses maintenance outer/inner into one label plus a
boolean, and records no acquisition interval or simultaneous active-role set.
The validator only requires the three coarse operation names, a peak of at
least two, and the maintenance boolean.  It therefore cannot establish the
report's exporter and both importers, recorder, and maintenance outer/inner
connections were all observed and overlapped in the intended contention
window.

Further, `provider_results_match_baseline` compares results/retries only.  The
pool-contention probe discards provider baseline/candidate latency arrays, so
the +5 ms assertion covers queue saturation and synthetic recorder failures,
not real shared-pool contention.

Minimum fix:

- Track stable roles (`usage_exporter`, `usage_importer_a`,
  `usage_importer_b`, `provider_recorder`, `maintenance_outer`,
  `maintenance_rebuild`) and acquisition/release intervals or active-role
  snapshots; validate all expected roles and the required overlap.
- Preserve raw provider latency samples for the pool-contention baseline and
  candidate and apply the same predeclared paired p95 budget.
- Recompute peak occupancy and second-connection claims from raw role events in
  the validator rather than trusting summary fields.

## Evidence/provenance assessment

The formal runner now generates business-path evidence in-process and validates
it against the full current Git commit, complete config, raw arrays, execution
order, UUID/timestamp shape, and canonical SHA-256.  It no longer accepts a
caller-provided evidence file, so stale-commit and post-generation field
tampering fail in the formal path.  The digest is correctly described as an
integrity checksum rather than a signature.  The test named "handwritten"
rejects only a summary-shaped object; a complete hand-authored schema plus a
recomputed public digest would pass the standalone validator, but it cannot be
substituted into the current same-process formal runner without changing code.
That limitation should remain explicit and must not be described as
cryptographic authenticity.

The dedicated database URL validation is local-only (`127.0.0.1:55432`) and
requires a named non-system database.  No external HTTP request is made because
all provider traffic terminates at `httpx.MockTransport`.  The commit adds no
SQLite, Redis, Kafka, RDS instance, service, or deployment unit.

Cleanup accounts separately for turn and attempt watermark tables, dirty days,
source attempts/corrections, both turn rollups, both attempt rollups, and users.
The formal cleanup gate requires cascade verification and all residual counts
to be zero.  The standalone pool probe deletes the only three seeded rollup
rows in `finally`; the implementation report's local probe recorded zero
residual state.  I could not repeat the local PostgreSQL probe in this review
sandbox because TCP access to `127.0.0.1:55432` was denied before connection;
no remote database was attempted.

## Verification

- `pytest -q tests/test_provider_attempt_business_path.py tests/test_provider_attempt_recorder.py tests/test_provider_client_async.py`
  — **54 passed, 1 skipped**.
- `git diff deff1285^ deff1285 --check` — passed.
- Static trace confirmed the production adapter branches, 503 retry behavior,
  real recorder hot path, Usage exporter/importer implementation, and actual
  maintenance outer/rebuild connection nesting.

## Ready verdict

**Not ready.**  Close C1 and C2, strengthen the pool proof in I1, then rerun an
independent focused review before starting the expensive formal 3M + 3M run.
