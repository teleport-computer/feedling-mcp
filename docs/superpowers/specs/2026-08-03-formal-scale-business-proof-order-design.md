# Formal Scale Business-Proof Ordering Design

## Problem

The business-path pool probe is deliberately an empty-database probe. A fresh
formal run invoked it before seeding, but the resume path invoked it between
exact prevalidation and postvalidation while the complete 3M fixture remained
installed. The production report and attempt-maintenance branches therefore
hit their unchanged 3,000ms statement limits against data the probe was never
designed to share. The resume failed before cleanup was armed, so the fixture
remained intact; a subsequent read-only exact validation confirmed every source
and rollup fact.

## Decision

Fresh and resumed formal runs will use one execution state machine. They differ
only in how the scale fixture is acquired:

1. Validate the dedicated local PostgreSQL identity and schema.
2. Acquire an exact formal fixture:
   - fresh: require an empty database, seed, and bootstrap rollups;
   - resume: run the existing read-only exact prevalidation.
3. Arm exact-prefix fixture cleanup.
4. Run scale report timing and EXPLAIN against the full fixture.
5. In `finally`, delete the fixture and its dedicated watermarks/dirty state,
   then collect exact global counts and require the whole dedicated database to
   be empty.
6. Only after verified empty state, run the commit-bound business/pool producer
   in the same database.
7. Collect producer pre- and post-counts and require both to be globally zero.
8. Evaluate the final gate and write the evidence artifact.

No second database, schema, service, or other infrastructure is introduced.
The production report's 15,000ms total deadline, its attempt subsection's
3,000ms limit, the maintenance probe's 3,000ms limit, and the resume semantic
queries' 180,000ms per-statement limit remain unchanged.

## Evidence Contract

The artifact records an ordered phase trace and the terminal phase. It retains
the existing exact source, rollup, timing, EXPLAIN, retention-index, cleanup,
and commit-bound business evidence. It adds:

- `post_fixture_empty_counts`: global source, rollup, watermark, dirty-day,
  rate-card, and user counts collected after fixture cleanup;
- `business_database_counts.pre`: the same global counts immediately before
  the producer;
- `business_database_counts.post`: the same global counts in a `finally`
  immediately after the producer returns or raises;
- structured failure evidence containing the failed phase and a bounded error
  type/message, plus fixture-cleanup outcome when applicable;
- an explicit producer status of `not_run`, `passed`, or `failed`.

The existing producer output remains sealed against the current full Git
commit and is revalidated by the final gate. The final gate additionally
requires successful fixture cleanup and all three new count maps to be present
and zero. A missing producer result can never be interpreted as a successful
business proof.

## Failure Semantics

- Resume prevalidation failure leaves the fixture untouched, records a failed
  artifact, and does not run timing or the producer.
- Timing or EXPLAIN failure still enters fixture cleanup. If cleanup succeeds,
  the artifact records the timing error and zero post-cleanup counts; the
  producer remains `not_run` and the command fails.
- Fixture-cleanup failure records both the primary error (if any) and cleanup
  error/counts, prohibits the producer, writes the artifact, and fails.
- Business-producer failure occurs only after verified fixture removal. Its
  own `finally` cleanup remains authoritative; the runner independently
  collects post-counts, records the failure, writes the artifact, and fails.
- Producer post-counts that are nonzero fail closed even if the producer
  returned evidence.
- Artifact output is attempted for every failure after argument/database
  validation. Artifact-writing failure must not replace the primary phase
  failure in the recorded in-memory error ordering.

## Implementation Boundaries

`admin_usage_scale.py` will gain a reusable global-count collector, a zero-count
validator, a phase/error recorder, and a single orchestration path around the
existing seeding, bootstrap, timing, cleanup, and producer operations. The
business producer itself and its commit sealing remain unchanged. Existing
exact resume snapshot collectors remain the sole authority for admitting a
retained fixture.

## Test Strategy

Strict RED/GREEN tests will establish:

1. successful fresh and resume ordering, including producer execution only
   after verified fixture cleanup;
2. timing failure performs cleanup, writes structured failed evidence with
   zero counts, and never calls the producer;
3. cleanup failure writes failed evidence and never calls the producer;
4. business failure starts only after fixture counts are zero, records post
   counts in `finally`, and fails after the fixture has already been removed;
5. nonzero post-fixture, producer-pre, or producer-post counts fail closed;
6. missing/stale commit-bound producer evidence still fails the final gate;
7. validation-only resume remains read-only and exits before timing, cleanup,
   or producer.

A small non-formal database probe will exercise the unified order without
touching the retained 3M fixture. Focused PostgreSQL tests, Ruff, compileall,
self-test, and diff-check complete verification. No formal 3M resume or cleanup
will be run during implementation.

## Single-Orchestrator Correction

Production `_run` must invoke `_execute_scale_workflow` exactly once for every
non-validation run. It must not duplicate phase tracing, exception
classification, cleanup, zero-count gates, producer ordering, or terminal-state
selection. The callbacks supplied by `_run` own only domain operations:

- `prepare_fixture(arm_cleanup)` handles the one intentional branch. Fresh
  runs require an empty database, arm cleanup before the first seed write, then
  seed and bootstrap. Resume runs exact prevalidation first and arm cleanup only
  after it passes. This preserves partial-fresh-seed cleanup while leaving an
  invalid retained fixture untouched.
- `run_fixture_workload()` owns the shared retention evidence, production
  report timing, and EXPLAIN checks.
- `cleanup_fixture()` performs exact-prefix cascade verification plus watermark
  and dirty-state removal and writes the existing top-level cleanup evidence.
- `collect_database_counts()` returns the twelve global counters.
- `produce_business()` returns the complete `{"pool": ..., "business": ...}`
  result after the helper has proved the database empty.

The helper is the only owner of `phase_trace`, `terminal_phase`, structured
`failure`, cleanup status, the three zero-count maps, `business_status`, and
`business_result`. `_run` copies the returned workflow unchanged into the
artifact, derives top-level `business_path` from a successful
`workflow.business_result.business`, evaluates the existing gates, and uses the
atomic writer on both success and failure. A failed workflow retains
`business_result=null`; its artifact must still be complete JSON and return 1.

Real-entry integration spy tests call `_run`, not only the helper. A replaceable
runtime-loading boundary supplies production modules normally and controlled
stateful fakes in tests; lower-level seed/report/cleanup operations are spied at
their existing module-function boundaries. Tests cover fresh and resume
success, timing failure with zero cleanup and no producer, cleanup failure with
no producer, business failure after the fixture is zero, invalid resume, and
read-only validation-only. Every test parses the actual atomically written
`--output` file and no test creates a large fixture.
