# TEE Snapshot FK-Safe Replacement Implementation Plan

> Design: `docs/superpowers/specs/2026-08-17-tee-snapshot-fk-safe-replace-design.md`

## Goal

Make the SNAPSHOT lane converge FK-connected tables without truncating retained
parents or cascading unrelated lane data, then deploy the fix to TEST and
re-evaluate the Stage B cutover gate.

## Global Constraints

- Work from a feature branch based on the latest `origin/test`; target the PR
  to `test`, never `main`.
- Use primary-key exact merge inside one TEE transaction per table.
- Never use `TRUNCATE CASCADE`, disable/drop foreign keys, or weaken Phase 4.
- Keep the existing `MAX_ROWS`, common-column drift behavior, failure reporting,
  scheduler isolation, and admin dry-run semantics.
- No TEST data cleanup, write freeze, secret mutation, or TEE-primary switch is
  authorized by this implementation approval.
- Use metadata and aggregate evidence only; do not print user content or DSNs.

## Task 1: Add Red FK Regression Tests

**Files:**

- Modify: `tests/test_tee_snapshot.py`

**Steps:**

1. Add paired source/TEE fixtures for a parent table and an external child table
   with a real foreign key.
2. Seed an existing TEE child referencing a retained parent.
3. Assert that `snapshot_table(parent)` succeeds, updates the parent, and leaves
   the retained child intact.
4. Cover source deletion and rollback on malformed target COPY payload.
5. Run the focused tests and record the expected pre-fix failure caused by
   `TRUNCATE`.

**Verification:**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_tee_snapshot.py -q
```

## Task 2: Implement Exact Merge

**Files:**

- Modify: `backend/tee_shadow/snapshot.py`
- Modify: `tests/test_tee_snapshot.py`

**Steps:**

1. Add target primary-key introspection in ordinal key order.
2. Reject missing/incompatible keys before target mutation.
3. Stage the binary payload in a transaction-local temporary table.
4. Execute quoted `INSERT ... ON CONFLICT ... DO UPDATE` (or `DO NOTHING` for
   key-only tables).
5. Delete target rows absent from the staged source using null-safe key joins.
6. Keep all target operations in one transaction and keep report shape stable.
7. Run focused tests to green, then temporarily reverse the implementation to
   prove the new regression test fails and restore it.

**Verification:**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_tee_snapshot.py \
  tests/test_tee_table_registry.py tests/test_tee_verify.py \
  tests/test_phase4_cutover.py -q
```

## Task 3: Review, Full Verification, and PR

**Files:**

- Review all branch changes.

**Steps:**

1. Run formatting/static checks applicable to the changed Python files.
2. Run the complete PostgreSQL-backed backend suite without silently skipped DB
   modules.
3. Inspect the diff for SQL identifier safety, rollback behavior, report
   compatibility, and scope containment.
4. Commit the design, plan, tests, and implementation.
5. Push the feature branch and open a PR targeting `test`.
6. Wait for and inspect required CI checks before merging through review.

## Task 4: TEST Deployment and Read-Only Cutover Recheck

**Steps:**

1. Deploy the merged `test` release through the existing GitHub Actions/Phala
   path without changing database secrets or schema mode.
2. Confirm main and runner release identity and public health endpoints.
3. Let or explicitly trigger the existing TEST shadow synchronization process;
   this writes only to the already-authorized shadow TEE database and must be
   separately approved immediately before execution if a manual trigger is
   needed.
4. Inspect aggregate sync results and run strict verification.
5. Rerun Phase 4 dry-run and report remaining blockers.
6. Stop before write freeze, Phase 4 apply, secret changes, or TEE-primary
   deployment and request a separate explicit approval.
