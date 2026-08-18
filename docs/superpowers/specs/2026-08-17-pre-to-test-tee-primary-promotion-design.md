# PRE to TEST Optional Encryption and TEE-Primary Promotion Design

## Goal

Bring PRE's validated encrypted/plaintext content-shape implementation into
`test`, preserve TEST-only runtime changes and deployment identity, and promote
TEST from RDS-primary plus TEE shadow writes to TEE PostgreSQL primary only
after an independently verified cutover gate.

## Current State

- `origin/test` contains TEST-only Runtime V2 fixes that are not all present in
  `origin/pre`.
- `origin/pre` contains the optional content-encryption implementation, strict
  plaintext enclave boundary, binary media fixes, TEE-primary schema, and PRE
  deployment hardening.
- TEST currently uses `TEST_DATABASE_URL` as `DATABASE_URL`, points
  `TEST_TEE_DATABASE_URL` at the TEE PostgreSQL shadow, and has dual-write
  enabled.
- PRE and TEST have different CVMs, database credentials, certificates,
  buckets, deployment pins, and environment-specific workflow inputs.

## Scope Decomposition

This promotion has two independently reviewable stages. Stage A is a code and
schema-capability convergence. Stage B is an operational database cutover.
Stage B is forbidden until Stage A is deployed and its evidence is accepted.

### Stage A: Code Convergence With RDS Still Primary

Create an integration branch from the latest `origin/test`, merge
`origin/pre`, and resolve conflicts by preserving both sides' intended
behavior:

- retain TEST-only Runtime V2 fixes;
- add PRE's optional encryption, plaintext routing, binary media, TEE schema,
  migration convergence, and preflight capabilities;
- retain TEST CVM IDs, TEST public endpoints, TEST secrets, TEST image pins,
  and TEST branch triggers;
- never substitute PRE secrets or infrastructure identifiers into TEST;
- keep `TEST_DATABASE_URL` as `DATABASE_URL` during the first deployment;
- keep TEST TEE dual-write enabled until cutover preparation begins;
- keep plaintext writes closed for the first compatibility deployment.

The merge must not be pushed directly to `test`. It is reviewed and tested on
the integration branch, then merged through the normal TEST branch workflow.

### Stage B: TEST TEE-Primary Promotion

After Stage A is healthy in TEST:

1. Verify the TEST RDS and TEE migration heads against the exact release.
2. Reconcile and strictly verify RDS-to-TEE data without printing content,
   credentials, or DSNs.
3. Run the Phase 4 dry run and confirm that queues, sequence bridges, runtime
   contracts, and prepared markers are ready.
4. Freeze writes and drain API, Runtime V2, Genesis, voice, action, outbox, and
   device-migration work.
5. Run Phase 4 apply using the TEST TEE owner DSN.
6. Atomically switch both the main CVM and runner to the TEST TEE app DSN and
   `FEEDLING_DATABASE_SCHEMA=tee`.
7. Remove TEE shadow dual-write configuration after promotion.
8. Keep plaintext writes closed for the first TEE-primary smoke test, then open
   them as a separate reversible configuration change.

After the first TEE-primary write, switching blindly back to the frozen RDS is
not a valid rollback. Rollback requires reverse reconciliation or restoration
of post-cutover TEE writes.

## Merge Resolution Rules

The known overlapping implementation files are Runtime V2 job storage and
worker behavior plus their tests and public documentation. Conflict resolution
must combine TEST's later watchdog/retry semantics with PRE's row-shape routing;
choosing either branch wholesale is not acceptable.

Migration files are additive except where explicit merge revisions converge
the RDS and TEE heads. The resulting release must expose exactly one RDS head
and exactly one TEE head, and the TEST live databases must either already be at
those heads or be migrated before deployment.

Generated deployment pin commits from PRE are historical evidence, not TEST
configuration. TEST pins are generated only by the TEST deployment workflow.

## Safety Invariants

- `content_encryption_effective`, not the raw preference, selects new writes.
- Unknown users and unavailable preference lookups fail closed to encryption.
- Reads select by stored row shape, so mixed historical rows remain readable.
- Plaintext content never enters enclave decrypt/read routes.
- Encrypted uploads from older clients remain accepted for plaintext accounts.
- `local_only` continues to require encryption.
- Main and runner always use the same primary database and schema mode.
- TEST and PRE credentials, certificates, object storage, CVMs, and domains
  remain isolated.

## Verification

### Stage A gates

- Merge conflict tests prove Runtime V2 retry/watchdog behavior and optional
  content-shape routing coexist.
- RDS and TEE Alembic trees each have one expected head.
- Migration-convergence and deployment-preflight tests pass.
- Focused encryption/plaintext tests cover Chat text, images, PDFs, Perception,
  screen frames, Memory, World Book, Genesis, Runtime V2, and mixed-row reads.
- The full PostgreSQL-backed backend suite passes without silently skipped DB
  modules.
- OpenAPI contract tests and documentation checks pass because this changes
  public behavior, trust boundaries, and deployment topology.
- The first TEST deployment reports RDS primary, TEE shadow dual-write, and a
  closed plaintext-write gate.

### Stage B gates

- Strict reconciliation reports no unexplained missing, extra, or mismatched
  rows.
- Phase 4 dry run is clean and the maintenance window has an assigned rollback
  owner.
- Main and runner both start against the same TEST TEE app DSN and schema head.
- Public API, custom enclave domain, direct attested enclave endpoint, and
  runner health checks pass.
- Separate encrypted and plaintext canary accounts pass text, image, PDF,
  Perception, screen, Memory, World Book, Runtime V2, and cross-user isolation
  checks.
- Container restart counts remain zero and active/failed queues are inspected.

## Delivery and Approval Gates

- Pushing the integration branch is allowed after local verification.
- Merging or pushing to `test` requires review of Stage A evidence.
- Starting the write freeze, Phase 4 apply, changing GitHub Secrets, or
  deploying TEE-primary requires a separate explicit approval after the dry-run
  report.
- No push to `main` is part of this work.

