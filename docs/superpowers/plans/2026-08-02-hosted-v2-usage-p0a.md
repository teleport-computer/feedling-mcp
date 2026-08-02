# Hosted V2 Usage P0-A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Hosted Runtime V2 token/model analytics into an independent Admin Usage view while Runtime Health retains per-user delivery reliability.

**Architecture:** A normalized `UsageQuery` drives one repeatable-read, read-only RDS snapshot that returns overview, averages, daily, per-user, and provider/model aggregates directly from `v2_turn_metrics`. Admin rendering consumes that content-free payload through an independently degradable Usage page; Runtime Health continues using the existing delivery rows without rendering token/model rows.

**Tech Stack:** Python 3, FastAPI/ASGI, psycopg 3, PostgreSQL/Alembic, server-rendered HTML, pytest.

## Global Constraints

- Work on `feat/admin-runtime-user-report`; update existing PR [#146](https://github.com/teleport-computer/feedling-mcp/pull/146), whose base remains `test`.
- No new service, database, cache, broker, deployment unit, or client telemetry path in P0-A.
- All source timestamps are UTC. Presentation defaults to `Asia/Shanghai`; custom windows are half-open `[start_at_utc, end_at_utc)` and at most 366 days.
- Provider/model/lane/user filters must use bound SQL parameters. Never render prompt, reply, tool input/output, credential, or outbox payload content.
- Keep the existing Runtime Health summary, lane token totals, delivery health, and per-user delivery reliability. Remove only the per-user token/model table from that page.
- Use direct queries first. Add a rollup only if the required 10x-data 90-day `EXPLAIN (ANALYZE, BUFFERS)` proof cannot satisfy p95 under two seconds after index tuning.
- Every implementation commit names only the files changed by that task with `git commit --only`, so unrelated workspace changes cannot leak into the PR.

---

## Task 1: Normalize the Usage query contract

**Files:**
- Create: `backend/admin/usage.py`
- Modify: `backend/admin/data_track.py`
- Test: `tests/test_admin_usage.py`

- [ ] Write failing unit tests for default 30-day window, 24h/7d/30d/90d presets, custom ISO-8601 dates, `Asia/Shanghai` conversion to UTC, half-open bounds, 366-day maximum, invalid timezone fallback, and normalized optional `user_id`, `lane`, `provider`, `model`, and `completeness` filters.
- [ ] Run `pytest -q tests/test_admin_usage.py` and confirm the new tests fail because the contract does not exist.
- [ ] Add immutable `UsageQuery` and `parse_usage_query(args, now_utc: datetime | None = None)`. Keep `start_at_utc` and `end_at_utc` timezone-aware UTC datetimes; expose the chosen timezone and original filter strings for links/forms.
- [ ] Add `_usage_page_href()` in `backend/admin/data_track.py` so pagination, filters, and drill-down links preserve the normalized query without preserving invalid or unrelated parameters.
- [ ] Run `pytest -q tests/test_admin_usage.py` and commit with `git commit --only backend/admin/usage.py backend/admin/data_track.py tests/test_admin_usage.py -m "feat(admin): normalize usage report queries"`.

## Task 2: Build a coherent RDS usage snapshot

**Files:**
- Modify: `backend/admin/usage.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/alembic/versions/0074_runtime_user_delivery_indexes.py` only if the existing migration remains unshipped and an additional concurrent index is proven necessary; otherwise create the next linear migration under `backend/alembic/versions/`
- Modify: `tests/test_v2_jobs_migration.py` if an index changes
- Test: `tests/test_admin_usage.py`
- Test: `tests/test_v2_runtime_health.py`

- [ ] Add failing PostgreSQL tests that seed several users, UTC days, lanes, providers/models, reported and unknown token rows, failed turns, zero-call turns, retries, and cache facts. Assert one payload with `overview`, `averages`, `daily`, `users`, `models`, `filters`, and `coverage`.
- [ ] Assert registered-reference count, activated users, model-active users, metered users, active user-days, turns, calls, retries, failures, known input/output/total/cache tokens, unknown usage, and coverage denominators separately.
- [ ] Assert averages are distinct: total per calendar day, per active user-day, all activated user-day only without dimension filters, per metered turn, user-day p50/p75/p90/p95/max, calls per turn, and retries per turn. Filtered all-activated user-day must be `None`, not zero.
- [ ] Run `pytest -q tests/test_admin_usage.py tests/test_v2_runtime_health.py -k 'usage or runtime_user'` and confirm RED.
- [ ] Implement `jobs_store.usage_report_snapshot(query)` using one connection and `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`. Keep SQL fragments and bound parameters centralized in `backend/admin/usage.py`; use `FILTER`/`percentile_cont` deliberately and return `None` for unknown metrics.
- [ ] Keep `recent_runtime_user_report()` delivery-compatible during the transition, but extract/reuse delivery aggregation rather than making Usage depend on delivery tables.
- [ ] If query plans require a new index, use a recoverable `CREATE INDEX CONCURRENTLY` migration following 0071/0074 and add migration-shape tests. Otherwise document the measured existing index in the performance test and do not create a migration.
- [ ] Run the focused tests and commit with `git commit --only backend/admin/usage.py backend/model_api_runtime/v2/jobs_store.py backend/alembic/versions tests/test_admin_usage.py tests/test_v2_jobs_migration.py tests/test_v2_runtime_health.py -m "feat(admin): aggregate hosted v2 usage"` (omit unchanged paths).

## Task 3: Render the independent Usage page

**Files:**
- Modify: `backend/admin/data_track.py`
- Modify: `backend/admin/admin_core.py`
- Modify: `backend/asgi_app.py`
- Test: `tests/test_data_track.py`
- Test: `tests/test_data_track_runtime_view.py`
- Test: `tests/test_admin_usage.py`

- [ ] Add failing rendering tests for the `Usage` navigation entry and `/admin/data-track?view=usage`; default window/filter controls; overview and average cards; daily trend; per-user table; provider/model table; coverage/unknown markers; and user drill-down links.
- [ ] Add failure-domain tests: a Usage query failure renders an explicit Usage-unavailable page and does not affect `view=runtime`; delivery-report failure on Runtime only degrades the delivery section.
- [ ] Run `pytest -q tests/test_data_track.py tests/test_data_track_runtime_view.py tests/test_admin_usage.py` and confirm RED.
- [ ] Add `view == "usage"` dispatch in `admin_core.page_html()`. Bind `data_track._usage_report` to `jobs_store.usage_report_snapshot` in `backend/asgi_app.py`, matching existing dependency injection.
- [ ] Render semantic HTML with compact tables, escaped values, GET filters, preset buttons, coverage notes, and a user drill-down that remains within the same Usage query. Daily trend may use CSS bars/table cells; do not introduce a JavaScript chart dependency.
- [ ] Split `_render_runtime_user_report()` so Runtime renders only `用户交付可靠性`; remove its model rows and update explanatory copy. Retain global lane token metrics already used for health diagnosis.
- [ ] Run focused tests and commit with `git commit --only backend/admin/data_track.py backend/admin/admin_core.py backend/asgi_app.py tests/test_data_track.py tests/test_data_track_runtime_view.py tests/test_admin_usage.py -m "feat(admin): add hosted v2 usage view"`.

## Task 4: Prove scale, regression safety, and PR scope

**Files:**
- Modify: `tests/test_admin_usage.py`
- Modify: `tests/test_v2_runtime_health.py`
- Review: `docs/superpowers/specs/2026-08-01-admin-runtime-user-token-report-design.md` for consistency; leave unchanged when it already matches the delivered split

- [ ] Add a deterministic 10x-volume fixture or repository-supported load harness and record `EXPLAIN (ANALYZE, BUFFERS)` for the worst 90-day unfiltered and provider/model-filtered queries. Assert the expected index/range predicates and no accidental content column reads.
- [ ] Measure at least five warmed executions and record p50/p95 in PR #146. If p95 is at least two seconds, tune/index and repeat; if still over budget, implement a current-RDS rollup in this PR with an idempotent watermark and reconciliation test before declaring P0-A complete.
- [ ] Run `pytest -q tests/test_admin_usage.py tests/test_data_track.py tests/test_data_track_runtime_view.py tests/test_v2_runtime_health.py tests/test_v2_jobs_migration.py`.
- [ ] Run the repository's full non-API test command with `FEEDLING_TEST_PG`; compare any failures with untouched `test` and link baseline evidence in the PR rather than hiding failures.
- [ ] Inspect `git diff test...HEAD`, verify no public API/OpenAPI change and no infrastructure/config dependency, then commit the exact changed test paths with message `test(admin): verify usage report scale`.
- [ ] Push `feat/admin-runtime-user-report`, update PR #146 description with screenshots/query evidence, and wait for required checks before beginning its stacked child branch.

### Task 4A: Add deletion-safe current-RDS rollup schema

**Files:**
- Create: the next linear migration under `backend/alembic/versions/` for `v2_usage_daily_users`, `v2_usage_daily_dimensions`, `v2_usage_rollup_watermarks`, and the `(updated_at, id)` source cursor index
- Modify: `backend/db.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_account_reset_purges_all_tables.py`
- Modify: `tests/test_admin_usage.py`

- [ ] Write RED migration and lifecycle tests for nullable user FK cascade, `UNIQUE NULLS NOT DISTINCT`, all/metered/unknown overlapping subaggregates, token/cache known counts, exact provider/model latency samples, watermarks, cursor index, and redundant account-reset cleanup.
- [ ] Implement schema only; Alembic must not perform a multi-million-row backfill transaction. No fleet table without user attribution, trigger, new database, broker, cache, service, or deployment unit.
- [ ] Run migration/account deletion tests and commit exact files.

### Task 4B: Rebuild rollups from the authoritative source off the hot path

**Files:**
- Create: `backend/model_api_runtime/v2/usage_rollup.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Create: `tests/test_v2_usage_rollup.py`

- [ ] Write RED real-PostgreSQL tests for one-day recompute equivalence, all subaggregate columns, overlapping completeness, NULL/known-count semantics, idempotent `DELETE + INSERT`, repeatable-read day/bootstrap snapshots, bounded-lateness dirty-day discovery, advisory-lock competition, cursor CAS, crash rollback, bounded cursor/day batches, and user deletion.
- [ ] Implement a bounded existing-worker maintenance tick. It never runs from `record_whole_turn_metric()` and catches pool, lock, SQL, timeout, and shutdown failures so provider/reply/retry/heartbeat behavior cannot change.
- [ ] Keep bootstrap/refresh transactions short and repeatable-read. Delay the `(updated_at,id)` cursor behind a bounded-lateness safe horizon instead of repeatedly rescanning a fixed overlap window; expose withheld/backlogged source as non-zero lag and leave old complete rows readable until a day replacement commits.
- [ ] Run worker/failure-injection tests and commit exact files.

### Task 4C: Read rollup and raw edges from one exported snapshot

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/usage_reporting.py`
- Modify: `backend/admin/data_track.py`
- Modify: `tests/test_admin_usage.py`

- [ ] Write RED tests that compare rollup-backed and raw payloads exactly for all metrics, filters, partial-day edges, dirty/unready days, deletion, and unknowns; other timezones and incomplete bootstrap must fall back raw.
- [ ] Add real-PostgreSQL exported-snapshot concurrency tests: total three RR/RO connections, importer snapshot before any read, same MVCC result during a concurrent writer, process/RDS single-flight admission, short pool acquisition, statement timeout, cancel/rollback, serial fallback, and per-breakdown unavailable behavior.
- [ ] Implement Asia/Shanghai full-day rollup + disjoint raw edges/dirty days. Display rollup freshness/lag and never render stale/failed data as zero.
- [ ] Run report/Admin/Runtime regression tests and commit exact files.

### Task 4D: Repeat the final 3M gate and finish PR #146

- [ ] Run the checked-in scale harness against the production implementation, not prototype TEMP/UNLOGGED tables. Record source/rollup rows and size, all five warmed default and provider/model-filtered timings, p50/p95, exact latency cost, and key EXPLAIN nodes/buffers.
- [ ] Require default and filtered 90-day p95 below two seconds. If either fails, do not push or claim P0-A complete.
- [ ] Run the complete P0-A related suites, full repository non-API suite, Ruff/compile/diff checks, dependency direction tests, and compare baseline failures.
- [ ] Verify test/prod RDS PostgreSQL compatibility read-only, inspect `test...HEAD` for public API/docs/infrastructure scope, then push and update PR #146 with architecture, fail-open, deletion, freshness, performance, and test evidence.
