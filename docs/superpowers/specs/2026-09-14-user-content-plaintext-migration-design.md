# User Content Plaintext Migration Design

**Status:** Proposed

**Date:** 2026-09-14

## Problem

Changing a user's `content_encryption` preference to `off` changes the shape of
new writes but does not rewrite existing sealed content. A plaintext-tier user
can therefore retain thousands of `body_ct` or sealed object-pointer rows and
continue to depend on the enclave for old Chat, Memory, World Book, Identity,
and Frame reads.

The public `POST /v1/content/swap` route is not a complete migration mechanism.
It accepts caller-supplied replacement bodies for Chat and Memory only. It does
not cover Chat thinking/caption sub-envelopes, World Book, Identity, or Frames,
and an external loop would put the same expensive decrypt traffic back through
the public enclave path.

## Goals

- Provide one operator-owned, single-user migration command that runs inside
  the managed backend CVM.
- Convert every enclave-decryptable sealed content shape for a plaintext-tier
  user to its canonical plaintext shape.
- Preserve IDs, ordering, timestamps, metadata, visibility, attachment bytes,
  and per-surface semantics exactly.
- Skip rather than weaken `local_only` records.
- Be dry-run by default, idempotent, rate limited, resumable by stored shape,
  and safe under concurrent writes.
- Keep plaintext and key material out of command output and logs.

## Non-goals

- Do not change a user's encryption preference.
- Do not convert `local_only` content to `shared`.
- Do not add an all-users migration mode.
- Do not remove the enclave from nonblank Memory search; that is a separate
  read-path architecture change.
- Do not use the TEE shadow database as the migration source or authority.

## Considered Approaches

### External `/v1/content/swap` loop

This reuses a public API but requires user credentials, covers only Chat and
Memory main bodies, and creates thousands of public enclave calls. It cannot
finish the migration and can recreate the incident load pattern. Rejected.

### Direct SQL rewrite

Direct updates cannot safely decrypt bodies, manage R2 object lifecycle,
preserve shape invariants, or propagate changes through the normal storage
coordination paths. Rejected.

### Internal CVM migration tool

The tool reuses the replicator's user-scoped runtime-token decrypt callback and
the production storage helpers. Plaintext exists only inside the CVM process,
and each write follows a surface-specific compare-and-swap path. Selected.

## Architecture

Create two focused modules:

- `backend/content/plaintext_migration.py` contains inventory, shape conversion,
  per-surface adapters, compare-and-swap application, and aggregate results.
- `backend/migrate_user_content_to_plaintext.py` contains CLI parsing, safety
  gates, rate limiting, bounded execution, and content-free progress output.

The library receives a `user_id`, a decrypt callback, an apply flag, a limit,
and a rate. The CLI obtains the decrypt callback from
`tee_replicator.worker._make_decrypt(user_id)`, which mints a short-lived,
user-bound runtime token. It never loads or prints the user's API key.

The migration is naturally resumable: inventory selects only canonical sealed
shapes. Successfully converted rows no longer match. A rerun therefore skips
completed work without a separate mutable checkpoint file.

## Safety Gates

Apply mode requires all of the following:

1. An exact `--user usr_...` argument. There is no wildcard or all-users mode.
2. The target user exists.
3. The stored preference resolves to `off`.
4. The deployment accepts plaintext writes.
5. `--apply` is present.
6. `--allow-plaintext-rewrite` is present.
7. `FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION=1` is set in the execution
   environment.

Dry-run performs inventory and validation without decrypting bodies, writing
PostgreSQL, uploading objects, or adding cleanup records.

## Record Classification

Each main or sub-envelope is classified as one of:

- `already_plaintext`: canonical `body`, `body_b64`, or plaintext object marker.
- `migratable_shared`: sealed shape with a valid `K_enclave` and `shared`
  visibility.
- `skipped_local_only`: sealed shape with `local_only` visibility or without
  `K_enclave`.
- `invalid_shape`: conflicting, incomplete, or unknown persisted shape.

An invalid shape is never guessed or rewritten. Apply records the content-free
failure and continues with other rows; the process exits nonzero if any failure
or CAS loss remains.

## Surface Conversion

### Chat

Inline main bodies are decrypted and replaced with `body` for UTF-8 text or
`body_b64` for image/file bytes. Existing content type, role, source, sequence,
timestamps, reply links, and other metadata remain unchanged.

R2-backed image/file bodies reuse the existing guarded upload and CAS protocol
in `db.migrate_chat_r2_pointer_to_plaintext`. The plaintext is uploaded under a
fresh versioned key, the row is atomically changed to `plaintext_v1`, and the
old ciphertext object is retired through the durable cleanup queue.

The independent `thinking_*` and `caption_*` sub-envelopes are decrypted and
replaced by their canonical `thinking_body` and `caption_body` fields. Their
sealed fields are removed only in the same successful row CAS. A main-body
success must not silently claim a sub-envelope success.

### Memory, World Book, and Identity

Each decrypted byte string becomes the canonical top-level `body` while all
non-envelope metadata stays unchanged. Writes use the existing per-user
mutation/advisory locks and compare the original sealed shape before replacing
it, preventing a concurrent update from being overwritten.

### Frames

Inline or R2-backed encrypted frame bytes become the existing plaintext frame
storage shape. The new plaintext object is written before the metadata CAS; a
durable guard owns crash cleanup, and the old ciphertext object is retired only
after the authoritative row commits. Frame hashes and sizes are recomputed from
the decrypted bytes and checked on readback.

## Concurrency and Failure Handling

- New writes are already plaintext after the preference change and never enter
  the sealed inventory.
- Every mutation compares the original envelope fields or generation before it
  commits. A mismatch is `cas_lost`, not success.
- Processing is one record at a time, with a default rate of two successful
  decrypt attempts per second and an operator-set `--limit`.
- One failed record does not roll back prior successful records. Rerunning is
  the recovery mechanism.
- Runtime tokens are refreshed through the existing replicator mechanism rather
  than being persisted.
- Logs contain only user ID, surface, opaque item ID, status, counts, and elapsed
  time. They never contain bodies, ciphertext, wrapped keys, object keys, or
  credentials.

## CLI Contract

Proposed commands:

```bash
# Inventory only; performs no decrypt or write.
python migrate_user_content_to_plaintext.py --user usr_example

# Small production canary after deployment approval.
FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION=1 \
python migrate_user_content_to_plaintext.py \
  --user usr_example \
  --apply --allow-plaintext-rewrite \
  --limit 20 --rate 1

# Idempotent continuation.
FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION=1 \
python migrate_user_content_to_plaintext.py \
  --user usr_example \
  --apply --allow-plaintext-rewrite \
  --rate 2
```

The final JSON summary contains counts by surface and status only. Apply exits
zero only when the selected work completed without failure or CAS loss.

## Verification

Unit and PostgreSQL-backed tests must prove:

- dry-run has no decrypt, database, R2, or cleanup side effects;
- all apply gates fail closed;
- explicit encrypted-tier and unknown users cannot be migrated;
- main and sub-envelope conversions remove every stale crypto field;
- binary bytes round-trip exactly;
- `local_only` records remain byte-for-byte unchanged;
- a concurrent write produces `cas_lost` and is not overwritten;
- R2 crash windows retain either the old readable ciphertext or the new readable
  plaintext and leave cleanup ownership behind;
- rerunning after partial success only visits remaining sealed records;
- output never includes content or key material.

Before production use, deploy through `test`, run a representative sealed-data
fixture including R2 bodies and sub-envelopes, and verify pre/post inventories.
Production execution then proceeds as dry-run, a 20-record canary at one record
per second, post-canary read verification, and finally an idempotent continuation
at no more than two records per second. Enclave latency and CPU are watched
throughout; apply stops if health latency or error rate regresses.

## Initial Incident Target

For the first affected account, the expected starting inventory is 6,030 sealed
Chat main bodies, 361 Memory records, 30 World Book records, one Identity record,
and 43 Frames. Nine Chat records are `local_only` and must remain encrypted.
There are no archived Chat rows. These numbers are operational expectations,
not constants in code; the production dry-run is authoritative at execution
time.
