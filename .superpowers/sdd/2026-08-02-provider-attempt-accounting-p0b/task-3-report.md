# P0-B Task 3 — Hosted V2 provider-attempt instrumentation

## Delivered

- Instrumented the shared async JSON dispatch seam used by every Hosted V2
  provider adapter. Each actual HTTP request emits a replay-stable started fact
  immediately before dispatch and one full terminal fact after response
  decoding, provider failure, timeout, cancellation, or unexpected exception.
- Extended the existing encrypted in-process attempt trace with content-free
  call identity, outer/inner ordinals, requested/resolved provider and model,
  transport and route fingerprint, request/first-byte/finish timestamps,
  request ID, normalized token/cache counters, bounded outcome/error class, and
  billing uncertainty. Accounting consumes only `http_attempt` facts, so outer
  retry trace markers cannot create duplicate ledger rows.
- Added immutable `ProviderAttemptContext` propagation from V2 jobs into tool
  rounds, extraction parse retries, trajectory review, summary checkpoint and
  compaction calls, and profile generation. Logical call IDs derive from stable
  job and content-free row/round identities, never claim attempt count or global
  mutable request state.
- Extended the Task 2 recorder row/upsert contract to persist the terminal
  timestamps, usage/cache counters, billing flag, latency, and TTFT fields that
  already exist in migration `0076_llm_provider_attempts`. A real PostgreSQL
  test executes the completed-event SQL against that schema.
- Wired explicit recorder startup before V2 worker slots and bounded shutdown
  off the provider path. Provider-side accounting remains fail-open and invokes
  only the non-blocking recorder enqueue API.
- Preserved the existing async adapter response contract while observing first
  response-body byte time through `httpx` streaming. Narrow fake clients that
  expose only `.post()` keep their established test/embedding seam.

## TDD evidence

1. RED: domain tests rejected the original event because terminal timestamps,
   usage/cache values, and latency fields were not accepted or serialized.
2. RED: provider tests failed before `ProviderAttemptContext` and provider-side
   recorder calls existed. Success, HTTP 503, timeout-before-headers, delayed
   first byte, compatibility retry, outer retry, recorder failure, redelivery,
   and unexpected terminal exception cases were added before their production
   behavior.
3. RED: V2 propagation tests showed the worker had no stable context binding,
   per-round call IDs, extraction retry IDs, recorder lifecycle, or explicit
   compaction call identity.
4. A focused regression exposed an accidental helper-body displacement while
   adding the worker helpers; source inspection restored the intended function
   boundary before continuing.
5. Broader provider tests exposed narrow fake clients without `.stream()`;
   production retains streaming TTFT measurement and a compatibility fallback
   preserves the pre-existing fake/client seam.

## Verification

- `python3 -m pytest -q tests/test_provider_client.py tests/test_provider_client_async.py tests/test_provider_client_async_reliable.py tests/test_provider_attempt_accounting.py tests/test_provider_attempt_recorder.py tests/test_v2_trajectory_unit.py tests/test_v2_profile.py tests/test_v2_profile_lane.py -x`
  — 205 passed, 1 skipped. The skip is the explicitly gated PostgreSQL
  recorder integration test when no test DSN is supplied.
- With local `DATABASE_URL` and `FEEDLING_TEST_PG`,
  `tests/test_provider_attempt_recorder.py::test_completed_event_executes_against_the_real_attempt_schema tests/test_v2_worker_tool_loop.py -x`
  — 40 passed. Alembic emitted its two existing `path_separator` deprecation
  warnings.
- `ruff check` on all touched Python files — passed.
- `python3 -m compileall -q` on all touched production modules — passed.
- `git diff --check` — passed.

## Self-review / boundaries

- Verified the five Hosted V2 provider-call surfaces: tool loop, extraction,
  trajectory review, compaction/checkpoint, and profile generation. The current
  runtime has no separate provider-failover dispatch path; no synthetic
  failover row or ordinal was introduced.
- The Task 1 lane constraint does not contain the pre-existing `profile` job
  lane, so profile attempts use the schema's typed `unknown` lane while retaining
  stable job/call/round identity. Changing that schema constraint is outside
  Task 3.
- No prompt, reply, reasoning text, tool payload, URL/hostname, credential,
  header, stack trace, or raw body was added to the accounting event or RDS row.
  Existing wire bodies remain only in the opt-in encrypted trajectory trace.
- No Admin Usage/reporting surface, deployment change, migration, retry policy,
  or provider failover behavior was added.
