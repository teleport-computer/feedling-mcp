# Provider Attempt Accounting P0-B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a canonical, content-free provider-attempt ledger in the current business RDS and use it for call, retry, usage, latency, possibly-billed, and cost accounting.

**Architecture:** Each actual Hosted V2 provider HTTP attempt emits started/completed facts into a bounded process-local queue using `put_nowait`; a background recorder performs idempotent full-row upserts into current-RDS tables. Whole-turn metrics remain authoritative for turn outcomes, while the Usage view joins aggregate attempt facts and visibly reconciles recorded logical calls against whole-turn `model_calls`.

**Tech Stack:** Python 3, asyncio/threading primitives already in repo, psycopg 3, PostgreSQL/Alembic, pytest.

## Global Constraints

- Create `feat/provider-attempt-accounting` from the final P0-A head. Open a stacked PR whose base is `feat/admin-runtime-user-report`; retarget it to `test` after PR #146 merges.
- No synchronous database or network wait on a provider hot path. Queue full, recorder crash, RDS timeout, malformed optional metadata, and shutdown races must never change provider-call or user-reply behavior.
- Do not treat `backend/provider_attempt_ledger.py` / `user_logs` as canonical. Preserve it temporarily only for compatibility and explicitly label it legacy diagnostic data.
- Deterministic `attempt_id` derives from stable logical call identity plus outer and inner ordinals; completed events are full upserts and can recover a dropped started event.
- Store allowlisted accounting metadata only—never request/response bodies, messages, prompts, reasoning text, tool payloads, credentials, or raw headers.
- Use current business RDS only. No SQLite, Redis, Kafka, new RDS instance, new service, or deployment unit.

---

## Task 1: Add canonical RDS schema and lifecycle rules

**Files:**
- Create: `backend/alembic/versions/0075_llm_provider_attempts.py`; if P0-A consumes revision 0075, use `0076_llm_provider_attempts.py` with P0-A's revision as `down_revision`
- Create: `backend/provider_attempt_accounting.py`
- Modify: `backend/db.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_account_reset_purges_all_tables.py`
- Create: `tests/test_provider_attempt_accounting.py`

- [ ] Write failing migration tests for `llm_provider_attempts`, `llm_provider_attempt_corrections`, `llm_rate_cards`, and `llm_usage_rollup_watermarks`; primary/foreign keys; idempotency uniqueness; time/user/provider/model/report indexes; and account-deletion cascade/explicit belt-and-suspenders cleanup.
- [ ] Define typed content-free event/row constructors and strict enums for source, lane, state, outcome, error class, and completeness. Define `stable_attempt_id(call_id, outer_ordinal, inner_ordinal)` and test restart/replay determinism.
- [ ] Implement the migration with current-RDS tables and recoverable concurrent indexes. Rate cards are effective-dated and immutable by version; corrections append signed deltas/reasons rather than mutating historical raw facts; watermarks make rollups resumable.
- [ ] Add the new user-scoped table to account-reset cleanup and any table registry/verification inventory required by this repo. Do not add it to TEE shadow replication unless an existing invariant explicitly requires plaintext metric mirroring.
- [ ] Run migration/account-reset/unit tests and commit schema/domain files only.

## Task 2: Implement the bounded fail-open recorder

**Files:**
- Modify: `backend/provider_attempt_accounting.py`
- Create: `tests/test_provider_attempt_recorder.py`

- [ ] Write failing tests for non-blocking enqueue, bounded capacity, drop counters, lazy single worker startup, batched inserts, retry with bounded backoff, full completed-event upsert without prior started event, idempotent replay, and clean bounded shutdown.
- [ ] Write failure-injection tests where queue insertion, pool creation, SQL execution, serialization, and recorder-thread startup all fail; assert the caller receives no exception and no changed result.
- [ ] Implement a process-local bounded queue and daemon/background recorder using existing project primitives. The hot-path API returns immediately after `put_nowait`; it logs/rate-limits diagnostics and increments in-memory dropped counters without blocking.
- [ ] Implement batch full-row `INSERT ... ON CONFLICT (attempt_id) DO UPDATE`, with terminal facts never downgraded by a later started replay. Mark stale started-only attempts `possibly_billed` in a background reconciliation query, not on the call path.
- [ ] Run `pytest -q tests/test_provider_attempt_accounting.py tests/test_provider_attempt_recorder.py` and commit.

## Task 3: Instrument every Hosted V2 provider HTTP attempt

**Files:**
- Modify: `backend/provider_client.py`
- Modify: `backend/model_api_runtime/v2/tool_loop.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/extraction.py`
- Test: `tests/test_provider_client_async.py`
- Test: `tests/test_v2_worker_tool_loop.py`
- Test: `tests/test_v2_trajectory_unit.py`

- [ ] Add failing tests covering success, HTTP failure, timeout before headers, streamed first-byte timing, inner provider retry, outer logical retry, extraction lane, and terminal exception. Assert one stable attempt row per actual HTTP request and no duplicate from trace mirroring.
- [ ] Extend the existing attempt-trace envelope with content-free logical `call_id`, outer/inner ordinals, request start, first-byte, finish, provider/model/route, request ID if returned, token/cache usage, outcome/error class, and billing uncertainty.
- [ ] Emit a started event immediately before each actual HTTP request and a full completed event from success/error/finally paths. Keep trace attachment for in-process whole-turn aggregation but make accounting enqueue independent from trace consumption.
- [ ] Propagate stable job/user/lane/logical-call context from V2 worker/tool loop into provider client without global mutable request context. Verify retry ordinals and attempt IDs are identical across redelivery.
- [ ] Run the focused provider/V2 tests plus explicit failure-injection tests, then commit.

## Task 4: Switch Usage accounting to the ledger

**Files:**
- Modify: `backend/admin/usage.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/admin/data_track.py`
- Modify: `tests/test_admin_usage.py`
- Modify: `tests/test_v2_runtime_health.py`

- [ ] Add failing report tests proving turns/outcomes still come from `v2_turn_metrics`, while calls/retries/token/cache/reasoning tokens/TTFT/possibly-billed/cost come from attempt rows plus corrections/rate cards.
- [ ] Add reconciliation assertions: `logical_call_coverage = distinct recorded call_id / sum(v2_turn_metrics.model_calls)`; missing ordinal sequences are a separate attempt-gap metric; unknown usage/cost remains unknown instead of zero.
- [ ] Extend the repeatable-read Usage snapshot with ledger aggregates, effective-dated rate-card lookup, correction sums, coverage/gap panels, and provider/model/user breakdowns.
- [ ] Update HTML labels/tooltips so operators can distinguish whole-turn truth, attempt truth, possibly billed attempts, estimated cost, and coverage gaps.
- [ ] Run Usage/Runtime tests and commit.

## Task 5: Reconcile, retain, and prove no business impact

**Files:**
- Modify: `backend/provider_attempt_accounting.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_provider_attempt_recorder.py`
- Modify: `tests/test_admin_usage.py`

- [ ] Add an idempotent reconciler for stale started rows, correction/rollup watermarks, and retention. It must be rate-limited, bounded per pass, and safe under multiple workers using database locking/claim semantics already used in the repo.
- [ ] Add a load test that saturates/fails the recorder while provider calls complete at baseline latency; assert queue memory stays bounded and user-visible results/retries are unchanged.
- [ ] Run focused tests, the full non-API suite, migration upgrade tests, and the 90-day Usage performance proof. Record any baseline-only failures separately.
- [ ] Inspect the branch diff for content leakage and infrastructure additions. Push the branch and open the stacked PR against `feat/admin-runtime-user-report`, including fail-open and reconciliation evidence.
