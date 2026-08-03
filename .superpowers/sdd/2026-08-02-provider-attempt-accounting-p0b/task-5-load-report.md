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
  p95 latency regression was +0.348584 ms.
- Every one of the 60 measured pool-contention candidate calls records its own
  `started_ns`/`finished_ns`; the validator recomputes its intersecting DB roles
  from the raw acquisition intervals.  No candidate had an empty intersection.
  Coverage was outer maintenance 60/60, rebuild 43/60, recorder 57/60,
  exporter 60/60, attempt importer 60/60, and core importer 59/60.  The real
  role intervals ranged from 8.12–44.15 ms for rebuild, 32.44–57.88 ms for the
  outer maintenance connection, 1.11–36.91 ms for recorder batches,
  35.13–68.82 ms for exporter, 21.85–64.23 ms for attempt importer, and
  16.53–50.74 ms for core importer.  Maintenance used twenty real dirty-day
  recompute inputs rather than repeated no-op ticks.
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
- Paired p95 hot-path regression was +0.092917 ms for queue saturation and
  +0.100708 ms for combined recorder failures, both strictly below the
  predeclared +5.0 ms budget.
- Probe cleanup left both watermark tables and the dirty-day table at zero.

A 100-turn/100-attempt end-to-end **non-formal** runner integration also reached
final cleanup.  Its report timings were 32.788 ms unfiltered and 21.973 ms
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
  turn/attempt watermark cleanup accounting.
- Focused verification: `93 passed, 1 skipped` across the new evidence tests,
  provider recorder tests and provider client tests.
- Ruff passed for both perf scripts and the new tests.
- Compileall passed for both perf scripts.
- `admin_usage_scale.py --self-test` passed its percentile, sensitive-column and
  half-open time-range checks.

The commit-bound JSON is generated after checkout by the producer/formal runner
and is intentionally not checked into the same commit: committing the artifact
would necessarily change HEAD and make its own commit binding stale.
