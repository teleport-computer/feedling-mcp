---
document_lifecycle: decision
canonical_owner: self
---
# TEE Primary Historical Ciphertext Repair Design

**Status:** Approved in conversation; implementation pending

**Date:** 2026-09-15

## Problem

PROD already uses the TEE PostgreSQL database as its authoritative primary, but
the primary still contains about 180,000 encrypted Chat rows and 28,000
encrypted Memory rows.  Phase 4's prepared marker records only 420 explicitly
preserved terminal ciphertext rows, so the bulk of the remaining ciphertext is
not explained by the audited terminal-preservation lane.

The migration entry point in `backend/tee_replicator/__main__.py` calls
`worker.run_table()` in a fresh process without first loading the account
registry.  `worker._carries_verbatim()` deliberately treats a user missing from
that in-memory registry as unknown and therefore copies ciphertext verbatim.
The documented pre-promotion procedure invokes this standalone entry point for
every ciphertext table.  A fresh-process reproduction observes zero loaded
users and classifies an otherwise existing user as carry-verbatim.

This failure mode is also capable of producing a false verification result:
`tee_shadow.verify` derives its expected row shape through the same
`_carries_verbatim()` helper.  A migration process and verifier with the same
empty registry can therefore agree that an unintended ciphertext row is
correct.

## Production Evidence

- The live backend has `DATABASE_URL` pointed at the PROD TEE PostgreSQL CVM,
  `FEEDLING_DATABASE_SCHEMA=tee`, and the legacy `TEE_DATABASE_URL` unset.
- The Phase 4 marker records 420 preserved terminal ciphertext rows.
- Current Chat ciphertext: 180,608 rows; 180,578 carry a shared envelope and an
  enclave-wrapped key, while 30 are local-only or lack an enclave key.
- Current Memory ciphertext: 28,803 rows; all 28,803 carry a shared envelope
  and an enclave-wrapped key.
- Using the final replication-cursor time as the promotion boundary, 177,813
  encrypted Chat rows and 26,135 encrypted Memory rows predate promotion.
- A fresh standalone process observes an empty registry and returns
  carry-verbatim for an arbitrary user ID.

The missing historical shell audit means the exact operator invocation cannot
be proven after the fact.  The code defect is reproducible, the documented
procedure selects the defective path, and the stored shape distribution
matches its predicted result; this is the high-confidence root cause.

## Goals

1. Make standalone replication and verification resolve content-encryption
   intent from authoritative database state rather than incidental process
   initialization.
2. Preserve ciphertext for explicit `content_encryption=on` users and for
   unknown users, while treating an existing user with an unset preference as
   the default plaintext tier.
3. Repair historical shared ciphertext for every effective-off user without
   changing IDs, ordering, timestamps, metadata, binary bodies, or user
   preferences.
4. Keep local-only, missing-key, and cryptographically undecryptable rows
   unchanged and auditable.
5. Make the operation resumable, rate limited, health gated, and independently
   verifiable so a repeated migration cannot produce another false green.

## Non-goals

- Do not rewrite or clear `content_encryption=on`.
- Do not turn local-only content into shared content.
- Do not delete preserved ciphertext merely to make an inventory green.
- Do not send bodies, wrapped keys, plaintext, or complete DSNs to logs.
- Do not enable the independent plaintext-shadow topology.
- Do not remove mixed-shape read compatibility in this change.

## Chosen Architecture

### Authoritative preference resolution

Add a database-backed resolver for replication policy.  It returns three
states from the selected source database:

- `on`: the user exists and explicitly opted into encryption;
- `off`: the user exists and is either explicitly off or unset;
- `None`: no authoritative user row exists.

`tee_replicator.worker` uses this resolver for carry/decrypt decisions and may
cache the result for the existing bounded TTL.  The ASGI registry remains an
application cache, but it is no longer a trust input for an offline migration.
An unknown user continues to fail safe to ciphertext and is surfaced in the
report; it is never silently treated as plaintext.

The standalone CLI performs a startup database probe before scanning content.
The probe must distinguish a genuinely empty user table from a failed read and
must abort on failure.  It reports only counts and selected schema mode.

### Independent verification policy

Verification resolves the expected tier through the authoritative resolver,
not through the worker's carry helper or cache.  This deliberately prevents a
single defective policy function from controlling both the write and its
oracle.

For effective-off users, verification expects canonical plaintext for every
non-terminal shared envelope.  For explicit-on users it expects exact
ciphertext preservation.  Unknown-user rows, local-only rows, missing-key rows,
and preserved terminal rows are separate non-green or explicitly exempt
classes; they cannot disappear into a generic skipped count.

### Historical repair coordinator

Extend the existing single-user plaintext migration rather than reviving the
old RDS-to-TEE shadow replicator.  The TEE primary is now authoritative, so the
repair must read and compare-and-swap rows in that database.

The coordinator selects existing users whose stored preference is `off` or
unset.  Before each user and each compare-and-swap it rechecks that the user is
not explicit-on.  It then delegates to the existing per-user migration library,
which already covers Chat live/archive bodies and sub-envelopes, Memory, World
Book, Identity, and Frames and preserves row identity and metadata.

The command is dry-run by default.  Apply mode requires the existing plaintext
rewrite gates plus a separate literal confirmation for an all-effective-off
run.  It supports deterministic user ordering, start-after user ID, per-user
and global limits, configurable QPS, and content-free progress counters.  A
rerun inventories stored shape and naturally skips completed rows.

## Alternatives Rejected

### Only call `accounts.registry.load_users()` in the CLI

This closes the immediate empty-process bug but leaves migration correctness
dependent on a mutable application cache and lets verification share the same
failure mode.  Keep the startup assertion as defense in depth, but use the
database-backed resolver as the authority.

### Set all unset users to explicit off

This would make the existing single-user gate easier to reuse, but it mutates
1,000 account documents solely for operator convenience and obscures the
product contract that unset already means default off.  The repair should
understand that contract directly.

### One unrestricted full-table replay

This would maximize enclave pressure, provide a poor restart boundary, and
repeat the operational shape that contributed to the incident.  User-scoped,
rate-limited repair gives bounded blast radius and useful progress evidence.

## Execution and Load Control

Roll out in three stages:

1. Deploy the resolver and verification fixes with no production mutation.
   Run focused tests and read-only inventory in TEST, then PROD.
2. Apply the historical repair to the already affected resident user and a
   small set of recently active effective-off canaries.  Verify history reads,
   ciphertext deltas, failure classes, and enclave latency before expanding.
3. Process remaining effective-off users in deterministic batches, one user at
   a time.

Start at 0.5 to 1 decrypt operation per second with no concurrent batch.  Pause
admission when the enclave health probe fails, crosses the configured latency
ceiling, or the rolling decrypt error rate exceeds its ceiling.  An in-flight
row may finish, but no new user begins until a configured sequence of healthy
probes passes.  Operators can resume from the last completed user ID.

At roughly 209,000 currently encrypted Chat and Memory rows, one decrypt per
second requires about 58 hours before accounting for other surfaces and
retries.  Runtime estimates are reporting aids, not reasons to raise load while
the enclave is unhealthy.

## Safety and Failure Semantics

- Every content mutation remains compare-and-swap protected against the exact
  original encrypted shape.
- The user tier is rechecked under the existing per-user mutation boundary.
- A concurrent switch to explicit-on prevents subsequent plaintext writes for
  that user and records a content-free tier-change stop.
- A decrypt failure leaves the original row untouched and records only table,
  user, item identifier, and fixed error class.
- Local-only and missing-key rows remain encrypted and are counted separately.
- Explicit-on rows are never submitted to the decrypt callback.
- Rate-limit and health-gate state is process local; durable resumability comes
  from stored row shape and deterministic user ordering, not from an unsafe
  mutable checkpoint.
- The command exits nonzero when any unexpected failure, compare-and-swap loss,
  unknown user, or verification mismatch remains.

## Testing

Add focused regression coverage for:

- a fresh standalone process resolving an existing unset user as off;
- unknown users remaining carry-verbatim and visible in the report;
- explicit-on users never invoking decrypt;
- CLI startup aborting when the authoritative user read fails;
- verification using an independent resolver and rejecting ciphertext for an
  effective-off user even when the worker cache says carry-verbatim;
- bulk selection including unset/off and excluding on;
- tier changes during apply stopping that user's migration;
- deterministic resume and limits;
- health-gate pause/resume without duplicate mutation;
- local-only, decrypt failure, and CAS-loss accounting;
- an end-to-end mixed-user fixture proving on remains ciphertext and off/unset
  converges to plaintext.

Run the focused suites with a real PostgreSQL test database, then the backend
L1 suite selected by `docs/testing/TESTING.md`.  A dry-run against TEST and a
small TEST apply batch are required before production deployment.

## Production Completion Criteria

- Explicit-on ciphertext counts are unchanged except for normal user writes.
- Effective-off shared ciphertext reaches zero for every supported surface.
- Remaining ciphertext is fully explained by explicit-on, local-only,
  missing-key, or preserved decrypt-failure classes.
- Chat and Memory plaintext gains reconcile with ciphertext reductions and no
  row identities disappear.
- Random canary history reads complete without enclave history decryption for
  migrated shared rows.
- Enclave latency and error rate stay within the recorded rollout ceilings.
- The corrected verifier is green independently of the migration worker cache.
