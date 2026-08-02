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
- Profile is now a first-class `AttemptLane.PROFILE` value and migration 0076's
  lane constraint accepts `profile`; profile calls no longer degrade to
  `unknown`.
- No prompt, reply, reasoning text, tool payload, URL/hostname, credential,
  header, stack trace, or raw body was added to the accounting event or RDS row.
  Existing wire bodies remain only in the opt-in encrypted trajectory trace.
- No Admin Usage/reporting surface, deployment change, retry policy, or provider
  failover behavior was added.

## Fix round 1

### Accepted findings closed

1. **Collision-free child and vision scopes.** Task batches reject duplicate
   sibling ids before any child runs. Child attempt scopes combine the stable
   logical task-batch ordinal with the child call id, then hash the result;
   photo-tool scopes combine the stable tool-call id with its per-id invocation
   ordinal. Both counters are allocated before an await, so completion order
   cannot affect identity, and fresh dispatcher/observer instances reproduce
   the same identities on redelivery. Pinned vision uses the durable message id.
2. **Synchronous Hosted V2 vision dispatch.** The five sync provider adapters
   (OpenAI Responses, OpenAI-compatible Chat, Anthropic Messages, Bedrock
   Converse, and Gemini GenerateContent) now account at their actual HTTP POST
   seams. Outer retries keep the existing cadence and exception classes while
   receiving real outer ordinals; compatibility retries receive real inner
   ordinals.
3. **Monotonic compaction identities.** Checkpoint and catch-up invocation
   counters live outside their resetting retry loops and are keyed by stable,
   content-free work identities. Repeated CAS/refusal work advances ordinals
   while independent batches each start at one.
4. **Immediate terminal facts with revision semantics.** Revision 0 is started,
   revision 1 is the transport terminal fact emitted before any compatibility
   or outer retry decision, and revision 2 is optional protocol/post-processing
   enrichment. Usage normalization is fail-open. Revision 2 preserves the
   revision-1 HTTP outcome and finish time. The SQL upsert accepts only a
   strictly greater revision, so lower and same-revision replays are no-ops;
   either revision can insert a missing row independently.
5. **PROFILE schema lane.** Added `AttemptLane.PROFILE` and `profile` to the
   0076 schema constraint, with profile runtime and real migration coverage.

### RED / GREEN matrix

| Finding | RED boundary | GREEN boundary |
| --- | --- | --- |
| Child scopes | Duplicate ids executed; parallel/replayed children shared `v2job:73:provider:1`; reused ids collided across later batches | Duplicate siblings rejected; four calls across two logical batches are distinct and reproduce exactly on redelivery |
| Vision scopes / sync seam | Executor did not pass tool id; dedicated/pinned configs lost context; sync retries emitted no rows; reused tool ids collided | Tool/message-derived scopes propagate; same-id later invocation is distinct and replay-stable; real sync retry emits two started/terminal pairs |
| Checkpoint/catch-up | CAS/refusal retries reset invocation ordinal to one | Per-work counters advance monotonically and remain independent across batches |
| Terminal/revision | Completed events had no revision; terminal fact was absent at the compatibility-retry decision seam | Revision-1 terminal is present before retry; revision-2 enrichment preserves HTTP facts; usage parser failure still emits terminal unknown usage |
| PROFILE | Enum access raised `AttributeError`; schema rejected the lane | Domain/runtime tests pass and migration 0076 accepts a real profile row |

### Fix-round verification

- Final combined focused verification against local PostgreSQL — 283 passed.
- Focused V2/vision set — 197 passed against the local PostgreSQL fixture.
- Accounting/provider/compaction pure set — 66 passed.
- Migration plus full recorder set against local PostgreSQL — 20 passed;
  the real recorder test additionally proves lower and same revisions cannot
  overwrite a stored revision-2 successful outcome.
- Collision/replay identity tests — 2 passed after their explicit RED boundary.
- Ruff passed for all touched files; the legacy `serve_worker.py` import block
  was checked with its established E402/F401 exclusions. `compileall` and
  `git diff --check` passed.
