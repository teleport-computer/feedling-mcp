# TEE Terminal Ciphertext Preservation Design

**Date:** 2026-08-20

**Status:** Approved direction; implementation pending

**Release baseline:** `a9be073a0787f9f561548ee7e9239f3c4a4249c7`

## Problem

The PROD TEE shadow currently has 887 terminal
`tee_pending_device_migration` rows: 798 `decrypt_failed` rows and 89
PendingDeviceMigration/local-only rows. They are historical ciphertext that the
current enclave cannot turn into the plaintext projection expected by the
shadow replicator. They are not a recent broad regression: 794 were marked on
2026-07-16 and 2026-07-17, and 773 of the 798 deterministic decrypt failures
belong to five users.

The affected data must not be silently discarded merely to satisfy the Phase 4
`pending == 0` gate. It also must not force account-wide preference changes.
Of the 59 affected user IDs, 58 map to current users whose
`content_encryption` preference is unset (default off), not explicitly off; one
TEE-side user is missing and must remain a blocker until reconciled or proven
orphaned.

The goal is to preserve the exact source ciphertext in TEE PostgreSQL, retain
an auditable marker, and let Phase 4 distinguish preserved history from work
that is still unresolved. Preservation prevents data loss; it does not claim
that the current enclave can decrypt the preserved bytes.

## Decision

Add a per-row **terminal ciphertext preservation lane**. The lane copies only
rows already carrying an allowlisted terminal pending reason, verifies a
canonical digest, and atomically changes the pending reason to an auditable
`preserved_ciphertext` marker. It does not change `users.doc.content_encryption`.

The marker remains in `tee_pending_device_migration`; it is not deleted. Phase
4 will gate on **blocking pending** rather than the physical row count. Strict
verification will treat a preserved marker as satisfied only when the exact
source and destination projections still hash identically.

This design preserves mixed historical row shapes. An unset/default-off user
may retain old ciphertext while future writes become plaintext after the
plaintext gate opens. Existing read code already routes by row shape, with
`body_ct` taking precedence over plaintext fields.

## Alternatives Considered

### Set all affected users to `content_encryption=on`

The current replicator would then carry their rows verbatim. This is rejected
as the default because a migration mechanism must not rewrite a user-level
product preference or force future writes to remain encrypted. It also does
not repair enclave decryption: a `decrypt_failed` envelope stays unreadable to
the agent after the preference changes.

### Delete or waive terminal pending rows

This makes the gate green without putting the source bytes in the promoted
database. It is rejected because it converts an observability condition into
silent data loss.

### Preserve only the five concentrated users

This removes most rows but leaves the cutover dependent on a second policy for
the long tail. The preservation operation is small enough to cover all 887
rows, so a concentration-based exception is unnecessary.

## Scope

Version 1 supports the four logical families currently present in PROD
terminal pending:

| Pending `table_name` | RDS source | TEE preservation target |
| --- | --- | --- |
| `chat_messages` | `chat_messages` | `chat_messages` |
| `memory_moments` | `memory_moments` | `memory_moments` |
| `identity` | `user_blobs WHERE kind='identity'` | `user_blobs WHERE kind='identity'` |
| `frame_envelopes` | `frame_envelopes` | `frame_envelopes` protocol bridge |

World Book and later ciphertext families are deliberately out of scope until a
real terminal row exists and its primary read contract is reviewed. Unknown
tables fail closed.

The operation does not call the enclave, fetch frame bodies from R2, mutate RDS,
or change account preferences. Frame pointers and inline envelopes are copied
as stored; Phase 4's frozen-source bridge copy remains the final authoritative
replacement.

## Preconditions

Apply mode requires all of the following:

1. The executing checkout is the exact release commit selected for promotion.
2. TEE PostgreSQL is at that release's single Alembic TEE head. For the current
   release this is `0025_lane_rollup_voice`, which creates the
   `frame_envelopes` bridge through its ancestry.
3. `DATABASE_URL`, `TEE_DATABASE_URL`, and `TEE_MIGRATION_DATABASE_URL` resolve
   to the intended source and destination; the two TEE URLs must share a
   fingerprint and the source must differ.
4. A fresh dry-run plan has produced an exact terminal count and plan SHA-256.
5. Apply receives the literal confirmation
   `PRESERVE-TERMINAL-CIPHERTEXT`, the expected count, and the expected plan
   digest.

Schema migration may therefore happen before application launch, but the old
TEE writer must be stopped before migrating through incompatible schema changes.
Preservation runs after the schema reaches the release head and before Phase 4
cutover.

## Marker Contract

A successful row changes its reason to:

```text
preserved_ciphertext:v1:<row-sha256>:<base64url-original-reason>
```

The digest covers a canonical JSON representation of:

- logical table name;
- user ID and item ID;
- the complete selected source row, including metadata and storage pointers.

JSON keys are sorted and timestamps use their stable string representation.
Bodies, keys, user IDs, and item IDs never appear in logs or workflow output.
Reports contain only aggregate counts, table names, plan digest, and database
fingerprints.

Keeping the original reason in the marker supports an exact pre-cutover revert.
The marker format is versioned and parsed strictly; malformed or unknown
versions remain blocking.

## Planning and Apply Flow

The implementation exposes a release-local CLI:

```bash
cd backend
python -m admin.tee_terminal_preservation
python -m admin.tee_terminal_preservation \
  --apply \
  --confirm PRESERVE-TERMINAL-CIPHERTEXT \
  --expected-count 887 \
  --expected-plan-sha256 "$DRY_RUN_PLAN_SHA256"
```

`DRY_RUN_PLAN_SHA256` is the exact 64-character digest printed by the
immediately preceding dry-run; an unset or malformed value is rejected.

Dry-run is the default and performs no writes. It:

1. validates database fingerprints and the TEE schema head;
2. reads terminal pending rows in stable `(table_name, user_id, item_id)` order;
3. fetches the exact RDS source row for every marker;
4. rejects missing users, missing source rows, unknown tables, malformed source
   shapes, and destination conflicts;
5. computes per-row digests and a plan digest without printing identifiers or
   content;
6. reports aggregate counts by table and terminal class.

Apply recomputes the complete plan under a repeatable-read source transaction.
It aborts before writing if count or digest differs from the operator-provided
values. Destination writes and marker updates occur in one TEE transaction, so
a row can never be marked preserved without its destination row.

For a destination key that already exists:

- exact canonical equality is idempotent only when the pending row already has
  a valid preservation marker;
- an unpreserved terminal row with an exact destination row is blocking because
  the tool cannot prove that it owns the row for a later revert;
- any different row is a conflict and blocks the entire operation;
- preservation never overwrites an existing plaintext projection.

If an RDS write races after the repeatable-read snapshot, the normal mirror
path replaces the preserved marker with a `requeue` reason. That row becomes
blocking again and must converge before Phase 4.

## Verification Changes

Strict verification splits pending rows into three groups:

- `requeue*`: active backlog and always blocking;
- terminal unpreserved (`decrypt_failed`, `pdm`, legacy local-only): missing
  projection accepted by the shadow count equation but blocking Phase 4;
- valid `preserved_ciphertext:v1` markers: not counted as a missing projection,
  and accepted only after deterministic source/destination raw equality.

For Chat, Memory, and Identity, the preserved row lives in the primary target
table. For Frames, preserved equality uses the `frame_envelopes` bridge, not the
storage-reencrypted `frames` projection. A preserved frame marker contributes
to the count equation only when the bridge row exists and its digest matches.

Strict verification audits every preserved row rather than sampling them. Any
missing row, digest drift, malformed marker, or source update makes the report
red and Phase 4-blocking.

## Phase 4 Gate

Phase 4 reports both:

- `tee_pending_device_migration_blocking`;
- `tee_terminal_ciphertext_preserved`.

Only the first is a blocker. The second is evidence and is embedded in the
prepared-marker report alongside its aggregate plan digest. The final
frozen-source `_copy_frame_bridge` still replaces the complete bridge and
verifies its full-row digest, so preserved Frames cannot be omitted during the
cutover copy.

The existing queue, Genesis, voice, outbox, and job drains remain unchanged.
No generic `pending == 0` check may remain elsewhere in the promotion path.

## Revert and Recovery

Before the first TEE-primary write, a guarded `--revert` mode may reverse this
operation. It requires the same plan digest and a literal
`REVERT-PRESERVED-CIPHERTEXT`. For each marker it verifies that the destination
row still matches the embedded digest, deletes only that exact preserved row,
and restores the decoded original terminal reason in one TEE transaction.

Revert refuses to run after the Phase 4 prepared marker exists or after any
destination row has changed. After TEE becomes primary, recovery follows the
normal TEE backup/reverse-reconciliation runbook; this CLI is no longer a valid
rollback tool.

The initial apply rejects exact pre-existing destination rows unless the
pending row already carries a valid preservation marker. Consequently, a valid
marker proves that this operation created the destination row and revert may
delete it without claiming unrelated pre-existing data.

## Security and Privacy

- The CLI uses the source role read-only and the TEE owner role only for the
  destination transaction.
- No plaintext is produced. Ciphertext and R2 pointers stay inside database
  connections.
- No row identifier, user identifier, body, key material, DSN, or original
  error text is logged.
- Unknown reason formats, unknown tables, missing parent users, and destination
  conflicts fail closed.
- The operation cannot set `content_encryption`, create users, delete RDS rows,
  or waive an unresolved marker.

## Tests and Acceptance Criteria

Automated tests must prove:

1. dry-run is read-only and produces stable counts/digests;
2. wrong confirm, count, plan digest, database fingerprint, or schema head
   rejects apply before writes;
3. each supported table preserves the exact source shape and marker;
4. identity maps to `user_blobs(kind='identity')` without touching other kinds;
5. Frames preserve `doc/env_meta/body_key` in the bridge without R2 or enclave
   calls;
6. missing source/user, unknown table, and different destination rows block the
   complete transaction;
7. exact existing rows make apply idempotent only with an existing valid
   preservation marker; unmarked exact rows block to preserve revert ownership;
8. strict verify audits all preserved rows and reports drift;
9. Phase 4 blocks unpreserved/requeue/malformed rows but permits valid preserved
   markers while reporting their count;
10. requeue overwrites a preserved marker after a later source mutation;
11. guarded pre-cutover revert restores the exact original reasons;
12. account `content_encryption` documents are byte-for-byte unchanged.

The focused TEE replication, verification, and Phase 4 suite must remain green.
The full backend suite, OpenAPI contract checks, documentation type/lint/build,
and deploy-YAML strict tests run before integration because this changes a
security boundary and production migration topology.

## Production Sequence

1. Merge the implementation into the release branch and repeat PRE dry-run,
   apply, strict verify, revert, and re-apply on synthetic terminal rows.
2. Stop the old PROD TEE writer and freeze schema-changing activity.
3. Back up and migrate PROD TEE PostgreSQL to the exact release head.
4. Run PROD preservation dry-run and record count plus plan digest.
5. Obtain explicit approval for the PROD write.
6. Apply preservation with exact compare-and-apply guards.
7. Run strict verification and Phase 4 dry-run; blocking pending must be zero.
8. Continue the single maintenance-window application/runner cutover.

No application release, preference mutation, pending deletion, or PROD apply is
authorized merely by merging this implementation.
