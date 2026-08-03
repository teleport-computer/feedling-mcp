# P0-B Task 5 load/formal proof independent review

Reviewed commit: `deff128524dea481e85c32e9a4943db1c1bb211d`

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
