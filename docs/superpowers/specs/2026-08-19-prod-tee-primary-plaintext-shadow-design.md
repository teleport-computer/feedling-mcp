# PROD TEE Primary and Plaintext Shadow Design

## Status

Approved in conversation on 2026-08-19. This document defines the design; it
does not authorize a production cutover until every release gate below is
green.

## Context

PROD currently uses the managed database as primary and mirrors data into the
existing TEE PostgreSQL database. The target topology promotes that existing
TEE database to primary and uses the separately provisioned Teleport PG17
cluster ("TEE SQL") as a decrypted plaintext shadow.

The plaintext shadow has the same content semantics requested for the current
TEE mirror: content that is encrypted at rest in the primary is decrypted
through the enclave before it is written to the shadow. This includes content
owned by users whose effective `content_encryption` preference is `on`.
Consequently, TEE SQL, its backups, credentials, operators, and observability
all enter the sensitive plaintext trust boundary.

The supplied TEE SQL connection document is not currently sufficient for a
release. Its documented credential paths disagree with the credential file on
disk, the application-facing least-privilege roles do not exist in the
document, and the advertised TLS gateway currently closes connections during
the TLS handshake. These are hard blockers for the plaintext-shadow gate, but
not for preparing code or promoting the existing TEE database independently.

## Goals

- Promote the existing PROD TEE database to the only authoritative primary.
- Add a separately named, independently guarded decrypted plaintext shadow.
- Preserve successful primary writes when the plaintext shadow is unavailable.
- Make every missed shadow write observable, durable, idempotently replayable,
  and verifiably convergent.
- Rotate away from the existing shared administrator credential without a
  credential outage.
- Perform the primary promotion and shadow activation as two explicit gates in
  the same maintenance window or release train.

## Non-goals

- TEE SQL is not a synchronous high-availability standby and cannot be promoted
  automatically.
- The frozen pre-promotion managed database is not a lossless rollback target
  after the first write to the TEE primary.
- PostgreSQL logical replication is not used because it cannot perform the
  required enclave decryption.
- This change does not rewrite the user-facing encryption preference or alter
  the primary database's content-tier behavior.

## Chosen architecture

Use a new plaintext-shadow abstraction and new configuration names. Do not
reverse the meaning of `TEE_DATABASE_URL`: that name describes the old
managed-primary-to-TEE-shadow topology and is too easy to configure as a
self-write after promotion.

The target topology is:

```text
API / Runtime / workers
          |
          v
Existing PROD TEE PostgreSQL (authoritative primary)
          |
          | durable sync intent + enclave decryption
          v
Teleport PG17 / TEE SQL (decrypted plaintext shadow)
```

The implementation should expose purpose-specific configuration, with final
names validated against existing conventions during planning:

- `PLAINTEXT_SHADOW_DATABASE_URL`: least-privilege application/sync DSN.
- `PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL`: owner-only migration DSN.
- `FEEDLING_PLAINTEXT_SHADOW_ENABLED`: explicit runtime gate, default `0`.

Startup must reject a shadow DSN that resolves to the primary DSN, reject an
enabled shadow with a missing DSN, and reject a schema version below the release
head. Main and runner receive the same primary DSN and schema mode. Only the
elected sync worker owns reconciliation so worker count does not multiply
shadow pressure.

## Write and reconciliation contract

The primary transaction is authoritative. A shadow outage must not turn a
valid user write into an API failure. At the same time, best-effort writes that
silently disappear are not acceptable.

Each shadow-relevant primary mutation must produce a durable, replayable sync
intent. The fast path may attempt the shadow write after the primary commit,
but failure leaves the intent pending with a structured error, attempt count,
and next retry time. The reconciler applies intents idempotently by entity key
and source version. An older intent must never overwrite a newer shadow value.

Deletes use durable tombstones or an equivalent source-versioned delete intent.
This prevents a row deleted during a shadow outage from reappearing when stale
upserts are replayed. Poison records are quarantined after bounded retry while
remaining visible in health and administrative inspection; they are never
reported as converged.

Decryption occurs only in the enclave-backed application path. Logs, metrics,
error strings, and reconciliation diagnostics may contain identifiers and
shape metadata but never plaintext content, content keys, passwords, or DSNs.

## Initial population and convergence

Gate 2 begins with the runtime shadow gate disabled. The release creates the
shadow schema at the exact release head, then performs a resumable full copy
from the TEE primary. Encrypted source fields are decrypted through the enclave
and stored in their plaintext shadow representation. The copy records a source
high-water mark, then the reconciler drains mutations after that mark.

Activation requires all of the following:

- Migration head matches the application release.
- Full-copy checkpoints are complete for every supported table family.
- Pending and failed intents are zero, including delete intents.
- Per-table row and primary-key-set checks converge.
- Source-version checks find no shadow row ahead of or behind its primary row.
- Content-shape checks find plaintext fields in the shadow and no unexpected
  ciphertext envelope for content required to be decrypted.
- Scoped content digests or enclave-side comparisons pass without printing
  content.
- Backup creation and a restore drill have succeeded for TEE SQL.

## Security and credential rotation

The documented `teleport_admin` credential is bootstrap-only. Production uses
separate roles:

- An owner/migration role that is not present in application containers.
- A sync role limited to the required shadow schema and DML operations.
- A read-only audit role if operational inspection is required.

Password rotation is staged, not performed as an in-place first step:

1. Restore and verify the TEE SQL TLS gateway and database health.
2. Authenticate with the bootstrap administrator without logging the secret.
3. Create or rotate the purpose-specific roles with new random credentials.
4. Store the new DSNs in the approved deployment secret store.
5. Verify migration and sync access, deploy with the gate disabled, and run the
   full-copy/convergence checks.
6. Enable the shadow and observe it under normal traffic.
7. Rotate or disable the old administrator password after confirming there are
   no remaining consumers; update the private operator record without placing
   credentials in Git.

TLS certificate verification and hostname verification are mandatory over the
Internet-facing hop. A loopback TLS wrapper is acceptable for operator access,
but production must use a supervised equivalent with health checks rather than
an ad-hoc terminal process.

## Release sequence

### Preflight

- Deploy and validate the new code path in TEST and PRE with independent
  primary and shadow databases.
- Confirm the release's TEE migration head on both PROD databases.
- Repair the TEE SQL endpoint and complete its backup/restore drill.
- Capture primary backup and restore evidence, convergence evidence, freeze
  owner, rollback owner, and exact secret/config diffs.
- Run the explicit `on` and `off` account regression across all documented
  content surfaces.

### Gate 1: promote the existing TEE database

1. Close plaintext writes if required by the existing migration runbook.
2. Freeze API and worker writes and drain all durable queues.
3. Run Phase 4 dry-run and apply from the exact release.
4. Point API, in-CVM workers, and independent runner at the existing TEE
   database with `FEEDLING_DATABASE_SCHEMA=tee`.
5. Keep `FEEDLING_PLAINTEXT_SHADOW_ENABLED=0` and remove the legacy TEE-shadow
   configuration.
6. Resume traffic only after schema, marker, queue, API, worker, encryption,
   backup, and two-account checks pass.

Gate 1 may complete while Gate 2 remains blocked. This is a supported and
stable state: the TEE database is primary and no new plaintext shadow is
enabled.

### Gate 2: activate TEE SQL plaintext shadow

1. Migrate TEE SQL with its owner credential while the runtime gate is off.
2. Run the resumable decrypted full copy and reconcile from its high-water
   mark.
3. Pass every convergence, shape, credential, TLS, backup, and restore gate.
4. Enable `FEEDLING_PLAINTEXT_SHADOW_ENABLED=1` on the elected sync worker.
5. Observe live writes, retries, deletes, queue depth, and lag before declaring
   the gate complete.
6. Finish the administrator credential rotation only after the observation
   period.

## Failure and rollback matrix

| State | Failure | Operator action | Data authority |
| --- | --- | --- | --- |
| Before Gate 1 | Phase 4 or freeze checks fail | Abort and resume the original topology | Original managed primary |
| Gate 1 switched, before first write | Startup/canary fails | Correct config or switch back while still frozen | Cutover evidence decides |
| Gate 1 after first TEE-primary write | API/worker issue | Keep TEE primary; roll back application release or restore/reconcile TEE | Existing TEE primary |
| Gate 2 full copy | Copy, decrypt, TLS, or restore check fails | Leave shadow disabled; resume checkpoints after repair | Existing TEE primary |
| Gate 2 active | TEE SQL unavailable or lagging | Disable fast-path attempts if needed; retain durable intents and reconcile later | Existing TEE primary |
| Gate 2 divergent | Version/digest/delete check fails | Disable shadow, quarantine affected ranges, repair and replay from primary | Existing TEE primary |

TEE SQL is a derived plaintext copy, not an automatic rollback database. Any
future decision to promote it requires a separate design, proof that all
primary-only state is represented, and an explicit security decision.

## Observability and acceptance criteria

Expose environment-scoped metrics and health for:

- Last successful shadow mutation and reconciliation poll.
- Oldest pending intent age, pending count, retry rate, and quarantined count.
- Per-table copy checkpoint and convergence state.
- Decryption failures categorized without content.
- Shadow connection/TLS health and pool pressure.
- Backup age and latest restore-drill result.

The release is accepted only when TEST and PRE evidence is green, Gate 1 PROD
canaries are green with the shadow off, and Gate 2 remains converged during the
agreed observation period. Restart tests must prove that pending intents and
copy checkpoints survive process replacement. Fault-injection tests must cover
shadow timeout, credential rejection, stale replay, duplicate replay, delete
reordering, malformed ciphertext, and recovery after an extended outage.

## Required repository changes

Implementation planning must cover:

- A generic plaintext-shadow pool and configuration boundary.
- Durable source-versioned sync intents and tombstones for all supported data
  families, reusing proven existing primitives where their semantics match.
- Reconciler election, retry/quarantine behavior, health, and metrics.
- Shadow migrations and resumable full-copy tooling.
- CI/deployment validation that permits the new topology while rejecting
  primary/shadow aliasing and incomplete credentials.
- Unit, integration, fault-injection, TEST, PRE, and PROD canary procedures.
- Updates to the content-encryption migration runbook, public architecture and
  trust-boundary documentation, self-hosting guidance, diagrams, changelog, and
  generated OpenAPI artifacts only where the public API contract changes.

## Current external blockers

- The advertised TEE SQL gateway does not currently complete a TLS handshake
  from the operator machine.
- The connection document references inconsistent credential paths and does
  not describe a supervised production tunnel or application roles.
- TEE SQL schema head, capacity, HA behavior, backup status, restore evidence,
  and monitoring have not yet been verified.

These blockers prevent Gate 2 activation and password rotation. They do not
justify weakening TLS verification, embedding the administrator password in
deployment configuration, or coupling Gate 2 success to Gate 1 promotion.
