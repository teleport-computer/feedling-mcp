# P0-B Task 5 — provider path and pool no-impact proof

## Scope

Added a repository-owned producer at
`scripts/perf/provider_attempt_business_path.py` and made the formal
`admin_usage_scale.py` runner invoke it in-process.  The formal gate no longer
accepts a caller-authored business-path JSON file.

The artifact is deliberately not presented as signed evidence.  It contains a
schema version, UUID run id, full Git commit, complete config, every raw timing,
result digest, exception fingerprint, HTTP-attempt/retry sample, an explicit
interleaved execution order, raw pool acquisitions, and a canonical SHA-256.
The runner recomputes and validates all of those fields against its own current
commit.  Missing, stale, tampered and summary-only fixtures fail closed.

## Production paths exercised

- Real synchronous `provider_client.reliable_chat_completion` and
  `chat_completion` adapters for OpenRouter, Anthropic and Gemini (reported as
  Google in the artifact).
- A deterministic in-process `httpx.MockTransport`; every logical call returns
  one retryable 503 and then one provider-shaped success.  No external network
  is used.
- Real started/terminal provider-attempt instrumentation and the real bounded
  `ProviderAttemptRecorder` hot path.
- Queue saturation plus worker startup, pool construction, SQL and
  serialization failure modes.
- The actual current-RDS pool shared concurrently by the recorder,
  `provider_attempt_rollup.run_maintenance_tick` (including its nested day
  rebuild connection), and `jobs_store.usage_report_snapshot` with the existing
  3,000 ms report statement budget.

Control, queue-saturated and recorder-failure calls are adjacent per provider
and sample.  Even samples run control first; odd samples reverse direction, so
warm-up or monotonic drift cannot masquerade as a telemetry regression.

## Local measured result

The focused producer ran against the explicit dedicated local database
`127.0.0.1:55432/feedling_usage_scale_task4d`; no remote RDS was touched.

- Pool capacity: 16; measured peak occupancy: 5; acquisition timeouts: 0.
- The raw interval stream contained all six exact roles: Usage exporter, core
  importer, attempt importer, recorder, attempt-maintenance outer connection,
  and its nested rebuild connection.  The three Usage roles overlapped; the
  two maintenance connections overlapped; and a measured five-connection
  snapshot contained outer maintenance, nested rebuild, recorder, exporter and
  attempt importer simultaneously.
- Provider results, exceptions, HTTP attempts and retries under pool contention
  matched their adjacent baseline samples exactly.  Pool contention's paired
  p95 latency regression was +0.244291 ms.
- Every one of the 60 measured pool-contention candidate calls records its own
  `started_ns`/`finished_ns`; the validator recomputes its intersecting DB roles
  from the raw acquisition intervals.  No candidate had an empty intersection.
  Coverage was outer maintenance 60/60, rebuild 1/60, recorder 57/60,
  exporter 60/60, attempt importer 60/60, and core importer 59/60.  The real
  role intervals ranged from 3.00–15.16 ms for rebuild, 10.59–80.60 ms for the
  outer maintenance connection, 1.25–38.80 ms for recorder batches,
  62.39–76.86 ms for exporter, 43.21–72.47 ms for attempt importer, and
  24.52–70.15 ms for core importer.
- Maintenance used twenty real dirty-day recompute inputs rather than repeated
  no-op ticks.  The artifact retains all 20 canonicalized raw tick outcomes;
  every tick had `status=ok`, refreshed exactly one unique ISO local day, had
  no error/cancel/lock-busy field, and was bound by tick index to both its outer
  and rebuild acquisition intervals.  The claimed and recomputed unique day
  counts were exactly 20, the last tick reported no dirty work pending, and a
  separate pre-cleanup query measured zero remaining dirty rows.
- Timeout evidence is separated by contract: the production Usage report total
  deadline is 15,000 ms, its attempt subsection is 3,000 ms, and the actual
  maintenance statement timeout passed by the probe is 3,000 ms.  The validator
  checks all three independently; 3 seconds is not described as the report's
  total budget.
- OpenRouter, Anthropic and Google each recorded exactly two real HTTP requests
  and one retry per sample, with zero business errors in every scenario.
- Queue saturation dropped 515 telemetry events while remaining bounded at its
  configured capacity.
- Startup, pool, SQL and serialization failures each have an isolated
  reason-bound witness: exact injection stage and exception type, an empty
  precondition queue, before/after drop deltas and zero queue-full drops.
  Serialization injects and consumes exactly one malformed item.  None altered
  provider results, exceptions, HTTP-attempt counts or retries.
- Paired p95 hot-path regression was +0.137583 ms for queue saturation and
  +0.154959 ms for combined recorder failures, both strictly below the
  predeclared +5.0 ms budget.
- Probe cleanup left both watermark tables and the dirty-day table at zero.

A 100-turn/100-attempt end-to-end **non-formal** runner integration also reached
final cleanup.  Its report timings were 22.651 ms unfiltered and 22.763 ms
filtered, and all source/rollup/watermark/dirty counts were zero after cleanup.
The final gate returned false by construction: only an exact 3,000,000-turn and
3,000,000-attempt run without `--non-formal` can pass.  Formal CLI invocations
reject any other cardinality before touching PostgreSQL.  No formal 3M run was
performed in this task.

## TDD and verification

- RED then GREEN tests cover stale commit, digest tampering, missing digest,
  hand-written healthy summaries, incomplete providers, false paired-p95
  claims, non-interleaved execution claims, missing measured pool evidence,
  nested maintenance occupancy, pre-existing rollup-state refusal, and separate
  turn/attempt watermark cleanup accounting.  Maintenance negatives cover
  error status, no-op success, duplicate days, insufficient seeded-day
  coverage, non-string/invalid local dates, and non-canonical Python dates.
- Focused verification: `100 passed, 1 skipped` across the new evidence tests,
  provider recorder tests and provider client tests.
- Ruff passed for both perf scripts and the new tests.
- Compileall passed for both perf scripts.
- `admin_usage_scale.py --self-test` passed its percentile, sensitive-column and
  half-open time-range checks.

The commit-bound JSON is generated after checkout by the producer/formal runner
and is intentionally not checked into the same commit: committing the artifact
would necessarily change HEAD and make its own commit binding stale.

## Formal resume recovery design and plan

`--resume-prefix` is a formal-only recovery path for a fully seeded, fully
rolled-up fixture whose original process was externally terminated.  It accepts
only `^scale_usage_[0-9a-f]{10}_$`; validates the connected local database,
schema, unique fixture ownership, exact 2,000-user/3,000,000-turn/3,000,000-
attempt cardinality, deterministic 90-day/raw-edge baseline, complete daily and
membership rollups, and clean completed watermarks before any business-path
probe runs.  It records the validated pre-existing counts in evidence, skips
seeding and rollup bootstrap, then follows the unchanged report timing,
EXPLAIN, formal gate, and exact-prefix cleanup path.

Implementation order is strict TDD: add prefix/formal-mode negative tests; add
partial, foreign-state, and cardinality/rollup mismatch tests for an isolated
resume validator; wire the validated branch ahead of the business producer;
allow that producer's local pool proof to preserve already validated rollup
state; verify parser/evidence/cleanup behavior; then run focused tests, Ruff,
compileall and the harness self-test.  This task neither executes the formal
resume nor deletes the existing dedicated fixture.

### Read-only recovery validation

The externally interrupted fixture was validated with
`--resume-prefix scale_usage_42e02f444a_ --validate-resume-only`.  The command
exited 0 before the business producer, timing, or cleanup paths and reported:
2,000 exact fixture users; 3,000,000 turns and 3,000,000 runtime attempts;
3,000,000 distinct membership calls with zero membership orphans; zero
attempt/job/user mismatches; 739,736 turns and attempts in the fixed 90-day
cohort; and 8,208 matching positive turn, attempt, and logical-call raw-edge
rows.  Global and exact-prefix counts matched, so no foreign state was present.

Both rollups were complete and clean.  The attempt maintenance watermark was
exactly complete through 2026-08-02 with `retained_from=2025-06-29` and no
pending retention.  This is intentionally distinct from the fixed report
partition: its full rollup days end on 2026-08-01 and 2026-08-02 remains the
upper partial raw day.  The retention boundary precedes both the earliest day
in the 365-day fixture and the 90-day report window, so it cannot truncate the
measured cohort.  No formal resume was run and the validated fixture remains
intact for the separately authorized timing run.

## Strengthened formal resume integrity design

The resume gate is a five-state machine: exact prevalidation, optional
read-only validation exit, business-producer proof, exact postvalidation, then
destructive-cleanup arming. Timing and EXPLAIN begin only after postvalidation;
any prefix/config/preflight/producer/postflight failure leaves the existing
fixture unarmed and untouched. Formal configuration is exactly 3,000,000
turns, 3,000,000 attempts, 2,000 users, and 365 history days. Non-formal
probes remain separately configurable and permanently ineligible to pass.

Expected source-window values are derived from the fixture's deterministic
formula (`(g * 104729) % (365 * 86400)`) and the fixed half-open production
window/raw-day bounds. The derivation, rather than an artifact literal, must
produce exactly 739,736 rows in the 90-day cohort and 8,208 matching turn,
attempt, and logical-call raw-edge rows.

Resume DB integrity is checked inside one read-only, repeatable-read snapshot.
Set-based bidirectional `EXCEPT`/anti-joins compare canonical raw-source facts
with both v2 daily grains, attempt dimensions, and call memberships; volatile
row IDs and refresh timestamps are excluded. Membership equality covers local
day, user, lane, call, requested/resolved provider/model, usage-known and gap
fields, while the source proof establishes the fixture's one-to-one attempt-
ID/call-ID mapping because the membership schema has no attempt-ID column.
Attempt dimensions cover canonical keys plus attempt, failure/retry, token-
known/token-sum, cost-kind/cost-count/cost-amount and TTFT summaries. Formal
reference state also requires zero corrections and zero rate cards.

Every expensive integrity statement has a formal-local bounded statement
timeout. Evidence records elapsed milliseconds, missing/extra counts, and
`EXPLAIN (FORMAT JSON)` plan/index evidence for the set-based checks. The
checks run against the same MVCC snapshot, avoiding a preflight assembled from
different database states.

### Strengthening implementation plan

1. Add RED tests that reject wrong formal users/history on both fresh and
   resume paths, and require the final gate to bind all four constants plus
   healthy pre/post integrity. Implement the minimal config/final-gate locks.
2. Add RED tests for a pure deterministic seed-formula calculator, including
   the fixed 739,736 cohort and 8,208 raw-edge result. Implement the cached
   formula and replace permissive snapshot source checks with exact equality.
3. Add RED tests for same-count source-time, daily key/aggregate, attempt
   dimension and membership-day corruption. Implement bounded, set-based,
   bidirectional DB checks and auditable plan/timing evidence in one read-only
   repeatable-read transaction.
4. Add spy RED tests for invalid, validation-only, producer-failure,
   postvalidation-failure and successful resume ordering. Implement the
   pre/producer/post gate, immutable snapshot comparison, and arm cleanup only
   after complete postvalidation.
5. Add final-gate RED tests for config and pre/post evidence mismatch, update
   the report evidence shape, and run focused tests after every GREEN cycle.
6. Run the real `--validate-resume-only` command, never formal resume; then run
   the complete focused suite, Ruff, compileall, harness self-test and
   diff-check before committing implementation.

### Strengthened read-only validation result

The strengthened 3M read-only validation passed with exit 0 and
`validated=true`. It ran before, and therefore never entered, the business
producer, timing, EXPLAIN-ANALYZE, or cleanup paths. The exact deterministic
source derivation and database facts agreed at 739,736 fixed-window rows and
8,208 rows for every raw-edge turn/attempt/logical-call counter.

All set-based semantic checks passed inside one read-only repeatable-read
snapshot with a 180,000ms per-statement hard limit:

- source turns: 8,159.331ms, 3,000,000 expected/actual, zero mismatches;
- source attempts: 22,739.376ms, 3,000,000 expected/actual, zero mismatches;
- daily users: 17,690.893ms, 731,199 expected/actual, zero mismatches;
- daily dimensions: 23,414.494ms, 731,199 expected/actual, zero mismatches;
- attempt dimensions: 21,877.056ms, 731,199 expected/actual, zero mismatches;
- memberships: 29,815.493ms, 3,000,000 expected/actual, zero mismatches.

Membership integrity is one fail-closed gate composed of the pre-existing
bidirectional key proof and the strengthened per-key facts proof. The key
proof found 3,000,000 distinct calls, zero membership orphans, and zero
attempts without membership; source identity separately proved 3,000,000
distinct attempt IDs and calls. The facts proof then checked every matched row's
Shanghai local day, user, cohort lane, call, requested/resolved provider and
model, effective-usage flag, and both ordinal-gap fields with zero mismatches.

Two earlier dry attempts correctly failed closed at the membership statement's
180-second timeout and did not clean the fixture. The final exact decomposition
removed redundant wide CTE materialization and repeated full-field anti-joins;
it preserved bidirectional key equality plus per-row fact equality and reduced
the membership check to 29.815 seconds. No formal resume was run, and the
dedicated fixture remains intact.

### Remaining source-field closure

The formal gate also binds every fixture user's parsed `created_at` instant to
the deterministic seed value (`SCALE_NOW_UTC - 365 days`). A PostgreSQL
mutation test changes one user's valid timestamp by one second without changing
any fixture count and proves that validation rejects it. This makes the
registered/activated population inputs exact rather than merely cardinality
complete.

The source-attempt proof now additionally requires the following eight fields
to be exactly `NULL`, as emitted by the seed path: `installation_id`, `runtime`,
`turn_id`, `round_id`, `provider_request_id`, `usage_unknown_reason`,
`latency_ms`, and `rate_card_version`. Eight parameterized PostgreSQL mutation
tests change one field on one attempt at a time, preserve every fixture count,
and prove that the same bidirectional exact query rejects all eight mutations.
Attempt-table `created_at` and `updated_at` are deliberately the only omitted
source columns: they are PostgreSQL wall-clock defaults/maintenance metadata,
not deterministic seed facts, so a resumed fixture cannot reproduce their
values exactly.

The widened statements passed PostgreSQL `EXPLAIN (FORMAT JSON)` parsing and a
bounded small-data execution test. A fresh real 3M read-only validation then
passed with exit 0 and retained the original 180,000ms per-statement limit:

- source turns: 8,304.616ms, 3,000,000 expected/actual, zero mismatches;
- source attempts: 23,860.519ms, 3,000,000 expected/actual, zero mismatches;
- daily users: 18,666.285ms, 731,199 expected/actual, zero mismatches;
- daily dimensions: 24,762.993ms, 731,199 expected/actual, zero mismatches;
- attempt dimensions: 23,552.349ms, 731,199 expected/actual, zero mismatches;
- memberships: 33,984.986ms, 3,000,000 expected/actual, zero mismatches.

The same snapshot reported `invalid_created_at=0`. This was again
`--validate-resume-only`: no business producer, formal timing, or cleanup ran,
and the dedicated fixture remains intact.

Fresh closure verification passed: 226 focused tests, Ruff over both perf
scripts and both focused test modules, compileall for both perf scripts, and the
harness self-test (percentiles, sensitive-column guard, and half-open time
range). The PostgreSQL EXPLAIN/small-data subset separately passed 2 tests.

## Unified fixture and business-proof ordering

The first real formal resume attempt failed safely before cleanup was armed.
The exact prevalidation was followed by the empty-database business/pool probe
while the full 3M fixture was still installed; the probe's production Usage
attempt subsection and maintenance rebuild reached their unchanged 3,000ms
statement limits. A fresh read-only exact validation after that failure passed
with all source/rollup counts unchanged, all six semantic mismatch counts zero,
`invalid_created_at=0`, and clean complete watermarks. The retained fixture was
therefore intact and was not cleaned during this correction.

Fresh and resume now share one state machine after fixture acquisition:
full-fixture timing/EXPLAIN, exact cleanup, global empty-database proof, then the
commit-bound business/pool producer in the same empty dedicated database.
Evidence includes the ordered phase trace, `post_fixture_empty_counts`, and
business pre/post database counts. All three count maps must be present and
zero. Timing failure still runs fixture cleanup but prohibits the producer;
cleanup failure also prohibits it; business failure happens only after fixture
removal and records post-counts in `finally`. Every workflow failure produces a
structured failed artifact through an atomic same-directory temporary write,
`fsync`, and `os.replace`, so an existing artifact cannot become partial JSON.

The producer remains sealed to the full current Git commit and is still checked
by its original strict validator. A previously timing-dependent serialization
witness was made deterministic: its successful malformed-item consumption is
captured at the injection boundary rather than inferred later from queue size
after normal fanout traffic. No retry or validator relaxation was added.

A real 100-turn/100-attempt non-formal probe against a separately created local
temporary database completed this exact order:
`prepare_fixture`, `fixture_workload`, `fixture_cleanup`,
`post_fixture_count`, `business_pre_count`, `business_producer`,
`business_post_count`. Fixture-post, business-pre, and business-post global
counts were all zero; business status passed with a canonical digest bound to
the then-current full commit. Report p95 was 21.341ms unfiltered and 21.281ms
filtered. Its process exit was 1 only because non-formal evidence is permanently
ineligible to pass the formal gate. The temporary database was dropped and a
catalog check showed only the retained 3M database remained. No second formal
resume or 3M cleanup was run.

Fresh final verification for the unified order passed 233 focused tests plus
Ruff, compileall, the harness self-test, and diff-check.

### Single production workflow closure

Review found that `_run` had reproduced the tested state machine instead of
calling it. The duplicate phase/error/cleanup/business block was removed.
Production now builds five domain callbacks and calls
`_execute_scale_workflow` exactly once; that helper is the sole owner of phase
trace, cleanup status, failure classification, all three zero-count maps,
business status/result, and terminal phase. A successful complete
`business_result` contains both pool and commit-bound business evidence, and
top-level `business_path` is derived from that same object. Failed workflows
retain `business_result=null` and are atomically rendered by `_run`.

Seven real-entry integration scenarios call `_run`, the real state machine, and
the real atomic writer with stateful domain callbacks: fresh success, resume
success, timing failure with cleanup-zero and no producer, cleanup failure with
no producer, business failure after fixture-zero with a post-count in
`finally`, invalid resume with cleanup unarmed/no mutation, and read-only
validation-only. The invalid-resume RED also caught and removed a misleading
`exact_prevalidated` claim whose `pre` value was null. Fresh preparation still
arms cleanup immediately before the first seed write, while resume arms only
after exact prevalidation.

A new real 100-turn/100-attempt non-formal temporary-database probe completed
the single production state machine. Its terminal phase was complete, business
status passed, `business_result` contained `pool` and `business`, all three
twelve-counter maps were zero, and fixture cleanup residuals were zero. Report
p95 was 27.456ms unfiltered and 22.666ms filtered. The process exited 1 only
because non-formal evidence cannot pass the formal gate. The temporary database
was dropped, and a catalog check again showed only the retained 3M database.
No formal resume or retained-fixture cleanup ran. Fresh focused verification
passed 240 tests.

## First formal artifact: scale and cleanup failures

The first formal artifact was written atomically to
`docs/superpowers/evidence/2026-08-03-admin-usage-attempt-rollup-scale.json` and
failed closed. Provider/model-filtered p50/p95 were 4,243.152/4,456.927ms;
unfiltered p50/p95 were 4,840.773/5,649.255ms. The attempt-ledger EXPLAIN took
34,617.528ms filtered and 52,134.309ms unfiltered. Raw-edge guards all passed:
only 8,208 raw calls and bounded runtime-job/call probes were present. The
actual bottleneck was the complete-day logical-call path: 178,000 attempt
dimension rows plus 731,528 wide call memberships, expanded into approximately
2.51M or 3.70M scope rows and externally sorted with substantial temporary I/O.

A read-only SQL-only candidate removed the five-scope lateral expansion and
used five exact per-scope `count(DISTINCT call_id)` aggregates. An unfiltered
ordinary fetch still took 53,078.502ms for 2,017 rows. It therefore did not
remove the distinct-call work and was rejected; the 3,000ms production attempt
limit and p95 budget remain unchanged.

Cleanup independently failed because one prefix-wide user delete inherited the
3s timeout while cascading into 3M memberships. PostgreSQL rolled back the
whole transaction. The artifact's post-cleanup map and a subsequent read-only
inspection both showed the full fixture remained present; no business producer
ran and no formal retry or cleanup followed.

The proposed correction is documented in
`docs/superpowers/specs/2026-08-03-attempt-ranked-flags-and-resumable-cleanup-design.md`.
It removes the not-yet-deployed membership relation and ranks exact logical
call/gap counters into 32 fixed BIGINT columns on the existing daily attempt
dimensions. The design covers every provider/model/completeness selector,
requested/resolved breakdown, raw edge, replay/retention path, migration and
storage gate, plus independently timed, batched, crash-resumable fixture
cleanup. It is a design only and has not been implemented.
