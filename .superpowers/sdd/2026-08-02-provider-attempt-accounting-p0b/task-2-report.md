# P0-B Task 2 — bounded fail-open provider-attempt recorder

## Delivered

- Added `ProviderAttemptRecorder`: a process-local bounded queue whose public
  `record()` call only uses `put_nowait`, always returns `None`, and contains
  queue, worker-start, pool, serialization, SQL, retry, and shutdown failures.
- The lazy daemon worker batches content-free `ProviderAttemptEvent.as_row()`
  values with psycopg `cursor.executemany`.  It uses full-row
  `INSERT ... ON CONFLICT (attempt_id) DO UPDATE`; a completed insert stands on
  its own if the started event was dropped, and later started replays preserve
  terminal state/facts.
- The worker has bounded exponential backoff and runs stale started-only
  `possibly_billed` reconciliation off the hot path.  Completion clears an
  earlier stale-only flag.
- Added process-singleton `record_provider_attempt()` and bounded
  `shutdown_provider_attempt_recorder()` lifecycle entry points.  Neither is
  wired into `provider_client` or reporting in this task.

## TDD evidence

1. RED: `python3 -m pytest -q tests/test_provider_attempt_recorder.py` failed
   at collection with the expected missing `ProviderAttemptRecorder` import.
2. GREEN: the recorder tests cover enqueue/no pool access, bounded drops,
   one lazy worker, batch upsert, completed-first recovery/replay, bounded
   retry, background-only reconciliation, bounded shutdown, and injected
   queue/thread/pool/SQL/serialization failures.

## Verification

- `python3 -m pytest -q tests/test_provider_attempt_accounting.py tests/test_provider_attempt_recorder.py` — 29 passed.
- `python3 -m ruff check backend/provider_attempt_accounting.py tests/test_provider_attempt_accounting.py tests/test_provider_attempt_recorder.py` — passed.
- `python3 -m compileall -q backend/provider_attempt_accounting.py` — passed.
- `git diff --check` — passed.

## Boundaries / concern

- This task intentionally does not emit facts from provider code or consume
  them in Usage reports; those are Tasks 3 and 4.
- The queue is deliberately lossy under saturation or recorder failure.  The
  eventual report must surface that loss through ledger coverage rather than
  treating it as zero provider activity.
