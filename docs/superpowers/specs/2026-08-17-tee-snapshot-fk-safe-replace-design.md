# TEE Snapshot FK-Safe Replacement Design

## Context

The TEST TEE shadow scheduler reports two persistent snapshot failures:

- `agent_jobs` cannot be truncated because several tables reference it;
- `v2_trajectory_streams` cannot be copied while its referenced `agent_jobs`
  row is absent.

The implementation currently refreshes each SNAPSHOT-lane table with an
independent `TRUNCATE + COPY` transaction. That assumes SNAPSHOT tables do not
reference one another and are not referenced by other lanes. The live TEST TEE
schema disproves both assumptions: five SNAPSHOT tables and the CIPHERTEXT-lane
`v2_trajectory_reviews` table reference `agent_jobs`.

## Decision

Replace destructive `TRUNCATE + COPY` with a per-table, transactionally exact
merge:

1. Read the source row count, source/target columns, and the target primary-key
   columns.
2. Stream the source's common columns into a transaction-local temporary table
   on the TEE connection.
3. Insert missing rows and update existing rows by primary key.
4. Delete target rows whose primary keys are absent from the staged source.
5. Commit all three target-side operations together.

The source snapshot and target transaction remain per table. A target-side
failure rolls back the staged merge and leaves the previous table contents
intact. Existing polling and strict verification provide eventual convergence
for source writes that race the source read.

## Why This Approach

- Truncating every SNAPSHOT table together still fails because
  `v2_trajectory_reviews` is outside that lane and references `agent_jobs`.
- `TRUNCATE ... CASCADE` could erase decrypted CIPHERTEXT-lane data without
  rebuilding it in the same transaction.
- Dropping or disabling foreign keys weakens the TEE-primary schema contract.
- An exact primary-key merge changes only rows that differ from the source and
  lets PostgreSQL enforce foreign-key safety throughout.

Deleting a stale parent may cascade or null stale child references. That is
consistent with the source database: its foreign keys make it impossible for a
valid source child to reference a parent absent from the source. Retained parent
rows are updated in place, so their children are not disturbed.

## Guardrails

- Reject a table without a primary key before any target mutation.
- Reject the refresh if a primary-key column is not present in the common
  source/target column set.
- Preserve the existing row limit and column-drift reporting.
- Quote generated identifiers with `psycopg.sql`; registered table names must
  not become free-form SQL.
- Preserve the admin dry-run short circuit and the single-table admin action.
- Do not change Phase 4 blockers, delete historical tails, freeze writes, or
  change TEST database secrets as part of this fix.

## Verification

Regression tests must demonstrate:

- a parent table can refresh while a retained external child references it;
- new and updated parent rows converge without disturbing retained children;
- source-deleted parents are removed using normal FK semantics;
- a target-side COPY failure rolls the complete merge back;
- primary-key and column-drift guardrails remain effective;
- the existing snapshot, registry, verify, and Phase 4 tests pass.

After merge and deployment to TEST, rerun a real snapshot cycle, strict
verification, and Phase 4 dry-run. Only a clean post-deploy report can reopen
the separate TEE-primary cutover approval gate.
