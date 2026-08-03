# P0-B Task 5 load/formal proof independent review

Reviewed commit: `deff128524dea481e85c32e9a4943db1c1bb211d`

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
