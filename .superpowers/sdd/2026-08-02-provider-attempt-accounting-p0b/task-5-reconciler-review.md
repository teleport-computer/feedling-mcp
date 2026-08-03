# P0-B Task 5 reconciler review — commit `0349d44c`

## Verdict

- **Spec review: FAIL**
- **Code-quality review: FAIL**

The state-machine basics are sound, but the implementation does not yet meet
the promised global row budget or the no-business-impact RDS-load requirement.

## Findings

### Important 1 — `max_changed_rows` is not a global four-stream budget

`_discover_changes()` assigns the full configured limit independently to the
attempt, correction, and turn streams, then also processes one rate card
(`backend/model_api_runtime/v2/provider_attempt_rollup.py:565-665`). A tick can
therefore fetch roughly `3 * max_changed_rows + 1` source rows, not
`max_changed_rows` globally. This contradicts the requested global row budget
and weakens the RDS-load bound. The test named as a global-budget proof checks
only the number of distinct dirty days, not fetched/accepted source rows
(`tests/test_provider_attempt_rollup_reconciler.py:265-281`).

Required fix: spend one shared remaining-row counter across all four cursors,
and add a test that observes each query limit/cursor advance and proves the
sum cannot exceed the configured budget.

### Important 2 — stale-start reconciliation has no supporting index

Every default-on tick searches canonical attempts by
`source/state/finished_at/possibly_billed/started_at`, orders by
`started_at,attempt_id`, and limits the result
(`backend/model_api_runtime/v2/provider_attempt_rollup.py:693-716`). Neither the
0076 attempt indexes nor the 0077 indexes provide that access path; 0077 adds
only the `(updated_at,attempt_id)` runtime index for cursor discovery
(`backend/alembic/versions/0077_llm_usage_attempt_rollups.py:248-278`). On a
multi-million-row ledger, the steady-state case with zero stale rows can scan a
large part of the table every five minutes. A result-row limit does not bound
rows examined, so this fails the no-business-impact/RDS-load gate.

Required fix: add and exact-shape-test a recoverable partial index matching the
stale predicate and `(started_at,attempt_id)` order, then prove with an
attempt-scale `EXPLAIN (ANALYZE, BUFFERS)` that the query is index-bounded.

### Important 3 — sustained writer rate above one page per cadence never catches up

The production worker invokes the reconciler with defaults, once per 300-second
loop (`backend/model_api_runtime/v2/serve_worker.py:4157-4167,4170-4183,4187-4265`).
The attempt/correction/turn cursors consume at most 2,000 rows per tick
(`backend/model_api_runtime/v2/provider_attempt_rollup.py:24-29,763-835`), with
no environment tuning, immediate catch-up loop, lag metric, or backlog alert.
Thus any sustained stream above about 6.7 rows/s grows backlog indefinitely;
later-day reporting can remain permanently stale. The fixed cursor order plus a
full dirty-day budget can also repeatedly prevent later streams from advancing;
there is no fairness or writer-faster-than-tick test.

Required fix: define a bounded catch-up policy that remains under a global
per-tick time/row/day budget, expose cursor lag/backlog safely, and test both
sustained overload and cross-stream fairness. Do not solve this by removing SQL
timeouts or allowing unbounded loops.

## Confirmed correct in this slice

- bootstrap head snapshot and later discovery are transactionally separated in
  a way that retains post-snapshot changes;
- cursor updates and dirty insertion share one transaction, so injected crashes
  roll back both;
- dirty generations fence replay/day-rebuild races, and rate-card interval
  overflow advances via a new bounded replay generation;
- the turn `OLD`/`NEW` trigger preserves both Shanghai days and downgrade drops
  the trigger before its table;
- stale-start marking leaves revision unchanged, so late complete revision 1 can
  win;
- advisory lock ownership is session-scoped, unlock uncertainty closes the
  physical session, cancellation is checked at transaction/day boundaries, and
  both maintenance lanes have separate exception boundaries;
- default-on opt-out, safe exception-class logs, content-free SQL, and no new
  database/service/queue/deployment unit are present.

The day rebuild currently obtains a second pooled connection while the advisory
leader connection remains checked out. This is not an unbounded deadlock: pool
acquisition is capped at 0.5 seconds and the Runtime V2 entrypoint reserves
connection headroom. It does, however, consume two business-pool connections
for up to the statement timeout and should be included in the final load proof.

Retention and the final load/performance proof are explicitly deferred by the
implementation report, so this reconciler-slice review does not claim Task 5 as
a whole is complete.

## Fresh verification

- Local PostgreSQL focused suites:
  `tests/test_provider_attempt_rollup_reconciler.py`,
  `tests/test_provider_attempt_rollup_migration.py`, and
  `tests/test_provider_attempt_rollup.py` — **30 passed**, 21 warnings.
- Ruff for reconciler/migration/tests — passed.
- `py_compile` for reconciler/migration/worker — passed.
- `git diff 0349d44c^ 0349d44c --check` — passed.

An initial sandboxed PostgreSQL run was invalid because localhost network access
was denied; the fresh passing run used only the local
`127.0.0.1:55432/postgres` test database with approved access. No test or prod
RDS was accessed.
