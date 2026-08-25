---
document_lifecycle: historical
canonical_owner: docs/RDS_TRACE_PARTITIONS_RUNBOOK.md
historical_reason: point-in-time
---
# RDS Trace T306 Release Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that the existing T306 changes in `origin/test` repair `trace_events` on an RDS-primary deployment and are safe to promote through TEST without direct production DDL.

**Architecture:** Treat T306 as an already-implemented selected-primary schema repair, not as code to recreate. Validate migration convergence and trace behavior locally, then use the standard `test` deployment path and verify the TEST RDS schema, public trace route, partition health, and absence of relation-missing errors.

**Tech Stack:** Alembic, PostgreSQL 16, psycopg, pytest, GitHub Actions, Phala CVM, curl.

**Spec:** `docs/superpowers/specs/2026-08-25-chat-snapshot-convergence-and-rds-trace-repair-design.md`

## Global Constraints

- Use the T306 implementation already present in `origin/test`: commits `c29bb3338c644dc3e74722636c3a238b57ce9b69` and `88cee1de007db71ef10909ae33595016bd3f8f47`.
- Do not create a second RDS trace migration or copy the table through the TEE shadow.
- `trace_events` remains selected-primary-local, registry lane `SKIP`, and `required_in_tee=True`.
- Do not run manual DDL against PROD.
- Do not promote to `main` until TEST migration and functional evidence are recorded.
- Preserve the public trace API contract and 30-day retention semantics.

---

### Task 1: Verify the T306 source and migration contract locally

**Files:**
- Verify: `backend/alembic/versions/0102_trace_events.py`
- Verify: `backend/alembic_tee/versions/0033_trace_events.py`
- Verify: `backend/tee_shadow/table_registry.py`
- Test: `tests/test_trace_events.py`
- Test: `tests/test_tee_table_registry.py`
- Test: `tests/test_pre_test_migration_convergence.py`
- Test: `tests/test_debug_trace_event_route.py`

**Interfaces:**
- Consumes: RDS revision `0102_trace_events`, revising `0101_chat_change_events`.
- Consumes: TEE revision `0033_trace_events` and byte-identical `_UP` SQL.
- Produces: local evidence that both selected-primary schemas expose the same trace table contract.

- [ ] **Step 1: Confirm T306 is an ancestor of the branch**

```bash
git merge-base --is-ancestor c29bb3338c644dc3e74722636c3a238b57ce9b69 HEAD
git merge-base --is-ancestor 88cee1de007db71ef10909ae33595016bd3f8f47 HEAD
```

Expected: both commands exit 0.

- [ ] **Step 2: Run migration, registry, trace storage, and route tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest \
  tests/test_trace_events.py \
  tests/test_tee_table_registry.py \
  tests/test_pre_test_migration_convergence.py \
  tests/test_debug_trace_event_route.py \
  -q
```

Expected: all tests pass against real local RDS and TEE test databases. The output must not report database-backed skips.

- [ ] **Step 3: Inspect the migration heads and shared SQL contract without a transient test database**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest \
  tests/test_pre_test_migration_convergence.py::test_rds_pre_and_test_heads_converge \
  tests/test_pre_test_migration_convergence.py::test_tee_migrations_reuse_the_rds_contract_sql \
  tests/test_trace_events.py::test_migration_has_beijing_bounds_no_fk_and_stable_indexes \
  -q
```

Expected: `3 passed`. These assertions prove the RDS head, byte-identical RDS/TEE `_UP`, table/index shape, Beijing partition bounds, and absence of an account foreign key.

---

### Task 2: Integrate through the repository branch flow

**Files:**
- Verify: `.github/workflows/ci.yml`
- Verify: repository branch-flow checks

**Interfaces:**
- Consumes: the Chat repair commits and the T306 commits already in `test` history.
- Produces: a pull request from `fix/chat-snapshot-convergence-20260825` to `test` with local evidence.

- [ ] **Step 1: Rebase or merge the latest `origin/test` before final verification**

```bash
git fetch origin
git merge origin/test
```

Expected: no unresolved conflicts. If `origin/test` advanced, rerun both plans' local regression commands.

- [ ] **Step 2: Push the ordinary repair branch**

```bash
git push -u origin fix/chat-snapshot-convergence-20260825
```

- [ ] **Step 3: Open the pull request against `test`**

```bash
gh pr create \
  --base test \
  --head fix/chat-snapshot-convergence-20260825 \
  --title "fix(chat): guarantee strict snapshot convergence" \
  --body "Production reproduced 57 strict-snapshot starvation failures in 10 minutes. This change keeps two optimistic reads and makes the third per-user snapshot serialized, preserving the three-query bound. T306 commits c29bb333 and 88cee1de are already in the test baseline and repair RDS-primary trace_events. Local evidence: focused RED/GREEN tests plus chat, migration, registry, trace storage, and trace route regression slices. TEST gate: verify migration head, trace partitions, authenticated trace smoke, Chat smoke, and zero relation-missing/strict-snapshot errors before any main promotion."
```

The PR body must list the production reproduction counts, RED/GREEN test evidence, the T306 commits, and the TEST validation checklist. It must not target `main`.

---

### Task 3: Validate the standard TEST deployment

**Files:**
- Verify: TEST RDS selected-primary schema
- Verify: TEST backend logs and public API

**Interfaces:**
- Consumes: merged `test` branch and its standard TEST deployment workflow.
- Produces: evidence required before any `test -> main` promotion.

- [ ] **Step 1: Confirm the TEST deployment workflow succeeds**

```bash
gh run list --branch test --workflow ci.yml --limit 10
gh run view <run-id> --log-failed
```

Expected: the merge commit's build, tests, migration, and TEST deployment jobs succeed.

- [ ] **Step 2: Verify TEST RDS migration and partition state read-only**

Load `TEST_DATABASE_URL` from the repository `.env` without printing it, then run:

```sql
SELECT version_num FROM alembic_version;
SELECT to_regclass('public.trace_events'),
       to_regclass('public.trace_events_default');
SELECT count(*) AS leaf_partitions,
       count(*) FILTER (WHERE relid::regclass::text = 'trace_events_default')
         AS default_partitions
FROM pg_partition_tree('public.trace_events'::regclass)
WHERE isleaf;
SELECT count(*) FROM trace_events_default;
```

Expected: migration head includes `0102_trace_events` or a later descendant, both tables exist, exactly one DEFAULT partition exists, dated partitions exist, and DEFAULT contains zero rows.

- [ ] **Step 3: Run a disposable authenticated trace smoke test on TEST**

Use a disposable TEST account and its API key:

```text
POST /v1/debug/trace/enable  {"enabled": true}  -> 200
POST /v1/debug/trace/event   {bounded synthetic event} -> 200
GET  /v1/debug/trace?limit=10 -> 200 and contains the synthetic event
DELETE /v1/debug/trace       -> 200
GET  /v1/debug/trace?limit=10 -> synthetic event absent
```

The synthetic event must contain no production user content or secrets. Remove/reset the disposable account after the smoke test through the normal TEST API if the test flow created one.

- [ ] **Step 4: Verify TEST Chat and logs**

Run the existing TEST Chat history, poll, and response smoke path, then inspect backend logs for the observation window.

Expected:

```text
chat cache changed during strict snapshot reload = 0
relation "trace_events" does not exist = 0
/v1/debug/trace 5xx = 0
Chat business 5xx = 0
```

- [ ] **Step 5: Record promotion evidence**

Record the TEST commit, workflow URL, migration head, trace smoke result, Chat smoke result, log window, and four zero-error counts in the PR or release handoff. Do not promote or mutate PROD as part of this step.
