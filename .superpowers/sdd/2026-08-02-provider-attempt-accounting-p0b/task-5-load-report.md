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
- The raw acquisition stream contained Usage exporter/importers, recorder, and
  two simultaneously held attempt-maintenance connections.
- Provider results under pool contention matched baseline exactly.
- OpenRouter, Anthropic and Google each recorded exactly two real HTTP requests
  and one retry per sample, with zero business errors in every scenario.
- Queue saturation dropped 263 telemetry events while remaining bounded at its
  configured capacity.
- Startup, pool, SQL and serialization failures all produced recorder drops and
  did not alter provider results, exceptions, HTTP-attempt counts or retries.
- Paired p95 hot-path regression was +0.089416 ms for queue saturation and
  +0.112750 ms for combined recorder failures, both strictly below the
  predeclared +5.0 ms budget.
- Probe cleanup left both watermark tables and the dirty-day table at zero.

A 100-turn/100-attempt end-to-end formal-runner integration also reached final
cleanup.  Its report timings were 31.381 ms unfiltered and 20.609 ms filtered,
and all source/rollup/watermark/dirty counts were zero after cleanup.  The final
formal gate correctly returned false for that tiny fixture because PostgreSQL
selected a sequential attempt scan instead of the 3M-scale job index; the
index/full-history plan guards were not weakened to make a small fixture pass.

## TDD and verification

- RED then GREEN tests cover stale commit, digest tampering, missing digest,
  hand-written healthy summaries, incomplete providers, false paired-p95
  claims, non-interleaved execution claims, missing measured pool evidence,
  nested maintenance occupancy, pre-existing rollup-state refusal, and separate
  turn/attempt watermark cleanup accounting.
- Focused verification: `80 passed, 1 skipped` across the new evidence tests,
  provider recorder tests and provider client tests.
- Ruff passed for both perf scripts and the new tests.
- Compileall passed for both perf scripts.
- `admin_usage_scale.py --self-test` passed its percentile, sensitive-column and
  half-open time-range checks.

The commit-bound JSON is generated after checkout by the producer/formal runner
and is intentionally not checked into the same commit: committing the artifact
would necessarily change HEAD and make its own commit binding stale.
