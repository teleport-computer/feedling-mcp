# Chat Incremental Sync and Query Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace cross-worker 5,000-row chat reloads with durable versioned reconciliation and bounded database queries.

**Architecture:** PostgreSQL statement triggers record compact per-user change events and send version-only wake hints. Workers apply exact changed IDs, rebuild only the chat hot window on gaps, and move resident poll, Runtime V2 tail, and verify correctness off the process cache.

**Tech Stack:** Python 3.11+, PostgreSQL 16, psycopg 3, FastAPI/ASGI, LISTEN/NOTIFY, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-rds-egress-and-tee-incremental-sync-design.md`

## Global Constraints

- PostgreSQL is durable truth; NOTIFY is only a latency hint.
- Public chat/history/verify contracts and durable history retention do not change.
- Old and new workers must coexist during rolling deployment.
- Logs and metrics contain no document, ciphertext, API key, connection string, or raw user ID.
- Observe mode starts with a 5,000-row cache; cutover moves 5,000 → 1,024 → 256.
- A reset, event gap, or more than 256 pending events rebuilds only the chat hot window.
- Create primary migration `0098_chat_change_events` after current head `0097_v2_job_recovery_events`.

## File map

- `backend/alembic/versions/0098_chat_change_events.py`: version tables, triggers, notification function, poll index.
- `backend/db.py`: snapshot, event, batch-point, verify, and poll query primitives.
- `backend/core/store.py`: versioned chat cache state machine.
- `backend/core/wake_bus.py`: legacy/v2 parsing and channel-specific dispatch.
- `backend/chat/chat_core.py`, `backend/chat/routes_asgi.py`: point-query verify.
- `backend/chat/poll_core.py`, `backend/chat/service.py`: durable resident candidates.
- `backend/model_api_runtime/v2/serve_worker.py`, `backend/model_api_runtime/v2/jobs_store.py`: direct prompt tails and local commit application.
- `backend/voice/routes_asgi.py`, `backend/hosted/chat_send_core.py`: remove remaining broad chat reloads.
- `docs/ops/chat-incremental-sync-runbook.md`: flags, rollout, rollback, and diagnosis.

---

### Task 1: Transactional chat change capture

**Files:**
- Create: `backend/alembic/versions/0098_chat_change_events.py`
- Create: `backend/alembic_tee/versions/0034_chat_poll_index.py`
- Modify: `backend/tee_shadow/table_registry.py`
- Modify: `backend/admin/plaintext_shadow.py`
- Create: `tests/test_chat_change_events.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_v2_jobs_migration.py`

**Interfaces:**
- Produces `chat_change_state(user_id, version, updated_at)`.
- Produces `chat_change_events(user_id, version, operation, message_ids, created_at)`.
- Produces `{v:2,c:"chat",u:<user>,r:<version>}` on the existing PostgreSQL wake channel.

- [x] **Step 1: Write real-Postgres RED tests**

Cover schema/trigger existence, one and multi-row INSERT, UPDATE, one-row DELETE, 65-row DELETE, two users in one statement, transaction rollback, and deleting a parent `users` row. Assert one version per user per statement, sorted unique IDs, `reset` above 64 IDs, no row or notification after rollback, and no recreated change-control row during account-delete cascade.

- [x] **Step 2: Register the DB-backed test in CI and verify RED**

Add `tests/test_chat_change_events.py` to the explicit DB-backed list in `.github/workflows/ci.yml`, then run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_chat_change_events.py tests/test_v2_jobs_migration.py -q
```

Expected: FAIL because revision 0098 and its schema do not exist.

- [x] **Step 3: Implement migration and triggers**

Create separate AFTER STATEMENT INSERT, UPDATE, and DELETE triggers with transition tables. For each affected user whose `users` row still exists, atomically increment state, insert `upsert`, `delete`, or `reset`, and call `pg_notify`. Events contain IDs only, never `doc`; the existence guard prevents a parent-user cascade from recreating child control rows.

- [x] **Step 4: Create the poll index concurrently**

Inside Alembic `autocommit_block()` execute:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_messages_user_ts_seq
ON chat_messages (user_id, ts, seq);
```

- [x] **Step 5: Verify upgrade/downgrade and commit**

Run the tests from Step 2, downgrade to 0097, upgrade to head, and require one head. Then:

```bash
git add backend/alembic/versions/0098_chat_change_events.py tests/test_chat_change_events.py tests/test_v2_jobs_migration.py .github/workflows/ci.yml
git commit -m "feat(chat): capture durable per-user change events"
```

---

### Task 2: Bounded database primitives

**Files:**
- Modify: `backend/db.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_v2_cursor.py`

**Interfaces:**
- Produces `chat_load_hot_snapshot_strict(user_id: str, limit: int) -> tuple[int, list[dict]]`.
- Produces `chat_change_version(user_id: str) -> int`.
- Produces `chat_change_events_after(user_id: str, after_version: int, limit: int) -> list[dict]`.
- Produces `chat_get_many_strict(user_id: str, message_ids: list[str]) -> list[dict]`.
- Produces `chat_verify_reply_strict(user_id: str, ping_id: str, ping_ts: float) -> dict | None`.
- Produces `chat_poll_candidates_strict(user_id: str, since: float, redelivery_floor: float, limit: int) -> list[dict]`.

- [x] **Step 1: Write RED tests for all six primitives**

Test empty users, relational `msg_id/ts/seq` authority, ordered events, deduplicated point reads, verify source/role/parent/time rejection, and an unanswered row inside the one-hour redelivery window but older than a 256-row tail.

- [x] **Step 2: Run RED tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_db.py tests/test_v2_cursor.py -q
```

- [x] **Step 3: Implement the snapshot and event reads**

`chat_load_hot_snapshot_strict` uses one read-only repeatable-read transaction: read version, then newest rows, then normalize relational identity/order into each result. Validate non-negative versions and cap batch/ID inputs at 256.

- [x] **Step 4: Implement verify and resident candidate queries**

Verify uses `(user_id,msg_id)` point reads for ping and reply. Poll candidates select `ts > since` plus unanswered user rows newer than `redelivery_floor`, order by seq, and limit 256; claim ownership remains in the existing CAS.

- [x] **Step 5: Prove index use and commit**

Seed 14,000 rows for one test user and use `EXPLAIN (ANALYZE, BUFFERS)` to assert none of the new queries performs a full-table sequential scan. Then:

```bash
git add backend/db.py tests/test_db.py tests/test_v2_cursor.py
git commit -m "feat(chat): add bounded sync and poll queries"
```

---

### Task 3: Versioned UserStore reconciliation

**Files:**
- Modify: `backend/core/store.py`
- Modify: `backend/db.py`
- Create: `tests/test_chat_incremental_sync.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces `UserStore.reload_chat_hot_strict() -> list[dict]`.
- Produces `UserStore.ensure_chat_fresh(*, force: bool = False, target_version: int | None = None) -> bool`.
- Produces `UserStore.apply_committed_chat_rows(rows: list[dict], *, version: int | None = None) -> None`.

- [x] **Step 1: Write state-machine RED tests**

Cover continuous upsert, update, delete, reset, duplicate, gap, overflow, expired history, seq ordering, trimming, fail-open ordinary read, strict failure, and waiter wake only after successful application.

- [x] **Step 2: Register and run RED tests**

Register the DB-backed file in CI and run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_chat_incremental_sync.py -q
```

- [x] **Step 3: Add cache state and limit parsing**

Add `chat_version`, `chat_max_seq`, and private `chat_messages_by_id`. Parse `FEEDLING_CHAT_HOT_CACHE_LIMIT`, default 5000, clamp 64–5000. Update list/index atomically under `chat_lock`.

- [x] **Step 4: Implement reconciliation**

Coalesce ordinary version checks for one second; forced wakes bypass coalescing. Read DB outside `chat_lock`, then atomically apply. Reset/gap/overflow calls only `reload_chat_hot_strict`, never `reload()`.

- [x] **Step 5: Preserve local fast paths and commit**

Use the ID index for append/finalize/update/delete and trim list/index together. Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_chat_incremental_sync.py tests/test_db.py tests/test_chat_response_finalize_cas.py -q
git add backend/core/store.py tests/test_chat_incremental_sync.py .github/workflows/ci.yml
git commit -m "feat(chat): reconcile worker caches by durable version"
```

---

### Task 4: Wake protocol and channel-specific dispatch

**Files:**
- Modify: `backend/core/wake_bus.py`
- Modify: `tests/test_wake_bus.py`
- Modify: `tests/test_chat_poll_cross_worker_staleness.py`

**Interfaces:**
- V2 chat wakes call `ensure_chat_fresh(force=True,target_version=r)`.
- Legacy chat wakes call `reload_chat_hot_strict()`.
- Frames/blob/proactive refresh only their own component.

- [x] **Step 1: Add RED compatibility tests**

Cover old receiver/new payload, new receiver/old payload, same-worker v1 suppression, v2 without origin, malformed version, duplicate version, channel isolation, and reconnect catch-up.

- [x] **Step 2: Run RED tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_wake_bus.py tests/test_chat_poll_cross_worker_staleness.py -q
```

- [x] **Step 3: Implement targeted dispatch and modes**

Validate `FEEDLING_CHAT_SYNC_MODE=legacy|observe|incremental` at startup. Preserve strict job-cancel parsing. Observe mode compares legacy/incremental ID+seq hashes for a deterministic 1% of users and records fixed mismatch slugs only.

- [x] **Step 4: Run tests and commit**

```bash
git add backend/core/wake_bus.py tests/test_wake_bus.py tests/test_chat_poll_cross_worker_staleness.py
git commit -m "feat(chat): dispatch versioned targeted wake events"
```

---

### Task 5: Point-query verify loop

**Files:**
- Modify: `backend/chat/chat_core.py`
- Modify: `backend/chat/routes_asgi.py`
- Modify: `backend/core/store.py`
- Modify: `tests/test_verify_loop_cross_worker.py`
- Modify: `tests/test_chat_response_verify_reload.py`
- Modify: `tests/test_verify_loop_gc_dangling_pointer.py`
- Modify: `tests/test_bootstrap_status_verify_ping.py`

**Interfaces:**
- Consumes `db.chat_verify_reply_strict` and `db.chat_get_strict`.
- Preserves response schema and maximum timeout.

- [x] **Step 1: Add RED tests**

Assert no `store.reload()`, cross-worker wake success, lost-NOTIFY two-second fallback, two concurrent verifies with isolated cleanup, timeout cleanup, and protection of ordinary replies.

- [x] **Step 2: Run the verify slice and confirm RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_verify_loop_cross_worker.py tests/test_chat_response_verify_reload.py tests/test_verify_loop_gc_dangling_pointer.py tests/test_bootstrap_status_verify_ping.py -q
```

- [x] **Step 3: Implement event-first verification**

Register waiter after ping commit, recheck to close the registration race, then wait in at-most-two-second intervals and point-read. Cleanup exact ping/reply IDs in `finally`. Replace negative response-admission reload with exact ping lookup.

- [x] **Step 4: Run tests and commit**

```bash
git add backend/chat/chat_core.py backend/chat/routes_asgi.py tests/test_verify_loop_cross_worker.py tests/test_chat_response_verify_reload.py tests/test_verify_loop_gc_dangling_pointer.py tests/test_bootstrap_status_verify_ping.py
git commit -m "perf(chat): verify resident loop with point reads"
```

---

### Task 6: Durable resident poll and Runtime V2 tail

**Files:**
- Modify: `backend/chat/poll_core.py`
- Modify: `backend/chat/service.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/voice/routes_asgi.py`
- Modify: `backend/hosted/chat_send_core.py`
- Modify: `tests/test_chat_poll_cross_worker_staleness.py`
- Modify: `tests/test_v2_serve_worker.py`
- Modify: `tests/test_v2_p0_history_safety.py`
- Modify: `tests/test_chat_response_finalize_cas.py`
- Modify: `tests/test_voice_gateway.py`

**Interfaces:**
- Resident selection consumes `chat_poll_candidates_strict`; `chat_try_claim_reply` remains final CAS.
- Runtime V2 consumes `chat_messages_after_seq(..., through_seq=..., oldest_first=False)`.
- Commit paths consume `apply_committed_chat_rows`.

- [x] **Step 1: Add cache-boundary RED tests**

With more than 256 rows, assert an older unanswered row in the redelivery window remains claimable. Assert Runtime V2 returns the identical seq membership and does not call `reload_chat_strict()`.

- [x] **Step 2: Run focused RED tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_chat_poll_cross_worker_staleness.py tests/test_v2_serve_worker.py tests/test_v2_history_safety.py tests/test_chat_response_finalize_cas.py -q
```

- [x] **Step 3: Refactor resident and V2 reads**

Use durable candidates while preserving batch 5, one-hour window, claim TTL, source exclusions, abandoned-claim exemption, legacy-adjacent repair, and superseded-tail CAS. Read V2 prompt rows directly by seq and frozen `through_seq`.

- [x] **Step 4: Remove broad post-commit reloads**

Apply returned parent/reply rows locally. Voice/hosted paths use `ensure_chat_fresh(force=True)` only when the committed row is unavailable.

- [x] **Step 5: Run tests and commit**

```bash
git add backend/chat/poll_core.py backend/chat/service.py backend/model_api_runtime/v2/serve_worker.py backend/model_api_runtime/v2/jobs_store.py backend/voice/routes_asgi.py backend/hosted/chat_send_core.py tests/test_chat_poll_cross_worker_staleness.py tests/test_v2_serve_worker.py tests/test_v2_history_safety.py tests/test_chat_response_finalize_cas.py
git commit -m "perf(chat): query durable poll and prompt windows directly"
```

---

### Task 7: Telemetry, docs, verification, and rollout

**Files:**
- Modify: `backend/core/store.py`
- Modify: `backend/core/wake_bus.py`
- Create: `docs/ops/chat-incremental-sync-runbook.md`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `tests/test_wake_bus.py`

**Interfaces:**
- Produces bounded sync/version/gap/fallback/hot-row/verify/poll metrics.
- Produces exact `legacy`, `observe`, `incremental`, rollout, and rollback procedures.

- [ ] **Step 1: Add privacy and enum tests**

Assert telemetry uses fixed mode/result/reason values, hashes user IDs, and rejects event docs, ciphertext, keys, and DSNs.

- [ ] **Step 2: Implement telemetry and documentation**

Document flags, event cleanup, gap diagnosis, 5%/25%/50%/100% stable-user rollout, 5,000→1,024→256 cache sequence, CloudWatch NetworkTransmit, and `pg_stat_statements` checks. Update architecture and Unreleased changelog; public API remains unchanged.

- [ ] **Step 3: Run repository verification**

```bash
~/fleet/bus/which_tests.sh --vs origin/test
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
cd docs-site && npm run types:check && npm run lint && npm run build
```

Expected: no new failures beyond the verified DB-backed baseline; all docs commands exit zero.

- [ ] **Step 4: Run four-worker fault simulation**

Replace the full gunicorn command array with `-w 4`. Verify append/finalize/delete/clear, listener outage, direct DB mutation, reconnect, worker recycle, and legacy/v2 payload mixing.

- [ ] **Step 5: Commit and deploy through test**

```bash
git add backend/core/store.py backend/core/wake_bus.py tests/test_wake_bus.py docs/ops/chat-incremental-sync-runbook.md docs-site/content/docs/architecture.mdx docs-site/content/docs/changelog.mdx
git commit -m "docs(chat): add incremental sync rollout and observability"
```

Merge into `test`, deploy test, and record evidence before promotion. Production requires 24 hours with zero observe mismatch/gap, then stable-user rollout. Require at least 85% NetworkTransmit reduction or below 200–250 GB/day before reducing the hot window to 256.
