# Runtime V2 Profile Durable Retry Verification

Date: 2026-08-14
Branch: `fix/v2-profile-durable-retry`
Base: `origin/test` at `6a7bf491` (includes merged PR #187 at `e752dc6c`)

## Scope verified locally

- `agent_jobs.available_at` is installed by the single Alembic head
  `0088_agent_jobs_available_at` and gates every claim/orphan/revalidation path.
- A retryable Profile failure moves the exact owner-, lease-, and
  runtime-generation-fenced Job back to delayed `pending`; it does not insert a
  successor Job.
- Queue metrics split `pending_ready` from `pending_delayed`; the compatibility
  key `pending` equals `pending_ready`. Watchdog claimability counts ready work
  only.
- Transient provider failures back off from 300 seconds to a 21,600-second cap.
  Shape failures receive three delayed retries, then terminate. Provider config,
  source/data, and unknown internal failures do not enter an unbounded timer
  loop.
- A recovered Job whose profile metadata still names a future scheduled retry
  is rescheduled before provider config decryption or Memory Garden reads.
- Ordinary post-Chat freshness coalesces without accelerating a delayed Job.
  Existing Dream force semantics and successful provider setup/activation can
  make that same Job ready now.
- No Dream scheduling gate or frequency was changed.

## Automated evidence

### Baseline after PR #187

```text
tests/test_v2_profile_lane.py
tests/test_v2_profile_refresh.py
tests/test_v2_profile_storage.py
tests/test_v2_profile_prompt.py

53 passed, 2 warnings in 0.77s
```

### Focused Runtime V2 matrix

```text
tests/test_v2_jobs_migration.py
tests/test_v2_jobs_store.py
tests/test_v2_watchdog.py
tests/test_v2_profile_retry.py
tests/test_v2_profile_lane.py
tests/test_v2_profile_refresh.py
tests/test_v2_profile_storage.py
tests/test_v2_profile_prompt.py
tests/test_v2_deterministic_compaction.py
tests/test_v2_compaction_integration.py
tests/test_v2_serve_worker.py

346 passed, 31 warnings in 8.78s
```

The tests used a real local PostgreSQL 16 service through both `DATABASE_URL`
and `FEEDLING_TEST_PG`; no DB-backed module was silently skipped.

### Repository-standard L1 regression

Command:

```bash
python -m pytest tests/ -q \
  --ignore=tests/e2e_model_api_test.py \
  --ignore=tests/test_api.py
```

Result:

```text
9492 passed, 3 skipped, 9 xfailed, 39 warnings, 3 subtests passed
in 430.61s (0:07:10)
```

The content-free `wake_bus` connection-close messages printed after 100% while
pytest removed its temporary databases; pytest exited successfully with no
failed tests.

### Static and syntax gates

The following completed with exit code 0 and no output:

```text
python -m compileall -q backend/model_api_runtime/v2 backend/hosted \
  backend/alembic/versions tests
python -m pyflakes backend/model_api_runtime/v2/profile_retry.py \
  backend/model_api_runtime/v2/jobs_store.py \
  backend/model_api_runtime/v2/worker.py \
  backend/hosted/config_store.py backend/hosted/setup_core.py
git diff --check
```

### Public contract and docs-site gates

```text
tests/openapi: 24 passed, 2 warnings in 0.66s
npm run types:check: PASS
npm run lint: PASS
npm run build: PASS (582/582 static pages generated)
```

The first sandboxed build attempt stopped making progress during Turbopack's
optimized-build phase. The same locked dependency tree built successfully when
rerun outside the restricted sandbox. `npm ci` reported seven audit findings in
the existing lockfile (one moderate, six high); no dependency versions were
changed as part of this Profile retry work.

## Documentation review

- Updated `docs/RUNTIME_V2_FLOWS.md` for durable delayed retries, ready/delayed
  queue semantics, unchanged Dream scheduling, and deterministic-only history
  coverage after PR #187.
- Updated the public Unreleased changelog because self-hosted user providers may
  receive bounded background retry calls.
- Updated `self-hosting.mdx`; its previous wording still described a legacy
  semantic summary fallback, which no longer exists after PR #187.
- No OpenAPI source/schema changed; OpenAPI regeneration was therefore not
  required. Contract tests plus docs-site type, lint, and production-build gates
  all passed.

## Test-environment evidence pending

No test, pre, or production environment was mutated during local verification.
After review and merge into `test`, append:

- deploy workflow/run and image pin;
- `/healthz` and attestation checks;
- installed Alembic head;
- fixed `4/2/2` pool child counts;
- one synthetic V2 user's baseline Chat and automatic Profile recovery times;
- ready/delayed queue observations, single-flight proof, and watchdog log scan;
- synthetic-account cleanup evidence.

Production and pre-production remain out of scope for this rollout.
